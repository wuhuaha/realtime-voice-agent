"""Optional PyAV/libopus adapter for the canonical RVA 60 ms media frame."""

from __future__ import annotations

import ctypes
import ctypes.util
import importlib.util
from fractions import Fraction
from pathlib import Path
from typing import Any

from .ports import WIRE_BYTES_PER_FRAME, WIRE_FORMAT, WIRE_FRAME_DURATION_MS, WIRE_SAMPLES_PER_FRAME


class OpusUnavailableError(RuntimeError):
    pass


class OpusDecodeError(ValueError):
    pass


def pyav_available() -> bool:
    return importlib.util.find_spec("av") is not None


class PyAvOpusCodec:
    """Stateful codec that has no import-time dependency on PyAV."""

    def __init__(self, *, bit_rate: int = 24_000) -> None:
        try:
            import av
            from av import AudioFrame, CodecContext
        except ImportError as exc:
            raise OpusUnavailableError("PyAV with libopus support is required for Opus media") from exc

        self._audio_frame_type = AudioFrame
        time_base = Fraction(1, WIRE_FORMAT.sample_rate_hz)
        self._opus = _load_opus_library(Path(av.__file__).resolve())
        self._configure_decoder_api()
        error = ctypes.c_int()
        decoder = self._opus.opus_decoder_create(
            WIRE_FORMAT.sample_rate_hz,
            WIRE_FORMAT.channels,
            ctypes.byref(error),
        )
        if not decoder or error.value != 0:
            raise OpusUnavailableError(f"libopus decoder initialization failed ({error.value})")

        try:
            encoder = CodecContext.create("libopus", "w")
            encoder.sample_rate = WIRE_FORMAT.sample_rate_hz
            encoder.layout = "mono"
            encoder.format = "s16"
            encoder.bit_rate = bit_rate
            encoder.time_base = time_base
            encoder.options = {
                "application": "audio",
                "dtx": "1",
                "frame_duration": str(WIRE_FRAME_DURATION_MS),
            }
            encoder.open()
        except BaseException:
            self._opus.opus_decoder_destroy(decoder)
            raise
        self._decoder = decoder
        self._encoder: Any = encoder
        self._time_base = time_base
        self._encoder_pts = 0
        self._closed = False

    def encode_60ms(self, pcm16le: bytes) -> bytes:
        self._require_open()
        if len(pcm16le) != WIRE_BYTES_PER_FRAME:
            raise ValueError(f"Opus input must contain exactly {WIRE_BYTES_PER_FRAME} PCM bytes")
        audio = self._audio_frame_type(format="s16", layout="mono", samples=WIRE_SAMPLES_PER_FRAME)
        audio.sample_rate = WIRE_FORMAT.sample_rate_hz
        audio.time_base = self._time_base
        audio.pts = self._encoder_pts
        audio.planes[0].update(pcm16le)
        self._encoder_pts += WIRE_SAMPLES_PER_FRAME
        packets = self._encoder.encode(audio)
        if len(packets) != 1:
            raise ValueError(f"libopus emitted {len(packets)} packets for one 60 ms frame")
        return bytes(packets[0])

    def decode_60ms(self, payload: bytes) -> bytes:
        self._require_open()
        if not payload:
            raise OpusDecodeError("Opus payload is empty")
        return self._decode(payload)

    def conceal_60ms(self) -> bytes:
        """Ask the stateful Opus decoder to conceal one missing 60 ms packet."""

        self._require_open()
        return self._decode(None)

    def close(self) -> None:
        if self._decoder:
            self._opus.opus_decoder_destroy(self._decoder)
        self._closed = True
        self._decoder = None
        self._encoder = None

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("Opus codec is closed")

    def _configure_decoder_api(self) -> None:
        self._opus.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        self._opus.opus_decoder_create.restype = ctypes.c_void_p
        self._opus.opus_decode.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_ubyte),
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_int16),
            ctypes.c_int,
            ctypes.c_int,
        ]
        self._opus.opus_decode.restype = ctypes.c_int
        self._opus.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        self._opus.opus_decoder_destroy.restype = None

    def _decode(self, payload: bytes | None) -> bytes:
        pcm = (ctypes.c_int16 * WIRE_SAMPLES_PER_FRAME)()
        packet: Any = None
        packet_pointer = None
        packet_size = 0
        if payload is not None:
            packet = (ctypes.c_ubyte * len(payload)).from_buffer_copy(payload)
            packet_pointer = ctypes.cast(packet, ctypes.POINTER(ctypes.c_ubyte))
            packet_size = len(payload)
        decoded_samples = self._opus.opus_decode(
            self._decoder,
            packet_pointer,
            packet_size,
            pcm,
            WIRE_SAMPLES_PER_FRAME,
            0,
        )
        if decoded_samples < 0:
            raise OpusDecodeError(f"libopus decode failed ({decoded_samples})")
        if decoded_samples != WIRE_SAMPLES_PER_FRAME:
            raise OpusDecodeError("Opus packet does not contain exactly 60 ms of audio")
        result = bytes(pcm)
        if len(result) != WIRE_BYTES_PER_FRAME:
            raise OpusDecodeError("Opus packet decoded to an unexpected PCM length")
        return result


def _load_opus_library(av_module_path: Path) -> Any:
    candidate_paths: list[Path] = []
    for directory in (av_module_path.parent, av_module_path.parent.parent / "av.libs"):
        if directory.is_dir():
            candidate_paths.extend(path for path in directory.glob("*opus*") if path.is_file())
    system_library = ctypes.util.find_library("opus")
    candidates = [str(path) for path in candidate_paths]
    if system_library:
        candidates.append(system_library)
    for candidate in candidates:
        try:
            return ctypes.CDLL(candidate)
        except OSError:
            continue
    raise OpusUnavailableError("PyAV is installed but a loadable libopus decoder was not found")


__all__ = ["OpusDecodeError", "OpusUnavailableError", "PyAvOpusCodec", "pyav_available"]
