from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class Confidence(StrEnum):
    HIGH = "high"
    LOW = "low"


class ClarificationType(StrEnum):
    AMBIGUOUS_TIME = "ambiguous_time"
    MISSING_TIME = "missing_time"
    INVALID_DATE = "invalid_date"
    PAST_DATETIME = "past_datetime"
    INVALID_TIMEZONE = "invalid_timezone"
    DST_TIME = "dst_time"
    UNRECOGNIZED = "unrecognized"


@dataclass(slots=True)
class ParseResult:
    title: str
    date: str | None
    time: str | None
    date_confidence: str = Confidence.HIGH
    time_confidence: str = Confidence.HIGH
    needs_clarification: bool = False
    clarification_type: str | None = None
    clarification_options: list[str] | None = None
    date_was_explicit: bool = False
    time_was_explicit: bool = False
    parser_type: str = "unknown"

    def __post_init__(self) -> None:
        if self.clarification_options is None:
            self.clarification_options = []

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParseResult":
        allowed = cls.__dataclass_fields__.keys()
        return cls(**{key: value for key, value in data.items() if key in allowed})


@dataclass(frozen=True, slots=True)
class Reminder:
    id: int
    telegram_user_id: int
    chat_id: int
    title: str
    source_text: str
    scheduled_at_utc: datetime
    user_timezone: str
    status: str
    parser_type: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PendingRequest:
    telegram_user_id: int
    chat_id: int
    source_text: str
    parse_result: ParseResult
    clarification_type: str
    created_at: datetime
    message_id: int | None = None


class ParseError(ValueError):
    """Базовая ошибка разбора пользовательского запроса."""


class UnsupportedInput(ParseError):
    """Rule-based parser не распознал запрос уверенно."""
