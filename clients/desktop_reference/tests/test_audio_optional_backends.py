from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from rva_desktop.audio import (
    WIRE_BYTES_PER_FRAME,
    PcmFrame,
    PyAvOpusCodec,
    SoundDeviceAudioSink,
    SoundDeviceAudioSource,
    SoundDeviceInputConfig,
    SoundDeviceOutputConfig,
    SoundDeviceUnavailableError,
    pyav_available,
)


def test_optional_modules_do_not_load_native_audio_libraries_on_import() -> None:
    assert isinstance(pyav_available(), bool)


def test_sounddevice_failure_is_deferred_until_interactive_backend_starts() -> None:
    async def scenario() -> None:
        source = SoundDeviceAudioSource(SoundDeviceInputConfig())
        with patch(
            "rva_desktop.audio.sounddevice_backend.importlib.import_module",
            side_effect=ImportError("not installed"),
        ):
            with pytest.raises(SoundDeviceUnavailableError, match="interactive"):
                await source.start()

    asyncio.run(scenario())


def test_fake_sounddevice_exercises_capture_playback_and_cleanup() -> None:
    input_streams: list[object] = []
    output_streams = []

    class FakeInputStream:
        def __init__(self, **kwargs: object) -> None:
            self.callback = kwargs["callback"]
            self.closed = False
            input_streams.append(self)

        def start(self) -> None:
            self.callback(b"\x01\x00" * 960, 960, None, False)

        def stop(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    class FakeOutputStream:
        def __init__(self, **_kwargs: object) -> None:
            self.writes: list[bytes] = []
            self.closed = False
            self.latency = 0.001
            self._started_at = 0.0
            output_streams.append(self)

        def start(self) -> None:
            self._started_at = time.monotonic()

        @property
        def time(self) -> float:
            return time.monotonic() - self._started_at

        def write(self, data: bytes) -> None:
            self.writes.append(bytes(data))

        def stop(self) -> None:
            pass

        def close(self) -> None:
            self.closed = True

    fake_module = SimpleNamespace(RawInputStream=FakeInputStream, RawOutputStream=FakeOutputStream)

    async def scenario() -> None:
        with patch("rva_desktop.audio.sounddevice_backend.importlib.import_module", return_value=fake_module):
            source = SoundDeviceAudioSource(
                SoundDeviceInputConfig(sample_rate_hz=16_000, channels=1, block_duration_ms=60)
            )
            await source.start()
            captured = await asyncio.wait_for(source.read_frame(), timeout=1)
            assert captured is not None and captured.data == b"\x01\x00" * 960
            await source.close()

            sink = SoundDeviceAudioSink(SoundDeviceOutputConfig(sample_rate_hz=48_000, channels=1))
            await sink.start()
            await sink.write_frame(
                PcmFrame(data=b"\x02\x00" * 960, sequence=0, timestamp_samples=0, captured_at=0.0)
            )
            await sink.drain()
            rendered = await sink.wait_rendered(0)
            assert rendered.sequence == 0
            assert rendered.timestamp_samples == 0
            await sink.close()

        assert input_streams[0].closed
        assert output_streams[0].closed
        assert output_streams[0].writes == [b"\x02\x00" * (WIRE_BYTES_PER_FRAME // 2 * 3)]

    asyncio.run(scenario())


def test_sounddevice_render_ack_and_drain_wait_for_reported_output_latency() -> None:
    write_completed = threading.Event()
    output_streams: list[object] = []

    class FakeOutputStream:
        latency = 1.0

        def __init__(self, **_kwargs: object) -> None:
            self.current_time = 0.0
            self.time_reads = 0
            output_streams.append(self)

        def start(self) -> None:
            pass

        @property
        def time(self) -> float:
            self.time_reads += 1
            if self.time_reads <= 2:
                return 0.0
            return self.current_time

        def write(self, _data: bytes) -> None:
            write_completed.set()

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

    fake_module = SimpleNamespace(RawOutputStream=FakeOutputStream)

    async def scenario() -> None:
        with patch("rva_desktop.audio.sounddevice_backend.importlib.import_module", return_value=fake_module):
            sink = SoundDeviceAudioSink(SoundDeviceOutputConfig(sample_rate_hz=16_000, channels=1))
            await sink.start()
            await sink.write_frame(
                PcmFrame(data=b"\x02\x00" * 960, sequence=0, timestamp_samples=0, captured_at=0.0)
            )
            rendered = asyncio.create_task(sink.wait_rendered(0))
            drained = asyncio.create_task(sink.drain())
            assert await asyncio.to_thread(write_completed.wait, 1)

            await asyncio.sleep(0.01)
            assert not rendered.done()
            assert not drained.done()

            output_streams[0].current_time = 1.0
            ack = await asyncio.wait_for(rendered, timeout=0.2)
            await asyncio.wait_for(drained, timeout=0.2)
            assert ack.sequence == 0
            await sink.close()

    asyncio.run(scenario())


def test_sounddevice_abort_cancels_pending_render_boundary_wait() -> None:
    write_completed = threading.Event()

    class FakeOutputStream:
        latency = 60.0

        def __init__(self, **_kwargs: object) -> None:
            self._started_at = 0.0

        def start(self) -> None:
            self._started_at = time.monotonic()

        @property
        def time(self) -> float:
            return time.monotonic() - self._started_at

        def write(self, _data: bytes) -> None:
            write_completed.set()

        def abort(self) -> None:
            pass

        def close(self) -> None:
            pass

    fake_module = SimpleNamespace(RawOutputStream=FakeOutputStream)

    async def scenario() -> None:
        with patch("rva_desktop.audio.sounddevice_backend.importlib.import_module", return_value=fake_module):
            sink = SoundDeviceAudioSink(SoundDeviceOutputConfig(sample_rate_hz=16_000, channels=1))
            await sink.start()
            await sink.write_frame(
                PcmFrame(data=b"\x02\x00" * 960, sequence=0, timestamp_samples=0, captured_at=0.0)
            )
            rendered = asyncio.create_task(sink.wait_rendered(0))
            assert await asyncio.to_thread(write_completed.wait, 1)

            await asyncio.wait_for(sink.abort(), timeout=0.2)
            with pytest.raises(RuntimeError, match="closed before"):
                await asyncio.wait_for(rendered, timeout=0.2)

    asyncio.run(scenario())


def test_sounddevice_render_clock_failure_propagates_and_close_does_not_hang() -> None:
    class FakeOutputStream:
        latency = 1.0

        def __init__(self, **_kwargs: object) -> None:
            self.time_reads = 0

        def start(self) -> None:
            pass

        @property
        def time(self) -> float:
            self.time_reads += 1
            if self.time_reads > 2:
                raise RuntimeError("stream clock failed")
            return 0.0

        def write(self, _data: bytes) -> None:
            pass

        def stop(self) -> None:
            pass

        def close(self) -> None:
            pass

    fake_module = SimpleNamespace(RawOutputStream=FakeOutputStream)

    async def scenario() -> None:
        with patch("rva_desktop.audio.sounddevice_backend.importlib.import_module", return_value=fake_module):
            sink = SoundDeviceAudioSink(SoundDeviceOutputConfig(sample_rate_hz=16_000, channels=1))
            await sink.start()
            await sink.write_frame(
                PcmFrame(data=b"\x02\x00" * 960, sequence=0, timestamp_samples=0, captured_at=0.0)
            )

            with pytest.raises(RuntimeError, match="output worker failed"):
                await asyncio.wait_for(sink.drain(), timeout=0.2)
            await asyncio.wait_for(sink.close(), timeout=0.2)

    asyncio.run(scenario())


def test_real_pyav_libopus_round_trip_and_packet_loss_concealment_are_60ms() -> None:
    pytest.importorskip("av", reason="the desktop opus extra is not installed")
    codec = PyAvOpusCodec()
    try:
        pcm = b"\x01\x00" * (WIRE_BYTES_PER_FRAME // 2)
        payload = codec.encode_60ms(pcm)
        decoded = codec.decode_60ms(payload)
        concealed = codec.conceal_60ms()
    finally:
        codec.close()

    assert payload
    assert len(decoded) == WIRE_BYTES_PER_FRAME
    assert len(concealed) == WIRE_BYTES_PER_FRAME
