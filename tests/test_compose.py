"""What code computes for Claude to write from (pipeline/compose.py), offline.

- group counts come from the trades, so "every" and "both" can come from a fact, and a
  full or empty count reads "all 10" or "none of the 4";
- each trade says how it ended and, when bought back, how much time was left.
"""

from __future__ import annotations

import pytest

from pipeline.compose import compose
from pipeline.run import facts_for


@pytest.fixture(scope="module")
def composed(fresh):
    out = {}
    for p in ("p04", "p06", "p09"):
        linked, sheet = facts_for(fresh / "raw", "2026-10", p)
        out[p] = (compose(sheet, linked), linked)
    return out


def shown(c) -> dict:
    return {f.id: f.display for f in c.sheet.facts}


def test_group_counts_come_from_the_trades(composed):
    c, linked = composed["p06"]
    losers = [s for s in linked.stories if s.status != "open" and s.realized_pnl < 0]
    early = [s for s in losers if s.status == "closed" and s.days_left_at_close]
    assert shown(c)["group.losing.bought_back_before_expiration_day"] == f"{len(early)} of {len(losers)}" == "3 of 5"
    assert shown(c)["group.losing.bought_back_on_expiration_day"] == "2 of 5"


def test_full_and_empty_counts_read_naturally(composed):
    assert shown(composed["p04"][0])["group.winning.bought_back_before_expiration_day"] == "all 10"
    assert shown(composed["p06"][0])["group.trades.rolled"] == "none of the 23"
    assert shown(composed["p09"][0])["group.winning.bought_back_before_expiration_day"] == "both"


def test_each_trade_says_how_it_ended(composed):
    c, _ = composed["p06"]
    endings = {t["how_it_ended"] for t in c.trades}
    assert endings <= {"bought back before its expiration day", "bought back on its expiration day", "expired",
                       "assigned", "closed", "still open at month end"}
    assert {"bought back on its expiration day", "expired"} <= endings


def test_time_left_is_given_for_trades_bought_back_early(composed):
    c, linked = composed["p09"]
    f = shown(c)
    sbux = next(t for t in c.trades if t["underlying"] == "SBUX")
    assert sbux["how_it_ended"] == "bought back before its expiration day"
    assert f[f"trade.{sbux['ref']}.left_at_close"] == "3 days"


def test_every_trade_fact_traces_to_its_story(composed):
    for c, linked in composed.values():
        ids = {s.story_id for s in linked.stories}
        for f in c.sheet.facts:
            if f.id.startswith(("trade.", "group.")):
                assert f.sources and all(s.removeprefix("story:") in ids for s in f.sources), f.id
