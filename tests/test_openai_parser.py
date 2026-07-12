from __future__ import annotations

import json
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from reminder_bot.parsers.hybrid import HybridParser
from reminder_bot.parsers.openai_parser import OpenAIParser


class FakeRuleParser:
    def parse(self, text, now, timezone_name):
        from reminder_bot.domain import UnsupportedInput

        raise UnsupportedInput(text)


class MissingTimeRuleParser:
    def parse(self, text, now, timezone_name):
        from reminder_bot.domain import ParseResult

        return ParseResult(
            title="позвонить врачу",
            date="2026-07-17",
            time=None,
            needs_clarification=True,
            clarification_type="missing_time",
            parser_type="rule_based",
        )


class OpenAIParserTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 12, 15, 0, tzinfo=ZoneInfo("Europe/Riga"))

    @staticmethod
    def response(data):
        return {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": json.dumps(data)}],
                }
            ]
        }

    def test_structured_response_is_mapped_to_shared_schema(self):
        captured = {}
        parsed = {
            "title": "концерт",
            "date": "2026-07-13",
            "time": "19:00",
            "date_confidence": "high",
            "time_confidence": "high",
            "needs_clarification": False,
            "clarification_type": None,
            "clarification_options": [],
            "date_was_explicit": True,
            "time_was_explicit": True,
        }

        def request_func(request, timeout):
            captured["payload"] = json.loads(request.data)
            captured["timeout"] = timeout
            return self.response(parsed)

        parser = OpenAIParser("test-key", request_func=request_func)
        result = parser.parse("концерт завтра в 7 вечера", self.now, "Europe/Riga")
        self.assertEqual("2026-07-13", result.date)
        self.assertEqual("19:00", result.time)
        self.assertEqual("openai", result.parser_type)
        format_spec = captured["payload"]["text"]["format"]
        self.assertEqual("json_schema", format_spec["type"])
        self.assertTrue(format_spec["strict"])
        self.assertFalse(format_spec["schema"]["additionalProperties"])

    def test_hybrid_uses_openai_after_rule_parser_declines(self):
        data = {
            "title": "позвонить врачу",
            "date": "2026-07-17",
            "time": "15:00",
            "date_confidence": "high",
            "time_confidence": "high",
            "needs_clarification": False,
            "clarification_type": None,
            "clarification_options": [],
            "date_was_explicit": True,
            "time_was_explicit": True,
        }
        openai = OpenAIParser("test-key", request_func=lambda *_: self.response(data))
        result = HybridParser(FakeRuleParser(), openai).parse(
            "в пятницу после обеда позвонить врачу", self.now, "Europe/Riga"
        )
        self.assertEqual("openai", result.parser_type)

    def test_missing_output_is_rejected(self):
        parser = OpenAIParser("test-key", request_func=lambda *_: {"output": []})
        with self.assertRaises(RuntimeError):
            parser.parse("что-то", self.now, "Europe/Riga")

    def test_safety_guard_restores_ambiguous_time(self):
        unsafe = {
            "title": "снять стирку",
            "date": "2026-07-15",
            "time": "06:35",
            "date_confidence": "high",
            "time_confidence": "high",
            "needs_clarification": False,
            "clarification_type": None,
            "clarification_options": [],
            "date_was_explicit": True,
            "time_was_explicit": True,
        }
        parser = OpenAIParser("test-key", request_func=lambda *_: self.response(unsafe))
        result = parser.parse("снять стирку 15/07 в 6:35", self.now, "Europe/Riga")
        self.assertIsNone(result.time)
        self.assertTrue(result.needs_clarification)
        self.assertEqual(["06:35", "18:35"], result.clarification_options)

    def test_safety_guard_corrects_weekday_date(self):
        unsafe = {
            "title": "концерт",
            "date": "2026-07-15",
            "time": "19:00",
            "date_confidence": "high",
            "time_confidence": "high",
            "needs_clarification": False,
            "clarification_type": None,
            "clarification_options": [],
            "date_was_explicit": True,
            "time_was_explicit": True,
        }
        parser = OpenAIParser("test-key", request_func=lambda *_: self.response(unsafe))
        result = parser.parse("в пятницу концерт в 19:00", self.now, "Europe/Riga")
        self.assertEqual("2026-07-17", result.date)

    def test_hybrid_retries_missing_time_with_openai(self):
        data = {
            "title": "позвонить врачу",
            "date": "2026-07-17",
            "time": "15:00",
            "date_confidence": "high",
            "time_confidence": "high",
            "needs_clarification": False,
            "clarification_type": None,
            "clarification_options": [],
            "date_was_explicit": True,
            "time_was_explicit": True,
        }
        openai = OpenAIParser("test-key", request_func=lambda *_: self.response(data))
        result = HybridParser(MissingTimeRuleParser(), openai).parse(
            "в пятницу после обеда позвонить врачу", self.now, "Europe/Riga"
        )
        self.assertEqual("15:00", result.time)
        self.assertEqual("openai", result.parser_type)


if __name__ == "__main__":
    unittest.main()
