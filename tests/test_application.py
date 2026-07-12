from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from reminder_bot.application import BotApplication
from reminder_bot.clock import FixedClock
from reminder_bot.constants import (
    MOSCOW_TIMEZONE,
    PENDING_MANUAL_DATE,
    PENDING_MANUAL_MENU,
    PENDING_MANUAL_TEXT,
    PENDING_MANUAL_TIME,
)
from reminder_bot.parsers.rule_based import RuleBasedParser
from reminder_bot.repository import Repository
from reminder_bot.service import ReminderService


class FakeTransport:
    def __init__(self):
        self.messages = []
        self.callbacks = []
        self.edits = []
        self.fail = False

    def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise RuntimeError("сеть недоступна")
        message = {"chat_id": chat_id, "text": text, "reply_markup": reply_markup}
        self.messages.append(message)
        return message

    def answer_callback(self, callback_query_id, text=None):
        self.callbacks.append((callback_query_id, text))
        return True

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        edit = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": reply_markup,
        }
        self.edits.append(edit)
        return edit


class CountingParser:
    def __init__(self):
        self.delegate = RuleBasedParser()
        self.calls = 0

    def parse(self, text, now, timezone_name):
        self.calls += 1
        return self.delegate.parse(text, now, timezone_name)


def message_update(text, user_id=1, chat_id=100, update_id=1):
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {"id": user_id},
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def callback_update(data, user_id=1, chat_id=100, update_id=2, message_id=500):
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": user_id},
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
            "data": data,
        },
    }


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.repository = Repository(":memory:")
        self.repository.migrate()
        self.transport = FakeTransport()
        # 12:00 UTC = 15:00 МСК.
        self.clock = FixedClock(datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc))
        self.parser = CountingParser()
        self.app = BotApplication(
            self.repository,
            self.parser,
            ReminderService(self.repository),
            self.transport,
            self.clock,
        )

    def tearDown(self):
        self.repository.close()

    def submit(self, text="концерт завтра в 19:00", update_id=1):
        self.app.handle_update(message_update(text, update_id=update_id))

    def create_confirmed(self, text="концерт завтра в 19:00"):
        self.submit(text)
        return self.repository.list_active(1)[0]

    def test_first_run_uses_moscow_without_onboarding_or_timezone_button(self):
        self.app.handle_update(message_update("/start"))
        response = self.transport.messages[-1]
        self.assertIn("Все даты и время — по Москве", response["text"])
        self.assertNotIn("В каком часовом поясе", response["text"])
        buttons = response["reply_markup"]["keyboard"]
        self.assertEqual([[{"text": "📋 Напоминания"}]], buttons)

        self.app.handle_update(message_update("/timezone", update_id=2))
        self.assertEqual("Часовой пояс фиксирован: МСК.", self.transport.messages[-1]["text"])

    def test_clear_task_is_created_immediately_with_undo_and_edit(self):
        self.submit()
        reminder = self.repository.list_active(1)[0]
        self.assertIsNone(self.repository.get_pending(1))
        self.assertEqual(
            "✅ Задача создана: Концерт\nСрок: 13.07.2026 19:00 (МСК)",
            self.transport.messages[-1]["text"],
        )
        keyboard = self.transport.messages[-1]["reply_markup"]["inline_keyboard"][0]
        self.assertEqual(
            [f"created:undo:{reminder.id}", f"created:edit:{reminder.id}"],
            [button["callback_data"] for button in keyboard],
        )

    def test_automatic_creation_uses_requested_message_format(self):
        self.submit("смотреть видео на итальянском завтра в 19:20")
        reminders = self.repository.list_active(1)
        self.assertEqual(1, len(reminders))
        self.assertEqual(MOSCOW_TIMEZONE, reminders[0].user_timezone)
        self.assertEqual(
            "✅ Задача создана: Смотреть видео на итальянском\n"
            "Срок: 13.07.2026 19:20 (МСК)",
            self.transport.messages[-1]["text"],
        )
        self.assertIsNone(self.repository.get_pending(1))

    def test_undo_removes_automatically_created_task(self):
        self.submit()
        reminder = self.repository.list_active(1)[0]
        self.app.handle_update(
            callback_update(f"created:undo:{reminder.id}", update_id=2)
        )
        self.assertEqual([], self.repository.list_active(1))
        self.assertIsNone(self.repository.get_pending(1))
        self.assertEqual("↩️ Отменено: Концерт", self.transport.edits[-1]["text"])
        self.assertEqual("Создание отменено", self.transport.callbacks[-1][1])

    def test_manual_edit_changes_fields_separately_without_parsers(self):
        self.submit("концерт завтра в 19:00", update_id=1)
        self.assertEqual(1, self.parser.calls)
        reminder = self.repository.list_active(1)[0]
        self.app.handle_update(
            callback_update(f"created:edit:{reminder.id}", update_id=2)
        )
        self.assertEqual(PENDING_MANUAL_MENU, self.repository.get_pending(1).clarification_type)
        self.assertEqual(reminder.id, self.repository.get_pending(1).reminder_id)
        self.assertIn("✏️ Ручное редактирование", self.transport.edits[-1]["text"])
        callbacks = [
            button["callback_data"]
            for row in self.transport.edits[-1]["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(
            ["manual:text", "manual:date", "manual:time", "manual:done", "manual:cancel"],
            callbacks,
        )

        self.app.handle_update(callback_update("manual:text", update_id=3))
        self.assertEqual(PENDING_MANUAL_TEXT, self.repository.get_pending(1).clarification_type)
        self.submit("Смотреть видео на итальянском", update_id=4)
        self.assertEqual(PENDING_MANUAL_MENU, self.repository.get_pending(1).clarification_type)

        self.app.handle_update(callback_update("manual:date", update_id=5))
        self.assertEqual(PENDING_MANUAL_DATE, self.repository.get_pending(1).clarification_type)
        self.submit("25.07.2026", update_id=6)

        self.app.handle_update(callback_update("manual:time", update_id=7))
        self.assertEqual(PENDING_MANUAL_TIME, self.repository.get_pending(1).clarification_type)
        # 07:05 — точное ручное значение, поэтому уточнения «утро/вечер» нет.
        self.submit("07:05", update_id=8)
        self.assertEqual(1, self.parser.calls)
        self.assertIn("Время: 07:05 (МСК)", self.transport.edits[-1]["text"])

        self.app.handle_update(callback_update("manual:done", update_id=9))
        self.assertIsNone(self.repository.get_pending(1))
        self.assertIn(
            "✅ Задача создана: Смотреть видео на итальянском\n"
            "Срок: 25.07.2026 07:05 (МСК)",
            self.transport.edits[-1]["text"],
        )
        reminder = self.repository.list_active(1)[0]
        self.assertEqual("Смотреть видео на итальянском", reminder.title)
        self.assertEqual(1, self.parser.calls)

    def test_manual_date_and_time_require_exact_valid_formats(self):
        self.submit()
        reminder = self.repository.list_active(1)[0]
        self.app.handle_update(
            callback_update(f"created:edit:{reminder.id}", update_id=2)
        )
        self.app.handle_update(callback_update("manual:date", update_id=3))
        self.submit("31.02.2027", update_id=4)
        self.assertEqual(PENDING_MANUAL_DATE, self.repository.get_pending(1).clarification_type)
        self.assertIn("не существует", self.transport.messages[-1]["text"])
        self.submit("01.08.2027", update_id=5)

        self.app.handle_update(callback_update("manual:time", update_id=6))
        self.submit("7 вечера", update_id=7)
        self.assertEqual(PENDING_MANUAL_TIME, self.repository.get_pending(1).clarification_type)
        self.assertIn("формат ЧЧ:ММ", self.transport.messages[-1]["text"])
        self.submit("19:00", update_id=8)
        self.assertEqual(PENDING_MANUAL_MENU, self.repository.get_pending(1).clarification_type)

    def test_cancel_edit_keeps_the_automatically_created_task_unchanged(self):
        self.submit("концерт завтра в 19:00")
        reminder = self.repository.list_active(1)[0]
        self.app.handle_update(
            callback_update(f"created:edit:{reminder.id}", update_id=2)
        )
        self.app.handle_update(callback_update("manual:text", update_id=3))
        self.submit("Совсем другой текст", update_id=4)
        self.app.handle_update(callback_update("manual:cancel", update_id=5))

        unchanged = self.repository.get_reminder(reminder.id)
        self.assertEqual("Концерт", unchanged.title)
        self.assertIsNone(self.repository.get_pending(1))
        self.assertEqual(
            f"created:edit:{reminder.id}",
            self.transport.edits[-1]["reply_markup"]["inline_keyboard"][0][1][
                "callback_data"
            ],
        )

    def test_ambiguous_time_is_clarified_then_created_automatically(self):
        self.submit("снять стирку 15/07 в 6:35")
        self.assertEqual([], self.repository.list_active(1))
        keyboard = self.transport.messages[-1]["reply_markup"]
        self.assertEqual("time:06:35", keyboard["inline_keyboard"][0][0]["callback_data"])

        self.app.handle_update(callback_update("time:18:35", update_id=2))
        self.assertEqual(1, len(self.repository.list_active(1)))
        self.assertIsNone(self.repository.get_pending(1))
        self.assertIn("Срок: 15.07.2026 18:35 (МСК)", self.transport.messages[-1]["text"])

    def test_missing_time_is_clarified_then_created_automatically(self):
        self.submit("завтра позвонить врачу")
        self.assertEqual("Во сколько?", self.transport.messages[-1]["text"])
        self.submit("19:00", update_id=2)
        self.assertIn("✅ Задача создана: Позвонить врачу", self.transport.messages[-1]["text"])
        self.assertEqual("Позвонить врачу", self.repository.list_active(1)[0].title)

    def test_yearless_date_rolls_forward_before_automatic_creation(self):
        self.submit("12/07 позвонить врачу")
        self.submit("14:00", update_id=2)
        self.assertIn("12.07.2027 14:00", self.transport.messages[-1]["text"])
        self.assertEqual(2027, self.repository.list_active(1)[0].scheduled_at_utc.year)

    def test_cancel_clears_any_pending_request(self):
        self.submit("завтра концерт")
        self.assertIsNotNone(self.repository.get_pending(1))
        self.submit("/cancel", update_id=2)
        self.assertIsNone(self.repository.get_pending(1))

    def test_list_and_manual_delete_are_scoped_to_user(self):
        reminder = self.create_confirmed()
        self.submit("📋 Напоминания", update_id=3)
        self.assertIn("13.07.2026 19:00 (МСК) — Концерт", self.transport.messages[-1]["text"])
        self.app.handle_update(callback_update(f"delete:{reminder.id}", update_id=4))
        self.assertEqual([], self.repository.list_active(1))
        self.assertEqual("Напоминание удалено", self.transport.callbacks[-1][1])
        self.assertEqual("Активных напоминаний пока нет.", self.transport.edits[-1]["text"])
        self.assertEqual(
            {"inline_keyboard": []},
            self.transport.edits[-1]["reply_markup"],
        )

        # Повторного действия в интерфейсе уже нет; даже запоздалый дубль callback безопасен.
        self.app.handle_update(callback_update(f"delete:{reminder.id}", update_id=5))
        self.assertEqual("Напоминание уже удалено", self.transport.callbacks[-1][1])

    def test_invalid_date_is_not_offered_for_confirmation(self):
        self.submit("встреча 31 февраля в 19:00")
        self.assertEqual([], self.repository.list_active(1))
        self.assertIsNone(self.repository.get_pending(1))
        self.assertIn("не существует", self.transport.messages[-1]["text"])

    def test_scheduler_sends_first_notification_and_reschedules_for_tomorrow(self):
        reminder = self.repository.create_reminder(
            1,
            100,
            "Проверить сервер",
            "исходный текст",
            self.clock.now_utc() - timedelta(seconds=1),
            MOSCOW_TIMEZONE,
            "test",
            self.clock.now_utc(),
        )
        sent = self.app.send_due_reminders()
        self.assertEqual(1, sent)
        message = self.transport.messages[-1]
        self.assertEqual(
            "⏰ Проверить сервер\n\nЕсли уже сделал — отметь одним нажатием.",
            message["text"],
        )
        self.assertEqual(
            [[
                {
                    "text": "✅ Сделано",
                    "callback_data": f"task:completed:{reminder.id}",
                },
                {
                    "text": "⏰ Позже",
                    "callback_data": f"snooze:menu:{reminder.id}",
                },
            ]],
            message["reply_markup"]["inline_keyboard"],
        )
        stored = self.repository.get_reminder(reminder.id)
        self.assertEqual("active", stored.status)
        self.assertEqual(1, stored.notification_count)
        self.assertGreater(stored.scheduled_at_utc, self.clock.now_utc())

    def test_completion_button_closes_task_and_records_completion(self):
        reminder = self.repository.create_reminder(
            1,
            100,
            "Позвонить маме",
            "исходный текст",
            self.clock.now_utc() - timedelta(seconds=1),
            MOSCOW_TIMEZONE,
            "test",
            self.clock.now_utc(),
        )
        self.app.send_due_reminders()
        self.app.handle_update(
            callback_update(f"task:completed:{reminder.id}", update_id=2)
        )
        completed = self.repository.get_reminder(reminder.id)
        self.assertEqual("completed", completed.status)
        self.assertEqual([], self.repository.list_active(1))
        self.assertEqual("✅ Сделано: Позвонить маме", self.transport.edits[-1]["text"])
        self.assertEqual(
            [("completed", "notification")],
            [
                (event.action, event.source)
                for event in self.repository.list_events(reminder.id)
            ],
        )

    def test_repeated_notification_offers_done_later_and_irrelevant(self):
        reminder = self.repository.create_reminder(
            1,
            100,
            "Проверить сервер",
            "исходный текст",
            self.clock.now_utc() - timedelta(seconds=1),
            MOSCOW_TIMEZONE,
            "test",
            self.clock.now_utc(),
        )
        self.app.send_due_reminders()
        next_delivery = self.repository.get_reminder(reminder.id).scheduled_at_utc
        self.clock.value = next_delivery + timedelta(seconds=1)
        self.app.send_due_reminders()

        message = self.transport.messages[-1]
        self.assertEqual(
            "⏰ Проверить сервер\n\nКак дела с этой задачей?",
            message["text"],
        )
        callbacks = [
            button["callback_data"]
            for row in message["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(
            [
                f"task:completed:{reminder.id}",
                f"snooze:menu:{reminder.id}",
                f"task:irrelevant:{reminder.id}",
            ],
            callbacks,
        )

    def test_later_reveals_snooze_choices_and_preserves_daily_time(self):
        original_due = self.clock.now_utc() - timedelta(seconds=1)
        reminder = self.repository.create_reminder(
            1,
            100,
            "Позвонить маме",
            "исходный текст",
            original_due,
            MOSCOW_TIMEZONE,
            "test",
            self.clock.now_utc(),
        )
        self.app.send_due_reminders()
        self.app.handle_update(
            callback_update(f"snooze:menu:{reminder.id}", update_id=2)
        )
        menu = self.transport.edits[-1]
        self.assertIn("Когда напомнить снова?", menu["text"])
        callbacks = [
            button["callback_data"]
            for row in menu["reply_markup"]["inline_keyboard"]
            for button in row
        ]
        self.assertEqual(
            [
                f"snooze:15:{reminder.id}",
                f"snooze:60:{reminder.id}",
                f"snooze:tomorrow:{reminder.id}",
                f"snooze:back:{reminder.id}",
            ],
            callbacks,
        )

        self.app.handle_update(
            callback_update(f"snooze:15:{reminder.id}", update_id=3)
        )
        snoozed = self.repository.get_reminder(reminder.id)
        self.assertEqual(
            self.clock.now_utc() + timedelta(minutes=15),
            snoozed.scheduled_at_utc,
        )
        self.assertIn("через 15 минут", self.transport.edits[-1]["text"])

        self.clock.value = snoozed.scheduled_at_utc + timedelta(seconds=1)
        self.app.send_due_reminders()
        rescheduled = self.repository.get_reminder(reminder.id)
        self.assertEqual(
            original_due + timedelta(days=1),
            rescheduled.scheduled_at_utc,
        )

    def test_irrelevant_button_stops_future_notifications(self):
        reminder = self.repository.create_reminder(
            1,
            100,
            "Купить старый билет",
            "исходный текст",
            self.clock.now_utc() - timedelta(seconds=1),
            MOSCOW_TIMEZONE,
            "test",
            self.clock.now_utc(),
        )
        self.app.send_due_reminders()
        self.app.handle_update(
            callback_update(f"task:irrelevant:{reminder.id}", update_id=2)
        )
        self.assertEqual("cancelled", self.repository.get_reminder(reminder.id).status)
        self.assertEqual([], self.repository.due_reminders(self.clock.now_utc() + timedelta(days=7)))
        self.assertEqual(
            "🚫 Больше не напоминаю: Купить старый билет",
            self.transport.edits[-1]["text"],
        )

    def test_daily_review_packs_yesterdays_tasks_and_updates_in_place(self):
        delivered_at = datetime(2026, 7, 12, 5, 0, tzinfo=timezone.utc)
        next_delivery = datetime(2026, 7, 13, 9, 0, tzinfo=timezone.utc)
        reminders = []
        for title in ["Позвонить маме", "Записаться к врачу"]:
            reminder = self.repository.create_reminder(
                1,
                100,
                title,
                title,
                delivered_at,
                MOSCOW_TIMEZONE,
                "test",
                delivered_at,
            )
            self.repository.mark_delivered(
                reminder.id,
                delivered_at=delivered_at,
                next_scheduled_at=next_delivery,
            )
            reminders.append(reminder)

        # 05:45 UTC = 08:45 МСК on the next day.
        self.clock.value = datetime(2026, 7, 13, 5, 45, tzinfo=timezone.utc)
        self.assertEqual(1, self.app.send_daily_reviews())
        review = self.transport.messages[-1]
        self.assertIn("1. Позвонить маме", review["text"])
        self.assertIn("2. Записаться к врачу", review["text"])
        self.assertEqual(2, len(review["reply_markup"]["inline_keyboard"]))
        self.assertEqual(0, self.app.send_daily_reviews())

        review_date = "20260713"
        self.app.handle_update(
            callback_update(
                f"review:not_done:{reminders[0].id}:{review_date}",
                update_id=3,
            )
        )
        self.assertEqual("active", self.repository.get_reminder(reminders[0].id).status)
        self.assertNotIn("Позвонить маме", self.transport.edits[-1]["text"])
        self.assertIn("Записаться к врачу", self.transport.edits[-1]["text"])

        self.app.handle_update(
            callback_update(
                f"review:completed:{reminders[1].id}:{review_date}",
                update_id=4,
            )
        )
        self.assertEqual("completed", self.repository.get_reminder(reminders[1].id).status)
        self.assertEqual("✅ Всё разобрано. Хорошего дня!", self.transport.edits[-1]["text"])
        self.assertEqual(
            ["not_done"],
            [event.action for event in self.repository.list_events(reminders[0].id)],
        )

    def test_daily_review_is_sent_only_in_the_morning_window(self):
        reminder = self.repository.create_reminder(
            1,
            100,
            "Старая задача",
            "текст",
            datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
            MOSCOW_TIMEZONE,
            "test",
            datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        )
        self.repository.mark_delivered(
            reminder.id,
            datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc),
        )
        # 15:00 МСК: a late restart must not produce a "good morning" message.
        self.clock.value = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(0, self.app.send_daily_reviews())

    def test_scheduler_keeps_reminder_when_delivery_fails(self):
        reminder = self.repository.create_reminder(
            1,
            100,
            "Проверить сервер",
            "исходный текст",
            self.clock.now_utc() - timedelta(seconds=1),
            MOSCOW_TIMEZONE,
            "test",
            self.clock.now_utc(),
        )
        self.transport.fail = True
        self.assertEqual(0, self.app.send_due_reminders())
        self.assertEqual("active", self.repository.get_reminder(reminder.id).status)


if __name__ == "__main__":
    unittest.main()
