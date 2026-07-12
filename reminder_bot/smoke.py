from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

from reminder_bot.config import Settings
from reminder_bot.parsers.openai_parser import OpenAIParser
from reminder_bot.telegram_api import TelegramClient


def main() -> None:
    settings = Settings.from_env()
    telegram = TelegramClient(settings.telegram_token)
    profile = telegram.get_me()
    print(f"Telegram: успешно (@{profile['username']})")

    if not settings.openai_api_key:
        print("OpenAI: ключ не задан, проверка пропущена")
        return
    parser = OpenAIParser(settings.openai_api_key, settings.openai_model)
    result = parser.parse(
        "концерт завтра в 7 вечера",
        datetime(2026, 7, 12, 15, 0, tzinfo=ZoneInfo("Europe/Moscow")),
        "Europe/Moscow",
    )
    expected = ("2026-07-13", "19:00", False)
    actual = (result.date, result.time, result.needs_clarification)
    if actual != expected:
        raise RuntimeError(
            "OpenAI parser не прошел проверку: "
            + json.dumps(result.to_dict(), ensure_ascii=False)
        )
    print(f"OpenAI: успешно ({settings.openai_model})")


if __name__ == "__main__":
    main()
