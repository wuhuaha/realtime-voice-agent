from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

import pytest
from realtime_worker.agent import AgentOutputSegment
from realtime_worker.audio import PCM_SAMPLES, PcmFrame
from realtime_worker.bindings.rva import (
    RvaOpusCodec,
    RvaRuntimeLimits,
    RvaWssConnection,
    WssMediaFrame,
)
from realtime_worker.interruption import InterruptionPolicyConfig, LayeredInterruptionPolicy


def pcm_frame(sequence: int) -> PcmFrame:
    return PcmFrame(0, sequence, sequence * PCM_SAMPLES, b"\x00" * (PCM_SAMPLES * 2))


def session_open() -> str:
    return json.dumps(
        {
            "type": "session.open",
            "protocol_version": 2,
            "request_id": "open-001",
            "device_id": "device-001",
            "supported_media_profiles": ["wss-opus-v3"],
            "preferred_media_profile": "wss-opus-v3",
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
        self.playback_started_event = asyncio.Event()
        self.block_interrupt = False
        self.interrupt_started = asyncio.Event()
        self.interrupt_release = asyncio.Event()
        self._user_text: Callable[[str, bool], None] | None = None
        self._assistant_text: Callable[[str], None] | None = None

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


def create_connection(
    websocket: FakeWebSocket,
    *,
    trigger_frames: int = 9,
    output_frames: int = 10,
    limits: RvaRuntimeLimits | None = None,
    interruption_policy: LayeredInterruptionPolicy | None = None,
) -> tuple[RvaWssConnection, list[FakeRunner]]:
    runners: list[FakeRunner] = []

    def factory(
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        stop: Callable[[int], None],
    ) -> FakeRunner:
        runner = FakeRunner(emit, stop, trigger_frames=trigger_frames, output_frames=output_frames)
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
        limits=limits,
        interruption_policy=interruption_policy,
    )
    return connection, runners


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


@pytest.mark.unit
def test_runtime_passes_non_default_close_stage_timeout_to_session_state() -> None:
    connection, _ = create_connection(
        FakeWebSocket(),
        limits=RvaRuntimeLimits(agent_close_stage_timeout_seconds=0.125),
    )

    assert connection.binding._responses._state._close_stage_timeout_seconds == 0.125  # noqa: SLF001


@pytest.mark.unit
def test_rva_opus_codec_roundtrips_one_60ms_packet_to_three_pcm_frames() -> None:
    encoder = RvaOpusCodec()
    decoder = RvaOpusCodec()

    payload = encoder.encode_60ms([pcm_frame(index) for index in range(3)])
    decoded = decoder.decode_60ms(payload, sequence_start=12)

    assert 0 < len(payload) <= 1_200
    assert [frame.sequence for frame in decoded] == [12, 13, 14]
    assert all(len(frame.pcm) == PCM_SAMPLES * 2 for frame in decoded)


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
    assert runner.pushes == 9
    # Physical playout is endpoint-owned in rva-control-v2 and is only
    # acknowledged after playback.started/playback.ended arrive from the device.
    assert runner.playback == []
    assert runner.close_calls == 1
    assert websocket.closed == [(1_000, "normal")]


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
    assert websocket.closed == [(1_000, "normal")]
    assert connection.binding.closed is True


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
        await self.release.wait()


@pytest.mark.integration
async def test_input_queue_overload_fails_connection_explicitly() -> None:
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
        limits=RvaRuntimeLimits(input_queue_packets=1),
    )
    websocket.feed_open()
    task = asyncio.create_task(connection.run())
    await websocket.wait_control("session.opened")
    for packet in uplink_packets(4):
        websocket.feed_media(packet)

    await asyncio.wait_for(task, timeout=2.0)

    assert websocket.closed == [(1_013, "media_overloaded")]
    assert runners[0].close_calls == 1


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
