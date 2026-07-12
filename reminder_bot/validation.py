from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from reminder_bot.domain import ClarificationType, ParseResult


@dataclass(frozen=True, slots=True)
class ValidationError(ValueError):
    code: str
    user_message: str

    def __str__(self) -> str:
        return self.user_message


def validate_timezone(timezone_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValidationError(
            ClarificationType.INVALID_TIMEZONE,
            "Не знаю такой часовой пояс. Пришли название вроде Europe/Moscow.",
        ) from error


def validate_and_build_datetime(
    result: ParseResult,
    now: datetime,
    timezone_name: str,
) -> datetime:
    """Проверяет недоверенный parse result и возвращает UTC datetime."""
    zone = validate_timezone(timezone_name)
    if now.tzinfo is None:
        raise ValueError("now должен содержать timezone")
    now_utc = now.astimezone(timezone.utc)

    if result.needs_clarification or not result.date or not result.time:
        raise ValidationError(
            result.clarification_type or ClarificationType.UNRECOGNIZED,
            "Нужно уточнить дату или время.",
        )
    if not result.title or not result.title.strip():
        raise ValidationError(ClarificationType.UNRECOGNIZED, "Не удалось понять, о чем напомнить.")

    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", result.date):
        raise ValidationError(
            ClarificationType.INVALID_DATE,
            "Такой даты не существует. Уточни дату.",
        )
    try:
        parsed_date = date.fromisoformat(result.date)
    except (TypeError, ValueError) as error:
        raise ValidationError(
            ClarificationType.INVALID_DATE,
            "Такой даты не существует. Уточни дату.",
        ) from error
    if not re.fullmatch(r"\d{2}:\d{2}", result.time):
        raise ValidationError(
            ClarificationType.UNRECOGNIZED,
            "Такого времени не существует. Уточни время.",
        )
    try:
        parsed_time = time.fromisoformat(result.time)
    except (TypeError, ValueError) as error:
        raise ValidationError(
            ClarificationType.UNRECOGNIZED,
            "Такого времени не существует. Уточни время.",
        ) from error

    naive = datetime.combine(parsed_date, parsed_time.replace(tzinfo=None))
    candidates: list[datetime] = []
    for fold in (0, 1):
        local = naive.replace(tzinfo=zone, fold=fold)
        roundtrip = local.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)
        if roundtrip == naive:
            candidates.append(local)

    if not candidates:
        raise ValidationError(
            ClarificationType.DST_TIME,
            "В это местное время часы переводятся. Выбери другое время.",
        )
    if len(candidates) == 2 and candidates[0].utcoffset() != candidates[1].utcoffset():
        raise ValidationError(
            ClarificationType.DST_TIME,
            "Это время повторяется из-за перевода часов. Выбери другое время.",
        )

    scheduled_utc = candidates[0].astimezone(timezone.utc)
    if scheduled_utc <= now_utc:
        raise ValidationError(
            ClarificationType.PAST_DATETIME,
            "Это время уже прошло. Уточни будущую дату и время.",
        )
    return scheduled_utc
