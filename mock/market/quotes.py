"""Theoretical prices from pricing_reference.py, simulated bid/ask, and fill prices.

Every option price goes through pricing_reference.select_pricing_model, so the model is
chosen from the contract's terms, never from an asset-class shortcut:
  equity / ETF option  -> American, physical, spot carry: CRR with discrete dividends,
                          continuous yield (ETFs) or no dividends
  SPX (SPXW)           -> European, cash-settled, spot carry: BSM with continuous yield
  /MES, /MNQ options   -> American, physical into the future, observed forward: CRR b=0

Inputs are rounded *before* pricing (underlying to 4 places, vol to 6) and stored in the
fill's market context, so anyone can re-price a fill exactly from the raw data.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from datetime import datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal

import pricing_reference as pr

from mock.market import calendar as cal
from mock.market.listings import Instrument
from mock.market.paths import OFF_HOURS_WEIGHT

CENT = Decimal("0.01")
PRICE_PLACES = Decimal("0.0001")
VOL_PLACES = Decimal("0.000001")


@dataclass(frozen=True)
class Quote:
    theo: Decimal
    bid: Decimal
    ask: Decimal
    underlying_price: Decimal
    iv: Decimal
    model_id: str


# ---------------------------------------------------------------------------
# Ticks
# ---------------------------------------------------------------------------

def option_ticks(world, inst: Instrument) -> tuple[Decimal, Decimal]:
    """(tick below $3, tick at or above $3)."""
    if inst.asset_class == "future_option":
        return Decimal("0.25"), Decimal("0.25")
    if inst.asset_class == "index_option":
        return Decimal("0.05"), Decimal("0.10")
    if world.symbols[inst.underlying].get("liquid"):
        return Decimal("0.01"), Decimal("0.05")
    return Decimal("0.05"), Decimal("0.10")


def _floor(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(ROUND_FLOOR) * step


def _ceil(x: Decimal, step: Decimal) -> Decimal:
    return (x / step).to_integral_value(ROUND_CEILING) * step


def floor_tick(x: Decimal, ticks: tuple[Decimal, Decimal]) -> Decimal:
    small, big = ticks
    return _floor(x, small) if x < 3 else _floor(x, big)


def ceil_tick(x: Decimal, ticks: tuple[Decimal, Decimal]) -> Decimal:
    small, big = ticks
    return _ceil(x, small) if x <= 3 else _ceil(x, big)


def tick_grid(lo: Decimal, hi: Decimal, ticks: tuple[Decimal, Decimal]) -> list[Decimal]:
    out, p = [], lo
    while p <= hi:
        out.append(p)
        p += ticks[0] if p < 3 else ticks[1]
    return out


# ---------------------------------------------------------------------------
# Theoretical value
# ---------------------------------------------------------------------------

def settlement_utc(inst: Instrument) -> datetime:
    return cal.at_ny_minutes(inst.expiration, cal.regular_close_minute(inst.expiration))


def contract_record(world, inst: Instrument, now_date) -> dict:
    if inst.asset_class == "future_option":
        fut_sym = inst.symbol.split(" ")[0]
        return {"exercise_style": "AMERICAN", "settlement_method": "PHYSICAL",
                "forward_basis": pr.FORWARD_BASIS_OBSERVED_FUTURE,
                "forward_reference_instrument": fut_sym}
    if inst.asset_class == "index_option":
        return {"exercise_style": "EUROPEAN", "settlement_method": "CASH",
                "forward_basis": pr.FORWARD_BASIS_SPOT_CARRY, "dividend_mode": "CONTINUOUS_YIELD"}
    spec = world.symbols[inst.underlying]
    if world.dividends_between(inst.underlying, now_date, inst.expiration):
        mode = "DISCRETE"
    elif spec.get("yield"):
        mode = "CONTINUOUS_YIELD"
    else:
        mode = "NONE"
    return {"exercise_style": "AMERICAN", "settlement_method": "PHYSICAL",
            "forward_basis": pr.FORWARD_BASIS_SPOT_CARRY, "dividend_mode": mode,
            "adjusted": False}


_theo_cache: dict[tuple, tuple[float, str]] = {}


def theo(world, inst: Instrument, ts_utc: datetime, s: Decimal, iv: Decimal) -> tuple[float, str]:
    """Theoretical value per unit of underlying, and the model that produced it."""
    key = (world.seed, inst.symbol, ts_utc, s, iv)
    hit = _theo_cache.get(key)
    if hit is not None:
        return hit
    now_date = cal.ny_date(ts_utc)
    contract = contract_record(world, inst, now_date)
    model, _notes = pr.select_pricing_model(contract)
    settle = settlement_utc(inst)
    T = pr.year_fraction(ts_utc, settle)
    S, K, sigma, r = float(s), float(inst.strike), float(iv), world.rate
    right = inst.put_call
    steps = int(world.defaults["pricer_steps"])
    if T <= 0:
        value = max(0.0, S - K) if right == "C" else max(0.0, K - S)
    elif model == "BSM-continuous-q":
        q = float(world.symbols[inst.underlying].get("yield", 0.0))
        value = float(pr.bsm_price(S, K, T, r, q, sigma, right))
    elif model == "CRR-1000-b0":
        value = pr.crr_american_futures(S, K, T, r, sigma, right, steps=steps)
    elif model == "CRR-1000-discrete-div":
        divs = []
        for ex, amt in world.dividends_between(inst.underlying, now_date, inst.expiration):
            t = pr.year_fraction(ts_utc, cal.at_ny(ex, 4))
            if 0 < t < T:
                divs.append((t, float(amt)))
        value = pr.crr_american(S, K, T, r, sigma, right, steps=steps, dividends=divs)
    elif model == "CRR-1000-continuous-q":
        q = world.symbols[inst.underlying].get("yield")
        value = pr.crr_american(S, K, T, r, sigma, right, steps=steps,
                                q=None if q is None else float(q))
    else:  # pragma: no cover - routing table is closed
        raise ValueError(f"unexpected model {model}")
    _theo_cache[key] = (float(value), model)
    return float(value), model


def delta(world, inst: Instrument, ts_utc: datetime, s: float, iv: float) -> float:
    """Fast European delta, used only for picking strikes and for engine decisions."""
    T = pr.year_fraction(ts_utc, settlement_utc(inst))
    if T <= 0:
        itm = (s > float(inst.strike)) if inst.put_call == "C" else (s < float(inst.strike))
        return (1.0 if itm else 0.0) * (1 if inst.put_call == "C" else -1)
    if inst.asset_class == "future_option":
        return pr.black76_greeks(s, float(inst.strike), T, world.rate, iv, inst.put_call)["delta"]
    q = 0.0
    if inst.asset_class != "future_option":
        q = float(world.symbols[inst.underlying].get("yield", 0.0))
    return float(pr.bsm_greeks(s, float(inst.strike), T, world.rate, q, iv, inst.put_call)["delta"])


# ---------------------------------------------------------------------------
# Market inputs, quotes, fills
# ---------------------------------------------------------------------------

SHORT_DATED_DAYS = 7


def _session_weight(d, a_min: float, b_min: float) -> float:
    """Variance weight of the stretch [a_min, b_min] (minutes after midnight, New York)
    of day d's 04:00-20:00 stock session, as a share of the whole day's variance.
    Mirrors paths.World._weights."""
    lo, hi = 4 * 60, (17 * 60 if cal.is_half_day(d) else 20 * 60)
    ro, rc = 9 * 60 + 30, cal.regular_close_minute(d)
    a_min, b_min = max(a_min, lo), min(b_min, hi)
    if b_min <= a_min:
        return 0.0
    reg = max(0.0, min(b_min, rc) - max(a_min, ro))
    total_reg = rc - ro
    w = OFF_HOURS_WEIGHT
    return (reg + w * ((b_min - a_min) - reg)) / (total_reg + w * ((hi - lo) - total_reg))


def contract_vol(world, inst: Instrument, ts_utc: datetime, day_iv: float) -> float:
    """The vol quoted for one contract.

    The pricer measures time in calendar hours, but simulated stock and index prices
    make about 90% of each day's move in the 6.5 regular-session hours (paths.World).
    Over weeks the two clocks agree on average; for stock and index options ending
    within a week they do not, and a same-day option priced on the calendar clock is
    far too cheap for the movement left. So short-dated contracts are quoted at the
    day's vol scaled by sqrt(trading time left / calendar time left), keeping them as
    rich relative to actual movement as longer-dated options."""
    if inst.asset_class not in ("equity_option", "index_option"):
        return day_iv
    if (inst.expiration - cal.ny_date(ts_utc)).days > SHORT_DATED_DAYS:
        return day_iv
    settle = settlement_utc(inst)
    T = pr.year_fraction(ts_utc, settle)
    if T <= 0:
        return day_iv
    days, d = 0.0, cal.ny_date(ts_utc)
    while d <= inst.expiration:
        if cal.is_trading_day(d):
            midnight = cal.at_ny(d, 0)
            a = max(0.0, (ts_utc - midnight).total_seconds() / 60)
            b = min(24 * 60.0, (settle - midnight).total_seconds() / 60)
            days += _session_weight(d, a, b)
        d += timedelta(days=1)
    if days <= 0:
        return day_iv
    return day_iv * math.sqrt(days / 252 / T)


def inputs(world, inst: Instrument, ts_utc: datetime) -> tuple[Decimal, Decimal]:
    """Rounded underlying price and vol for an instant: exactly what the fill's
    market context records."""
    s = Decimal(repr(world.price_at(inst.price_symbol, ts_utc))).quantize(PRICE_PLACES, ROUND_HALF_EVEN)
    d = world.session_day(inst.price_symbol, ts_utc)[0]
    v = contract_vol(world, inst, ts_utc, world.iv(inst.price_symbol, d))
    iv = Decimal(repr(v)).quantize(VOL_PLACES, ROUND_HALF_EVEN)
    return s, iv


def half_spread_share(world, inst: Instrument, ts_utc: datetime) -> Decimal:
    cfg = world.defaults["option_spread"]
    liquid = inst.asset_class == "index_option" or (
        inst.asset_class == "equity_option" and world.symbols[inst.underlying].get("liquid"))
    share = Decimal(str(cfg["liquid"] if liquid else cfg["other"]))
    dte = (inst.expiration - cal.ny_date(ts_utc)).days
    if cal.session_of(ts_utc) != "regular" or dte <= 1:
        share *= Decimal(str(cfg["outside_hours_or_last_day_multiplier"]))
    return share


def quote(world, inst: Instrument, ts_utc: datetime) -> Quote:
    s, iv = inputs(world, inst, ts_utc)
    value, model = theo(world, inst, ts_utc, s, iv)
    t = Decimal(repr(value))
    ticks = option_ticks(world, inst)
    half = max(ticks[0], t * half_spread_share(world, inst, ts_utc))
    bid = max(Decimal(0), floor_tick(t - half, ticks))
    ask = max(ticks[0], ceil_tick(t + half, ticks))
    return Quote(_q4(t), bid, ask, s, iv, model)


def _q4(x: Decimal) -> Decimal:
    return x.quantize(PRICE_PLACES, ROUND_HALF_EVEN)


def option_fill_price(world, inst: Instrument, q: Quote, side: str, rng, at_mid: bool = False) -> Decimal:
    """A price on the tick grid inside [bid, ask]: buyers pay mid or worse,
    sellers receive mid or worse. A patient order (`at_mid`) gets the tick
    nearest the mid on that side."""
    grid = tick_grid(q.bid, q.ask, option_ticks(world, inst))
    mid = (q.bid + q.ask) / 2
    if side == "buy":
        cands = [p for p in grid if p >= mid and p > 0]
    else:
        cands = [p for p in grid if p <= mid and p > 0]
    if not cands:
        cands = [q.ask]
    if at_mid:
        return min(cands) if side == "buy" else max(cands)
    return cands[int(rng.integers(len(cands)))]


def underlying_tick(world, inst: Instrument) -> Decimal:
    if inst.asset_class == "future":
        return Decimal(str(world.symbols[inst.price_symbol]["tick"]))
    return CENT


def linear_fill_price(world, inst: Instrument, ts_utc: datetime, side: str) -> Decimal:
    """Stocks, futures, crypto: one tick (crypto: 5 basis points) worse than the
    path price, kept inside the session's low and high."""
    tick = underlying_tick(world, inst)
    p = world.price_at(inst.price_symbol, ts_utc)
    d, _ = world.session_day(inst.price_symbol, ts_utc)
    bar = world.bar(inst.price_symbol, d, tick)
    if inst.asset_class == "crypto":
        raw = Decimal(repr(p * (1.0005 if side == "buy" else 0.9995)))
    else:
        raw = Decimal(repr(p)) + (tick if side == "buy" else -tick)
    px = (raw / tick).to_integral_value(ROUND_HALF_EVEN) * tick
    return min(max(px, bar["low"]), bar["high"])


def mark(world, inst: Instrument, ts_utc: datetime) -> Decimal:
    """Month-end / month-start valuation price per unit."""
    if inst.is_option:
        if cal.ny_date(ts_utc) >= inst.expiration and ts_utc >= settlement_utc(inst):
            s = Decimal(repr(world.price_at(inst.price_symbol, settlement_utc(inst))))
            k = inst.strike
            return _q4(max(Decimal(0), s - k) if inst.put_call == "C" else max(Decimal(0), k - s))
        s, iv = inputs(world, inst, ts_utc)
        return _q4(Decimal(repr(theo(world, inst, ts_utc, s, iv)[0])))
    tick = underlying_tick(world, inst)
    p = Decimal(repr(world.price_at(inst.price_symbol, ts_utc)))
    return (p / tick).to_integral_value(ROUND_HALF_EVEN) * tick
