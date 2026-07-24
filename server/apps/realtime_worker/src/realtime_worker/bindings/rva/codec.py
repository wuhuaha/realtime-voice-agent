"""RVA Opus profile codec backed by PyAV/libopus."""

from __future__ import annotations

from fractions import Fraction

from av import AudioFrame, AudioResampler, CodecContext, Packet
from av.error import InvalidDataError

from realtime_worker.audio import PCM_SAMPLES, PcmFrame

SAMPLE_RATE = 16_000
CHANNELS = 1
FRAME_DURATION_MS = 60
SAMPLES_PER_PACKET = SAMPLE_RATE * FRAME_DURATION_MS // 1_000
_TIME_BASE = Fraction(1, SAMPLE_RATE)


class RvaOpusDecodeError(ValueError):
    """The packet is authenticated media but is not a valid RVA Opus frame."""


class RvaOpusCodec:
    """Stateful 16 kHz mono codec with an exact 60 ms packet boundary."""

    def __init__(self) -> None:
        decoder = CodecContext.create("opus", "r")
        decoder.sample_rate = SAMPLE_RATE
        decoder.open()
        self._decoder = decoder
        self._decoder_resampler = AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)

        encoder = CodecContext.create("libopus", "w")
        encoder.sample_rate = SAMPLE_RATE
        encoder.layout = "mono"
        encoder.format = "s16"
        encoder.bit_rate = 24_000
        encoder.time_base = _TIME_BASE
        encoder.options = {
            "application": "audio",
            "dtx": "1",
            "frame_duration": str(FRAME_DURATION_MS),
        }
        encoder.open()
        self._encoder = encoder
        self._encoder_pts = 0

    def decode_60ms(self, payload: bytes, *, sequence_start: int) -> list[PcmFrame]:
        try:
            decoded = self._decoder.decode(Packet(payload))
        except InvalidDataError as exc:
            raise RvaOpusDecodeError("invalid Opus packet") from exc
        duration = sum(frame.samples / frame.sample_rate for frame in decoded)
        if abs(duration - FRAME_DURATION_MS / 1_000) > 0.001:
            raise RvaOpusDecodeError("Opus packet does not contain exactly 60 ms of audio")
        pcm = bytearray()
        for frame in decoded:
            converted = self._decoder_resampler.resample(frame)
            converted_frames = converted if isinstance(converted, list) else [converted]
            for output in converted_frames:
                if output is not None:
                    pcm.extend(bytes(output.planes[0])[: output.samples * 2])
        expected_bytes = SAMPLES_PER_PACKET * 2
        if expected_bytes - 64 <= len(pcm) < expected_bytes:
            pcm.extend(b"\x00" * (expected_bytes - len(pcm)))
        if len(pcm) != expected_bytes:
            raise RvaOpusDecodeError("Opus packet decoded to an unexpected PCM length")
        frames: list[PcmFrame] = []
        for index in range(SAMPLES_PER_PACKET // PCM_SAMPLES):
            sequence = sequence_start + index
            offset = index * PCM_SAMPLES * 2
            frames.append(
                PcmFrame(
                    generation=1,
                    sequence=sequence,
                    timestamp_samples=sequence * PCM_SAMPLES,
                    pcm=bytes(pcm[offset : offset + PCM_SAMPLES * 2]),
                )
            )
        return frames

    def encode_60ms(self, frames: list[PcmFrame]) -> bytes:
        if not 1 <= len(frames) <= 3:
            raise ValueError("an RVA packet requires one to three 20 ms PCM frames")
        pcm = b"".join(frame.pcm for frame in frames)
        pcm += b"\x00" * (SAMPLES_PER_PACKET * 2 - len(pcm))
        audio = AudioFrame(format="s16", layout="mono", samples=SAMPLES_PER_PACKET)
        audio.sample_rate = SAMPLE_RATE
        audio.time_base = _TIME_BASE
        audio.pts = self._encoder_pts
        audio.planes[0].update(pcm)
        self._encoder_pts += SAMPLES_PER_PACKET
        packets = self._encoder.encode(audio)
        if len(packets) != 1:
            raise ValueError(f"libopus emitted {len(packets)} packets for one 60 ms frame")
        return bytes(packets[0])


__all__ = [
    "CHANNELS",
    "FRAME_DURATION_MS",
    "RvaOpusCodec",
    "RvaOpusDecodeError",
    "SAMPLE_RATE",
    "SAMPLES_PER_PACKET",
]
