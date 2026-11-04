"""Stage 4c: the claim check. A second model call reads every sentence Claude wrote in a
recap beside the customer's facts, counts and full trade table, and marks each sentence
supported or unsupported.

The validator (pipeline/validate_copy.py) checks numbers, facts, wording and structure;
it cannot check what a sentence claims about the trades ("every losing spread was closed
before expiration", "the replacement puts went further out", "losing trades opened Nov 11,
Nov 12 and Nov 17"). At thousands of recaps a month people cannot read them all, so this
check does: an unsupported sentence fails the check `claims_supported`, the recap gets one
rewrite with the reason, and if it still fails it goes to review.

Every sentence is checked, not only those with group words (the word list in the gallery
missed "in your first month" and "late in the day").
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pipeline.compose import Composed
from pipeline.validate_copy import filled
from schemas.recap import CheckResult, RecapCopy

REPO = Path(__file__).resolve().parent.parent
PROMPT_VERSION = "claimcheck_v2"
SENTENCE = re.compile(r"(?<=[.!?])\s+")

OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["sentences"],
    "properties": {"sentences": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["id", "verdict", "reason"],
        "properties": {"id": {"type": "string"}, "verdict": {"type": "string", "enum": ["supported", "unsupported"]},
                       "reason": {"type": "string"}}}}},
}


def sentences(copy: RecapCopy) -> list[dict]:
    """Every sentence Claude wrote, with an id and where it sits (headings whole)."""
    out = []

    def add(where: str, text: str, whole: bool = False):
        for s in ([text.strip()] if whole else SENTENCE.split(text.strip())):
            if s:
                out.append({"id": f"s{len(out) + 1}", "where": where, "text": s})

    add("overall picture", copy.overall)
    for i, o in enumerate(copy.observations, start=1):
        add(f"what stood out {i}, heading", o.heading, whole=True)
        add(f"what stood out {i}", o.text)
    return out


def evidence(c: Composed) -> dict:
    """What the checker reads: the month's facts and counts, and every trade as a row."""
    shown = {f.id: f for f in c.sheet.facts}
    trades = []
    for t in c.trades:
        ref = t["ref"]
        row = {"ticker": t["underlying"], "strategy_at_open": t["strategy_at_open"], "strategy_at_end": t["strategy_at_end"],
               "opened_before_this_month": t["opened_before_this_month"], "how_it_ended": t["how_it_ended"],
               "outcome": t["outcome"], "kinds_of_rolls": t["roll_kinds"]}
        for f in t["facts"]:
            row[f["id"].split(".")[-1]] = f"{f['display']} ({f['plain']})"
        trades.append(row)
    return {"first_name": c.sheet.first_name, "month": c.sheet.month,
            "flags": [f for f in c.sheet.flags if f != "first_month"],
            "month_facts": [f"{f.display}: {f.plain}" for f in c.sheet.facts if not f.id.startswith(("trade.", "group."))],
            "counts": [f"{f.display}: {f.plain}" for f in c.sheet.facts if f.id.startswith("group.")],
            "trades_in_opening_order": trades}


def user_turn(copy: RecapCopy, c: Composed) -> str:
    sents = [{"id": s["id"], "where": s["where"], "text": filled(s["text"], c.sheet)} for s in sentences(copy)]
    return json.dumps({"sentences_to_check": sents, "data": evidence(c)}, ensure_ascii=False, sort_keys=True)


def system_prompt() -> str:
    return (REPO / "prompts" / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")


@dataclass(frozen=True)
class CheckReply:
    verdicts: dict[str, dict]      # sentence id -> {"verdict", "reason"}
    usage: dict
    raw: str


def parse(text: str) -> dict[str, dict]:
    d = json.loads(text)
    return {s["id"]: {"verdict": s["verdict"], "reason": s["reason"]} for s in d["sentences"]}


def result(copy: RecapCopy, c: Composed, verdicts: dict[str, dict]) -> CheckResult:
    """The check `claims_supported`: fails with one reason per unsupported sentence. A
    sentence the checker skipped counts as unsupported."""
    reasons = []
    for s in sentences(copy):
        v = verdicts.get(s["id"])
        if v is None:
            reasons.append(f'{s["where"]}: "{filled(s["text"], c.sheet)}" was not checked')
        elif v["verdict"] != "supported":
            reasons.append(f'{s["where"]}: "{filled(s["text"], c.sheet)}" is not supported by the trades: {v["reason"]}')
    return CheckResult(name="claims_supported", passed=not reasons, reasons=reasons)


def anthropic_check(model: str, effort: str = "medium", client=None):
    """A caller: (system, user) -> CheckReply, using the Messages API."""
    import anthropic

    client = client or anthropic.Anthropic()

    def call(system: str, user: str) -> CheckReply:
        kwargs = dict(model=model, max_tokens=8000,
                      system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                      messages=[{"role": "user", "content": user}],
                      output_config={"effort": effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}})
        resp = client.messages.create(**kwargs)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        u = resp.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0, "model": resp.model}
        if resp.stop_reason == "refusal":
            return CheckReply({}, usage, text)
        return CheckReply(parse(text), usage, text)

    return call


def check(copy: RecapCopy, c: Composed, call) -> tuple[CheckResult, CheckReply]:
    reply = call(system_prompt(), user_turn(copy, c))
    return result(copy, c, reply.verdicts), reply
