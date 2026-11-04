"""Phase 4 offline: the prompt, the validator, the writer loop and rendering (no API calls).

Code writes the fact lines, tiles, results line and what changed (pipeline/compose.py);
Claude writes the overall picture and observations; the validator checks Claude's words.
"""

from __future__ import annotations

import json

import pytest

from pipeline import validate_copy
from pipeline.compose import compose
from pipeline.render import UnknownPlaceholder, render_text
from pipeline.run import facts_for, run
from pipeline.validate_copy import copy_rules, validate
from pipeline.write import ModelConfig, ModelReply, anthropic_caller, user_turn, write_recap
from schemas.recap import RecapCopy
from tests.conftest import REPO
from tests.recap_fixtures import good

CFG = ModelConfig.load()
PROMPT = (REPO / "prompts" / f"{CFG.prompt_version}.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def cases(fresh):
    return {p: facts_for(fresh / "raw", "2026-10", p) for p in ("p01", "p04", "p09")}


@pytest.fixture(scope="module")
def p09(cases):
    linked, sheet = cases["p09"]
    return sheet, linked, compose(sheet, linked).sheet


# ---------------------------------------------------------------------------
# The prompt states the rules
# ---------------------------------------------------------------------------

def test_prompt_states_the_copy_rules():
    low = PROMPT.lower()
    for w in copy_rules()["banned"]:
        assert w in low, w
    for phrase in ("second person", "past tense", "em dash", "exclamation", "you should", "never type a number",
                   "low_activity", "red_month", "first half of the month", "expiration", "strike", "premium",
                   "rolled out", "trade_counts", "trades_by_opening_day", "how_it_ended", "every trade it covers"):
        assert phrase in low, phrase


# ---------------------------------------------------------------------------
# The validator
# ---------------------------------------------------------------------------

def test_good_copy_passes(p09):
    _, _, full = p09
    copy, v = validate(good(), full)
    assert v.passed, v.reasons()


def _with(change):
    d = good()
    change(d)
    return d


def _obs(text):
    return lambda d: d["observations"][0].__setitem__("text", text)


BAD = {
    "raw number": (_obs("You sold 2 SBUX puts."), "no_raw_numbers", "2"),
    "unknown placeholder": (_obs("You rolled {{rolls_total_ever}}."), "known_facts", "rolls_total_ever"),
    "placeholder in heading": (lambda d: d["observations"][0].__setitem__("heading", "Sold on {{trade.t2.opened}}"),
                               "known_facts", "must not contain placeholders"),
    "banned word": (_obs("It was a quiet month."), "copy_rules", "quiet"),
    "bet": (_obs("Each put was a bet on SBUX."), "copy_rules", "bet"),
    "jargon": (_obs("You sold puts at a low delta."), "copy_rules", "delta"),
    "retired wording": (_obs("You picked end dates a few weeks out."), "copy_rules", "end dates"),
    "em dash": (_obs("You sold puts — and held them."), "copy_rules", "—"),
    "exclamation": (lambda d: d.__setitem__("subject_line", "Your October!"), "copy_rules", "!"),
    "advice": (_obs("You should consider rolling earlier."), "no_advice", "you should"),
    "praise": (_obs("Great job on the SBUX puts."), "no_praise", "great"),
    "consolation": (_obs("It was a tough month, but it happens."), "no_praise", "tough month"),
    "month's result": (_obs("Your account ended {{month_return}}."), "results_left_to_code", "month_return"),
    "doubled unit": (_obs("You held the first SBUX put {{trade.t2.held}} days."), "no_doubled_units", "repeats 'days'"),
    "repeated word": (lambda d: d.__setitem__("overall", "You sold puts and sold more puts and puts puts away."),
                      "no_doubled_units", "repeats the word 'puts'"),
    "too long": (_obs("You sold puts. " * 40), "schema_and_lengths", "characters"),
    "one observation": (lambda d: d["observations"].pop(), "schema_and_lengths", "observations"),
    "not json": (None, "schema_and_lengths", "not a valid RecapCopy"),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_planted_bad_output_fails_with_reason(p09, name):
    change, check, needle = BAD[name]
    raw = "{not json" if change is None else _with(change)
    _, v = validate(raw, p09[2])
    assert not v.passed, name
    failed = {c.name: c.reasons for c in v.checks if not c.passed}
    assert check in failed, (name, failed)
    assert any(needle.lower() in r.lower() for r in failed[check]), (name, failed[check])


def test_a_trade_cited_in_a_passage_must_be_named_in_it(p09, cases):
    sheet, linked, _ = p09
    c = compose(sheet, linked)
    nke = next(t["ref"] for t in c.trades if t["underlying"] == "NKE")
    # an NKE trade's date in a passage that only names SBUX
    bad = _with(_obs(f"Your SBUX put was opened {{{{trade.{nke}.opened}}}}."))
    reasons = " ".join(validate(bad, c.sheet, c.trades)[1].reasons())
    assert "trades_named" in reasons and "a NKE trade, but never names NKE" in reasons
    assert validate(good(), c.sheet, c.trades)[1].passed


def test_sentences_opening_with_a_value_are_capitalized(p09):
    from pipeline.render import sentence
    _, _, full = p09
    out = sentence("You held them. {{group.winning.bought_back_before_expiration_day}} winning trades ended early.", full)
    assert "You held them. Both winning trades" in out


def test_low_activity_allows_at_most_one_observation(cases):
    linked, sheet = cases["p01"]
    assert "low_activity" in sheet.flags
    full = compose(sheet, linked).sheet
    d = {"overall": "You added to funds you already held.", "subject_line": "Your October", "observations": []}
    assert validate(d, full)[1].passed
    d["observations"] = [{"heading": "One", "text": "You held funds."}, {"heading": "Two", "text": "You held more."}]
    reasons = " ".join(validate(d, full)[1].reasons())
    assert "need 0 to 1" in reasons


# ---------------------------------------------------------------------------
# What code writes, and rendering
# ---------------------------------------------------------------------------

def test_red_month_names_the_loss_once_beside_the_market(p09):
    sheet, linked, full = p09
    text = render_text(RecapCopy.model_validate(good()), sheet, linked)
    shown = {f.id: f.display for f in full.facts}
    assert text.count(shown["month_return"]) == 1
    line = next(l for l in text.splitlines() if shown["month_return"] in l)
    assert shown["spy_move"] in line


def test_render_fills_every_placeholder_and_keeps_the_fine_print(p09):
    sheet, linked, _ = p09
    text = render_text(RecapCopy.model_validate(good()), sheet, linked)
    assert "{{" not in text and "Riley, here is your October 2026." in text
    for head in ("AT A GLANCE", "GETTING IN", "WHAT STOOD OUT", "HOW IT TURNED OUT", "FINE PRINT"):
        assert head in text, head
    assert "Your SBUX puts were bought back early. The SBUX put opened Oct 1" in text


def test_render_rejects_unknown_placeholder(p09):
    sheet, linked, _ = p09
    bad = RecapCopy.model_validate(_with(_obs("You rolled {{not_a_fact}}.")))
    with pytest.raises(UnknownPlaceholder):
        render_text(bad, sheet, linked)


def test_code_written_lines_use_trader_terms_and_no_retired_wording(cases):
    rules = copy_rules()
    for linked, sheet in cases.values():
        c = compose(sheet, linked)
        lines = sum(c.sections.values(), []) + [c.results_line, c.what_changed] + [t.plain for t in c.tiles]
        for line in filter(None, lines):
            assert "{{" not in line
            assert not validate_copy._phrases(line, rules["banned"] + rules["jargon"] + rules["retired"]), line


def test_low_activity_recap_has_no_sections(cases):
    linked, sheet = cases["p01"]
    c = compose(sheet, linked)
    assert all(not v for v in c.sections.values()) and len(c.tiles) <= 3


# ---------------------------------------------------------------------------
# Retry once, then review
# ---------------------------------------------------------------------------

def fake(*outputs, stop="end_turn"):
    calls = []

    def call(cfg, system, user):
        calls.append(user)
        out = outputs[min(len(calls) - 1, len(outputs) - 1)]
        return ModelReply(out if isinstance(out, str) else json.dumps(out), stop, {"input_tokens": 1000, "output_tokens": 300})

    call.calls = calls
    return call


def test_first_try_passes(p09):
    sheet, linked, _ = p09
    res = write_recap(sheet, linked, CFG, fake(good()))
    assert res.status == "passed" and len(res.attempts) == 1 and res.recap is not None
    assert res.prompt_version == CFG.prompt_version and len(res.prompt_sha256) == 64


def test_retry_once_with_reasons(p09):
    sheet, linked, _ = p09
    call = fake(_with(BAD["raw number"][0]), good())
    res = write_recap(sheet, linked, CFG, call)
    assert res.status == "passed" and len(res.attempts) == 2
    assert "failed these checks" in call.calls[1] and "no_raw_numbers" in call.calls[1]


def test_two_failures_go_to_review(p09, tmp_path, fresh):
    sheet, linked, _ = p09
    bad = _with(BAD["advice"][0])
    res = write_recap(sheet, linked, CFG, fake(bad, bad))
    assert res.status == "review" and res.recap is None and len(res.attempts) == 2
    run("2026-10", "p09", fresh / "raw", tmp_path, cfg=CFG, call=fake(bad, bad))
    assert (tmp_path / "2026-10" / "review" / "p09.json").exists()
    assert not (tmp_path / "2026-10" / "p09" / "recap.txt").exists()


def test_refusal_goes_to_review(p09):
    sheet, linked, _ = p09
    res = write_recap(sheet, linked, CFG, fake("", stop="refusal"))
    assert res.status == "review"
    assert res.attempts[0].validation.checks[0].name == "model_refused"


def test_cli_writes_recap(fresh, tmp_path):
    run("2026-10", "p09", fresh / "raw", tmp_path, cfg=CFG, call=fake(good()))
    text = (tmp_path / "2026-10" / "p09" / "recap.txt").read_text(encoding="utf-8")
    assert "{{" not in text and "your statement is correct" in text and "Riley" in text


# ---------------------------------------------------------------------------
# What the model is sent
# ---------------------------------------------------------------------------

def test_user_turn_has_facts_counts_and_trades_but_no_sources(p09):
    sheet, linked, _ = p09
    c = compose(sheet, linked)
    u = json.loads(user_turn(c))
    days = u["trades_by_opening_day"]
    assert u["first_name"] == "Riley" and u["month_facts"] and u["trade_counts"] and sum(len(d["trades"]) for d in days) == 4
    assert [d["opened"] for d in days] == ["Sep 21", "Oct 1", "Oct 20", "Oct 29"]
    assert "first_month" in sheet.flags and "first_month" not in u["flags"]   # read as "your first month"
    assert all("sources" not in f for f in u["month_facts"] + u["trade_counts"])
    assert {t["how_it_ended"] for d in days for t in d["trades"]} >= {"bought back before its expiration day"}
    assert u["already_in_the_recap"]["results_line"].startswith("Your account ended the month")
    assert user_turn(c) == user_turn(compose(sheet, linked))       # stable bytes, cache friendly


class _FakeClient:
    def __init__(self):
        self.sent = {}

        class _Resp:
            content = [type("B", (), {"type": "text", "text": json.dumps(good())})()]
            stop_reason = "end_turn"
            model = "x"
            usage = type("U", (), {"input_tokens": 1, "output_tokens": 1, "cache_read_input_tokens": 0,
                                   "cache_creation_input_tokens": 0})()

        def create(**kw):
            self.sent = kw
            return _Resp()

        self.messages = type("M", (), {"create": staticmethod(create)})()
        self.beta = type("B", (), {"messages": type("M", (), {"create": staticmethod(create)})()})()


@pytest.mark.parametrize("model,fallback", [("claude-opus-5-5", True), ("claude-sonnet-5-5", True),
                                            ("claude-haiku-5-5", False)])
def test_request_shape(p09, model, fallback):
    sheet, linked, _ = p09
    client = _FakeClient()
    cfg = ModelConfig.load(model=model)
    anthropic_caller(client)(cfg, "SYSTEM", user_turn(compose(sheet, linked)))
    kw = client.sent
    assert kw["model"] == model
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["output_config"]["format"]["schema"]["required"] == ["overall", "observations", "subject_line"]
    assert kw["output_config"]["effort"] == cfg.effort
    assert "temperature" not in kw and "thinking" not in kw
    assert ("fallbacks" in kw) == fallback
    assert len(kw["messages"]) == 1 and kw["messages"][0]["role"] == "user"
