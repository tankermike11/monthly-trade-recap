"""Instruments, listed expirations and strike grids."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from decimal import Decimal

from mock.market import calendar as cal

FUTURES_MONTH_CODES = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
                       7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}
WEEKLY_HORIZON_DAYS = 42
SPX_DAILY_HORIZON_DAYS = 45


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: str
    underlying: str           # stock / index symbol, futures root (/MES) or coin (BTC)
    price_symbol: str         # the world path that prices the underlying (ES for /MES)
    multiplier: int
    strike: Decimal | None = None
    expiration: date | None = None
    put_call: str | None = None
    contract_month: str | None = None
    adjusted: bool = False    # strike changed by a split

    @property
    def is_option(self) -> bool:
        return self.put_call is not None

    @property
    def is_future(self) -> bool:
        return self.asset_class == "future"


def _fmt_strike(k: Decimal) -> str:
    return format(k.normalize(), "f")


def occ_symbol(root: str, exp: date, pc: str, strike: Decimal) -> str:
    return f"{root:<6}{exp:%y%m%d}{pc}{int(strike * 1000):08d}"


def stock(sym: str) -> Instrument:
    return Instrument(sym, "stock_etf", sym, sym, 1)


def crypto(coin: str) -> Instrument:
    return Instrument(f"{coin}-USD", "crypto", coin, coin, 1)


def future(world, root: str) -> Instrument:
    spec = world.futures[root]
    cm = str(spec["contract_month"])
    y, m = (int(x) for x in cm.split("-"))
    return Instrument(f"{root}{FUTURES_MONTH_CODES[m]}{y % 100}", "future", root,
                      spec["tracks"], int(spec["multiplier"]), contract_month=cm)


def option(world, underlying: str, exp: date, pc: str, strike: Decimal) -> Instrument:
    strike = Decimal(strike)
    if underlying in world.futures:
        fut = future(world, underlying)
        sym = f"{fut.symbol} {exp:%y%m%d}{pc}{_fmt_strike(strike)}"
        return Instrument(sym, "future_option", underlying, fut.price_symbol, fut.multiplier,
                          strike, exp, pc, fut.contract_month)
    kind = world.kind(underlying)
    if kind == "index":
        return Instrument(occ_symbol("SPXW", exp, pc, strike), "index_option", underlying,
                          underlying, 100, strike, exp, pc)
    return Instrument(occ_symbol(underlying, exp, pc, strike), "equity_option", underlying,
                      underlying, 100, strike, exp, pc)


def split_adjusted(inst: Instrument, ratio: int) -> Instrument:
    """Standard OCC adjustment for an N-for-1 split: strike / N, contracts x N,
    multiplier unchanged, so the deliverable stays 100 shares per contract."""
    k = inst.strike / ratio
    return replace(inst, symbol=occ_symbol(inst.underlying, inst.expiration, inst.put_call, k),
                   strike=k, adjusted=True)


# ---------------------------------------------------------------------------
# Expirations
# ---------------------------------------------------------------------------

def _friday_or_thursday(d: date) -> date:
    return d if cal.is_trading_day(d) else cal.prev_trading_day(d)


def monthly_expirations(after: date, months: int = 13) -> list[date]:
    out = []
    y, m = after.year, after.month
    for _ in range(months):
        tf = cal.third_friday(y, m)
        if tf >= after:
            out.append(tf)
        y, m = y + (m == 12), m % 12 + 1
    return out


def listed_expirations(world, underlying: str, trade_date: date) -> list[date]:
    if underlying in world.futures:
        cm = str(world.futures[underlying]["contract_month"])
        y, m = (int(x) for x in cm.split("-"))
        last = cal.third_friday(y, m)
        return [d for d in monthly_expirations(trade_date) if d <= last]
    exps = set(monthly_expirations(trade_date))
    kind = world.kind(underlying)
    if kind == "index":
        d = trade_date
        while (d - trade_date).days <= SPX_DAILY_HORIZON_DAYS:
            if cal.is_trading_day(d):
                exps.add(d)
            d += timedelta(days=1)
    elif world.symbols[underlying].get("liquid"):
        d = trade_date + timedelta(days=(4 - trade_date.weekday()) % 7)
        while (d - trade_date).days <= WEEKLY_HORIZON_DAYS:
            fd = _friday_or_thursday(d)
            if fd >= trade_date:
                exps.add(fd)
            d += timedelta(weeks=1)
    return sorted(exps)


def is_monthly(d: date) -> bool:
    return d == cal.third_friday(d.year, d.month)


def strike_step(world, underlying: str) -> Decimal:
    if underlying in world.futures:
        return Decimal(str(world.futures[underlying]["option_step"]))
    return Decimal(str(world.symbols[underlying]["step"]))


def on_grid(world, inst: Instrument) -> bool:
    step = strike_step(world, inst.underlying)
    if inst.adjusted:
        return True
    return inst.strike % step == 0


def pick_expiration(world, underlying: str, trade_date: date, aim: int, lo: int, hi: int,
                    monthly_only: bool) -> date | None:
    """Listed expiration closest to `aim` calendar days, within [lo, hi]."""
    cands = []
    for e in listed_expirations(world, underlying, trade_date):
        dte = (e - trade_date).days
        if lo <= dte <= hi and (is_monthly(e) or not monthly_only):
            cands.append((abs(dte - aim), dte, e))
    return min(cands)[2] if cands else None


def instrument_from_fill(world, fill) -> Instrument:
    """Rebuild the instrument a raw Fill (or fill dict) refers to."""
    g = (lambda k: fill[k]) if isinstance(fill, dict) else (lambda k: getattr(fill, k))
    ac = str(g("asset_class"))
    und = g("underlying")
    if ac in ("future", "future_option"):
        price_symbol = world.futures[und]["tracks"]
    elif ac == "crypto":
        price_symbol = und
    else:
        price_symbol = und
    pc = g("put_call")
    return Instrument(g("symbol"), ac, und, price_symbol, int(g("multiplier")),
                      None if g("strike") is None else Decimal(g("strike")).normalize(),
                      g("expiration"), None if pc is None else str(pc), g("contract_month"))
