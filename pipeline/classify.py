"""Stage 2a: rule-based strategy classifier, order effects and roll types. Never AI.

Definitions are in schemas/trades.py. Everything here is a pure function of the legs
it is given, so each rule can be unit-tested with hand-built positions.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from schemas.trades import OrderEffect, RollType, StrategyLabel as L

ZERO = Decimal(0)
OPTION_CLASSES = ("equity_option", "index_option", "future_option")


@dataclass(frozen=True)
class Instrument:
    symbol: str
    asset_class: str
    underlying: str
    multiplier: int
    strike: Decimal | None = None
    expiration: date | None = None
    put_call: str | None = None          # "C" | "P"
    contract_month: str | None = None

    @property
    def is_option(self) -> bool:
        return self.asset_class in OPTION_CLASSES


class Confidence(StrEnum):
    exact = "exact"          # follows from the legs alone
    inferred = "inferred"    # also needed the cash balance, or fell back to custom


@dataclass(frozen=True)
class Leg:
    inst: Instrument
    qty: Decimal             # signed: negative = short


# ---------------------------------------------------------------------------
# Strategy label
# ---------------------------------------------------------------------------

def classify(legs: Iterable[Leg], cash: Decimal | None = None) -> tuple[L, Confidence]:
    """Label one underlying's whole position.

    `cash` is the account's cash balance at that moment, if known. A lone short put on
    a stock or ETF is a cash-secured put when cash covers strike x multiplier x
    contracts; without cash data it is a plain short put. Index and futures puts are
    never cash-secured (they settle in cash or into a future, not by buying shares)."""
    legs = [l for l in legs if l.qty != 0]
    if not legs:
        raise ValueError("cannot classify an empty position")
    opts = [l for l in legs if l.inst.is_option]
    lin = [l for l in legs if not l.inst.is_option]
    kinds = {l.inst.asset_class for l in lin}

    if not opts:
        if len(kinds) != 1 or len({l.inst.symbol for l in lin}) != 1:
            return L.custom, Confidence.inferred
        kind, q = kinds.pop(), lin[0].qty
        long_ = q > 0
        if kind == "stock_etf":
            return (L.stock_long if long_ else L.stock_short), Confidence.exact
        if kind == "future":
            return (L.future_long if long_ else L.future_short), Confidence.exact
        if kind == "crypto":
            return (L.crypto_long if long_ else L.crypto_short), Confidence.exact
        return L.custom, Confidence.inferred

    if lin:
        return _with_shares(lin, opts)
    if len({l.inst.multiplier for l in opts}) != 1:
        return L.custom, Confidence.inferred
    return _options_only(opts, cash)


def _with_shares(lin: list[Leg], opts: list[Leg]) -> tuple[L, Confidence]:
    """Covered call: long shares plus short calls, at least one round lot of shares
    (the option's multiplier) per contract."""
    if len(lin) == 1 and lin[0].inst.asset_class == "stock_etf" and lin[0].qty > 0:
        calls = [l for l in opts if l.inst.put_call == "C"]
        if len(calls) == len(opts) and all(l.qty < 0 for l in calls):
            covered = sum((-l.qty * l.inst.multiplier for l in calls), ZERO)
            if lin[0].qty >= covered:
                return L.covered_call, Confidence.exact
    return L.custom, Confidence.inferred


def _options_only(opts: list[Leg], cash: Decimal | None) -> tuple[L, Confidence]:
    if len(opts) == 1:
        l = opts[0]
        short = l.qty < 0
        if l.inst.put_call == "C":
            return (L.short_call if short else L.long_call), Confidence.exact
        if not short:
            return L.long_put, Confidence.exact
        if cash is None or l.inst.asset_class != "equity_option":
            return L.short_put, Confidence.exact
        need = -l.qty * l.inst.strike * l.inst.multiplier
        return (L.cash_secured_put if cash >= need else L.short_put), Confidence.inferred

    sizes = {abs(l.qty) for l in opts}
    exps = {l.inst.expiration for l in opts}
    types = {l.inst.put_call for l in opts}

    if len(opts) == 2:
        a, b = sorted(opts, key=lambda l: (l.inst.put_call, l.inst.strike, l.inst.expiration))
        if len(sizes) != 1:
            return L.custom, Confidence.inferred
        if len(types) == 1 and (a.qty > 0) != (b.qty > 0):
            if len(exps) == 1:
                if a.inst.strike == b.inst.strike:
                    return L.custom, Confidence.inferred
                short = a if a.qty < 0 else b
                long_ = b if short is a else a
                # credit when the sold option is the more valuable one
                credit = (short.inst.strike > long_.inst.strike) if a.inst.put_call == "P" \
                    else (short.inst.strike < long_.inst.strike)
                return (L.credit_vertical if credit else L.debit_vertical), Confidence.exact
            return (L.calendar if a.inst.strike == b.inst.strike else L.diagonal), Confidence.exact
        if len(types) == 2 and len(exps) == 1 and (a.qty > 0) == (b.qty > 0):
            short = a.qty < 0
            if a.inst.strike == b.inst.strike:
                return (L.short_straddle if short else L.long_straddle), Confidence.exact
            return (L.short_strangle if short else L.long_strangle), Confidence.exact
        return L.custom, Confidence.inferred

    if len(opts) == 3 and len(types) == 1 and len(exps) == 1:
        lo, mid, hi = sorted(opts, key=lambda l: l.inst.strike)
        if (lo.qty == hi.qty and mid.qty == -2 * lo.qty
                and lo.inst.strike < mid.inst.strike < hi.inst.strike):
            equal = mid.inst.strike - lo.inst.strike == hi.inst.strike - mid.inst.strike
            return (L.butterfly if equal else L.broken_wing_butterfly), Confidence.exact
        return L.custom, Confidence.inferred

    if len(opts) == 4 and len(exps) == 1 and len(sizes) == 1 and types == {"C", "P"}:
        puts = sorted((l for l in opts if l.inst.put_call == "P"), key=lambda l: l.inst.strike)
        calls = sorted((l for l in opts if l.inst.put_call == "C"), key=lambda l: l.inst.strike)
        if len(puts) == 2 and len(calls) == 2:
            # sold the inner strikes, bought the outer ones
            if puts[0].qty > 0 > puts[1].qty and calls[0].qty < 0 < calls[1].qty \
                    and puts[1].inst.strike <= calls[0].inst.strike:
                if puts[1].inst.strike == calls[0].inst.strike:
                    return L.iron_butterfly, Confidence.exact
                return L.iron_condor, Confidence.exact
    return L.custom, Confidence.inferred


# ---------------------------------------------------------------------------
# Order effect
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Change:
    """How one order changed the story's position (symbol -> signed qty)."""
    reduced: frozenset[str]
    removed: frozenset[str]
    increased: frozenset[str]
    new: frozenset[str]
    flat_after: bool
    was_empty: bool

    @property
    def only_closing(self) -> bool:
        return bool(self.reduced) and not self.increased

    @property
    def only_opening(self) -> bool:
        return bool(self.increased) and not self.reduced


def change(before: Mapping[str, Decimal], after: Mapping[str, Decimal]) -> Change:
    return Change(
        reduced=frozenset(s for s in before if abs(after.get(s, ZERO)) < abs(before[s])),
        removed=frozenset(s for s in before if s not in after),
        increased=frozenset(s for s in after if abs(after[s]) > abs(before.get(s, ZERO))),
        new=frozenset(s for s in after if s not in before),
        flat_after=not after,
        was_empty=not before,
    )


def order_effect(c: Change) -> OrderEffect:
    """One order on its own. Two-order rolls are joined later by the linker."""
    if c.was_empty:
        return OrderEffect.open
    if c.flat_after:
        return OrderEffect.close
    if c.reduced and c.increased:
        return OrderEffect.roll
    if c.only_closing:
        return OrderEffect.adjust if c.removed else OrderEffect.partial_close
    if c.new:
        return OrderEffect.adjust
    return OrderEffect.add


# ---------------------------------------------------------------------------
# Roll types
# ---------------------------------------------------------------------------

def roll_types(before: Mapping[str, Decimal], after: Mapping[str, Decimal],
               insts: Mapping[str, Instrument], spot: Decimal) -> list[RollType]:
    """Types of one roll, judged on the position before and after it and the
    underlying price at the roll. Closed legs pair with opened legs of the same
    put/call type and side, in strike order."""

    def changed(src, other):
        out = []
        for s, q in src.items():
            if abs(other.get(s, ZERO)) < abs(q) and insts[s].is_option:
                out.append((insts[s].put_call, "short" if q < 0 else "long", insts[s]))
        return out

    closed, opened = changed(before, after), changed(after, before)
    shorts = {pc: [insts[s] for s, q in before.items()
                   if q < 0 and insts[s].is_option and insts[s].put_call == pc] for pc in ("C", "P")}
    tested = None
    if shorts["C"] and shorts["P"]:
        kc = min(i.strike for i in shorts["C"])
        kp = max(i.strike for i in shorts["P"])
        tested = "C" if abs(kc - spot) < abs(spot - kp) else "P"

    types: set[RollType] = set()
    for pc in ("C", "P"):
        for side in ("short", "long"):
            olds = sorted((i for p, sd, i in closed if p == pc and sd == side), key=lambda i: (i.strike, i.expiration))
            news = sorted((i for p, sd, i in opened if p == pc and sd == side), key=lambda i: (i.strike, i.expiration))
            for o, n in zip(olds, news):
                if n.expiration > o.expiration:
                    types.add(RollType.out_in_time)
                if n.strike > o.strike:
                    types.add(RollType.up)
                elif n.strike < o.strike:
                    types.add(RollType.down)
                if tested and side == "short":
                    if pc != tested and abs(n.strike - spot) < abs(o.strike - spot):
                        types.add(RollType.untested_toward_money)
                    if pc == tested and abs(n.strike - spot) > abs(o.strike - spot):
                        types.add(RollType.tested_away_from_money)
    wb, wa = width(before, insts), width(after, insts)
    if wb is not None and wa is not None:
        if wa > wb:
            types.add(RollType.widened)
        elif wa < wb:
            types.add(RollType.narrowed)
    return sorted(types)


def width(pos: Mapping[str, Decimal], insts: Mapping[str, Instrument]) -> Decimal | None:
    """Lowest short call strike minus highest short put strike when both are short;
    otherwise the strike distance between the short and long leg of one type
    (puts first; the lowest strike of each when there are several)."""
    def strikes(pc, short):
        return sorted(insts[s].strike for s, q in pos.items()
                      if insts[s].is_option and insts[s].put_call == pc and (q < 0) == short)
    sc, sp = strikes("C", True), strikes("P", True)
    if sc and sp:
        return min(sc) - max(sp)
    for pc in ("P", "C"):
        sh, lg = strikes(pc, True), strikes(pc, False)
        if sh and lg:
            return abs(sh[0] - lg[0])
    return None
