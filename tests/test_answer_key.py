"""The answer keys are internally consistent, so Phase 2 can be tested against them."""

from __future__ import annotations

from collections import Counter, defaultdict
from decimal import Decimal

from schemas.trades import StrategyLabel
from tests.conftest import MONTHS, PERSONA_IDS, facts


def test_every_fill_in_exactly_one_story(raw, keys):
    for k, rm in raw.items():
        owners = Counter(e for s in keys[k]["stories"] for e in s["exec_ids"])
        fills = {f.exec_id for f in rm.fills}
        assert set(owners) == fills, f"{k}: stories and fills disagree"
        assert all(n == 1 for n in owners.values()), f"{k}: a fill sits in two stories"


def test_every_order_and_event_in_exactly_one_story(raw, keys):
    for k, rm in raw.items():
        orders = Counter(o["order_id"] for s in keys[k]["stories"] for o in s["orders"])
        assert set(orders) == {o.order_id for o in rm.orders} and max(orders.values(), default=1) == 1
        events = Counter(e for s in keys[k]["stories"] for e in s["event_ids"])
        assert set(events) == {e.event_id for e in rm.events} and max(events.values(), default=1) == 1


def test_story_positions_reconcile_to_snapshot(raw, keys):
    for k, rm in raw.items():
        held = defaultdict(Decimal)
        for s in keys[k]["stories"]:
            for sym, q in s["end_positions"].items():
                held[sym] += Decimal(q)
        snap = {l.symbol: l.qty for l in rm.positions_end.lines}
        assert dict(held) == snap, f"{k}: story positions != month-end snapshot"


def test_pnl_reconciles_to_account_change(keys):
    for k, key in keys.items():
        diff = abs(Decimal(key["reconciliation"]["difference"]))
        assert diff < 1, f"{k}: realized + open P&L misses the account change by {diff}"


def test_fees_missing_is_the_only_reconciliation_gap(keys):
    gaps = {k: Decimal(v["reconciliation"]["difference"]) for k, v in keys.items()}
    # the broker charged a fee the data does not show, so the data comes up short by it
    assert Decimal("-1") < gaps[("2026-10", "p10")] < 0
    for k, g in gaps.items():
        if k != ("2026-10", "p10"):
            assert abs(g) < Decimal("0.01"), f"{k}: unexpected gap {g}"


def test_required_flags(keys):
    oct_ = {p: keys[("2026-10", p)]["flags"] for p in PERSONA_IDS}
    assert "red_month" in oct_["p09"]
    assert "low_activity" in oct_["p01"]
    assert "data_quality_issue" in oct_["p10"]
    assert "green_month" in oct_["p04"], "keep at least one green October for the Phase 4 examples"
    assert all("first_month" in f for f in oct_.values())
    assert not any("first_month" in keys[("2026-11", p)]["flags"] for p in PERSONA_IDS)


def test_persona_9_names_the_loss(keys):
    f = facts(keys[("2026-10", "p09")])
    loss = f["biggest_loss"]
    assert loss and Decimal(loss["pnl"]) < -1000
    story = next(s for s in keys[("2026-10", "p09")]["stories"] if s["story_id"] == loss["story_id"])
    assert story["underlying"] == "NKE"
    assert any(o["effect"] == "add" for o in story["orders"]), "sold more of the same put as it fell"


def test_persona_10_quality_issues(keys):
    issues = {d["issue"] for d in keys[("2026-10", "p10")]["data_quality_issues"]}
    assert issues == {"fees_missing", "order_partially_filled", "split_adjusted_positions"}
    for k, key in keys.items():
        if k[1] != "p10":
            assert not key["data_quality_issues"], f"{k} should be clean"


def test_two_order_roll_and_early_assignment(raw, keys):
    rolls = [r for s in keys[("2026-10", "p10")]["stories"] for r in s["rolls"]]
    assert any(len(r["order_ids"]) == 2 for r in rolls)
    early = [e for e in raw[("2026-10", "p10")].events if e.early]
    assert len(early) == 1 and early[0].type == "assignment"


def test_wheel_is_one_story(keys):
    """p03: put assigned into shares, then calls on those shares, all in one story."""
    stories = keys[("2026-10", "p03")]["stories"]
    wheel = [s for s in stories if s["underlying"] == "F"]
    assert len(wheel) == 1 and wheel[0]["label_end"] == "covered_call" and wheel[0]["event_ids"]


def test_premium_seller_rolls(keys):
    for m in MONTHS:
        f = facts(keys[(m, "p04")])
        assert f["rolls_total"] >= 10
        assert f["rolls_by_type"]["untested_toward_money"] > 0
        assert f["rolls_by_type"]["out_in_time"] > 0
        assert f["stories_open_at_end"] > 0


def test_label_coverage(keys):
    used = {s[f] for key in keys.values() for s in key["stories"] for f in ("label_open", "label_end")}
    must = {"stock_long", "stock_short", "long_call", "short_put", "covered_call", "cash_secured_put",
            "credit_vertical", "debit_vertical", "short_strangle", "iron_condor", "diagonal",
            "future_long", "future_short", "crypto_long", "custom"}
    assert must <= used, f"missing labels: {sorted(must - used)}"
    assert used <= {l.value for l in StrategyLabel}


def test_every_fact_has_a_source(keys):
    for k, key in keys.items():
        for f in key["headline_facts"]:
            v = f["value"]
            empty = v in (None, 0, {}, [], "0.00") or (isinstance(v, dict) and not any(v.values()))
            if not empty:
                assert f["sources"], f"{k}: {f['id']} has no source"
