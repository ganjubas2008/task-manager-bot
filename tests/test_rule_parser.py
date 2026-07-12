from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from reminder_bot.domain import ClarificationType
from reminder_bot.parsers.rule_based import RuleBasedParser


class RuleBasedParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.parser = RuleBasedParser()
        self.now = datetime(2026, 7, 12, 15, 0, tzinfo=ZoneInfo("Europe/Riga"))

    def assert_parse(self, text, expected_date, expected_time, clarification=False):
        result = self.parser.parse(text, self.now, "Europe/Riga")
        self.assertEqual(expected_date, result.date, text)
        self.assertEqual(expected_time, result.time, text)
        self.assertEqual(clarification, result.needs_clarification, text)
        return result

    def test_today_tomorrow_and_day_after_tomorrow(self):
        cases = [
            ("концерт сегодня в 19:00", "2026-07-12"),
            ("концерт завтра в 19:00", "2026-07-13"),
            ("концерт послезавтра в 19:00", "2026-07-14"),
            ("завтра концерт в 19:00", "2026-07-13"),
            ("в 19:00 завтра концерт", "2026-07-13"),
        ]
        for text, expected_date in cases:
            with self.subTest(text=text):
                self.assert_parse(text, expected_date, "19:00")

    def test_supported_time_formats(self):
        cases = [
            ("концерт в 19:00", "19:00"),
            ("концерт в 19 00", "19:00"),
            ("концерт в 19.00", "19:00"),
            ("концерт в 7 утра", "07:00"),
            ("концерт в 7 вечера", "19:00"),
            ("концерт в полдень", "12:00"),
            ("концерт в полночь", "00:00"),
            ("концерт в 19", "19:00"),
            ("концерт в двенадцать", "12:00"),
        ]
        for text, expected_time in cases:
            with self.subTest(text=text):
                result = self.parser.parse(text, self.now, "Europe/Riga")
                self.assertEqual(expected_time, result.time)
                self.assertFalse(result.needs_clarification)

    def test_past_time_without_date_moves_to_tomorrow(self):
        self.assert_parse("концерт в 14:00", "2026-07-13", "14:00")

    def test_ambiguous_times_are_never_guessed(self):
        cases = [
            ("концерт в 7", ["07:00", "19:00"]),
            ("снять стирку в 6:35", ["06:35", "18:35"]),
            ("позвонить в 9", ["09:00", "21:00"]),
            ("встреча в 11", ["11:00", "23:00"]),
        ]
        for text, options in cases:
            with self.subTest(text=text):
                result = self.parser.parse(text, self.now, "Europe/Riga")
                self.assertTrue(result.needs_clarification)
                self.assertEqual(ClarificationType.AMBIGUOUS_TIME, result.clarification_type)
                self.assertEqual(options, result.clarification_options)
                self.assertIsNone(result.time)

    def test_relative_time(self):
        cases = [
            ("напомни через 5 минут", "2026-07-12", "15:05"),
            ("напомни через 20 минут снять чайник", "2026-07-12", "15:20"),
            ("через час позвонить маме", "2026-07-12", "16:00"),
            ("через 2 часа проверить сервер", "2026-07-12", "17:00"),
            ("через сутки проверить результат", "2026-07-13", "15:00"),
        ]
        for text, expected_date, expected_time in cases:
            with self.subTest(text=text):
                self.assert_parse(text, expected_date, expected_time)

    def test_numeric_and_named_dates(self):
        cases = [
            ("концерт 15/07 в 19:00", "2026-07-15"),
            ("концерт 15.07 в 19:00", "2026-07-15"),
            ("концерт 15-07 в 19:00", "2026-07-15"),
            ("концерт 15 июля в 19:00", "2026-07-15"),
            ("концерт 15 июля 2027 в 19:00", "2027-07-15"),
        ]
        for text, expected_date in cases:
            with self.subTest(text=text):
                self.assert_parse(text, expected_date, "19:00")

    def test_date_without_year_uses_nearest_future_year(self):
        now = datetime(2026, 7, 20, 10, 0, tzinfo=ZoneInfo("Europe/Riga"))
        result = self.parser.parse("концерт 15/07 в 19:00", now, "Europe/Riga")
        self.assertEqual("2027-07-15", result.date)

        same_day_late = datetime(2026, 7, 20, 20, 0, tzinfo=ZoneInfo("Europe/Riga"))
        result = self.parser.parse("концерт 20/07 в 19:00", same_day_late, "Europe/Riga")
        self.assertEqual("2027-07-20", result.date)

    def test_weekdays(self):
        cases = [
            ("концерт в пятницу в 19:00", "2026-07-17", "19:00"),
            ("в пятницу концерт в 19:00", "2026-07-17", "19:00"),
            ("следующий понедельник в 8 утра зал", "2026-07-13", "08:00"),
            ("в эту субботу в 12 встреча", "2026-07-18", "12:00"),
        ]
        for text, expected_date, expected_time in cases:
            with self.subTest(text=text):
                self.assert_parse(text, expected_date, expected_time)

    def test_month_and_year_boundaries(self):
        january = datetime(2026, 1, 31, 9, 0, tzinfo=ZoneInfo("Europe/Riga"))
        result = self.parser.parse("завтра в 10:00 встреча", january, "Europe/Riga")
        self.assertEqual("2026-02-01", result.date)

        december = datetime(2026, 12, 31, 9, 0, tzinfo=ZoneInfo("Europe/Riga"))
        result = self.parser.parse("через 2 дня встреча", december, "Europe/Riga")
        self.assertEqual("2027-01-02", result.date)
        self.assertEqual(ClarificationType.MISSING_TIME, result.clarification_type)

    def test_leap_day_uses_next_valid_year(self):
        result = self.parser.parse("29 февраля в 19:00 встреча", self.now, "Europe/Riga")
        self.assertEqual("2028-02-29", result.date)

    def test_invalid_dates_request_clarification(self):
        for text in (
            "31 февраля в 19:00 встреча",
            "32 июля в 19:00 встреча",
            "15/15 в 19:00 встреча",
        ):
            with self.subTest(text=text):
                result = self.parser.parse(text, self.now, "Europe/Riga")
                self.assertTrue(result.needs_clarification)
                self.assertEqual(ClarificationType.INVALID_DATE, result.clarification_type)

    def test_missing_time_requests_it(self):
        for text in ("завтра концерт", "15 июля концерт", "в пятницу встреча"):
            with self.subTest(text=text):
                result = self.parser.parse(text, self.now, "Europe/Riga")
                self.assertEqual(ClarificationType.MISSING_TIME, result.clarification_type)
                self.assertTrue(result.needs_clarification)

    def test_order_of_words_does_not_change_result(self):
        for text in (
            "концерт завтра в 19:00",
            "завтра концерт в 19:00",
            "в 19:00 завтра концерт",
            "в 19:00 концерт завтра",
        ):
            with self.subTest(text=text):
                self.assert_parse(text, "2026-07-13", "19:00")

    def test_unrelated_numbers_are_not_time(self):
        cases = [
            ("купить 2 билета на концерт 15/07 в 19:00", "19:00"),
            ("позвонить в офис 123 в 18:00", "18:00"),
            ("тренировка 5 км завтра в 7 утра", "07:00"),
        ]
        for text, expected_time in cases:
            with self.subTest(text=text):
                result = self.parser.parse(text, self.now, "Europe/Riga")
                self.assertEqual(expected_time, result.time)

    def test_title_is_cleaned(self):
        result = self.parser.parse(
            "напомни мне завтра в 19:00 позвонить маме", self.now, "Europe/Riga"
        )
        self.assertEqual("позвонить маме", result.title)


if __name__ == "__main__":
    unittest.main()
