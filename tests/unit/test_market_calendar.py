from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from app.market_calendar.us_equities import (
    NEW_YORK,
    _early_close_days,
    _market_holidays,
    market_session_status,
)


ET = NEW_YORK

OFFICIAL_HOLIDAYS = {
    2026: {
        date(2026, 1, 1),
        date(2026, 1, 19),
        date(2026, 2, 16),
        date(2026, 4, 3),
        date(2026, 5, 25),
        date(2026, 6, 19),
        date(2026, 7, 3),
        date(2026, 9, 7),
        date(2026, 11, 26),
        date(2026, 12, 25),
    },
    2027: {
        date(2027, 1, 1),
        date(2027, 1, 18),
        date(2027, 2, 15),
        date(2027, 3, 26),
        date(2027, 5, 31),
        date(2027, 6, 18),
        date(2027, 7, 5),
        date(2027, 9, 6),
        date(2027, 11, 25),
        date(2027, 12, 24),
    },
    2028: {
        date(2028, 1, 17),
        date(2028, 2, 21),
        date(2028, 4, 14),
        date(2028, 5, 29),
        date(2028, 6, 19),
        date(2028, 7, 4),
        date(2028, 9, 4),
        date(2028, 11, 23),
        date(2028, 12, 25),
    },
}

OFFICIAL_EARLY_CLOSES = {
    2026: {date(2026, 11, 27), date(2026, 12, 24)},
    2027: {date(2027, 11, 26)},
    2028: {date(2028, 7, 3), date(2028, 11, 24)},
}


@pytest.mark.parametrize("year", [2026, 2027, 2028])
def test_official_nyse_holiday_table(year: int) -> None:
    assert _market_holidays(year) == OFFICIAL_HOLIDAYS[year]


@pytest.mark.parametrize("year", [2026, 2027, 2028])
def test_official_nyse_early_close_table(year: int) -> None:
    assert _early_close_days(year) == OFFICIAL_EARLY_CLOSES[year]


@pytest.mark.parametrize(
    "holiday",
    [day for year in sorted(OFFICIAL_HOLIDAYS) for day in sorted(OFFICIAL_HOLIDAYS[year])],
    ids=lambda day: day.isoformat(),
)
def test_each_published_nyse_holiday_is_closed(holiday: date) -> None:
    assert market_session_status(datetime.combine(holiday, datetime.min.time().replace(hour=12), ET)).is_open is False


@pytest.mark.parametrize(
    "early_close",
    [day for year in sorted(OFFICIAL_EARLY_CLOSES) for day in sorted(OFFICIAL_EARLY_CLOSES[year])],
    ids=lambda day: day.isoformat(),
)
def test_each_published_nyse_early_close_uses_1300_et(early_close: date) -> None:
    before_close = market_session_status(datetime.combine(early_close, datetime.min.time().replace(hour=12, minute=59), ET))
    at_close = market_session_status(datetime.combine(early_close, datetime.min.time().replace(hour=13), ET))
    assert before_close.is_open is True
    assert before_close.next_transition_et.hour == 13
    assert at_close.is_open is False


@pytest.mark.parametrize(
    ("moment", "expected_open"),
    [
        (datetime(2026, 8, 18, 9, 29, tzinfo=ET), False),
        (datetime(2026, 8, 18, 9, 30, tzinfo=ET), True),
        (datetime(2026, 8, 18, 12, 0, tzinfo=ET), True),
        (datetime(2026, 8, 18, 16, 0, tzinfo=ET), False),
        (datetime(2026, 8, 22, 12, 0, tzinfo=ET), False),
        (datetime(2026, 7, 3, 12, 0, tzinfo=ET), False),
    ],
)
def test_regular_market_open_closed_boundaries(moment: datetime, expected_open: bool) -> None:
    status = market_session_status(moment)
    assert status.is_open is expected_open
    assert status.label.startswith("Market: OPEN" if expected_open else "Market: CLOSED")


def test_known_holiday_is_closed() -> None:
    status = market_session_status(datetime(2026, 12, 25, 12, 0, tzinfo=ET))
    assert status.is_open is False
    assert status.next_transition_et.date().isoformat() == "2026-12-28"


def test_thanksgiving_friday_uses_real_early_close() -> None:
    before_close = market_session_status(datetime(2026, 11, 27, 12, 59, tzinfo=ET))
    at_close = market_session_status(datetime(2026, 11, 27, 13, 0, tzinfo=ET))
    assert before_close.is_open is True
    assert before_close.next_transition_et.hour == 13
    assert at_close.is_open is False


@pytest.mark.parametrize(
    ("moment", "expected_open", "expected_close_hour"),
    [
        (datetime(2026, 7, 2, 12, 59, tzinfo=ET), True, 16),
        (datetime(2026, 7, 2, 13, 1, tzinfo=ET), True, 16),
        (datetime(2026, 11, 27, 12, 59, tzinfo=ET), True, 13),
        (datetime(2026, 11, 27, 13, 0, tzinfo=ET), False, None),
        (datetime(2026, 12, 24, 12, 59, tzinfo=ET), True, 13),
        (datetime(2027, 7, 2, 13, 1, tzinfo=ET), True, 16),
        (datetime(2027, 12, 23, 13, 1, tzinfo=ET), True, 16),
        (datetime(2027, 12, 31, 12, 0, tzinfo=ET), True, 16),
        (datetime(2028, 7, 3, 12, 59, tzinfo=ET), True, 13),
        (datetime(2028, 7, 3, 13, 0, tzinfo=ET), False, None),
        (datetime(2028, 12, 22, 13, 1, tzinfo=ET), True, 16),
    ],
)
def test_published_calendar_boundaries(
    moment: datetime,
    expected_open: bool,
    expected_close_hour: int | None,
) -> None:
    status = market_session_status(moment)
    assert status.is_open is expected_open
    if expected_close_hour is not None:
        assert status.next_transition_et.hour == expected_close_hour


def test_dst_boundary_uses_new_york_rules_not_a_fixed_utc_offset() -> None:
    before_dst = market_session_status(datetime(2026, 3, 6, 14, 30, tzinfo=timezone.utc))
    after_dst = market_session_status(datetime(2026, 3, 9, 13, 30, tzinfo=timezone.utc))
    assert before_dst.is_open is True
    assert after_dst.is_open is True
    assert before_dst.checked_at_et.utcoffset().total_seconds() == -5 * 3600
    assert after_dst.checked_at_et.utcoffset().total_seconds() == -4 * 3600


def test_non_new_york_system_timezone_input_is_converted() -> None:
    los_angeles = timezone(-timedelta(hours=7), name="PDT")
    status = market_session_status(datetime(2026, 8, 18, 6, 30, tzinfo=los_angeles))
    assert status.is_open is True
    assert status.checked_at_et.hour == 9
