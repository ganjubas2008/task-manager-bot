from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol


class Clock(Protocol):
    def now_utc(self) -> datetime: ...


class SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)


class FixedClock:
    def __init__(self, value: datetime):
        if value.tzinfo is None:
            raise ValueError("FixedClock требует datetime с timezone")
        self.value = value.astimezone(timezone.utc)

    def now_utc(self) -> datetime:
        return self.value
