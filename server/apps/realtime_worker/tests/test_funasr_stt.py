from __future__ import annotations

import asyncio
import json
from array import array
from collections.abc import Callable

import pytest
from livekit import rtc
from livekit.agents import APIConnectionError, APIConnectOptions
from realtime_worker.config import Settings
from realtime_worker.errors import BackpressureError, ProviderError
from realtime_worker.observability.events import InMemoryTraceSink, TraceContext, Tracer
from realtime_worker.providers import funasr_stt
from realtime_worker.providers.funasr_stt import (
    FunASRProtocol,
    FunASRStream,
    FunASRStreamConfig,
    FunASRSTT,
    RecognitionEvent,
    RecognitionKind,
    StandaloneFunASRStream,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.incoming: asyncio.Queue[str | bytes | BaseException] = asyncio.Queue()
        self.closed = False

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)

    async def recv(self) -> str | bytes:
        message = await self.incoming.get()
        if isinstance(message, BaseException):
            raise message
        return message

    async def close(self) -> None:
        self.closed = True


class BlockingFakeWebSocket(FakeWebSocket):
    def __init__(self) -> None:
        super().__init__()
        self.release_audio = asyncio.Event()

    async def send(self, message: str | bytes) -> None:
        self.sent.append(message)
        if isinstance(message, bytes):
            await self.release_audio.wait()


class InitialSendFailingWebSocket(FakeWebSocket):
    async def send(self, message: str | bytes) -> None:
        raise OSError("initial send failed")


async def fake_factory(socket: FakeWebSocket, _url: str, _timeout: float) -> FakeWebSocket:
    return socket


async def wait_until(predicate: Callable[[], bool], *, attempts: int = 20) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


@pytest.mark.asyncio
async def test_funasr_maps_online_replacement_and_offline_final() -> None:
    socket = FakeWebSocket()
    config = FunASRStreamConfig(
        url="ws://funasr.test",
        queue_max_chunks=2,
        hotwords=("LiveKit", "语音助手"),
    )
    stream = FunASRStream(config, websocket_factory=lambda url, timeout: fake_factory(socket, url, timeout))
    await stream.start()
    initial = json.loads(str(socket.sent[0]))
    assert initial["mode"] == "2pass"
    assert initial["audio_fs"] == 16000
    assert initial["chunk_size"] == [8, 8, 4]
    assert initial["chunk_interval"] == 8
    assert initial["encoder_chunk_look_back"] == 4
    assert initial["decoder_chunk_look_back"] == 0
    assert initial["itn"] is True
    assert initial["hotwords"] == "LiveKit 语音助手"

    stream.push_audio(b"\x01\x00")
    await socket.incoming.put(json.dumps({"mode": "2pass-online", "text": "我想问"}))
    events = stream.events()
    interim = await anext(events)
    assert interim.kind is RecognitionKind.INTERIM
    assert interim.text == "我想问"

    await stream.flush()
    await asyncio.sleep(0)
    await socket.incoming.put(json.dumps({"mode": "2pass-offline", "text": "我想问一下"}))
    final = await anext(events)
    assert final.kind is RecognitionKind.FINAL
    assert final.segment_id == interim.segment_id
    assert final.text == "我想问一下"
    assert {"is_speaking": False} == json.loads(str(socket.sent[-2]))
    assert socket.sent[-1] == bytes(1920)

    await stream.aclose()
    assert socket.closed


@pytest.mark.asyncio
async def test_funasr_empty_final_releases_the_next_turn() -> None:
    socket = FakeWebSocket()
    stream = FunASRStream(
        FunASRStreamConfig(url="ws://funasr.test", queue_max_chunks=2),
        websocket_factory=lambda url, timeout: fake_factory(socket, url, timeout),
    )
    await stream.start()

    stream.push_audio(b"turn-one")
    await stream.flush()
    await socket.incoming.put(json.dumps({"mode": "2pass-offline", "text": ""}))
    await asyncio.sleep(0)

    stream.push_audio(b"turn-two")
    await stream.flush()
    await stream.aclose()


@pytest.mark.asyncio
async def test_funasr_fails_on_bounded_audio_queue() -> None:
    socket = FakeWebSocket()
    stream = FunASRStream(
        FunASRStreamConfig(url="ws://funasr.test", queue_max_chunks=1),
        websocket_factory=lambda url, timeout: fake_factory(socket, url, timeout),
    )
    await stream.start()

    stream.push_audio(b"a" * 1920)
    with pytest.raises(BackpressureError):
        stream.push_audio(b"b" * 1920)

    await stream.aclose()


@pytest.mark.asyncio
async def test_funasr_batches_small_frames_and_flushes_tail_before_marker() -> None:
    socket = FakeWebSocket()
    stream = FunASRStream(
        FunASRStreamConfig(url="ws://funasr.test"),
        websocket_factory=lambda url, timeout: fake_factory(socket, url, timeout),
    )
    await stream.start()

    stream.push_audio(b"a" * 1600)
    stream.push_audio(b"b" * 1600)
    await stream.flush()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert socket.sent[1:] == [
        b"a" * 1600 + b"b" * 320,
        b"b" * 1280,
        '{"is_speaking": false}',
        bytes(1920),
    ]
    await stream.aclose()


@pytest.mark.asyncio
async def test_funasr_close_waits_for_queue_and_always_closes_connection() -> None:
    socket = BlockingFakeWebSocket()
    stream = FunASRStream(
        FunASRStreamConfig(url="ws://funasr.test", queue_max_chunks=1, timeout_seconds=1.0),
        websocket_factory=lambda url, timeout: fake_factory(socket, url, timeout),
    )
    await stream.start()

    stream.push_audio(b"a" * 1920)
    await asyncio.sleep(0)
    stream.push_audio(b"b" * 1920)
    stream.push_audio(b"tail")
    close_task = asyncio.create_task(stream.aclose())
    await asyncio.sleep(0)
    assert not close_task.done()

    socket.release_audio.set()
    await close_task

    assert socket.closed
    assert socket.sent[1:4] == [b"a" * 1920, b"b" * 1920, b"tail"]


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("audio_fs", 0, "audio_fs"),
        ("send_chunk_ms", 0, "send_chunk_ms"),
        ("queue_max_chunks", 0, "queue_max_chunks"),
        ("chunk_size", (8, 0, 4), "chunk_size"),
        ("encoder_chunk_look_back", -1, "look-back"),
    ],
)
def test_funasr_rejects_invalid_buffer_configuration(field: str, value: object, error: str) -> None:
    with pytest.raises(ValueError, match=error):
        FunASRStreamConfig(url="ws://funasr.test", **{field: value})


@pytest.mark.asyncio
async def test_funasr_disconnect_is_reported_to_event_consumer() -> None:
    socket = FakeWebSocket()
    stream = FunASRStream(
        FunASRStreamConfig(url="ws://funasr.test"),
        websocket_factory=lambda url, timeout: fake_factory(socket, url, timeout),
    )
    await stream.start()
    await socket.incoming.put(OSError("disconnect"))

    with pytest.raises(Exception, match="funasr"):
        await anext(stream.events())

    await stream.aclose()


@pytest.mark.asyncio
async def test_funasr_closes_connection_when_initial_message_fails() -> None:
    socket = InitialSendFailingWebSocket()
    stream = FunASRStream(
        FunASRStreamConfig(url="ws://funasr.test"),
        websocket_factory=lambda url, timeout: fake_factory(socket, url, timeout),
    )

    with pytest.raises(OSError, match="initial send failed"):
        await stream.start()

    assert socket.closed is True
    await stream.aclose()


@pytest.mark.asyncio
async def test_standalone_funasr_pads_tail_and_maps_cumulative_interim_and_full_final() -> None:
    socket = FakeWebSocket()
    config = FunASRStreamConfig(
        url="ws://standalone-funasr.test/v1/asr/stream",
        protocol=FunASRProtocol.STANDALONE,
        hotwords=("LiveKit", "语音助手"),
    )
    stream = StandaloneFunASRStream(
        config,
        websocket_factory=lambda url, timeout: fake_factory(socket, url, timeout),
    )
    await stream.start()

    stream.push_audio(b"a" * 1600)
    await stream.flush()
    await socket.incoming.put(json.dumps({"type": "ready"}))
    await socket.incoming.put(json.dumps({"type": "started"}))
    await socket.incoming.put(json.dumps({"type": "interim", "text": "我想问", "is_final": False}))
    await socket.incoming.put(
        json.dumps(
            {
                "type": "final",
                "text": "我想问一下。",
                "is_final": True,
                "model": "iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch",
            }
        )
    )

    events = stream.events()
    interim = await anext(events)
    final = await anext(events)
    await wait_until(lambda: socket.closed)

    start = json.loads(str(socket.sent[0]))
    assert start == {
        "type": "start",
        "sample_rate_hz": 16000,
        "channels": 1,
        "codec": "pcm16le",
        "language": "zh",
        "hotwords": ["LiveKit", "语音助手"],
    }
    assert socket.sent[1] == b"a" * 1600 + bytes(320)
    assert json.loads(str(socket.sent[2])) == {"type": "finish"}
    assert interim.kind is RecognitionKind.INTERIM
    assert interim.text == "我想问"
    assert final.kind is RecognitionKind.FINAL
    assert final.text == "我想问一下。"
    assert final.segment_id == interim.segment_id

    await stream.aclose()


@pytest.mark.asyncio
async def test_standalone_funasr_deduplicates_adjacent_normalized_interim() -> None:
    socket = FakeWebSocket()
    sink = InMemoryTraceSink()
    tracer = Tracer(TraceContext(trace_id="trace-dedup"), sink)
    stream = StandaloneFunASRStream(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        ),
        websocket_factory=lambda url, timeout: fake_factory(socket, url, timeout),
        tracer=tracer,
    )
    await stream.start()
    stream.push_audio(b"a" * 1920)
    await stream.flush()

    await socket.incoming.put(json.dumps({"type": "interim", "text": "重复内容"}))
    await socket.incoming.put(json.dumps({"type": "interim", "text": "  重复内容  "}))
    await socket.incoming.put(json.dumps({"type": "interim", "text": "重复内容更新"}))
    await socket.incoming.put(json.dumps({"type": "final", "text": "重复内容更新。"}))

    events = stream.events()
    received = [await anext(events) for _ in range(3)]

    assert [(event.kind, event.text) for event in received] == [
        (RecognitionKind.INTERIM, "重复内容"),
        (RecognitionKind.INTERIM, "重复内容更新"),
        (RecognitionKind.FINAL, "重复内容更新。"),
    ]
    dedup_event = next(event for event in sink.events if event.name == "asr_interim_deduplicated")
    assert dedup_event.fields["text_length"] == 4
    assert isinstance(dedup_event.fields["text_hash"], str)
    assert len(dedup_event.fields["text_hash"]) == 12
    assert "text" not in dedup_event.fields

    await stream.aclose()


@pytest.mark.asyncio
async def test_standalone_funasr_reconnects_for_each_utterance() -> None:
    sockets = [FakeWebSocket(), FakeWebSocket()]
    opened: list[FakeWebSocket] = []

    async def factory(_url: str, _timeout: float) -> FakeWebSocket:
        socket = sockets[len(opened)]
        opened.append(socket)
        return socket

    stream = StandaloneFunASRStream(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        ),
        websocket_factory=factory,
    )
    await stream.start()
    events = stream.events()

    for index, socket in enumerate(sockets, start=1):
        stream.push_audio(bytes([index]) * 1920)
        await stream.flush()
        await socket.incoming.put(json.dumps({"type": "final", "text": f"第{index}轮"}))
        final = await anext(events)
        assert final.text == f"第{index}轮"
        await wait_until(lambda socket=socket: socket.closed)

    assert len(opened) == 2
    assert [json.loads(str(socket.sent[0]))["type"] for socket in sockets] == ["start", "start"]
    assert [json.loads(str(socket.sent[-1]))["type"] for socket in sockets] == ["finish", "finish"]

    await stream.aclose()


@pytest.mark.asyncio
async def test_standalone_funasr_reports_provider_error_and_closes_connection() -> None:
    socket = FakeWebSocket()
    stream = StandaloneFunASRStream(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        ),
        websocket_factory=lambda url, timeout: fake_factory(socket, url, timeout),
    )
    await stream.start()
    stream.push_audio(b"a" * 1920)
    await stream.flush()
    await socket.incoming.put(json.dumps({"type": "error", "error": "private provider detail"}))

    with pytest.raises(Exception, match="funasr") as error:
        await anext(stream.events())

    assert "private provider detail" not in str(error.value)
    await wait_until(lambda: socket.closed)
    await stream.aclose()


@pytest.mark.asyncio
async def test_standalone_funasr_close_is_bounded_when_final_never_arrives() -> None:
    socket = FakeWebSocket()
    stream = StandaloneFunASRStream(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
            timeout_seconds=0.01,
        ),
        websocket_factory=lambda url, timeout: fake_factory(socket, url, timeout),
    )
    await stream.start()
    stream.push_audio(b"a" * 1920)

    await stream.aclose()

    assert socket.closed is True


@pytest.mark.asyncio
async def test_standalone_funasr_sync_push_remains_fail_fast() -> None:
    socket = FakeWebSocket()
    stream = StandaloneFunASRStream(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
            queue_max_chunks=1,
            timeout_seconds=0.01,
        ),
        websocket_factory=lambda url, timeout: fake_factory(socket, url, timeout),
    )
    await stream.start()

    stream.push_audio(b"a" * 1920)
    with pytest.raises(BackpressureError):
        stream.push_audio(b"b" * 1920)

    await stream.aclose()


def test_funasr_capabilities_follow_protocol() -> None:
    local = FunASRSTT(FunASRStreamConfig(url="ws://local-funasr.test"))
    standalone = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        )
    )

    assert local.capabilities.streaming is True
    assert local.capabilities.interim_results is True
    assert local.capabilities.offline_recognize is False
    assert standalone.capabilities.streaming is False
    assert standalone.capabilities.interim_results is False
    assert standalone.capabilities.offline_recognize is True


@pytest.mark.asyncio
async def test_standalone_funasr_recognize_sends_utterance_and_returns_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = FakeWebSocket()
    monkeypatch.setattr(
        funasr_stt,
        "_open_websocket",
        lambda url, timeout: fake_factory(socket, url, timeout),
    )
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        )
    )
    frame = rtc.AudioFrame(data=b"a" * 1600, sample_rate=16000, num_channels=1, samples_per_channel=800)
    await socket.incoming.put(json.dumps({"type": "interim", "text": "中间结果"}))
    await socket.incoming.put(json.dumps({"type": "final", "text": "最终结果。"}))

    event = await adapter.recognize(frame, conn_options=APIConnectOptions(max_retry=0))

    assert event.type is funasr_stt.stt.SpeechEventType.FINAL_TRANSCRIPT
    assert event.alternatives[0].text == "最终结果。"
    assert event.alternatives[0].language == "zh-CN"
    assert json.loads(str(socket.sent[0]))["type"] == "start"
    assert socket.sent[1] == b"a" * 1600 + bytes(320)
    assert json.loads(str(socket.sent[2])) == {"type": "finish"}
    assert socket.closed is True


@pytest.mark.asyncio
async def test_standalone_funasr_recognize_resamples_48khz_multiframe_audio(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = FakeWebSocket()
    monkeypatch.setattr(
        funasr_stt,
        "_open_websocket",
        lambda url, timeout: fake_factory(socket, url, timeout),
    )
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        )
    )
    samples = array("h", [1000] * 8640)
    frames = [
        rtc.AudioFrame(
            data=samples[index : index + 4320].tobytes(),
            sample_rate=48000,
            num_channels=1,
            samples_per_channel=4320,
        )
        for index in (0, 4320)
    ]
    await socket.incoming.put(json.dumps({"type": "final", "text": "重采样成功。"}))

    event = await adapter.recognize(frames, conn_options=APIConnectOptions(max_retry=0))

    assert event.alternatives[0].text == "重采样成功。"
    audio_packets = [message for message in socket.sent if isinstance(message, bytes)]
    assert [len(packet) for packet in audio_packets] == [1920, 1920, 1920]
    resampled = array("h")
    resampled.frombytes(b"".join(audio_packets))
    assert len(resampled) == 2880
    assert sum(resampled[100:-100]) / len(resampled[100:-100]) == pytest.approx(1000, abs=2)


@pytest.mark.asyncio
async def test_standalone_funasr_recognize_waits_for_queue_capacity_for_long_utterance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = BlockingFakeWebSocket()
    monkeypatch.setattr(
        funasr_stt,
        "_open_websocket",
        lambda url, timeout: fake_factory(socket, url, timeout),
    )
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
            queue_max_chunks=2,
            timeout_seconds=1.0,
        )
    )
    pcm_s16le = b"a" * (20 * 1920 + 640)
    frame = rtc.AudioFrame(
        data=pcm_s16le,
        sample_rate=16000,
        num_channels=1,
        samples_per_channel=len(pcm_s16le) // 2,
    )
    recognize_task = asyncio.create_task(adapter.recognize(frame, conn_options=APIConnectOptions(max_retry=0)))
    try:
        await wait_until(lambda: any(isinstance(message, bytes) for message in socket.sent), attempts=100)
        assert recognize_task.done() is False

        socket.release_audio.set()
        await wait_until(
            lambda: any(message == json.dumps({"type": "finish"}) for message in socket.sent),
            attempts=100,
        )
        await socket.incoming.put(json.dumps({"type": "final", "text": "长语音完成。"}))
        event = await recognize_task

        assert event.alternatives[0].text == "长语音完成。"
        audio_packets = [message for message in socket.sent if isinstance(message, bytes)]
        assert [len(packet) for packet in audio_packets] == [1920] * 20 + [640]
        assert b"".join(audio_packets) == pcm_s16le
    finally:
        socket.release_audio.set()
        if not recognize_task.done():
            recognize_task.cancel()
        await asyncio.gather(recognize_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_standalone_funasr_recognize_maps_sender_stall_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = BlockingFakeWebSocket()
    monkeypatch.setattr(
        funasr_stt,
        "_open_websocket",
        lambda url, timeout: fake_factory(socket, url, timeout),
    )
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
            queue_max_chunks=1,
            timeout_seconds=0.01,
        )
    )
    pcm_s16le = b"a" * (4 * 1920)
    frame = rtc.AudioFrame(
        data=pcm_s16le,
        sample_rate=16000,
        num_channels=1,
        samples_per_channel=len(pcm_s16le) // 2,
    )

    with pytest.raises(APIConnectionError) as error:
        await adapter.recognize(frame, conn_options=APIConnectOptions(max_retry=0))

    assert error.value.retryable is True
    assert socket.closed is True


@pytest.mark.asyncio
async def test_standalone_funasr_recognize_rejects_non_mono_audio() -> None:
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        )
    )
    frame = rtc.AudioFrame(
        data=b"a" * 3840,
        sample_rate=48000,
        num_channels=2,
        samples_per_channel=960,
    )

    with pytest.raises(ValueError, match="mono"):
        await adapter.recognize(frame, conn_options=APIConnectOptions(max_retry=0))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "reason"),
    [
        ({"type": "error", "code": "no_speech", "error": "private provider detail"}, "no_speech"),
        ({"type": "final", "text": ""}, "empty_final"),
        ({"type": "final", "text": "   "}, "empty_final"),
        (
            {
                "type": "error",
                "code": "invalid_audio",
                "error": "FunASR returned empty text",
            },
            "legacy_empty_result",
        ),
    ],
)
async def test_standalone_funasr_recognize_accepts_provider_empty_result_without_retry(
    monkeypatch: pytest.MonkeyPatch,
    response: dict[str, str],
    reason: str,
) -> None:
    socket = FakeWebSocket()
    sink = InMemoryTraceSink()
    monkeypatch.setattr(
        funasr_stt,
        "_open_websocket",
        lambda url, timeout: fake_factory(socket, url, timeout),
    )
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
            timeout_seconds=1.0,
        ),
        tracer=Tracer(TraceContext(trace_id="trace-no-result"), sink),
    )
    frame = rtc.AudioFrame(data=b"a" * 1920, sample_rate=16000, num_channels=1, samples_per_channel=960)
    await socket.incoming.put(json.dumps(response))

    event = await adapter.recognize(frame, conn_options=APIConnectOptions(max_retry=0))

    assert event.alternatives[0].text == ""
    no_result = [event for event in sink.events if event.name == "asr_no_result"]
    assert len(no_result) == 1
    assert no_result[0].fields["reason"] == reason
    assert "error" not in no_result[0].fields
    assert socket.closed is True


@pytest.mark.asyncio
async def test_standalone_funasr_legacy_empty_result_requires_exact_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = FakeWebSocket()
    opened = 0

    async def factory(_url: str, _timeout: float) -> FakeWebSocket:
        nonlocal opened
        opened += 1
        return socket

    monkeypatch.setattr(funasr_stt, "_open_websocket", factory)
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        )
    )
    frame = rtc.AudioFrame(data=b"a" * 1920, sample_rate=16000, num_channels=1, samples_per_channel=960)
    await socket.incoming.put(
        json.dumps(
            {
                "type": "error",
                "code": "invalid_audio",
                "error": "FunASR returned empty text.",
            }
        )
    )

    with pytest.raises(ProviderError) as error:
        await adapter.recognize(frame, conn_options=APIConnectOptions(max_retry=3, retry_interval=0.0))

    assert error.value.retryable is False
    assert "FunASR returned empty text" not in str(error.value)
    assert opened == 1
    assert socket.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("code", "expected_retryable"),
    [
        ("inference_failed", True),
        ("busy", True),
        ("invalid_audio", False),
        ("invalid_start", False),
        ("unknown_provider_code", False),
    ],
)
async def test_standalone_funasr_recognize_classifies_provider_error_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    code: str,
    expected_retryable: bool,
) -> None:
    socket = FakeWebSocket()
    monkeypatch.setattr(
        funasr_stt,
        "_open_websocket",
        lambda url, timeout: fake_factory(socket, url, timeout),
    )
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        )
    )
    frame = rtc.AudioFrame(data=b"a" * 1920, sample_rate=16000, num_channels=1, samples_per_channel=960)
    await socket.incoming.put(
        json.dumps({"type": "error", "code": code, "error": "private provider detail"})
    )

    expected_error = APIConnectionError if expected_retryable else ProviderError
    with pytest.raises(expected_error) as error:
        await adapter.recognize(frame, conn_options=APIConnectOptions(max_retry=0))

    assert error.value.retryable is expected_retryable
    expected_code = "unknown" if code == "unknown_provider_code" else code
    assert expected_code in str(error.value)
    if code == "unknown_provider_code":
        assert code not in str(error.value)
    assert "private provider detail" not in str(error.value)
    assert socket.closed is True


@pytest.mark.asyncio
async def test_standalone_funasr_rejects_non_string_error_code_without_retry_or_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    socket = FakeWebSocket()
    monkeypatch.setattr(
        funasr_stt,
        "_open_websocket",
        lambda url, timeout: fake_factory(socket, url, timeout),
    )
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        )
    )
    frame = rtc.AudioFrame(data=b"a" * 1920, sample_rate=16000, num_channels=1, samples_per_channel=960)
    await socket.incoming.put(
        json.dumps({"type": "error", "code": ["invalid_start"], "error": "private provider detail"})
    )

    with pytest.raises(ProviderError) as error:
        await adapter.recognize(frame, conn_options=APIConnectOptions(max_retry=0))

    assert error.value.retryable is False
    assert "unknown" in str(error.value)
    assert "private provider detail" not in str(error.value)
    assert socket.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "{private malformed provider body",
        json.dumps(["private provider body"]),
        json.dumps({"private": "provider body"}),
        json.dumps({"type": "final", "text": {"private": "provider body"}}),
        b"private binary provider body",
    ],
)
async def test_standalone_funasr_malformed_event_is_fatal_without_framework_retry(
    monkeypatch: pytest.MonkeyPatch,
    message: str | bytes,
) -> None:
    sockets = [FakeWebSocket() for _ in range(3)]
    opened = 0

    async def factory(_url: str, _timeout: float) -> FakeWebSocket:
        nonlocal opened
        socket = sockets[opened]
        opened += 1
        return socket

    monkeypatch.setattr(funasr_stt, "_open_websocket", factory)
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        )
    )
    frame = rtc.AudioFrame(data=b"a" * 1920, sample_rate=16000, num_channels=1, samples_per_channel=960)
    await sockets[0].incoming.put(message)

    with pytest.raises(ProviderError) as error:
        await adapter.recognize(frame, conn_options=APIConnectOptions(max_retry=2, retry_interval=0.0))

    assert error.value.retryable is False
    assert "malformed" in str(error.value)
    assert "private" not in str(error.value)
    assert opened == 1
    assert sockets[0].closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [TimeoutError("private timeout detail"), OSError("private connection detail")])
async def test_standalone_funasr_transport_failure_uses_only_livekit_bounded_retry(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    opened = 0

    async def factory(_url: str, _timeout: float) -> FakeWebSocket:
        nonlocal opened
        opened += 1
        raise failure

    monkeypatch.setattr(funasr_stt, "_open_websocket", factory)
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        )
    )
    frame = rtc.AudioFrame(data=b"a" * 1920, sample_rate=16000, num_channels=1, samples_per_channel=960)

    with pytest.raises(APIConnectionError) as error:
        await adapter.recognize(frame, conn_options=APIConnectOptions(max_retry=1, retry_interval=0.0))

    assert "private" not in str(error.value)
    assert opened == 2


@pytest.mark.asyncio
async def test_standalone_funasr_busy_uses_only_livekit_bounded_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sockets = [FakeWebSocket(), FakeWebSocket()]
    opened = 0

    async def factory(_url: str, _timeout: float) -> FakeWebSocket:
        nonlocal opened
        socket = sockets[opened]
        opened += 1
        await socket.incoming.put(
            json.dumps({"type": "error", "code": "busy", "error": "private provider detail"})
        )
        return socket

    monkeypatch.setattr(funasr_stt, "_open_websocket", factory)
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        )
    )
    frame = rtc.AudioFrame(data=b"a" * 1920, sample_rate=16000, num_channels=1, samples_per_channel=960)

    with pytest.raises(APIConnectionError) as error:
        await adapter.recognize(frame, conn_options=APIConnectOptions(max_retry=1, retry_interval=0.0))

    assert "private provider detail" not in str(error.value)
    assert opened == 2
    assert all(socket.closed for socket in sockets)


@pytest.mark.asyncio
async def test_local_funasr_recognize_remains_unsupported() -> None:
    adapter = FunASRSTT(FunASRStreamConfig(url="ws://local-funasr.test"))
    frame = rtc.AudioFrame(data=b"a" * 1920, sample_rate=16000, num_channels=1, samples_per_channel=960)

    with pytest.raises(NotImplementedError, match="only supports streaming"):
        await adapter.recognize(frame, conn_options=APIConnectOptions(max_retry=0))


@pytest.mark.asyncio
async def test_livekit_funasr_stream_retries_after_provider_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[object] = []
    keep_second_stream_open = asyncio.Event()

    class DisconnectThenRecoverStream:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.index = len(instances)
            instances.append(self)

        async def start(self) -> None:
            pass

        def push_audio(self, _pcm_s16le: bytes) -> None:
            pass

        async def flush(self) -> None:
            pass

        async def aclose(self) -> None:
            pass

        async def events(self):  # type: ignore[no-untyped-def]
            if self.index == 0:
                raise ProviderError("funasr", "ConnectionClosedError", retryable=True)
            yield RecognitionEvent(
                kind=RecognitionKind.INTERIM,
                text="恢复成功",
                segment_id="segment-2",
                request_id="request-2",
                raw_mode="standalone-interim",
            )
            await keep_second_stream_open.wait()

    monkeypatch.setattr(funasr_stt, "StandaloneFunASRStream", DisconnectThenRecoverStream)
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        )
    )
    stream = adapter.stream(conn_options=APIConnectOptions(max_retry=1, retry_interval=0.0))
    try:
        event = await asyncio.wait_for(anext(stream), timeout=1.0)
        assert event.alternatives[0].text == "恢复成功"
        assert len(instances) == 2
    finally:
        keep_second_stream_open.set()
        await stream.aclose()


@pytest.mark.asyncio
async def test_livekit_funasr_stream_does_not_retry_fatal_provider_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    instances: list[object] = []

    class FatalStream:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            instances.append(self)

        async def start(self) -> None:
            pass

        def push_audio(self, _pcm_s16le: bytes) -> None:
            pass

        async def flush(self) -> None:
            pass

        async def aclose(self) -> None:
            pass

        async def events(self):  # type: ignore[no-untyped-def]
            raise ProviderError("funasr", "standalone ASR rejected the stream (invalid_audio)", retryable=False)
            yield

    monkeypatch.setattr(funasr_stt, "StandaloneFunASRStream", FatalStream)
    adapter = FunASRSTT(
        FunASRStreamConfig(
            url="ws://standalone-funasr.test/v1/asr/stream",
            protocol=FunASRProtocol.STANDALONE,
        )
    )
    stream = adapter.stream(conn_options=APIConnectOptions(max_retry=3, retry_interval=0.0))
    try:
        with pytest.raises(ProviderError) as error:
            await anext(stream)

        assert error.value.retryable is False
        assert len(instances) == 1
    finally:
        await stream.aclose()


@pytest.mark.parametrize("configured", ["local", "funasr"])
def test_local_funasr_protocol_aliases_select_local_transport(configured: str) -> None:
    settings = Settings(
        _env_file=None,
        runner="deterministic",
        internal_token="validator-internal-token",
        grant_signing_key="validator-grant-signing-key-with-32-bytes",
        lab_token="lab-test-token",
        funasr_protocol=configured,
    )
    assert FunASRStreamConfig.from_settings(settings).protocol is FunASRProtocol.LOCAL
