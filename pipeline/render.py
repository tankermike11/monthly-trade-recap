"""Stage 5 (plain text): the recap a customer reads. Code-written parts come from
pipeline/compose.py; Claude's overall picture and observations have their {{fact_id}}
placeholders filled with each fact's display string. An unknown placeholder is a hard
error.

Only this module turns Claude's placeholders into numbers.
"""

from __future__ import annotations

import re
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from pipeline.compose import compose
from pipeline.display import MONTHS
from pipeline.link import LinkResult
from pipeline.validate_copy import PLACEHOLDER
from schemas.facts import FactsSheet
from schemas.recap import RecapCopy

REPO = Path(__file__).resolve().parent.parent

__all__ = ["MONTHS", "UnknownPlaceholder", "fill", "sentence", "render_text"]


class UnknownPlaceholder(KeyError):
    pass


def fill(text: str, sheet: FactsSheet) -> str:
    display = {f.id: f.display for f in sheet.facts}

    def sub(m):
        if m.group(1) not in display:
            raise UnknownPlaceholder(f"{{{{{m.group(1)}}}}} is not in {sheet.customer_id}'s facts sheet")
        return display[m.group(1)]

    return PLACEHOLDER.sub(sub, text)


_MARK = "\x00"


def sentence(text: str, sheet: FactsSheet) -> str:
    """Fill copy and capitalize every sentence that opens with a placeholder, since a
    display string may start lowercase ("all 10 winning trades...")."""
    marked = re.sub(r"(^|[.!?]\s+)(?=\{\{)", lambda m: m.group(1) + _MARK, text)
    out = fill(marked, sheet)
    out = re.sub(_MARK + r"(.)", lambda m: m.group(1).upper(), out)
    return out[:1].upper() + out[1:]


def _env() -> Environment:
    return Environment(loader=FileSystemLoader(REPO / "templates"), undefined=StrictUndefined,
                       keep_trailing_newline=True, trim_blocks=True, lstrip_blocks=True)


def context(copy: RecapCopy, sheet: FactsSheet, linked: LinkResult) -> dict:
    c = compose(sheet, linked)
    f = {x.id: x for x in c.sheet.facts}
    s = lambda t: sentence(t, c.sheet)  # noqa: E731
    tally = None
    if "wins" in f and "losses" in f and "stories_ended" in f:
        tally = (f"Of the {f['stories_ended'].display} that ended, {f['wins'].display.split()[0]} "
                 f"ended with a gain and {f['losses'].display.split()[0]} with a loss.")
    return {
        "first_name": sheet.first_name, "month_name": f"{MONTHS[int(sheet.month[5:7]) - 1]} {sheet.month[:4]}",
        "subject": copy.subject_line, "overall": s(copy.overall),
        "tiles": [{"display": t.display, "plain": t.plain} for t in c.tiles],
        "sections": [(title, c.sections[key]) for key, title in
                     (("getting_in", "Getting in"), ("managing", "Managing"), ("getting_out", "Getting out"))],
        "observations": [{"heading": o.heading.rstrip(". "), "text": s(o.text)} for o in copy.observations],
        "results_line": c.results_line, "what_changed": c.what_changed, "tally": tally,
        "fees": f["total_fees"].display if "total_fees" in f else None,
        "open": f["open_stories"].display if "open_stories" in f else None,
    }


def render_text(copy: RecapCopy, sheet: FactsSheet, linked: LinkResult) -> str:
    text = _env().get_template("recap.txt.j2").render(**context(copy, sheet, linked))
    if PLACEHOLDER.search(text):
        raise UnknownPlaceholder("a placeholder survived rendering")
    return text
