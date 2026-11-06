"""The trade-story page (pipeline/storyview.py) replays each month step by step. Its steps
must add up to the answer key's stories, which come from the generator's own ledger."""

from decimal import Decimal

import pytest

from pipeline.link import link
from pipeline.metrics import build_facts
from pipeline.storyview import render_story_view, replay_steps
from tests.conftest import MONTHS, PERSONA_IDS

Q4 = Decimal("0.0001")


@pytest.mark.parametrize("month", MONTHS)
@pytest.mark.parametrize("pid", PERSONA_IDS)
def test_steps_add_up_to_the_answer_key(raw, keys, month, pid):
    _, by_sid, _ = replay_steps(raw[(month, pid)])
    for story in keys[(month, pid)]["stories"]:
        steps = by_sid[story["story_id"]]
        total = sum((st.delta for st in steps), Decimal(0))
        assert total.quantize(Q4) == Decimal(story["realized_pnl"]).quantize(Q4), story["story_id"]
        assert steps[-1].realized.quantize(Q4) == total.quantize(Q4)
        held_end = {s: q for s, (q, _) in steps[-1].held.items()}
        want = {s: Decimal(q) for s, q in story["end_positions"].items()} if story["status"] == "open" else {}
        assert held_end == want, story["story_id"]
        orders = [st.ref for st in steps if st.kind == "order"]
        assert orders == [o["order_id"] for o in story["orders"]]


def test_page_shows_every_story_and_ties_to_the_facts_sheet(raw):
    r = raw[("2026-10", "p09")]
    linked = link(r)
    sheet = build_facts(r, linked, previous=None, first_month=True)
    page = render_story_view(r, linked, sheet)
    assert page.count('<details class="story"') == len(linked.stories)
    assert page.count("✓ The steps add to") == len(linked.stories)
    assert "✗" not in page
    assert page.count("✓ matches") >= 2          # locked-in total and rolls agree with the facts sheet
    assert "—" not in page
