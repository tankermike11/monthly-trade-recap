"""Phase 4 acceptance against the real API. Opt-in only: `pytest -m live` with
ANTHROPIC_API_KEY set. Costs a few cents per run at the configured model.

- all 10 personas pass the validator with at most one retry;
- persona 9's recap names the loss once and puts it next to the market move;
- re-running with the same facts sheet and prompt version passes the same checks.
"""

from __future__ import annotations

import os

import pytest

from pipeline.render import render_text
from pipeline.run import facts_for
from pipeline.validate_copy import MARKET_FACTS, RESULTS_FACTS, placeholders, texts
from pipeline.write import ModelConfig, write_recap
from tests.conftest import PERSONA_IDS

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="ANTHROPIC_API_KEY not set")]
MONTH = "2026-10"


@pytest.fixture(scope="module")
def results(fresh):
    cfg = ModelConfig.load(model=os.environ.get("RECAP_MODEL"))
    out = {}
    for p in PERSONA_IDS:
        linked, sheet = facts_for(fresh / "raw", MONTH, p)
        out[p] = (sheet, linked, write_recap(sheet, linked, cfg))
    return out


@pytest.mark.parametrize("p", PERSONA_IDS)
def test_passes_with_at_most_one_retry(results, p):
    sheet, linked, res = results[p]
    assert res.status == "passed", [a.validation.reasons() for a in res.attempts]
    assert len(res.attempts) <= 2
    render_text(res.recap, sheet, linked)


def test_persona_9_names_the_loss_once_next_to_the_market(results):
    sheet, linked, res = results["p09"]
    assert not [p for _, t in texts(res.recap) for p in placeholders(t) if p in RESULTS_FACTS]
    shown = {f.id: f.display for f in sheet.facts}
    text = render_text(res.recap, sheet, linked)
    assert text.count(shown["month_return"]) == 1
    line = next(l for l in text.splitlines() if shown["month_return"] in l)
    assert any(shown[m] in line for m in MARKET_FACTS if m in shown)


def test_rerun_passes_the_same_checks(results):
    sheet, linked, first = results["p09"]
    again = write_recap(sheet, linked, ModelConfig.load(model=os.environ.get("RECAP_MODEL")))
    assert again.status == first.status == "passed"
    assert again.prompt_sha256 == first.prompt_sha256
