"""Display strings for facts, formatted in code (SPEC 3.2). The model inserts these
verbatim; every format here must read aloud cleanly."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from fractions import Fraction

NAMES = {
    "stock_etf": "stocks and funds",
    "equity_option": "options on stocks and funds",
    "index_option": "index options",
    "future": "futures",
    "future_option": "options on futures",
    "crypto": "crypto",
    "event_contract": "event contracts",
}


def whole(x) -> int:
    return int(Decimal(str(x)).quantize(Decimal(1), ROUND_HALF_UP))


def money(x) -> str:
    """$1,240 / -$596. Whole dollars from $100 up, cents below."""
    d = Decimal(str(x))
    sign = "-" if d < 0 else ""
    d = abs(d)
    if d >= 100:
        return f"{sign}${whole(d):,}"
    return f"{sign}${d.quantize(Decimal('0.01'), ROUND_HALF_UP):,.2f}"


def count(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def ratio(x: float) -> str:
    """3.23 -> '3 to 1'; 0.5 -> '1 to 2'; 1 -> '1 to 1'; 0 -> '0 to 1'."""
    if x <= 0:
        return "0 to 1"
    if x >= 1:
        return f"{max(1, whole(x))} to 1"
    return f"1 to {max(1, whole(1 / x))}"


def share(x: float) -> str:
    """0.25 -> '1 in 4'; 0.6667 -> '2 in 3'; 0 -> 'none'; 1 -> 'all'."""
    if x <= 0:
        return "none"
    if x >= 1:
        return "all"
    f = Fraction(x).limit_denominator(10)
    if f == 0:
        return "fewer than 1 in 10"
    if f == 1:
        return "nearly all"
    return f"{f.numerator} in {f.denominator}"


def portion(x: float) -> str:
    """A share said in words: 'about a third', 'about half', 'nearly all'."""
    if x <= 0:
        return "none"
    if x >= 1:
        return "all"
    words = [(1 / 10, "about a tenth"), (1 / 5, "about a fifth"), (1 / 4, "about a quarter"),
             (1 / 3, "about a third"), (1 / 2, "about half"), (2 / 3, "about two thirds"),
             (3 / 4, "about three quarters"), (9 / 10, "nearly all")]
    if x < 0.07:
        return "very little"
    return min(words, key=lambda w: abs(w[0] - x))[1]


def multiple(x: float) -> str:
    """How one amount compares with another: 'about half', 'about the same', '2.3 times'."""
    if 0.9 <= x < 1.1:
        return "about the same"
    if x >= 1.1:
        return f"{x:.1f} times"
    return portion(x)


def percent(x: float, places: int = 1) -> str:
    return f"{abs(x) * 100:.{places}f}%"


def move(x: float) -> str:
    """Index move: 'up 0.7%', 'down 6.8%', 'flat'."""
    if round(abs(x) * 100, 1) == 0:
        return "flat"
    return f"{'up' if x > 0 else 'down'} {percent(x)}"


def days(n: float) -> str:
    """Calendar days as people say them: 'the same day', '3 days', 'about 6 weeks'."""
    n = float(n)
    if n < 0.5:
        return "the same day"
    if n < 13.5:
        return count(whole(n), "day")
    weeks = whole(n / 7)
    if weeks < 9:
        return f"about {weeks} weeks"
    return f"about {whole(n / 30.4)} months"


def price_level(rank: float) -> str:
    """Where option prices sat against their past year (no jargon in copy)."""
    if rank < 30:
        return "low"
    if rank <= 60:
        return "average"
    return "high"


def names(items: list[str]) -> str:
    if len(items) <= 2:
        return " and ".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]
