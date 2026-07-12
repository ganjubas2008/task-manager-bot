from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from reminder_bot.constants import MOSCOW_LABEL, MOSCOW_TIMEZONE
from reminder_bot.domain import ParseResult, Reminder
from reminder_bot.repository import Repository
from reminder_bot.validation import validate_and_build_datetime


class ReminderService:
    def __init__(self, repository: Repository):
        self.repository = repository

    def create(
        self,
        telegram_user_id: int,
        chat_id: int,
        source_text: str,
        result: ParseResult,
        now: datetime,
    ) -> Reminder:
        scheduled_at_utc = validate_and_build_datetime(result, now, MOSCOW_TIMEZONE)
        return self.repository.create_reminder(
            telegram_user_id=telegram_user_id,
            chat_id=chat_id,
            title=self.normalize_title(result.title),
            source_text=source_text,
            scheduled_at_utc=scheduled_at_utc,
            user_timezone=MOSCOW_TIMEZONE,
            parser_type=result.parser_type,
            now=now,
        )

    @staticmethod
    def format_confirmation(reminder: Reminder) -> str:
        local = reminder.scheduled_at_utc.astimezone(ZoneInfo(MOSCOW_TIMEZONE))
        return (
            f"✅ Задача создана: {reminder.title}\n"
            f"Срок: {local:%d.%m.%Y %H:%M} ({MOSCOW_LABEL})"
        )

    @classmethod
    def format_preview(
        cls,
        result: ParseResult,
        now: datetime,
    ) -> str:
        scheduled_at_utc = validate_and_build_datetime(result, now, MOSCOW_TIMEZONE)
        local = scheduled_at_utc.astimezone(ZoneInfo(MOSCOW_TIMEZONE))
        return (
            f"Создать задачу: {cls.normalize_title(result.title)}\n"
            f"Срок: {local:%d.%m.%Y %H:%M} ({MOSCOW_LABEL})"
        )

    @staticmethod
    def format_list(reminders: list[Reminder]) -> str:
        if not reminders:
            return "Активных напоминаний пока нет."
        lines = ["📋 Напоминания", ""]
        for index, reminder in enumerate(reminders, 1):
            local = reminder.scheduled_at_utc.astimezone(ZoneInfo(MOSCOW_TIMEZONE))
            lines.append(
                f"{index}. {local:%d.%m.%Y %H:%M} ({MOSCOW_LABEL}) — {reminder.title}"
            )
        return "\n".join(lines)

    @staticmethod
    def normalize_title(title: str) -> str:
        value = title.strip()
        return value[:1].upper() + value[1:] if value else "Задача"

    @staticmethod
    def delete_keyboard(reminders: list[Reminder]) -> dict:
        return {
            "inline_keyboard": [
                [{"text": f"🗑 Удалить №{index}", "callback_data": f"delete:{item.id}"}]
                for index, item in enumerate(reminders, 1)
            ]
        }
