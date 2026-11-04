"""CLI for the pipeline stages built so far (ingest -> link -> facts -> write -> render).

    python -m pipeline.run --month 2026-10 [--customer p04] [--raw data/raw] [--out out]
                           [--write [--model claude-sonnet-5-5] [--effort low]]

Writes out/<month>/<customer>/stories.json and facts.json; with --write also calls
Claude (ANTHROPIC_API_KEY) and writes recap.json and recap.txt, or
out/<month>/review/<customer>.json when the copy fails validation twice. Each stage's
output is a pydantic model stored as JSON, so later stages can re-run from it alone.
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


def write_stage(sheet: FactsSheet, linked: LinkResult, folder: Path, review_dir: Path, cfg, call=None):
    from pipeline.render import render_text
    from pipeline.write import write_recap

    result = write_recap(sheet, linked, cfg, call=call)
    if result.status == "passed":
        _write(folder / "recap.json", _dump(result.model_dump(mode="json")))
        _write(folder / "recap.txt", render_text(result.recap, sheet, linked))
    else:
        _write(review_dir / f"{sheet.customer_id}.json", _dump(result.model_dump(mode="json")))
    return result


def run(month: str, customer: str | None, raw_root: Path, out_root: Path, cfg=None, call=None) -> list[Path]:
    written = []
    for cid in ([customer] if customer else customers(raw_root, month)):
        linked, sheet = facts_for(raw_root, month, cid)
        folder = out_root / month / cid
        _write(folder / "stories.json", _dump({
            "stories": [s.model_dump(mode="json") for s in linked.stories],
            "data_quality_issues": [{"issue": d.issue, "refs": list(d.refs)} for d in linked.data_quality_issues],
        }))
        _write(folder / "facts.json", _dump(sheet.model_dump(mode="json")))
        if cfg is not None:
            res = write_stage(sheet, linked, folder, out_root / month / "review", cfg, call)
            print(f"{cid}: {res.status} after {len(res.attempts)} attempt(s)")
        written.append(folder)
    return written


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--month", required=True)
    ap.add_argument("--customer")
    ap.add_argument("--raw", type=Path, default=REPO / "data" / "raw")
    ap.add_argument("--out", type=Path, default=REPO / "out")
    ap.add_argument("--write", action="store_true", help="call Claude and render the recap")
    ap.add_argument("--model")
    ap.add_argument("--effort")
    ap.add_argument("--gallery", action="store_true",
                    help="build out/<month>/gallery.html from this month's recaps (no API calls)")
    ap.add_argument("--results", type=Path,
                    help="with --gallery: read recaps from a saved comparison file instead of out/<month>/")
    ap.add_argument("--batch", action="store_true",
                    help="the monthly run: every customer through the Batch API, archived (pipeline/monthly.py)")
    ap.add_argument("--archive", type=Path, default=REPO / "archive")
    ap.add_argument("--resume", help="with --batch: continue an earlier run, e.g. run-0003")
    ap.add_argument("--no-wait", action="store_true", help="with --batch: submit, then stop; resume later")
    ap.add_argument("--sample-rate", type=float, default=0.05, help="with --batch: share of ready recaps to sample for review")
    a = ap.parse_args(argv)
    if a.batch:
        from pipeline.monthly import describe, run_month
        from pipeline.write import ModelConfig

        summary = run_month(a.month, a.raw, a.archive, ModelConfig.load(model=a.model, effort=a.effort),
                            resume=a.resume, no_wait=a.no_wait, sample_rate=a.sample_rate,
                            only=[a.customer] if a.customer else None)
        if summary:
            print(describe(summary))
        return
    if a.gallery and not a.write:
        from pipeline.gallery import build, load_results

        path = a.out / a.month / "gallery.html"
        _write(path, build(a.month, a.raw, load_results(a.out, a.month, a.results)))
        print(path)
        return
    cfg = None
    if a.write:
        from pipeline.write import ModelConfig
        cfg = ModelConfig.load(model=a.model, effort=a.effort)
    for folder in run(a.month, a.customer, a.raw, a.out, cfg=cfg):
        print(folder)


if __name__ == "__main__":
    main()
