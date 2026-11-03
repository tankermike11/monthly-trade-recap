"""Phase 4, offline: the validator, the retry/review flow, rendering and the request
shape, with a fake model in place of the API. Live runs are in test_live.py
(pytest -m live)."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pipeline.render import UnknownPlaceholder, render_text
from pipeline.run import facts_for, run
from pipeline import validate_copy
from pipeline.validate_copy import copy_rules, validate
from pipeline.write import ModelConfig, ModelReply, anthropic_caller, user_turn, write_recap
from tests.conftest import REPO

PROMPT_VERSION = ModelConfig.load().prompt_version
PROMPT = (REPO / "prompts" / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")
EXAMPLES = dict(zip(["p04", "p09", "p01"], re.findall(r"```json\n(.*?)```", PROMPT, re.S)))


@pytest.fixture(scope="module")
def sheets(fresh):
    return {p: facts_for(fresh / "raw", "2026-10", p)[1] for p in ("p01", "p04", "p09")}


def good(p="p09") -> dict:
    return json.loads(EXAMPLES[p])


# ---------------------------------------------------------------------------
# The worked examples in the prompt pass the validator for their own personas
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("p", ["p04", "p09", "p01"])
def test_prompt_examples_pass(sheets, p):
    copy, v = validate(EXAMPLES[p], sheets[p])
    assert v.passed, v.reasons()
    render_text(copy, sheets[p])


def test_prompt_states_the_copy_rules():
    low = PROMPT.lower()
    for w in copy_rules()["banned"]:
        assert w in low, w
    for phrase in ("second person", "past tense", "em dash", "exclamation", "you should", "never type a number",
                   "name the month's result exactly once", "low_activity", "first half of the month",
                   "expiration", "strike", "premium", "rolled out"):
        assert phrase in low, phrase


# ---------------------------------------------------------------------------
# Planted bad outputs fail with a clear reason each
# ---------------------------------------------------------------------------

def _plant(change, p="p09"):
    d = good(p)
    change(d)
    return d


BAD = {
    "retired wording": (lambda d: d["sections"]["getting_in"].__setitem__(0, "You usually picked end dates {{median_entry_dte}} away."),
                        "copy_rules", "end dates"),
    "raw number": (lambda d: d["sections"]["getting_in"].__setitem__(0, "You usually opened trades expiring 21 days out."),
                   "no_raw_numbers", "21"),
    "banned word": (lambda d: d.__setitem__("one_liner", "You had a quiet month selling options for cash upfront."),
                    "copy_rules", "quiet"),
    "jargon": (lambda d: d["sections"]["getting_in"].__setitem__(0, "You usually sold strangles at the 16 delta strike {{median_entry_dte}} out."),
               None, None),
    "unknown placeholder": (lambda d: d["sections"]["managing"].__setitem__(0, "You rolled {{roll_count_total}}."),
                            "known_facts", "roll_count_total"),
    "unknown tile": (lambda d: d["glance_tiles"].__setitem__(0, "trades_ever"), "known_facts", "trades_ever"),
    "advice": (lambda d: d.__setitem__("what_changed", "You should consider picking later end dates."),
               "no_advice", "you should"),
    "praise": (lambda d: d.__setitem__("one_liner", "Great job selling options for cash upfront."),
               "no_praise", "great job"),
    "consolation": (lambda d: d.__setitem__("what_changed", "It was a tough month, but it happens."),
                    "no_praise", "tough month"),
    "em dash": (lambda d: d.__setitem__("one_liner", "You sold options — and held them."), "copy_rules", "—"),
    "exclamation": (lambda d: d.__setitem__("subject_line", "Your October!"), "copy_rules", "!"),
    "too long": (lambda d: d.__setitem__("one_liner", "You sold options " * 12), "schema_and_lengths", "characters"),
    "subject too long": (lambda d: d.__setitem__("subject_line", "Your October, trade by trade, every single one of them"),
                         "schema_and_lengths", "subject_line"),
    "four lines": (lambda d: d["sections"]["getting_out"].extend(["a.", "b."]), "schema_and_lengths", "getting_out"),
    "red month without market": (lambda d: d.__setitem__("results_line", "Your account ended the month {{month_return}}."),
                                 "red_month", "market"),
    "loss named twice": (lambda d: d.__setitem__("what_changed", "Your account changed by {{month_pnl}}."),
                         "red_month", "named 2 times"),
    "not json": (None, "schema_and_lengths", "not a valid RecapCopy"),
    "doubled unit": (lambda d: d["sections"]["getting_out"].__setitem__(0, "You ended {{stories_ended}} trades this month."),
                     "no_doubled_units", "repeats 'trades'"),
    "doubled unit, singular": (lambda d: d["sections"]["getting_out"].__setitem__(0, "Only {{losses}} new trades lost money."),
                               "no_doubled_units", "repeats 'trades'"),
}


@pytest.mark.parametrize("name", sorted(BAD))
def test_planted_bad_output_fails_with_reason(sheets, name):
    change, check, needle = BAD[name]
    raw = "{not json" if change is None else _plant(change)
    _, v = validate(raw, sheets["p09"])
    assert not v.passed, name
    failed = {c.name: c.reasons for c in v.checks if not c.passed}
    if check:
        assert check in failed, (name, failed)
        assert any(needle.lower() in r.lower() for r in failed[check]), (name, failed[check])
    else:   # jargon: any of the copy-rule checks names it
        assert any("jargon" in r for rs in failed.values() for r in rs), failed


def test_glance_tiles_need_one_short_value_each(fresh):
    sheet = facts_for(fresh / "raw", "2026-10", "p08")[1]
    by_display = {}
    for f in sheet.facts:
        by_display.setdefault(f.display, []).append(f.id)
    twins = next(ids for d, ids in by_display.items() if len(ids) > 1 and len(d) <= 32 and ", " not in d)
    listy = next(f.id for f in sheet.facts if ", " in f.display)
    short = [f.id for f in sheet.facts if len(f.display) <= 12 and f.id not in twins][:2]

    def reasons(tiles):
        d = good("p09") | {"glance_tiles": tiles}
        _, v = validate(d, sheet)
        return [r for c in v.checks if c.name == "glance_tiles" for r in c.reasons]

    assert reasons(short + [twins[0]]) == []
    assert any("the same as" in r for r in reasons(short + twins[:2]))
    assert any("not a list" in r for r in reasons(short + [listy]))


def test_doubled_unit_check_leaves_normal_sentences_alone(sheets):
    sheet = sheets["p09"]
    for line in ("Of {{stories_ended}} that ended, {{losses}} ended with a loss.",
                 "Your largest loss was {{biggest_loss}}, about the same as the premium collected.",
                 "You usually opened trades expiring {{median_entry_dte}} out.",
                 "You usually kept a trade for {{median_days_held}}; most trades closed early."):
        assert validate_copy.doubled_units(line, sheet) == [], line


def _length_reasons(line: str, sheet) -> list[str]:
    _, v = validate(_plant(lambda d: d.__setitem__("one_liner", line)), sheet)
    return [r for c in v.checks if c.name == "schema_and_lengths" for r in c.reasons]


def test_line_length_counts_filled_text_not_placeholder_names(sheets):
    """The prompt promises 160 characters after filling; the validator must count the same way."""
    sheet = sheets["p09"]
    shrinks = min(sheet.facts, key=lambda f: len(f.display) - len(f.id))
    grows = max(sheet.facts, key=lambda f: len(f.display) - len(f.id))
    assert len(shrinks.display) < len(shrinks.id) + 4 and len(grows.display) > len(grows.id) + 4

    # Over 160 as typed, under 160 as read: passes.
    line = "You opened trades " + f"{{{{{shrinks.id}}}}} " * 3
    line += "and closed them " * ((165 - len(line)) // 16 + 1)
    assert len(line) > 160 and len(validate_copy.filled(line, sheet)) <= 160
    assert not _length_reasons(line, sheet)

    # Under 160 as typed, over 160 as read: fails, and the reason says so.
    base = f"You traded {{{{{grows.id}}}}} " * 3
    line = base + "a" * (160 - len(base) - 1)
    assert len(line) <= 160 < len(validate_copy.filled(line, sheet))
    reasons = _length_reasons(line, sheet)
    assert reasons and "once the placeholders are filled" in reasons[0]


def test_short_month_rules(sheets):
    d = good("p01")
    d["sections"]["managing"] = ["You held your funds."]
    d["glance_tiles"].append("month_return")
    _, v = validate(d, sheets["p01"])
    reasons = " ".join(v.reasons())
    assert "must be empty" in reasons and "glance tiles" in reasons


def test_render_rejects_unknown_placeholder(sheets):
    copy, _ = validate(EXAMPLES["p04"], sheets["p04"])
    bad = copy.model_copy(update={"one_liner": "You moved {{not_a_fact}}."})
    with pytest.raises(UnknownPlaceholder):
        render_text(bad, sheets["p04"])


def test_render_capitalizes_a_line_that_opens_with_a_placeholder(sheets):
    sheet = sheets["p09"]
    fact = next(f for f in sheet.facts if f.display[:1].islower())
    copy, _ = validate(EXAMPLES["p09"], sheet)
    line = f"{{{{{fact.id}}}}} stayed with your winning trades."
    text = render_text(copy.model_copy(update={"what_changed": line}), sheet)
    assert fact.display[:1].upper() + fact.display[1:] + " stayed with" in text
    assert fact.display + " stayed with" not in text


def test_render_teaser_capitalizes_the_value_not_the_line(sheets):
    """A teaser reads "<value> <line>", so the line continues the value."""
    sheet = sheets["p09"]
    fact = next(f for f in sheet.facts if f.display[:1].islower())
    d = good("p09")
    d["teasers"] = [{"fact": fact.id, "line": "of the cash stayed with your winning trades."}]
    copy, _ = validate(d, sheet)
    text = render_text(copy, sheet)
    assert fact.display[:1].upper() + fact.display[1:] + " of the cash stayed" in text
    assert " Of the cash stayed" not in text


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


CFG = ModelConfig.load()


def test_first_try_passes(sheets):
    call = fake(good())
    res = write_recap(sheets["p09"], CFG, call)
    assert res.status == "passed" and len(res.attempts) == 1 and res.recap is not None
    assert res.prompt_version == PROMPT_VERSION and len(res.prompt_sha256) == 64


def test_retry_once_with_reasons(sheets):
    bad = _plant(BAD["raw number"][0])
    call = fake(bad, good())
    res = write_recap(sheets["p09"], CFG, call)
    assert res.status == "passed" and len(res.attempts) == 2
    assert "failed these checks" in call.calls[1] and "no_raw_numbers" in call.calls[1]


def test_two_failures_go_to_review(sheets, tmp_path, fresh):
    bad = _plant(BAD["advice"][0])
    res = write_recap(sheets["p09"], CFG, fake(bad, bad))
    assert res.status == "review" and res.recap is None and len(res.attempts) == 2
    run("2026-10", "p09", fresh / "raw", tmp_path, cfg=CFG, call=fake(bad, bad))
    assert (tmp_path / "2026-10" / "review" / "p09.json").exists()
    assert not (tmp_path / "2026-10" / "p09" / "recap.txt").exists()


def test_refusal_goes_to_review(sheets):
    res = write_recap(sheets["p09"], CFG, fake("", stop="refusal"))
    assert res.status == "review"
    assert res.attempts[0].validation.checks[0].name == "model_refused"


def test_cli_writes_recap(fresh, tmp_path):
    run("2026-10", "p09", fresh / "raw", tmp_path, cfg=CFG, call=fake(good()))
    text = (tmp_path / "2026-10" / "p09" / "recap.txt").read_text(encoding="utf-8")
    assert "{{" not in text and "your statement is correct" in text and "Riley" in text


# ---------------------------------------------------------------------------
# What the model is sent
# ---------------------------------------------------------------------------

def test_user_turn_is_the_facts_sheet_without_sources(sheets):
    u = json.loads(user_turn(sheets["p09"]))
    assert u["customer_id"] == "p09" and u["facts"]
    assert all("sources" not in f for f in u["facts"])
    assert user_turn(sheets["p09"]) == user_turn(sheets["p09"])      # stable bytes, cache friendly


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
def test_request_shape(sheets, model, fallback):
    client = _FakeClient()
    cfg = ModelConfig.load(model=model)
    anthropic_caller(client)(cfg, "SYSTEM", user_turn(sheets["p09"]))
    kw = client.sent
    assert kw["model"] == model
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["output_config"]["effort"] == cfg.effort
    assert "temperature" not in kw and "thinking" not in kw
    assert ("fallbacks" in kw) == fallback
    assert len(kw["messages"]) == 1 and kw["messages"][0]["role"] == "user"
