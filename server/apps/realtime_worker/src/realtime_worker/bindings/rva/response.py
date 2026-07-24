"""Single owner for RVA response identity, fencing, and playback facts."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Literal

from realtime_worker.voice.session import CancelDisposition, PlaybackRef, VoiceSessionState

from .protocol import RvaBindingError

ResponseOutcome = Literal["completed", "cancelled", "failed"]
PlaybackOutcome = Literal["completed", "stopped", "failed"]


@dataclass(slots=True)
class ResponseRecord:
    response_id: str
    target: PlaybackRef
    producer_epoch: int
    first_media_sequence: int | None = None
    last_media_sequence: int | None = None
    outcome: ResponseOutcome | None = None
    error_code: str | None = None
    stop_sent: bool = False
    playback_started: bool = False
    playback_ended: bool = False


@dataclass(frozen=True, slots=True)
class ResponseFence:
    record: ResponseRecord
    fence_generation: int


@dataclass(frozen=True, slots=True)
class PlaybackEndedFact:
    record: ResponseRecord
    outcome: PlaybackOutcome
    played_samples: int
    last_media_sequence: int | None


class ResponseCoordinator:
    """Serialize semantic responses while retaining terminal targets for ACKs."""

    def __init__(self, *, connection_epoch: str, close_ports: tuple[object, ...], close_timeout: float) -> None:
        self._state = VoiceSessionState(
            close_ports=close_ports,  # type: ignore[arg-type]
            connection_epoch=connection_epoch,
            close_stage_timeout_seconds=close_timeout,
        )
        self._active: ResponseRecord | None = None
        self._playback: ResponseRecord | None = None
        self._records: dict[tuple[str, int], ResponseRecord] = {}
        self._terminal_order: deque[tuple[str, int]] = deque()

    @property
    def active(self) -> ResponseRecord | None:
        return self._active

    @property
    def playback(self) -> ResponseRecord | None:
        return self._playback

    @property
    def generation(self) -> int:
        return self._state.playback_generation

    @property
    def closed(self) -> bool:
        return self._state.closed

    async def begin(self, response_id: str, producer_epoch: int) -> tuple[ResponseRecord, bool]:
        active = self._active
        if active is not None:
            if active.producer_epoch != producer_epoch:
                raise RvaBindingError("response_already_active")
            return active, False
        if self._playback is not None:
            raise RvaBindingError("playback_already_active")
        target = await self._state.begin_playback()
        record = ResponseRecord(response_id, target, producer_epoch)
        self._active = record
        self._playback = record
        self._records[(response_id, target.generation)] = record
        return record, True

    def accepts(self, record: ResponseRecord) -> bool:
        return record is self._active and self._state.accepts_callback(record.target)

    def note_media(self, record: ResponseRecord, sequence: int) -> None:
        if not self.accepts(record):
            raise RvaBindingError("stale_generation")
        if record.first_media_sequence is None:
            record.first_media_sequence = sequence
        record.last_media_sequence = sequence

    async def complete(self, record: ResponseRecord) -> None:
        if record.first_media_sequence is None or record.last_media_sequence is None:
            raise RvaBindingError("response_has_no_media")
        if record is not self._active or record is not self._playback:
            raise RvaBindingError("stale_generation")
        self._active = None
        record.outcome = "completed"
        self._remember_terminal(record)

    async def fence(
        self,
        record: ResponseRecord,
        *,
        outcome: Literal["cancelled", "failed"],
        error_code: str | None = None,
    ) -> ResponseFence:
        if record is not self._playback or record.stop_sent:
            raise RvaBindingError("stale_generation")
        disposition = await self._state.cancel_playback(record.target)
        if disposition is not CancelDisposition.APPLIED:
            raise RvaBindingError("stale_generation")
        if record is self._active:
            self._active = None
            record.outcome = outcome
            record.error_code = error_code
        record.stop_sent = True
        self._remember_terminal(record)
        return ResponseFence(record, self._state.playback_generation)

    def playback_started(self, response_id: str, generation: int, first_media_sequence: int) -> ResponseRecord:
        record = self._record(response_id, generation)
        if record.playback_started or record.playback_ended:
            raise RvaBindingError("duplicate_playback_started")
        if (
            record.first_media_sequence is None
            or record.last_media_sequence is None
            or not record.first_media_sequence <= first_media_sequence <= record.last_media_sequence
        ):
            raise RvaBindingError("invalid_playback_sequence")
        record.playback_started = True
        return record

    async def playback_ended(
        self,
        response_id: str,
        generation: int,
        *,
        outcome: PlaybackOutcome,
        played_samples: int,
        last_media_sequence: int | None,
    ) -> PlaybackEndedFact:
        record = self._record(response_id, generation)
        if record.playback_ended:
            raise RvaBindingError("duplicate_playback_ended")
        if record is not self._playback:
            raise RvaBindingError("stale_generation")
        expected = {"completed": "completed", "cancelled": "stopped", "failed": "failed"}.get(record.outcome)
        if record.outcome == "completed" and record.stop_sent:
            expected = "stopped"
        if expected is None or outcome != expected:
            raise RvaBindingError("invalid_playback_outcome")
        if outcome == "completed" and played_samples == 0:
            raise RvaBindingError("playback_evidence_mismatch")
        if outcome == "completed" and last_media_sequence != record.last_media_sequence:
            raise RvaBindingError("invalid_playback_sequence")
        if outcome != "completed" and last_media_sequence is not None:
            invalid_last_sequence = (
                played_samples == 0
                or record.last_media_sequence is None
                or last_media_sequence > record.last_media_sequence
            )
            if invalid_last_sequence:
                raise RvaBindingError("invalid_playback_sequence")
        record.playback_ended = True
        if outcome == "completed" and not await self._state.finish_playback(record.target):
            raise RvaBindingError("stale_generation")
        self._playback = None
        return PlaybackEndedFact(record, outcome, played_samples, last_media_sequence)

    async def close(self) -> None:
        await self._state.close()

    def _record(self, response_id: str, generation: int) -> ResponseRecord:
        record = self._records.get((response_id, generation))
        if record is None:
            raise RvaBindingError("stale_generation")
        return record

    def _remember_terminal(self, record: ResponseRecord) -> None:
        key = (record.response_id, record.target.generation)
        self._terminal_order.append(key)
        while len(self._terminal_order) > 32:
            old = self._terminal_order.popleft()
            old_record = self._records.get(old)
            if old_record is not None and old_record.playback_ended:
                self._records.pop(old, None)


__all__ = [
    "PlaybackEndedFact",
    "ResponseCoordinator",
    "ResponseFence",
    "ResponseOutcome",
    "ResponseRecord",
]
