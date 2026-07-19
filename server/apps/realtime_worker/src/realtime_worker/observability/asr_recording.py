"""Best-effort WAV capture for inspecting audio delivered to the ASR adapter."""

from __future__ import annotations

import logging
import re
import uuid
import wave
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class AsrWavRecorder:
    """Append mono PCM16LE frames to one diagnostic file without affecting ASR."""

    def __init__(
        self,
        directory: Path,
        *,
        sample_rate: int,
        room: str | None,
        on_saved: Callable[[Path], None] | None = None,
        on_error: Callable[[], None] | None = None,
    ) -> None:
        self._directory = directory
        self._sample_rate = sample_rate
        self._room = room
        self._on_saved = on_saved
        self._on_error = on_error
        self._writer: wave.Wave_write | None = None
        self._path: Path | None = None
        self._failed = False

    @property
    def path(self) -> Path | None:
        return self._path

    def write(self, pcm_s16le: bytes) -> None:
        if self._failed or not pcm_s16le:
            return
        try:
            if self._writer is None:
                self._open()
            assert self._writer is not None
            self._writer.writeframesraw(pcm_s16le)
        except (OSError, wave.Error) as exc:
            self._fail(exc)

    def close(self) -> None:
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        try:
            writer.close()
        except (OSError, wave.Error) as exc:
            self._fail(exc)
            return
        if self._path is not None and self._on_saved is not None:
            self._on_saved(self._path)

    def _open(self) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        room = re.sub(r"[^A-Za-z0-9_.-]+", "_", self._room or "unknown-room").strip("._") or "unknown-room"
        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        self._path = self._directory / f"asr-input-{room}-{timestamp}-{uuid.uuid4().hex[:8]}.wav"
        writer = wave.open(str(self._path), "wb")
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(self._sample_rate)
        self._writer = writer

    def _fail(self, exc: BaseException) -> None:
        logger.warning("ASR diagnostic recording disabled after write failure: %s", type(exc).__name__)
        self._failed = True
        writer = self._writer
        self._writer = None
        if writer is not None:
            try:
                writer.close()
            except (OSError, wave.Error):
                pass
        if self._path is not None:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass
        if self._on_error is not None:
            self._on_error()
