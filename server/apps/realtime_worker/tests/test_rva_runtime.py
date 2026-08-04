from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from types import SimpleNamespace

import pytest
from realtime_worker.agent import AgentOutputSegment, AgentRunnerTerminal, AgentRunnerTerminalKind
from realtime_worker.audio import PCM_SAMPLES, PcmFrame
from realtime_worker.bindings.rva import (
    RvaBindingError,
    RvaOpusCodec,
    RvaOpusDecodeError,
    RvaOverloadedError,
    RvaRuntimeLimits,
    RvaWssConnection,
    WssMediaFrame,
)
from realtime_worker.bindings.rva.binding import ControlEffect, InboundAudioPacket
from realtime_worker.bindings.rva.runtime import RvaControlTimeoutError
from realtime_worker.interruption import InterruptionPolicyConfig, LayeredInterruptionPolicy
from realtime_worker.transport.udp_gateway import UdpGrantExpiredError


def pcm_frame(sequence: int) -> PcmFrame:
    return PcmFrame(0, sequence, sequence * PCM_SAMPLES, b"\x00" * (PCM_SAMPLES * 2))


class MutableMonotonicClock:
    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def session_open() -> str:
    return json.dumps(
        {
            "type": "session.open",
            "protocol_version": 1,
            "request_id": "open-001",
            "device_id": "device-001",
            "supported_media_profiles": ["wss-opus/1"],
            "preferred_media_profile": "wss-opus/1",
            "audio": {"codec": "opus", "sample_rate_hz": 16_000, "channels": 1, "frame_duration_ms": 60},
            "capabilities": {"aec": True, "vad": True},
        }
    )


class FakeWebSocket:
    def __init__(self) -> None:
        self.inbound: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        self.control: list[dict[str, object]] = []
        self.media: list[bytes] = []
        self.closed: list[tuple[int, str]] = []
        self.changed = asyncio.Condition()

    async def receive(self) -> dict[str, object]:
        return await self.inbound.get()

    async def send_text(self, payload: str) -> None:
        async with self.changed:
            self.control.append(json.loads(payload))
            self.changed.notify_all()

    async def send_bytes(self, payload: bytes) -> None:
        async with self.changed:
            self.media.append(payload)
            self.changed.notify_all()

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append((code, reason))

    def feed_open(self) -> None:
        self.inbound.put_nowait({"type": "websocket.receive", "text": session_open()})

    def feed_media(self, payload: bytes) -> None:
        self.inbound.put_nowait({"type": "websocket.receive", "bytes": payload})

    def disconnect(self) -> None:
        self.inbound.put_nowait({"type": "websocket.disconnect", "code": 1_000})

    async def wait_control(self, event_type: str, *, timeout: float = 2.0) -> dict[str, object]:
        async def find() -> dict[str, object]:
            async with self.changed:
                while True:
                    for event in self.control:
                        if event.get("type") == event_type:
                            return event
                    await self.changed.wait()

        return await asyncio.wait_for(find(), timeout=timeout)

    async def wait_media_count(self, count: int, *, timeout: float = 2.0) -> None:
        async def wait() -> None:
            async with self.changed:
                while len(self.media) < count:
                    await self.changed.wait()

        await asyncio.wait_for(wait(), timeout=timeout)


class FakeRunner:
    def __init__(
        self,
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        response_end: Callable[[int], None],
        *,
        trigger_frames: int = 9,
        output_frames: int = 10,
    ) -> None:
        self.emit = emit
        self.response_end = response_end
        self.trigger_frames = trigger_frames
        self.output_frames = output_frames
        self.pushes = 0
        self.interrupts = 0
        self.producer_epoch = 1
        self.close_calls = 0
        self.started = False
        self.playback: list[tuple[float, bool]] = []
        self.response_gate: list[bool] = []
        self.playback_started_event = asyncio.Event()
        self.block_interrupt = False
        self.interrupt_started = asyncio.Event()
        self.interrupt_release = asyncio.Event()
        self._user_text: Callable[[str, bool], None] | None = None
        self._assistant_text: Callable[[str], None] | None = None
        self._terminal = asyncio.get_running_loop().create_future()

    @property
    def terminal(self) -> asyncio.Future[AgentRunnerTerminal]:
        return self._terminal

    def fail_runtime(self) -> None:
        if not self._terminal.done():
            self._terminal.set_result(AgentRunnerTerminal(AgentRunnerTerminalKind.RUNTIME_FAILED))

    def set_text_sinks(
        self,
        user_transcript: Callable[[str, bool], None],
        assistant_text: Callable[[str], None],
    ) -> None:
        self._user_text = user_transcript
        self._assistant_text = assistant_text

    async def start(self) -> None:
        self.started = True

    async def push_audio(self, frame: PcmFrame) -> None:
        self.pushes += 1
        if self.pushes != self.trigger_frames:
            return
        assert self._user_text is not None and self._assistant_text is not None
        self._user_text("hello", False)
        self._user_text("hello world", True)
        self._assistant_text("response text")
        await self.emit(
            AgentOutputSegment(
                self.producer_epoch,
                [pcm_frame(index) for index in range(self.output_frames)],
            )
        )
        self.response_end(self.producer_epoch)

    def set_response_gate(self, active: bool) -> None:
        self.response_gate.append(active)

    async def commit_text(self, text: str) -> None:
        return None

    async def playback_started(self, created_at: float) -> None:
        del created_at
        self.playback_started_event.set()

    async def playback_finished(self, position: float, interrupted: bool) -> None:
        self.playback.append((position, interrupted))

    async def interrupt(self) -> int:
        self.interrupts += 1
        self.interrupt_started.set()
        if self.block_interrupt:
            await self.interrupt_release.wait()
        self.producer_epoch += 1
        return self.producer_epoch

    async def close(self) -> None:
        self.close_calls += 1
        if not self._terminal.done():
            self._terminal.set_result(AgentRunnerTerminal(AgentRunnerTerminalKind.OWNER_CLOSED))


def create_connection(
    websocket: FakeWebSocket,
    *,
    trigger_frames: int = 9,
    output_frames: int = 10,
    limits: RvaRuntimeLimits | None = None,
    interruption_policy: LayeredInterruptionPolicy | None = None,
    codec_factory: Callable[[], RvaOpusCodec] = RvaOpusCodec,
    clock: Callable[[], float] | None = None,
) -> tuple[RvaWssConnection, list[FakeRunner]]:
    runners: list[FakeRunner] = []

    def factory(
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        stop: Callable[[int], None],
    ) -> FakeRunner:
        runner = FakeRunner(emit, stop, trigger_frames=trigger_frames, output_frames=output_frames)
        runners.append(runner)
        return runner

    connection_kwargs = {"clock": clock} if clock is not None else {}
    connection = RvaWssConnection(
        websocket,  # type: ignore[arg-type]
        expected_device_id="device-001",
        session_id="session-001",
        session_epoch="grant-epoch-001",
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        runner_factory=factory,
        limits=limits,
        interruption_policy=interruption_policy,
        codec_factory=codec_factory,
        **connection_kwargs,  # type: ignore[arg-type]
    )
    return connection, runners


class ScriptedDecodeCodec:
    def __init__(self, outcomes: list[bool]) -> None:
        self._outcomes = iter(outcomes)
        self.calls = 0

    def decode_60ms(self, payload: bytes, *, sequence_start: int) -> list[PcmFrame]:
        del payload
        self.calls += 1
        if not next(self._outcomes):
            raise RvaOpusDecodeError("sensitive-provider-detail payload=do-not-log")
        return [pcm_frame(sequence_start + offset) for offset in range(3)]


class CrashingDecodeCodec:
    def decode_60ms(self, payload: bytes, *, sequence_start: int) -> list[PcmFrame]:
        del payload, sequence_start
        raise RuntimeError("unexpected decoder implementation failure")


async def wait_until(predicate: Callable[[], bool]) -> None:
    async def wait() -> None:
        while not predicate():
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), timeout=2.0)


def uplink_packets(count: int) -> list[bytes]:
    codec = RvaOpusCodec()
    packets: list[bytes] = []
    for sequence in range(count):
        payload = codec.encode_60ms([pcm_frame(sequence * 3 + offset) for offset in range(3)])
        packets.append(
            WssMediaFrame(
                media_id=bytes.fromhex("0123456789abcdef"),
                media_epoch=7,
                sequence=sequence,
                timestamp=sequence * 960,
                generation=0,
                payload=payload,
            ).serialize()
        )
    return packets


@pytest.mark.integration
async def test_isolated_invalid_opus_packet_is_dropped_without_closing_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = FakeWebSocket()
    codec = ScriptedDecodeCodec([False])
    connection, runners = create_connection(
        websocket,
        trigger_frames=1_000,
        codec_factory=lambda: codec,  # type: ignore[arg-type]
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")

    with caplog.at_level("WARNING"):
        websocket.feed_media(uplink_packets(1)[0])
        await wait_until(lambda: connection._invalid_opus_packets == 1)  # noqa: SLF001

    assert not task.done()
    assert runners[0].pushes == 0
    assert connection._invalid_opus_packets == 1  # noqa: SLF001
    assert connection._consecutive_invalid_opus_packets == 1  # noqa: SLF001
    assert "rva_opus_packet_dropped" in caplog.text
    assert "decoder_error=RvaOpusDecodeError" in caplog.text
    assert "sensitive-provider-detail" not in caplog.text
    assert "payload=do-not-log" not in caplog.text

    websocket.disconnect()
    await asyncio.wait_for(task, timeout=2.0)
    assert websocket.closed == [(1_000, "normal")]


@pytest.mark.integration
async def test_valid_opus_packet_resets_consecutive_decode_failure_count() -> None:
    websocket = FakeWebSocket()
    codec = ScriptedDecodeCodec([False, True])
    connection, runners = create_connection(
        websocket,
        trigger_frames=1_000,
        codec_factory=lambda: codec,  # type: ignore[arg-type]
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")

    for index, packet in enumerate(uplink_packets(2), start=1):
        websocket.feed_media(packet)
        if index == 1:
            await wait_until(lambda: connection._consecutive_invalid_opus_packets == 1)  # noqa: SLF001
        else:
            await wait_until(lambda: runners[0].pushes == 3)

    assert not task.done()
    assert connection._invalid_opus_packets == 1  # noqa: SLF001
    assert connection._consecutive_invalid_opus_packets == 0  # noqa: SLF001
    assert runners[0].pushes == 3

    websocket.disconnect()
    await asyncio.wait_for(task, timeout=2.0)
    assert websocket.closed == [(1_000, "normal")]


@pytest.mark.integration
async def test_consecutive_invalid_opus_threshold_closes_with_stable_media_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = FakeWebSocket()
    codec = ScriptedDecodeCodec([False, False, False])
    connection, runners = create_connection(
        websocket,
        trigger_frames=1_000,
        limits=RvaRuntimeLimits(max_consecutive_invalid_opus_packets=3),
        codec_factory=lambda: codec,  # type: ignore[arg-type]
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")

    with caplog.at_level("WARNING"):
        for packet in uplink_packets(3):
            websocket.feed_media(packet)
        await asyncio.wait_for(task, timeout=2.0)

    assert runners[0].pushes == 0
    assert runners[0].close_calls == 1
    assert connection._invalid_opus_packets == 3  # noqa: SLF001
    assert connection._consecutive_invalid_opus_packets == 3  # noqa: SLF001
    assert websocket.closed == [(1_011, "media_decode_failed")]
    assert "rva_opus_packet_dropped" in caplog.text
    assert "sensitive-provider-detail" not in caplog.text
    assert "payload=do-not-log" not in caplog.text


@pytest.mark.integration
async def test_unexpected_decoder_failure_is_not_treated_as_malformed_media() -> None:
    websocket = FakeWebSocket()
    connection, runners = create_connection(
        websocket,
        trigger_frames=1_000,
        codec_factory=CrashingDecodeCodec,  # type: ignore[arg-type]
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")

    websocket.feed_media(uplink_packets(1)[0])
    await asyncio.wait_for(task, timeout=2.0)

    assert runners[0].pushes == 0
    assert runners[0].close_calls == 1
    assert connection._invalid_opus_packets == 0  # noqa: SLF001
    assert websocket.closed == [(1_011, "runtime_failure")]


@pytest.mark.unit
def test_runtime_passes_non_default_close_stage_timeout_to_session_state() -> None:
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(agent_close_stage_timeout_seconds=0.125),
    )

    assert connection.binding._responses._state._close_stage_timeout_seconds == 0.125  # noqa: SLF001


@pytest.mark.unit
def test_udp_grant_advertises_monotonic_refresh_before_hard_expiry() -> None:
    connection = object.__new__(RvaWssConnection)
    connection._wall_clock = lambda: 1_000.0  # type: ignore[attr-defined]  # noqa: SLF001
    connection._udp_session = SimpleNamespace(  # type: ignore[attr-defined]  # noqa: SLF001
        grant=SimpleNamespace(
            host="voice.example.test",
            port=8093,
            expires_at=1_600,
            uplink_key=bytes(16),
            uplink_salt=bytes(8),
            downlink_key=bytes(16),
            downlink_salt=bytes(8),
            probe_timeout_ms=1_500,
        )
    )

    grant = connection._rva_udp_grant()  # noqa: SLF001

    assert grant is not None
    assert grant["expires_at_ms"] == 1_600_000
    assert grant["refresh_after_ms"] == 595_000


@pytest.mark.unit
def test_rva_opus_codec_roundtrips_one_60ms_packet_to_three_pcm_frames() -> None:
    encoder = RvaOpusCodec()
    decoder = RvaOpusCodec()

    payload = encoder.encode_60ms([pcm_frame(index) for index in range(3)])
    decoded = decoder.decode_60ms(payload, sequence_start=12)

    assert 0 < len(payload) <= 1_200
    assert [frame.sequence for frame in decoded] == [12, 13, 14]
    assert all(len(frame.pcm) == PCM_SAMPLES * 2 for frame in decoded)


@pytest.mark.unit
def test_rva_opus_codec_normalizes_invalid_packet_to_decode_error() -> None:
    decoder = RvaOpusCodec()

    with pytest.raises(RvaOpusDecodeError, match="invalid Opus packet"):
        decoder.decode_60ms(b"not-opus", sequence_start=0)


@pytest.mark.integration
async def test_three_uplink_packets_produce_text_and_generation_framed_audio() -> None:
    websocket = FakeWebSocket()
    connection, runners = create_connection(websocket)
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")

    for packet in uplink_packets(3):
        websocket.feed_media(packet)
    end = await websocket.wait_control("response.end")
    websocket.disconnect()
    await asyncio.wait_for(task, timeout=2.0)

    runner = runners[0]
    event_types = [event["type"] for event in websocket.control]
    assert event_types == [
        "session.opened",
        "transcript.delta",
        "transcript.final",
        "response.begin",
        "response.text",
        "response.end",
    ]
    begin = next(event for event in websocket.control if event["type"] == "response.begin")
    decoded_media = [WssMediaFrame.parse(packet) for packet in websocket.media]
    assert len(decoded_media) == 4
    assert [frame.sequence for frame in decoded_media] == [0, 1, 2, 3]
    assert {frame.generation for frame in decoded_media} == {begin["generation"]}
    assert end["generation"] == begin["generation"]
    assert end["outcome"] == "completed"
    assert end["final_media_sequence"] == decoded_media[-1].sequence
    assert runner.response_gate == [True]
    assert runner.pushes == 9
    # Physical playout is endpoint-owned in rva/1 and is only
    # acknowledged after playback.started/playback.ended arrive from the device.
    assert runner.playback == []
    assert runner.close_calls == 1
    assert websocket.closed == [(1_000, "normal")]


@pytest.mark.integration
async def test_missing_endpoint_playback_terminal_closes_for_fresh_reopen() -> None:
    websocket = FakeWebSocket()
    limits = RvaRuntimeLimits(playback_terminal_timeout_seconds=0.05)
    connection, runners = create_connection(websocket, limits=limits)
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")

    for packet in uplink_packets(3):
        websocket.feed_media(packet)
    await websocket.wait_control("response.end")
    error = await websocket.wait_control("session.error")
    await asyncio.wait_for(task, timeout=2.0)

    assert error["code"] == "playback_terminal_timeout"
    assert error["retryable"] is True
    assert runners[0].response_gate == [True]
    assert websocket.closed == [(1_011, "playback_terminal_timeout")]


@pytest.mark.integration
async def test_exact_cancel_wakes_pacing_and_fences_old_segment() -> None:
    websocket = FakeWebSocket()
    limits = RvaRuntimeLimits(playback_prebuffer_packets=0)
    connection, runners = create_connection(websocket, trigger_frames=3, output_frames=30, limits=limits)
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    websocket.feed_media(uplink_packets(1)[0])
    begin = await websocket.wait_control("response.begin")
    runners[0].block_interrupt = True
    await runners[0].emit(AgentOutputSegment(1, [pcm_frame(index) for index in range(6)]))
    websocket.inbound.put_nowait(
        {
            "type": "websocket.receive",
            "text": json.dumps(
                {
                    "type": "response.cancel.request",
                    "session_id": "session-001",
                    "session_epoch": "grant-epoch-001",
                    "request_id": "cancel-001",
                    "target": {"response_id": begin["response_id"], "generation": begin["generation"]},
                    "cause": "user_request",
                }
            ),
        }
    )
    await runners[0].interrupt_started.wait()
    await asyncio.sleep(0.02)
    assert sum(event["type"] == "response.begin" for event in websocket.control) == 1
    runners[0].interrupt_release.set()
    stopped = await websocket.wait_control("playback.stop")
    ended = await websocket.wait_control("response.end")
    media_after_cancel = len(websocket.media)
    await runners[0].emit(AgentOutputSegment(1, [pcm_frame(index) for index in range(3)]))
    await asyncio.sleep(0.08)
    websocket.disconnect()
    await asyncio.wait_for(task, timeout=2.0)

    assert runners[0].interrupts == 1
    assert stopped["target"] == {"response_id": begin["response_id"], "generation": begin["generation"]}
    assert ended["outcome"] == "cancelled"
    assert len(websocket.media) == media_after_cancel
    assert runners[0].playback == []


@pytest.mark.integration
async def test_legacy_device_barge_in_cancel_fails_closed() -> None:
    websocket = FakeWebSocket()
    connection, runners = create_connection(
        websocket,
        trigger_frames=3,
        output_frames=12,
        limits=RvaRuntimeLimits(playback_prebuffer_packets=0),
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    websocket.feed_media(uplink_packets(1)[0])
    begin = await websocket.wait_control("response.begin")
    await websocket.wait_media_count(1)
    websocket.inbound.put_nowait(
        {
            "type": "websocket.receive",
            "text": json.dumps(
                {
                    "type": "response.cancel",
                    "session_id": "session-001",
                    "session_epoch": "grant-epoch-001",
                    "target": {"response_id": begin["response_id"], "generation": begin["generation"]},
                    "reason": "barge_in",
                }
            ),
        }
    )

    await asyncio.wait_for(task, timeout=2.0)

    assert websocket.closed == [(1_002, "protocol_error")]
    assert runners[0].interrupts == 0
    assert not any(event["type"] == "playback.stop" for event in websocket.control)


@pytest.mark.integration
async def test_layered_interruption_policy_cancels_active_playback_on_explicit_phrase() -> None:
    websocket = FakeWebSocket()
    policy = LayeredInterruptionPolicy(InterruptionPolicyConfig())
    connection, runners = create_connection(
        websocket,
        trigger_frames=3,
        output_frames=60,
        limits=RvaRuntimeLimits(playback_prebuffer_packets=0),
        interruption_policy=policy,
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    websocket.feed_media(uplink_packets(1)[0])
    begin = await websocket.wait_control("response.begin")
    await websocket.wait_media_count(1)
    websocket.inbound.put_nowait(
        {
            "type": "websocket.receive",
            "text": json.dumps(
                {
                    "type": "playback.started",
                    "session_id": "session-001",
                    "session_epoch": "grant-epoch-001",
                    "target": {"response_id": begin["response_id"], "generation": begin["generation"]},
                    "first_media_sequence": 0,
                }
            ),
        }
    )
    await runners[0].playback_started_event.wait()

    connection._emit_user_transcript("停一下", True)  # noqa: SLF001
    stopped = await websocket.wait_control("playback.stop")
    ended = await websocket.wait_control("response.end")
    websocket.disconnect()
    await asyncio.wait_for(task, timeout=2.0)

    assert stopped["target"] == {"response_id": begin["response_id"], "generation": begin["generation"]}
    assert ended["outcome"] == "cancelled"
    assert runners[0].interrupts == 1


@pytest.mark.integration
async def test_layered_interruption_policy_keeps_playback_for_backchannel() -> None:
    websocket = FakeWebSocket()
    policy = LayeredInterruptionPolicy(InterruptionPolicyConfig())
    connection, runners = create_connection(
        websocket,
        trigger_frames=3,
        output_frames=12,
        limits=RvaRuntimeLimits(playback_prebuffer_packets=0),
        interruption_policy=policy,
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    websocket.feed_media(uplink_packets(1)[0])
    begin = await websocket.wait_control("response.begin")
    await websocket.wait_media_count(1)
    websocket.inbound.put_nowait(
        {
            "type": "websocket.receive",
            "text": json.dumps(
                {
                    "type": "playback.started",
                    "session_id": "session-001",
                    "session_epoch": "grant-epoch-001",
                    "target": {"response_id": begin["response_id"], "generation": begin["generation"]},
                    "first_media_sequence": 0,
                }
            ),
        }
    )
    await runners[0].playback_started_event.wait()

    connection._emit_user_transcript("嗯嗯", True)  # noqa: SLF001
    end = await websocket.wait_control("response.end")
    websocket.disconnect()
    await asyncio.wait_for(task, timeout=2.0)

    assert end["type"] == "response.end"
    assert not any(event["type"] == "response.cancelled" for event in websocket.control)
    assert runners[0].interrupts == 0


@pytest.mark.integration
async def test_disconnect_closes_runner_and_every_connection_task() -> None:
    websocket = FakeWebSocket()
    connection, runners = create_connection(websocket, trigger_frames=1_000)
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    websocket.disconnect()

    await asyncio.wait_for(task, timeout=2.0)

    assert runners[0].close_calls == 1
    assert runners[0].terminal.result().kind is AgentRunnerTerminalKind.OWNER_CLOSED
    assert websocket.closed == [(1_000, "normal")]
    assert connection.binding.closed is True
    assert not connection._tasks and not connection._aux_tasks  # noqa: SLF001


@pytest.mark.integration
async def test_agent_runtime_terminal_sends_stable_error_then_closes_for_fresh_reopen() -> None:
    websocket = FakeWebSocket()
    connection, runners = create_connection(websocket, trigger_frames=1_000)
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")

    runners[0].fail_runtime()
    error = await websocket.wait_control("session.error")
    await asyncio.wait_for(task, timeout=2.0)

    assert error["code"] == "agent_runtime_failed"
    assert error["retryable"] is True
    assert error["message"] == "Agent runtime terminated; reconnect required"
    assert websocket.closed == [(1_011, "runtime_failure")]
    assert runners[0].close_calls == 1
    assert not connection._tasks and not connection._aux_tasks  # noqa: SLF001


@pytest.mark.integration
async def test_agent_runtime_terminal_remains_primary_when_error_notification_send_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeWebSocket()
    connection, runners = create_connection(
        websocket,
        trigger_frames=1_000,
        limits=RvaRuntimeLimits(
            queue_timeout_seconds=0.03,
            wire_send_timeout_seconds=0.03,
            close_timeout_seconds=0.1,
            agent_close_stage_timeout_seconds=0.05,
        ),
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    send_started = asyncio.Event()

    async def stalled_send_text(_payload: str) -> None:
        send_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(websocket, "send_text", stalled_send_text)
    runners[0].fail_runtime()
    await asyncio.wait_for(send_started.wait(), timeout=1.0)
    await asyncio.wait_for(task, timeout=1.0)

    assert websocket.closed == [(1_011, "runtime_failure")]
    assert runners[0].close_calls == 1
    assert not connection._tasks and not connection._aux_tasks  # noqa: SLF001


@pytest.mark.integration
async def test_freshness_close_remains_primary_when_error_notification_send_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeWebSocket()
    connection, runners = create_connection(
        websocket,
        trigger_frames=1_000,
        limits=RvaRuntimeLimits(
            queue_timeout_seconds=0.03,
            wire_send_timeout_seconds=0.03,
            close_timeout_seconds=0.1,
            agent_close_stage_timeout_seconds=0.05,
        ),
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    send_started = asyncio.Event()

    async def stalled_send_text(_payload: str) -> None:
        send_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(websocket, "send_text", stalled_send_text)
    connection._report_failure(  # noqa: SLF001
        RvaOverloadedError(
            "stale media",
            source="opus_input_stale",
            qsize=1,
            capacity=3,
            media_age_ms=700,
            dropped_packets=1,
            fresh_packet_available=True,
        )
    )
    await asyncio.wait_for(send_started.wait(), timeout=1.0)
    await asyncio.wait_for(task, timeout=1.0)

    assert websocket.closed == [(1_013, "media_overloaded")]
    assert runners[0].close_calls == 1
    assert not connection._tasks and not connection._aux_tasks  # noqa: SLF001


@pytest.mark.integration
async def test_idle_session_is_closed_at_the_advertised_deadline() -> None:
    websocket = FakeWebSocket()
    connection, runners = create_connection(
        websocket,
        trigger_frames=1_000,
        limits=RvaRuntimeLimits(idle_timeout_seconds=0.05),
    )
    websocket.feed_open()

    await asyncio.wait_for(connection.run(), timeout=1.0)

    assert runners[0].close_calls == 1
    assert websocket.closed == [(1_000, "idle_timeout")]


@pytest.mark.integration
async def test_udp_grant_expiry_requests_planned_session_rotation() -> None:
    websocket = FakeWebSocket()
    connection, runners = create_connection(websocket, trigger_frames=1_000)
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")

    connection._report_failure(UdpGrantExpiredError("sensitive expiry detail"))  # noqa: SLF001
    await asyncio.wait_for(task, timeout=2.0)

    assert runners[0].close_calls == 1
    assert websocket.closed == [(1_000, "udp_grant_expired")]


@pytest.mark.integration
async def test_protocol_failure_logs_only_stable_error_code(caplog: pytest.LogCaptureFixture) -> None:
    websocket = FakeWebSocket()
    connection, _ = create_connection(websocket, trigger_frames=1_000)
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")

    with caplog.at_level("WARNING"):
        connection._report_failure(RvaBindingError("invalid_test_control", "sensitive detail"))  # noqa: SLF001
        await asyncio.wait_for(task, timeout=2.0)

    assert websocket.closed == [(1_002, "protocol_error")]
    assert "error_code=invalid_test_control" in caplog.text
    assert "sensitive detail" not in caplog.text


@pytest.mark.unit
async def test_nonzero_udp_uplink_generation_is_dropped_without_failing_session() -> None:
    websocket = FakeWebSocket()
    connection, _ = create_connection(websocket)

    await connection._receive_udp_audio(b"stale-opus", 960, 1)  # noqa: SLF001

    assert connection._audio_port.queue.empty()  # noqa: SLF001
    assert connection._failures.empty()  # noqa: SLF001


class SlowRunner(FakeRunner):
    def __init__(
        self,
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        response_end: Callable[[int], None],
    ) -> None:
        super().__init__(emit, response_end, trigger_frames=1_000)
        self.release = asyncio.Event()

    async def push_audio(self, frame: PcmFrame) -> None:
        self.pushes += 1
        await self.release.wait()


@pytest.mark.integration
async def test_input_queue_short_burst_waits_for_consumer_and_recovers() -> None:
    websocket = FakeWebSocket()
    runners: list[SlowRunner] = []

    def factory(
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        stop: Callable[[int], None],
    ) -> SlowRunner:
        runner = SlowRunner(emit, stop)
        runners.append(runner)
        return runner

    connection = RvaWssConnection(
        websocket,  # type: ignore[arg-type]
        expected_device_id="device-001",
        session_id="session-001",
        session_epoch="grant-epoch-001",
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        runner_factory=factory,
        limits=RvaRuntimeLimits(input_queue_packets=1, queue_timeout_seconds=0.2),
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    packets = uplink_packets(4)
    websocket.feed_media(packets[0])
    await wait_until(lambda: runners[0].pushes == 1)
    for packet in packets[1:]:
        websocket.feed_media(packet)

    await asyncio.sleep(0.1)
    assert not task.done()
    runners[0].release.set()
    await wait_until(lambda: runners[0].pushes == 12)
    websocket.disconnect()
    await asyncio.wait_for(task, timeout=2.0)

    assert websocket.closed == [(1_000, "normal")]


@pytest.mark.unit
async def test_input_queue_close_wakes_blocked_producer_without_late_enqueue() -> None:
    websocket = FakeWebSocket()
    connection, _ = create_connection(
        websocket,
        limits=RvaRuntimeLimits(input_queue_packets=1, queue_timeout_seconds=0.2),
    )
    port = connection._audio_port  # noqa: SLF001
    first = InboundAudioPacket(0, 0, b"first")
    second = InboundAudioPacket(1, 960, b"late")
    await port.receive_audio(first)
    pending = asyncio.create_task(port.receive_audio(second))
    await asyncio.sleep(0)

    await port.close()
    await asyncio.wait_for(pending, timeout=0.1)

    assert await port.queue.get() is None
    assert port.queue.empty()


@pytest.mark.unit
async def test_input_timeline_accepts_wrap_and_reanchors_clock_skew() -> None:
    websocket = FakeWebSocket()
    clock = MutableMonotonicClock()
    connection, _ = create_connection(websocket, clock=clock)
    port = connection._audio_port  # noqa: SLF001

    timestamp = 0xFFFFFC40
    await port.receive_audio(InboundAudioPacket(0, timestamp, b"first"))
    queued = await port.queue.get()
    assert queued is not None
    port.mark_consumed(queued)

    for sequence in range(1, 10_100):
        clock.advance(0.06006)  # 1,000 ppm slower than the nominal media clock.
        timestamp = (timestamp + 960) & 0xFFFFFFFF
        await port.receive_audio(InboundAudioPacket(sequence, timestamp, b"audio"))
        queued = await port.queue.get()
        assert queued is not None
        assert queued.deadline_at > clock()
        port.mark_consumed(queued)


@pytest.mark.unit
async def test_input_timeline_does_not_treat_endpoint_clock_skew_as_backlog() -> None:
    websocket = FakeWebSocket()
    clock = MutableMonotonicClock()
    connection, _ = create_connection(websocket, clock=clock)
    port = connection._audio_port  # noqa: SLF001

    await port.receive_audio(InboundAudioPacket(0, 0, b"first"))
    queued = await port.queue.get()
    assert queued is not None
    port.mark_consumed(queued)

    for sequence in range(1, 1_000):
        clock.advance(0.06118)  # Endpoint and worker clocks are not phase locked.
        await port.receive_audio(InboundAudioPacket(sequence, sequence * 960, b"audio"))
        queued = await port.queue.get()
        assert queued is not None
        assert queued.deadline_at > clock()
        port.mark_consumed(queued)


@pytest.mark.unit
async def test_input_timeline_does_not_reanchor_a_stalled_tcp_burst() -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(input_queue_packets=8, uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001

    await port.receive_audio(InboundAudioPacket(0, 0, b"first"))
    first = port.queue.get_nowait()
    assert first is not None
    port.mark_consumed(first)
    clock.advance(0.06118)
    await port.receive_audio(InboundAudioPacket(1, 960, b"paced"))
    paced = port.queue.get_nowait()
    assert paced is not None
    assert paced.deadline_at > clock()
    port.mark_consumed(paced)

    clock.advance(0.9)
    for sequence in range(2, 7):
        await port.receive_audio(InboundAudioPacket(sequence, sequence * 960, b"burst"))
        queued = port.queue.get_nowait()
        assert queued is not None
        assert queued.deadline_at <= clock()


@pytest.mark.unit
async def test_input_timeline_does_not_forget_prior_transport_delay() -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001

    await port.receive_audio(InboundAudioPacket(0, 0, b"first"))
    first = port.queue.get_nowait()
    assert first is not None
    port.mark_consumed(first)

    clock.advance(0.36)  # 60 ms media plus a 300 ms transport stall.
    await port.receive_audio(InboundAudioPacket(1, 960, b"delayed"))
    delayed = port.queue.get_nowait()
    assert delayed is not None
    assert delayed.deadline_at > clock()
    port.mark_consumed(delayed)

    for sequence in range(2, 20):
        clock.advance(0.06)
        await port.receive_udp_audio(InboundAudioPacket(sequence, sequence * 960, b"paced"))
        paced = port.queue.get_nowait()
        assert paced is not None
        assert paced.deadline_at > clock()
        port.mark_consumed(paced)

    clock.advance(0.36)  # A second 300 ms stall exceeds the cumulative budget.
    await port.receive_audio(InboundAudioPacket(20, 20 * 960, b"too-old"))
    too_old = port.queue.get_nowait()
    assert too_old is not None
    assert too_old.deadline_at <= clock()


@pytest.mark.unit
async def test_input_timeline_rebase_preserves_existing_phase_delay() -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(FakeWebSocket(), clock=clock)
    port = connection._audio_port  # noqa: SLF001

    await port.receive_audio(InboundAudioPacket(0, 0, b"first"))
    first = port.queue.get_nowait()
    assert first is not None
    port.mark_consumed(first)
    clock.advance(0.36)
    await port.receive_audio(InboundAudioPacket(1, 960, b"delayed"))
    delayed = port.queue.get_nowait()
    assert delayed is not None
    port.mark_consumed(delayed)

    queued = delayed
    for sequence in range(2, 502):
        clock.advance(0.06)
        await port.receive_audio(InboundAudioPacket(sequence, sequence * 960, b"paced"))
        queued = port.queue.get_nowait()
        assert queued is not None
        port.mark_consumed(queued)

    assert clock() - queued.expected_at == pytest.approx(0.3)
    assert queued.deadline_at - clock() == pytest.approx(0.3)


@pytest.mark.unit
async def test_input_timeline_rejects_duplicate_or_non_cadenced_timestamp() -> None:
    websocket = FakeWebSocket()
    clock = MutableMonotonicClock()
    connection, _ = create_connection(websocket, clock=clock)
    port = connection._audio_port  # noqa: SLF001

    await port.receive_audio(InboundAudioPacket(0, 0, b"first"))
    queued = await port.queue.get()
    assert queued is not None
    port.mark_consumed(queued)
    clock.advance(0.06)

    with pytest.raises(RvaBindingError, match="invalid_media_timestamp"):
        await port.receive_audio(InboundAudioPacket(1, 961, b"invalid-cadence"))

    with pytest.raises(RvaBindingError, match="invalid_media_timestamp"):
        await port.receive_audio(InboundAudioPacket(1, 0, b"duplicate"))


@pytest.mark.unit
async def test_udp_input_timeline_reanchors_jittered_clock_after_accumulated_future_lead(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001

    await port.receive_audio(InboundAudioPacket(0, 0, b"first"))
    first = port.queue.get_nowait()
    assert first is not None
    port.mark_consumed(first)

    for sequence in range(1, 2_400):
        # Both intervals remain inside the pacing tolerance. Their corrections
        # cancel after clamping, while the small average clock lead accumulates.
        clock.advance(0.074 if sequence % 2 else 0.0455)
        await port.receive_udp_audio(InboundAudioPacket(sequence, sequence * 960, b"jittered"))
        queued = port.queue.get_nowait()
        assert queued is not None
        assert queued.deadline_at > clock()
        port.mark_consumed(queued)

    assert sum("reason=accumulated_future action=reanchor" in message for message in caplog.messages) == 1


@pytest.mark.unit
async def test_wss_input_timeline_reanchors_bounded_clock_skew(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001

    await port.receive_audio(InboundAudioPacket(0, 0, b"first"))
    first = port.queue.get_nowait()
    assert first is not None
    port.mark_consumed(first)
    for sequence in range(1, 2_400):
        clock.advance(0.074 if sequence % 2 else 0.0455)
        await port.receive_audio(InboundAudioPacket(sequence, sequence * 960, b"jittered"))
        queued = port.queue.get_nowait()
        assert queued is not None
        assert queued.deadline_at > clock()
        port.mark_consumed(queued)

    assert sum("reason=accumulated_future action=reanchor" in message for message in caplog.messages) == 1


@pytest.mark.unit
async def test_wss_input_timeline_rejects_burst_beyond_freshness_budget(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001

    await port.receive_audio(InboundAudioPacket(0, 0, b"first"))
    first = port.queue.get_nowait()
    assert first is not None
    port.mark_consumed(first)

    for sequence in range(1, 11):
        await port.receive_audio(InboundAudioPacket(sequence, sequence * 960, b"bounded-burst"))
        queued = port.queue.get_nowait()
        assert queued is not None
        port.mark_consumed(queued)

    with pytest.raises(RvaBindingError, match="invalid_media_timestamp"):
        await port.receive_audio(InboundAudioPacket(11, 11 * 960, b"excess-burst"))

    assert any("reason=wss_future_burst action=reject" in message for message in caplog.messages)


@pytest.mark.unit
async def test_wss_input_timeline_retains_burst_budget_after_long_paced_session() -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001

    await port.receive_audio(InboundAudioPacket(0, 0, b"first"))
    first = port.queue.get_nowait()
    assert first is not None
    port.mark_consumed(first)

    for sequence in range(1, 2_000):
        clock.advance(0.06)
        await port.receive_audio(InboundAudioPacket(sequence, sequence * 960, b"paced"))
        queued = port.queue.get_nowait()
        assert queued is not None
        port.mark_consumed(queued)

    for sequence in range(2_000, 2_010):
        await port.receive_audio(InboundAudioPacket(sequence, sequence * 960, b"bounded-burst"))
        queued = port.queue.get_nowait()
        assert queued is not None
        port.mark_consumed(queued)

    with pytest.raises(RvaBindingError, match="invalid_media_timestamp"):
        await port.receive_audio(InboundAudioPacket(2_010, 2_010 * 960, b"excess-burst"))


@pytest.mark.unit
async def test_input_timeline_rejects_oversized_cadenced_future_jump() -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001

    await port.receive_audio(InboundAudioPacket(0, 0, b"first"))
    first = port.queue.get_nowait()
    assert first is not None
    port.mark_consumed(first)
    clock.advance(0.06)

    with pytest.raises(RvaBindingError, match="invalid_media_timestamp"):
        await port.receive_udp_audio(InboundAudioPacket(1, 960 * 20, b"oversized-jump"))


@pytest.mark.unit
@pytest.mark.parametrize("stall_seconds", [0.1, 0.3])
async def test_subbudget_input_stall_remains_admissible(stall_seconds: float) -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(uplink_max_age_seconds=0.6),
        clock=clock,
    )
    await connection._audio_port.receive_audio(InboundAudioPacket(0, 0, b"audio"))  # noqa: SLF001
    queued = connection._audio_port.queue.get_nowait()  # noqa: SLF001
    assert queued is not None
    clock.advance(stall_seconds)

    remaining = connection._remaining_uplink_budget(queued)  # noqa: SLF001

    assert remaining == pytest.approx(0.6 - stall_seconds)
    assert connection._audio_port.queue.empty()  # noqa: SLF001


@pytest.mark.unit
async def test_isolated_stale_head_is_dropped_to_fresh_live_edge_before_runner() -> None:
    websocket = FakeWebSocket()
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        websocket,
        limits=RvaRuntimeLimits(input_queue_packets=3, uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001
    await connection.binding.receive_control(session_open())
    await port.receive_audio(InboundAudioPacket(0, 0, b"stale"))
    clock.advance(0.7)
    await port.receive_audio(InboundAudioPacket(1, 12 * 960, b"fresh"))
    clock.advance(0.11)
    runner = FakeRunner(connection._emit_segment, connection._request_response_end, trigger_frames=1_000)  # noqa: SLF001
    connection._runner = runner  # noqa: SLF001
    connection._codec = ScriptedDecodeCodec([True])  # type: ignore[assignment]  # noqa: SLF001

    task = asyncio.create_task(connection._input_loop())  # noqa: SLF001
    await wait_until(lambda: runner.pushes == 3)
    await port.close()
    await asyncio.wait_for(task, timeout=1.0)

    assert connection._recovered_stale_packets == 1  # noqa: SLF001
    assert runner.pushes == 3
    assert port.queue.empty()


@pytest.mark.unit
async def test_current_only_stale_packet_is_not_misclassified_as_backpressure() -> None:
    websocket = FakeWebSocket()
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        websocket,
        limits=RvaRuntimeLimits(input_queue_packets=3, uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001
    await connection.binding.receive_control(session_open())
    await port.receive_audio(InboundAudioPacket(0, 0, b"stale"))
    queued = port.queue.get_nowait()
    assert queued is not None
    clock.advance(0.7)

    error = connection._stale_uplink_error(queued)  # noqa: SLF001

    assert error.source == "opus_input_stale"
    assert error.qsize == 0
    assert error.dropped_packets == 1
    assert error.fresh_packet_available is False
    assert port._consumer_timeline_timestamp is None  # noqa: SLF001

    assert connection._recover_isolated_stale(error) is True  # noqa: SLF001
    await port.receive_audio(InboundAudioPacket(1, 960, b"buffered"))
    assert port.queue.empty()

    clock.advance(0.06)
    await port.receive_audio(InboundAudioPacket(2, 2 * 960, b"paced-candidate"))
    assert port.queue.empty()

    clock.advance(0.06)
    await port.receive_audio(InboundAudioPacket(3, 3 * 960, b"fresh"))
    fresh = port.queue.get_nowait()
    assert fresh is not None
    assert fresh.packet.sequence == 3
    assert fresh.deadline_at > clock()


@pytest.mark.unit
async def test_isolated_stale_recovery_discards_wss_catchup_burst_until_live_edge() -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(input_queue_packets=16, uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001
    await connection.binding.receive_control(session_open())
    await port.receive_audio(InboundAudioPacket(0, 0, b"stale"))
    queued = port.queue.get_nowait()
    assert queued is not None
    clock.advance(0.7)

    assert connection._recover_isolated_stale(connection._stale_uplink_error(queued)) is True  # noqa: SLF001
    for sequence in range(1, 13):
        await port.receive_audio(InboundAudioPacket(sequence, sequence * 960, b"catchup"))
    assert port.queue.empty()

    clock.advance(0.06)
    await port.receive_audio(InboundAudioPacket(13, 13 * 960, b"paced-candidate"))
    assert port.queue.empty()
    clock.advance(0.06)
    await port.receive_audio(InboundAudioPacket(14, 14 * 960, b"live"))
    live = port.queue.get_nowait()
    assert live is not None
    assert live.packet.sequence == 14
    assert live.deadline_at > clock()
    assert connection._recovered_catchup_packets == 13  # noqa: SLF001


@pytest.mark.unit
async def test_wss_catchup_does_not_treat_a_long_burst_pause_as_live_edge() -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(input_queue_packets=16, uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001
    await connection.binding.receive_control(session_open())
    await port.receive_audio(InboundAudioPacket(0, 0, b"stale"))
    queued = port.queue.get_nowait()
    assert queued is not None
    clock.advance(0.7)
    assert connection._recover_isolated_stale(connection._stale_uplink_error(queued)) is True  # noqa: SLF001

    await port.receive_audio(InboundAudioPacket(1, 960, b"catchup"))
    clock.advance(0.3)
    await port.receive_audio(InboundAudioPacket(2, 2 * 960, b"paused-catchup"))
    await port.receive_audio(InboundAudioPacket(3, 3 * 960, b"catchup"))
    assert port.queue.empty()
    assert connection._recovered_catchup_packets == 0  # noqa: SLF001

    clock.advance(0.06)
    await port.receive_audio(InboundAudioPacket(4, 4 * 960, b"paced-candidate"))
    assert port.queue.empty()
    clock.advance(0.06)
    await port.receive_audio(InboundAudioPacket(5, 5 * 960, b"live"))
    live = port.queue.get_nowait()
    assert live is not None
    assert live.packet.sequence == 5
    assert connection._recovered_catchup_packets == 4  # noqa: SLF001


@pytest.mark.unit
async def test_isolated_stale_recovery_fails_closed_when_wss_catchup_does_not_converge() -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(input_queue_packets=16, uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001
    await connection.binding.receive_control(session_open())
    await port.receive_audio(InboundAudioPacket(0, 0, b"stale"))
    queued = port.queue.get_nowait()
    assert queued is not None
    clock.advance(0.7)
    assert connection._recover_isolated_stale(connection._stale_uplink_error(queued)) is True  # noqa: SLF001

    with pytest.raises(RvaOverloadedError) as captured:
        for sequence in range(1, 22):
            await port.receive_audio(InboundAudioPacket(sequence, sequence * 960, b"catchup"))

    assert captured.value.source == "opus_input_backpressure"
    assert captured.value.dropped_packets == 21
    assert port.queue.empty()
    assert connection._recovered_catchup_packets == 0  # noqa: SLF001


@pytest.mark.unit
async def test_pending_queue_producer_prevents_isolated_stale_recovery() -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(input_queue_packets=1, uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001
    await connection.binding.receive_control(session_open())
    await port.receive_audio(InboundAudioPacket(0, 0, b"stale"))
    pending = asyncio.create_task(port.receive_audio(InboundAudioPacket(1, 960, b"pending")))
    await wait_until(lambda: port._pending_audio_puts == 1)  # noqa: SLF001

    current = port.queue.get_nowait()
    assert current is not None
    clock.advance(0.7)
    error = connection._stale_uplink_error(current)  # noqa: SLF001

    assert error.source == "opus_input_backpressure"
    assert connection._recover_isolated_stale(error) is False  # noqa: SLF001
    pending.cancel()
    await asyncio.gather(pending, return_exceptions=True)
    assert port._pending_audio_puts == 0  # noqa: SLF001


@pytest.mark.unit
async def test_repeated_isolated_stale_packets_fail_closed_within_recovery_window() -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001
    await connection.binding.receive_control(session_open())

    recoveries: list[bool] = []
    await port.receive_audio(InboundAudioPacket(0, 0, b"stale"))
    queued = port.queue.get_nowait()
    assert queued is not None
    next_sequence = 1
    for recovery_index in range(3):
        clock.advance(0.7)
        recoveries.append(connection._recover_isolated_stale(connection._stale_uplink_error(queued)))  # noqa: SLF001
        if recovery_index < 2:
            await port.receive_audio(
                InboundAudioPacket(next_sequence, next_sequence * 960, b"catchup"),
            )
            assert port.queue.empty()
            next_sequence += 1
            clock.advance(0.06)
            await port.receive_audio(
                InboundAudioPacket(next_sequence, next_sequence * 960, b"paced-candidate"),
            )
            assert port.queue.empty()
            next_sequence += 1
            clock.advance(0.06)
            await port.receive_audio(
                InboundAudioPacket(next_sequence, next_sequence * 960, b"live"),
            )
            queued = port.queue.get_nowait()
            assert queued is not None
            next_sequence += 1

    assert recoveries == [True, True, False]
    assert connection._recovered_stale_packets == 2  # noqa: SLF001


@pytest.mark.unit
async def test_stale_after_partial_runner_push_still_fails_closed() -> None:
    clock = MutableMonotonicClock()
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(uplink_max_age_seconds=0.6),
        clock=clock,
    )
    port = connection._audio_port  # noqa: SLF001
    await connection.binding.receive_control(session_open())
    runner = FakeRunner(connection._emit_segment, connection._request_response_end, trigger_frames=1_000)  # noqa: SLF001

    async def delayed_push(_frame: PcmFrame) -> None:
        runner.pushes += 1
        clock.advance(0.7)

    runner.push_audio = delayed_push  # type: ignore[method-assign]
    connection._runner = runner  # noqa: SLF001
    connection._codec = ScriptedDecodeCodec([True])  # type: ignore[assignment]  # noqa: SLF001
    await port.receive_audio(InboundAudioPacket(0, 0, b"audio"))

    with pytest.raises(RvaOverloadedError) as captured:
        await connection._input_loop()  # noqa: SLF001

    assert captured.value.source == "opus_input_stale"
    assert runner.pushes == 1
    assert connection._recovered_stale_packets == 0  # noqa: SLF001


@pytest.mark.integration
async def test_input_media_timeline_age_fails_closed_before_stale_audio_reaches_runner(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = FakeWebSocket()
    clock = MutableMonotonicClock()
    runners: list[SlowRunner] = []

    def factory(
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        stop: Callable[[int], None],
    ) -> SlowRunner:
        runner = SlowRunner(emit, stop)
        runners.append(runner)
        return runner

    connection = RvaWssConnection(
        websocket,  # type: ignore[arg-type]
        expected_device_id="device-001",
        session_id="session-001",
        session_epoch="grant-epoch-001",
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        runner_factory=factory,
        limits=RvaRuntimeLimits(
            input_queue_packets=2,
            queue_timeout_seconds=0.2,
            uplink_max_age_seconds=0.6,
        ),
        clock=clock,
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    packets = uplink_packets(2)
    websocket.feed_media(packets[0])
    await wait_until(lambda: runners[0].pushes == 1)
    clock.advance(0.2)
    websocket.feed_media(packets[1])
    await wait_until(lambda: connection._audio_port.queue.qsize() == 1)  # noqa: SLF001
    clock.advance(0.7)

    with caplog.at_level("INFO"):
        runners[0].release.set()
        error = await websocket.wait_control("session.error")
        await asyncio.wait_for(task, timeout=1.0)

    assert runners[0].pushes == 1
    assert error["code"] == "media_overloaded"
    assert error["retryable"] is True
    assert websocket.closed == [(1_013, "media_overloaded")]
    assert "overload_source=opus_input_backpressure" in caplog.text
    assert "overload_media_age_ms=900" in caplog.text
    assert "overload_dropped_packets=2" in caplog.text
    assert "overload_fresh_packet_available=false" in caplog.text


@pytest.mark.integration
async def test_input_queue_overload_fails_connection_explicitly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = FakeWebSocket()
    runners: list[SlowRunner] = []

    def factory(
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        stop: Callable[[int], None],
    ) -> SlowRunner:
        runner = SlowRunner(emit, stop)
        runners.append(runner)
        return runner

    connection = RvaWssConnection(
        websocket,  # type: ignore[arg-type]
        expected_device_id="device-001",
        session_id="session-001",
        session_epoch="grant-epoch-001",
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        runner_factory=factory,
        limits=RvaRuntimeLimits(input_queue_packets=1, queue_timeout_seconds=0.05),
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    with caplog.at_level("INFO"):
        for packet in uplink_packets(4):
            websocket.feed_media(packet)

        await asyncio.wait_for(task, timeout=2.0)

    assert websocket.closed == [(1_013, "media_overloaded")]
    assert runners[0].close_calls == 1
    assert "overload_source=opus_input" in caplog.text
    assert "overload_qsize=1 overload_capacity=1" in caplog.text


@pytest.mark.integration
async def test_runtime_control_ack_timeout_is_not_misclassified_as_handshake_timeout(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    websocket = FakeWebSocket()
    connection, _ = create_connection(
        websocket,
        trigger_frames=1_000,
        limits=RvaRuntimeLimits(queue_timeout_seconds=0.2, wire_send_timeout_seconds=0.03),
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")

    send_started = asyncio.Event()
    send_release = asyncio.Event()

    async def stalled_send_text(_payload: str) -> None:
        send_started.set()
        await send_release.wait()

    async def control_with_reply(_raw: str) -> ControlEffect:
        return ControlEffect(outbound=(json.dumps({"type": "runtime.test"}),))

    monkeypatch.setattr(websocket, "send_text", stalled_send_text)
    monkeypatch.setattr(connection.binding, "receive_control", control_with_reply)
    websocket.inbound.put_nowait({"type": "websocket.receive", "text": "{}"})
    await asyncio.wait_for(send_started.wait(), timeout=1.0)
    with caplog.at_level("WARNING"):
        await asyncio.wait_for(task, timeout=1.0)

    assert websocket.closed == [(1_011, "control_timeout")]
    assert "rva_control_timeout" in caplog.text
    assert "stage=wire_send" in caplog.text


@pytest.mark.unit
async def test_control_queue_ack_timeout_reports_distinct_stage() -> None:
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(queue_timeout_seconds=0.01, wire_send_timeout_seconds=0.01),
    )

    with pytest.raises(RvaControlTimeoutError) as captured:
        await connection._send_control_serialized('{"type":"runtime.test"}')  # noqa: SLF001

    assert captured.value.stage == "queue_ack"
    assert captured.value.queue_size == 1
    assert captured.value.queue_capacity >= captured.value.queue_size


@pytest.mark.integration
async def test_stalled_wss_media_send_has_its_own_bounded_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    websocket = FakeWebSocket()
    connection, runners = create_connection(
        websocket,
        limits=RvaRuntimeLimits(
            queue_timeout_seconds=0.02,
            wire_send_timeout_seconds=0.03,
            playback_prebuffer_packets=0,
        ),
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")

    send_started = asyncio.Event()

    async def stalled_send_bytes(_payload: bytes) -> None:
        send_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(websocket, "send_bytes", stalled_send_bytes)
    try:
        # Exercise the downlink boundary directly. Routing this assertion through
        # uplink Opus decode and the fake runner makes unrelated executor/freshness
        # scheduling decide whether send_bytes is reached under a loaded test host.
        await runners[0].emit(AgentOutputSegment(1, [pcm_frame(index) for index in range(3)]))
        await asyncio.wait_for(send_started.wait(), timeout=1.0)
        await asyncio.wait_for(task, timeout=1.0)
    finally:
        await connection.close()
        await asyncio.gather(task, return_exceptions=True)

    assert websocket.closed == [(1_011, "media_send_timeout")]
    assert not any(task.get_name().endswith("-session-001") for task in asyncio.all_tasks())


class BlockingStartRunner(FakeRunner):
    async def start(self) -> None:
        await asyncio.Event().wait()


@pytest.mark.integration
async def test_runner_start_timeout_is_not_misclassified_as_handshake_timeout() -> None:
    websocket = FakeWebSocket()

    def factory(
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        stop: Callable[[int], None],
    ) -> BlockingStartRunner:
        return BlockingStartRunner(emit, stop, trigger_frames=1_000)

    connection = RvaWssConnection(
        websocket,  # type: ignore[arg-type]
        expected_device_id="device-001",
        session_id="session-001",
        session_epoch="grant-epoch-001",
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        runner_factory=factory,
        limits=RvaRuntimeLimits(runner_timeout_seconds=0.02),
    )
    websocket.feed_open()

    await asyncio.wait_for(connection.run(), timeout=1.0)

    assert websocket.closed == [(1_011, "runtime_start_timeout")]


class BlockingCloseRunner(FakeRunner):
    def __init__(
        self,
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        response_end: Callable[[int], None],
    ) -> None:
        super().__init__(emit, response_end, trigger_frames=1_000)
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    async def close(self) -> None:
        self.close_calls += 1
        self.close_started.set()
        await self.close_release.wait()


@pytest.mark.integration
async def test_close_wait_is_bounded_while_shielded_cleanup_continues() -> None:
    websocket = FakeWebSocket()
    runners: list[BlockingCloseRunner] = []

    def factory(
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        stop: Callable[[int], None],
    ) -> BlockingCloseRunner:
        runner = BlockingCloseRunner(emit, stop)
        runners.append(runner)
        return runner

    connection = RvaWssConnection(
        websocket,  # type: ignore[arg-type]
        expected_device_id="device-001",
        session_id="session-001",
        session_epoch="grant-epoch-001",
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        runner_factory=factory,
        limits=RvaRuntimeLimits(close_timeout_seconds=0.05, runner_timeout_seconds=1.0),
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    websocket.disconnect()
    await runners[0].close_started.wait()

    await asyncio.wait_for(task, timeout=0.2)
    assert websocket.closed == []
    runners[0].close_release.set()
    await connection.close()

    assert runners[0].close_calls == 1
    assert websocket.closed == [(1_000, "normal")]


@pytest.mark.integration
async def test_close_stage_timeout_does_not_orphan_nested_runner_task() -> None:
    websocket = FakeWebSocket()
    runners: list[BlockingCloseRunner] = []

    def factory(
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        stop: Callable[[int], None],
    ) -> BlockingCloseRunner:
        runner = BlockingCloseRunner(emit, stop)
        runners.append(runner)
        return runner

    connection = RvaWssConnection(
        websocket,  # type: ignore[arg-type]
        expected_device_id="device-001",
        session_id="session-001",
        session_epoch="grant-epoch-001",
        media_id=bytes.fromhex("0123456789abcdef"),
        media_epoch=7,
        runner_factory=factory,
        limits=RvaRuntimeLimits(
            agent_close_stage_timeout_seconds=0.05,
            close_timeout_seconds=0.5,
            runner_timeout_seconds=1.0,
        ),
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    websocket.disconnect()

    await asyncio.wait_for(task, timeout=1.0)

    assert runners[0].close_calls == 1
    assert websocket.closed == [(1_000, "normal")]
    assert not any(task.get_name().startswith("rva-runner-close-") for task in asyncio.all_tasks())
