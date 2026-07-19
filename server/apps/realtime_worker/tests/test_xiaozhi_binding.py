from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
import time
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from av import CodecContext, Packet
from av.error import InvalidDataError
from realtime_worker.agent import AgentOutputSegment
from realtime_worker.audio import PCM_SAMPLES, PcmFrame
from realtime_worker.auth import AuthContext
from realtime_worker.bindings.xiaozhi import (
    XIAOZHI_OPUS_APPLICATION,
    XIAOZHI_OUTPUT_SAMPLE_RATE,
    SharedSessionAdmission,
    XiaozhiConnection,
    XiaozhiOpusCodec,
    XiaozhiOverloadedError,
    XiaozhiProtocolError,
    XiaozhiSessionRegistry,
    normalize_device_id,
    parse_client_hello,
    parse_client_message,
    resolve_xiaozhi_device_id,
)
from realtime_worker.config import Settings

FIXTURES = Path(__file__).parent / "fixtures" / "xiaozhi"


def fixture(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def pcm_frame(sequence: int, *, generation: int = 0) -> PcmFrame:
    samples = (
        int(1200 * math.sin(2 * math.pi * 440 * (sequence * PCM_SAMPLES + index) / 16_000))
        for index in range(PCM_SAMPLES)
    )
    return PcmFrame(generation, sequence, sequence * PCM_SAMPLES, struct.pack("<320h", *samples))


def test_fixed_source_hello_contract_is_accepted() -> None:
    hello = parse_client_hello(json.dumps(fixture("client_hello_v1.json")))

    assert hello.version == 1
    assert hello.sample_rate == 16_000
    assert hello.channels == 1
    assert hello.frame_duration_ms == 60


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 2),
        ("transport", "mqtt"),
        ("audio_params", {"format": "pcm", "sample_rate": 16_000, "channels": 1, "frame_duration": 60}),
        ("audio_params", {"format": "opus", "sample_rate": 24_000, "channels": 1, "frame_duration": 60}),
    ],
)
def test_unsupported_hello_variant_is_rejected(field: str, value: object) -> None:
    message = fixture("client_hello_v1.json")
    message[field] = value

    with pytest.raises(XiaozhiProtocolError):
        parse_client_hello(json.dumps(message))


def test_duplicate_json_key_is_rejected() -> None:
    with pytest.raises(XiaozhiProtocolError, match="duplicate"):
        parse_client_hello('{"type":"hello","type":"hello"}')


def test_listen_and_abort_require_the_active_session() -> None:
    listen = fixture("listen_realtime.json")
    listen["session_id"] = "session-1"
    abort = fixture("abort.json")
    abort["session_id"] = "session-1"

    assert parse_client_message(json.dumps(listen), "session-1").kind == "listen_start"
    assert parse_client_message(json.dumps(abort), "session-1").kind == "abort"
    with pytest.raises(XiaozhiProtocolError, match="session_id"):
        parse_client_message(json.dumps(listen), "session-other")


def test_xiaozhi_device_id_accepts_source_mac_header_without_weakening_auth_shape() -> None:
    assert normalize_device_id("AA:BB:CC:DD:EE:FF") == "AA-BB-CC-DD-EE-FF"
    assert normalize_device_id("550e8400-e29b-41d4-a716-446655440000") == ("550e8400-e29b-41d4-a716-446655440000")
    assert normalize_device_id("bad/device") is None


@pytest.mark.parametrize(
    ("device_id", "client_id", "expected"),
    [
        ("AA:BB:CC:DD:EE:FF", None, "AA-BB-CC-DD-EE-FF"),
        ("AA:BB:CC:DD:EE:FF", "client-uuid", "AA-BB-CC-DD-EE-FF"),
        (None, "client-uuid", None),
        ("AA:BB:CC:DD:EE:FF", "bad/client", None),
    ],
)
def test_physical_device_id_is_the_required_stable_principal(
    device_id: str | None, client_id: str | None, expected: str | None
) -> None:
    assert resolve_xiaozhi_device_id(device_id, client_id) == expected


def test_opus_codec_emits_one_60ms_packet_and_decodes_three_runtime_frames() -> None:
    codec = XiaozhiOpusCodec()
    packet = codec.encode_60ms([pcm_frame(0), pcm_frame(1), pcm_frame(2)])

    assert 0 < len(packet) < 4096
    decoded = codec.decode_60ms(packet, sequence_start=7)
    assert [frame.sequence for frame in decoded] == [7, 8, 9]
    assert all(len(frame.pcm) == PCM_SAMPLES * 2 for frame in decoded)


def test_output_codec_emits_exactly_60ms_of_24khz_opus() -> None:
    assert XIAOZHI_OPUS_APPLICATION == "audio"
    encoder = XiaozhiOpusCodec(encode_sample_rate=XIAOZHI_OUTPUT_SAMPLE_RATE)
    decoder = CodecContext.create("opus", "r")
    decoder.sample_rate = XIAOZHI_OUTPUT_SAMPLE_RATE
    decoder.open()

    for sequence in range(2):
        packet = encoder.encode_60ms([pcm_frame(sequence * 3 + offset) for offset in range(3)])
        decoded = decoder.decode(Packet(packet))
        assert sum(frame.samples / frame.sample_rate for frame in decoded) == pytest.approx(0.06)

        runtime_frames = XiaozhiOpusCodec().decode_60ms(packet, sequence_start=sequence * 3)
        assert len(runtime_frames) == 3


def test_malformed_opus_is_rejected() -> None:
    codec = XiaozhiOpusCodec()
    with pytest.raises((InvalidDataError, ValueError)):
        codec.decode_60ms(b"not-opus", sequence_start=0)


def test_audio_diagnostics_are_disabled_by_default(caplog: pytest.LogCaptureFixture) -> None:
    connection = XiaozhiConnection(
        object(),  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(_env_file=None, lab_token="test-token"),
    )

    assert connection._audio_diagnostics is None  # noqa: SLF001
    assert "audio diagnostics" not in caplog.text


def test_audio_diagnostics_log_first_packet_and_bounded_summaries_without_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="realtime_worker.bindings.xiaozhi")
    connection = XiaozhiConnection(
        object(),  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="private-device-id"),
        Settings(
            lab_token="validator-token-not-for-logs",
            xiaozhi_audio_diagnostics=True,
            xiaozhi_audio_diagnostics_interval_packets=2,
        ),
    )
    diagnostics = connection._audio_diagnostics  # noqa: SLF001
    assert diagnostics is not None
    frames = [pcm_frame(0), pcm_frame(1), pcm_frame(2)]

    for _ in range(4):
        diagnostics.observe(frames)

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3
    assert "event=first_packet opus_packets=1 window_opus_packets=1" in messages[0]
    assert "event=summary opus_packets=2 window_opus_packets=2" in messages[1]
    assert "event=summary opus_packets=4 window_opus_packets=2" in messages[2]
    assert all("decoded_pcm_samples=" in message for message in messages)
    assert all("pcm_peak=" in message and "pcm_rms=" in message for message in messages)
    assert all("pcm_nonzero_samples=" in message for message in messages)
    log_output = "\n".join(messages)
    assert "private-device-id" not in log_output
    assert "secret-token-not-for-logs" not in log_output
    assert repr(frames[0].pcm) not in log_output


@pytest.mark.asyncio
async def test_output_backpressure_fails_within_the_configured_bound() -> None:
    settings = Settings(
        lab_token="test-token",
        xiaozhi_media_queue_frames=4,
        xiaozhi_queue_timeout_seconds=0.01,
    )
    connection = XiaozhiConnection(
        object(),  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        settings,
    )
    for _ in range(4):
        await connection._enqueue("text", {"type": "test"}, 1)  # noqa: SLF001

    with pytest.raises(XiaozhiOverloadedError, match="output media queue"):
        await connection._enqueue("text", {"type": "test"}, 1)  # noqa: SLF001


class _FakeRunner:
    def __init__(self, emit: Callable[[AgentOutputSegment], Awaitable[None]]) -> None:
        self.emit = emit
        self.closed = False
        self.interrupts = 0
        self.producer_epoch = 1

    async def start(self) -> None:
        return None

    async def push_audio(self, frame: PcmFrame) -> None:
        return None

    async def commit_text(self, text: str) -> None:
        return None

    async def playback_started(self, created_at: float) -> None:
        return None

    async def playback_finished(self, position: float, interrupted: bool) -> None:
        return None

    async def interrupt(self) -> int:
        self.interrupts += 1
        self.producer_epoch += 1
        return self.producer_epoch

    async def close(self) -> None:
        self.closed = True


class _TextAwareRunner(_FakeRunner):
    def set_text_sinks(
        self,
        user_transcript: Callable[[str, bool], None],
        assistant_text: Callable[[str], None],
    ) -> None:
        user_transcript("你", False)
        user_transcript("你好", True)
        assistant_text("你好呀")


@pytest.mark.asyncio
async def test_runner_text_sinks_emit_xiaozhi_stt_and_sentence_start_wire() -> None:
    connection = XiaozhiConnection(
        object(),  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(lab_token="test-token"),
        runner_factory=lambda settings, emit, stop: _TextAwareRunner(emit),  # type: ignore[arg-type]
    )

    await connection._start_runner_locked()  # noqa: SLF001

    events = [(await connection._output.get()).payload for _ in range(3)]  # noqa: SLF001
    assert events == [
        {"session_id": connection.session_id, "type": "stt", "text": "你", "is_final": False},
        {"session_id": connection.session_id, "type": "stt", "text": "你好", "is_final": True},
        {
            "session_id": connection.session_id,
            "type": "tts",
            "state": "sentence_start",
            "text": "你好呀",
        },
    ]


@pytest.mark.asyncio
async def test_text_sink_queue_overflow_reports_bounded_connection_failure() -> None:
    connection = XiaozhiConnection(
        object(),  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(lab_token="test-token", xiaozhi_media_queue_frames=4),
    )
    for index in range(4):
        connection._enqueue_control_nowait({"type": "queued", "index": index})  # noqa: SLF001

    connection._emit_user_transcript("第五条", True)  # noqa: SLF001

    assert isinstance(connection._failures.get_nowait(), XiaozhiOverloadedError)  # noqa: SLF001


@pytest.mark.asyncio
async def test_streaming_assistant_text_coalesces_while_waiting_for_writer() -> None:
    connection = XiaozhiConnection(
        object(),  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(lab_token="test-token", xiaozhi_media_queue_frames=4),
    )

    connection._emit_assistant_text("你")  # noqa: SLF001
    connection._emit_assistant_text("你好")  # noqa: SLF001
    connection._emit_assistant_text("你好呀")  # noqa: SLF001

    assert connection._output.qsize() == 1  # noqa: SLF001
    item = connection._output.get_nowait()  # noqa: SLF001
    assert item.payload == {
        "session_id": connection.session_id,
        "type": "tts",
        "state": "sentence_start",
        "text": "你好呀",
    }


@pytest.mark.asyncio
async def test_abort_interrupts_same_producer_and_rejects_late_old_segments() -> None:
    runners: list[_FakeRunner] = []

    def factory(
        settings: Settings,
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        stop: Callable[[int], None],
    ):
        runner = _FakeRunner(emit)
        runners.append(runner)
        return runner

    connection = XiaozhiConnection(
        object(),  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(lab_token="test-token", xiaozhi_media_queue_frames=12),
        runner_factory=factory,  # type: ignore[arg-type]
    )
    connection._codec = XiaozhiOpusCodec()  # noqa: SLF001
    await connection._start_runner_locked()  # noqa: SLF001
    old_runner = runners[0]

    await asyncio.gather(
        connection._abort_current_response(),  # noqa: SLF001
        connection._abort_current_response(),  # noqa: SLF001
    )
    assert old_runner.closed is False
    assert old_runner.interrupts == 1
    assert len(runners) == 1
    assert (await connection._output.get()).interrupted is True  # noqa: SLF001

    await old_runner.emit(AgentOutputSegment(1, [pcm_frame(0), pcm_frame(1), pcm_frame(2)]))
    await old_runner.emit(AgentOutputSegment(1, [pcm_frame(3), pcm_frame(4), pcm_frame(5)]))
    assert connection._output.empty()  # noqa: SLF001

    await old_runner.emit(AgentOutputSegment(2, [pcm_frame(0), pcm_frame(1), pcm_frame(2)]))
    assert connection._output.qsize() == 3  # noqa: SLF001
    while not connection._output.empty():  # noqa: SLF001
        connection._output.get_nowait()  # noqa: SLF001
    await old_runner.emit(AgentOutputSegment(1, [pcm_frame(6), pcm_frame(7), pcm_frame(8)]))
    assert connection._output.empty()  # noqa: SLF001


@pytest.mark.asyncio
async def test_agent_stop_callback_storm_has_one_bounded_task_and_one_stop() -> None:
    connection = XiaozhiConnection(
        object(),  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(lab_token="test-token"),
    )
    for _ in range(100):
        connection._request_stop(2)  # noqa: SLF001
    task = connection._stop_task  # noqa: SLF001
    assert task is not None
    await task
    assert connection._output.qsize() == 1  # noqa: SLF001


class _StalledWriter:
    async def send_json(self, payload: dict[str, object]) -> None:
        return None

    async def send_bytes(self, payload: bytes) -> None:
        await asyncio.Event().wait()


class _TimedWriter:
    def __init__(self) -> None:
        self.audio_sent_at: list[float] = []
        self.stop_sent = asyncio.Event()

    async def send_json(self, payload: dict[str, object]) -> None:
        if payload.get("state") == "stop":
            self.stop_sent.set()

    async def send_bytes(self, payload: bytes) -> None:
        self.audio_sent_at.append(time.monotonic())


@pytest.mark.asyncio
async def test_writer_prebuffers_then_uses_absolute_playback_cadence() -> None:
    websocket = _TimedWriter()
    connection = XiaozhiConnection(
        websocket,  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(lab_token="test-token"),
    )
    generation = connection._generation  # noqa: SLF001
    await connection._enqueue("text", connection._tts_event("start"), generation)  # noqa: SLF001
    for _ in range(6):
        await connection._enqueue("audio", b"opus", generation, samples=960)  # noqa: SLF001
    await connection._enqueue("text", connection._tts_event("stop"), generation)  # noqa: SLF001

    writer = asyncio.create_task(connection._writer_loop())  # noqa: SLF001
    try:
        await asyncio.wait_for(websocket.stop_sent.wait(), timeout=1.0)
        sent = websocket.audio_sent_at
        assert len(sent) == 6
        assert sent[3] - sent[0] < 0.02
        assert sent[4] - sent[0] == pytest.approx(0.06, abs=0.025)
        assert sent[5] - sent[0] == pytest.approx(0.12, abs=0.025)
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


@pytest.mark.asyncio
async def test_writer_interrupt_wakes_pacing_wait_and_drops_stale_audio() -> None:
    websocket = _TimedWriter()
    connection = XiaozhiConnection(
        websocket,  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(lab_token="test-token"),
    )
    generation = connection._generation  # noqa: SLF001
    await connection._enqueue("text", connection._tts_event("start"), generation)  # noqa: SLF001
    for _ in range(6):
        await connection._enqueue("audio", b"opus", generation, samples=960)  # noqa: SLF001

    writer = asyncio.create_task(connection._writer_loop())  # noqa: SLF001
    try:
        async with asyncio.timeout(0.5):
            while len(websocket.audio_sent_at) < 4:
                await asyncio.sleep(0)
        interrupted_at = time.monotonic()
        await connection._fence_playback()  # noqa: SLF001
        await asyncio.wait_for(websocket.stop_sent.wait(), timeout=0.1)
        assert time.monotonic() - interrupted_at < 0.1
        assert len(websocket.audio_sent_at) == 4
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


@pytest.mark.asyncio
async def test_writer_stall_turns_into_bounded_output_overload() -> None:
    connection = XiaozhiConnection(
        _StalledWriter(),  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(
            lab_token="test-token",
            xiaozhi_media_queue_frames=4,
            xiaozhi_queue_timeout_seconds=0.01,
        ),
    )
    connection._codec = XiaozhiOpusCodec()  # noqa: SLF001
    writer = asyncio.create_task(connection._writer_loop())  # noqa: SLF001
    try:
        with pytest.raises(XiaozhiOverloadedError, match="output media queue"):
            await connection._emit_segment(  # noqa: SLF001
                AgentOutputSegment(1, [pcm_frame(index) for index in range(30)])
            )
        assert isinstance(connection._failures.get_nowait(), XiaozhiOverloadedError)  # noqa: SLF001
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


class _HandshakeWebSocket:
    def __init__(self) -> None:
        self.hello_sent = asyncio.Event()
        self.closed: list[tuple[int, str]] = []
        self._first = True

    async def receive(self) -> dict[str, object]:
        if self._first:
            self._first = False
            return {"type": "websocket.receive", "text": json.dumps(fixture("client_hello_v1.json"))}
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    async def send_json(self, payload: dict[str, object]) -> None:
        if payload.get("type") == "hello":
            self.hello_sent.set()

    async def send_bytes(self, payload: bytes) -> None:
        return None

    async def close(self, *, code: int, reason: str) -> None:
        self.closed.append((code, reason))


def fake_runner_factory(
    settings: Settings,
    emit: Callable[[AgentOutputSegment], Awaitable[None]],
    stop: Callable[[int], None],
) -> _FakeRunner:
    return _FakeRunner(emit)


@pytest.mark.asyncio
async def test_unexpected_cancelled_child_is_a_runtime_failure_not_an_invalid_state_read() -> None:
    websocket = _HandshakeWebSocket()
    connection = XiaozhiConnection(
        websocket,  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(lab_token="test-token"),
        runner_factory=fake_runner_factory,  # type: ignore[arg-type]
    )
    run = asyncio.create_task(connection.run())
    await websocket.hello_sent.wait()
    reader = next(task for task in connection._tasks if "reader" in task.get_name())  # noqa: SLF001
    reader.cancel()
    await run
    assert websocket.closed == [(1011, "runtime_failure")]


@pytest.mark.asyncio
async def test_shutdown_cancellation_is_clean_and_idempotent() -> None:
    websocket = _HandshakeWebSocket()
    connection = XiaozhiConnection(
        websocket,  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(lab_token="test-token"),
        runner_factory=fake_runner_factory,  # type: ignore[arg-type]
    )
    run = asyncio.create_task(connection.run())
    await websocket.hello_sent.wait()
    await connection.close(code=1001, reason="server_shutdown")
    await run
    assert websocket.closed == [(1001, "server_shutdown")]


class _SelfCancellingCloseRunner(_FakeRunner):
    async def close(self) -> None:
        self.closed = True
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_runner_self_cancellation_does_not_escape_connection_cleanup() -> None:
    websocket = _HandshakeWebSocket()
    runner = _SelfCancellingCloseRunner(lambda segment: asyncio.sleep(0))
    connection = XiaozhiConnection(
        websocket,  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(lab_token="test-token"),
    )
    connection._runner = runner  # noqa: SLF001

    await connection.close(code=1001, reason="server_shutdown")

    assert runner.closed is True
    assert websocket.closed == [(1001, "server_shutdown")]


class _DisconnectWebSocket(_HandshakeWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.disconnect = asyncio.Event()

    async def receive(self) -> dict[str, object]:
        if self._first:
            return await super().receive()
        await self.disconnect.wait()
        return {"type": "websocket.disconnect", "code": 1000}


class _BlockingCloseRunner(_FakeRunner):
    def __init__(self, emit: Callable[[AgentOutputSegment], Awaitable[None]]) -> None:
        super().__init__(emit)
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await self.close_release.wait()
        self.closed = True


@pytest.mark.asyncio
async def test_disconnect_and_registry_close_share_one_cleanup_completion() -> None:
    websocket = _DisconnectWebSocket()
    admission = SharedSessionAdmission(1)
    runners: list[_BlockingCloseRunner] = []

    def factory(
        settings: Settings,
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        stop: Callable[[int], None],
    ) -> _BlockingCloseRunner:
        runner = _BlockingCloseRunner(emit)
        runners.append(runner)
        return runner

    registry = XiaozhiSessionRegistry(
        Settings(lab_token="test-token"),
        admission,
        runner_factory=factory,
    )
    run = asyncio.create_task(
        registry.run(websocket, AuthContext(tenant_id="lab", device_id="device-1"))  # type: ignore[arg-type]
    )
    await websocket.hello_sent.wait()
    websocket.disconnect.set()
    await runners[0].close_started.wait()

    registry_close = asyncio.create_task(registry.close())
    await asyncio.sleep(0)
    assert not run.done()
    assert not registry_close.done()

    runners[0].close_release.set()
    await asyncio.gather(run, registry_close)
    assert runners[0].closed is True
    assert admission.active_count == 0
    assert len(websocket.closed) == 1


class _StartupRunner(_FakeRunner):
    def __init__(
        self,
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        *,
        start_error: Exception | None = None,
        block_start: bool = False,
    ) -> None:
        super().__init__(emit)
        self.start_error = start_error
        self.block_start = block_start

    async def start(self) -> None:
        if self.start_error is not None:
            raise self.start_error
        if self.block_start:
            await asyncio.Event().wait()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["throw", "timeout"])
async def test_partial_runner_start_is_always_closed(failure: str) -> None:
    websocket = _HandshakeWebSocket()
    runners: list[_StartupRunner] = []

    def factory(
        settings: Settings,
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        stop: Callable[[int], None],
    ) -> _StartupRunner:
        runner = _StartupRunner(
            emit,
            start_error=RuntimeError("partial start failed") if failure == "throw" else None,
            block_start=failure == "timeout",
        )
        runners.append(runner)
        return runner

    connection = XiaozhiConnection(
        websocket,  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(lab_token="test-token", xiaozhi_handshake_timeout_seconds=0.01),
        runner_factory=factory,
    )
    await connection.run()
    assert runners[0].closed is True
    assert websocket.closed == [(1008, "handshake_timeout") if failure == "timeout" else (1011, "runtime_failure")]


class _StopStallWebSocket(_HandshakeWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.audio_stalled = asyncio.Event()

    async def send_json(self, payload: dict[str, object]) -> None:
        if payload.get("type") == "hello":
            self.hello_sent.set()
            return
        self.audio_stalled.set()
        await asyncio.Event().wait()


class _DuplicateHelloWebSocket(_HandshakeWebSocket):
    async def receive(self) -> dict[str, object]:
        if self._first:
            self._first = False
            return {"type": "websocket.receive", "text": json.dumps(fixture("client_hello_v1.json"))}
        return {"type": "websocket.receive", "text": json.dumps(fixture("client_hello_v1.json"))}


@pytest.mark.asyncio
async def test_repeated_hello_closes_the_owned_runner() -> None:
    websocket = _DuplicateHelloWebSocket()
    runners: list[_FakeRunner] = []

    def factory(
        settings: Settings,
        emit: Callable[[AgentOutputSegment], Awaitable[None]],
        stop: Callable[[int], None],
    ) -> _FakeRunner:
        runner = _FakeRunner(emit)
        runners.append(runner)
        return runner

    connection = XiaozhiConnection(
        websocket,  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        Settings(lab_token="test-token"),
        runner_factory=factory,
    )
    await connection.run()
    assert runners[0].closed is True
    assert websocket.closed == [(1002, "protocol_error")]


class _SaturatedStopConnection(XiaozhiConnection):
    async def _start_runner_locked(self) -> None:
        await super()._start_runner_locked()
        for _ in range(4):
            await self._enqueue("text", {"type": "queued"}, 1)
        asyncio.create_task(self._saturate_and_stop())

    async def _saturate_and_stop(self) -> None:
        websocket = self._websocket  # noqa: SLF001
        assert isinstance(websocket, _StopStallWebSocket)
        await websocket.audio_stalled.wait()
        await self._enqueue("text", {"type": "queued"}, 1)
        for _ in range(100):
            self._request_stop(2)


@pytest.mark.asyncio
async def test_stop_queue_timeout_closes_connection_with_1013_and_releases_admission() -> None:
    websocket = _StopStallWebSocket()
    settings = Settings(
        lab_token="test-token",
        xiaozhi_media_queue_frames=4,
        xiaozhi_queue_timeout_seconds=0.05,
    )
    admission = SharedSessionAdmission(1)

    token = await admission.reserve(("lab", "device-1"))
    assert token is not None
    connection = _SaturatedStopConnection(
        websocket,  # type: ignore[arg-type]
        AuthContext(tenant_id="lab", device_id="device-1"),
        settings,
        runner_factory=fake_runner_factory,  # type: ignore[arg-type]
    )
    try:
        await connection.run()
    finally:
        await admission.release(token)
    assert websocket.closed == [(1013, "media_overloaded")]
    assert admission.active_count == 0
