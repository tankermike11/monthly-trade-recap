"""PROTOTYPE: a different split of work between code and Claude (not wired into the site).

Today's recap asks Claude to restate single facts ("You usually opened trades expiring
{{median_entry_dte}} out"), which code can do. This prototype:

- code writes every single-fact sentence (getting in, managing, getting out), the at a
  glance tiles, the results line and what changed, from fixed templates;
- Claude gets the month's facts plus every trade (ticker, strategy, dates, rolls, outcome,
  each number behind a placeholder) and writes only what code cannot: an overall
  picture and two or three observations about how this customer traded, in the voice
  of the curated written report;
- the same guardrails check Claude's words: no typed numbers, only real facts, trader
  terms without shorthand, no advice, no praise, no doubled units, and the month's
  result is left to the code-written results line.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from pipeline import display as D
from pipeline.link import LinkResult
from pipeline.nytime import ny_date
from pipeline.render import MONTHS
from pipeline.validate_copy import (PLACEHOLDER, RESULTS_FACTS, _phrases, copy_rules, doubled_units, filled,
                                    placeholders)
from pipeline.write import ModelConfig, ModelReply, NO_SERVER_FALLBACK, cost
from schemas.facts import Fact, FactsSheet
from schemas.raw import RawMonth

REPO = Path(__file__).resolve().parent.parent
PROMPT_VERSION = "narrative_proto_v3"
NARRATIVE_BANNED = ["bet", "bets", "betting"]   # fold into config/copy_rules.yaml if this approach is adopted
MAX_OVERALL, MAX_OBSERVATION, MAX_HEADING = 600, 450, 60

STRATEGY = {"stock_long": "shares", "stock_short": "a short stock position", "long_call": "a long call",
            "long_put": "a long put", "short_call": "a short call", "short_put": "a short put",
            "covered_call": "a covered call", "cash_secured_put": "a cash-secured put",
            "credit_vertical": "a credit spread", "debit_vertical": "a debit spread",
            "short_strangle": "a short strangle", "long_strangle": "a long strangle",
            "short_straddle": "a short straddle", "long_straddle": "a long straddle",
            "iron_condor": "an iron condor", "iron_butterfly": "an iron butterfly", "butterfly": "a butterfly",
            "broken_wing_butterfly": "a broken-wing butterfly", "calendar": "a calendar", "diagonal": "a diagonal",
            "future_long": "a long futures position", "future_short": "a short futures position",
            "crypto_long": "crypto", "crypto_short": "a short crypto position", "custom": "a custom position"}
ROLL_WORDS = {"out_in_time": "rolled out to a later expiration", "up": "rolled a strike up",
              "down": "rolled a strike down", "untested_toward_money": "rolled the side not under pressure toward the stock price",
              "tested_away_from_money": "rolled the side under pressure further away",
              "widened": "widened the strikes", "narrowed": "narrowed the strikes"}
STATUS = {"open": "still open at month end", "closed": "closed", "expired": "expired", "assigned": "assigned"}

# Code-written sentences: fact id -> template ({v} is the fact's display). A section shows
# the facts present, in this order, at most three.
SENTENCES = {
    "getting_in": [
        ("median_entry_dte", "You usually opened options trades expiring {v} out."),
        ("median_opening_credit_per_contract", "You usually collected {v} in premium per contract when you opened a trade."),
        ("median_opening_debit_per_contract", "You usually paid a debit of {v} per contract when you opened a trade."),
        ("median_price_level_at_entry", "Implied volatility was usually {v} compared with its past year when you opened a trade."),
    ],
    "managing": [
        ("rolls_total", "You rolled trades {v}."),
        ("credit_roll_share", "{V} of your rolls were done for a credit."),
    ],
    "getting_out": [
        ("median_days_held", "You usually kept a trade for {v}."),
        ("median_days_left_at_close", "When you closed an options trade, it was usually still {v} from expiration."),
        ("median_upfront_kept_on_winners", "Your winning trades usually kept {v} of the premium you collected."),
        ("losers_closed_early", "You closed {v} that ended with a loss before expiration."),
    ],
}
TILE_ORDER = ["stories_opened", "stories_ended", "rolls_total", "credit_roll_share", "median_entry_dte",
              "median_days_held", "median_opening_credit_per_contract", "median_upfront_kept_on_winners",
              "biggest_loss", "active_days", "stories_touched", "stories_open_at_end"]


def _day(d) -> str:
    return f"{MONTHS[d.month - 1][:3]} {d.day}"


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


# ---------------------------------------------------------------------------
# Per-trade facts: every number behind a placeholder, words as plain text
# ---------------------------------------------------------------------------

def trade_facts(sheet: FactsSheet, linked: LinkResult) -> tuple[list[Fact], list[dict]]:
    facts, briefs = [], []
    for i, s in enumerate(sorted(linked.stories, key=lambda s: (s.opened_date, s.story_id)), start=1):
        ref = f"t{i}"
        mine: list[dict] = []

        def add(key: str, value, shown: str, plain: str):
            fid = f"trade.{ref}.{key}"
            facts.append(Fact(id=fid, section="managing", value=str(value), display=shown, plain=plain,
                              weight=0.5, sources=[f"story:{s.story_id}"]))
            mine.append({"id": fid, "display": shown, "plain": plain})

        add("opened", s.opened_date.isoformat(), _day(s.opened_date), "date the trade was opened")
        if s.closed_ts is not None:
            add("ended", ny_date(s.closed_ts).isoformat(), _day(ny_date(s.closed_ts)), "date the trade ended")
        if s.days_held is not None:
            add("held", s.days_held, D.days(s.days_held), "how long the trade was held")
        if s.opening_dte is not None:
            add("expiry_at_open", s.opening_dte, D.days(s.opening_dte), "how far out the expiration was when opened")
        if s.status == "closed" and s.days_left_at_close:
            add("left_at_close", s.days_left_at_close, D.days(s.days_left_at_close),
                "time left before expiration when the trade was bought back")
        if s.upfront_cash:
            add("upfront", s.upfront_cash, D.money(abs(s.upfront_cash)),
                "premium collected when opened" if s.upfront_cash > 0 else "debit paid when opened")
        if s.rolls:
            add("rolls", len(s.rolls), D.count(len(s.rolls), "roll"), "rolls on this trade this month")
            credit = sum(r.credit for r in s.rolls)
            add("credit_rolls", credit, D.count(credit, "roll"), "of those rolls, the ones done for a credit")
            add("first_roll", s.rolls[0].ts_utc.isoformat(), _day(ny_date(s.rolls[0].ts_utc)), "date of the first roll")
        if s.status != "open":
            add("result", s.realized_pnl, D.money(abs(s.realized_pnl)),
                "gain on the trade after fees" if s.realized_pnl >= 0 else "loss on the trade after fees")
        kinds = sorted({str(t) for r in s.rolls for t in r.types})
        if s.status == "closed":
            ended = ("bought back on its expiration day" if s.days_left_at_close == 0 else
                     "bought back before its expiration day" if s.days_left_at_close is not None else "closed")
        else:
            ended = STATUS[str(s.status)]
        briefs.append({"ref": ref, "underlying": s.underlying, "strategy_at_open": STRATEGY.get(str(s.label_open), str(s.label_open)),
                       "strategy_at_end": STRATEGY.get(str(s.label_end), str(s.label_end)),
                       "opened_before_this_month": s.carried_in, "how_it_ended": ended,
                       "outcome": None if s.status == "open" else ("gain" if s.realized_pnl > 0 else "loss" if s.realized_pnl < 0 else "flat"),
                       "roll_kinds": [ROLL_WORDS.get(k, k) for k in kinds], "facts": mine})
    return facts, briefs


def group_facts(linked: LinkResult) -> list[Fact]:
    """Counts across trades, computed by code, so "every", "both" and "most" can come
    from a fact instead of Claude's reading of the trade list. Display: "3 of 5"."""
    stories = linked.stories
    ended = [s for s in stories if s.status != "open"]
    out: list[Fact] = []

    def add(key: str, members: list, pool: list, plain: str):
        if pool:
            out.append(Fact(id=f"group.{key}", section="managing", value=f"{len(members)}/{len(pool)}",
                            display=f"{len(members)} of {len(pool)}", plain=plain, weight=0.5,
                            sources=[f"story:{s.story_id}" for s in pool]))

    for name, pool in (("winning", [s for s in ended if s.realized_pnl > 0]), ("losing", [s for s in ended if s.realized_pnl < 0])):
        opts = [s for s in pool if s.days_left_at_close is not None or s.status in ("expired", "assigned")]
        add(f"{name}.bought_back_before_expiration_day", [s for s in opts if s.status == "closed" and s.days_left_at_close],
            opts, f"{name} options trades bought back before their expiration day, out of all {name} options trades")
        add(f"{name}.bought_back_on_expiration_day", [s for s in opts if s.status == "closed" and s.days_left_at_close == 0],
            opts, f"{name} options trades bought back on their expiration day, out of all {name} options trades")
        add(f"{name}.expired", [s for s in opts if s.status == "expired"], opts,
            f"{name} options trades left to expire, out of all {name} options trades")
        add(f"{name}.assigned", [s for s in opts if s.status == "assigned"], opts,
            f"{name} options trades that were assigned, out of all {name} options trades")
        add(f"{name}.ended_same_day_opened", [s for s in pool if s.days_held == 0], pool,
            f"{name} trades that ended the same day they were opened, out of all {name} trades")
    add("ended.same_day_opened", [s for s in ended if s.days_held == 0], ended,
        "trades that ended the same day they were opened, out of all trades that ended")
    add("ended.next_day", [s for s in ended if s.days_held == 1], ended,
        "trades that ended the day after they were opened, out of all trades that ended")
    add("trades.rolled", [s for s in stories if s.rolls], stories, "trades rolled at least once, out of all trades")
    add("trades.opened_before_month", [s for s in stories if s.carried_in], stories,
        "trades opened before this month, out of all trades")
    add("trades.still_open", [s for s in stories if s.status == "open"], stories,
        "trades still open at month end, out of all trades")
    return out


def with_trades(sheet: FactsSheet, extra: list[Fact]) -> FactsSheet:
    return sheet.model_copy(update={"facts": list(sheet.facts) + extra})


# ---------------------------------------------------------------------------
# Code-written parts
# ---------------------------------------------------------------------------

def code_sections(sheet: FactsSheet) -> dict[str, list[str]]:
    f = {x.id: x.display for x in sheet.facts}
    out = {}
    for sec, rows in SENTENCES.items():
        lines = []
        for fid, tmpl in rows:
            if fid in f and len(lines) < 3:
                v = f[fid]
                if fid == "median_days_held" and v == "the same day":
                    lines.append("You usually closed a trade the same day you opened it.")
                elif fid == "rolls_total" and v == "0 times":
                    lines.append("You held or closed your trades without rolling any of them.")
                elif fid == "losers_closed_early" and v.startswith("0 "):
                    continue
                else:
                    lines.append(tmpl.format(v=v, V=_cap(v)))
        out[sec] = lines
    return out


def code_tiles(sheet: FactsSheet) -> list[Fact]:
    f = {x.id: x for x in sheet.facts}
    tiles, shown = [], set()
    for fid in TILE_ORDER:
        x = f.get(fid)
        if x and len(x.display) <= 32 and ", " not in x.display and x.display not in shown and not x.display.startswith("0 "):
            tiles.append(x)
            shown.add(x.display)
        if len(tiles) == 4:
            break
    return tiles


def code_results_line(sheet: FactsSheet) -> str:
    f = {x.id: x.display for x in sheet.facts}
    result = f.get("month_return") or f.get("month_pnl")
    if not result or "spy_move" not in f:
        return ""
    tail = f" and large technology companies were {f['qqq_move']}" if "qqq_move" in f else ""
    return f"Your account ended the month {result}, while the broad US market was {f['spy_move']}{tail}."


def code_what_changed(sheet: FactsSheet) -> str:
    f = {x.id: x.display for x in sheet.facts}
    if "entry_dte_by_half" in f:
        return f"How far out your expirations were: {f['entry_dte_by_half']}."
    if "rolls_by_half" in f:
        return f"Rolls you made: {f['rolls_by_half']}."
    if "stories_opened_by_half" in f:
        return f"New trades you opened: {f['stories_opened_by_half']}."
    return ""


# ---------------------------------------------------------------------------
# Claude's part
# ---------------------------------------------------------------------------

OUTPUT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["overall", "observations", "subject_line"],
    "properties": {
        "overall": {"type": "string"},
        "observations": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["heading", "text"],
            "properties": {"heading": {"type": "string"}, "text": {"type": "string"}}}},
        "subject_line": {"type": "string"},
    },
}


def system_prompt() -> str:
    return (REPO / "prompts" / f"{PROMPT_VERSION}.md").read_text(encoding="utf-8")


def user_turn(sheet: FactsSheet, briefs: list[dict], code_parts: dict) -> str:
    month_facts = [{"id": f.id, "display": f.display, "plain": f.plain} for f in sheet.facts
                   if not f.id.startswith(("trade.", "group."))]
    counts = [{"id": f.id, "display": f.display, "plain": f.plain} for f in sheet.facts if f.id.startswith("group.")]
    return json.dumps({"first_name": sheet.first_name, "month": sheet.month, "flags": sheet.flags,
                       "month_facts": month_facts, "trade_counts": counts, "trades": briefs,
                       "already_in_the_recap": code_parts}, ensure_ascii=False, sort_keys=True)


def anthropic_call(cfg: ModelConfig, system: str, user: str) -> ModelReply:
    import anthropic

    client = anthropic.Anthropic()
    kwargs = dict(model=cfg.model, max_tokens=cfg.max_tokens,
                  system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                  messages=[{"role": "user", "content": user}],
                  output_config={"effort": cfg.effort, "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}})
    if cfg.fallbacks and not cfg.model.startswith(NO_SERVER_FALLBACK):
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks=cfg.fallbacks, **kwargs)
    else:
        resp = client.messages.create(**kwargs)
    text = next((b.text for b in resp.content if b.type == "text"), "")
    u = resp.usage
    return ModelReply(text, resp.stop_reason, {
        "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
        "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0, "model": resp.model})


def check(copy: dict, sheet: FactsSheet) -> list[str]:
    """The guardrails, applied to Claude's narrative. Returns the problems (empty = pass)."""
    rules = copy_rules()
    known = {f.id for f in sheet.facts}
    strings = [("overall", copy.get("overall", ""), MAX_OVERALL), ("subject_line", copy.get("subject_line", ""), 50)]
    obs = copy.get("observations") or []
    if not 2 <= len(obs) <= 3:
        return [f"observations: need 2 or 3, got {len(obs)}"]
    for i, o in enumerate(obs):
        strings += [(f"observations[{i}].heading", o["heading"], MAX_HEADING), (f"observations[{i}].text", o["text"], MAX_OBSERVATION)]
    r = []
    for where, t, limit in strings:
        if not t.strip():
            r.append(f"{where} is empty")
            continue
        if len(filled(t, sheet)) > limit:
            r.append(f"{where} is {len(filled(t, sheet))} characters once filled (max {limit})")
        if re.findall(r"\d", PLACEHOLDER.sub("", t)):
            r.append(f"{where} types a digit instead of using a placeholder")
        for p in placeholders(t):
            if p not in known:
                r.append(f"{where} uses {{{{{p}}}}}, which is not a fact")
            if p in RESULTS_FACTS:
                r.append(f"{where} names the month's result; the results line already does")
        if (where.endswith("heading") or where == "subject_line") and placeholders(t):
            r.append(f"{where} must not contain placeholders")
        for ch in rules["characters"]["forbidden"]:
            if ch in t:
                r.append(f"{where} contains {ch!r}")
        for kind in ("banned", "jargon", "retired", "advice", "praise", "consolation"):
            for w in _phrases(t, rules[kind]):
                r.append(f"{where} uses {w!r} ({kind})")
        for w in _phrases(t, NARRATIVE_BANNED):
            r.append(f"{where} uses {w!r} (banned)")
        r += [f"{where} {x}" for x in doubled_units(t, sheet)]
        for m in re.finditer(r"\b([A-Za-z]+)\s+\1\b", filled(t, sheet), re.I):
            r.append(f"{where} repeats the word {m.group(1)!r} once the placeholders are filled "
                     f"(a display may already start with it, such as 'about 7 weeks')")
    return r


@dataclass
class NarrativeResult:
    status: str
    copy: dict | None
    attempts: list[dict] = field(default_factory=list)
    cost_usd: float = 0.0
    prompt_sha256: str = ""


def write_narrative(sheet: FactsSheet, briefs: list[dict], code_parts: dict, cfg: ModelConfig, call=None) -> NarrativeResult:
    call = call or anthropic_call
    system = system_prompt()
    user = user_turn(sheet, briefs, code_parts)
    turn, attempts, copy = user, [], None
    from schemas.recap import Attempt, CheckResult, ValidationResult, WriteResult
    for _ in range(2):
        reply = call(cfg, system, turn)
        try:
            copy = json.loads(reply.text)
            problems = check(copy, sheet)
        except (json.JSONDecodeError, KeyError, TypeError) as e:
            copy, problems = None, [f"not valid output: {type(e).__name__}"]
        attempts.append({"raw": reply.text, "problems": problems, "usage": reply.usage})
        if not problems:
            break
        turn = (user + "\n\nYour previous draft failed these checks:\n" + "\n".join(f"- {p}" for p in problems)
                + "\n\nPrevious draft:\n" + reply.text + "\n\nWrite a corrected version that passes every check.")
    passed = not attempts[-1]["problems"]
    fake = WriteResult(customer_id=sheet.customer_id, month=sheet.month, status="passed" if passed else "review", recap=None,
                       attempts=[Attempt(raw_output=a["raw"], validation=ValidationResult(passed=not a["problems"], checks=[
                           CheckResult(name="narrative", passed=not a["problems"], reasons=a["problems"])]),
                           stop_reason=None, usage=a["usage"]) for a in attempts],
                       model=cfg.model, effort=cfg.effort, max_tokens=cfg.max_tokens, prompt_version=PROMPT_VERSION,
                       prompt_sha256=hashlib.sha256(system.encode()).hexdigest())
    return NarrativeResult("passed" if passed else "review", copy if passed else None, attempts,
                           round(cost(fake, cfg), 4), fake.prompt_sha256)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render(sheet: FactsSheet, raw: RawMonth, code_parts: dict, tiles: list[Fact], narrative: dict | None) -> str:
    f = {x.id: x.display for x in sheet.facts}
    fill = lambda t: _cap(filled(t, sheet))  # noqa: E731
    month = f"{MONTHS[int(sheet.month[5:7]) - 1]} {sheet.month[:4]}"
    out = [f"{sheet.first_name}, here is your {month}.", ""]
    if narrative:
        out += ["OVERALL PICTURE", fill(narrative["overall"]), ""]
    out += ["AT A GLANCE"] + [f"- {t.display}: {t.plain}" for t in tiles] + [""]
    for sec, title in (("getting_in", "GETTING IN"), ("managing", "MANAGING"), ("getting_out", "GETTING OUT")):
        if code_parts["sections"][sec]:
            out += [title] + [f"- {x}" for x in code_parts["sections"][sec]] + [""]
    if narrative:
        out += ["WHAT STOOD OUT"]
        for o in narrative["observations"]:
            out += [f"{o['heading'].rstrip('.')}. {fill(o['text'])}", ""]
    out += ["HOW IT TURNED OUT"]
    if code_parts["results_line"]:
        out.append(code_parts["results_line"])
    if {"wins", "losses", "stories_ended"} <= set(f):
        out.append(f"Of the {f['stories_ended']} that ended, {f['wins'].split()[0]} ended with a gain and "
                   f"{f['losses'].split()[0]} with a loss.")
    if "total_fees" in f:
        out.append(f"Fees and commissions: {f['total_fees']}.")
    out.append("")
    if "open_stories" in f:
        out += ["STILL OPEN", f"Going into next month: {f['open_stories']}.", ""]
    if code_parts["what_changed"]:
        out += ["WHAT CHANGED", code_parts["what_changed"], ""]
    return "\n".join(out).rstrip() + "\n"
