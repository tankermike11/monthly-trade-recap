"""Phase 3 acceptance: metrics and the facts sheet.

- each persona's headline facts match its answer key (values, and sources where the
  key lists them);
- every fact has at least one source, and every source resolves to real fills;
- p09 is red_month, p01 low_activity, p10 data_quality_issue (all flags match the keys);
- a facts sheet builds in under 1 second per customer.
Also: only catalog fact types exist, none of them rewards activity, and every plain
phrase and display string passes the copy rules.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date
from decimal import Decimal

import pytest

from pipeline import display as D
from pipeline import tradingcal
from pipeline.run import facts_for, run
from schemas.facts import FactsSheet
from schemas.facts_catalog import FACTS, FORBIDDEN_FACT_WORDS, HEADLINE_FACTS
from tests.conftest import MONTHS, PERSONA_IDS

CASES = [(m, p) for m in MONTHS for p in PERSONA_IDS]
from pipeline.validate_copy import copy_rules  # noqa: E402  (shared word lists)

_RULES = copy_rules()
BANNED, JARGON, PRAISE, ADVICE = _RULES["banned"], _RULES["jargon"], _RULES["praise"], _RULES["advice"]
RETIRED = _RULES["retired"]


@pytest.fixture(scope="session")
def sheets(fresh):
    return {(m, p): facts_for(fresh / "raw", m, p) for m, p in CASES}


def _norm(v):
    if isinstance(v, dict):
        return {k: _norm(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_norm(x) for x in v]
    if isinstance(v, (str, float)) and not isinstance(v, bool):
        try:
            return Decimal(str(v))
        except Exception:
            return v
    return v


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}-{c[1]}")
def test_headline_facts_match_answer_key(sheets, keys, case):
    _, sheet = sheets[case]
    got = {f["id"]: f for f in json.loads(sheet.model_dump_json())["facts"]}
    for e in keys[case]["headline_facts"]:
        assert e["id"] in HEADLINE_FACTS
        if e["value"] in (None, {}, []):
            assert e["id"] not in got, f"{e['id']} has no value and must be left out"
            continue
        assert e["id"] in got, f"{e['id']} missing"
        assert _norm(got[e["id"]]["value"]) == _norm(e["value"]), e["id"]
        if e["sources"]:
            assert got[e["id"]]["sources"] == e["sources"], e["id"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: f"{c[0]}-{c[1]}")
def test_flags_match_answer_key(sheets, keys, case):
    assert sheets[case][1].flags == keys[case]["flags"]


def test_required_flags(sheets):
    assert "red_month" in sheets[("2026-10", "p09")][1].flags
    assert "low_activity" in sheets[("2026-10", "p01")][1].flags
    assert "data_quality_issue" in sheets[("2026-10", "p10")][1].flags


def test_every_source_resolves_to_real_fills(sheets, raw):
    for case, (linked, sheet) in sheets.items():
        rm = raw[case]
        fills = {f.exec_id for f in rm.fills}
        carried = {l.underlying for l in rm.positions_start.lines}
        stories = {s.story_id: s for s in linked.stories}
        indexes = {i.symbol for i in rm.market.indexes}
        for f in sheet.facts:
            assert f.sources, f"{case} {f.id} has no source"
            for src in f.sources:
                kind, _, ref = src.partition(":")
                if kind == "story":
                    s = stories[ref]
                    assert set(s.exec_ids) <= fills
                    # a story resolves to its fills this month, or (carried in, untouched)
                    # to the month-start positions those earlier fills built
                    assert s.exec_ids or (s.carried_in and s.underlying in carried), f"{case} {src}"
                elif kind == "fill":
                    assert ref in fills
                elif kind == "market":
                    assert ref in indexes
                else:
                    raise AssertionError(f"{case} {f.id}: unknown source {src}")


def test_only_catalog_facts_and_none_reward_activity(sheets):
    for word in FORBIDDEN_FACT_WORDS:
        assert not [fid for fid in FACTS if word in fid], word
    for _, sheet in sheets.values():
        for f in sheet.facts:
            assert f.id in FACTS
            assert f.section == FACTS[f.id][0]
            assert f.plain == FACTS[f.id][1]


def _copy_ok(text: str, where: str) -> None:
    low = text.lower()
    assert "—" not in text and "!" not in text, where
    for w in BANNED + JARGON + PRAISE + ADVICE + RETIRED:
        assert not re.search(rf"\b{re.escape(w)}\b", low), f"{where}: '{w}' in {text!r}"


def test_plain_phrases_pass_copy_rules():
    for fid, (_, plain) in FACTS.items():
        _copy_ok(plain, fid)


def test_plain_phrases_have_no_digits():
    """The model reads these phrases and may reuse them, but may not type a digit
    (validate_copy's no_raw_numbers), so a digit here invites a rejected draft."""
    bad = {fid: plain for fid, (_, plain) in FACTS.items() if any(c.isdigit() for c in plain)}
    assert not bad, bad


def test_half_month_displays_name_the_month(sheets):
    """Copy rule: "the first half of the month", never a bare "first half"."""
    seen = 0
    for case, (_, sheet) in sheets.items():
        for f in sheet.facts:
            if "first half" in f.display:
                seen += 1
                assert "first half of the month" in f.display, f"{case} {f.id}: {f.display!r}"
    assert seen, "no half-of-month facts in any sheet"


def test_display_strings_pass_copy_rules(sheets):
    for case, (_, sheet) in sheets.items():
        for f in sheet.facts:
            assert f.display, f"{case} {f.id} has an empty display"
            _copy_ok(f.display, f"{case} {f.id}")


def test_sheets_are_30_to_60_facts_for_option_traders(sheets):
    """Option facts (entry timing, moves, cash kept) make up much of the catalog, so the
    30 to 60 range applies to option traders with a full month; stock-only, futures-only
    and short months get fewer, never more than 60."""
    for case, (_, sheet) in sheets.items():
        n = len(sheet.facts)
        assert n <= 60, case
        if not {"low_activity", "no_options"} & set(sheet.flags):
            assert n >= 30, f"{case}: only {n} facts"


def test_what_changed_uses_last_month_only_when_it_exists(sheets):
    comparable = {"median_entry_dte", "adjust_to_close_ratio", "credit_roll_share",
                  "outside_hours_share", "median_days_held"}
    for (m, p), (_, sheet) in sheets.items():
        has = {f.id.split(".", 1)[1] for f in sheet.facts if f.id.startswith("vs_last_month.")}
        if m == "2026-10":
            assert not has and "first_month" in sheet.flags
        else:
            before = {f.id for f in sheets[("2026-10", p)][1].facts}
            now = {f.id for f in sheet.facts}
            assert has == comparable & before & now, p


def test_red_month_weights_the_loss_and_the_market(sheets):
    _, sheet = sheets[("2026-10", "p09")]
    w = {f.id: f.weight for f in sheet.facts}
    assert w["biggest_loss"] >= 0.8 and w["spy_move"] >= 0.7 and w["biggest_win"] < w["biggest_loss"]


def test_build_is_deterministic(fresh):
    a = facts_for(fresh / "raw", "2026-11", "p04")[1].model_dump_json()
    b = facts_for(fresh / "raw", "2026-11", "p04")[1].model_dump_json()
    assert a == b


def test_build_under_one_second_per_customer(fresh):
    worst = 0.0
    for m, p in CASES:
        t = time.perf_counter()
        facts_for(fresh / "raw", m, p)         # ingest + link + facts (+ last month's sheet)
        worst = max(worst, time.perf_counter() - t)
    assert worst < 1.0, f"slowest customer took {worst:.2f}s"


def test_cli_writes_stage_outputs(fresh, tmp_path):
    folders = run("2026-10", "p09", fresh / "raw", tmp_path)
    sheet = FactsSheet.model_validate_json((folders[0] / "facts.json").read_text(encoding="utf-8"))
    assert sheet.customer_id == "p09" and (folders[0] / "stories.json").exists()


# ---------------------------------------------------------------------------
# Display formatting and trading-day halves
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("fn,x,out", [
    (D.money, Decimal("1240.4"), "$1,240"), (D.money, Decimal("-596.2"), "-$596"), (D.money, Decimal("12.5"), "$12.50"),
    (D.ratio, 3.23, "3 to 1"), (D.ratio, 0.5, "1 to 2"), (D.ratio, 1.0, "1 to 1"),
    (D.share, 0.25, "1 in 4"), (D.share, 0.6667, "2 in 3"), (D.share, 0, "none"), (D.share, 1, "all"),
    (D.portion, 0.34, "about a third"), (D.portion, 0.5, "about half"), (D.portion, 0.95, "nearly all"),
    (D.multiple, 0.97, "about the same"), (D.multiple, 2.34, "2.3 times"), (D.multiple, 0.5, "about half"),
    (D.move, 0.0074, "up 0.7%"), (D.move, -0.0675, "down 6.8%"), (D.move, 0.0001, "flat"),
    (D.days, 0, "the same day"), (D.days, 1, "1 day"), (D.days, 12, "12 days"), (D.days, 44, "about 6 weeks"),
    (D.days, 120, "about 4 months"), (D.price_level, 12.0, "low"), (D.price_level, 45.0, "average"),
    (D.price_level, 80.0, "high"), (D.names, ["AMD", "INTC", "MSFT"], "AMD, INTC and MSFT"),
])
def test_display(fn, x, out):
    assert fn(x) == out


def test_trading_days_match_the_exchange_calendar():
    from mock.market import calendar as cal   # tests may compare against the mock's calendar
    for month in ("2025-11", "2026-01", "2026-04", "2026-07", "2026-10", "2026-11", "2026-12"):
        assert tradingcal.month_trading_days(month) == cal.month_trading_days(month), month


def test_halves_are_counted_in_trading_days():
    # October 2026 has 22 trading days: the first half ends on the 11th trading day, Oct 15
    assert tradingcal.first_half_end("2026-10") == date(2026, 10, 15)
    # November 2026 has 20 (Thanksgiving closed): first half ends Nov 13
    assert tradingcal.first_half_end("2026-11") == date(2026, 11, 13)
    assert tradingcal.half_of(date(2026, 11, 14), "2026-11") == "second"


def test_fees_line_never_reads_zero_when_fees_are_unknown(raw):
    """Missing fees count as 0 in the total, so the display says 'at least'; when no fill
    says what it cost there is no fees fact at all, rather than a false '$0.00'."""
    from pipeline.link import link
    from pipeline.metrics import build_facts

    def fees_fact(rm):
        sheet = build_facts(rm, link(rm), previous=None, first_month=True)
        return {f.id: f for f in sheet.facts}.get("total_fees")

    p10 = raw[("2026-10", "p10")]                       # one fill arrives with its fees missing
    assert fees_fact(p10).display.startswith("at least $")
    p04 = raw[("2026-10", "p04")]
    assert not fees_fact(p04).display.startswith("at least")
    unknown = p04.model_copy(update={"fills": [f.model_copy(update={"fees": None}) for f in p04.fills]})
    assert fees_fact(unknown) is None
