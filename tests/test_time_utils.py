from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from personal_assistant.time_utils import interpret_user_datetime, interpret_user_datetime_range


def test_interpret_user_datetime_flags_ambiguous_numeric_date():
    reference = datetime(2026, 4, 8, 12, 0, tzinfo=ZoneInfo("Asia/Singapore"))

    result = interpret_user_datetime("04/05/2026 9am", "Asia/Singapore", reference=reference)

    assert result.status == "ambiguous"
    assert "which did you mean" in result.prompt.lower()


def test_interpret_user_datetime_marks_coarse_period_as_ambiguous():
    reference = datetime(2026, 4, 8, 12, 0, tzinfo=ZoneInfo("Asia/Singapore"))

    result = interpret_user_datetime("next week", "Asia/Singapore", reference=reference)

    assert result.status == "ambiguous"
    assert "date range" in result.prompt.lower()


def test_interpret_user_datetime_range_parses_between_window():
    reference = datetime(2026, 4, 8, 12, 0, tzinfo=ZoneInfo("Asia/Singapore"))

    result = interpret_user_datetime_range("between tomorrow 2pm and 5pm", "Asia/Singapore", reference=reference)

    assert result.status == "ok"
    assert result.start is not None
    assert result.end is not None
    assert result.start.date().isoformat() == "2026-04-09"
    assert result.start.hour == 14
    assert result.end.hour == 17


def test_interpret_user_datetime_rolls_time_only_to_tomorrow_if_time_passed():
    reference = datetime(2026, 4, 8, 22, 0, tzinfo=ZoneInfo("Asia/Singapore"))

    result = interpret_user_datetime("9am", "Asia/Singapore", reference=reference)

    assert result.status == "ok"
    assert result.value is not None
    assert result.value.date().isoformat() == "2026-04-09"
    assert result.value.hour == 9


def test_interpret_user_datetime_treats_tomorrow_as_later_today_before_4am():
    reference = datetime(2026, 4, 8, 1, 0, tzinfo=ZoneInfo("Asia/Singapore"))

    result = interpret_user_datetime("tomorrow 9am", "Asia/Singapore", reference=reference)

    assert result.status == "ok"
    assert result.value is not None
    assert result.value.date().isoformat() == "2026-04-08"
    assert result.value.hour == 9
    assert "before 4 am" in result.note.lower()


def test_interpret_user_datetime_preserves_dotted_and_compact_minutes():
    reference = datetime(2026, 4, 8, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))

    dotted = interpret_user_datetime("12.55pm", "Asia/Singapore", reference=reference)
    compact = interpret_user_datetime("0940", "Asia/Singapore", reference=reference)
    dotted_with_space = interpret_user_datetime("9.40 am", "Asia/Singapore", reference=reference)

    assert dotted.status == "ok"
    assert dotted.value is not None
    assert dotted.value.hour == 12
    assert dotted.value.minute == 55
    assert compact.status == "ok"
    assert compact.value is not None
    assert compact.value.hour == 9
    assert compact.value.minute == 40
    assert dotted_with_space.status == "ok"
    assert dotted_with_space.value is not None
    assert dotted_with_space.value.hour == 9
    assert dotted_with_space.value.minute == 40


def test_interpret_user_datetime_treats_relative_durations_as_specific_times():
    reference = datetime(2026, 4, 8, 8, 0, tzinfo=ZoneInfo("Asia/Singapore"))

    word_duration = interpret_user_datetime("in one hour", "Asia/Singapore", reference=reference)
    shorthand = interpret_user_datetime("30m", "Asia/Singapore", reference=reference)

    assert word_duration.status == "ok"
    assert word_duration.value is not None
    assert word_duration.value.hour == 9
    assert word_duration.has_explicit_time is True
    assert shorthand.status == "ok"
    assert shorthand.value is not None
    assert shorthand.value.hour == 8
    assert shorthand.value.minute == 30
    assert shorthand.has_explicit_time is True
