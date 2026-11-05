"""Stage 4a: everything code writes in a recap, and the per-trade facts Claude writes from.

Code writes every single-fact sentence (getting in, managing, getting out), picks the
at-a-glance tiles, and writes the results line (the month's result beside the market
move, so a losing month is named once, next to the market, by construction) and what
changed. Claude (pipeline/write.py) writes only the overall picture and the
observations, from the month's facts plus every trade.

Per-trade facts ("trade.t3.opened") and group counts ("group.losing.expired") join the
facts sheet for writing and rendering, so every number Claude places still comes from
code through a placeholder. Group counts let words like "every" or "both" rest on a
count instead of Claude's reading of the trade list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pipeline import display as D
from pipeline.link import LinkResult
from pipeline.nytime import ny_date
from schemas.facts import Fact, FactsSheet

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
    return f"{D.MONTHS[d.month - 1][:3]} {d.day}"


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

        if s.story_id not in linked.unknown_open_dates:      # an upload's stand-in date is not a fact
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
        # Only for trades opened this month: a carried-in trade's value is its cost as
        # recorded at the start of the month, which is not what it cost when opened once it
        # was added to or adjusted ("SHOP ... cost $7,344 when opened"; it cost $14,775), and
        # neither the writer nor the claim check can tell which.
        if s.upfront_cash and not s.carried_in:
            add("upfront", s.upfront_cash, D.money(abs(s.upfront_cash)),
                "premium collected when opened, plus any added later" if s.upfront_cash > 0
                else "debit paid when opened, plus any added later")
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
    from a fact instead of Claude's reading of the trade list. Display: "3 of 5",
    "all 10", "none of the 4", "both", "neither of the 2"."""
    stories = linked.stories
    ended = [s for s in stories if s.status != "open"]
    out: list[Fact] = []

    def add(key: str, members: list, pool: list, plain: str):
        if pool:
            k, n = len(members), len(pool)
            shown = ("both" if k == n == 2 else "neither of the 2" if k == 0 and n == 2 else
                     f"all {n}" if k == n and n > 1 else f"none of the {n}" if k == 0 else f"{k} of {n}")
            out.append(Fact(id=f"group.{key}", section="managing", value=f"{k}/{n}",
                            display=shown, plain=plain, weight=0.5,
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


@dataclass(frozen=True)
class Composed:
    sheet: FactsSheet            # the month's facts plus per-trade facts and group counts
    trades: list[dict]           # one brief per trade, for the model
    sections: dict[str, list[str]]
    tiles: list[Fact]
    results_line: str
    what_changed: str
    story_ids: dict[str, str] = field(default_factory=dict)   # trade ref ("t3") -> story_id, for the gallery

    def code_written(self) -> dict:
        """What code already wrote, shown to the model so it does not repeat it."""
        return {"sections": self.sections, "tiles": [f"{t.display}: {t.plain}" for t in self.tiles],
                "results_line": self.results_line, "what_changed": self.what_changed}


def compose(sheet: FactsSheet, linked: LinkResult) -> Composed:
    extra, briefs = trade_facts(sheet, linked)
    full = with_trades(sheet, extra + group_facts(linked))
    short = "low_activity" in sheet.flags
    sections = {k: [] for k in SENTENCES} if short else code_sections(sheet)
    refs = {f"t{i}": s.story_id for i, s in
            enumerate(sorted(linked.stories, key=lambda s: (s.opened_date, s.story_id)), start=1)}   # as trade_facts numbers them
    return Composed(full, briefs, sections, code_tiles(sheet)[:3] if short else code_tiles(sheet),
                    code_results_line(sheet), code_what_changed(sheet), refs)
