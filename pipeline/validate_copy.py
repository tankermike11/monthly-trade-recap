"""Stage 4b: the copy validator (SPEC 4.3). Code checks the model's words before
anything renders. Each rule is a named check that passes or fails with reasons.

1. schema_and_lengths   valid RecapCopy; max 3 lines per section, 160 characters per
                        line and subject line max 50 (both counted after the
                        placeholders are filled), 3 to 4 glance tiles (3 for short
                        months), max 3 teasers
2. no_raw_numbers       no digits anywhere outside {{placeholders}}
3. known_facts          every placeholder, glance tile and teaser fact is in this
                        customer's facts sheet
4. copy_rules           no banned words, no jargon, no em or en dashes, no "!"
5. no_advice            no advice or prediction phrases
6. no_praise            no praise for activity or wins; no consolation cliches
7. red_month            if red_month: the results line names one results fact, once
                        in the whole recap, beside at least one market fact
8. no_doubled_units     a placeholder whose display ends in its unit ("53 trades") is
                        not followed by that unit again ("53 trades new trades")
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from schemas.facts import FactsSheet
from schemas.recap import CheckResult, RecapCopy, ValidationResult

REPO = Path(__file__).resolve().parent.parent
PLACEHOLDER = re.compile(r"\{\{\s*([a-z0-9_.]+)\s*\}\}")
RESULTS_FACTS = ("month_pnl", "month_return", "net_realized_pnl")
MARKET_FACTS = ("spy_move", "qqq_move")
MAX_LINES, MAX_CHARS, MAX_SUBJECT, MAX_TEASERS = 3, 160, 50, 3


@lru_cache(maxsize=1)
def copy_rules() -> dict:
    return yaml.safe_load((REPO / "config" / "copy_rules.yaml").read_text(encoding="utf-8"))


def texts(copy: RecapCopy) -> list[tuple[str, str]]:
    """Every customer-facing string with where it sits."""
    out = [("one_liner", copy.one_liner), ("results_line", copy.results_line),
           ("what_changed", copy.what_changed), ("subject_line", copy.subject_line)]
    for name in ("getting_in", "managing", "getting_out"):
        out += [(f"{name}[{i}]", t) for i, t in enumerate(getattr(copy.sections, name))]
    out += [(f"teasers[{i}]", t.line) for i, t in enumerate(copy.teasers)]
    return [(w, t) for w, t in out if t]


def placeholders(text: str) -> list[str]:
    return PLACEHOLDER.findall(text)


def filled(text: str, sheet: FactsSheet) -> str:
    """The text as the customer will read it. Unlike pipeline.render.fill, an unknown
    placeholder is left as typed rather than raised, so the validator can report it."""
    display = {f.id: f.display for f in sheet.facts}
    return PLACEHOLDER.sub(lambda m: display.get(m.group(1), m.group(0)), text)


def _phrases(text: str, words: list[str]) -> list[str]:
    low = PLACEHOLDER.sub(" ", text).lower()
    return [w for w in words if re.search(rf"(?<![a-z]){re.escape(w.lower())}(?![a-z])", low)]


def _check(name: str, reasons: list[str]) -> CheckResult:
    return CheckResult(name=name, passed=not reasons, reasons=reasons)


def validate(raw: str | dict | RecapCopy, sheet: FactsSheet) -> tuple[RecapCopy | None, ValidationResult]:
    # 1. schema and lengths
    try:
        if isinstance(raw, RecapCopy):
            copy = raw
        elif isinstance(raw, dict):
            copy = RecapCopy.model_validate(raw)
        else:
            copy = RecapCopy.model_validate(json.loads(raw))
    except (ValidationError, json.JSONDecodeError, TypeError) as e:
        msg = str(e).splitlines()[0] if str(e) else type(e).__name__
        return None, ValidationResult(passed=False, checks=[_check("schema_and_lengths", [f"not a valid RecapCopy: {msg}"])])

    short = "low_activity" in sheet.flags
    r = []
    for name in ("getting_in", "managing", "getting_out"):
        n = len(getattr(copy.sections, name))
        if n > MAX_LINES:
            r.append(f"sections.{name} has {n} lines (max {MAX_LINES})")
        if short and n:
            r.append(f"sections.{name} must be empty in a short (low_activity) recap")
    # Lengths count what the customer reads: placeholders filled with their display
    # strings, as the renderer does. Unknown placeholders stay as typed (check 3 names them).
    for where, t in texts(copy):
        n = len(filled(t, sheet))
        if n > MAX_CHARS:
            r.append(f"{where} is {n} characters once the placeholders are filled (max {MAX_CHARS})")
    n = len(filled(copy.subject_line, sheet))
    if n > MAX_SUBJECT:
        r.append(f"subject_line is {n} characters once the placeholders are filled (max {MAX_SUBJECT})")
    if not copy.one_liner.strip() or not copy.subject_line.strip():
        r.append("one_liner and subject_line must not be empty")
    lo, hi = (3, 3) if short else (3, 4)
    if not lo <= len(copy.glance_tiles) <= hi:
        r.append(f"{len(copy.glance_tiles)} glance tiles (need {lo}" + (f" to {hi})" if hi != lo else ")"))
    if len(copy.teasers) > MAX_TEASERS:
        r.append(f"{len(copy.teasers)} teasers (max {MAX_TEASERS})")
    checks = [_check("schema_and_lengths", r)]

    # 2. no raw numbers
    r = []
    for where, t in texts(copy):
        stripped = PLACEHOLDER.sub("", t)
        found = re.findall(r"\d+(?:[.,]\d+)*%?", stripped)
        if found:
            r.append(f"{where} types the number(s) {', '.join(found)} instead of a placeholder")
    checks.append(_check("no_raw_numbers", r))

    # 3. known facts
    known = {f.id for f in sheet.facts}
    r = []
    for where, t in texts(copy):
        for p in placeholders(t):
            if p not in known:
                r.append(f"{where} uses {{{{{p}}}}}, which is not in this customer's facts sheet")
        if re.search(r"\{\{(?![a-z0-9_.\s]+\}\})", t) or re.search(r"(?<!\})\}\}", PLACEHOLDER.sub("", t)):
            r.append(f"{where} has a malformed placeholder")
    for i, tile in enumerate(copy.glance_tiles):
        if tile not in known:
            r.append(f"glance_tiles[{i}] is {tile!r}, which is not in this customer's facts sheet")
    for i, t in enumerate(copy.teasers):
        if t.fact not in known:
            r.append(f"teasers[{i}].fact is {t.fact!r}, which is not in this customer's facts sheet")
    checks.append(_check("known_facts", r))

    rules = copy_rules()
    # 4. copy rules
    r = []
    for where, t in texts(copy):
        for ch in rules["characters"]["forbidden"]:
            if ch in t:
                r.append(f"{where} contains {ch!r}")
        for w in _phrases(t, rules["banned"]):
            r.append(f"{where} uses the banned word {w!r}")
        for w in _phrases(t, rules["jargon"]):
            r.append(f"{where} uses the jargon {w!r}")
    checks.append(_check("copy_rules", r))

    # 5. no advice or predictions
    r = [f"{where} gives advice or a prediction ({w!r})"
         for where, t in texts(copy) for w in _phrases(t, rules["advice"])]
    checks.append(_check("no_advice", r))

    # 6. no praise for activity or wins, no consolation cliches
    r = [f"{where} praises or consoles ({w!r})"
         for where, t in texts(copy) for w in _phrases(t, rules["praise"] + rules["consolation"])]
    checks.append(_check("no_praise", r))

    # 7. red month
    r = []
    if "red_month" in sheet.flags:
        res_uses = [(w, p) for w, t in texts(copy) for p in placeholders(t) if p in RESULTS_FACTS]
        line = copy.results_line
        in_line = [p for p in placeholders(line) if p in RESULTS_FACTS]
        market = [p for p in placeholders(line) if p in MARKET_FACTS]
        if len(in_line) != 1:
            r.append(f"the results line must name the month's result once with one of "
                     f"{', '.join('{{%s}}' % f for f in RESULTS_FACTS)} (found {len(in_line)})")
        if len(res_uses) > 1:
            r.append(f"the result is named {len(res_uses)} times; name the loss once "
                     f"({', '.join(w for w, _ in res_uses)})")
        if not market:
            r.append("the results line must put the loss next to the market move "
                     "({{spy_move}} or {{qqq_move}})")
    checks.append(_check("red_month", r))

    # 8. no doubled units
    checks.append(_check("no_doubled_units", [f"{where} {r}" for where, t in texts(copy)
                                              for r in doubled_units(t, sheet)]))

    return copy, ValidationResult(passed=all(c.passed for c in checks), checks=checks)


def _unit(word: str) -> str:
    w = word.lower()
    return w[:-1] if w.endswith("s") and len(w) > 3 else w


def doubled_units(text: str, sheet: FactsSheet, window: int = 2) -> list[str]:
    """A placeholder whose display already ends in its unit ("53 trades") followed,
    within `window` words, by that unit again: "{{stories_opened}} new trades" reads
    "53 trades new trades". Singular and plural count as the same unit."""
    display = {f.id: f.display for f in sheet.facts}
    found = []
    for m in PLACEHOLDER.finditer(text):
        shown = display.get(m.group(1))
        if not shown or not re.search(r"\d", shown):
            continue
        last = re.search(r"([a-z]+)\s*$", shown)          # lowercase unit word, so tickers never match
        if not last:
            continue
        after = re.findall(r"[A-Za-z]+", filled(text[m.end():], sheet))[:window]
        hit = next((w for w in after if _unit(w) == _unit(last.group(1))), None)
        if hit:
            found.append(f"repeats {hit!r} after {{{{{m.group(1)}}}}}, which already reads {shown!r}")
    return found
