from __future__ import annotations

import os
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from reminder_bot.config import load_env
from reminder_bot.parsers.openai_parser import OpenAIParser
from reminder_bot.parsers.rule_based import RuleBasedParser


load_env()
LIVE_ENABLED = os.getenv("RUN_LIVE_OPENAI_TESTS") == "1"


@unittest.skipUnless(LIVE_ENABLED, "Для платных API-проверок задайте RUN_LIVE_OPENAI_TESTS=1")
class LiveDifferentialTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.now = datetime(2026, 7, 12, 15, 0, tzinfo=ZoneInfo("Europe/Riga"))
        cls.rule = RuleBasedParser()
        cls.openai = OpenAIParser(
            os.environ["OPENAI_API_KEY"], os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        )

    def test_critical_date_time_fields_match(self):
        cases = [
            "концерт завтра в 7 вечера",
            "снять стирку 15/07 в 6:35",
            "позвонить маме через 2 часа",
            "купить 2 билета на концерт 15/07 в 19:00",
            "в пятницу концерт в 19:00",
        ]
        for text in cases:
            with self.subTest(text=text):
                rule = self.rule.parse(text, self.now, "Europe/Riga")
                ai = self.openai.parse(text, self.now, "Europe/Riga")
                self.assertEqual(rule.date, ai.date, text)
                self.assertEqual(rule.time, ai.time, text)
                self.assertEqual(rule.needs_clarification, ai.needs_clarification, text)
                self.assertEqual(rule.clarification_options, ai.clarification_options, text)


if __name__ == "__main__":
    unittest.main()
