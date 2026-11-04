"""The claim check (pipeline/claimcheck.py), offline.

- every sentence Claude wrote is checked (headings whole), with the numbers filled in,
  beside every trade; no audit sources go out;
- an unsupported sentence fails `claims_supported`; a sentence the checker skipped
  counts as unsupported;
- the writer rewrites once with the checker's reason, then passes or goes to review;
- the checker's cost is counted; tests with a fake writer never reach the real API.
"""

from __future__ import annotations

import json

import pytest

from pipeline import claimcheck
from pipeline.compose import compose
from pipeline.run import facts_for
from pipeline.write import ModelConfig, ModelReply, cost, write_recap
from schemas.recap import RecapCopy
from tests.recap_fixtures import good

CFG = ModelConfig.load()


@pytest.fixture(scope="module")
def p09(fresh):
    linked, sheet = facts_for(fresh / "raw", "2026-10", "p09")
    return sheet, linked, compose(sheet, linked)


def test_every_sentence_is_listed_with_headings_whole(p09):
    copy = RecapCopy.model_validate(good())
    s = claimcheck.sentences(copy)
    assert [x["where"] for x in s] == ["overall picture", "what stood out 1, heading", "what stood out 1",
                                       "what stood out 2, heading", "what stood out 2"]
    assert s[1]["text"] == "Your SBUX puts were bought back early"


def test_the_checker_sees_filled_text_and_every_trade_but_no_sources(p09):
    sheet, linked, c = p09
    u = json.loads(claimcheck.user_turn(RecapCopy.model_validate(good()), c))
    assert "{{" not in json.dumps(u["sentences_to_check"])
    assert len(u["data"]["trades_in_opening_order"]) == len(c.trades)
    assert "story:" not in json.dumps(u) and "first_month" not in u["data"]["flags"]


def verdicts(copy, **overrides):
    out = {s["id"]: {"verdict": "supported", "reason": ""} for s in claimcheck.sentences(copy)}
    out.update(overrides)
    return out


def test_unsupported_and_skipped_sentences_fail(p09):
    _, _, c = p09
    copy = RecapCopy.model_validate(good())
    assert claimcheck.result(copy, c, verdicts(copy)).passed
    bad = claimcheck.result(copy, c, verdicts(copy, s3={"verdict": "unsupported", "reason": "contradicted: Oct 2"}))
    assert not bad.passed and "is not supported by the trades: contradicted: Oct 2" in bad.reasons[0]
    skipped = verdicts(copy)
    skipped.pop("s5")
    assert "was not checked" in claimcheck.result(copy, c, skipped).reasons[0]


def fake_writer(*outputs):
    calls = []

    def call(cfg, system, user):
        calls.append(user)
        return ModelReply(json.dumps(outputs[min(len(calls), len(outputs)) - 1]), "end_turn",
                          {"input_tokens": 1000, "output_tokens": 300, "model": CFG.model})
    call.calls = calls
    return call


def fake_checker(*answers):
    seen = []

    def call(system, user):
        seen.append(user)
        ids = [s["id"] for s in json.loads(user)["sentences_to_check"]]
        bad = answers[min(len(seen), len(answers)) - 1]
        v = {i: {"verdict": "unsupported" if i in bad else "supported", "reason": bad.get(i, "")} for i in ids}
        return claimcheck.CheckReply(v, {"input_tokens": 4000, "output_tokens": 400, "model": "claude-opus-5-5"}, "")
    call.seen = seen
    return call


def test_writer_rewrites_once_with_the_checkers_reason(p09):
    sheet, linked, _ = p09
    writer = fake_writer(good(), good())
    checker = fake_checker({"s3": "group: the Oct 20 put had about 3 weeks left"}, {})
    res = write_recap(sheet, linked, CFG, writer, checker)
    assert res.status == "passed" and len(res.attempts) == 2
    assert "claims_supported" in writer.calls[1] and "about 3 weeks left" in writer.calls[1]
    assert [c.name for c in res.attempts[1].validation.checks][-1] == "claims_supported"
    assert res.attempts[0].usage["claim_check"]["input_tokens"] == 4000


def test_two_unsupported_drafts_go_to_review(p09):
    sheet, linked, _ = p09
    res = write_recap(sheet, linked, CFG, fake_writer(good()), fake_checker({"s1": "not in the data"}))
    assert res.status == "review" and res.recap is None


def test_cost_counts_the_checker(p09):
    sheet, linked, _ = p09
    with_check = write_recap(sheet, linked, CFG, fake_writer(good()), fake_checker({}))
    without = write_recap(sheet, linked, CFG, fake_writer(good()))
    assert cost(with_check, CFG) > cost(without, CFG) > 0


def test_a_fake_writer_without_a_checker_never_calls_the_api(p09, monkeypatch):
    sheet, linked, _ = p09
    monkeypatch.setattr(claimcheck, "anthropic_check", lambda *a, **k: pytest.fail("real API reached"))
    res = write_recap(sheet, linked, CFG, fake_writer(good()))
    assert res.status == "passed" and all(c.name != "claims_supported" for c in res.attempts[0].validation.checks)
