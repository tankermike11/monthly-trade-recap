"""Stage 5 (plain text, pulled forward): fill {{fact_id}} placeholders with each fact's
display string and render the recap. An unknown placeholder is a hard error.

Only this module turns placeholders into numbers.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from pipeline.validate_copy import PLACEHOLDER
from schemas.facts import FactsSheet
from schemas.recap import RecapCopy

REPO = Path(__file__).resolve().parent.parent
MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August",
          "September", "October", "November", "December"]


class UnknownPlaceholder(KeyError):
    pass


def fill(text: str, sheet: FactsSheet) -> str:
    display = {f.id: f.display for f in sheet.facts}

    def sub(m):
        if m.group(1) not in display:
            raise UnknownPlaceholder(f"{{{{{m.group(1)}}}}} is not in {sheet.customer_id}'s facts sheet")
        return display[m.group(1)]

    return PLACEHOLDER.sub(sub, text)


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(REPO / "templates"), undefined=StrictUndefined,
                       keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True)


def context(copy: RecapCopy, sheet: FactsSheet) -> dict:
    facts = {f.id: f for f in sheet.facts}
    f = lambda t: fill(t, sheet)
    tiles = []
    for fid in copy.glance_tiles:
        if fid not in facts:
            raise UnknownPlaceholder(f"glance tile {fid} is not in the facts sheet")
        tiles.append({"display": facts[fid].display, "plain": facts[fid].plain})
    teasers = []
    for t in copy.teasers:
        if t.fact not in facts:
            raise UnknownPlaceholder(f"teaser fact {t.fact} is not in the facts sheet")
        teasers.append({"display": facts[t.fact].display, "line": f(t.line)})
    month_name = f"{MONTHS[int(sheet.month[5:7]) - 1]} {sheet.month[:4]}"
    tally = None
    if "wins" in facts and "losses" in facts and "stories_ended" in facts:
        tally = (f"Of the {facts['stories_ended'].display} that ended, {facts['wins'].display.split()[0]} "
                 f"ended with a gain and {facts['losses'].display.split()[0]} with a loss.")
    return {
        "first_name": sheet.first_name, "month_name": month_name, "short": "low_activity" in sheet.flags,
        "subject": f(copy.subject_line), "one_liner": f(copy.one_liner), "tiles": tiles,
        "sections": [(title, [f(x) for x in getattr(copy.sections, key)]) for key, title in
                     (("getting_in", "Getting in"), ("managing", "Managing"), ("getting_out", "Getting out"))],
        "results_line": f(copy.results_line), "what_changed": f(copy.what_changed), "teasers": teasers,
        "tally": tally, "fees": facts["total_fees"].display if "total_fees" in facts else None,
        "open": facts["open_stories"].display if "open_stories" in facts else None,
    }


def render_text(copy: RecapCopy, sheet: FactsSheet) -> str:
    text = _env().get_template("recap.txt.j2").render(**context(copy, sheet))
    if PLACEHOLDER.search(text):
        raise UnknownPlaceholder("a placeholder survived rendering")
    return text
