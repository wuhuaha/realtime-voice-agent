from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from ..errors import ProtocolError, SessionClosed
from ..events import SessionEvent
from ..protocol import FLAG_AUDIO, MediaFrame, PlaybackTarget, SessionOpened, validate_server_message

_TOMBSTONE_LIMIT = 64


@dataclass(slots=True)
class _Playback:
    target: PlaybackTarget
    started: bool = False
    ended: bool = False
    stopped: bool = False
    final_media_sequence: int | None = None
    response_terminal: bool = False
    stop_fence: int | None = None
    stop_cause: str | None = None
    first_media_sequence: int | None = None
    last_media_sequence: int | None = None


class SessionState:
    """Strict endpoint-side projection of the canonical RVA lifecycle."""

    def __init__(self, opened: SessionOpened) -> None:
        self.opened = opened
        self.closed = False
        self.fence_generation = 1
        self.highest_generation = 0
        self.current_playback: PlaybackTarget | None = None
        self._playbacks: dict[int, _Playback] = {}
        self._playback_tombstones: deque[_Playback] = deque(maxlen=_TOMBSTONE_LIMIT)
        self._transcript_sequences: dict[str, int] = {}
        self._final_transcripts: set[str] = set()
        self._final_transcript_order: deque[str] = deque()
        self._response_text_sequence: dict[int, int] = {}
        self._last_media_timestamp: dict[int, int] = {}
        self._next_downlink_sequence = 0 if opened.selected_profile.value == "wss-opus-v3" else 1

    def accept_control(self, message: dict[str, Any]) -> SessionEvent | None:
        if self.closed:
            raise SessionClosed()
        validate_server_message(message)
        if message["type"] == "session.opened":
            raise ProtocolError("duplicate_session_opened")
        self._identity(message)
        kind = message["type"]
        target: PlaybackTarget | None = None
        if kind.startswith("transcript."):
            utterance_id = str(message["utterance_id"])
            if utterance_id in self._final_transcripts:
                raise ProtocolError("transcript_already_final")
            self._advance_sequence(
                self._transcript_sequences,
                utterance_id,
                int(message["sequence"]),
            )
            if kind == "transcript.final":
                self._remember_final_transcript(utterance_id)
                self._transcript_sequences.pop(utterance_id, None)
        elif kind == "response.begin":
            target = PlaybackTarget(str(message["response_id"]), int(message["generation"]))
            if target.generation <= self.highest_generation or target.generation < self.fence_generation:
                raise ProtocolError("stale_generation")
            if self.current_playback is not None:
                raise ProtocolError("playback_already_active")
            self.highest_generation = target.generation
            self.current_playback = target
            self._playbacks[target.generation] = _Playback(target)
            self._response_text_sequence[target.generation] = 0
        elif kind == "response.text":
            target = self._message_target(message)
            self._require_current(target)
            self._advance_sequence(
                self._response_text_sequence,
                target.generation,
                int(message["sequence"]),
            )
        elif kind == "response.end":
            target = self._message_target(message)
            playback = self._require_known(target)
            if playback.response_terminal:
                raise ProtocolError("response_already_terminal")
            outcome = message["outcome"]
            if outcome not in {"completed", "cancelled", "failed"}:
                raise ProtocolError("invalid_response_outcome")
            if outcome == "completed":
                playback.final_media_sequence = int(message["final_media_sequence"])
            playback.response_terminal = True
        elif kind == "playback.stop":
            raw_target = message["target"]
            target = PlaybackTarget(str(raw_target["response_id"]), int(raw_target["generation"]))
            playback = self._require_known(target)
            if playback.ended:
                raise ProtocolError("playback_already_ended")
            fence = int(message["fence_generation"])
            if playback.stop_fence is not None:
                if playback.stop_fence == fence and playback.stop_cause == message["cause"]:
                    return None
                raise ProtocolError("playback_stop_conflict")
            if fence <= target.generation or fence <= self.fence_generation:
                raise ProtocolError("stale_generation")
            self.fence_generation = fence
            playback.stopped = True
            playback.stop_fence = fence
            playback.stop_cause = str(message["cause"])
        elif kind == "session.close":
            self.closed = True
        return SessionEvent(kind, dict(message), target=target)  # type: ignore[arg-type]

    def accept_media(self, frame: MediaFrame) -> SessionEvent | None:
        if self.closed:
            raise SessionClosed()
        admitted = self.validate_media_admission(frame)
        if self.opened.selected_profile.value == "wss-opus-v3":
            if frame.sequence != self._next_downlink_sequence:
                raise ProtocolError("invalid_media_sequence")
        elif frame.sequence < self._next_downlink_sequence:
            raise ProtocolError("invalid_media_sequence")
        self._next_downlink_sequence = frame.sequence + 1
        if not admitted:
            return None
        self._last_media_timestamp[frame.generation] = frame.timestamp
        playback = self._playbacks[frame.generation]
        if playback.first_media_sequence is None:
            playback.first_media_sequence = frame.sequence
        playback.last_media_sequence = frame.sequence
        return SessionEvent("media.audio", target=playback.target, media=frame)

    def validate_media_admission(self, frame: MediaFrame) -> bool:
        """Check identity and exact target before transport commits packet admission."""

        if frame.media_id != self.opened.media_id or frame.media_epoch != self.opened.media_epoch:
            raise ProtocolError("stale_media_identity")
        if frame.flags != FLAG_AUDIO:
            raise ProtocolError("invalid_media_flags")
        playback = self._find_playback(frame.generation)
        if playback is None:
            raise ProtocolError("unknown_media_generation")
        if frame.generation < self.fence_generation or playback.stopped or playback.ended:
            return False
        if self.current_playback != playback.target:
            raise ProtocolError("playback_target_mismatch")
        previous_timestamp = self._last_media_timestamp.get(frame.generation)
        if previous_timestamp is not None:
            delta = (frame.timestamp - previous_timestamp) & 0xFFFFFFFF
            if self.opened.selected_profile.value == "wss-opus-v3":
                if delta != 960:
                    raise ProtocolError("invalid_media_timestamp")
            elif delta == 0 or delta % 960 != 0:
                raise ProtocolError("invalid_media_timestamp")
        return True

    def playback_started(self, target: PlaybackTarget, first_media_sequence: int) -> dict[str, Any]:
        playback = self._require_known(target)
        if playback.started:
            raise ProtocolError("playback_already_started")
        if playback.ended:
            raise ProtocolError("playback_already_ended")
        sequence = _uint32(first_media_sequence, "first_media_sequence")
        if playback.stopped or sequence != playback.first_media_sequence:
            raise ProtocolError("playback_evidence_mismatch")
        playback.started = True
        return self._fact(
            "playback.started",
            target,
            first_media_sequence=sequence,
        )

    def playback_ended(
        self,
        target: PlaybackTarget,
        *,
        outcome: str,
        played_samples: int,
        last_media_sequence: int | None,
    ) -> dict[str, Any]:
        playback = self._require_known(target)
        if playback.ended:
            raise ProtocolError("playback_already_ended")
        if outcome not in {"completed", "stopped", "failed"}:
            raise ProtocolError("invalid_playback_outcome")
        if type(played_samples) is not int or not 0 <= played_samples <= 0xFFFFFFFFFFFFFFFF:
            raise ProtocolError("invalid_played_samples")
        if played_samples == 0 and last_media_sequence is not None:
            raise ProtocolError("playback_evidence_mismatch")
        if last_media_sequence is not None:
            sequence = _uint32(last_media_sequence, "last_media_sequence")
            if not playback.started or sequence != playback.last_media_sequence:
                raise ProtocolError("playback_evidence_mismatch")
        elif played_samples != 0:
            raise ProtocolError("playback_evidence_mismatch")
        if playback.started and played_samples == 0:
            raise ProtocolError("playback_evidence_mismatch")
        if outcome == "completed":
            if (
                not playback.started
                or not playback.response_terminal
                or playback.final_media_sequence is None
                or played_samples == 0
                or last_media_sequence != playback.final_media_sequence
            ):
                raise ProtocolError("playback_evidence_mismatch")
        playback.ended = True
        if self.current_playback == target:
            self.current_playback = None
        self._playbacks.pop(target.generation, None)
        self._response_text_sequence.pop(target.generation, None)
        self._last_media_timestamp.pop(target.generation, None)
        self._playback_tombstones.append(playback)
        fields: dict[str, Any] = {"outcome": outcome, "played_samples": played_samples}
        if last_media_sequence is not None:
            fields["last_media_sequence"] = last_media_sequence
        return self._fact("playback.ended", target, **fields)

    def cancel_request(self, target: PlaybackTarget, request_id: str) -> dict[str, Any]:
        self._require_current(target)
        return self._fact(
            "response.cancel.request",
            target,
            request_id=request_id,
            cause="user_request",
        )

    def close_message(self, reason: str, *, detail: str | None = None) -> dict[str, Any]:
        if self.closed:
            raise SessionClosed()
        if reason not in {"normal", "idle_timeout", "network_change", "protocol_error", "server_shutdown"}:
            raise ProtocolError("invalid_close_reason")
        if detail is not None and ("\x00" in detail or len(detail.encode("utf-8")) > 256):
            raise ProtocolError("invalid_close_detail")
        self.closed = True
        message: dict[str, Any] = {
            "type": "session.close",
            "session_id": self.opened.session_id,
            "session_epoch": self.opened.session_epoch,
            "reason": reason,
            "initiated_by": "device",
        }
        if detail is not None:
            message["detail"] = detail
        return message

    def _identity(self, message: dict[str, Any]) -> None:
        if message["session_id"] != self.opened.session_id or message["session_epoch"] != self.opened.session_epoch:
            raise ProtocolError("stale_session")

    def _message_target(self, message: dict[str, Any]) -> PlaybackTarget:
        return PlaybackTarget(str(message["response_id"]), int(message["generation"]))

    def _require_current(self, target: PlaybackTarget) -> _Playback:
        if target != self.current_playback:
            raise ProtocolError("response_target_mismatch")
        return self._require_known(target)

    def _require_known(self, target: PlaybackTarget) -> _Playback:
        playback = self._find_playback(target.generation)
        if playback is None or playback.target != target:
            raise ProtocolError("playback_target_mismatch")
        return playback

    def _find_playback(self, generation: int) -> _Playback | None:
        playback = self._playbacks.get(generation)
        if playback is not None:
            return playback
        return next(
            (item for item in reversed(self._playback_tombstones) if item.target.generation == generation),
            None,
        )

    def _remember_final_transcript(self, utterance_id: str) -> None:
        if len(self._final_transcript_order) >= _TOMBSTONE_LIMIT:
            expired = self._final_transcript_order.popleft()
            self._final_transcripts.remove(expired)
        self._final_transcript_order.append(utterance_id)
        self._final_transcripts.add(utterance_id)

    def _fact(self, kind: str, target: PlaybackTarget, **fields: Any) -> dict[str, Any]:
        return {
            "type": kind,
            "session_id": self.opened.session_id,
            "session_epoch": self.opened.session_epoch,
            **fields,
            "target": {"response_id": target.response_id, "generation": target.generation},
        }

    @staticmethod
    def _advance_sequence(ledger: dict[Any, int], scope: Any, sequence: int) -> None:
        expected = ledger.get(scope, 0)
        if sequence != expected:
            raise ProtocolError("invalid_sequence")
        ledger[scope] = expected + 1


def _uint32(value: int, field: str) -> int:
    if type(value) is not int or not 0 <= value <= 0xFFFFFFFF:
        raise ProtocolError(f"invalid_{field}")
    return value
