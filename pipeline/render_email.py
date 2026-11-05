"""Stage 5c: the HTML email (SPEC 5.1), from the October Trader Recap design: dark, yellow
accent, 640 px wide, email-safe (tables, inline styles, no web fonts needed). The only image
is the Lossdog logo (templates/lossdog_logo.png, transparent background), embedded as a data
URI by default; a send step can pass `logo_src` with a hosted URL instead.

Same content as the plain-text recap: Claude's overall picture and observations (filled
from the facts), and everything code writes (tiles, getting in / managing / getting out,
the results line, still open, what changed). The charts are drawn by code from the
facts, as table cells: what you traded (asset mix), adjusted against closed, rolls done
for a credit, wins and losses, and new trades in each half of the month.

One change from the design: no big net-result number. The month's result is named once,
in the code-written results line beside the market move (the red-month rule).
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from pipeline.compose import compose
from pipeline.display import MONTHS
from pipeline.link import LinkResult
from pipeline.render import UnknownPlaceholder, sentence
from pipeline.validate_copy import PLACEHOLDER
from schemas.facts import FactsSheet
from schemas.recap import RecapCopy

REPO = Path(__file__).resolve().parent.parent
MAX_DOTS = 80
ASSET_NAMES = {"stock_etf": "Stocks and funds", "equity_option": "Options on stocks and funds",
               "index_option": "Index options", "future": "Futures", "future_option": "Options on futures",
               "crypto": "Crypto", "event_contract": "Event contracts"}
ASSET_SHADES = ["#FFC800", "#B8901C", "#7A6A2E", "#5A5A60", "#3E3E44", "#2A2A2E", "#202024"]
LOGO = REPO / "templates" / "lossdog_logo.png"
DEFAULT_LINKS = {"trades": "[TRADES_URL]", "unsubscribe": "[UNSUBSCRIBE_URL]", "settings": "[EMAIL_SETTINGS_URL]"}


def logo_data_uri() -> str:
    return "data:image/png;base64," + base64.b64encode(LOGO.read_bytes()).decode("ascii")


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(REPO / "templates"), autoescape=select_autoescape(["html", "j2"]),
                       trim_blocks=True, lstrip_blocks=True)


def _pct(parts: list[float]) -> list[int]:
    """Whole percentages that add up to 100 (largest remainder), for bar widths."""
    total = sum(parts) or 1
    raw = [p / total * 100 for p in parts]
    out = [int(r) for r in raw]
    for i in sorted(range(len(raw)), key=lambda i: raw[i] - out[i], reverse=True)[:100 - sum(out)]:
        out[i] += 1
    return out


def asset_bar(facts: dict) -> list[dict]:
    mix = facts["asset_mix"].value if "asset_mix" in facts else {}
    items = sorted(mix.items(), key=lambda kv: -kv[1])
    widths = _pct([v for _, v in items])
    return [{"name": ASSET_NAMES.get(k, k), "pct": round(v * 100), "width": w, "color": ASSET_SHADES[min(i, len(ASSET_SHADES) - 1)]}
            for i, ((k, v), w) in enumerate(zip(items, widths))]


def adjusted_closed(facts: dict, linked: LinkResult) -> dict | None:
    """The counts behind adjust_to_close_ratio: rolls plus adjustments, against closes."""
    if "adjust_to_close_ratio" not in facts:
        return None
    rolls = int(facts["rolls_total"].value) if "rolls_total" in facts else 0
    adjusts = sum(1 for s in linked.stories for o in s.orders if str(o.effect) == "adjust")
    closes = sum(1 for s in linked.stories for o in s.orders if str(o.effect) == "close")
    adjusted = rolls + adjusts
    if adjusted == 0 and closes == 0:
        return None
    top = max(adjusted, closes)
    return {"adjusted": adjusted, "closed": closes,
            "adjusted_width": max(2, round(adjusted / top * 100)) if adjusted else 0,
            "closed_width": max(2, round(closes / top * 100)) if closes else 0}


def credit_dots(facts: dict) -> dict | None:
    f = facts.get("credit_roll_share")
    if f is None:
        return None
    m = re.match(r"(\d+) in (\d+)", f.display)
    filled, total = (int(m.group(1)), int(m.group(2))) if m else ((5, 5) if f.display in ("all", "nearly all") else (0, 5))
    return {"filled": filled, "empty": total - filled, "display": f.display}


def result_dots(facts: dict) -> dict | None:
    wins = int(facts["wins"].value) if "wins" in facts else 0
    losses = int(facts["losses"].value) if "losses" in facts else 0
    if wins + losses == 0:
        return None
    per = max(1, -(-(wins + losses) // MAX_DOTS))           # trades per dot, when there are many
    return {"wins": wins, "losses": losses, "win_dots": -(-wins // per), "loss_dots": -(-losses // per), "per": per}


def halves(facts: dict) -> dict | None:
    f = facts.get("stories_opened_by_half")
    if f is None or not isinstance(f.value, dict):
        return None
    first, second = int(f.value.get("first", 0)), int(f.value.get("second", 0))
    if first + second == 0:
        return None
    w = _pct([first, second])
    return {"first": first, "second": second, "first_width": w[0], "second_width": w[1]}


def big_number(facts: dict, ids: list[str]) -> dict | None:
    for fid in ids:
        if fid in facts and not facts[fid].display.startswith("0 "):
            return {"display": facts[fid].display, "plain": facts[fid].plain}
    return None


def _without(lines: list[str], *shown: str | None) -> list[str]:
    """Drop the line that repeats a value the card already shows as a big number or chart."""
    keys = [s for s in shown if s]
    return [ln for ln in lines if not any(k in ln for k in keys)]


def context(copy: RecapCopy, sheet: FactsSheet, linked: LinkResult, links: dict | None = None,
            logo_src: str | None = None) -> dict:
    c = compose(sheet, linked)
    facts = {f.id: f for f in c.sheet.facts}
    s = lambda t: sentence(t, c.sheet)  # noqa: E731
    month = MONTHS[int(sheet.month[5:7]) - 1]
    next_month = MONTHS[int(sheet.month[5:7]) % 12]
    tally = None
    if {"wins", "losses", "stories_ended"} <= set(facts) and int(facts["stories_ended"].value):
        tally = (f"Of the {facts['stories_ended'].display} that ended, {facts['wins'].display.split()[0]} ended with a gain "
                 f"and {facts['losses'].display.split()[0]} with a loss.")
    open_list = facts["open_stories"].value if "open_stories" in facts else []
    overall = s(copy.overall)
    gin = big_number(facts, ["median_opening_credit_per_contract", "median_opening_debit_per_contract", "median_entry_dte"])
    gout = big_number(facts, ["median_days_held", "median_days_left_at_close"])
    bars, credit = adjusted_closed(facts, linked), credit_dots(facts)
    return {
        "first_name": sheet.first_name, "month": month, "year": sheet.month[:4], "next_month": next_month,
        "subject": copy.subject_line, "preheader": re.split(r"(?<=[.!?])\s", overall)[0],
        "overall": overall, "short": "low_activity" in sheet.flags,
        "tiles": [{"display": t.display, "plain": t.plain} for t in c.tiles],
        "asset_bar": asset_bar(facts),
        "getting_in": {"big": gin, "lines": _without(c.sections["getting_in"], gin and gin["display"])},
        "managing": {"bars": bars, "credit": credit,
                     "lines": _without(c.sections["managing"], credit and f"{credit['display'].capitalize()} of your rolls",
                                       bars and facts["rolls_total"].display if bars and "rolls_total" in facts else None)},
        "getting_out": {"big": gout, "lines": _without(c.sections["getting_out"], gout and gout["display"])},
        "observations": [{"heading": o.heading.rstrip(". "), "text": s(o.text)} for o in copy.observations],
        "results_line": c.results_line, "tally": tally, "dots": result_dots(facts),
        "fees": facts["total_fees"].display if "total_fees" in facts else None,
        "open": [o["underlying"] for o in open_list] if isinstance(open_list, list) else [],
        "what_changed": c.what_changed, "halves": halves(facts),
        "links": {**DEFAULT_LINKS, **(links or {})},
        "logo_src": logo_src or logo_data_uri(),
    }


def render_html(copy: RecapCopy, sheet: FactsSheet, linked: LinkResult, links: dict | None = None,
                logo_src: str | None = None) -> str:
    html = _env().get_template("recap.html.j2").render(**context(copy, sheet, linked, links, logo_src))
    if PLACEHOLDER.search(html):
        raise UnknownPlaceholder("a placeholder survived rendering")
    return html
