"""Audio ports and adapters for interactive and deterministic desktop clients."""

from .fixture import FixturePcmSource, NullAudioSink, RecordingAudioSink
from .opus import OpusDecodeError, OpusUnavailableError, PyAvOpusCodec, pyav_available
from .ports import (
    WIRE_BYTES_PER_FRAME,
    WIRE_FORMAT,
    WIRE_FRAME_DURATION_MS,
    WIRE_SAMPLE_RATE_HZ,
    WIRE_SAMPLES_PER_FRAME,
    AudioFormat,
    AudioSink,
    AudioSource,
    Clock,
    PcmFrame,
    RenderAck,
    SystemClock,
)
from .queue import AudioQueueClosed, BoundedAudioQueue
from .resample import Pcm16MonoResampler, PcmFramer, WirePcmConverter
from .sounddevice_backend import (
    SoundDeviceAudioSink,
    SoundDeviceAudioSource,
    SoundDeviceInputConfig,
    SoundDeviceOutputConfig,
    SoundDeviceUnavailableError,
)

__all__ = [
    "AudioFormat",
    "AudioQueueClosed",
    "AudioSink",
    "AudioSource",
    "BoundedAudioQueue",
    "Clock",
    "FixturePcmSource",
    "NullAudioSink",
    "OpusDecodeError",
    "OpusUnavailableError",
    "Pcm16MonoResampler",
    "PcmFrame",
    "RenderAck",
    "PcmFramer",
    "PyAvOpusCodec",
    "RecordingAudioSink",
    "SoundDeviceAudioSink",
    "SoundDeviceAudioSource",
    "SoundDeviceInputConfig",
    "SoundDeviceOutputConfig",
    "SoundDeviceUnavailableError",
    "SystemClock",
    "WIRE_BYTES_PER_FRAME",
    "WIRE_FORMAT",
    "WIRE_FRAME_DURATION_MS",
    "WIRE_SAMPLE_RATE_HZ",
    "WIRE_SAMPLES_PER_FRAME",
    "WirePcmConverter",
    "pyav_available",
]
