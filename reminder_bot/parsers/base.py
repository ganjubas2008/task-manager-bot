from __future__ import annotations

from datetime import datetime
from typing import Protocol

from reminder_bot.domain import ParseResult


class ReminderParser(Protocol):
    def parse(self, text: str, now: datetime, timezone_name: str) -> ParseResult: ...
