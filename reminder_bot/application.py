from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Protocol
from zoneinfo import ZoneInfo

from reminder_bot.clock import Clock
from reminder_bot.constants import (
    MOSCOW_TIMEZONE,
    PENDING_CONFIRMATION,
    PENDING_MANUAL_DATE,
    PENDING_MANUAL_MENU,
    PENDING_MANUAL_TEXT,
    PENDING_MANUAL_TIME,
)
from reminder_bot.domain import ClarificationType, ParseResult, PendingRequest, UnsupportedInput
from reminder_bot.parsers.base import ReminderParser
from reminder_bot.repository import Repository
from reminder_bot.service import ReminderService
from reminder_bot.validation import ValidationError


MAIN_KEYBOARD = {
    "keyboard": [[{"text": "📋 Напоминания"}]],
    "resize_keyboard": True,
}

HELP_TEXT = """Я создаю напоминания из обычного текста.

Примеры:
• концерт завтра в 7 вечера
• снять стирку 15/07 в 18:35
• позвонить маме через 2 часа
• встреча в пятницу в 15:00

Если время неоднозначно, я обязательно уточню.
Все даты и время — по Москве.
Перед созданием задачи я попрошу подтверждение.
Кнопка «Изменить» открывает ручное редактирование текста, даты и времени.
Отмена текущего уточнения: /cancel"""


class BotTransport(Protocol):
    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None): ...

    def answer_callback(self, callback_query_id: str, text: str | None = None): ...

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ): ...


class BotApplication:
    def __init__(
        self,
        repository: Repository,
        parser: ReminderParser,
        service: ReminderService,
        transport: BotTransport,
        clock: Clock,
    ):
        self.repository = repository
        self.parser = parser
        self.service = service
        self.transport = transport
        self.clock = clock

    def handle_update(self, update: dict) -> None:
        callback = update.get("callback_query")
        if callback:
            self._handle_callback(callback)
            return
        message = update.get("message")
        if message and isinstance(message.get("text"), str):
            self._handle_message(message)

    def _handle_message(self, message: dict) -> None:
        user_id = int(message["from"]["id"])
        chat_id = int(message["chat"]["id"])
        text = message["text"].strip()
        now_utc = self.clock.now_utc()

        if text.startswith("/start"):
            self._send(chat_id, "Привет! Пришли текст напоминания.\n\n" + HELP_TEXT)
            return

        if text.startswith("/help"):
            self._send(chat_id, HELP_TEXT)
            return
        if text.startswith("/cancel"):
            self.repository.clear_pending(user_id)
            self._send(chat_id, "Текущее уточнение отменено.")
            return
        if text.startswith("/timezone"):
            self._send(chat_id, "Часовой пояс фиксирован: МСК.")
            return

        if text.startswith("/reminders") or text == "📋 Напоминания":
            self._show_reminders(user_id, chat_id)
            return

        pending = self.repository.get_pending(user_id)
        if pending:
            if pending.clarification_type in {
                PENDING_MANUAL_TEXT,
                PENDING_MANUAL_DATE,
                PENDING_MANUAL_TIME,
            }:
                self._apply_manual_value(pending, text, now_utc)
            elif pending.clarification_type == PENDING_MANUAL_MENU:
                self._send(chat_id, "Выбери поле кнопкой в меню ручного редактирования.")
            else:
                self._continue_pending(pending, text, now_utc)
            return

        self._process_new_request(user_id, chat_id, text, now_utc)

    def _process_new_request(
        self,
        user_id: int,
        chat_id: int,
        text: str,
        now_utc: datetime,
    ) -> None:
        local_now = now_utc.astimezone(ZoneInfo(MOSCOW_TIMEZONE))
        try:
            result = self.parser.parse(text, local_now, MOSCOW_TIMEZONE)
        except UnsupportedInput:
            self._send(
                chat_id,
                "Не удалось понять дату и время. Например: «концерт завтра в 19:00».",
            )
            return
        except RuntimeError:
            self._send(
                chat_id,
                "Сейчас не удалось разобрать запрос. Попробуй еще раз чуть позже.",
            )
            return

        if result.needs_clarification:
            self._request_clarification(user_id, chat_id, text, result, now_utc)
            return
        self._request_confirmation(user_id, chat_id, text, result, now_utc)

    def _handle_callback(self, callback: dict) -> None:
        callback_id = callback["id"]
        user_id = int(callback["from"]["id"])
        message = callback.get("message", {})
        chat_id = int(message.get("chat", {}).get("id", user_id))
        message_id = message.get("message_id")
        data = callback.get("data", "")
        now_utc = self.clock.now_utc()

        if data.startswith("delete:"):
            try:
                reminder_id = int(data.split(":", 1)[1])
            except ValueError:
                self.transport.answer_callback(callback_id, "Некорректная команда")
                return
            deleted = self.repository.cancel_reminder(reminder_id, user_id, now_utc)
            self.transport.answer_callback(
                callback_id,
                "Напоминание удалено" if deleted else "Напоминание уже удалено",
            )
            if message_id is not None:
                self._refresh_reminder_list_message(user_id, chat_id, int(message_id))
            elif deleted:
                self._show_reminders(user_id, chat_id)
            return

        if data.startswith("time:"):
            value = data.split(":", 1)[1]
            pending = self.repository.get_pending(user_id)
            self.transport.answer_callback(callback_id)
            if pending:
                self._continue_pending(pending, value, now_utc)
            else:
                self._send(chat_id, "Уточнение уже неактуально.")
            return

        if data.startswith("confirm:"):
            action = data.split(":", 1)[1]
            pending = self.repository.get_pending(user_id)
            self.transport.answer_callback(callback_id)
            if not pending or pending.clarification_type != PENDING_CONFIRMATION:
                self._send(chat_id, "Это подтверждение уже неактуально.")
                return

            if action == "yes":
                self.repository.clear_pending(user_id)
                self._create_and_confirm(
                    user_id,
                    pending.chat_id,
                    pending.source_text,
                    pending.parse_result,
                    now_utc,
                )
                return
            if action == "no":
                self.repository.clear_pending(user_id)
                self._send(pending.chat_id, "❌ Задача отменена.")
                return
            if action == "edit":
                edited = self._pending_with_type(
                    pending,
                    PENDING_MANUAL_MENU,
                    now_utc,
                    message_id=int(message_id) if message_id is not None else None,
                )
                self.repository.save_pending(edited)
                self._show_manual_menu(
                    edited,
                    message_id=int(message_id) if message_id is not None else None,
                )
                return
            self._send(chat_id, "Неизвестный вариант подтверждения.")
            return

        if data.startswith("manual:"):
            action = data.split(":", 1)[1]
            pending = self.repository.get_pending(user_id)
            self.transport.answer_callback(callback_id)
            if not pending or pending.clarification_type not in {
                PENDING_MANUAL_MENU,
                PENDING_MANUAL_TEXT,
                PENDING_MANUAL_DATE,
                PENDING_MANUAL_TIME,
            }:
                self._send(chat_id, "Ручное редактирование уже завершено.")
                return

            if action == "cancel":
                self.repository.clear_pending(user_id)
                self._send(pending.chat_id, "❌ Задача отменена.")
                return
            if action == "done":
                self._request_confirmation(
                    user_id,
                    pending.chat_id,
                    pending.source_text,
                    pending.parse_result,
                    now_utc,
                    message_id=int(message_id) if message_id is not None else None,
                )
                return
            if action == "back":
                menu_pending = self._pending_with_type(
                    pending,
                    PENDING_MANUAL_MENU,
                    now_utc,
                    message_id=int(message_id) if message_id is not None else None,
                )
                self.repository.save_pending(menu_pending)
                self._show_manual_menu(
                    menu_pending,
                    message_id=int(message_id) if message_id is not None else None,
                )
                return

            prompts = {
                "text": (
                    PENDING_MANUAL_TEXT,
                    "Пришли новый текст задачи одним сообщением.",
                ),
                "date": (
                    PENDING_MANUAL_DATE,
                    "Пришли дату строго в формате ДД.ММ.ГГГГ, например 25.07.2026.",
                ),
                "time": (
                    PENDING_MANUAL_TIME,
                    "Пришли время строго в формате ЧЧ:ММ, например 09:30. Это будет точное время МСК.",
                ),
            }
            if action in prompts:
                pending_type, prompt = prompts[action]
                field_pending = self._pending_with_type(
                    pending,
                    pending_type,
                    now_utc,
                    message_id=int(message_id) if message_id is not None else None,
                )
                self.repository.save_pending(field_pending)
                back_keyboard = {
                    "inline_keyboard": [[
                        {"text": "↩️ Назад", "callback_data": "manual:back"}
                    ]]
                }
                self._edit_or_send(
                    pending.chat_id,
                    int(message_id) if message_id is not None else None,
                    prompt,
                    back_keyboard,
                )
                return

            self._send(chat_id, "Неизвестный пункт ручного редактирования.")
            return

        self.transport.answer_callback(callback_id, "Неизвестная команда")

    def _request_clarification(
        self,
        user_id: int,
        chat_id: int,
        source_text: str,
        result: ParseResult,
        now: datetime,
    ) -> None:
        clarification = result.clarification_type or ClarificationType.UNRECOGNIZED
        if clarification == ClarificationType.INVALID_DATE:
            self._send(chat_id, "Такой даты не существует. Уточни дату.")
            return
        if clarification == ClarificationType.PAST_DATETIME:
            self._send(chat_id, "Это время уже прошло. Уточни будущую дату и время.")
            return

        pending = PendingRequest(
            telegram_user_id=user_id,
            chat_id=chat_id,
            source_text=source_text,
            parse_result=result,
            clarification_type=clarification,
            created_at=now,
        )
        self.repository.save_pending(pending)

        if clarification == ClarificationType.AMBIGUOUS_TIME:
            options = result.clarification_options or []
            if len(options) >= 2:
                keyboard = {
                    "inline_keyboard": [[
                        {"text": option, "callback_data": f"time:{option}"} for option in options
                    ]]
                }
                self.transport.send_message(
                    chat_id,
                    f"Ты имеешь в виду {options[0]} или {options[1]}?",
                    reply_markup=keyboard,
                )
                return
        self._send(chat_id, "Во сколько?")

    def _continue_pending(
        self,
        pending: PendingRequest,
        answer: str,
        now_utc: datetime,
    ) -> None:
        local_now = now_utc.astimezone(ZoneInfo(MOSCOW_TIMEZONE))
        result = pending.parse_result

        if pending.clarification_type == PENDING_CONFIRMATION:
            self._send(pending.chat_id, "Подтверди задачу кнопкой: Да, Нет или Изменить.")
            return

        if pending.clarification_type == ClarificationType.AMBIGUOUS_TIME:
            normalized = self._normalize_time_answer(answer)
            if normalized not in (result.clarification_options or []):
                options = result.clarification_options or []
                self._send(
                    pending.chat_id,
                    "Выбери один из вариантов: " + " или ".join(options) + ".",
                )
                return
            result.time = normalized
            result.time_confidence = "high"
            result.needs_clarification = False
            result.clarification_type = None
            result.clarification_options = []
            if not result.date or not result.date_was_explicit:
                chosen = datetime.strptime(normalized, "%H:%M").time()
                target = datetime.combine(local_now.date(), chosen, tzinfo=local_now.tzinfo)
                if target <= local_now:
                    target += timedelta(days=1)
                result.date = target.date().isoformat()
        elif pending.clarification_type == ClarificationType.MISSING_TIME:
            try:
                answer_result = self.parser.parse(
                    f"напомни в {answer}", local_now, MOSCOW_TIMEZONE
                )
            except (UnsupportedInput, RuntimeError):
                self._send(pending.chat_id, "Не удалось понять время. Например: 19:00.")
                return
            if answer_result.needs_clarification:
                result.clarification_type = answer_result.clarification_type
                result.clarification_options = answer_result.clarification_options
                result.needs_clarification = True
                updated = PendingRequest(
                    telegram_user_id=pending.telegram_user_id,
                    chat_id=pending.chat_id,
                    source_text=pending.source_text,
                    parse_result=result,
                    clarification_type=answer_result.clarification_type or ClarificationType.UNRECOGNIZED,
                    created_at=now_utc,
                )
                self.repository.save_pending(updated)
                self._request_clarification(
                    pending.telegram_user_id,
                    pending.chat_id,
                    pending.source_text,
                    result,
                    now_utc,
                )
                return
            result.time = answer_result.time
            result.time_confidence = "high"
            result.time_was_explicit = True
            result.needs_clarification = False
            result.clarification_type = None
            if not result.date:
                result.date = answer_result.date
        else:
            self.repository.clear_pending(pending.telegram_user_id)
            self._send(pending.chat_id, "Отправь запрос заново с уточненной датой и временем.")
            return

        self._move_yearless_date_to_future(result, pending.source_text, local_now)
        self.repository.clear_pending(pending.telegram_user_id)
        self._request_confirmation(
            pending.telegram_user_id,
            pending.chat_id,
            pending.source_text,
            result,
            now_utc,
        )

    def _apply_manual_value(
        self,
        pending: PendingRequest,
        answer: str,
        now_utc: datetime,
    ) -> None:
        result = pending.parse_result
        source_text = pending.source_text

        if pending.clarification_type == PENDING_MANUAL_TEXT:
            value = answer.strip()
            if not value:
                self._send(pending.chat_id, "Текст задачи не может быть пустым.")
                return
            result.title = value
            source_text = value
        elif pending.clarification_type == PENDING_MANUAL_DATE:
            if not re.fullmatch(r"\d{2}\.\d{2}\.\d{4}", answer):
                self._send(
                    pending.chat_id,
                    "Нужен точный формат ДД.ММ.ГГГГ, например 25.07.2026.",
                )
                return
            try:
                manual_date = datetime.strptime(answer, "%d.%m.%Y").date()
            except ValueError:
                self._send(pending.chat_id, "Такой даты не существует. Введи другую дату.")
                return
            result.date = manual_date.isoformat()
            result.date_confidence = "high"
            result.date_was_explicit = True
        elif pending.clarification_type == PENDING_MANUAL_TIME:
            if not re.fullmatch(r"\d{2}:\d{2}", answer):
                self._send(
                    pending.chat_id,
                    "Нужен точный формат ЧЧ:ММ, например 09:30.",
                )
                return
            try:
                manual_time = datetime.strptime(answer, "%H:%M").time()
            except ValueError:
                self._send(pending.chat_id, "Такого времени не существует. Введи другое время.")
                return
            result.time = manual_time.strftime("%H:%M")
            result.time_confidence = "high"
            result.time_was_explicit = True
        else:
            self._send(pending.chat_id, "Неизвестное поле ручного редактирования.")
            return

        result.needs_clarification = False
        result.clarification_type = None
        result.clarification_options = []
        menu_pending = PendingRequest(
            telegram_user_id=pending.telegram_user_id,
            chat_id=pending.chat_id,
            source_text=source_text,
            parse_result=result,
            clarification_type=PENDING_MANUAL_MENU,
            created_at=now_utc,
            message_id=pending.message_id,
        )
        self.repository.save_pending(menu_pending)
        self._show_manual_menu(menu_pending, message_id=pending.message_id)

    def _show_manual_menu(
        self,
        pending: PendingRequest,
        message_id: int | None = None,
    ) -> None:
        result = pending.parse_result
        try:
            date_label = datetime.strptime(result.date or "", "%Y-%m-%d").strftime("%d.%m.%Y")
        except ValueError:
            date_label = "не задана"
        time_label = result.time or "не задано"
        text = (
            "✏️ Ручное редактирование\n\n"
            f"Текст: {self.service.normalize_title(result.title)}\n"
            f"Дата: {date_label}\n"
            f"Время: {time_label} (МСК)"
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "📝 Текст", "callback_data": "manual:text"},
                    {"text": "📅 Дата", "callback_data": "manual:date"},
                    {"text": "🕒 Время", "callback_data": "manual:time"},
                ],
                [
                    {"text": "✅ Готово", "callback_data": "manual:done"},
                    {"text": "❌ Отмена", "callback_data": "manual:cancel"},
                ],
            ]
        }
        effective_message_id = message_id if message_id is not None else pending.message_id
        self._edit_or_send(pending.chat_id, effective_message_id, text, keyboard)

    @staticmethod
    def _pending_with_type(
        pending: PendingRequest,
        pending_type: str,
        now: datetime,
        message_id: int | None = None,
    ) -> PendingRequest:
        return PendingRequest(
            telegram_user_id=pending.telegram_user_id,
            chat_id=pending.chat_id,
            source_text=pending.source_text,
            parse_result=pending.parse_result,
            clarification_type=pending_type,
            created_at=now,
            message_id=message_id if message_id is not None else pending.message_id,
        )

    def _edit_or_send(
        self,
        chat_id: int,
        message_id: int | None,
        text: str,
        reply_markup: dict,
    ) -> None:
        if message_id is None:
            self.transport.send_message(chat_id, text, reply_markup=reply_markup)
            return
        self.transport.edit_message_text(
            chat_id,
            message_id,
            text,
            reply_markup=reply_markup,
        )

    def _request_confirmation(
        self,
        user_id: int,
        chat_id: int,
        source_text: str,
        result: ParseResult,
        now: datetime,
        message_id: int | None = None,
    ) -> None:
        try:
            preview = self.service.format_preview(result, now)
        except ValidationError as error:
            self._send(chat_id, error.user_message)
            return

        self.repository.save_pending(
            PendingRequest(
                telegram_user_id=user_id,
                chat_id=chat_id,
                source_text=source_text,
                parse_result=result,
                clarification_type=PENDING_CONFIRMATION,
                created_at=now,
                message_id=message_id,
            )
        )
        keyboard = {
            "inline_keyboard": [[
                {"text": "✅ Да", "callback_data": "confirm:yes"},
                {"text": "❌ Нет", "callback_data": "confirm:no"},
                {"text": "✏️ Изменить", "callback_data": "confirm:edit"},
            ]]
        }
        self._edit_or_send(
            chat_id,
            message_id,
            preview,
            keyboard,
        )

    def _create_and_confirm(
        self,
        user_id: int,
        chat_id: int,
        source_text: str,
        result: ParseResult,
        now: datetime,
    ) -> None:
        try:
            reminder = self.service.create(
                user_id, chat_id, source_text, result, now
            )
        except ValidationError as error:
            self._send(chat_id, error.user_message)
            return
        self._send(chat_id, self.service.format_confirmation(reminder))

    def _show_reminders(self, user_id: int, chat_id: int) -> None:
        reminders = self.repository.list_active(user_id)
        markup = self.service.delete_keyboard(reminders) if reminders else MAIN_KEYBOARD
        self.transport.send_message(
            chat_id,
            self.service.format_list(reminders),
            reply_markup=markup,
        )

    def _refresh_reminder_list_message(
        self,
        user_id: int,
        chat_id: int,
        message_id: int,
    ) -> None:
        reminders = self.repository.list_active(user_id)
        markup = (
            self.service.delete_keyboard(reminders)
            if reminders
            else {"inline_keyboard": []}
        )
        self.transport.edit_message_text(
            chat_id,
            message_id,
            self.service.format_list(reminders),
            reply_markup=markup,
        )

    def send_due_reminders(self) -> int:
        now = self.clock.now_utc()
        sent = 0
        for reminder in self.repository.due_reminders(now):
            try:
                self.transport.send_message(reminder.chat_id, f"⏰ {reminder.title}")
            except RuntimeError:
                # Оставляем active: следующая итерация повторит доставку.
                continue
            if self.repository.delete_after_delivery(reminder.id):
                sent += 1
        return sent

    def _send(self, chat_id: int, text: str) -> None:
        self.transport.send_message(chat_id, text, reply_markup=MAIN_KEYBOARD)

    @staticmethod
    def _normalize_time_answer(value: str) -> str:
        match = re.fullmatch(r"\s*(\d{1,2})[:.](\d{2})\s*", value)
        if not match:
            return value.strip()
        return f"{int(match.group(1)):02d}:{int(match.group(2)):02d}"

    @staticmethod
    def _move_yearless_date_to_future(
        result: ParseResult,
        source_text: str,
        local_now: datetime,
    ) -> None:
        if not result.date or not result.time or re.search(r"\b\d{4}\b", source_text):
            return
        has_numeric = re.search(r"(?<!\d)\d{1,2}[./-]\d{1,2}(?![./-]\d)", source_text)
        month_names = (
            "январ", "феврал", "март", "апрел", "мая", "май", "июн", "июл",
            "август", "сентябр", "октябр", "ноябр", "декабр",
        )
        if not has_numeric and not any(name in source_text.lower() for name in month_names):
            return
        try:
            parsed_date = date.fromisoformat(result.date)
            parsed_time = datetime.strptime(result.time, "%H:%M").time()
        except ValueError:
            return
        target = datetime.combine(parsed_date, parsed_time, tzinfo=local_now.tzinfo)
        if target > local_now:
            return
        for year in range(parsed_date.year + 1, parsed_date.year + 9):
            try:
                candidate = parsed_date.replace(year=year)
            except ValueError:
                continue
            result.date = candidate.isoformat()
            return
