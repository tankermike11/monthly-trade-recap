"""CLI for the pipeline stages built so far (ingest -> link -> facts).

    python -m pipeline.run --month 2026-10 [--customer p04] [--raw data/raw] [--out out]

Writes out/<month>/<customer>/stories.json and facts.json. Each stage's output is a
pydantic model stored as JSON, so later stages can re-run from it alone.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.ingest import customers, load_month
from pipeline.link import LinkResult, link
from pipeline.metrics import build_facts
from pipeline.tradingcal import prev_month
from schemas.facts import FactsSheet

REPO = Path(__file__).resolve().parent.parent


def facts_for(raw_root: Path, month: str, customer: str, with_history: bool = True) -> tuple[LinkResult, FactsSheet]:
    """Facts sheet for one customer and month. Last month's sheet (for 'what changed')
    is rebuilt from last month's raw data when it exists."""
    raw = load_month(raw_root / month / customer)
    linked = link(raw)
    prev_folder = raw_root / prev_month(month) / customer
    previous = None
    if prev_folder.exists() and with_history:
        _, previous = facts_for(raw_root, prev_month(month), customer, with_history=False)
    return linked, build_facts(raw, linked, previous=previous, first_month=not prev_folder.exists())


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def run(month: str, customer: str | None, raw_root: Path, out_root: Path) -> list[Path]:
    written = []
    for cid in ([customer] if customer else customers(raw_root, month)):
        linked, sheet = facts_for(raw_root, month, cid)
        folder = out_root / month / cid
        _write(folder / "stories.json", _dump({
            "stories": [s.model_dump(mode="json") for s in linked.stories],
            "data_quality_issues": [{"issue": d.issue, "refs": list(d.refs)} for d in linked.data_quality_issues],
        }))
        _write(folder / "facts.json", _dump(sheet.model_dump(mode="json")))
        written.append(folder)
    return written


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--month", required=True)
    ap.add_argument("--customer")
    ap.add_argument("--raw", type=Path, default=REPO / "data" / "raw")
    ap.add_argument("--out", type=Path, default=REPO / "out")
    a = ap.parse_args(argv)
    for folder in run(a.month, a.customer, a.raw, a.out):
        print(folder)


if __name__ == "__main__":
    main()
