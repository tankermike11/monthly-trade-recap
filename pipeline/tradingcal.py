"""US stock-market trading days, for "first half / second half of the month" (SPEC 3.1:
halves are counted in trading days, not calendar days).

NYSE full-day closures are listed by hand for the years the pipeline covers; add a
year's list before running a month in it (month_trading_days refuses unknown years).
"""

from __future__ import annotations

from datetime import date, timedelta

NYSE_CLOSED = {
    2025: {date(2025, 1, 1), date(2025, 1, 9), date(2025, 1, 20), date(2025, 2, 17), date(2025, 4, 18),
           date(2025, 5, 26), date(2025, 6, 19), date(2025, 7, 4), date(2025, 9, 1),
           date(2025, 11, 27), date(2025, 12, 25)},
    2026: {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3), date(2026, 5, 25),
           date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26), date(2026, 12, 25)},
    2027: {date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26), date(2027, 5, 31),
           date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6), date(2027, 11, 25), date(2027, 12, 24)},
}


def month_bounds(month: str) -> tuple[date, date]:
    y, m = (int(x) for x in month.split("-"))
    first = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


def prev_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y - (m == 1)}-{(m - 2) % 12 + 1:02d}"


def is_trading_day(d: date) -> bool:
    if d.year not in NYSE_CLOSED:
        raise ValueError(f"no market holiday list for {d.year}")
    return d.weekday() < 5 and d not in NYSE_CLOSED[d.year]


def month_trading_days(month: str) -> list[date]:
    a, b = month_bounds(month)
    return [a + timedelta(days=i) for i in range((b - a).days + 1) if is_trading_day(a + timedelta(days=i))]


def first_half_end(month: str) -> date:
    """Last date of the first half: the first ceil(n/2) trading days. Weekend and
    holiday dates belong to the half of the trading day before them."""
    days = month_trading_days(month)
    return days[(len(days) + 1) // 2 - 1]


def half_of(d: date, month: str) -> str:
    return "first" if d <= first_half_end(month) else "second"
