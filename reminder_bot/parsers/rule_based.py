from __future__ import annotations

import calendar
import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from reminder_bot.domain import (
    ClarificationType,
    Confidence,
    ParseResult,
    UnsupportedInput,
)


MONTHS = {
    "января": 1,
    "январь": 1,
    "февраля": 2,
    "февраль": 2,
    "марта": 3,
    "март": 3,
    "апреля": 4,
    "апрель": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июля": 7,
    "июль": 7,
    "августа": 8,
    "август": 8,
    "сентября": 9,
    "сентябрь": 9,
    "октября": 10,
    "октябрь": 10,
    "ноября": 11,
    "ноябрь": 11,
    "декабря": 12,
    "декабрь": 12,
}

WEEKDAYS = {
    "понедельник": 0,
    "понедельника": 0,
    "понедельнике": 0,
    "вторник": 1,
    "вторника": 1,
    "вторнике": 1,
    "среда": 2,
    "среду": 2,
    "среды": 2,
    "четверг": 3,
    "четверга": 3,
    "четверге": 3,
    "пятница": 4,
    "пятницу": 4,
    "пятницы": 4,
    "суббота": 5,
    "субботу": 5,
    "субботы": 5,
    "воскресенье": 6,
    "воскресенья": 6,
}

NUMBER_WORDS = {
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "час": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
}

DATE_TOKEN = "__ДАТА__"
TIME_TOKEN = "__ВРЕМЯ__"


class RuleBasedParser:
    """Детерминированный parser для поддерживаемых русских конструкций."""

    parser_type = "rule_based"

    def parse(self, text: str, now: datetime, timezone_name: str) -> ParseResult:
        if not text or not text.strip():
            raise UnsupportedInput("Пустой запрос")
        if now.tzinfo is None:
            now = now.replace(tzinfo=ZoneInfo(timezone_name))
        else:
            now = now.astimezone(ZoneInfo(timezone_name))

        normalized = self._normalize(text)
        relative = self._parse_relative_datetime(normalized, now)
        if relative:
            target, matched = relative
            title = self._clean_title(normalized.replace(matched, f" {DATE_TOKEN} {TIME_TOKEN} "))
            return ParseResult(
                title=title,
                date=target.date().isoformat(),
                time=target.strftime("%H:%M"),
                date_was_explicit=True,
                time_was_explicit=True,
                parser_type=self.parser_type,
            )

        parsed_date, date_match, date_explicit, invalid_date = self._parse_date(normalized, now)
        if invalid_date:
            return ParseResult(
                title=self._clean_title(normalized.replace(date_match or "", f" {DATE_TOKEN} ")),
                date=None,
                time=None,
                date_confidence=Confidence.LOW,
                time_confidence=Confidence.LOW,
                needs_clarification=True,
                clarification_type=ClarificationType.INVALID_DATE,
                date_was_explicit=True,
                parser_type=self.parser_type,
            )

        # Убираем распознанную дату до поиска времени: иначе 15.07 можно
        # ошибочно прочитать как 15:07.
        time_source = normalized
        if date_match:
            time_source = time_source.replace(date_match, " ", 1)
        parsed_time, time_match, time_explicit, ambiguous_options = self._parse_time(time_source)
        working = normalized
        if date_match:
            working = working.replace(date_match, f" {DATE_TOKEN} ", 1)
        if time_match:
            working = working.replace(time_match, f" {TIME_TOKEN} ", 1)
        title = self._clean_title(working)

        if parsed_date is None and parsed_time is None and not ambiguous_options:
            raise UnsupportedInput("Дата и время не распознаны")

        if ambiguous_options:
            return ParseResult(
                title=title,
                date=parsed_date.isoformat() if parsed_date else None,
                time=None,
                time_confidence=Confidence.LOW,
                needs_clarification=True,
                clarification_type=ClarificationType.AMBIGUOUS_TIME,
                clarification_options=ambiguous_options,
                date_was_explicit=date_explicit,
                time_was_explicit=True,
                parser_type=self.parser_type,
            )

        if parsed_time is None:
            return ParseResult(
                title=title,
                date=parsed_date.isoformat() if parsed_date else None,
                time=None,
                time_confidence=Confidence.LOW,
                needs_clarification=True,
                clarification_type=ClarificationType.MISSING_TIME,
                date_was_explicit=date_explicit,
                time_was_explicit=False,
                parser_type=self.parser_type,
            )

        if parsed_date is None:
            candidate = datetime.combine(now.date(), parsed_time, tzinfo=now.tzinfo)
            if candidate <= now:
                candidate += timedelta(days=1)
            parsed_date = candidate.date()
        else:
            # День недели без модификатора может означать сегодня. Если время
            # уже прошло, выбираем следующую неделю.
            candidate = datetime.combine(parsed_date, parsed_time, tzinfo=now.tzinfo)
            if candidate <= now:
                if self._is_weekday_match(date_match):
                    parsed_date += timedelta(days=7)
                elif self._is_yearless_calendar_date(date_match):
                    parsed_date = self._next_valid_annual_date(parsed_date, now.date())

        return ParseResult(
            title=title,
            date=parsed_date.isoformat(),
            time=parsed_time.strftime("%H:%M"),
            date_was_explicit=date_explicit,
            time_was_explicit=time_explicit,
            parser_type=self.parser_type,
        )

    @staticmethod
    def _normalize(text: str) -> str:
        value = text.lower().replace("ё", "е")
        value = re.sub(r"\s+", " ", value).strip()
        return value

    def _parse_relative_datetime(self, text: str, now: datetime):
        pattern = re.compile(
            r"\bчерез\s+(?P<n>\d+|[а-я]+)\s*"
            r"(?P<unit>минут(?:у|ы)?|час(?:а|ов)?|сут(?:ки|ок))\b"
        )
        match = pattern.search(text)
        if not match:
            # «через час» уже покрывается словарём, но отдельная ветка делает
            # намерение очевидным и устойчивым к окончаниям.
            match = re.search(r"\bчерез\s+час\b", text)
            if match:
                return now + timedelta(hours=1), match.group(0)
            match = re.search(r"\bчерез\s+сутки\b", text)
            if match:
                return now + timedelta(days=1), match.group(0)
            return None

        raw_number = match.group("n")
        number = int(raw_number) if raw_number.isdigit() else NUMBER_WORDS.get(raw_number)
        if number is None or number < 1:
            return None
        unit = match.group("unit")
        if unit.startswith("минут"):
            delta = timedelta(minutes=number)
        elif unit.startswith("час"):
            delta = timedelta(hours=number)
        else:
            delta = timedelta(days=number)
        return now + delta, match.group(0)

    def _parse_date(self, text: str, now: datetime):
        relative_dates = (
            (r"\bпослезавтра\b", 2),
            (r"\bзавтра\b", 1),
            (r"\bсегодня\b", 0),
            (r"\bчерез\s+недел(?:ю|и)\b", 7),
        )
        for pattern, days in relative_dates:
            match = re.search(pattern, text)
            if match:
                return now.date() + timedelta(days=days), match.group(0), True, False

        days_match = re.search(r"\bчерез\s+(\d+)\s+д(?:ень|ня|ней)\b", text)
        if days_match:
            return (
                now.date() + timedelta(days=int(days_match.group(1))),
                days_match.group(0),
                True,
                False,
            )

        numeric = None
        numeric_pattern = re.compile(
            r"(?<!\d)(\d{1,2})[./-](\d{1,2})(?:[./-](\d{4}))?(?!\d)"
        )
        for candidate in numeric_pattern.finditer(text):
            # В конструкции «в 19.00» точка является разделителем времени.
            if re.search(r"\bв\s*$", text[: candidate.start()]):
                continue
            numeric = candidate
            break
        if numeric:
            day, month = int(numeric.group(1)), int(numeric.group(2))
            year = int(numeric.group(3)) if numeric.group(3) else None
            parsed, invalid = self._future_date(day, month, year, now.date())
            return parsed, numeric.group(0), True, invalid

        month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
        named = re.search(rf"(?<!\d)(\d{{1,2}})\s+({month_names})(?:\s+(\d{{4}}))?\b", text)
        if named:
            day, month = int(named.group(1)), MONTHS[named.group(2)]
            year = int(named.group(3)) if named.group(3) else None
            parsed, invalid = self._future_date(day, month, year, now.date())
            return parsed, named.group(0), True, invalid

        weekday_names = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
        weekday = re.search(
            rf"\b(?:(следующ(?:ий|ую|ее)|эт(?:от|у|о))\s+)?({weekday_names})\b",
            text,
        )
        if weekday:
            modifier, name = weekday.group(1), weekday.group(2)
            target_weekday = WEEKDAYS[name]
            delta = (target_weekday - now.weekday()) % 7
            if modifier and modifier.startswith("следующ"):
                delta = delta or 7
            parsed = now.date() + timedelta(days=delta)
            return parsed, weekday.group(0), True, False

        return None, None, False, False

    @staticmethod
    def _future_date(day: int, month: int, year: int | None, today: date):
        if month < 1 or month > 12 or day < 1:
            return None, True
        if year is not None:
            try:
                return date(year, month, day), False
            except ValueError:
                return None, True

        for candidate_year in (today.year, today.year + 1):
            try:
                candidate = date(candidate_year, month, day)
            except ValueError:
                # 29 февраля может быть валидным в более позднем високосном году.
                if month == 2 and day == 29:
                    for leap_year in range(candidate_year + 1, candidate_year + 9):
                        if calendar.isleap(leap_year):
                            candidate = date(leap_year, month, day)
                            if candidate >= today:
                                return candidate, False
                    return None, True
                return None, True
            if candidate >= today:
                return candidate, False
        return None, True

    def _parse_time(self, text: str):
        noon = re.search(r"\bполдень\b", text)
        if noon:
            return time(12, 0), noon.group(0), True, []
        midnight = re.search(r"\bполночь\b", text)
        if midnight:
            return time(0, 0), midnight.group(0), True, []

        qualified = re.search(
            r"\b(?:в\s+)?(\d{1,2}|[а-я]+)(?::|[ .])(\d{2})\s*(утра|вечера|дня|ночи)\b"
            r"|\b(?:в\s+)?(\d{1,2}|[а-я]+)\s*(утра|вечера|дня|ночи)\b",
            text,
        )
        if qualified:
            raw_hour = qualified.group(1) or qualified.group(4)
            qualifier = qualified.group(3) or qualified.group(5)
            minute = int(qualified.group(2) or 0)
            hour = self._number(raw_hour)
            converted = self._qualified_hour(hour, qualifier)
            if converted is None or minute > 59:
                return None, qualified.group(0), True, []
            return time(converted, minute), qualified.group(0), True, []

        # Разделитель обязателен, чтобы числа в «купить 2 билета» не стали временем.
        digital = re.search(r"\b(?:в\s+)?(\d{1,2})(?::|\.|\s)(\d{2})\b", text)
        if digital:
            hour, minute = int(digital.group(1)), int(digital.group(2))
            if hour > 23 or minute > 59:
                return None, digital.group(0), True, []
            if 1 <= hour <= 11:
                return None, digital.group(0), True, [f"{hour:02d}:{minute:02d}", f"{hour + 12:02d}:{minute:02d}"]
            return time(hour, minute), digital.group(0), True, []

        word_names = "|".join(sorted(NUMBER_WORDS, key=len, reverse=True))
        simple = re.search(rf"\bв\s+(\d{{1,2}}|{word_names})\b", text)
        if simple:
            hour = self._number(simple.group(1))
            if hour is None or hour > 23:
                return None, simple.group(0), True, []
            if 1 <= hour <= 11:
                return None, simple.group(0), True, [f"{hour:02d}:00", f"{hour + 12:02d}:00"]
            return time(hour, 0), simple.group(0), True, []

        return None, None, False, []

    @staticmethod
    def _number(value: str) -> int | None:
        return int(value) if value.isdigit() else NUMBER_WORDS.get(value)

    @staticmethod
    def _qualified_hour(hour: int | None, qualifier: str) -> int | None:
        if hour is None or hour < 0 or hour > 23:
            return None
        if qualifier in {"вечера", "дня"} and 1 <= hour <= 11:
            return hour + 12
        if qualifier == "ночи" and hour == 12:
            return 0
        if qualifier == "утра" and hour == 12:
            return 0
        return hour

    @staticmethod
    def _is_weekday_match(match: str | None) -> bool:
        return bool(match and any(name in match for name in WEEKDAYS))

    @staticmethod
    def _is_yearless_calendar_date(match: str | None) -> bool:
        if not match or re.search(r"\b\d{4}\b", match):
            return False
        return bool(re.search(r"\d{1,2}[./-]\d{1,2}", match) or any(name in match for name in MONTHS))

    @staticmethod
    def _next_valid_annual_date(value: date, today: date) -> date:
        for year in range(max(value.year + 1, today.year), today.year + 9):
            try:
                candidate = date(year, value.month, value.day)
            except ValueError:
                continue
            if candidate > today:
                return candidate
        raise ValueError("Не удалось найти следующую календарную дату")

    @staticmethod
    def _clean_title(text: str) -> str:
        value = text.replace(DATE_TOKEN, " ").replace(TIME_TOKEN, " ")
        value = re.sub(r"\b(?:напомни(?:ть)?|мне|пожалуйста|не забудь|надо бы)\b", " ", value)
        value = re.sub(r"\b(?:в|на|к)\b(?=\s*$)", " ", value)
        value = re.sub(r"^[,;:\s]+|[,;:\s]+$", "", value)
        value = re.sub(r"\s+", " ", value).strip()
        return value or "Напоминание"
