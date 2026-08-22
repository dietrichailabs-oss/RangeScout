"""DST-safe regular-session calendar for the primary U.S. equities exchanges."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class _AmericaNewYorkFallback(tzinfo):
    """Post-2007 U.S. Eastern rules for Windows runtimes without IANA tzdata."""

    key = "America/New_York"

    def utcoffset(self, value: datetime | None) -> timedelta:
        return timedelta(hours=-5) + self.dst(value)

    def dst(self, value: datetime | None) -> timedelta:
        if value is None:
            return timedelta(0)
        naive = value.replace(tzinfo=None)
        start, end = _dst_range(naive.year)
        if start <= naive < end:
            return timedelta(hours=1)
        return timedelta(0)

    def tzname(self, value: datetime | None) -> str:
        return "EDT" if self.dst(value) else "EST"

    def fromutc(self, value: datetime) -> datetime:
        standard = (value.replace(tzinfo=None) + timedelta(hours=-5)).replace(tzinfo=self)
        daylight = standard + timedelta(hours=1)
        start, end = _dst_range(standard.year)
        if start <= daylight.replace(tzinfo=None) < end:
            return daylight
        return standard


def _dst_range(year: int) -> tuple[datetime, datetime]:
    march_first = date(year, 3, 1)
    second_sunday = march_first + timedelta(days=(6 - march_first.weekday()) % 7 + 7)
    november_first = date(year, 11, 1)
    first_sunday = november_first + timedelta(days=(6 - november_first.weekday()) % 7)
    return datetime.combine(second_sunday, time(2)), datetime.combine(first_sunday, time(2))


try:
    NEW_YORK: tzinfo = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    NEW_YORK = _AmericaNewYorkFallback()
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)
EARLY_CLOSE = time(13, 0)


@dataclass(frozen=True)
class MarketSessionStatus:
    is_open: bool
    label: str
    checked_at_et: datetime
    next_transition_et: datetime


def market_session_status(at: datetime | None = None) -> MarketSessionStatus:
    """Return the local regular-session state and the next open/close in ET."""
    current = _as_new_york(at)
    session_date = current.date()
    if _is_session_day(session_date):
        session_open = datetime.combine(session_date, REGULAR_OPEN, NEW_YORK)
        session_close = datetime.combine(session_date, _close_time(session_date), NEW_YORK)
        if session_open <= current < session_close:
            return MarketSessionStatus(
                is_open=True,
                label=f"Market: OPEN - Closes {_format_transition(session_close)}",
                checked_at_et=current,
                next_transition_et=session_close,
            )
        if current < session_open:
            return MarketSessionStatus(
                is_open=False,
                label=f"Market: CLOSED - Opens {_format_transition(session_open)}",
                checked_at_et=current,
                next_transition_et=session_open,
            )

    next_open = _next_session_open(session_date + timedelta(days=1))
    return MarketSessionStatus(
        is_open=False,
        label=f"Market: CLOSED - Opens {_format_transition(next_open)}",
        checked_at_et=current,
        next_transition_et=next_open,
    )


def _as_new_york(value: datetime | None) -> datetime:
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=NEW_YORK)
    return value.astimezone(NEW_YORK)


def _next_session_open(candidate: date) -> datetime:
    while not _is_session_day(candidate):
        candidate += timedelta(days=1)
    return datetime.combine(candidate, REGULAR_OPEN, NEW_YORK)


def _is_session_day(day: date) -> bool:
    return day.weekday() < 5 and day not in _market_holidays(day.year)


def _close_time(day: date) -> time:
    return EARLY_CLOSE if day in _early_close_days(day.year) else REGULAR_CLOSE


def _market_holidays(year: int) -> set[date]:
    holidays = {
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    new_year = _nyse_new_year_holiday(year)
    if new_year is not None:
        holidays.add(new_year)
    return holidays


def _early_close_days(year: int) -> set[date]:
    candidates = {
        _nth_weekday(year, 11, 3, 4) + timedelta(days=1),
        date(year, 7, 3),
        date(year, 12, 24),
    }
    return {day for day in candidates if _is_session_day(day)}


def _nyse_new_year_holiday(year: int) -> date | None:
    new_year = date(year, 1, 1)
    if new_year.weekday() == 5:
        # NYSE does not observe a Saturday January 1 on the preceding Friday.
        return None
    if new_year.weekday() == 6:
        return new_year + timedelta(days=1)
    return new_year


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (occurrence - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    # Anonymous Gregorian computus; Good Friday is two days before this date.
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    month_adjustment = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * month_adjustment) // 451
    month = (h + month_adjustment - 7 * m + 114) // 31
    day = ((h + month_adjustment - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _format_transition(value: datetime) -> str:
    return value.strftime("%a %b %d, %I:%M %p ET").replace(" 0", " ")
