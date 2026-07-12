from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_env(path: Path | None = None) -> None:
    """Загружает простой KEY=VALUE файл, не перезаписывая окружение."""
    env_path = path or Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(name.strip(), value)


@dataclass(frozen=True, slots=True)
class Settings:
    telegram_token: str
    openai_api_key: str | None
    openai_model: str
    database_path: Path
    poll_timeout: int = 20

    @classmethod
    def from_env(cls) -> "Settings":
        load_env()
        telegram_token = os.getenv("TG_BOT_API", "").strip()
        if not telegram_token:
            raise RuntimeError("В файле .env не задан TG_BOT_API")

        # В исходной спецификации ключ называется OPENAI_API.
        openai_key = (
            os.getenv("OPENAI_API_KEY", "").strip()
            or os.getenv("OPENAI_API", "").strip()
            or None
        )
        project_root = Path(__file__).resolve().parent.parent
        db_value = os.getenv("DATABASE_PATH", "data/reminders.sqlite3")
        database_path = Path(db_value)
        if not database_path.is_absolute():
            database_path = project_root / database_path

        return cls(
            telegram_token=telegram_token,
            openai_api_key=openai_key,
            openai_model=os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip(),
            database_path=database_path,
            poll_timeout=int(os.getenv("POLL_TIMEOUT", "20")),
        )
