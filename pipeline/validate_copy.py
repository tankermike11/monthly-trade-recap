"""Stage 4b: the copy validator. Code checks Claude's words before anything renders.

Claude writes the overall picture, the observations and the subject line (schemas/
recap.py); everything else in a recap is written by code (pipeline/compose.py). Each
rule is a named check that passes or fails with reasons. Lengths count the text as the
customer reads it, placeholders filled.

1. schema_and_lengths    valid RecapCopy; overall at most 600 characters, 2 or 3
                         observations (at most 1 in a low-activity month), headings at
                         most 60, observation text at most 450, subject line at most 50
2. no_raw_numbers        no digits anywhere outside {{placeholders}}
3. known_facts           every placeholder is in this customer's facts sheet (month
                         facts, per-trade facts, group counts); none in headings or the
                         subject line
4. copy_rules            no banned, shorthand or retired words; no em or en dashes; no "!"
5. no_advice             no advice or prediction phrases
6. no_praise             no praise for activity or wins; no consolation cliches
7. results_left_to_code  the month's result is named only in the code-written results
                         line, beside the market move, so Claude never names it
8. no_doubled_units      no unit repeated after a value ("53 trades new trades") and no
                         word repeated across a value ("about about 7 weeks")
9. trades_named          a passage that cites a trade's facts ({{trade.t3.opened}}) names
                         that trade's ticker, so a date or amount cannot drift onto another
                         trade (a /MES trade's date in a sentence about /MNQ)
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
MAX_OVERALL, MAX_HEADING, MAX_OBSERVATION, MAX_SUBJECT = 600, 60, 450, 50


@lru_cache(maxsize=1)
def copy_rules() -> dict:
    return yaml.safe_load((REPO / "config" / "copy_rules.yaml").read_text(encoding="utf-8"))


def texts(copy: RecapCopy) -> list[tuple[str, str]]:
    """Every string Claude wrote, with where it sits."""
    out = [("overall", copy.overall), ("subject_line", copy.subject_line)]
    for i, o in enumerate(copy.observations):
        out += [(f"observations[{i}].heading", o.heading), (f"observations[{i}].text", o.text)]
    return out


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


def repeated_words(text: str, sheet: FactsSheet) -> list[str]:
    """A word written twice in a row once placeholders are filled, usually because a
    display starts with it: "about {{median_entry_dte}}" reads "about about 7 weeks"."""
    return [f"repeats the word {m.group(1)!r} once the placeholders are filled"
            for m in re.finditer(r"\b([A-Za-z]+)\s+\1\b", filled(text, sheet), re.I)]


def trades_not_named(text: str, underlying: dict[str, str]) -> list[str]:
    """Trade placeholders whose ticker the passage never names. `underlying` maps a
    trade ref ("t3") to its ticker."""
    out = []
    for p in dict.fromkeys(placeholders(text)):
        if not p.startswith("trade."):
            continue
        ref = p.split(".")[1]
        u = underlying.get(ref)
        if u and not re.search(rf"(?<![A-Za-z/]){re.escape(u)}(?![A-Za-z])", text):
            out.append(f"uses {{{{{p}}}}}, a {u} trade, but never names {u}")
    return out


def validate(raw: str | dict | RecapCopy, sheet: FactsSheet,
             trades: list[dict] | None = None) -> tuple[RecapCopy | None, ValidationResult]:
    """`sheet` is the composed sheet (pipeline.compose): month facts, per-trade facts
    and group counts. `trades` is the composed trade list, for check 9."""
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

    r = []
    short = "low_activity" in sheet.flags
    lo, hi = (0, 1) if short else (2, 3)
    if not lo <= len(copy.observations) <= hi:
        r.append(f"{len(copy.observations)} observations (need {lo} to {hi}"
                 + (", a low-activity month)" if short else ")"))
    limits = {"overall": MAX_OVERALL, "subject_line": MAX_SUBJECT}
    for where, t in texts(copy):
        limit = limits.get(where) or (MAX_HEADING if where.endswith("heading") else MAX_OBSERVATION)
        if not t.strip():
            r.append(f"{where} is empty")
        n = len(filled(t, sheet))
        if n > limit:
            r.append(f"{where} is {n} characters once the placeholders are filled (max {limit})")
    checks = [_check("schema_and_lengths", r)]

    # 2. no raw numbers
    r = []
    for where, t in texts(copy):
        found = re.findall(r"\d+(?:[.,]\d+)*%?", PLACEHOLDER.sub("", t))
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
        if (where.endswith("heading") or where == "subject_line") and placeholders(t):
            r.append(f"{where} must not contain placeholders")
        if re.search(r"\{\{(?![a-z0-9_.\s]+\}\})", t) or re.search(r"(?<!\})\}\}", PLACEHOLDER.sub("", t)):
            r.append(f"{where} has a malformed placeholder")
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
        for w in _phrases(t, rules["retired"]):
            r.append(f"{where} uses the retired wording {w!r}; use the trader term (expiration, strike, premium, rolled)")
    checks.append(_check("copy_rules", r))

    # 5. no advice or predictions
    checks.append(_check("no_advice", [f"{where} gives advice or a prediction ({w!r})"
                                       for where, t in texts(copy) for w in _phrases(t, rules["advice"])]))

    # 6. no praise for activity or wins, no consolation cliches
    checks.append(_check("no_praise", [f"{where} praises or consoles ({w!r})" for where, t in texts(copy)
                                       for w in _phrases(t, rules["praise"] + rules["consolation"])]))

    # 7. the month's result stays in the code-written results line
    checks.append(_check("results_left_to_code", [
        f"{where} names the month's result with {{{{{p}}}}}; the results line already does, beside the market move"
        for where, t in texts(copy) for p in placeholders(t) if p in RESULTS_FACTS]))

    # 8. no doubled units, no repeated words
    checks.append(_check("no_doubled_units", [f"{where} {x}" for where, t in texts(copy)
                                              for x in doubled_units(t, sheet) + repeated_words(t, sheet)]))

    # 9. every trade a passage cites is named in it
    underlying = {t["ref"]: t["underlying"] for t in (trades or [])}
    checks.append(_check("trades_named", [f"{where} {x}" for where, t in texts(copy)
                                          for x in trades_not_named(t, underlying)]))

    return copy, ValidationResult(passed=all(c.passed for c in checks), checks=checks)
