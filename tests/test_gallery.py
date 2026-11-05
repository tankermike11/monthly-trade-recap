"""The review gallery (pipeline/gallery.py, SPEC 5.2), offline.

- all 10 personas get a card; p10 (data-quality issues) needs review, with the issues
  named; a customer with no recap needs review;
- claims to spot-check come from Claude's own words (headings included), not from the
  values code fills in, and carry the trades they name;
- everything Claude or a workbook wrote is escaped in the page.
"""

from __future__ import annotations

import json
import re

import pytest

from pipeline.gallery import build, card, load_results
from pipeline.write import ModelConfig
from schemas.recap import Attempt, CheckResult, RecapCopy, ValidationResult, WriteResult
from tests.conftest import PERSONA_IDS

MONTH = "2026-10"
CFG = ModelConfig.load()


def result(cid: str, copy: dict, status: str = "passed") -> WriteResult:
    v = ValidationResult(passed=status == "passed", checks=[CheckResult(name="schema_and_lengths", passed=status == "passed")])
    return WriteResult(customer_id=cid, month=MONTH, status=status, recap=RecapCopy.model_validate(copy) if status == "passed" else None,
                       attempts=[Attempt(raw_output=json.dumps(copy), validation=v, stop_reason="end_turn", usage={})],
                       model=CFG.model, effort=CFG.effort, max_tokens=CFG.max_tokens, prompt_version=CFG.prompt_version,
                       prompt_sha256="0" * 64)


GENERIC = {"overall": "You traded this month.", "subject_line": "Your October",
           "observations": [{"heading": "Every trade ended the same day", "text": "You traded."}]}


@pytest.fixture(scope="module")
def results():
    return {cid: result(cid, GENERIC) for cid in PERSONA_IDS}


def test_every_persona_has_a_card_and_p10_needs_review(fresh, results):
    html = build(MONTH, fresh / "raw", results)
    for cid in PERSONA_IDS:
        assert f'id="{cid}"' in html
    c10 = card(fresh / "raw", MONTH, "p10", results["p10"], CFG)
    assert c10["status"] == "needs review" and any(r.startswith("Data:") for r in c10["reasons"])
    assert card(fresh / "raw", MONTH, "p04", results["p04"], CFG)["status"] == "ready"


def test_needs_review_filter_can_hide_cards(fresh, results):
    """The filter hides cards with [hidden]; .card sets display, so it needs its own rule."""
    html = build(MONTH, fresh / "raw", results)
    assert 'data-filter="review"' in html and "c.hidden =" in html
    assert re.search(r"\.card\[hidden\]\s*\{\s*display:\s*none", html)


def test_missing_or_failed_recaps_need_review(fresh):
    assert "No recap" in card(fresh / "raw", MONTH, "p04", None, CFG)["reasons"][0]
    failed = card(fresh / "raw", MONTH, "p04", result("p04", GENERIC, status="review"), CFG)
    assert failed["status"] == "needs review" and "failed the checks twice" in failed["reasons"][0]


def test_claims_come_from_claudes_words_and_name_their_trades(fresh):
    copy = {"overall": "{{group.winning.bought_back_before_expiration_day}} winning trades were bought back early.",
            "subject_line": "Your October",
            "observations": [{"heading": "SBUX puts were bought back well before expiration", "text": "You sold puts."},
                             {"heading": "NKE came back", "text": "You returned to NKE."}]}
    c = card(fresh / "raw", MONTH, "p09", result("p09", copy), CFG)
    texts = [k["text"] for k in c["claims"]]
    # "both" in the filled count is code's word, not a claim to check
    assert not any(t.startswith("Both winning trades") for t in texts)
    claim = next(k for k in c["claims"] if k["text"].startswith("SBUX puts"))
    assert claim["words"] == ["well"] and {t["underlying"] for t in claim["trades"]} == {"SBUX"}
    assert all(t["left"] for t in claim["trades"])            # time left is shown, to check "well before"


def test_page_escapes_what_claude_wrote(fresh, results):
    copy = dict(GENERIC, observations=[{"heading": "<script>alert(1)</script>", "text": "You traded."}])
    html = build(MONTH, fresh / "raw", dict(results, p04=result("p04", copy)))
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_results_load_from_a_comparison_file_or_a_run_folder(tmp_path, results):
    f = tmp_path / "cmp.json"
    f.write_text(json.dumps([results["p04"].model_dump(mode="json")]), encoding="utf-8")
    assert set(load_results(tmp_path, MONTH, f)) == {"p04"}
    (tmp_path / MONTH / "p09").mkdir(parents=True)
    (tmp_path / MONTH / "p09" / "recap.json").write_text(results["p09"].model_dump_json(), encoding="utf-8")
    (tmp_path / MONTH / "review").mkdir()
    (tmp_path / MONTH / "review" / "p10.json").write_text(result("p10", GENERIC, "review").model_dump_json(), encoding="utf-8")
    assert set(load_results(tmp_path, MONTH)) == {"p09", "p10"}
