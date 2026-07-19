from __future__ import annotations

from dataclasses import dataclass


@dataclass
class MutableClock:
    value: float = 1_700_000_000.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds
