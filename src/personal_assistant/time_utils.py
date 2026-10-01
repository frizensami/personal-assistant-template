from __future__ import annotations

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import get_close_matches
from typing import Literal
from zoneinfo import ZoneInfo

import dateparser
from dateparser.search import search_dates
from dateutil import parser as date_parser


NUMBER_WORDS = {
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
NUMBER_VALUE_PATTERN = r"(?:\d+|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
RELATIVE_UNIT_PATTERN = r"(?:minutes?|mins?|min|m|hours?|hrs?|hr|h|days?|d)"
DURATION_UNIT_PATTERN = r"(?:minutes?|mins?|min|m|hours?|hrs?|hr|h)"

RELATIVE_RE = re.compile(
    rf"\b(?:in|after)\s+(?P<value>{NUMBER_VALUE_PATTERN})\s*(?P<unit>{RELATIVE_UNIT_PATTERN})\b",
    re.IGNORECASE,
)

DURATION_RE = re.compile(
    rf"(?P<value>{NUMBER_VALUE_PATTERN})\s*(?P<unit>{DURATION_UNIT_PATTERN})\b",
    re.IGNORECASE,
)

FROM_NOW_RE = re.compile(
    rf"\b(?P<value>{NUMBER_VALUE_PATTERN})\s*(?P<unit>{RELATIVE_UNIT_PATTERN})\s+from\s+now\b",
    re.IGNORECASE,
)

DURATION_ONLY_RE = re.compile(
    rf"^(?P<value>{NUMBER_VALUE_PATTERN})\s*(?P<unit>{RELATIVE_UNIT_PATTERN})$",
    re.IGNORECASE,
)

WEEKDAY_RE = re.compile(
    r"\b(?:(?P<modifier>this|next)\s+)?(?P<weekday>mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:rs|rsday|rday)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b",
    re.IGNORECASE,
)

TIME_RE = re.compile(
    r"\b(?:at\s+)?(?P<hour>\d{1,2})(?:(?::(?P<minute>\d{2}))|(?P<compact_minute>\d{2}))?\s*(?P<meridiem>am|pm)?\b",
    re.IGNORECASE,
)

AMPM_COMPACT_RE = re.compile(r"\b(?P<hour>\d{1,2})(?P<meridiem>a|p)\b", re.IGNORECASE)
COMPACT_TIME_RE = re.compile(r"\b(?P<hour>\d{1,2})(?P<minute>\d{2})(?P<meridiem>am|pm)\b", re.IGNORECASE)
FOUR_DIGIT_TIME_RE = re.compile(r"\b(?P<hour>[01]?\d|2[0-3])(?P<minute>[0-5]\d)\b")

FUZZY_VOCAB = (
    "today",
    "tomorrow",
    "tonight",
    "morning",
    "afternoon",
    "evening",
    "noon",
    "midnight",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "next",
    "this",
    "minute",
    "minutes",
    "hour",
    "hours",
    "day",
    "days",
)

TOKEN_NORMALIZATIONS = {
    "tmr": "tomorrow",
    "tmrw": "tomorrow",
    "tmorrow": "tomorrow",
    "tomorow": "tomorrow",
    "tod": "today",
    "nite": "night",
    "tonite": "tonight",
    "mins": "minutes",
    "mins.": "minutes",
    "min": "minute",
    "hrs": "hours",
    "hr": "hour",
    "mon": "monday",
    "tue": "tuesday",
    "tues": "tuesday",
    "wed": "wednesday",
    "thu": "thursday",
    "thur": "thursday",
    "thurs": "thursday",
    "fri": "friday",
    "sat": "saturday",
    "sun": "sunday",
}

WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

DAYPART_DEFAULTS = {
    "morning": (9, 0),
    "afternoon": (15, 0),
    "evening": (19, 0),
    "tonight": (21, 0),
    "noon": (12, 0),
    "midnight": (0, 0),
}

MONTH_NAME_RE = re.compile(
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\b",
    re.IGNORECASE,
)
AMBIGUOUS_NUMERIC_DATE_RE = re.compile(
    r"\b(?P<first>\d{1,2})[/-](?P<second>\d{1,2})(?:[/-](?P<year>\d{2,4}))?(?:\s+(?P<tail>\d{1,2}(?::\d{2})?\s*(?:am|pm)?))?\b",
    re.IGNORECASE,
)
COARSE_PERIOD_RE = re.compile(
    r"^(?:(?P<modifier>this|next)\s+)?(?P<unit>week|month|year|weekend)$",
    re.IGNORECASE,
)
MONTH_ONLY_RE = re.compile(
    r"^(?:in\s+)?(?P<month>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|"
    r"sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)(?:\s+(?P<year>\d{4}))?$",
    re.IGNORECASE,
)
YEAR_ONLY_RE = re.compile(r"^\d{4}$")
RANGE_TO_RE = re.compile(
    r"^(?:from\s+)?(?P<start>.+?)(?:\s*-\s*|\s+(?:to|until|til|through|thru)\s+)(?P<end>.+)$",
    re.IGNORECASE,
)
RANGE_BETWEEN_RE = re.compile(r"^between\s+(?P<start>.+?)\s+and\s+(?P<end>.+)$", re.IGNORECASE)
EXPLICIT_TIME_RE = re.compile(
    r"\b\d{1,2}(?::\d{2})?\s*(?:am|pm)\b|\b(?:[01]?\d|2[0-3]):[0-5]\d\b|"
    r"\b(?:at\s+)?(?:[01]?\d|2[0-3])[0-5]\d\b|\b\d{3,4}(?:am|pm)\b|"
    r"\b(?:morning|afternoon|evening|tonight|noon|midnight)\b",
    re.IGNORECASE,
)
EXPLICIT_DATE_RE = re.compile(
    r"\b(today|tomorrow|yesterday|tonight|day after tomorrow|day before yesterday|"
    r"monday|tuesday|wednesday|thursday|friday|saturday|sunday|next|this)\b|"
    r"\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|"
    r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b",
    re.IGNORECASE,
)

ParseStatus = Literal["ok", "ambiguous", "invalid"]


@dataclass(frozen=True)
class DateTimeInterpretation:
    status: ParseStatus
    value: datetime | None = None
    normalized_text: str = ""
    prompt: str = ""
    note: str = ""
    has_explicit_time: bool = False
    has_explicit_date: bool = False


@dataclass(frozen=True)
class DateRangeInterpretation:
    status: ParseStatus
    start: datetime | None = None
    end: datetime | None = None
    normalized_text: str = ""
    prompt: str = ""


def now_utc() -> datetime:
    return datetime.now(tz=ZoneInfo("UTC"))


def ensure_timezone(value: datetime, timezone_name: str) -> datetime:
    zone = ZoneInfo(timezone_name)
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def _normalize_datetime_text(text: str) -> str:
    def normalize_word(match: re.Match[str]) -> str:
        word = match.group(0)
        lowered = word.lower()
        if lowered in TOKEN_NORMALIZATIONS:
            return TOKEN_NORMALIZATIONS[lowered]
        if len(lowered) >= 4:
            close = get_close_matches(lowered, FUZZY_VOCAB, n=1, cutoff=0.88)
            if close:
                return close[0]
        return lowered

    normalized = text.strip().lower()
    normalized = COMPACT_TIME_RE.sub(r"\g<hour>:\g<minute> \g<meridiem>", normalized)
    normalized = AMPM_COMPACT_RE.sub(
        lambda match: f"{match.group('hour')} {'am' if match.group('meridiem').lower() == 'a' else 'pm'}",
        normalized,
    )
    normalized = re.sub(
        r"\b(?P<hour>\d{1,2})\.(?P<minute>[0-5]\d)\s*(?P<meridiem>am|pm)\b",
        r"\g<hour>:\g<minute> \g<meridiem>",
        normalized,
    )
    normalized = re.sub(r"\b(?P<hour>\d{1,2})\.(?P<minute>\d{2})\b", r"\g<hour>:\g<minute>", normalized)
    normalized = re.sub(
        r"\b(?P<hour>\d{1,2})\s+(?P<minute>[0-5]\d)\s*(?P<meridiem>am|pm)\b",
        r"\g<hour>:\g<minute> \g<meridiem>",
        normalized,
    )
    normalized = normalized.replace(",", " ")
    normalized = normalized.replace(".", " ")
    normalized = normalized.replace("'", "")
    normalized = re.sub(r"\b(?P<value>\d+)(?P<unit>[mhd])\b", r"\g<value> \g<unit>", normalized)
    normalized = re.sub(r"\b(?P<value>\d+)(?P<unit>am|pm)\b", r"\g<value> \g<unit>", normalized)
    normalized = re.sub(r"\b[a-z]+\b", normalize_word, normalized)
    normalized = re.sub(r"\bnoon\b", "12 pm", normalized)
    normalized = re.sub(r"\bmidnight\b", "12 am", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.strip()


def _relative_delta(value: int, unit: str) -> timedelta:
    unit = unit.lower()
    if unit.startswith("min") or unit == "m":
        return timedelta(minutes=value)
    if unit.startswith("hour") or unit.startswith("hr") or unit == "h":
        return timedelta(hours=value)
    return timedelta(days=value)


def _parse_number_value(raw: str) -> int:
    lowered = raw.strip().lower()
    if lowered.isdigit():
        return int(lowered)
    return NUMBER_WORDS.get(lowered, 0)


def _has_relative_duration(text: str) -> bool:
    normalized = _normalize_datetime_text(text)
    return bool(
        RELATIVE_RE.search(normalized)
        or FROM_NOW_RE.search(normalized)
        or DURATION_ONLY_RE.fullmatch(normalized)
    )


def _extract_time_components(text: str) -> tuple[int, int] | None:
    lowered = text.lower()
    explicit = TIME_RE.search(lowered)
    if explicit:
        hour = int(explicit.group("hour"))
        minute_text = explicit.group("minute") or explicit.group("compact_minute")
        minute = int(minute_text) if minute_text else 0
        meridiem = explicit.group("meridiem")
        if minute > 59 or hour > 23 or (meridiem and hour > 12):
            return None
        if meridiem:
            meridiem = meridiem.lower()
            if hour == 12:
                hour = 0
            if meridiem == "pm":
                hour += 12
        return hour % 24, minute
    for daypart, value in DAYPART_DEFAULTS.items():
        if daypart in lowered:
            return value
    return None


def _combine_with_day(base: datetime, target_date: datetime, text: str) -> datetime:
    time_value = _extract_time_components(text)
    if time_value is None:
        if text.strip() == "today":
            return base
        if text.strip() == "tomorrow":
            return base + timedelta(days=1)
        return target_date.replace(hour=base.hour, minute=base.minute, second=0, microsecond=0)
    hour, minute = time_value
    return target_date.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _dateparser_settings(base: datetime, timezone_name: str, *, date_order: str | None = None) -> dict[str, object]:
    settings: dict[str, object] = {
        "RELATIVE_BASE": base,
        "TIMEZONE": timezone_name,
        "TO_TIMEZONE": timezone_name,
        "RETURN_AS_TIMEZONE_AWARE": True,
        "PREFER_DATES_FROM": "future",
        "PREFER_DAY_OF_MONTH": "first",
    }
    if date_order:
        settings["DATE_ORDER"] = date_order
    return settings


def _parse_user_datetime_guess(
    text: str,
    timezone_name: str,
    *,
    reference: datetime | None = None,
    date_order: str | None = None,
) -> datetime | None:
    base = ensure_timezone(reference or now_utc(), timezone_name)
    raw = text.strip()
    if not raw:
        return None
    try:
        return ensure_timezone(datetime.fromisoformat(raw), timezone_name)
    except ValueError:
        pass

    if re.search(r"\b\d{1,4}[/-]\d{1,2}(?:[/-]\d{1,4})?\b", raw) or MONTH_NAME_RE.search(raw):
        parsed = dateparser.parse(
            raw,
            settings=_dateparser_settings(base, timezone_name, date_order=date_order),
            languages=["en"],
        )
        if parsed is not None:
            return ensure_timezone(parsed, timezone_name)

    stripped = _normalize_datetime_text(raw)
    if not stripped:
        return None

    if stripped == "today":
        return base
    if stripped == "tomorrow":
        if base.hour < 4:
            return base
        return base + timedelta(days=1)
    if stripped == "day after tomorrow":
        return base + timedelta(days=2)
    if stripped == "tonight":
        return base.replace(hour=21, minute=0, second=0, microsecond=0)

    relative_match = RELATIVE_RE.search(stripped)
    if relative_match:
        return base + _relative_delta(_parse_number_value(relative_match.group("value")), relative_match.group("unit"))

    from_now_match = FROM_NOW_RE.search(stripped)
    if from_now_match:
        return base + _relative_delta(_parse_number_value(from_now_match.group("value")), from_now_match.group("unit"))

    duration_only_match = DURATION_ONLY_RE.fullmatch(stripped)
    if duration_only_match:
        return base + _relative_delta(
            _parse_number_value(duration_only_match.group("value")),
            duration_only_match.group("unit"),
        )

    if "day after tomorrow" in stripped:
        target = base + timedelta(days=2)
        return _combine_with_day(base, target, stripped)

    if "tomorrow" in stripped:
        target = base if base.hour < 4 else base + timedelta(days=1)
        return _combine_with_day(base, target, stripped)

    if "today" in stripped:
        return _combine_with_day(base, base, stripped)

    weekday_match = WEEKDAY_RE.search(stripped)
    if weekday_match:
        weekday_name = weekday_match.group("weekday").lower()
        modifier = (weekday_match.group("modifier") or "").lower()
        target_idx = WEEKDAY_INDEX[weekday_name]
        delta_days = (target_idx - base.weekday()) % 7
        if modifier == "next" and delta_days == 0:
            delta_days = 7
        target = base + timedelta(days=delta_days)
        return _combine_with_day(base, target, stripped)

    compact_only = re.fullmatch(r"(?:at\s+)?(?P<time>\d{3,4})", stripped)
    if compact_only:
        stripped_time = compact_only.group("time")
    else:
        stripped_time = stripped
    if re.fullmatch(r"\d{3,4}", stripped_time):
        compact_time = FOUR_DIGIT_TIME_RE.fullmatch(stripped_time)
        if compact_time:
            hour = int(compact_time.group("hour"))
            minute = int(compact_time.group("minute"))
            candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if candidate <= base:
                candidate += timedelta(days=1)
            return candidate

    time_only = _extract_time_components(stripped)
    if time_only is not None and re.search(r"\b(am|pm|morning|afternoon|evening|tonight|\d{1,2}:\d{2})\b", stripped):
        hour, minute = time_only
        candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= base:
            candidate += timedelta(days=1)
        return candidate

    parsed = dateparser.parse(
        stripped,
        settings=_dateparser_settings(base, timezone_name, date_order=date_order),
        languages=["en"],
    )
    if parsed is not None:
        return ensure_timezone(parsed, timezone_name)

    try:
        parsed = date_parser.parse(stripped, fuzzy=True, default=base.replace(minute=0, second=0, microsecond=0))
    except (ValueError, OverflowError):
        return None
    return ensure_timezone(parsed, timezone_name)


def _has_explicit_time(text: str) -> bool:
    normalized = _normalize_datetime_text(text)
    return bool(EXPLICIT_TIME_RE.search(normalized) or _has_relative_duration(normalized))


def _has_explicit_date(text: str) -> bool:
    normalized = _normalize_datetime_text(text)
    if EXPLICIT_DATE_RE.search(normalized) or MONTH_NAME_RE.search(normalized):
        return True
    return bool(RELATIVE_RE.search(normalized) or FROM_NOW_RE.search(normalized) or DURATION_ONLY_RE.fullmatch(normalized))


def _format_prompt_datetime(value: datetime, timezone_name: str, *, include_time: bool) -> str:
    local_value = ensure_timezone(value, timezone_name)
    if include_time:
        return local_value.strftime("%d %b %Y %I:%M %p")
    return local_value.strftime("%d %b %Y")


def _ambiguous_numeric_date_prompt(raw: str, timezone_name: str, *, reference: datetime | None = None) -> str | None:
    if MONTH_NAME_RE.search(raw):
        return None
    base = ensure_timezone(reference or now_utc(), timezone_name)
    for match in AMBIGUOUS_NUMERIC_DATE_RE.finditer(raw):
        fragment = match.group(0)
        if fragment.count("/") + fragment.count("-") == 0:
            continue
        first = int(match.group("first"))
        second = int(match.group("second"))
        if first > 12 or second > 12:
            continue
        dmy = dateparser.parse(
            fragment,
            settings=_dateparser_settings(base, timezone_name, date_order="DMY"),
            languages=["en"],
        )
        mdy = dateparser.parse(
            fragment,
            settings=_dateparser_settings(base, timezone_name, date_order="MDY"),
            languages=["en"],
        )
        if dmy is None or mdy is None or dmy.date() == mdy.date():
            continue
        include_time = _has_explicit_time(fragment)
        return (
            f"I can read `{fragment}` as `{_format_prompt_datetime(ensure_timezone(dmy, timezone_name), timezone_name, include_time=include_time)}` "
            f"or `{_format_prompt_datetime(ensure_timezone(mdy, timezone_name), timezone_name, include_time=include_time)}`. "
            "Which did you mean? Reply with a month name."
        )
    return None


def _coarse_datetime_prompt(text: str) -> str | None:
    normalized = _normalize_datetime_text(text)
    if COARSE_PERIOD_RE.fullmatch(normalized):
        return f"`{text.strip()}` covers a date range, not one exact date and time. Reply with a specific day or time."
    if MONTH_ONLY_RE.fullmatch(normalized):
        return f"`{text.strip()}` is too broad for one exact date. Reply with a specific day."
    if YEAR_ONLY_RE.fullmatch(normalized):
        compact_time = FOUR_DIGIT_TIME_RE.fullmatch(normalized)
        if compact_time and not normalized.startswith(("19", "20")):
            return None
        return f"`{text.strip()}` is too broad for one exact date. Reply with a specific month and day."
    return None


def interpret_user_datetime(
    text: str,
    timezone_name: str,
    *,
    reference: datetime | None = None,
) -> DateTimeInterpretation:
    raw = text.strip()
    if not raw:
        return DateTimeInterpretation(status="invalid")
    normalized = _normalize_datetime_text(raw)
    if not normalized:
        return DateTimeInterpretation(status="invalid")

    if prompt := _ambiguous_numeric_date_prompt(raw, timezone_name, reference=reference):
        return DateTimeInterpretation(
            status="ambiguous",
            normalized_text=normalized,
            prompt=prompt,
            has_explicit_time=_has_explicit_time(raw),
            has_explicit_date=True,
        )
    if prompt := _coarse_datetime_prompt(raw):
        return DateTimeInterpretation(
            status="ambiguous",
            normalized_text=normalized,
            prompt=prompt,
            has_explicit_time=_has_explicit_time(raw),
            has_explicit_date=_has_explicit_date(raw),
        )

    value = _parse_user_datetime_guess(raw, timezone_name, reference=reference)
    if value is None:
        return DateTimeInterpretation(
            status="invalid",
            normalized_text=normalized,
            has_explicit_time=_has_explicit_time(raw),
            has_explicit_date=_has_explicit_date(raw),
        )
    return DateTimeInterpretation(
        status="ok",
        value=value,
        normalized_text=normalized,
        note=_datetime_interpretation_note(raw, normalized, timezone_name, reference=reference),
        has_explicit_time=_has_explicit_time(raw),
        has_explicit_date=_has_explicit_date(raw),
    )


def _datetime_interpretation_note(
    raw: str,
    normalized: str,
    timezone_name: str,
    *,
    reference: datetime | None = None,
) -> str:
    base = ensure_timezone(reference or now_utc(), timezone_name)
    if base.hour < 4 and "day after tomorrow" not in normalized and "tomorrow" in normalized:
        return "Interpreted `tomorrow` as later today because it is before 4 AM."
    return ""


def _start_of_day(value: datetime, timezone_name: str) -> datetime:
    local_value = ensure_timezone(value, timezone_name)
    return local_value.replace(hour=0, minute=0, second=0, microsecond=0)


def _end_of_day(value: datetime, timezone_name: str) -> datetime:
    local_value = ensure_timezone(value, timezone_name)
    return local_value.replace(hour=23, minute=59, second=59, microsecond=999999)


def _month_window(base: datetime, month: int, year: int) -> tuple[datetime, datetime]:
    start = base.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)
    end_day = monthrange(year, month)[1]
    end = base.replace(year=year, month=month, day=end_day, hour=23, minute=59, second=59, microsecond=999999)
    return start, end


def _coarse_period_range(normalized: str, base: datetime) -> tuple[datetime, datetime] | None:
    lowered = normalized.lower()
    if lowered in {"today", "tomorrow", "yesterday", "day after tomorrow", "day before yesterday"}:
        anchor = _parse_user_datetime_guess(lowered, base.tzinfo.key if hasattr(base.tzinfo, "key") else "UTC", reference=base)
        if anchor is None:
            return None
        return _start_of_day(anchor, base.tzinfo.key if hasattr(base.tzinfo, "key") else "UTC"), _end_of_day(anchor, base.tzinfo.key if hasattr(base.tzinfo, "key") else "UTC")

    match = COARSE_PERIOD_RE.fullmatch(lowered)
    if not match:
        return None
    modifier = (match.group("modifier") or "this").lower()
    unit = match.group("unit").lower()
    if unit == "week":
        start = _start_of_day(base, base.tzinfo.key if hasattr(base.tzinfo, "key") else "UTC") - timedelta(days=base.weekday())
        if modifier == "next":
            start += timedelta(days=7)
        end = start + timedelta(days=6, hours=23, minutes=59, seconds=59, microseconds=999999)
        return start, end
    if unit == "weekend":
        week_start = _start_of_day(base, base.tzinfo.key if hasattr(base.tzinfo, "key") else "UTC") - timedelta(days=base.weekday())
        if modifier == "next":
            week_start += timedelta(days=7)
        start = week_start + timedelta(days=5)
        end = start + timedelta(days=1, hours=23, minutes=59, seconds=59, microseconds=999999)
        return start, end
    if unit == "month":
        month = base.month + (1 if modifier == "next" else 0)
        year = base.year
        if month > 12:
            month = 1
            year += 1
        return _month_window(base, month, year)
    if unit == "year":
        year = base.year + (1 if modifier == "next" else 0)
        start = base.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        end = base.replace(year=year, month=12, day=31, hour=23, minute=59, second=59, microsecond=999999)
        return start, end
    return None


def _combine_date_and_time(date_value: datetime, time_value: datetime) -> datetime:
    local_date = ensure_timezone(date_value, date_value.tzinfo.key if hasattr(date_value.tzinfo, "key") else "UTC")
    local_time = ensure_timezone(time_value, local_date.tzinfo.key if hasattr(local_date.tzinfo, "key") else "UTC")
    return local_date.replace(
        hour=local_time.hour,
        minute=local_time.minute,
        second=local_time.second,
        microsecond=local_time.microsecond,
    )


def interpret_user_datetime_range(
    text: str,
    timezone_name: str,
    *,
    reference: datetime | None = None,
) -> DateRangeInterpretation:
    base = ensure_timezone(reference or now_utc(), timezone_name)
    raw = text.strip()
    if not raw:
        return DateRangeInterpretation(status="invalid")
    normalized = _normalize_datetime_text(raw)
    if not normalized:
        return DateRangeInterpretation(status="invalid")

    coarse = _coarse_period_range(normalized, base)
    if coarse is not None:
        return DateRangeInterpretation(status="ok", start=coarse[0], end=coarse[1], normalized_text=normalized)

    match = RANGE_BETWEEN_RE.match(raw) or RANGE_TO_RE.match(raw)
    if match is None:
        return DateRangeInterpretation(status="invalid", normalized_text=normalized)

    start_text = match.group("start").strip(" ,.;:-")
    end_text = match.group("end").strip(" ,.;:-")
    if not start_text or not end_text:
        return DateRangeInterpretation(status="invalid", normalized_text=normalized)

    start_result = interpret_user_datetime(start_text, timezone_name, reference=base)
    if start_result.status == "ambiguous":
        return DateRangeInterpretation(status="ambiguous", normalized_text=normalized, prompt=start_result.prompt)
    end_result = interpret_user_datetime(end_text, timezone_name, reference=start_result.value or base)
    if end_result.status == "ambiguous":
        return DateRangeInterpretation(status="ambiguous", normalized_text=normalized, prompt=end_result.prompt)
    if start_result.status != "ok" or end_result.status != "ok" or start_result.value is None or end_result.value is None:
        return DateRangeInterpretation(status="invalid", normalized_text=normalized)

    start_value = start_result.value
    end_value = end_result.value
    if not start_result.has_explicit_date and end_result.has_explicit_date:
        start_value = _combine_date_and_time(end_value, start_value)
    if not end_result.has_explicit_date and start_result.has_explicit_date:
        end_value = _combine_date_and_time(start_value, end_value)

    if not start_result.has_explicit_time:
        start_value = _start_of_day(start_value, timezone_name)
    if not end_result.has_explicit_time:
        end_value = _end_of_day(end_value, timezone_name)

    if end_value <= start_value and end_result.has_explicit_time and not end_result.has_explicit_date:
        end_value += timedelta(days=1)
    if end_value <= start_value:
        return DateRangeInterpretation(
            status="ambiguous",
            normalized_text=normalized,
            prompt="I need the end of that range to be after the start. Reply with a clearer range like `tomorrow 2pm to 5pm`.",
        )
    return DateRangeInterpretation(status="ok", start=start_value, end=end_value, normalized_text=normalized)


def parse_user_datetime(
    text: str,
    timezone_name: str,
    *,
    reference: datetime | None = None,
) -> datetime | None:
    interpretation = interpret_user_datetime(text, timezone_name, reference=reference)
    if interpretation.status == "ok":
        return interpretation.value
    return _parse_user_datetime_guess(text, timezone_name, reference=reference)


def search_user_datetimes(
    text: str,
    timezone_name: str,
    *,
    reference: datetime | None = None,
) -> list[tuple[str, datetime]]:
    base = ensure_timezone(reference or now_utc(), timezone_name)
    normalized = _normalize_datetime_text(text)
    if not normalized:
        return []
    try:
        results = search_dates(
            normalized,
            settings=_dateparser_settings(base, timezone_name),
            languages=["en"],
        )
    except Exception:
        results = None
    matches: list[tuple[str, datetime]] = []
    for item in results or []:
        if not isinstance(item, tuple) or len(item) < 2:
            continue
        fragment = str(item[0]).strip(" ,.;:-")
        parsed = item[1]
        if not fragment or not isinstance(parsed, datetime):
            continue
        matches.append((fragment, ensure_timezone(parsed, timezone_name)))
    return matches


def parse_duration_minutes(text: str, default_minutes: int = 30) -> int:
    matches = list(DURATION_RE.finditer(_normalize_datetime_text(text)))
    if not matches:
        return default_minutes
    total = 0
    for match in matches:
        value = _parse_number_value(match.group("value"))
        unit = match.group("unit").lower()
        if unit.startswith("hour") or unit.startswith("hr") or unit == "h":
            total += value * 60
        else:
            total += value
    return total or default_minutes


def format_local(value: datetime, timezone_name: str) -> str:
    local_value = ensure_timezone(value, timezone_name)
    local_now = ensure_timezone(now_utc(), timezone_name)
    day_delta = (local_value.date() - local_now.date()).days
    if day_delta == 0:
        prefix = "📍 Today"
    elif day_delta == 1:
        prefix = "⏭️ Tomorrow"
    elif day_delta == -1:
        prefix = "↩️ Yesterday"
    elif day_delta == 2:
        prefix = "⏩ Day After Tomorrow"
    elif day_delta == -2:
        prefix = "⏪ Day Before Yesterday"
    else:
        if local_value.year == local_now.year:
            return local_value.strftime("%a %d %b %I:%M %p")
        return local_value.strftime("%a %d %b %Y %I:%M %p")
    return f"{prefix} {local_value.strftime('%I:%M %p')}"


def format_note_local(value: datetime, timezone_name: str) -> str:
    return format_local(value, timezone_name)


def local_day_window(
    anchor: datetime,
    timezone_name: str,
    *,
    start_hour: int,
    end_hour: int,
) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone_name)
    local_anchor = ensure_timezone(anchor, timezone_name)
    start = datetime(
        year=local_anchor.year,
        month=local_anchor.month,
        day=local_anchor.day,
        hour=start_hour,
        minute=0,
        tzinfo=zone,
    )
    end = datetime(
        year=local_anchor.year,
        month=local_anchor.month,
        day=local_anchor.day,
        hour=end_hour,
        minute=0,
        tzinfo=zone,
    )
    if end <= start:
        end = start + timedelta(hours=8)
    return start, end


def isoformat_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None
