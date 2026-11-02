"""Phase 2 acceptance: unit tests cover each label and each roll type with a hand-built
order. Also the order effects."""

from __future__ import annotations

from datetime import date
from decimal import Decimal as D

import pytest

from pipeline.classify import Instrument, Leg, change, classify, order_effect, roll_types, width
from schemas.trades import OrderEffect, RollType, StrategyLabel

NOV, DEC, JAN = date(2026, 11, 20), date(2026, 12, 18), date(2027, 1, 15)


def opt(pc, k, exp=NOV, und="XYZ", cls="equity_option", mult=100):
    k = D(str(k))
    return Instrument(f"{und} {exp:%y%m%d}{pc}{k}", cls, und, mult, k, exp, pc)


def stock(und="XYZ"):
    return Instrument(und, "stock_etf", und, 1)


FUT = Instrument("/MES 2026-12", "future", "/MES", 5, contract_month="2026-12")
BTC = Instrument("BTC", "crypto", "BTC", 1)


def leg(inst, q):
    return Leg(inst, D(q))


def label(*legs, cash=None):
    return classify(legs, cash)[0]


# ---------------------------------------------------------------------------
# Every label in StrategyLabel, from hand-built positions
# ---------------------------------------------------------------------------

CASES = {
    "stock_long": [leg(stock(), 100)],
    "stock_short": [leg(stock(), -50)],
    "long_call": [leg(opt("C", 50), 1)],
    "long_put": [leg(opt("P", 50), 2)],
    "short_call": [leg(opt("C", 50), -1)],
    "short_put": [leg(opt("P", 50), -1)],                       # no cash data
    "covered_call": [leg(stock(), 200), leg(opt("C", 55), -2)],
    "credit_vertical": [leg(opt("P", 50), -1), leg(opt("P", 45), 1)],
    "debit_vertical": [leg(opt("C", 50), 1), leg(opt("C", 55), -1)],
    "short_strangle": [leg(opt("P", 45), -1), leg(opt("C", 55), -1)],
    "long_strangle": [leg(opt("P", 45), 1), leg(opt("C", 55), 1)],
    "short_straddle": [leg(opt("P", 50), -3), leg(opt("C", 50), -3)],
    "long_straddle": [leg(opt("P", 50), 1), leg(opt("C", 50), 1)],
    "iron_condor": [leg(opt("P", 40), 1), leg(opt("P", 45), -1), leg(opt("C", 55), -1), leg(opt("C", 60), 1)],
    "iron_butterfly": [leg(opt("P", 45), 1), leg(opt("P", 50), -1), leg(opt("C", 50), -1), leg(opt("C", 55), 1)],
    "butterfly": [leg(opt("C", 45), 1), leg(opt("C", 50), -2), leg(opt("C", 55), 1)],
    "broken_wing_butterfly": [leg(opt("P", 40), 1), leg(opt("P", 50), -2), leg(opt("P", 55), 1)],
    "calendar": [leg(opt("C", 50, NOV), -1), leg(opt("C", 50, DEC), 1)],
    "diagonal": [leg(opt("C", 55, NOV), -1), leg(opt("C", 50, JAN), 1)],
    "future_long": [leg(FUT, 2)],
    "future_short": [leg(FUT, -1)],
    "crypto_long": [leg(BTC, "0.05")],
    "crypto_short": [leg(BTC, "-0.05")],
    "custom": [leg(opt("P", 50), -1), leg(opt("P", 45), 2)],   # ratio spread: never forced
}


def test_cases_cover_every_label():
    assert set(CASES) == {l.value for l in StrategyLabel} - {"cash_secured_put"}


@pytest.mark.parametrize("name", sorted(CASES))
def test_label(name):
    assert label(*CASES[name]) == name


def test_cash_secured_put_needs_the_cash():
    p = leg(opt("P", 50), -2)                    # obligation 2 x 50 x 100 = 10,000
    assert label(p, cash=D(10000)) == "cash_secured_put"
    assert label(p, cash=D("9999.99")) == "short_put"
    assert label(p) == "short_put"               # no cash data
    assert classify([p], D(10000))[1] == "inferred"


def test_index_and_futures_puts_are_never_cash_secured():
    spx = leg(opt("P", 6000, und="SPX", cls="index_option"), -1)
    mes = leg(opt("P", 6000, und="/MES", cls="future_option", mult=5), -1)
    assert label(spx, cash=D(10**9)) == "short_put"
    assert label(mes, cash=D(10**9)) == "short_put"


@pytest.mark.parametrize("legs", [
    [leg(stock(), 150), leg(opt("C", 55), -2)],               # not enough shares to cover
    [leg(stock(), 100), leg(opt("P", 45), -1)],               # shares plus a put
    [leg(stock(), 100), leg(opt("C", 55), 1)],                # bought call, not sold
    [leg(opt("P", 50), -1), leg(opt("P", 45), -1)],           # two short puts, two strikes
    [leg(opt("P", 45), -1), leg(opt("C", 55, DEC), -1)],      # strangle across expirations
    [leg(opt("C", 45), 1), leg(opt("C", 50), -2), leg(opt("C", 60), 2)],
    [leg(opt("P", 40), 1), leg(opt("P", 45), -1), leg(opt("C", 55), -1), leg(opt("C", 60), 2)],
    [leg(FUT, 1), leg(opt("C", 6000, und="/MES", cls="future_option", mult=5), -1)],
])
def test_anything_else_is_custom(legs):
    assert label(*legs) == "custom"


def test_empty_position_is_an_error():
    with pytest.raises(ValueError):
        classify([], None)


# ---------------------------------------------------------------------------
# Order effects
# ---------------------------------------------------------------------------

P50, P45, C55, C60 = (opt("P", 50).symbol, opt("P", 45).symbol, opt("C", 55).symbol, opt("C", 60).symbol)


@pytest.mark.parametrize("before,after,effect", [
    ({}, {P50: D(-1)}, "open"),
    ({P50: D(-1)}, {}, "close"),
    ({P50: D(-1)}, {P45: D(-1)}, "roll"),
    ({P50: D(-1)}, {P50: D(-1), C55: D(-1)}, "adjust"),               # adds a leg
    ({P50: D(-1), C55: D(-1)}, {P50: D(-1)}, "adjust"),               # removes a leg
    ({P50: D(-2)}, {P50: D(-3)}, "add"),
    ({P50: D(-3)}, {P50: D(-1)}, "partial_close"),
])
def test_order_effects(before, after, effect):
    assert order_effect(change(before, after)) == effect


# ---------------------------------------------------------------------------
# Roll types: one hand-built roll per type
# ---------------------------------------------------------------------------

def _roll(before_legs, after_legs, spot):
    insts = {l.inst.symbol: l.inst for l in before_legs + after_legs}
    before = {l.inst.symbol: l.qty for l in before_legs}
    after = {l.inst.symbol: l.qty for l in after_legs}
    return set(roll_types(before, after, insts, D(spot)))


def test_out_in_time():
    assert _roll([leg(opt("P", 45, NOV), -1)], [leg(opt("P", 45, DEC), -1)], 50) == {RollType.out_in_time}


def test_up_and_down():
    assert _roll([leg(opt("C", 55), -1)], [leg(opt("C", 60), -1)], 50) == {RollType.up}
    assert _roll([leg(opt("P", 45), -1)], [leg(opt("P", 40), -1)], 50) == {RollType.down}


def test_untested_toward_money():
    # stock fell to 46: the put (45) is tested, the call (55) is untested and moves down to 50
    t = _roll([leg(opt("P", 45), -1), leg(opt("C", 55), -1)],
              [leg(opt("P", 45), -1), leg(opt("C", 50), -1)], 46)
    assert t == {RollType.untested_toward_money, RollType.down, RollType.narrowed}


def test_tested_away_from_money():
    # stock rose to 54: the call (55) is tested and moves up and out to 60
    t = _roll([leg(opt("P", 45), -1), leg(opt("C", 55), -1)],
              [leg(opt("P", 45), -1), leg(opt("C", 60, DEC), -1)], 54)
    assert t == {RollType.tested_away_from_money, RollType.up, RollType.out_in_time, RollType.widened}


def test_widened_and_narrowed_vertical():
    assert RollType.widened in _roll([leg(opt("P", 50), -1), leg(opt("P", 45), 1)],
                                     [leg(opt("P", 50), -1), leg(opt("P", 40), 1)], 55)
    assert RollType.narrowed in _roll([leg(opt("P", 50), -1), leg(opt("P", 40), 1)],
                                      [leg(opt("P", 50), -1), leg(opt("P", 45), 1)], 55)


def test_long_legs_pair_only_with_long_legs():
    # closing a long put and opening a short put is not a roll of one leg into the other
    assert _roll([leg(opt("P", 45), 1)], [leg(opt("P", 40), -1)], 50) == set()


def test_width_definition():
    insts = {i.symbol: i for i in (opt("P", 45), opt("C", 55), opt("P", 40))}
    assert width({opt("P", 45).symbol: D(-1), opt("C", 55).symbol: D(-1)}, insts) == 10
    assert width({opt("P", 45).symbol: D(-1), opt("P", 40).symbol: D(1)}, insts) == 5
    assert width({opt("P", 45).symbol: D(-1)}, insts) is None
