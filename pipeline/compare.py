"""SPEC 4.4 model comparison: run every persona through each model and report first-try
pass rate, retries, review count and cost. Spends real money (cents per model).

    python -m pipeline.compare --month 2026-10 --model claude-haiku-5-5 --model claude-sonnet-5-5

Writes out/compare/<month>/<model>.json (every WriteResult) and recap text for blind
review, and prints a summary table.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from pipeline.ingest import customers
from pipeline.render import render_text
from pipeline.run import REPO, facts_for
from pipeline.write import ModelConfig, cost, write_recap


def compare(month: str, models: list[str], raw_root: Path, out_root: Path, effort: str | None = None) -> list[dict]:
    rows = []
    sheets = {cid: facts_for(raw_root, month, cid) for cid in customers(raw_root, month)}
    for model in models:
        cfg = ModelConfig.load(model=model, effort=effort)
        results, spent = [], 0.0
        for cid, (linked, sheet) in sheets.items():
            res = write_recap(sheet, linked, cfg)
            spent += cost(res, cfg)
            results.append(res)
            folder = out_root / month / model
            folder.mkdir(parents=True, exist_ok=True)
            if res.status == "passed":
                (folder / f"{cid}.txt").write_text(render_text(res.recap, sheet, linked), encoding="utf-8")
        (out_root / month / f"{model}.json").write_text(
            json.dumps([r.model_dump(mode="json") for r in results], indent=2, sort_keys=True), encoding="utf-8")
        rows.append({
            "model": model, "effort": cfg.effort, "customers": len(results),
            "first_try": sum(1 for r in results if r.status == "passed" and len(r.attempts) == 1),
            "after_retry": sum(1 for r in results if r.status == "passed" and len(r.attempts) == 2),
            "review": [r.customer_id for r in results if r.status == "review"],
            "cost_usd": round(spent, 4),
        })
    return rows


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Compare models on every persona (SPEC 4.4)")
    ap.add_argument("--month", default="2026-10")
    ap.add_argument("--model", action="append", required=True)
    ap.add_argument("--effort")
    ap.add_argument("--raw", type=Path, default=REPO / "data" / "raw")
    ap.add_argument("--out", type=Path, default=REPO / "out" / "compare")
    a = ap.parse_args(argv)
    rows = compare(a.month, a.model, a.raw, a.out, a.effort)
    print(f"{'model':22} {'first try':>10} {'after retry':>12} {'review':>8} {'cost':>9}")
    for r in rows:
        print(f"{r['model']:22} {r['first_try']:>7}/{r['customers']:<2} {r['after_retry']:>12} "
              f"{len(r['review']):>8} ${r['cost_usd']:>8.4f}  {' '.join(r['review'])}")


if __name__ == "__main__":
    main()
