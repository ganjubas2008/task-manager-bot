from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from reminder_bot.constants import MOSCOW_TIMEZONE
from reminder_bot.domain import ParseResult, PendingRequest, Reminder, ReminderEvent


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
                updated_at TEXT NOT NULL,
                original_scheduled_at_utc TEXT,
                notification_count INTEGER NOT NULL DEFAULT 0,
                first_notified_at TEXT,
                last_notified_at TEXT,
                completed_at TEXT
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
                message_id INTEGER,
                reminder_id INTEGER
            );

            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        reminder_columns = {
            row["name"]
            for row in self.connection.execute("PRAGMA table_info(reminders)").fetchall()
        }
        reminder_migrations = {
            "original_scheduled_at_utc": (
                "ALTER TABLE reminders ADD COLUMN original_scheduled_at_utc TEXT"
            ),
            "notification_count": (
                "ALTER TABLE reminders "
                "ADD COLUMN notification_count INTEGER NOT NULL DEFAULT 0"
            ),
            "first_notified_at": "ALTER TABLE reminders ADD COLUMN first_notified_at TEXT",
            "last_notified_at": "ALTER TABLE reminders ADD COLUMN last_notified_at TEXT",
            "completed_at": "ALTER TABLE reminders ADD COLUMN completed_at TEXT",
        }
        for column, statement in reminder_migrations.items():
            if column not in reminder_columns:
                self.connection.execute(statement)
        self.connection.execute(
            """
            UPDATE reminders
            SET original_scheduled_at_utc = scheduled_at_utc
            WHERE original_scheduled_at_utc IS NULL
            """
        )

        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS reminder_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                reminder_id INTEGER NOT NULL,
                telegram_user_id INTEGER NOT NULL,
                action TEXT NOT NULL
                    CHECK(action IN ('completed', 'not_done', 'irrelevant')),
                source TEXT NOT NULL
                    CHECK(source IN ('notification', 'daily_review', 'list')),
                created_at TEXT NOT NULL,
                FOREIGN KEY(reminder_id) REFERENCES reminders(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_reminder_events_task
                ON reminder_events(reminder_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_reminder_events_user_day
                ON reminder_events(telegram_user_id, action, created_at);

            CREATE TABLE IF NOT EXISTS daily_reviews (
                telegram_user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                review_date TEXT NOT NULL,
                sent_at TEXT NOT NULL,
                PRIMARY KEY(telegram_user_id, chat_id, review_date)
            );

            CREATE TABLE IF NOT EXISTS daily_review_items (
                telegram_user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                review_date TEXT NOT NULL,
                reminder_id INTEGER NOT NULL,
                response TEXT
                    CHECK(response IS NULL OR response IN ('completed', 'not_done', 'irrelevant')),
                responded_at TEXT,
                PRIMARY KEY(telegram_user_id, chat_id, review_date, reminder_id),
                FOREIGN KEY(reminder_id) REFERENCES reminders(id) ON DELETE CASCADE,
                FOREIGN KEY(telegram_user_id, chat_id, review_date)
                    REFERENCES daily_reviews(telegram_user_id, chat_id, review_date)
                    ON DELETE CASCADE
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
        if "reminder_id" not in pending_columns:
            self.connection.execute(
                "ALTER TABLE pending_requests ADD COLUMN reminder_id INTEGER"
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
                user_timezone, status, parser_type, created_at, updated_at,
                original_scheduled_at_utc
            ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
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
                self._iso(scheduled_at_utc),
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

    def find_active_reminder(
        self,
        reminder_id: int,
        telegram_user_id: int,
    ) -> Reminder | None:
        row = self.connection.execute(
            """
            SELECT * FROM reminders
            WHERE id = ? AND telegram_user_id = ? AND status = 'active'
            """,
            (reminder_id, telegram_user_id),
        ).fetchone()
        return self._reminder(row) if row else None

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
        return self.respond_to_reminder(
            reminder_id=reminder_id,
            telegram_user_id=telegram_user_id,
            action="irrelevant",
            source="list",
            now=now,
        ) == "updated"

    def respond_to_reminder(
        self,
        reminder_id: int,
        telegram_user_id: int,
        action: str,
        source: str,
        now: datetime,
    ) -> str:
        if action not in {"completed", "not_done", "irrelevant"}:
            raise ValueError(f"Неизвестный ответ на задачу: {action}")
        if source not in {"notification", "daily_review", "list"}:
            raise ValueError(f"Неизвестный источник ответа: {source}")

        row = self.connection.execute(
            "SELECT telegram_user_id, status FROM reminders WHERE id = ?",
            (reminder_id,),
        ).fetchone()
        if not row or row["telegram_user_id"] != telegram_user_id:
            return "not_found"
        if row["status"] != "active":
            return "already_closed"

        stamp = self._iso(now)
        if action == "completed":
            self.connection.execute(
                """
                UPDATE reminders
                SET status = 'completed', completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (stamp, stamp, reminder_id),
            )
        elif action == "irrelevant":
            self.connection.execute(
                """
                UPDATE reminders SET status = 'cancelled', updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (stamp, reminder_id),
            )
        else:
            self.connection.execute(
                "UPDATE reminders SET updated_at = ? WHERE id = ? AND status = 'active'",
                (stamp, reminder_id),
            )
        self.connection.execute(
            """
            INSERT INTO reminder_events(
                reminder_id, telegram_user_id, action, source, created_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (reminder_id, telegram_user_id, action, source, stamp),
        )
        self.connection.commit()
        return "updated"

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

    def mark_delivered(
        self,
        reminder_id: int,
        delivered_at: datetime,
        next_scheduled_at: datetime,
    ) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE reminders
            SET scheduled_at_utc = ?,
                notification_count = notification_count + 1,
                first_notified_at = COALESCE(first_notified_at, ?),
                last_notified_at = ?,
                updated_at = ?
            WHERE id = ? AND status = 'active'
            """,
            (
                self._iso(next_scheduled_at),
                self._iso(delivered_at),
                self._iso(delivered_at),
                self._iso(delivered_at),
                reminder_id,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def update_reminder(
        self,
        reminder_id: int,
        telegram_user_id: int,
        title: str,
        source_text: str,
        scheduled_at_utc: datetime,
        user_timezone: str,
        parser_type: str,
        now: datetime,
    ) -> Reminder | None:
        cursor = self.connection.execute(
            """
            UPDATE reminders
            SET title = ?, source_text = ?, scheduled_at_utc = ?,
                original_scheduled_at_utc = ?, user_timezone = ?, parser_type = ?,
                updated_at = ?
            WHERE id = ? AND telegram_user_id = ? AND status = 'active'
            """,
            (
                title,
                source_text,
                self._iso(scheduled_at_utc),
                self._iso(scheduled_at_utc),
                user_timezone,
                parser_type,
                self._iso(now),
                reminder_id,
                telegram_user_id,
            ),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            return None
        return self.get_reminder(reminder_id)

    def snooze_reminder(
        self,
        reminder_id: int,
        telegram_user_id: int,
        scheduled_at_utc: datetime,
        now: datetime,
    ) -> Reminder | None:
        cursor = self.connection.execute(
            """
            UPDATE reminders
            SET scheduled_at_utc = ?, updated_at = ?
            WHERE id = ? AND telegram_user_id = ? AND status = 'active'
            """,
            (
                self._iso(scheduled_at_utc),
                self._iso(now),
                reminder_id,
                telegram_user_id,
            ),
        )
        self.connection.commit()
        if cursor.rowcount != 1:
            return None
        return self.get_reminder(reminder_id)

    def list_events(self, reminder_id: int) -> list[ReminderEvent]:
        rows = self.connection.execute(
            "SELECT * FROM reminder_events WHERE reminder_id = ? ORDER BY created_at, id",
            (reminder_id,),
        ).fetchall()
        return [self._event(row) for row in rows]

    def list_daily_review_candidates(
        self,
        day_started_at: datetime,
        now: datetime,
    ) -> list[Reminder]:
        rows = self.connection.execute(
            """
            SELECT r.* FROM reminders AS r
            WHERE r.status = 'active'
              AND r.notification_count > 0
              AND r.last_notified_at < ?
              AND NOT EXISTS (
                  SELECT 1 FROM reminder_events AS e
                  WHERE e.reminder_id = r.id
                    AND e.action = 'not_done'
                    AND e.created_at >= ?
                    AND e.created_at <= ?
              )
            ORDER BY r.last_notified_at, r.id
            """,
            (self._iso(day_started_at), self._iso(day_started_at), self._iso(now)),
        ).fetchall()
        return [self._reminder(row) for row in rows]

    def daily_review_was_sent(
        self,
        telegram_user_id: int,
        chat_id: int,
        review_date: str,
    ) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM daily_reviews
            WHERE telegram_user_id = ? AND chat_id = ? AND review_date = ?
            """,
            (telegram_user_id, chat_id, review_date),
        ).fetchone()
        return row is not None

    def save_daily_review(
        self,
        telegram_user_id: int,
        chat_id: int,
        review_date: str,
        reminder_ids: list[int],
        sent_at: datetime,
    ) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO daily_reviews(
                telegram_user_id, chat_id, review_date, sent_at
            ) VALUES (?, ?, ?, ?)
            """,
            (telegram_user_id, chat_id, review_date, self._iso(sent_at)),
        )
        if cursor.rowcount != 1:
            self.connection.commit()
            return False
        self.connection.executemany(
            """
            INSERT INTO daily_review_items(
                telegram_user_id, chat_id, review_date, reminder_id
            ) VALUES (?, ?, ?, ?)
            """,
            [
                (telegram_user_id, chat_id, review_date, reminder_id)
                for reminder_id in reminder_ids
            ],
        )
        self.connection.commit()
        return True

    def mark_daily_review_response(
        self,
        telegram_user_id: int,
        chat_id: int,
        review_date: str,
        reminder_id: int,
        response: str,
        now: datetime,
    ) -> bool:
        if response not in {"completed", "not_done", "irrelevant"}:
            raise ValueError(f"Неизвестный ответ на проверку: {response}")
        cursor = self.connection.execute(
            """
            UPDATE daily_review_items
            SET response = ?, responded_at = ?
            WHERE telegram_user_id = ? AND chat_id = ? AND review_date = ?
              AND reminder_id = ? AND response IS NULL
            """,
            (
                response,
                self._iso(now),
                telegram_user_id,
                chat_id,
                review_date,
                reminder_id,
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def list_open_daily_review_items(
        self,
        telegram_user_id: int,
        chat_id: int,
        review_date: str,
    ) -> list[Reminder]:
        rows = self.connection.execute(
            """
            SELECT r.*
            FROM daily_review_items AS i
            JOIN reminders AS r ON r.id = i.reminder_id
            WHERE i.telegram_user_id = ? AND i.chat_id = ? AND i.review_date = ?
              AND i.response IS NULL AND r.status = 'active'
            ORDER BY r.last_notified_at, r.id
            """,
            (telegram_user_id, chat_id, review_date),
        ).fetchall()
        return [self._reminder(row) for row in rows]

    def save_pending(self, pending: PendingRequest) -> None:
        self.connection.execute(
            """
            INSERT INTO pending_requests(
                telegram_user_id, chat_id, source_text, parse_result_json,
                clarification_type, created_at, message_id, reminder_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                chat_id = excluded.chat_id,
                source_text = excluded.source_text,
                parse_result_json = excluded.parse_result_json,
                clarification_type = excluded.clarification_type,
                created_at = excluded.created_at,
                message_id = excluded.message_id,
                reminder_id = excluded.reminder_id
            """,
            (
                pending.telegram_user_id,
                pending.chat_id,
                pending.source_text,
                json.dumps(pending.parse_result.to_dict(), ensure_ascii=False),
                pending.clarification_type,
                self._iso(pending.created_at),
                pending.message_id,
                pending.reminder_id,
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
            reminder_id=row["reminder_id"],
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
    def _optional_datetime(cls, value: str | None) -> datetime | None:
        return cls._datetime(value) if value else None

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
            original_scheduled_at_utc=cls._optional_datetime(
                row["original_scheduled_at_utc"]
            ),
            notification_count=row["notification_count"],
            first_notified_at=cls._optional_datetime(row["first_notified_at"]),
            last_notified_at=cls._optional_datetime(row["last_notified_at"]),
            completed_at=cls._optional_datetime(row["completed_at"]),
        )

    @classmethod
    def _event(cls, row: sqlite3.Row) -> ReminderEvent:
        return ReminderEvent(
            id=row["id"],
            reminder_id=row["reminder_id"],
            telegram_user_id=row["telegram_user_id"],
            action=row["action"],
            source=row["source"],
            created_at=cls._datetime(row["created_at"]),
        )
