from __future__ import annotations

import unittest
from datetime import datetime, timezone

from reminder_bot.domain import ClarificationType, ParseResult
from reminder_bot.validation import ValidationError, validate_and_build_datetime, validate_timezone


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)

    def result(self, date="2026-07-13", time="19:00"):
        return ParseResult(title="концерт", date=date, time=time, parser_type="test")

    def test_valid_result_is_converted_to_utc(self):
        actual = validate_and_build_datetime(self.result(), self.now, "Europe/Moscow")
        self.assertEqual(datetime(2026, 7, 13, 16, 0, tzinfo=timezone.utc), actual)

    def test_impossible_date_is_rejected(self):
        with self.assertRaises(ValidationError) as caught:
            validate_and_build_datetime(self.result("2026-02-31"), self.now, "Europe/Moscow")
        self.assertEqual(ClarificationType.INVALID_DATE, caught.exception.code)

    def test_invalid_time_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_and_build_datetime(self.result(time="25:00"), self.now, "Europe/Moscow")

    def test_noncanonical_formats_are_rejected(self):
        with self.assertRaises(ValidationError):
            validate_and_build_datetime(self.result(date="20260713"), self.now, "Europe/Moscow")
        with self.assertRaises(ValidationError):
            validate_and_build_datetime(self.result(time="19:00:00"), self.now, "Europe/Moscow")

    def test_past_datetime_is_rejected(self):
        with self.assertRaises(ValidationError) as caught:
            validate_and_build_datetime(self.result("2025-07-13"), self.now, "Europe/Moscow")
        self.assertEqual(ClarificationType.PAST_DATETIME, caught.exception.code)

    def test_invalid_timezone_is_rejected(self):
        with self.assertRaises(ValidationError):
            validate_timezone("Mars/Olympus")

    def test_nonexistent_dst_time_is_rejected(self):
        now = datetime(2026, 3, 1, tzinfo=timezone.utc)
        with self.assertRaises(ValidationError) as caught:
            validate_and_build_datetime(
                self.result("2026-03-29", "02:30"), now, "Europe/Berlin"
            )
        self.assertEqual(ClarificationType.DST_TIME, caught.exception.code)

    def test_ambiguous_dst_time_is_rejected(self):
        now = datetime(2026, 10, 1, tzinfo=timezone.utc)
        with self.assertRaises(ValidationError) as caught:
            validate_and_build_datetime(
                self.result("2026-10-25", "02:30"), now, "Europe/Berlin"
            )
        self.assertEqual(ClarificationType.DST_TIME, caught.exception.code)


if __name__ == "__main__":
    unittest.main()
