from __future__ import annotations

import asyncio
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable

from ..config import ClientConfig, MediaProfile
from ..errors import FreshReopenRequired, ProtocolError, SessionClosed, TransportError
from ..events import SessionEvent
from ..protocol import (
    FLAG_AUDIO,
    MediaFrame,
    PlaybackTarget,
    build_session_open,
    decode_control,
    encode_control,
    parse_session_opened,
)
from ..trace import NullTrace, TraceSink
from ..transport import BootstrapGrant, DirectorClient, UdpMediaTransport, WssTransport
from .state import SessionState


class DesktopSession:
    """Single owner of bootstrap, transport selection and RVA endpoint state."""

    _CLOSE_CONTROL_TIMEOUT_SECONDS = 1.0
    _TEARDOWN_TIMEOUT_SECONDS = 6.0

    def __init__(
        self,
        config: ClientConfig,
        *,
        director: DirectorClient | None = None,
        wss_factory: Callable[[], WssTransport] = WssTransport,
        udp_factory: Callable[[], UdpMediaTransport] | None = None,
        trace: TraceSink | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self._trace = trace or NullTrace()
        self._monotonic = monotonic
        self._director = director or DirectorClient(config, trace=self._trace)
        self._wss_factory = wss_factory
        self._udp_factory = udp_factory or (
            lambda: UdpMediaTransport(
                trace=self._trace,
                max_media_age_seconds=config.media_max_age_seconds,
                probe_retry_seconds=config.udp_probe_retry_seconds,
            )
        )
        self._grant: BootstrapGrant | None = None
        self._wss: WssTransport | None = None
        self._udp: UdpMediaTransport | None = None
        self._state: SessionState | None = None
        self._uplink_sequence = 0
        self._uplink_timestamp = 0
        self._event_backlog: deque[SessionEvent] = deque()
        self._wss_fresh_generation: int | None = None
        self._wss_fresh_timestamp = 0
        self._wss_fresh_arrival = 0.0

    @property
    def selected_profile(self) -> MediaProfile | None:
        return self._state.opened.selected_profile if self._state is not None else None

    @property
    def state(self) -> SessionState:
        if self._state is None:
            raise SessionClosed("session has not been opened")
        return self._state

    async def connect(self) -> SessionEvent:
        if self._wss is not None:
            raise TransportError("session_already_open")
        grant = await self._director.bootstrap()
        wss = self._wss_factory()
        udp: UdpMediaTransport | None = None
        try:
            await wss.open(grant.worker_wss_url, grant=grant.connect_grant, device_id=self.config.device_id)
            request_id = f"desktop-{secrets.token_hex(8)}"
            await wss.send_control(encode_control(build_session_open(self.config, request_id)))
            wire = await asyncio.wait_for(wss.receive(), timeout=self.config.control_timeout_seconds)
            if not isinstance(wire, str):
                raise ProtocolError("expected_session_opened")
            opened_message = decode_control(wire)
            opened = parse_session_opened(opened_message, request_id=request_id)
            if opened.session_epoch != grant.session_epoch:
                raise ProtocolError("session_epoch_mismatch")
            if opened.selected_profile not in grant.allowed_profiles:
                raise ProtocolError("unsupported_selection")
            if opened.selected_profile is MediaProfile.UDP_OPUS_GCM_V1:
                assert opened.udp_grant is not None
                udp = self._udp_factory()
                await udp.open(opened.udp_grant, media_id=opened.media_id, media_epoch=opened.media_epoch)
        except BaseException:
            await self._rollback_connect(grant, wss, udp)
            raise
        state = SessionState(opened)
        if udp is not None:
            udp.set_media_validator(state.validate_media_admission)
        self._grant = grant
        self._wss = wss
        self._udp = udp
        self._state = state
        self._uplink_sequence = 0
        self._uplink_timestamp = 0
        self._event_backlog.clear()
        self._wss_fresh_generation = None
        self._trace.emit(
            "session.opened",
            {
                "session_id": opened.session_id,
                "session_epoch": opened.session_epoch,
                "selected_profile": opened.selected_profile.value,
            },
        )
        return SessionEvent("session.opened", opened_message)

    async def _rollback_connect(
        self,
        grant: BootstrapGrant,
        wss: WssTransport,
        udp: UdpMediaTransport | None,
    ) -> None:
        cleanups = []
        if udp is not None:
            cleanups.append(udp.close())
        cleanups.extend(
            (
                wss.close(code=1008, reason="handshake_failed"),
                self._director.release(grant),
            )
        )
        results = await asyncio.gather(*cleanups, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                self._trace.emit("session.connect_cleanup.failed", {"error_type": type(result).__name__})

    async def send_opus(self, payload: bytes, *, samples: int = 960) -> None:
        if samples != 960:
            raise ValueError("RVA Protocol 1.0 requires one 60 ms / 960 sample Opus packet")
        state = self.state
        if state.closed:
            raise SessionClosed()
        if state.opened.selected_profile is MediaProfile.UDP_OPUS_GCM_V1:
            assert self._udp is not None
            await self._udp.send_audio(payload, timestamp=self._uplink_timestamp)
        else:
            frame = MediaFrame(
                FLAG_AUDIO,
                state.opened.media_id,
                state.opened.media_epoch,
                self._uplink_sequence,
                self._uplink_timestamp,
                0,
                payload,
            )
            await self._wss_required().send_media(frame.encode_plain())
        self._uplink_sequence += 1
        self._uplink_timestamp = (self._uplink_timestamp + samples) & 0xFFFFFFFF

    async def send_keepalive(self) -> None:
        state = self.state
        if state.closed:
            raise SessionClosed()
        if state.opened.selected_profile is MediaProfile.UDP_OPUS_GCM_V1:
            assert self._udp is not None
            await self._udp.send_keepalive(timestamp=self._uplink_timestamp)

    async def next_event(self) -> SessionEvent:
        state = self.state
        if state.closed:
            raise SessionClosed()
        if self._event_backlog:
            return self._event_backlog.popleft()
        if state.opened.selected_profile is MediaProfile.WSS_OPUS_V1:
            while True:
                event = self._from_wss(await self._wss_required().receive())
                if event is not None:
                    return event
        assert self._udp is not None
        while True:
            if self._udp.refresh_due:
                return SessionEvent("transport.reopen_required", {"reason": "udp_grant_refresh"})
            control = asyncio.create_task(self._wss_required().receive(), name="desktop-control-recv")
            media = asyncio.create_task(self._udp.receive_audio(), name="desktop-udp-recv")
            done, pending = await asyncio.wait({control, media}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            events: list[SessionEvent] = []
            # Control is projected first when both sources become ready together.
            if control in done:
                try:
                    value = await control
                except FreshReopenRequired:
                    events.append(SessionEvent("transport.reopen_required", {"reason": "udp_grant_refresh"}))
                else:
                    event = self._from_wss(value)
                    if event is not None and event.kind == "session.close":
                        if media in done:
                            await asyncio.gather(media, return_exceptions=True)
                        return event
                    if event is not None:
                        events.append(event)
            if media in done:
                try:
                    value = await media
                except FreshReopenRequired:
                    events.append(SessionEvent("transport.reopen_required", {"reason": "udp_grant_refresh"}))
                else:
                    event = state.accept_media(value)
                    if event is not None:
                        events.append(event)
            if events:
                self._event_backlog.extend(events[1:])
                return events[0]

    async def playback_started(self, target: PlaybackTarget, first_media_sequence: int) -> None:
        await self._send_control(self.state.playback_started(target, first_media_sequence))

    async def playback_ended(
        self,
        target: PlaybackTarget,
        *,
        outcome: str,
        played_samples: int,
        last_media_sequence: int | None,
    ) -> None:
        await self._send_control(
            self.state.playback_ended(
                target,
                outcome=outcome,
                played_samples=played_samples,
                last_media_sequence=last_media_sequence,
            )
        )

    async def request_cancel(self, target: PlaybackTarget) -> None:
        request_id = f"cancel-{secrets.token_hex(8)}"
        await self._send_control(self.state.cancel_request(target, request_id))

    async def reopen(self) -> SessionEvent:
        await self._close_active(reason="network_change", detail="fresh_reopen", release=True)
        return await self.connect()

    async def close(self, *, reason: str = "normal", detail: str | None = None) -> None:
        cleanup = asyncio.create_task(
            self._bounded_teardown(self._close_all(reason=reason, detail=detail)),
            name="desktop-session-close",
        )
        await self._await_cleanup(cleanup)

    async def _close_all(self, *, reason: str, detail: str | None) -> None:
        failures: list[Exception] = []
        cancelled: asyncio.CancelledError | None = None
        try:
            await self._close_active_impl(reason=reason, detail=detail, release=True)
        except asyncio.CancelledError as exc:
            cancelled = exc
        except Exception as exc:
            failures.append(exc)
        try:
            await asyncio.wait_for(
                self._director.close(),
                timeout=self._CLOSE_CONTROL_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            failures.append(exc)
        if cancelled is not None:
            raise cancelled
        if failures:
            raise ExceptionGroup("desktop session close failed", failures)

    def _from_wss(self, wire: str | bytes) -> SessionEvent | None:
        if isinstance(wire, bytes):
            if self.state.opened.selected_profile is not MediaProfile.WSS_OPUS_V1:
                raise ProtocolError("transport_mismatch")
            frame = MediaFrame.decode_plain(wire)
            admitted = self.state.validate_media_admission(frame)
            if admitted and not self._wss_media_is_fresh(frame):
                self._trace.emit(
                    "wss.audio.stale",
                    {"sequence": frame.sequence, "generation": frame.generation},
                )
                return SessionEvent("transport.reopen_required", {"reason": "stale_media"})
            return self.state.accept_media(frame)
        return self.state.accept_control(decode_control(wire))

    def _wss_media_is_fresh(self, frame: MediaFrame) -> bool:
        now = self._monotonic()
        if self._wss_fresh_generation != frame.generation:
            self._wss_fresh_generation = frame.generation
            self._wss_fresh_timestamp = frame.timestamp
            self._wss_fresh_arrival = now
            return True
        media_elapsed = ((frame.timestamp - self._wss_fresh_timestamp) & 0xFFFFFFFF) / 16_000
        age = now - self._wss_fresh_arrival - media_elapsed
        return age <= self.config.media_max_age_seconds

    async def _send_control(self, message: dict[str, object]) -> None:
        await self._wss_required().send_control(encode_control(message))

    async def _close_active(self, *, reason: str, release: bool, detail: str | None = None) -> None:
        cleanup = asyncio.create_task(
            self._bounded_teardown(
                self._close_active_impl(reason=reason, release=release, detail=detail)
            ),
            name="desktop-active-session-close",
        )
        await self._await_cleanup(cleanup)

    async def _close_active_impl(self, *, reason: str, release: bool, detail: str | None = None) -> None:
        state, wss, udp, grant = self._state, self._wss, self._udp, self._grant
        control_wire: str | None = None
        failures: list[Exception] = []
        if state is not None and not state.closed and wss is not None:
            try:
                control_wire = encode_control(state.close_message(reason, detail=detail))
            except Exception as exc:
                failures.append(exc)
        try:
            if control_wire is not None and wss is not None:
                try:
                    await asyncio.wait_for(
                        wss.send_control(control_wire),
                        timeout=self._CLOSE_CONTROL_TIMEOUT_SECONDS,
                    )
                except Exception as exc:
                    self._trace.emit("session.close_control.failed", {"error_type": type(exc).__name__})
            cleanups = []
            if udp is not None:
                cleanups.append(udp.close())
            if wss is not None:
                cleanups.append(wss.close(reason=reason))
            if release and grant is not None:
                cleanups.append(self._director.release(grant))
            for result in await asyncio.gather(*cleanups, return_exceptions=True):
                if isinstance(result, Exception):
                    failures.append(result)
                elif isinstance(result, BaseException):
                    raise result
        finally:
            if self._state is state:
                self._state = None
            if self._wss is wss:
                self._wss = None
            if self._udp is udp:
                self._udp = None
            if self._grant is grant:
                self._grant = None
        if failures:
            raise ExceptionGroup("active desktop session cleanup failed", failures)

    @staticmethod
    async def _await_cleanup(cleanup: asyncio.Task[None]) -> None:
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            try:
                await cleanup
            except Exception:
                pass
            raise

    async def _bounded_teardown(self, teardown: Awaitable[None]) -> None:
        try:
            await asyncio.wait_for(teardown, timeout=self._TEARDOWN_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise TransportError("teardown_timeout", retryable=True) from exc

    def _wss_required(self) -> WssTransport:
        if self._wss is None:
            raise SessionClosed()
        return self._wss
