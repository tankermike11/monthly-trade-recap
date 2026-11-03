"""New York calendar dates for UTC instants.

Python's zoneinfo needs the tzdata package on Windows, which is not an approved
dependency, so the US daylight-saving rule is written out here: clocks go forward at
02:00 local on the second Sunday of March and back at 02:00 local on the first Sunday
of November (rule in force since 2007).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

_STD = -5   # New York standard time, hours from UTC


def _nth_sunday(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    d += timedelta(days=(6 - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _dst_window_utc(year: int) -> tuple[datetime, datetime]:
    start = datetime.combine(_nth_sunday(year, 3, 2), datetime.min.time()) + timedelta(hours=2 - _STD)
    end = datetime.combine(_nth_sunday(year, 11, 1), datetime.min.time()) + timedelta(hours=2 - (_STD + 1))
    return start.replace(tzinfo=UTC), end.replace(tzinfo=UTC)


def ny(ts_utc: datetime) -> datetime:
    """Aware UTC instant -> naive New York wall time."""
    if ts_utc.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware UTC")
    start, end = _dst_window_utc(ts_utc.year)
    offset = _STD + (1 if start <= ts_utc < end else 0)
    return (ts_utc + timedelta(hours=offset)).replace(tzinfo=None)


def ny_date(ts_utc: datetime) -> date:
    return ny(ts_utc).date()


def ny_to_utc(wall: datetime) -> datetime:
    """Naive New York wall time -> aware UTC instant. In the repeated hour when clocks
    go back, the first (daylight-time) instant is used."""
    if wall.tzinfo is not None:
        raise ValueError("wall time must be naive New York time")
    for hours in (_STD + 1, _STD):                  # daylight time first, then standard
        ts = (wall - timedelta(hours=hours)).replace(tzinfo=UTC)
        if ny(ts) == wall:
            return ts
    raise ValueError(f"{wall} does not exist in New York (clocks went forward)")


def ny_midnight_utc(d: date) -> datetime:
    """The UTC instant of 00:00 New York time on day d (never inside a DST switch)."""
    guess = datetime.combine(d, datetime.min.time()).replace(tzinfo=UTC) - timedelta(hours=_STD)
    start, end = _dst_window_utc(d.year)
    return guess - timedelta(hours=1) if start <= guess - timedelta(hours=1) < end else guess
