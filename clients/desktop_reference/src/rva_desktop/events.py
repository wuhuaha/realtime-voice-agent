from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .protocol import MediaFrame, PlaybackTarget

EventKind = Literal[
    "session.opened",
    "transcript.delta",
    "transcript.final",
    "response.begin",
    "response.text",
    "response.end",
    "playback.stop",
    "media.audio",
    "session.error",
    "session.close",
    "transport.reopen_required",
]


@dataclass(frozen=True, slots=True)
class SessionEvent:
    kind: EventKind
    message: dict[str, Any] = field(default_factory=dict)
    target: PlaybackTarget | None = None
    media: MediaFrame | None = None
