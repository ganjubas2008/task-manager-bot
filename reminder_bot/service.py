from __future__ import annotations

from datetime import datetime, timedelta, timezone
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

    def update(
        self,
        reminder_id: int,
        telegram_user_id: int,
        source_text: str,
        result: ParseResult,
        now: datetime,
    ) -> Reminder | None:
        scheduled_at_utc = validate_and_build_datetime(result, now, MOSCOW_TIMEZONE)
        return self.repository.update_reminder(
            reminder_id=reminder_id,
            telegram_user_id=telegram_user_id,
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
    def format_notification(reminder: Reminder) -> str:
        if reminder.notification_count == 0:
            return f"⏰ {reminder.title}\n\nЕсли уже сделал — отметь одним нажатием."
        return f"⏰ {reminder.title}\n\nКак дела с этой задачей?"

    @staticmethod
    def notification_keyboard(reminder: Reminder) -> dict:
        rows = [[
            {
                "text": "✅ Сделано",
                "callback_data": f"task:completed:{reminder.id}",
            },
            {
                "text": "⏰ Позже",
                "callback_data": f"snooze:menu:{reminder.id}",
            },
        ]]
        if reminder.notification_count > 0:
            rows.append(
                [
                    {
                        "text": "🚫 Неактуально",
                        "callback_data": f"task:irrelevant:{reminder.id}",
                    }
                ]
            )
        return {"inline_keyboard": rows}

    @staticmethod
    def created_task_keyboard(reminder: Reminder) -> dict:
        return {
            "inline_keyboard": [[
                {
                    "text": "↩️ Отменить",
                    "callback_data": f"created:undo:{reminder.id}",
                },
                {
                    "text": "✏️ Изменить",
                    "callback_data": f"created:edit:{reminder.id}",
                },
            ]]
        }

    @staticmethod
    def snooze_keyboard(reminder: Reminder) -> dict:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "15 минут",
                        "callback_data": f"snooze:15:{reminder.id}",
                    },
                    {
                        "text": "1 час",
                        "callback_data": f"snooze:60:{reminder.id}",
                    },
                ],
                [
                    {
                        "text": "Завтра",
                        "callback_data": f"snooze:tomorrow:{reminder.id}",
                    },
                    {
                        "text": "↩️ Назад",
                        "callback_data": f"snooze:back:{reminder.id}",
                    },
                ],
            ]
        }

    @staticmethod
    def format_daily_review(reminders: list[Reminder]) -> str:
        lines = ["🌅 Доброе утро! Что с задачами со вчера?", ""]
        for index, reminder in enumerate(reminders, 1):
            title = reminder.title
            if len(title) > 120:
                title = title[:117].rstrip() + "…"
            lines.append(f"{index}. {title}")
        lines.extend(["", "✅ сделано · ⏳ ещё нет · 🚫 неактуально"])
        return "\n".join(lines)

    @staticmethod
    def daily_review_keyboard(reminders: list[Reminder], review_date: str) -> dict:
        compact_date = review_date.replace("-", "")
        return {
            "inline_keyboard": [
                [
                    {
                        "text": f"✅ {index}",
                        "callback_data": (
                            f"review:completed:{reminder.id}:{compact_date}"
                        ),
                    },
                    {
                        "text": f"⏳ {index}",
                        "callback_data": f"review:not_done:{reminder.id}:{compact_date}",
                    },
                    {
                        "text": f"🚫 {index}",
                        "callback_data": (
                            f"review:irrelevant:{reminder.id}:{compact_date}"
                        ),
                    },
                ]
                for index, reminder in enumerate(reminders, 1)
            ]
        }

    @staticmethod
    def next_daily_delivery(reminder: Reminder, now: datetime) -> datetime:
        zone = ZoneInfo(reminder.user_timezone)
        anchor = reminder.original_scheduled_at_utc or reminder.scheduled_at_utc
        local_scheduled = anchor.astimezone(zone)
        local_now = now.astimezone(zone)
        # Даже после простоя отправляем не больше одного уведомления за местный
        # календарный день: следующее будет завтра в исходное время.
        next_date = max(
            local_scheduled.date() + timedelta(days=1),
            local_now.date() + timedelta(days=1),
        )
        while True:
            candidate = datetime.combine(
                next_date,
                local_scheduled.time(),
                tzinfo=zone,
            )
            if candidate > local_now:
                return candidate.astimezone(timezone.utc)
            next_date += timedelta(days=1)

    @classmethod
    def snooze_target(
        cls,
        reminder: Reminder,
        choice: str,
        now: datetime,
    ) -> datetime:
        if choice == "15":
            return now + timedelta(minutes=15)
        if choice == "60":
            return now + timedelta(hours=1)
        if choice == "tomorrow":
            return cls.next_daily_delivery(reminder, now)
        raise ValueError(f"Неизвестный вариант отсрочки: {choice}")

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
