"""Acceptance: every option fill is within the simulated bid-ask of the theoretical price;
every stock (and futures, crypto) fill is within that session's range."""

from __future__ import annotations

from decimal import Decimal

import polars as pl
import pricing_reference as pr
import pytest

from mock.market import calendar as cal
from mock.market import listings as L
from mock.market import quotes as Q
from tests.conftest import MONTHS, PERSONA_IDS

OPTION_CLASSES = {"equity_option", "index_option", "future_option"}


def _option_fills(raw):
    for (m, p), rm in raw.items():
        ctx = {c.exec_id: c for c in rm.market.fills}
        for f in rm.fills:
            if f.asset_class in OPTION_CLASSES:
                yield m, p, f, ctx[f.exec_id]


def test_every_option_fill_has_a_quote(raw, keys):
    n = 0
    for m, p, f, _ in _option_fills(raw):
        assert f.exec_id in keys[(m, p)]["option_quotes"], f"{f.exec_id} has no recorded quote"
        n += 1
    assert n > 300, "expected several hundred option fills across the mock data"


def test_option_fills_inside_bid_ask_around_theo(raw, keys, world):
    """Re-price every option fill from the raw market context, then check
    bid <= theo <= ask and bid <= fill price <= ask."""
    Q._theo_cache.clear()
    for m, p, f, c in _option_fills(raw):
        q = keys[(m, p)]["option_quotes"][f.exec_id]
        inst = L.instrument_from_fill(world, f)
        value, model = Q.theo(world, inst, f.ts_utc, c.underlying_price, c.iv)
        theo = Decimal(repr(value)).quantize(Decimal("0.0001"))
        assert theo == Decimal(q["theo"]), f"{f.exec_id}: re-priced {theo} != recorded {q['theo']}"
        assert model == q["model_id"]
        bid, ask = Decimal(q["bid"]), Decimal(q["ask"])
        assert bid <= theo <= ask, f"{f.exec_id}: theo {theo} outside [{bid}, {ask}]"
        assert bid <= f.price <= ask, f"{f.exec_id}: fill {f.price} outside [{bid}, {ask}]"
        assert Decimal(q["underlying_price"]) == c.underlying_price
        assert Decimal(q["iv"]) == c.iv


def test_models_are_routed_by_contract_terms(raw, keys):
    seen = {}
    for m, p, f, _ in _option_fills(raw):
        seen.setdefault(f.asset_class, set()).add(keys[(m, p)]["option_quotes"][f.exec_id]["model_id"])
    assert seen["index_option"] == {"BSM-continuous-q"}
    assert seen["future_option"] == {"CRR-1000-b0"}
    assert seen["equity_option"] <= {"CRR-1000-discrete-div", "CRR-1000-continuous-q"}
    assert "CRR-1000-discrete-div" in seen["equity_option"], "a dividend-paying name should route to discrete dividends"


@pytest.mark.parametrize("model", ["BSM-continuous-q", "CRR-1000-b0", "CRR-1000-continuous-q"])
def test_cross_check_against_pricer_directly(raw, keys, world, model):
    """Independent of mock.market.quotes: call pricing_reference by hand for a few fills."""
    checked = 0
    for m, p, f, c in _option_fills(raw):
        q = keys[(m, p)]["option_quotes"][f.exec_id]
        if q["model_id"] != model:
            continue
        settle = cal.at_ny_minutes(f.expiration, cal.regular_close_minute(f.expiration))
        T = pr.year_fraction(f.ts_utc, settle)
        S, K, sig = float(c.underlying_price), float(f.strike), float(c.iv)
        right = str(f.put_call)
        if model == "BSM-continuous-q":
            v = pr.bsm_price(S, K, T, 0.04, 0.012, sig, right)
        elif model == "CRR-1000-b0":
            v = pr.crr_american_futures(S, K, T, 0.04, sig, right, steps=1000)
        else:
            y = world.symbols[f.underlying].get("yield")
            v = pr.crr_american(S, K, T, 0.04, sig, right, steps=1000, q=y)
        assert abs(float(v) - float(q["theo"])) < 1e-4
        checked += 1
        if checked == 5:
            break
    assert checked > 0


def test_linear_fills_inside_session_range(raw, fresh, world):
    """Stocks, ETFs, futures and crypto: fill price within the session's low and high."""
    checked = 0
    for m in MONTHS:
        bars = pl.read_parquet(fresh / "market" / m / "bars.parquet")
        rng = {(r["symbol"], r["date"]): (r["low"], r["high"]) for r in bars.iter_rows(named=True)}
        for p in PERSONA_IDS:
            for f in raw[(m, p)].fills:
                if f.asset_class in OPTION_CLASSES:
                    continue
                if f.source == "event":
                    # shares delivered by an assignment are priced at the strike, not traded
                    ev = next(e for e in raw[(m, p)].events if f.exec_id in e.resulting_exec_ids)
                    assert ev.type in ("assignment", "exercise")
                    continue
                inst = L.instrument_from_fill(world, f)
                day, _ = world.session_day(inst.price_symbol, f.ts_utc)
                lo, hi = rng[(inst.price_symbol, day)]
                assert lo <= f.price <= hi, f"{p} {f.exec_id} {f.symbol} {f.price} outside [{lo}, {hi}] on {day}"
                checked += 1
    assert checked > 300


def test_option_fills_trade_in_regular_hours(raw):
    for m, p, f, _ in _option_fills(raw):
        if f.asset_class in ("equity_option", "index_option") and f.source == "trade":
            assert f.session == "regular", f"{f.exec_id} traded in {f.session}"


def test_short_dated_options_priced_for_session_movement(world):
    """Same-day options are quoted at a higher vol than the day's level, because the
    simulated price makes most of its daily move in regular hours; contracts more than
    a week out use the day's vol unchanged."""
    from datetime import date
    day = date(2026, 10, 14)
    ts = cal.at_ny(day, 10)
    same_day = L.option(world, "SPX", day, "P", Decimal("6500"))
    six_weeks = L.option(world, "SPX", date(2026, 11, 20), "P", Decimal("6500"))
    base = world.iv("SPX", day)
    assert 1.8 < Q.contract_vol(world, same_day, ts, base) / base < 2.4
    assert Q.contract_vol(world, six_weeks, ts, base) == base


def test_p06_closes_most_winners_at_the_mid(raw, keys, world):
    """p06 waits for the middle price on most winning closes: those fills sit on the
    tick nearest the mid on the customer's side, never further toward the far side."""
    at_mid = closes = 0
    for m in MONTHS:
        rm, q = raw[(m, "p06")], keys[(m, "p06")]["option_quotes"]
        for f in rm.fills:
            if f.source != "trade" or f.open_close != "close":
                continue
            bid, ask = Decimal(q[f.exec_id]["bid"]), Decimal(q[f.exec_id]["ask"])
            mid = (bid + ask) / 2
            grid = Q.tick_grid(bid, ask, Q.option_ticks(world, L.instrument_from_fill(world, f)))
            best = min(p for p in grid if p >= mid) if f.side == "buy" else max(p for p in grid if p <= mid)
            closes += 1
            at_mid += f.price == best
    assert closes > 20 and at_mid / closes > 0.6
