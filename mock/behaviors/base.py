"""Shared machinery for behaviour engines.

An engine reads one persona's YAML rules and, day by day, tells the ledger what the
customer does. Engines decide with fast European approximations (BSM / Black-76);
the ledger prices every actual fill with pricing_reference's routed model.
"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from decimal import Decimal

import pricing_reference as pr

from mock.ledger import Ledger, LegReq
from mock.market import calendar as cal
from mock.market import listings as L
from mock.market import quotes as Q
from mock.market.paths import World
from mock.rng import stream

ZERO = Decimal(0)


def pct(x) -> float:
    """'50%' or 0.5 -> 0.5"""
    if isinstance(x, str) and x.endswith("%"):
        return float(x[:-1]) / 100
    return float(x)


def rng_range(rng, spec) -> int:
    if isinstance(spec, dict):
        return int(rng.integers(int(spec["from"]), int(spec["to"]) + 1))
    return int(spec)


class Engine:
    def __init__(self, world: World, ledger: Ledger, persona: dict):
        self.world = world
        self.ledger = ledger
        self.p = persona
        self.tz = persona["time_zone"]
        self.seed = world.seed

    # ------------------------------------------------------------------ randomness and time

    def rng(self, day: date, purpose: str):
        return stream(self.seed, self.p["id"], purpose, day.isoformat())

    def window(self, day: date, spec: str) -> tuple[datetime, datetime]:
        """'08:45-10:30' in the persona's time zone -> UTC bounds on `day`."""
        a, b = spec.split("-")
        ha, ma = (int(x) for x in a.split(":"))
        hb, mb = (int(x) for x in b.split(":"))
        start = cal.to_utc(datetime.combine(day, datetime.min.time()) + timedelta(hours=ha, minutes=ma), self.tz)
        end = cal.to_utc(datetime.combine(day, datetime.min.time()) + timedelta(hours=hb, minutes=mb), self.tz)
        return start, end

    def at_local(self, day: date, hhmm: str) -> datetime:
        h, m = (int(x) for x in hhmm.split(":"))
        return cal.to_utc(datetime.combine(day, datetime.min.time()) + timedelta(hours=h, minutes=m), self.tz)

    @staticmethod
    def random_minute(rng, start: datetime, end: datetime) -> datetime:
        n = int((end - start).total_seconds() // 60)
        return start + timedelta(minutes=int(rng.integers(0, max(n, 1))))

    def regular_cutoff(self, day: date, minutes_before_close: int = 15) -> datetime:
        return cal.at_ny_minutes(day, cal.regular_close_minute(day) - minutes_before_close)

    # ------------------------------------------------------------------ options helpers

    def spot(self, inst_or_sym, ts: datetime) -> float:
        sym = inst_or_sym if isinstance(inst_or_sym, str) else inst_or_sym.price_symbol
        return self.world.price_at(sym, ts)

    def vol(self, inst: L.Instrument, ts: datetime) -> float:
        return Q.contract_vol(self.world, inst, ts, self.world.iv(inst.price_symbol, cal.ny_date(ts)))

    def fair(self, inst: L.Instrument, ts: datetime) -> float:
        """Fast European value per unit, for decisions only."""
        s = self.spot(inst, ts)
        if not inst.is_option:
            return s
        T = pr.year_fraction(ts, Q.settlement_utc(inst))
        k = float(inst.strike)
        if T <= 0:
            return max(0.0, s - k) if inst.put_call == "C" else max(0.0, k - s)
        sigma = self.vol(inst, ts)
        if inst.asset_class == "future_option":
            return pr.black76_price(s, k, T, self.world.rate, sigma, inst.put_call)
        q = float(self.world.symbols[inst.underlying].get("yield", 0.0))
        return float(pr.bsm_price(s, k, T, self.world.rate, q, sigma, inst.put_call))

    def delta(self, inst: L.Instrument, ts: datetime) -> float:
        return Q.delta(self.world, inst, ts, self.spot(inst, ts), self.vol(inst, ts))

    def strike_by_delta(self, underlying: str, exp: date, pc: str, target: float, ts: datetime,
                        at_least: Decimal | None = None, at_most: Decimal | None = None) -> Decimal:
        """Listed strike whose |delta| is closest to `target`."""
        step = L.strike_step(self.world, underlying)
        probe = L.option(self.world, underlying, exp, pc, step)
        s = self.spot(probe, ts)
        sigma = self.vol(probe, ts)
        T = max(pr.year_fraction(ts, Q.settlement_utc(probe)), 1 / 365 / 24)
        span = s * min(0.9, 4.5 * sigma * math.sqrt(T) + 0.02)
        lo = Decimal(math.floor((s - span) / float(step))) * step
        hi = Decimal(math.ceil((s + span) / float(step))) * step
        best, k = None, max(lo, step)
        while k <= hi:
            if (at_least is None or k >= at_least) and (at_most is None or k <= at_most):
                d = abs(self.delta(L.option(self.world, underlying, exp, pc, k), ts))
                score = (abs(d - target), k)
                if best is None or score < best[0]:
                    best = (score, k)
            k += step
        if best is None:
            raise RuntimeError(f"no strike for {underlying} {exp} {pc} near delta {target}")
        return best[1]

    def opt(self, underlying: str, exp: date, pc: str, strike) -> L.Instrument:
        return L.option(self.world, underlying, exp, pc, Decimal(strike))

    def legs_value(self, underlying: str, ts: datetime) -> float:
        """Signed fair value of the story's open legs (long positive, short negative)."""
        total = 0.0
        for pos in self.ledger.story_legs(underlying):
            total += float(pos.qty) * self.fair(pos.inst, ts) * pos.inst.multiplier
        return total

    def close_all(self, underlying: str, ts: datetime, at_mid: bool = False) -> str:
        legs = [LegReq(p.inst, "buy" if p.qty < 0 else "sell", abs(p.qty))
                for p in self.ledger.story_legs(underlying)]
        return self.ledger.trade(ts, legs, at_mid=at_mid)

    def tradable(self, sym: str, ts: datetime) -> bool:
        return self.world.is_open(sym, ts) and cal.ny_date(ts) not in cal.HOLIDAYS

    # ------------------------------------------------------------------ carried-in positions

    def seed_option(self, underlying: str, exp: date, pc: str, target_delta: float, opened: date,
                    contracts: int, side: str = "sell") -> L.Instrument:
        ts = cal.at_ny(opened, 11)
        k = self.strike_by_delta(underlying, exp, pc, target_delta, ts)
        inst = self.opt(underlying, exp, pc, k)
        q = Q.quote(self.world, inst, ts)
        qty = Decimal(-contracts if side == "sell" else contracts)
        self.ledger.seed_position(inst, qty, q.theo, opened, "custom")
        return inst

    def seed_stock(self, sym: str, shares, opened: date) -> None:
        ts = cal.at_ny(opened, 11)
        price = Decimal(repr(round(self.world.price_at(sym, ts), 2)))
        self.ledger.seed_position(L.stock(sym), Decimal(shares), price, opened, "stock_long")

    # ------------------------------------------------------------------ interface

    def setup(self) -> None:
        """Create positions that were opened before October (no fills are written)."""

    def on_day(self, day: date) -> None:
        raise NotImplementedError
