"""Phase 2 acceptance: the trade linker against the answer keys.

- every trade story's fills, labels, rolls and outcome match the answer key exactly
  (every TradeStory field, for all 10 personas, October and November);
- net quantity across all stories reconciles to the month-end snapshot;
- realized plus open P&L reconciles to the account change net of deposits and
  withdrawals within $1;
- persona 10's problems become named data-quality flags, never a crash.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

import pytest

from pipeline.ingest import load_month
from pipeline.link import link
from schemas.answer_key import AnswerKey
from schemas.facts_catalog import DATA_QUALITY_ISSUES
from tests.conftest import MONTHS, PERSONA_IDS

CASES = [(m, p) for m in MONTHS for p in PERSONA_IDS]


@pytest.fixture(scope="session")
def linked(fresh):
    """{(month, pid): (RawMonth, LinkResult, AnswerKey)} built only from raw files."""
    out = {}
    for m, p in CASES:
        raw = load_month(fresh / "raw" / m / p)
        key = AnswerKey.model_validate_json((fresh / "expected" / m / f"{p}.json").read_text(encoding="utf-8"))
        out[(m, p)] = (raw, link(raw), key)
    return out


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}-{c[1]}")
def test_stories_match_answer_key_exactly(linked, case):
    _, res, key = linked[case]
    got = {s.story_id: s.model_dump() for s in res.stories}
    exp = {s.story_id: s.model_dump() for s in key.stories}
    assert list(got) == list(exp), "story IDs or their order differ"
    for sid, e in exp.items():
        diffs = {f: (e[f], got[sid][f]) for f in e if got[sid][f] != e[f]}
        assert not diffs, f"{sid}: {diffs}"


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}-{c[1]}")
def test_data_quality_issues_match_answer_key(linked, case):
    _, res, key = linked[case]
    assert [(d.issue, list(d.refs)) for d in res.data_quality_issues] == \
           [(d.issue, d.refs) for d in key.data_quality_issues]
    assert all(d.issue in DATA_QUALITY_ISSUES for d in res.data_quality_issues)


def test_net_quantity_reconciles_to_snapshot(linked):
    for case, (raw, res, _) in linked.items():
        held = defaultdict(Decimal)
        for s in res.stories:
            for sym, q in s.end_positions.items():
                held[sym] += q
        assert dict(held) == {l.symbol: l.qty for l in raw.positions_end.lines}, case
        assert not [d for d in res.data_quality_issues if d.issue == "positions_do_not_reconcile"], case


def test_pnl_reconciles_to_account_change(linked):
    for case, (raw, res, _) in linked.items():
        a = raw.account
        flows = sum((c.amount if c.kind == "deposit" else -c.amount for c in a.cash_flows), Decimal(0))
        realized = sum((s.realized_pnl for s in res.stories), Decimal(0))
        open_change = sum((s.open_pnl_end - s.open_pnl_start for s in res.stories), Decimal(0))
        gap = (a.end_value - a.start_value - flows) - (realized + open_change)
        assert abs(gap) < 1, f"{case}: off by {gap}"


def test_every_fill_order_and_event_lands_in_one_story(linked):
    for case, (raw, res, _) in linked.items():
        fills = [e for s in res.stories for e in s.exec_ids]
        orders = [o.order_id for s in res.stories for o in s.orders]
        events = [e for s in res.stories for e in s.event_ids]
        assert sorted(fills) == sorted(f.exec_id for f in raw.fills), case
        assert sorted(orders) == sorted(o.order_id for o in raw.orders), case
        assert sorted(events) == sorted(e.event_id for e in raw.events), case


def test_persona_10_flags(linked):
    _, res, _ = linked[("2026-10", "p10")]
    assert {d.issue for d in res.data_quality_issues} == {
        "fees_missing", "order_partially_filled", "split_adjusted_positions"}


def test_wheel_stays_one_story(linked):
    _, res, _ = linked[("2026-10", "p03")]
    f = [s for s in res.stories if s.underlying == "F"]
    assert len(f) == 1 and f[0].event_ids and f[0].label_end == "covered_call"


def test_trade_across_months(linked):
    """p05's IWM spread opened Oct 30 is open at October's end and closes Nov 3."""
    _, oct_, _ = linked[("2026-10", "p05")]
    _, nov, _ = linked[("2026-11", "p05")]
    assert any(s.underlying == "IWM" and s.status == "open" for s in oct_.stories)
    iwm = [s for s in nov.stories if s.underlying == "IWM"]
    assert iwm[0].carried_in and iwm[0].status == "closed"


# ---------------------------------------------------------------------------
# Damaged data never crashes the linker
# ---------------------------------------------------------------------------

def test_missing_carried_in_line_is_flagged_not_fatal(linked):
    raw, _, _ = linked[("2026-10", "p10")]
    start = raw.positions_start
    damaged = raw.model_copy(update={"positions_start": start.model_copy(update={"lines": start.lines[1:]})})
    res = link(damaged)
    issues = {d.issue for d in res.data_quality_issues}
    assert "positions_do_not_reconcile" in issues or "event_without_position" in issues


def test_closing_more_than_held_is_flagged_not_fatal(linked):
    raw, _, _ = linked[("2026-10", "p09")]
    # double the size of the first closing fill
    f = next(x for x in raw.fills if x.open_close == "close" and x.source == "trade")
    fills = [x.model_copy(update={"qty": x.qty * 3}) if x.exec_id == f.exec_id else x for x in raw.fills]
    res = link(raw.model_copy(update={"fills": fills}))
    assert "fill_exceeds_position" in {d.issue for d in res.data_quality_issues}


def test_event_on_missing_position_is_flagged_not_fatal(linked):
    raw, _, _ = linked[("2026-10", "p09")]
    ev = next(e for e in raw.events if e.type == "assignment")
    bogus = ev.model_copy(update={"event_id": "bogus", "symbol": "NKE   261016P00099000", "resulting_exec_ids": []})
    res = link(raw.model_copy(update={"events": raw.events + [bogus]}))
    assert "event_without_position" in {d.issue for d in res.data_quality_issues}
