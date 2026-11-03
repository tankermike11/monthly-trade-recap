"""Demo site: one recap as the site shows it, for a persona (built ahead of time) or an
uploaded workbook (built live). Both go through payload(), so they look the same.

Uploaded workbooks are processed in memory and never stored or logged; log lines
carry counts only (CLAUDE.md, "Demo site").
"""

from __future__ import annotations

from pipeline.link import link
from pipeline.metrics import build_facts
from pipeline.render import MONTHS, render_text
from pipeline.workbook import read_workbook
from pipeline.write import Caller, ModelConfig, cost, write_recap
from schemas.facts import FactsSheet
from schemas.recap import WriteResult

FLAG_WORDS = {"red_month": "losing month", "green_month": "gaining month", "flat_month": "flat month",
              "first_month": "first month", "low_activity": "few trades", "no_options": "no options",
              "no_futures": "no futures", "has_open_positions": "trades still open",
              "data_quality_issue": "data needs a look"}
ISSUE_WORDS = {
    "fees_missing": "Some trades have no commission or fees.",
    "order_partially_filled": "An order was only partly filled.",
    "split_adjusted_positions": "A stock split changed positions without an order.",
    "positions_do_not_reconcile": "Replaying the trades does not reach the month-end positions.",
    "fill_exceeds_position": "A closing trade closed more than was held; the rest was read as a new position.",
    "event_without_position": "An expiration, assignment or dividend names a position the account did not hold.",
}


def month_name(month: str) -> str:
    return f"{MONTHS[int(month[5:7]) - 1]} {month[:4]}"


def payload(sheet: FactsSheet, result: WriteResult, cfg: ModelConfig, assumptions: list[str]) -> dict:
    passed = result.status == "passed"
    return {
        "first_name": sheet.first_name,
        "month": sheet.month,
        "month_name": month_name(sheet.month),
        "flags": [FLAG_WORDS[f] for f in sheet.flags if f in FLAG_WORDS],
        "status": result.status,
        "recap_text": render_text(result.recap, sheet) if passed else None,
        "copy": result.recap.model_dump(mode="json") if passed else None,
        "facts": [{"id": f.id, "section": str(f.section), "display": f.display, "plain": f.plain,
                   "weight": f.weight, "sources": len(f.sources), "source_examples": f.sources[:3]}
                  for f in sheet.facts],
        "assumptions": assumptions,
        "issues": [{"issue": i.issue, "text": ISSUE_WORDS.get(i.issue, i.issue), "count": len(i.refs)}
                   for i in sheet.data_quality_issues],
        "attempts": [{"passed": a.validation.passed, "reasons": a.validation.reasons(),
                      "checks": [c.name for c in a.validation.checks]} for a in result.attempts],
        "draft": None if passed else (result.attempts[-1].raw_output if result.attempts else None),
        "model": result.model,
        "effort": result.effort,
        "prompt_version": result.prompt_version,
        "cost_usd": round(cost(result, cfg), 4),
    }


def run_upload(data: bytes, cfg: ModelConfig | None = None, call: Caller | None = None) -> dict:
    """Workbook bytes -> recap payload. Raises pipeline.workbook.WorkbookError for a bad
    workbook. Calls Claude once (twice if the first draft fails the validator)."""
    imported = read_workbook(data)
    raw = imported.raw
    sheet = build_facts(raw, link(raw), previous=None, first_month=True)
    cfg = cfg or ModelConfig.load()
    result = write_recap(sheet, cfg, call)
    return payload(sheet, result, cfg, imported.assumptions)
