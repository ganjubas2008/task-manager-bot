from __future__ import annotations

import unittest
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from reminder_bot.constants import MOSCOW_TIMEZONE
from reminder_bot.domain import ParseResult, PendingRequest
from reminder_bot.repository import Repository
from reminder_bot.service import ReminderService


class RepositoryAndServiceTests(unittest.TestCase):
    def setUp(self):
        self.repository = Repository(":memory:")
        self.repository.migrate()
        self.service = ReminderService(self.repository)
        self.now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.repository.close()

    def test_create_list_cancel_lifecycle(self):
        result = ParseResult(
            title="позвонить маме",
            date="2026-07-13",
            time="19:00",
            parser_type="rule_based",
        )
        reminder = self.service.create(
            10, 20, "завтра позвонить маме в 19:00", result, self.now
        )
        self.assertEqual("Позвонить маме", reminder.title)
        self.assertEqual("active", reminder.status)
        self.assertEqual([reminder.id], [item.id for item in self.repository.list_active(10)])

        deleted = self.repository.cancel_reminder(reminder.id, 10, self.now)
        self.assertTrue(deleted)
        self.assertEqual([], self.repository.list_active(10))

    def test_user_cannot_delete_another_users_reminder(self):
        result = ParseResult(
            title="секрет",
            date="2026-07-13",
            time="19:00",
            parser_type="test",
        )
        reminder = self.service.create(1, 1, "секрет", result, self.now)
        self.assertFalse(self.repository.cancel_reminder(reminder.id, 2, self.now))
        self.assertEqual(1, len(self.repository.list_active(1)))

    def test_due_reminders_are_sorted_and_deleted_after_delivery(self):
        first = self.repository.create_reminder(
            1, 1, "первое", "первое", self.now - timedelta(minutes=2), "UTC", "test", self.now
        )
        second = self.repository.create_reminder(
            1, 1, "второе", "второе", self.now - timedelta(minutes=1), "UTC", "test", self.now
        )
        due = self.repository.due_reminders(self.now)
        self.assertEqual([first.id, second.id], [item.id for item in due])
        self.assertTrue(self.repository.delete_after_delivery(first.id))
        self.assertEqual([second.id], [item.id for item in self.repository.due_reminders(self.now)])
        with self.assertRaises(KeyError):
            self.repository.get_reminder(first.id)

    def test_pending_request_survives_round_trip(self):
        parsed = ParseResult(
            title="концерт",
            date="2026-07-13",
            time=None,
            needs_clarification=True,
            clarification_type="missing_time",
            parser_type="rule_based",
        )
        self.repository.save_pending(
            PendingRequest(
                1,
                2,
                "завтра концерт",
                parsed,
                "missing_time",
                self.now,
                message_id=321,
            )
        )
        restored = self.repository.get_pending(1)
        self.assertIsNotNone(restored)
        self.assertEqual("концерт", restored.parse_result.title)
        self.assertEqual(321, restored.message_id)
        self.repository.clear_pending(1)
        self.assertIsNone(self.repository.get_pending(1))

    def test_app_state_is_persistent(self):
        self.repository.set_state("offset", "42")
        self.assertEqual("42", self.repository.get_state("offset"))

    def test_active_reminder_survives_database_restart(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "reminders.sqlite3"
            first = Repository(path)
            first.migrate()
            created = first.create_reminder(
                7,
                8,
                "После перезапуска",
                "исходный текст",
                self.now + timedelta(hours=1),
                "UTC",
                "test",
                self.now,
            )
            first.close()

            reopened = Repository(path)
            reopened.migrate()
            restored = reopened.list_active(7)
            self.assertEqual([created.id], [item.id for item in restored])
            self.assertEqual("После перезапуска", restored[0].title)
            self.assertEqual(MOSCOW_TIMEZONE, restored[0].user_timezone)
            reopened.close()

    def test_old_pending_table_gets_message_id_migration(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE pending_requests (
                    telegram_user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    source_text TEXT NOT NULL,
                    parse_result_json TEXT NOT NULL,
                    clarification_type TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            connection.commit()
            connection.close()

            migrated = Repository(path)
            migrated.migrate()
            columns = {
                row["name"]
                for row in migrated.connection.execute(
                    "PRAGMA table_info(pending_requests)"
                ).fetchall()
            }
            self.assertIn("message_id", columns)
            migrated.close()

    def test_confirmation_and_list_are_russian(self):
        result = ParseResult(
            title="снять стирку",
            date="2026-07-15",
            time="18:35",
            parser_type="test",
        )
        reminder = self.service.create(1, 1, "текст", result, self.now)
        self.assertEqual(
            "✅ Задача создана: Снять стирку\nСрок: 15.07.2026 18:35 (МСК)",
            self.service.format_confirmation(reminder),
        )
        self.assertEqual(
            "Создать задачу: Снять стирку\nСрок: 15.07.2026 18:35 (МСК)",
            self.service.format_preview(result, self.now),
        )
        listing = self.service.format_list([reminder])
        self.assertIn("📋 Напоминания", listing)
        self.assertIn("15.07.2026 18:35 (МСК) — Снять стирку", listing)


if __name__ == "__main__":
    unittest.main()
