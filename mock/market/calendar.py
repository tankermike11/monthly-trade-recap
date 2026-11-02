"""Exchange calendar and US clock rules for the simulated world.

The standard library's zoneinfo needs the third-party tzdata package on Windows, so
the US daylight-saving rule (second Sunday of March to first Sunday of November,
switching at 02:00 local time) is written out here. Only US zones are supported.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

UTC = timezone.utc

# Standard-time offsets in hours.
_STD_OFFSET = {
    "America/New_York": -5,
    "America/Chicago": -6,
    "America/Denver": -7,
    "America/Los_Angeles": -8,
}

# NYSE full-day closures inside the simulated window.
HOLIDAYS = frozenset({
    date(2025, 9, 1), date(2025, 11, 27), date(2025, 12, 25),
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
    date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
    date(2026, 11, 26), date(2026, 12, 25),
})

# Early closes: stocks stop at 13:00 New York time.
HALF_DAYS = frozenset({
    date(2025, 11, 28), date(2025, 12, 24), date(2026, 11, 27), date(2026, 12, 24),
})

WINDOW_START = date(2025, 9, 1)
WINDOW_END = date(2026, 12, 31)


# ---------------------------------------------------------------------------
# Clock
# ---------------------------------------------------------------------------

def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def dst_bounds_utc(year: int, tz: str) -> tuple[datetime, datetime]:
    """UTC instants when DST starts and ends in `tz` for `year`."""
    std = _STD_OFFSET[tz]
    start_local = datetime.combine(_nth_sunday(year, 3, 2), time(2, 0))
    end_local = datetime.combine(_nth_sunday(year, 11, 1), time(2, 0))
    start = (start_local - timedelta(hours=std)).replace(tzinfo=UTC)
    end = (end_local - timedelta(hours=std + 1)).replace(tzinfo=UTC)
    return start, end


def utc_offset_hours(ts_utc: datetime, tz: str) -> int:
    start, end = dst_bounds_utc(ts_utc.year, tz)
    return _STD_OFFSET[tz] + (1 if start <= ts_utc < end else 0)


def to_local(ts_utc: datetime, tz: str) -> datetime:
    """Aware UTC -> naive local wall time."""
    return (ts_utc + timedelta(hours=utc_offset_hours(ts_utc, tz))).replace(tzinfo=None)


def to_utc(local: datetime, tz: str) -> datetime:
    """Naive local wall time -> aware UTC. Refuses the skipped hour; an ambiguous
    time (the repeated hour in November) resolves to the earlier, daylight instant."""
    std = _STD_OFFSET[tz]
    for off in (std + 1, std):
        cand = (local - timedelta(hours=off)).replace(tzinfo=UTC)
        if to_local(cand, tz) == local:
            return cand
    raise ValueError(f"{local} does not exist in {tz} (clocks skipped it)")


def ny(ts_utc: datetime) -> datetime:
    return to_local(ts_utc, "America/New_York")


def ny_date(ts_utc: datetime) -> date:
    return ny(ts_utc).date()


def at_ny(d: date, hh: int, mm: int = 0) -> datetime:
    return to_utc(datetime.combine(d, time(hh, mm)), "America/New_York")


def at_ny_minutes(d: date, minutes_after_midnight: int) -> datetime:
    """UTC instant for a New York wall time given in minutes after local midnight
    of day `d` (may be negative or above 1440 to reach neighbouring days)."""
    local = datetime.combine(d, time(0, 0)) + timedelta(minutes=minutes_after_midnight)
    return to_utc(local, "America/New_York")


# ---------------------------------------------------------------------------
# Trading days
# ---------------------------------------------------------------------------

def is_trading_day(d: date) -> bool:
    return d.weekday() < 5 and d not in HOLIDAYS


def is_half_day(d: date) -> bool:
    return d in HALF_DAYS


@lru_cache(maxsize=None)
def trading_days(start: date = WINDOW_START, end: date = WINDOW_END) -> tuple[date, ...]:
    out, d = [], start
    while d <= end:
        if is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return tuple(out)


def calendar_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def month_bounds(month: str) -> tuple[date, date]:
    y, m = (int(x) for x in month.split("-"))
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def month_trading_days(month: str) -> list[date]:
    a, b = month_bounds(month)
    return [d for d in trading_days() if a <= d <= b]


def last_trading_day(month: str) -> date:
    return month_trading_days(month)[-1]


def prev_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y - (m == 1)}-{(m - 2) % 12 + 1:02d}"


def next_trading_day(d: date) -> date:
    d += timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d


def prev_trading_day(d: date) -> date:
    d -= timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def regular_close_minute(d: date) -> int:
    """Minutes after New York midnight when the regular stock session closes."""
    return 13 * 60 if is_half_day(d) else 16 * 60


def third_friday(year: int, month: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(4 - d.weekday()) % 7)
    d += timedelta(weeks=2)
    return d if is_trading_day(d) else prev_trading_day(d)


# ---------------------------------------------------------------------------
# Sessions (SPEC 1.1): buckets of New York wall time, used for every asset class
# ---------------------------------------------------------------------------

def session_of(ts_utc: datetime) -> str:
    local = ny(ts_utc)
    d = local.date()
    minute = local.hour * 60 + local.minute
    if not is_trading_day(d):
        return "overnight"
    close = regular_close_minute(d)
    if minute < 4 * 60 or minute >= 20 * 60:
        return "overnight"
    if minute < 9 * 60 + 30:
        return "pre_market"
    if minute < close:
        return "regular"
    return "after_hours"
