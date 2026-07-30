"""Streaming PCM16LE downmix, resampling and wire framing."""

from __future__ import annotations

import sys
from array import array

from .ports import WIRE_BYTES_PER_FRAME, WIRE_SAMPLE_RATE_HZ


def _pcm16le_to_samples(data: bytes) -> array[int]:
    if len(data) % 2:
        raise ValueError("PCM16LE data length must be even")
    samples = array("h")
    samples.frombytes(data)
    if sys.byteorder != "little":
        samples.byteswap()
    return samples


def _samples_to_pcm16le(samples: list[int]) -> bytes:
    result = array("h", samples)
    if sys.byteorder != "little":
        result.byteswap()
    return result.tobytes()


class Pcm16MonoResampler:
    """Stateful linear resampler from interleaved PCM16LE to mono PCM16LE."""

    def __init__(self, input_sample_rate_hz: int, input_channels: int, output_sample_rate_hz: int) -> None:
        if input_sample_rate_hz <= 0 or output_sample_rate_hz <= 0:
            raise ValueError("sample rates must be positive")
        if input_channels <= 0:
            raise ValueError("input_channels must be positive")
        self._input_rate = input_sample_rate_hz
        self._channels = input_channels
        self._output_rate = output_sample_rate_hz
        self._samples: list[int] = []
        self._base_index = 0
        self._next_position_numerator = 0
        self._flushed = False

    def push(self, pcm16le: bytes) -> bytes:
        if self._flushed:
            raise RuntimeError("cannot push audio after flush")
        raw = _pcm16le_to_samples(pcm16le)
        if len(raw) % self._channels:
            raise ValueError("PCM sample count is not aligned to channel count")
        if self._channels == 1:
            mono = list(raw)
        else:
            mono = []
            for offset in range(0, len(raw), self._channels):
                mono.append(round(sum(raw[offset : offset + self._channels]) / self._channels))
        self._samples.extend(mono)
        return _samples_to_pcm16le(self._produce(repeat_last=False))

    def flush(self) -> bytes:
        if self._flushed:
            return b""
        self._flushed = True
        output = self._produce(repeat_last=True)
        self._samples.clear()
        return _samples_to_pcm16le(output)

    def _produce(self, *, repeat_last: bool) -> list[int]:
        if not self._samples:
            return []
        last_index = self._base_index + len(self._samples) - 1
        output: list[int] = []
        while True:
            index, remainder = divmod(self._next_position_numerator, self._output_rate)
            if index > last_index:
                break
            relative = index - self._base_index
            if relative < 0:
                raise RuntimeError("resampler position fell behind its retained input")
            if remainder:
                if index == last_index:
                    if not repeat_last:
                        break
                    following = self._samples[relative]
                else:
                    following = self._samples[relative + 1]
                current = self._samples[relative]
                value = round(current + (following - current) * remainder / self._output_rate)
            else:
                value = self._samples[relative]
            output.append(max(-32_768, min(32_767, value)))
            self._next_position_numerator += self._input_rate

        next_index = self._next_position_numerator // self._output_rate
        removable = max(0, min(len(self._samples), next_index - self._base_index))
        if removable:
            del self._samples[:removable]
            self._base_index += removable
        return output


class WirePcmConverter(Pcm16MonoResampler):
    def __init__(self, input_sample_rate_hz: int, input_channels: int) -> None:
        super().__init__(input_sample_rate_hz, input_channels, WIRE_SAMPLE_RATE_HZ)


class PcmFramer:
    def __init__(self, frame_bytes: int = WIRE_BYTES_PER_FRAME) -> None:
        if frame_bytes <= 0:
            raise ValueError("frame_bytes must be positive")
        self._frame_bytes = frame_bytes
        self._buffer = bytearray()

    @property
    def pending_bytes(self) -> int:
        return len(self._buffer)

    def push(self, pcm: bytes) -> list[bytes]:
        self._buffer.extend(pcm)
        frames: list[bytes] = []
        while len(self._buffer) >= self._frame_bytes:
            frames.append(bytes(self._buffer[: self._frame_bytes]))
            del self._buffer[: self._frame_bytes]
        return frames

    def flush(self, *, pad: bool = False) -> bytes | None:
        if not self._buffer:
            return None
        if not pad:
            raise ValueError("partial PCM frame remains")
        frame = bytes(self._buffer) + b"\x00" * (self._frame_bytes - len(self._buffer))
        self._buffer.clear()
        return frame


__all__ = ["Pcm16MonoResampler", "PcmFramer", "WirePcmConverter"]
