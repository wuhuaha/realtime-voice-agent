from __future__ import annotations

import asyncio
import json
import time

import pytest

from rva_desktop.config import ClientConfig, MediaProfile
from rva_desktop.errors import ProtocolError, SessionClosed, TransportError
from rva_desktop.protocol import FLAG_AUDIO, MediaFrame, PlaybackTarget, SessionOpened, UdpGrant
from rva_desktop.session import DesktopSession, SessionState
from rva_desktop.transport import BootstrapGrant
from rva_desktop.transport.wss import WssTransport


class FakeDirector:
    def __init__(self) -> None:
        self.grant = BootstrapGrant(
            worker_id="worker-1",
            worker_wss_url="wss://worker.test/rva/v1/voice",
            connect_grant="grant-secret",
            session_epoch="epoch-1",
            fencing_token=1,
            allowed_profiles=(MediaProfile.WSS_OPUS_V1,),
            expires_at=time.time() + 60,
        )
        self.released = False

    async def bootstrap(self) -> BootstrapGrant:
        return self.grant

    async def release(self, _grant: BootstrapGrant) -> None:
        self.released = True

    async def close(self) -> None:
        return None


class SessionConnection:
    def __init__(self) -> None:
        self.inbound: asyncio.Queue[str | bytes] = asyncio.Queue()
        self.sent: list[str | bytes] = []

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)
        if isinstance(message, str) and json.loads(message)["type"] == "session.open":
            request = json.loads(message)
            self.inbound.put_nowait(json.dumps({
                "type": "session.opened",
                "request_id": request["request_id"],
                "session_id": "session-1",
                "session_epoch": "epoch-1",
                "media_id": "0123456789abcdef",
                "media_epoch": 7,
                "selected_media_profile": "wss-opus/1",
                "audio": {"codec": "opus", "sample_rate_hz": 16000, "channels": 1, "frame_duration_ms": 60},
                "heartbeat_interval_ms": 15000,
                "idle_timeout_ms": 45000,
                "max_control_message_bytes": 32768,
            }))

    async def recv(self) -> str | bytes:
        return await self.inbound.get()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        return None


class Clock:
    value = 10.0

    def __call__(self) -> float:
        return self.value


def opened_session() -> SessionOpened:
    return SessionOpened(
        request_id="open-1",
        session_id="session-1",
        session_epoch="epoch-1",
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        selected_profile=MediaProfile.WSS_OPUS_V1,
        heartbeat_interval_ms=15_000,
        idle_timeout_ms=45_000,
        udp_grant=None,
    )


def server_message(kind: str, **fields: object) -> dict[str, object]:
    return {
        "type": kind,
        "session_id": "session-1",
        "session_epoch": "epoch-1",
        **fields,
    }


def test_session_projects_response_media_and_physical_facts() -> None:
    async def scenario() -> None:
        director = FakeDirector()
        connection = SessionConnection()

        async def connector(_url: str, _headers: dict[str, str]) -> SessionConnection:
            return connection

        session = DesktopSession(
            ClientConfig(
                director_url="https://director.test",
                bootstrap_token="secret",
                device_id="desktop-1",
                supported_profiles=(MediaProfile.WSS_OPUS_V1,),
                preferred_profile=MediaProfile.WSS_OPUS_V1,
            ),
            director=director,  # type: ignore[arg-type]
            wss_factory=lambda: WssTransport(connector),
        )
        opened = await session.connect()
        assert opened.kind == "session.opened"

        connection.inbound.put_nowait(json.dumps({
            "type": "response.begin", "session_id": "session-1", "session_epoch": "epoch-1",
            "response_id": "resp-1", "generation": 1,
        }))
        begin = await session.next_event()
        assert begin.target is not None

        connection.inbound.put_nowait(
            MediaFrame(FLAG_AUDIO, bytes.fromhex("0123456789abcdef"), 7, 0, 0, 1, b"opus").encode_plain()
        )
        audio = await session.next_event()
        assert audio.kind == "media.audio"
        await session.playback_started(begin.target, 0)

        connection.inbound.put_nowait(json.dumps({
            "type": "response.end", "session_id": "session-1", "session_epoch": "epoch-1",
            "response_id": "resp-1", "generation": 1, "outcome": "completed", "final_media_sequence": 0,
        }))
        assert (await session.next_event()).kind == "response.end"
        await session.playback_ended(
            begin.target, outcome="completed", played_samples=960, last_media_sequence=0
        )
        facts = [json.loads(item) for item in connection.sent if isinstance(item, str)]
        assert [item["type"] for item in facts[-2:]] == ["playback.started", "playback.ended"]

        await session.close()
        assert director.released
        close = json.loads(next(item for item in reversed(connection.sent) if isinstance(item, str)))
        assert close["type"] == "session.close"
        assert close["reason"] == "normal"

    asyncio.run(scenario())


def test_stale_wss_media_does_not_commit_playback_cursor() -> None:
    async def scenario() -> None:
        director = FakeDirector()
        connection = SessionConnection()
        clock = Clock()

        async def connector(_url: str, _headers: dict[str, str]) -> SessionConnection:
            return connection

        session = DesktopSession(
            ClientConfig(
                director_url="https://director.test",
                bootstrap_token="secret",
                device_id="desktop-1",
                supported_profiles=(MediaProfile.WSS_OPUS_V1,),
                preferred_profile=MediaProfile.WSS_OPUS_V1,
            ),
            director=director,  # type: ignore[arg-type]
            wss_factory=lambda: WssTransport(connector),
            monotonic=clock,
        )
        await session.connect()
        connection.inbound.put_nowait(
            json.dumps(server_message("response.begin", response_id="resp-1", generation=1))
        )
        begin = await session.next_event()
        assert begin.target is not None
        connection.inbound.put_nowait(
            MediaFrame(FLAG_AUDIO, opened_session().media_id, 7, 0, 0, 1, b"first").encode_plain()
        )
        assert (await session.next_event()).kind == "media.audio"
        await session.playback_started(begin.target, 0)

        clock.value += 0.20
        connection.inbound.put_nowait(
            MediaFrame(FLAG_AUDIO, opened_session().media_id, 7, 1, 960, 1, b"stale").encode_plain()
        )
        assert (await session.next_event()).kind == "transport.reopen_required"
        await session.playback_ended(
            begin.target,
            outcome="stopped",
            played_samples=960,
            last_media_sequence=0,
        )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "reason",
    ["normal", "idle_timeout", "network_change", "protocol_error", "server_shutdown"],
)
def test_session_close_uses_canonical_reason_and_optional_detail(reason: str) -> None:
    outgoing = SessionState(opened_session()).close_message(reason, detail="client shutdown")
    assert outgoing == {
        "type": "session.close",
        "session_id": "session-1",
        "session_epoch": "epoch-1",
        "reason": reason,
        "initiated_by": "device",
        "detail": "client shutdown",
    }

    incoming = SessionState(opened_session()).accept_control(
        server_message(
            "session.close",
            reason=reason,
            initiated_by="server",
            detail="server shutdown",
        )
    )
    assert incoming is not None and incoming.kind == "session.close"


def test_session_close_rejects_noncanonical_reason_and_invalid_detail() -> None:
    with pytest.raises(ProtocolError, match="invalid_close_reason"):
        SessionState(opened_session()).close_message("transport_error")
    with pytest.raises(ProtocolError, match="invalid_close_detail"):
        SessionState(opened_session()).close_message("normal", detail="\x00")


def test_transcript_final_is_terminal_for_utterance() -> None:
    state = SessionState(opened_session())
    assert state.accept_control(
        server_message("transcript.delta", utterance_id="utt-1", sequence=0, text="你")
    ) is not None
    final = state.accept_control(
        server_message("transcript.final", utterance_id="utt-1", sequence=1, text="你好")
    )
    assert final is not None and final.kind == "transcript.final"

    with pytest.raises(ProtocolError, match="transcript_already_final"):
        state.accept_control(
            server_message("transcript.delta", utterance_id="utt-1", sequence=2, text="呀")
        )


def test_completed_playback_requires_started_and_terminal_response_evidence() -> None:
    state = SessionState(opened_session())
    target = PlaybackTarget("resp-1", 1)
    state.accept_control(server_message("response.begin", response_id="resp-1", generation=1))

    with pytest.raises(ProtocolError, match="playback_evidence_mismatch"):
        state.playback_ended(
            target,
            outcome="completed",
            played_samples=960,
            last_media_sequence=4,
        )

    with pytest.raises(ProtocolError, match="playback_evidence_mismatch"):
        state.playback_started(target, 0)
    state.accept_media(
        MediaFrame(FLAG_AUDIO, opened_session().media_id, 7, 0, 0, 1, b"opus")
    )
    state.playback_started(target, 0)
    with pytest.raises(ProtocolError, match="playback_evidence_mismatch"):
        state.playback_ended(
            target,
            outcome="completed",
            played_samples=960,
            last_media_sequence=0,
        )

    state.accept_control(
        server_message(
            "response.end",
            response_id="resp-1",
            generation=1,
            outcome="completed",
            final_media_sequence=0,
        )
    )
    ended = state.playback_ended(
        target,
        outcome="completed",
        played_samples=0xFFFFFFFFFFFFFFFF,
        last_media_sequence=0,
    )
    assert ended["played_samples"] == 0xFFFFFFFFFFFFFFFF


def test_playback_facts_require_exact_admitted_cursor_and_stop_ordering() -> None:
    state = SessionState(opened_session())
    target = PlaybackTarget("resp-1", 1)
    state.accept_control(server_message("response.begin", response_id="resp-1", generation=1))
    state.accept_media(MediaFrame(FLAG_AUDIO, opened_session().media_id, 7, 0, 0, 1, b"first"))
    state.playback_started(target, 0)
    state.accept_media(MediaFrame(FLAG_AUDIO, opened_session().media_id, 7, 1, 960, 1, b"second"))

    with pytest.raises(ProtocolError, match="playback_evidence_mismatch"):
        state.playback_ended(target, outcome="failed", played_samples=960, last_media_sequence=0)
    with pytest.raises(ProtocolError, match="playback_evidence_mismatch"):
        state.playback_ended(target, outcome="failed", played_samples=0, last_media_sequence=None)

    stopped = SessionState(opened_session())
    stopped.accept_control(server_message("response.begin", response_id="resp-1", generation=1))
    stopped.accept_control(
        server_message(
            "playback.stop",
            target={"response_id": "resp-1", "generation": 1},
            fence_generation=2,
            cause="recognized_interrupt",
        )
    )
    with pytest.raises(ProtocolError, match="playback_evidence_mismatch"):
        stopped.playback_started(target, 0)
    ended = stopped.playback_ended(
        target,
        outcome="stopped",
        played_samples=0,
        last_media_sequence=None,
    )
    assert ended["outcome"] == "stopped"


def test_terminal_control_wins_over_same_tick_udp_media() -> None:
    async def scenario() -> None:
        udp_grant = UdpGrant(
            host="voice.test",
            port=8443,
            expires_at_ms=2_000_000,
            refresh_after_ms=1_000,
            uplink_key=b"0" * 16,
            uplink_salt=b"1" * 8,
            downlink_key=b"2" * 16,
            downlink_salt=b"3" * 8,
            probe_timeout_ms=500,
        )
        opened = SessionOpened(
            request_id="open-1",
            session_id="session-1",
            session_epoch="epoch-1",
            media_id=opened_session().media_id,
            media_epoch=7,
            selected_profile=MediaProfile.UDP_OPUS_GCM_V1,
            heartbeat_interval_ms=15_000,
            idle_timeout_ms=45_000,
            udp_grant=udp_grant,
        )

        class ImmediateControl:
            async def receive(self) -> str:
                return json.dumps(server_message("session.close", reason="normal", initiated_by="server"))

        class ImmediateMedia:
            refresh_due = False

            async def receive_audio(self) -> MediaFrame:
                return MediaFrame(FLAG_AUDIO, opened.media_id, 7, 1, 0, 99, b"late")

        session = DesktopSession(
            ClientConfig(
                director_url="https://director.test",
                bootstrap_token="secret",
                device_id="desktop-1",
            )
        )
        session._state = SessionState(opened)
        session._wss = ImmediateControl()  # type: ignore[assignment]
        session._udp = ImmediateMedia()  # type: ignore[assignment]

        event = await session.next_event()
        assert event.kind == "session.close"
        assert session.state.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["close", "reopen"])
def test_session_teardown_finishes_when_caller_is_cancelled(operation: str) -> None:
    async def scenario() -> None:
        class BlockingDirector(FakeDirector):
            def __init__(self) -> None:
                super().__init__()
                self.release_started = asyncio.Event()
                self.allow_release = asyncio.Event()
                self.closed = False

            async def release(self, _grant: BootstrapGrant) -> None:
                self.release_started.set()
                await self.allow_release.wait()
                self.released = True

            async def close(self) -> None:
                self.closed = True

        director = BlockingDirector()
        connection = SessionConnection()

        async def connector(_url: str, _headers: dict[str, str]) -> SessionConnection:
            return connection

        session = DesktopSession(
            ClientConfig(
                director_url="https://director.test",
                bootstrap_token="secret",
                device_id="desktop-1",
                supported_profiles=(MediaProfile.WSS_OPUS_V1,),
                preferred_profile=MediaProfile.WSS_OPUS_V1,
            ),
            director=director,  # type: ignore[arg-type]
            wss_factory=lambda: WssTransport(connector),
        )
        await session.connect()

        task = asyncio.create_task(session.close() if operation == "close" else session.reopen())
        await asyncio.wait_for(director.release_started.wait(), timeout=1)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        director.allow_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert director.released
        assert director.closed is (operation == "close")
        with pytest.raises(SessionClosed):
            _ = session.state

    asyncio.run(scenario())


def test_cancelled_close_is_bounded_when_close_control_send_never_returns() -> None:
    async def scenario() -> None:
        class BlockingCloseConnection(SessionConnection):
            def __init__(self) -> None:
                super().__init__()
                self.close_send_started = asyncio.Event()

            async def send(self, message: str | bytes) -> None:
                if isinstance(message, str) and json.loads(message)["type"] == "session.close":
                    self.close_send_started.set()
                    await asyncio.Event().wait()
                await super().send(message)

        director = FakeDirector()
        connection = BlockingCloseConnection()

        async def connector(_url: str, _headers: dict[str, str]) -> BlockingCloseConnection:
            return connection

        session = DesktopSession(
            ClientConfig(
                director_url="https://director.test",
                bootstrap_token="secret",
                device_id="desktop-1",
                supported_profiles=(MediaProfile.WSS_OPUS_V1,),
                preferred_profile=MediaProfile.WSS_OPUS_V1,
            ),
            director=director,  # type: ignore[arg-type]
            wss_factory=lambda: WssTransport(connector),
        )
        session._CLOSE_CONTROL_TIMEOUT_SECONDS = 0.02
        session._TEARDOWN_TIMEOUT_SECONDS = 0.2
        await session.connect()

        closing = asyncio.create_task(session.close())
        await asyncio.wait_for(connection.close_send_started.wait(), timeout=1)
        closing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(closing, timeout=0.3)
        assert director.released
        with pytest.raises(SessionClosed):
            _ = session.state

    asyncio.run(scenario())


def test_close_timeout_still_closes_director_client_and_discards_active_handles() -> None:
    async def scenario() -> None:
        class NeverReleaseDirector(FakeDirector):
            def __init__(self) -> None:
                super().__init__()
                self.closed = False

            async def release(self, _grant: BootstrapGrant) -> None:
                await asyncio.Event().wait()

            async def close(self) -> None:
                self.closed = True

        director = NeverReleaseDirector()
        connection = SessionConnection()

        async def connector(_url: str, _headers: dict[str, str]) -> SessionConnection:
            return connection

        session = DesktopSession(
            ClientConfig(
                director_url="https://director.test",
                bootstrap_token="secret",
                device_id="desktop-1",
                supported_profiles=(MediaProfile.WSS_OPUS_V1,),
                preferred_profile=MediaProfile.WSS_OPUS_V1,
            ),
            director=director,  # type: ignore[arg-type]
            wss_factory=lambda: WssTransport(connector),
        )
        session._CLOSE_CONTROL_TIMEOUT_SECONDS = 0.02
        session._TEARDOWN_TIMEOUT_SECONDS = 0.05
        await session.connect()

        with pytest.raises(TransportError, match="teardown_timeout"):
            await asyncio.wait_for(session.close(), timeout=0.2)
        assert director.closed
        with pytest.raises(SessionClosed):
            _ = session.state

    asyncio.run(scenario())


@pytest.mark.parametrize("played_samples", [-1, 0x1_0000_0000_0000_0000, True])
def test_playback_rejects_values_outside_canonical_uint64(played_samples: object) -> None:
    state = SessionState(opened_session())
    target = PlaybackTarget("resp-1", 1)
    state.accept_control(server_message("response.begin", response_id="resp-1", generation=1))

    with pytest.raises(ProtocolError, match="invalid_played_samples"):
        state.playback_ended(
            target,
            outcome="failed",
            played_samples=played_samples,  # type: ignore[arg-type]
            last_media_sequence=None,
        )


def test_duplicate_playback_stop_is_noop_and_stale_target_is_rejected() -> None:
    state = SessionState(opened_session())
    state.accept_control(server_message("response.begin", response_id="resp-1", generation=1))
    stop = server_message(
        "playback.stop",
        target={"response_id": "resp-1", "generation": 1},
        fence_generation=2,
        cause="recognized_interrupt",
    )

    first = state.accept_control(stop)
    assert first is not None and first.kind == "playback.stop"
    assert state.accept_control(stop) is None

    with pytest.raises(ProtocolError, match="playback_target_mismatch"):
        state.accept_control(
            server_message(
                "playback.stop",
                target={"response_id": "stale-response", "generation": 1},
                fence_generation=3,
                cause="recognized_interrupt",
            )
        )


def test_terminal_session_ledgers_remain_bounded_across_many_turns() -> None:
    state = SessionState(opened_session())
    identity = {"session_id": "session-1", "session_epoch": "epoch-1"}

    for index in range(100):
        utterance_id = f"utterance-{index}"
        state.accept_control(
            {"type": "transcript.final", **identity, "utterance_id": utterance_id, "sequence": 0, "text": "ok"}
        )
        generation = index + 1
        response_id = f"response-{generation}"
        target = PlaybackTarget(response_id, generation)
        state.accept_control(
            {"type": "response.begin", **identity, "response_id": response_id, "generation": generation}
        )
        state.accept_media(
            MediaFrame(FLAG_AUDIO, opened_session().media_id, 7, index, 0, generation, b"opus")
        )
        state.playback_started(target, index)
        state.accept_control(
            {
                "type": "response.end",
                **identity,
                "response_id": response_id,
                "generation": generation,
                "outcome": "completed",
                "final_media_sequence": index,
            }
        )
        state.playback_ended(
            target,
            outcome="completed",
            played_samples=960,
            last_media_sequence=index,
        )

    assert not state._playbacks
    assert not state._transcript_sequences
    assert not state._response_text_sequence
    assert not state._last_media_timestamp
    assert len(state._playback_tombstones) == 64
    assert len(state._final_transcripts) == 64
    assert not state.validate_media_admission(
        MediaFrame(FLAG_AUDIO, opened_session().media_id, 7, 100, 960, 100, b"late")
    )
