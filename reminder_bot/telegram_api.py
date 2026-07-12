from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class TelegramAPIError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str):
        self.token = token
        self.base_url = f"https://api.telegram.org/bot{token}"

    def request(self, method: str, **parameters: Any) -> Any:
        encoded: dict[str, Any] = {}
        for key, value in parameters.items():
            if value is None:
                continue
            encoded[key] = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value
        data = urllib.parse.urlencode(encoded).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}/{method}", data=data, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise TelegramAPIError(f"Telegram HTTP {error.code}: {detail[:300]}") from error
        except urllib.error.URLError as error:
            raise TelegramAPIError(f"Нет соединения с Telegram: {error.reason}") from error
        if not result.get("ok"):
            raise TelegramAPIError(result.get("description", "Telegram вернул ошибку"))
        return result.get("result")

    def get_me(self) -> dict:
        return self.request("getMe")

    def get_updates(self, offset: int, timeout: int) -> list[dict]:
        return self.request(
            "getUpdates",
            offset=offset,
            timeout=timeout,
            allowed_updates=["message", "callback_query"],
        )

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> dict:
        return self.request(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
        )

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
    ) -> dict:
        return self.request(
            "editMessageText",
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )

    def answer_callback(self, callback_query_id: str, text: str | None = None) -> bool:
        return self.request("answerCallbackQuery", callback_query_id=callback_query_id, text=text)

    def delete_webhook(self) -> bool:
        return self.request("deleteWebhook", drop_pending_updates=False)

    def set_commands(self) -> bool:
        return self.request(
            "setMyCommands",
            commands=[
                {"command": "start", "description": "Начать работу"},
                {"command": "reminders", "description": "Показать напоминания"},
                {"command": "cancel", "description": "Отменить уточнение"},
                {"command": "help", "description": "Показать справку"},
            ],
            language_code="ru",
        )
