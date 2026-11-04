"""Stage 5b: the review gallery (SPEC 5.2). One self-contained HTML page per month: a
card per customer with the rendered recap, its status and flags, the validator result,
the facts sheet, every trade, and the claims a reviewer should spot-check.

Claims to spot-check: sentences and headings in which Claude's own words (not the
values code fills in) say something about a group of trades or a comparison: "every",
"all", "both", "only", "the largest", "first", "last", "well before", "the same day",
"through" (a range of days). The validator checks numbers, facts and wording but cannot
check these, so each is shown beside the counts and trades it cites. Counts themselves
("3 of 5") are computed by code and need no check.

A customer "needs review" when the copy failed the validator twice, the model declined,
or the data has a quality issue; the reasons are named on the card.

    python -m pipeline.run --month 2026-10 --gallery [--results out/compare_v4c/2026-10/claude-opus-5-5.json]
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline.compose import Composed, compose
from pipeline.demo import FLAG_WORDS, ISSUE_WORDS, month_name
from pipeline.ingest import customers
from pipeline.render import render_text, sentence
from pipeline.run import facts_for
from pipeline.validate_copy import PLACEHOLDER, placeholders
from pipeline.write import ModelConfig, cost
from schemas.recap import RecapCopy, WriteResult

REPO = Path(__file__).resolve().parent.parent
CLAIM_WORDS = re.compile(r"\b(every|all|each|both|none|neither|never|always|only|most|mostly|largest|smallest|"
                         r"biggest|first|last|through|until|whole|entire|any|well|almost|nearly|often|rarely|"
                         r"same|steadily|further|farther|longer|shorter|more|less|fewer|earlier|later|closer|"
                         r"higher|lower|larger|smaller|bigger|instead|replacement|than)\b", re.I)
SENTENCE = re.compile(r"(?<=[.!?])\s+")


def _trade_rows(c: Composed) -> tuple[list[dict], dict[str, str]]:
    """One row per trade, and story id -> trade ref (trade facts cite their story)."""
    shown = {f.id: f for f in c.sheet.facts}
    rows, by_story = [], {}
    for t in c.trades:
        ref = t["ref"]
        g = lambda k: shown[f"trade.{ref}.{k}"].display if f"trade.{ref}.{k}" in shown else ""  # noqa: E731
        sid = shown[f"trade.{ref}.opened"].sources[0].removeprefix("story:")
        by_story[sid] = ref
        rows.append({"ref": ref, "underlying": t["underlying"], "strategy": t["strategy_at_open"],
                     "strategy_end": t["strategy_at_end"], "opened": g("opened"), "ended": g("ended"),
                     "held": g("held"), "expiry_at_open": g("expiry_at_open"), "left": g("left_at_close"),
                     "how_it_ended": t["how_it_ended"], "outcome": t["outcome"] or "", "result": g("result"),
                     "rolls": g("rolls")})
    return rows, by_story


def claims(copy: RecapCopy, c: Composed, rows: list[dict], by_story: dict[str, str]) -> list[dict]:
    """Sentences that make a claim about a group of trades, with what they cite."""
    facts = {f.id: f for f in c.sheet.facts}
    row = {r["ref"]: r for r in rows}
    out = []
    texts = [("Overall picture", copy.overall)]
    for o in copy.observations:
        texts += [(f"What stood out, heading: {o.heading}", o.heading), (f"What stood out: {o.heading}", o.text)]
    for where, text in texts:
        for s in SENTENCE.split(text.strip()):
            cited = list(dict.fromkeys(placeholders(s)))
            own_words = PLACEHOLDER.sub(" ", s)            # Claude's words only: filled values come from code
            words = sorted({w.lower() for w in CLAIM_WORDS.findall(own_words)})
            if not words:
                continue
            refs = {p.split(".")[1] for p in cited if p.startswith("trade.")}
            # trades of every ticker the sentence names: "SBUX puts were bought back..."
            refs |= {r["ref"] for r in rows if re.search(rf"(?<![A-Za-z/]){re.escape(r['underlying'])}(?![A-Za-z])", own_words)}
            out.append({"where": where, "text": sentence(s, c.sheet), "words": words,
                        "facts": [{"id": p, "display": facts[p].display, "plain": facts[p].plain}
                                  for p in cited if p in facts and not p.startswith("trade.")],
                        "trades": [r for r in rows if r["ref"] in refs]})
    return out


def card(raw_root: Path, month: str, cid: str, res: WriteResult | None, cfg: ModelConfig) -> dict:
    linked, sheet = facts_for(raw_root, month, cid)
    c = compose(sheet, linked)
    rows, by_story = _trade_rows(c)
    reasons = []
    if res is None:
        reasons.append("No recap was written for this customer in this run.")
    elif res.status != "passed":
        last = res.attempts[-1].validation if res.attempts else None
        if last and any(ch.name == "model_refused" for ch in last.checks):
            reasons.append("The model declined to write this recap.")
        else:
            reasons.append("Claude's copy failed the checks twice: " + "; ".join(last.reasons() if last else []))
    for i in sheet.data_quality_issues:
        reasons.append(f"Data: {ISSUE_WORDS.get(i.issue, i.issue)} ({len(i.refs)})")
    passed = res is not None and res.status == "passed"
    return {
        "id": cid, "name": sheet.first_name, "month_name": month_name(month),
        "status": "ready" if passed and not sheet.data_quality_issues else "needs review", "reasons": reasons,
        "flags": [FLAG_WORDS[f] for f in sheet.flags if f in FLAG_WORDS],
        "recap_text": render_text(res.recap, sheet, linked) if passed else None,
        "draft": res.attempts[-1].raw_output if res is not None and not passed and res.attempts else None,
        "attempts": [{"passed": a.validation.passed, "reasons": a.validation.reasons(),
                      "checks": [(ch.name, ch.passed) for ch in a.validation.checks]} for a in (res.attempts if res else [])],
        "claims": claims(res.recap, c, rows, by_story) if passed else [],
        "facts": [{"id": f.id, "display": f.display, "plain": f.plain, "kind": "count" if f.id.startswith("group.") else "month"}
                  for f in c.sheet.facts if not f.id.startswith("trade.")],
        "trades": rows,
        "model": res.model if res else "", "prompt_version": res.prompt_version if res else "",
        "cost": round(cost(res, cfg), 4) if res else 0.0,
    }


def load_results(out_root: Path, month: str, results: Path | None = None) -> dict[str, WriteResult]:
    """WriteResults by customer: from a saved comparison file, or from a run's output
    folder (out/<month>/<customer>/recap.json and out/<month>/review/<customer>.json)."""
    if results is not None:
        return {r["customer_id"]: WriteResult.model_validate(r) for r in json.loads(results.read_text(encoding="utf-8"))}
    found = {}
    for path in sorted((out_root / month).glob("*/recap.json")) + sorted((out_root / month / "review").glob("*.json")):
        r = WriteResult.model_validate(json.loads(path.read_text(encoding="utf-8")))
        found[r.customer_id] = r
    return found


def build(month: str, raw_root: Path, results: dict[str, WriteResult], only: list[str] | None = None) -> str:
    """`only` limits the cards (a monthly run shows every recap that needs review plus a
    sample, never thousands of cards); by default every customer in the month."""
    cfg = ModelConfig.load()
    cards = [card(raw_root, month, cid, results.get(cid), cfg) for cid in (only if only is not None else customers(raw_root, month))]
    env = Environment(loader=FileSystemLoader(REPO / "templates"), autoescape=select_autoescape(["html", "j2"]),
                      trim_blocks=True, lstrip_blocks=True)
    return env.get_template("gallery.html.j2").render(
        month_name=month_name(month), cards=cards,
        ready=sum(c["status"] == "ready" for c in cards), review=sum(c["status"] != "ready" for c in cards),
        claims=sum(len(c["claims"]) for c in cards), cost=round(sum(c["cost"] for c in cards), 2),
        models=sorted({c["model"] for c in cards if c["model"]}),
        prompts=sorted({c["prompt_version"] for c in cards if c["prompt_version"]}))
