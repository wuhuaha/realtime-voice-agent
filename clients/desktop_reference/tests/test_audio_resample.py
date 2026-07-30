from __future__ import annotations

import sys
from array import array

import pytest

from rva_desktop.audio import WIRE_BYTES_PER_FRAME, Pcm16MonoResampler, PcmFramer, WirePcmConverter


def pcm(samples: list[int]) -> bytes:
    values = array("h", samples)
    if sys.byteorder != "little":
        values.byteswap()
    return values.tobytes()


def unpack(data: bytes) -> list[int]:
    values = array("h")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    return list(values)


def test_48khz_mono_converts_to_one_exact_wire_frame() -> None:
    converter = WirePcmConverter(48_000, 1)
    output = converter.push(pcm(list(range(2_880))))
    assert len(output) == WIRE_BYTES_PER_FRAME
    assert unpack(output)[:4] == [0, 3, 6, 9]


def test_stereo_is_downmixed_before_resampling() -> None:
    converter = WirePcmConverter(16_000, 2)
    output = converter.push(pcm([1_000, -1_000, 2_000, 0]))
    assert unpack(output) == [0, 1_000]


def test_streaming_resampler_is_chunk_boundary_independent() -> None:
    samples = [((index * 97) % 40_000) - 20_000 for index in range(2_646)]
    whole = Pcm16MonoResampler(44_100, 1, 16_000)
    expected = whole.push(pcm(samples)) + whole.flush()

    chunked = Pcm16MonoResampler(44_100, 1, 16_000)
    actual = b""
    for start in range(0, len(samples), 137):
        actual += chunked.push(pcm(samples[start : start + 137]))
    actual += chunked.flush()

    assert actual == expected
    assert len(actual) == WIRE_BYTES_PER_FRAME


def test_resampler_rejects_partial_interleaved_sample() -> None:
    converter = WirePcmConverter(48_000, 2)
    with pytest.raises(ValueError, match="channel"):
        converter.push(pcm([1]))


def test_framer_retains_partial_data_and_requires_explicit_padding() -> None:
    framer = PcmFramer(frame_bytes=4)
    assert framer.push(b"abc") == []
    assert framer.push(b"def") == [b"abcd"]
    assert framer.pending_bytes == 2
    with pytest.raises(ValueError, match="partial"):
        framer.flush()
    assert framer.flush(pad=True) == b"ef\x00\x00"
    assert framer.pending_bytes == 0
