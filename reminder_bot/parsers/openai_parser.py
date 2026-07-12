from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable

from reminder_bot.domain import ParseResult, UnsupportedInput


PARSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {"type": "string"},
        "date": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "time": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "date_confidence": {"type": "string", "enum": ["high", "low"]},
        "time_confidence": {"type": "string", "enum": ["high", "low"]},
        "needs_clarification": {"type": "boolean"},
        "clarification_type": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "clarification_options": {"type": "array", "items": {"type": "string"}},
        "date_was_explicit": {"type": "boolean"},
        "time_was_explicit": {"type": "boolean"},
    },
    "required": [
        "title",
        "date",
        "time",
        "date_confidence",
        "time_confidence",
        "needs_clarification",
        "clarification_type",
        "clarification_options",
        "date_was_explicit",
        "time_was_explicit",
    ],
}


SYSTEM_PROMPT = """Ты — строго детерминированный parser русских напоминаний.
Верни только данные по заданной JSON-схеме. Ничего не создавай и не отправляй.

Критические правила:
- date имеет формат YYYY-MM-DD, time — HH:MM.
- Относительные даты и время считай от переданного локального now.
- Дата без года означает ближайшую будущую такую дату.
- Если дата отсутствует, выбери ближайший будущий момент для указанного времени.
- ЛЮБОЕ время с часом 1–11 без слов «утра/вечера/дня/ночи» неоднозначно.
  Всегда верни time=null, time_confidence="low", needs_clarification=true,
  clarification_type="ambiguous_time" и два варианта.
- Это правило обязательно и для времени с минутами: «в 6:35» означает варианты
  ["06:35", "18:35"]. Никогда не выбирай один из них самостоятельно.
- 13–23, полдень и полночь однозначны.
- Если время отсутствует: clarification_type="missing_time".
- Несуществующая дата: clarification_type="invalid_date".
- Числа в названии события не являются временем без временного контекста.
- title содержит только действие, без даты, времени и вводных «напомни мне».
- Не угадывай при неопределённости.

Контрольные примеры:
- «концерт в 7» → time=null, options=["07:00", "19:00"].
- «стирка в 6:35» → time=null, options=["06:35", "18:35"].
- «концерт в 7 вечера» → time="19:00", уточнение не нужно.
"""


WEEKDAY_NAMES = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

WEEKDAY_FORMS = {
    "понедельник": 0,
    "понедельника": 0,
    "вторник": 1,
    "вторника": 1,
    "среда": 2,
    "среду": 2,
    "четверг": 3,
    "четверга": 3,
    "пятница": 4,
    "пятницу": 4,
    "суббота": 5,
    "субботу": 5,
    "воскресенье": 6,
}


class OpenAIParser:
    parser_type = "openai"

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4o-mini",
        request_func: Callable[[urllib.request.Request, float], dict[str, Any]] | None = None,
    ):
        if not api_key:
            raise ValueError("Нужен OpenAI API key")
        self.api_key = api_key
        self.model = model
        self.request_func = request_func or self._urlopen_json

    def parse(self, text: str, now: datetime, timezone_name: str) -> ParseResult:
        calendar_lines = []
        for days in range(8):
            current = now.date() + timedelta(days=days)
            calendar_lines.append(f"{current.isoformat()} — {WEEKDAY_NAMES[current.weekday()]}")
        payload = {
            "model": self.model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"timezone: {timezone_name}\n"
                        f"local now: {now.isoformat()}\n"
                        "Проверенный календарь от текущей даты:\n"
                        + "\n".join(calendar_lines)
                        + "\n"
                        f"request: {text}"
                    ),
                },
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "reminder_parse_result",
                    "strict": True,
                    "schema": PARSE_SCHEMA,
                }
            },
            "temperature": 0,
            "max_output_tokens": 500,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            "https://api.openai.com/v1/responses",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            response = self.request_func(request, 45.0)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI API вернул HTTP {error.code}: {detail[:300]}") from error
        except urllib.error.URLError as error:
            raise RuntimeError(f"Нет соединения с OpenAI API: {error.reason}") from error

        output_text = self._extract_output_text(response)
        try:
            data = json.loads(output_text)
        except json.JSONDecodeError as error:
            raise RuntimeError("OpenAI вернул некорректный JSON") from error

        result = ParseResult.from_dict(data)
        result.parser_type = self.parser_type
        if not result.title.strip():
            raise UnsupportedInput("OpenAI не выделил название события")
        self._enforce_ambiguity_safety(text, result)
        self._enforce_weekday_safety(text, now, result)
        return result

    @staticmethod
    def _enforce_ambiguity_safety(text: str, result: ParseResult) -> None:
        """Не позволяет модели silently выбрать утро или вечер."""
        normalized = text.lower().replace("ё", "е")
        if re.search(r"\b(?:утра|вечера|дня|ночи|полдень|полночь)\b", normalized):
            return
        match = re.search(r"\bв\s+(\d{1,2})(?:(?::|\.|\s)(\d{2}))?\b", normalized)
        if not match:
            return
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if not (1 <= hour <= 11 and 0 <= minute <= 59):
            return
        result.time = None
        result.time_confidence = "low"
        result.needs_clarification = True
        result.clarification_type = "ambiguous_time"
        result.clarification_options = [
            f"{hour:02d}:{minute:02d}",
            f"{hour + 12:02d}:{minute:02d}",
        ]
        result.time_was_explicit = True

    @staticmethod
    def _enforce_weekday_safety(text: str, now: datetime, result: ParseResult) -> None:
        """Сверяет названный день недели с локальным календарём."""
        normalized = text.lower().replace("ё", "е")
        forms = "|".join(sorted(WEEKDAY_FORMS, key=len, reverse=True))
        match = re.search(
            rf"\b(?:(следующ(?:ий|ую|ее)|эт(?:от|у|о))\s+)?({forms})\b",
            normalized,
        )
        if not match:
            return
        modifier, name = match.group(1), match.group(2)
        delta = (WEEKDAY_FORMS[name] - now.weekday()) % 7
        if modifier and modifier.startswith("следующ"):
            delta = delta or 7
        if delta == 0 and result.time:
            try:
                parsed_time = datetime.strptime(result.time, "%H:%M").time()
            except ValueError:
                parsed_time = None
            if parsed_time and datetime.combine(now.date(), parsed_time, tzinfo=now.tzinfo) <= now:
                delta = 7
        result.date = (now.date() + timedelta(days=delta)).isoformat()
        result.date_confidence = "high"
        result.date_was_explicit = True

    @staticmethod
    def _urlopen_json(request: urllib.request.Request, timeout: float) -> dict[str, Any]:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    @staticmethod
    def _extract_output_text(response: dict[str, Any]) -> str:
        # Поле output_text есть в некоторых клиентах; сырой REST гарантированно
        # содержит output[].content[].text.
        if isinstance(response.get("output_text"), str):
            return response["output_text"]
        for item in response.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    return content["text"]
                if content.get("type") == "refusal":
                    raise RuntimeError("OpenAI отказался разбирать запрос")
        raise RuntimeError("OpenAI не вернул текстовый результат")
