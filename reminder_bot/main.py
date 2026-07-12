from __future__ import annotations

import sys
import time

from reminder_bot.application import BotApplication
from reminder_bot.clock import SystemClock
from reminder_bot.config import Settings
from reminder_bot.parsers import HybridParser, OpenAIParser, RuleBasedParser
from reminder_bot.repository import Repository
from reminder_bot.service import ReminderService
from reminder_bot.telegram_api import TelegramAPIError, TelegramClient


def build_application(settings: Settings):
    repository = Repository(settings.database_path)
    repository.migrate()
    rule_parser = RuleBasedParser()
    openai_parser = (
        OpenAIParser(settings.openai_api_key, settings.openai_model)
        if settings.openai_api_key
        else None
    )
    parser = HybridParser(rule_parser, openai_parser)
    client = TelegramClient(settings.telegram_token)
    clock = SystemClock()
    application = BotApplication(
        repository=repository,
        parser=parser,
        service=ReminderService(repository),
        transport=client,
        clock=clock,
    )
    return application, repository, client


def main() -> None:
    settings = Settings.from_env()
    application, repository, client = build_application(settings)
    try:
        profile = client.get_me()
        client.delete_webhook()
        client.set_commands()
        offset = int(repository.get_state("telegram_offset", "0") or "0")
        print(f"Бот @{profile['username']} запущен. Для остановки нажмите Ctrl+C.")

        while True:
            try:
                updates = client.get_updates(offset=offset, timeout=settings.poll_timeout)
                for update in updates:
                    try:
                        application.handle_update(update)
                    except Exception as error:  # Один update не должен остановить процесс.
                        print(f"Ошибка обработки сообщения: {type(error).__name__}: {error}")
                    finally:
                        offset = int(update["update_id"]) + 1
                        repository.set_state("telegram_offset", str(offset))
                application.send_due_reminders()
            except TelegramAPIError as error:
                print(f"Ошибка Telegram: {error}. Повтор через 3 секунды.")
                time.sleep(3)
    except KeyboardInterrupt:
        print("\nБот остановлен.")
    finally:
        repository.close()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as error:
        print(f"Ошибка запуска: {error}", file=sys.stderr)
        raise SystemExit(1)
