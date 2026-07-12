from __future__ import annotations

from datetime import datetime

from reminder_bot.domain import ClarificationType, ParseResult, UnsupportedInput
from reminder_bot.parsers.base import ReminderParser


class HybridParser:
    """Rule-based first; OpenAI используется только для неизвестных конструкций."""

    def __init__(self, rule_parser: ReminderParser, openai_parser: ReminderParser | None = None):
        self.rule_parser = rule_parser
        self.openai_parser = openai_parser

    def parse(self, text: str, now: datetime, timezone_name: str) -> ParseResult:
        try:
            rule_result = self.rule_parser.parse(text, now, timezone_name)
        except UnsupportedInput:
            if self.openai_parser is None:
                raise
            return self.openai_parser.parse(text, now, timezone_name)

        # «После обеда», «в половине седьмого» и похожие обороты выглядят для
        # rule parser как отсутствие времени. Даем OpenAI шанс распознать их,
        # но при сбое сохраняем безопасный детерминированный результат.
        if (
            self.openai_parser is not None
            and rule_result.clarification_type == ClarificationType.MISSING_TIME
        ):
            try:
                return self.openai_parser.parse(text, now, timezone_name)
            except RuntimeError:
                return rule_result
        return rule_result
