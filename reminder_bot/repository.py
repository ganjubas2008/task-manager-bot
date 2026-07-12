from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from reminder_bot.constants import MOSCOW_TIMEZONE
from reminder_bot.domain import ParseResult, PendingRequest, Reminder


class Repository:
    def __init__(self, database_path: str | Path):
        value = str(database_path)
        if value != ":memory:":
            Path(value).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(value)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")

    def migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                telegram_user_id INTEGER PRIMARY KEY,
                timezone TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                source_text TEXT NOT NULL,
                scheduled_at_utc TEXT NOT NULL,
                user_timezone TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'sent', 'completed', 'cancelled')),
                parser_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_due
                ON reminders(status, scheduled_at_utc);
            CREATE INDEX IF NOT EXISTS idx_reminders_user
                ON reminders(telegram_user_id, status, scheduled_at_utc);

            CREATE TABLE IF NOT EXISTS pending_requests (
                telegram_user_id INTEGER PRIMARY KEY,
                chat_id INTEGER NOT NULL,
                source_text TEXT NOT NULL,
                parse_result_json TEXT NOT NULL,
                clarification_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                message_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        pending_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(pending_requests)").fetchall()
        }
        if "message_id" not in pending_columns:
            self.connection.execute(
                "ALTER TABLE pending_requests ADD COLUMN message_id INTEGER"
            )
        # Миграция старых данных: отображение всех активных задач теперь всегда
        # привязано к Москве. Сам UTC-момент задачи при этом не меняется.
        self.connection.execute(
            "UPDATE reminders SET user_timezone = ? WHERE status = 'active'",
            (MOSCOW_TIMEZONE,),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def create_reminder(
        self,
        telegram_user_id: int,
        chat_id: int,
        title: str,
        source_text: str,
        scheduled_at_utc: datetime,
        user_timezone: str,
        parser_type: str,
        now: datetime,
    ) -> Reminder:
        stamp = self._iso(now)
        cursor = self.connection.execute(
            """
            INSERT INTO reminders(
                telegram_user_id, chat_id, title, source_text, scheduled_at_utc,
                user_timezone, status, parser_type, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (
                telegram_user_id,
                chat_id,
                title,
                source_text,
                self._iso(scheduled_at_utc),
                user_timezone,
                parser_type,
                stamp,
                stamp,
            ),
        )
        self.connection.commit()
        return self.get_reminder(cursor.lastrowid)

    def get_reminder(self, reminder_id: int) -> Reminder:
        row = self.connection.execute(
            "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
        ).fetchone()
        if not row:
            raise KeyError(reminder_id)
        return self._reminder(row)

    def list_active(self, telegram_user_id: int) -> list[Reminder]:
        rows = self.connection.execute(
            """
            SELECT * FROM reminders
            WHERE telegram_user_id = ? AND status = 'active'
            ORDER BY scheduled_at_utc, id
            """,
            (telegram_user_id,),
        ).fetchall()
        return [self._reminder(row) for row in rows]

    def cancel_reminder(self, reminder_id: int, telegram_user_id: int, now: datetime) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE reminders SET status = 'cancelled', updated_at = ?
            WHERE id = ? AND telegram_user_id = ? AND status = 'active'
            """,
            (self._iso(now), reminder_id, telegram_user_id),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def due_reminders(self, now: datetime, limit: int = 100) -> list[Reminder]:
        rows = self.connection.execute(
            """
            SELECT * FROM reminders
            WHERE status = 'active' AND scheduled_at_utc <= ?
            ORDER BY scheduled_at_utc, id LIMIT ?
            """,
            (self._iso(now), limit),
        ).fetchall()
        return [self._reminder(row) for row in rows]

    def delete_after_delivery(self, reminder_id: int) -> bool:
        cursor = self.connection.execute(
            "DELETE FROM reminders WHERE id = ? AND status = 'active'",
            (reminder_id,),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def save_pending(self, pending: PendingRequest) -> None:
        self.connection.execute(
            """
            INSERT INTO pending_requests(
                telegram_user_id, chat_id, source_text, parse_result_json,
                clarification_type, created_at, message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                source_text = excluded.source_text,
                parse_result_json = excluded.parse_result_json,
                clarification_type = excluded.clarification_type,
                created_at = excluded.created_at,
                message_id = excluded.message_id
            """,
            (
                pending.telegram_user_id,
                pending.chat_id,
                pending.source_text,
                json.dumps(pending.parse_result.to_dict(), ensure_ascii=False),
                pending.clarification_type,
                self._iso(pending.created_at),
                pending.message_id,
            ),
        )
        self.connection.commit()

    def get_pending(self, telegram_user_id: int) -> PendingRequest | None:
        row = self.connection.execute(
            "SELECT * FROM pending_requests WHERE telegram_user_id = ?",
            (telegram_user_id,),
        ).fetchone()
        if not row:
            return None
        return PendingRequest(
            telegram_user_id=row["telegram_user_id"],
            chat_id=row["chat_id"],
            source_text=row["source_text"],
            parse_result=ParseResult.from_dict(json.loads(row["parse_result_json"])),
            clarification_type=row["clarification_type"],
            created_at=self._datetime(row["created_at"]),
            message_id=row["message_id"],
        )

    def clear_pending(self, telegram_user_id: int) -> None:
        self.connection.execute(
            "DELETE FROM pending_requests WHERE telegram_user_id = ?", (telegram_user_id,)
        )
        self.connection.commit()

    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.connection.execute(
            "SELECT value FROM app_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO app_state(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (key, value),
        )
        self.connection.commit()

    @staticmethod
    def _iso(value: datetime) -> str:
        if value.tzinfo is None:
            raise ValueError("datetime должен содержать timezone")
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _datetime(value: str) -> datetime:
        return datetime.fromisoformat(value).astimezone(timezone.utc)

    @classmethod
    def _reminder(cls, row: sqlite3.Row) -> Reminder:
        return Reminder(
            id=row["id"],
            telegram_user_id=row["telegram_user_id"],
            chat_id=row["chat_id"],
            title=row["title"],
            source_text=row["source_text"],
            scheduled_at_utc=cls._datetime(row["scheduled_at_utc"]),
            user_timezone=row["user_timezone"],
            status=row["status"],
            parser_type=row["parser_type"],
            created_at=cls._datetime(row["created_at"]),
            updated_at=cls._datetime(row["updated_at"]),
        )
