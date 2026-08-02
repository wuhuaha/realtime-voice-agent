from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from .audio import WIRE_SAMPLES_PER_FRAME, AudioSink, AudioSource, PcmFrame
from .config import MediaProfile
from .errors import FreshReopenRequired, ProtocolError, RvaClientError, TransportError
from .events import SessionEvent
from .protocol import PlaybackTarget
from .session import DesktopSession


class OpusCodec(Protocol):
    def encode_60ms(self, pcm16le: bytes) -> bytes: ...

    def decode_60ms(self, payload: bytes) -> bytes: ...

    def conceal_60ms(self) -> bytes: ...

    def close(self) -> None: ...


EventHandler = Callable[[SessionEvent], Awaitable[None] | None]
SinkFactory = Callable[[], AudioSink]


@dataclass(frozen=True, slots=True)
class RunResult:
    uplink_frames: int
    playback_frames: int
    completed_playbacks: int
    source_exhausted: bool


@dataclass(slots=True)
class _Playback:
    target: PlaybackTarget
    started: bool = False
    ended: bool = False
    played_samples: int = 0
    last_media_sequence: int | None = None
    last_media_timestamp: int | None = None
    final_media_sequence: int | None = None
    completion_deadline_at: float | None = None


_MAX_PLC_GAP_FRAMES = 4


class DesktopApp:
    """Composes one DesktopSession with transport-neutral audio ports."""

    def __init__(
        self,
        session: DesktopSession,
        *,
        source: AudioSource,
        sink: AudioSink,
        codec: OpusCodec,
        sink_factory: SinkFactory | None = None,
        on_event: EventHandler | None = None,
    ) -> None:
        self._session = session
        self._source = source
        self._sink = sink
        self._codec = codec
        self._sink_factory = sink_factory
        self._on_event = on_event
        self._session_operation = asyncio.Lock()
        self._reopen_operation = asyncio.Lock()
        self._session_ready = asyncio.Event()
        self._completion_changed = asyncio.Event()
        self._playbacks: dict[int, _Playback] = {}
        self._sink_owner: PlaybackTarget | None = None
        self._fence_generation = 1
        self._uplink_frames = 0
        self._playback_frames = 0
        self._completed_playbacks = 0
        self._source_exhausted = False
        self._running = False
        self._session_generation = 0

    async def run(
        self,
        *,
        stop_event: asyncio.Event | None = None,
        stop_after_playbacks: int | None = None,
    ) -> RunResult:
        if self._running:
            raise RuntimeError("desktop app is already running")
        if stop_after_playbacks is not None and stop_after_playbacks <= 0:
            raise ValueError("stop_after_playbacks must be positive")
        self._running = True
        source_started = False
        sink_started = False
        tasks: set[asyncio.Task[None]] = set()
        try:
            await self._source.start()
            source_started = True
            await self._sink.start()
            sink_started = True
            opened = await self._session.connect()
            self._session_generation += 1
            self._session_ready.set()
            await self._notify(opened)

            uplink = asyncio.create_task(self._run_uplink(), name="desktop-uplink")
            receiver = asyncio.create_task(self._run_receiver(), name="desktop-receiver")
            keepalive = asyncio.create_task(self._run_keepalive(), name="desktop-keepalive")
            tasks.update((uplink, receiver, keepalive))
            stopper = (
                asyncio.create_task(stop_event.wait(), name="desktop-stop")
                if stop_event is not None
                else None
            )
            completion = (
                asyncio.create_task(
                    self._wait_for_playbacks(stop_after_playbacks),
                    name="desktop-playback-completion",
                )
                if stop_after_playbacks is not None
                else None
            )
            auxiliaries = {task for task in (stopper, completion) if task is not None}
            tasks.update(auxiliaries)
            core_tasks = {uplink, receiver, keepalive}

            while tasks:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                tasks.difference_update(done)
                # Core failures always win over a simultaneous stop/completion.
                for task in done & core_tasks:
                    if task.cancelled():
                        raise asyncio.CancelledError
                    failure = task.exception()
                    if failure is not None:
                        raise failure
                if receiver in done:
                    return self._result()
                if keepalive in done:
                    raise RuntimeError("keepalive task exited unexpectedly")
                if uplink in done and stop_after_playbacks is None:
                    return self._result()
                auxiliary_done = done - core_tasks
                if auxiliary_done:
                    for task in auxiliary_done:
                        await task
                    return self._result()
            return self._result()
        finally:
            active_failure = sys.exception()
            cleanup_failures: list[Exception] = []
            self._session_ready.clear()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            if self._session_is_healthy():
                await self._cleanup(self._stop_active_playback, cleanup_failures)
            if source_started:
                await self._cleanup(self._source.close, cleanup_failures)
            if sink_started:
                await self._cleanup(self._close_sink, cleanup_failures)
            reason, detail = self._close_reason(active_failure)
            await self._cleanup(
                lambda: self._session.close(reason=reason, detail=detail),
                cleanup_failures,
            )
            await self._cleanup(
                lambda: asyncio.to_thread(self._codec.close),
                cleanup_failures,
            )
            self._running = False
            if active_failure is None and cleanup_failures:
                raise ExceptionGroup("desktop app cleanup failed", cleanup_failures)

    async def _run_uplink(self) -> None:
        while True:
            frame = await self._source.read_frame()
            if frame is None:
                self._source_exhausted = True
                return
            payload = await asyncio.to_thread(self._codec.encode_60ms, frame.data)
            await self._session_ready.wait()
            async with self._session_operation:
                await self._session.send_opus(payload, samples=WIRE_SAMPLES_PER_FRAME)
            self._uplink_frames += 1

    async def _run_receiver(self) -> None:
        while True:
            timeout = self._next_completion_timeout()
            try:
                if timeout is None:
                    event = await self._session.next_event()
                else:
                    event = await asyncio.wait_for(self._session.next_event(), timeout=timeout)
            except TimeoutError:
                await self._expire_incomplete_playback()
                continue
            if event.kind == "transport.reopen_required":
                await self._notify(event)
                await self._fresh_reopen(self._session_generation)
                continue
            await self._handle_event(event)
            await self._notify(event)
            if event.kind == "session.close":
                return

    async def _run_keepalive(self) -> None:
        while True:
            await self._session_ready.wait()
            if self._session.selected_profile is not MediaProfile.UDP_OPUS_GCM_V1:
                await asyncio.sleep(1)
                continue
            interval = max(0.1, self._session.state.opened.heartbeat_interval_ms / 2_000)
            await asyncio.sleep(interval)
            await self._session_ready.wait()
            async with self._session_operation:
                if self._session.selected_profile is MediaProfile.UDP_OPUS_GCM_V1:
                    try:
                        await self._session.send_keepalive()
                    except FreshReopenRequired:
                        reopen_generation = self._session_generation
                    else:
                        reopen_generation = None
            if reopen_generation is not None:
                await self._fresh_reopen(reopen_generation)

    async def _handle_event(self, event: SessionEvent) -> None:
        if event.kind == "response.begin":
            assert event.target is not None
            playback = _Playback(event.target)
            playback.completion_deadline_at = (
                asyncio.get_running_loop().time() + self._response_terminal_timeout()
            )
            self._playbacks[event.target.generation] = playback
            return
        if event.kind == "media.audio":
            await self._play_audio(event)
            return
        if event.kind == "response.end":
            await self._finish_response(event)
            return
        if event.kind == "playback.stop":
            await self._stop_playback(event)

    async def _play_audio(self, event: SessionEvent) -> None:
        assert event.target is not None and event.media is not None
        playback = self._playbacks.get(event.target.generation)
        if (
            playback is None
            or playback.target != event.target
            or playback.ended
            or event.target.generation < self._fence_generation
        ):
            return
        if self._sink_owner is None:
            self._sink_owner = event.target
        elif self._sink_owner != event.target:
            return

        if not await self._conceal_bounded_gap(
            playback,
            event.media.sequence,
            event.media.timestamp,
        ):
            return
        pcm = await asyncio.to_thread(self._codec.decode_60ms, event.media.payload)
        await self._render_pcm(
            playback,
            pcm,
            sequence=event.media.sequence,
            timestamp=event.media.timestamp,
            report_sequence=event.media.sequence,
        )
        playback.last_media_sequence = event.media.sequence
        playback.last_media_timestamp = event.media.timestamp
        playback.completion_deadline_at = (
            asyncio.get_running_loop().time() + self._response_terminal_timeout()
        )
        await self._complete_if_ready(playback)

    async def _finish_response(self, event: SessionEvent) -> None:
        assert event.target is not None
        playback = self._playbacks.get(event.target.generation)
        if playback is None or playback.target != event.target or playback.ended:
            return
        outcome = str(event.message["outcome"])
        if outcome == "completed":
            playback.final_media_sequence = int(event.message["final_media_sequence"])
            await self._complete_if_ready(playback)
            if not playback.ended:
                playback.completion_deadline_at = (
                    asyncio.get_running_loop().time() + self._media_completion_timeout()
                )
            return
        await self._end_playback(playback, "stopped" if outcome == "cancelled" else "failed")

    async def _complete_if_ready(self, playback: _Playback) -> None:
        if playback.ended or playback.final_media_sequence is None:
            return
        if playback.last_media_sequence != playback.final_media_sequence:
            return
        await self._sink.drain()
        await self._end_playback(playback, "completed")

    async def _stop_playback(self, event: SessionEvent) -> None:
        assert event.target is not None
        self._fence_generation = max(
            self._fence_generation,
            int(event.message["fence_generation"]),
        )
        playback = self._playbacks.get(event.target.generation)
        if playback is None or playback.target != event.target or playback.ended:
            return
        if self._sink_owner == event.target:
            await self._reset_sink()
            self._sink_owner = None
        await self._end_playback(playback, "stopped")

    async def _end_playback(
        self,
        playback: _Playback,
        outcome: str,
        *,
        signal_completion: bool = True,
    ) -> None:
        if playback.ended:
            return
        # Fence the local lifecycle before I/O. DesktopSession performs the same
        # transition before writing, so a failed socket write cannot duplicate facts.
        playback.ended = True
        if self._sink_owner == playback.target:
            self._sink_owner = None
        await self._session.playback_ended(
            playback.target,
            outcome=outcome,
            played_samples=playback.played_samples,
            last_media_sequence=playback.last_media_sequence,
        )
        self._completed_playbacks += 1
        playback.completion_deadline_at = None
        if signal_completion:
            self._completion_changed.set()

    async def _conceal_bounded_gap(
        self,
        playback: _Playback,
        sequence: int,
        timestamp: int,
    ) -> bool:
        previous_sequence = playback.last_media_sequence
        previous_timestamp = playback.last_media_timestamp
        if (
            self._session.selected_profile is not MediaProfile.UDP_OPUS_GCM_V1
            or previous_sequence is None
            or previous_timestamp is None
        ):
            return True
        timestamp_delta = (timestamp - previous_timestamp) & 0xFFFFFFFF
        sequence_delta = sequence - previous_sequence
        if (
            timestamp_delta == 0
            or timestamp_delta % WIRE_SAMPLES_PER_FRAME != 0
            or sequence_delta <= 0
        ):
            await self._recover_from_media_gap(playback)
            return False
        missing = timestamp_delta // WIRE_SAMPLES_PER_FRAME - 1
        # Packet sequence is global to audio and keepalive traffic. It is only
        # evidence that enough packet positions exist; timestamps define audio loss.
        if missing > sequence_delta - 1 or missing > _MAX_PLC_GAP_FRAMES:
            await self._recover_from_media_gap(playback)
            return False
        for offset in range(1, missing + 1):
            missing_sequence = sequence - missing + offset - 1
            missing_timestamp = (
                previous_timestamp + offset * WIRE_SAMPLES_PER_FRAME
            ) & 0xFFFFFFFF
            pcm = await asyncio.to_thread(self._codec.conceal_60ms)
            await self._render_pcm(
                playback,
                pcm,
                sequence=missing_sequence,
                timestamp=missing_timestamp,
                report_sequence=None,
            )
        return True

    async def _recover_from_media_gap(self, playback: _Playback) -> None:
        # Do not let a headless completion waiter terminate the receiver while
        # it still owns a required fresh reopen.
        await self._end_playback(playback, "stopped", signal_completion=False)
        await self._fresh_reopen(self._session_generation)
        self._completion_changed.set()

    async def _render_pcm(
        self,
        playback: _Playback,
        pcm: bytes,
        *,
        sequence: int,
        timestamp: int,
        report_sequence: int | None,
    ) -> None:
        frame = PcmFrame(
            data=pcm,
            sequence=sequence,
            timestamp_samples=timestamp,
            captured_at=asyncio.get_running_loop().time(),
        )
        await self._sink.write_frame(frame)
        ack = await self._sink.wait_rendered(sequence)
        if ack.sequence != sequence or ack.timestamp_samples != timestamp:
            raise RuntimeError("audio sink returned a mismatched render acknowledgement")
        if not playback.started and report_sequence is not None:
            await self._session.playback_started(playback.target, report_sequence)
            playback.started = True
        playback.played_samples += ack.rendered_samples
        self._playback_frames += 1

    async def _stop_active_playback(self) -> None:
        for playback in tuple(self._playbacks.values()):
            if not playback.ended:
                await self._end_playback(playback, "stopped")

    async def _fresh_reopen(self, observed_generation: int) -> None:
        async with self._reopen_operation:
            if observed_generation != self._session_generation:
                return
            self._session_ready.clear()
            ended_playback = False
            try:
                async with self._session_operation:
                    for playback in tuple(self._playbacks.values()):
                        if not playback.ended:
                            await self._end_playback(
                                playback,
                                "stopped",
                                signal_completion=False,
                            )
                            ended_playback = True
                    await self._reset_sink()
                    self._playbacks.clear()
                    self._sink_owner = None
                    self._fence_generation = 1
                    opened = await self._session.reopen()
                    self._session_generation += 1
            finally:
                self._session_ready.set()
            await self._notify(opened)
            if ended_playback:
                self._completion_changed.set()

    def _next_completion_timeout(self) -> float | None:
        deadlines = [
            playback.completion_deadline_at
            for playback in self._playbacks.values()
            if not playback.ended and playback.completion_deadline_at is not None
        ]
        if not deadlines:
            return None
        return max(0.0, min(deadlines) - asyncio.get_running_loop().time())

    async def _expire_incomplete_playback(self) -> None:
        now = asyncio.get_running_loop().time()
        expired = next(
            (
                playback
                for playback in self._playbacks.values()
                if not playback.ended
                and playback.completion_deadline_at is not None
                and playback.completion_deadline_at <= now
            ),
            None,
        )
        if expired is not None:
            await self._recover_from_media_gap(expired)

    def _media_completion_timeout(self) -> float:
        config = getattr(self._session, "config", None)
        return max(0.05, float(getattr(config, "media_max_age_seconds", 0.12)))

    def _response_terminal_timeout(self) -> float:
        config = getattr(self._session, "config", None)
        media_timeout = float(getattr(config, "media_max_age_seconds", 0.12))
        control_timeout = float(getattr(config, "control_timeout_seconds", 5.0))
        return max(1.0, media_timeout * 8, control_timeout)

    async def _reset_sink(self) -> None:
        if self._sink_factory is None:
            # Ports without a reset factory can only fence future frames. This is
            # sufficient for immediate/headless sinks, which retain no audio queue.
            return
        await self._abort_sink()
        self._sink = self._sink_factory()
        await self._sink.start()

    async def _abort_sink(self) -> None:
        abort = getattr(self._sink, "abort", None)
        if abort is not None:
            result = abort()
            if inspect.isawaitable(result):
                await result
            return
        await self._sink.close()

    async def _close_sink(self) -> None:
        try:
            await self._sink.drain()
        finally:
            await self._sink.close()

    async def _wait_for_playbacks(self, count: int) -> None:
        while self._completed_playbacks < count:
            self._completion_changed.clear()
            if self._completed_playbacks >= count:
                return
            await self._completion_changed.wait()

    async def _notify(self, event: SessionEvent) -> None:
        if self._on_event is None:
            return
        result = self._on_event(event)
        if inspect.isawaitable(result):
            await result

    @staticmethod
    async def _cleanup(
        operation: Callable[[], Awaitable[object]],
        failures: list[Exception],
    ) -> None:
        try:
            await operation()
        except Exception as exc:
            failures.append(exc)

    def _result(self) -> RunResult:
        return RunResult(
            uplink_frames=self._uplink_frames,
            playback_frames=self._playback_frames,
            completed_playbacks=self._completed_playbacks,
            source_exhausted=self._source_exhausted,
        )

    def _session_is_healthy(self) -> bool:
        try:
            return not bool(getattr(self._session.state, "closed", False))
        except Exception:
            return False

    @staticmethod
    def _close_reason(failure: BaseException | None) -> tuple[str, str | None]:
        if failure is None or isinstance(failure, asyncio.CancelledError):
            return "normal", None
        if isinstance(failure, ProtocolError):
            return "protocol_error", failure.code
        if isinstance(failure, TransportError):
            return "network_change", failure.code
        if isinstance(failure, RvaClientError):
            return ("network_change" if failure.retryable else "protocol_error"), failure.code
        return "protocol_error", type(failure).__name__


__all__ = ["DesktopApp", "EventHandler", "OpusCodec", "RunResult", "SinkFactory"]
