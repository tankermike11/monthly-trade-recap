"""The narrative prototype (pipeline/narrative.py), offline.

- group counts come from the trades, so "every" and "both" can come from a fact;
- per-trade facts say how each trade ended and how much time was left;
- the checks reject typed numbers, the month's result, banned words, doubled units and a
  word repeated across a filled placeholder ("about about 7 weeks"), and pass clean copy.
"""

from __future__ import annotations

import pytest

from pipeline import narrative as N
from pipeline.run import facts_for


@pytest.fixture(scope="module")
def alex(fresh):
    linked, sheet = facts_for(fresh / "raw", "2026-10", "p06")
    extra, briefs = N.trade_facts(sheet, linked)
    return N.with_trades(sheet, extra + N.group_facts(linked)), briefs, linked


def test_group_counts_come_from_the_trades(alex):
    sheet, _, linked = alex
    shown = {f.id: f.display for f in sheet.facts}
    losers = [s for s in linked.stories if s.status != "open" and s.realized_pnl < 0]
    early = [s for s in losers if s.status == "closed" and s.days_left_at_close]
    assert shown["group.losing.bought_back_before_expiration_day"] == f"{len(early)} of {len(losers)}"
    assert shown["group.losing.bought_back_before_expiration_day"] == "3 of 5"
    assert shown["group.losing.bought_back_on_expiration_day"] == "2 of 5"
    assert shown["group.trades.rolled"] == "0 of 23"


def test_each_trade_says_how_it_ended(alex):
    _, briefs, _ = alex
    endings = {b["how_it_ended"] for b in briefs}
    assert endings <= {"bought back before its expiration day", "bought back on its expiration day", "expired",
                       "assigned", "closed", "still open at month end"}
    assert {"bought back on its expiration day", "expired"} <= endings


def _copy(text):
    return {"overall": "You traded SPX credit spreads on {{active_days}}.", "subject_line": "Your October",
            "observations": [{"heading": "Spreads were short", "text": text},
                             {"heading": "Losses came late", "text": "Your last spreads ended with a loss."}]}


def test_clean_narrative_passes(alex):
    sheet = alex[0]
    assert N.check(_copy("{{group.losing.bought_back_before_expiration_day}} of your losing spreads were bought back early."), sheet) == []


def test_word_repeated_across_a_placeholder_is_rejected(alex):
    sheet = alex[0]
    f = next(f for f in sheet.facts if f.display.split()[0].isalpha() and not f.id.startswith("group."))
    word = f.display.split()[0]
    problems = N.check(_copy(f"You traded with {word} {{{{{f.id}}}}} in mind."), sheet)
    assert any(f"repeats the word {word!r}" in p for p in problems), (f.display, problems)


@pytest.mark.parametrize("text, needle", [
    ("You opened 23 spreads.", "types a digit"),
    ("Your account ended {{month_return}}.", "names the month's result"),
    ("Each spread was a bet on the close.", "'bet'"),
    ("You traded {{active_days}} days.", "repeats 'days'"),
    ("You used {{not_a_fact}}.", "not a fact"),
])
def test_checks_reject(alex, text, needle):
    problems = N.check(_copy(text), alex[0])
    assert any(needle in p for p in problems), problems
