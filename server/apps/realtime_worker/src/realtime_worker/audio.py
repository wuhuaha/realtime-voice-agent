"""Transport-neutral in-memory PCM frame used by the roomless voice runtime."""

from __future__ import annotations

from dataclasses import dataclass

PCM_SAMPLE_RATE = 16_000
PCM_SAMPLES = 320
PCM_BYTES = PCM_SAMPLES * 2


class AudioFrameError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PcmFrame:
    generation: int
    sequence: int
    timestamp_samples: int
    pcm: bytes
    enqueued_at: float = 0.0

    def __post_init__(self) -> None:
        if len(self.pcm) != PCM_BYTES:
            raise AudioFrameError(f"PCM frame must contain exactly {PCM_BYTES} bytes")
