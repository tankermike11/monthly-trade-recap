"""PROTOTYPE runner: the narrative split (pipeline/narrative.py) for a few personas.

    python scripts/prototype_narrative.py p04 p06 p09 [--dry-run]

Writes out/prototype/<pid>.txt (the new recap), <pid>.json (everything, including
Claude's attempts and cost) and prints a summary. --dry-run uses a canned narrative
instead of calling Claude, to check the code-written parts for free.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline import narrative as N  # noqa: E402
from pipeline.ingest import load_month  # noqa: E402
from pipeline.run import facts_for  # noqa: E402
from pipeline.write import ModelConfig, ModelReply  # noqa: E402

MONTH = "2026-10"


def canned(cfg, system, user):
    return ModelReply(json.dumps({"overall": "You traded options this month.",
                                  "observations": [{"heading": "One", "text": "You traded."},
                                                   {"heading": "Two", "text": "You traded again."}],
                                  "subject_line": "Your October"}), "end_turn", {})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("personas", nargs="+")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    cfg = ModelConfig.load()
    out = REPO / "out" / "prototype"
    out.mkdir(parents=True, exist_ok=True)
    total = 0.0
    for pid in a.personas:
        raw = load_month(REPO / "data" / "raw" / MONTH / pid)
        linked, sheet = facts_for(REPO / "data" / "raw", MONTH, pid)
        extra, briefs = N.trade_facts(sheet, linked)
        full = N.with_trades(sheet, extra)
        tiles = N.code_tiles(sheet)
        parts = {"sections": N.code_sections(sheet), "results_line": N.code_results_line(sheet),
                 "what_changed": N.code_what_changed(sheet),
                 "tiles": [f"{t.display}: {t.plain}" for t in tiles]}
        res = N.write_narrative(full, briefs, parts, cfg, call=canned if a.dry_run else None)
        text = N.render(full, raw, parts, tiles, res.copy)
        (out / f"{pid}.txt").write_text(text, encoding="utf-8")
        (out / f"{pid}.json").write_text(json.dumps({"status": res.status, "narrative": res.copy, "code_parts": parts,
                                                     "attempts": res.attempts, "cost_usd": res.cost_usd,
                                                     "prompt_version": N.PROMPT_VERSION, "model": cfg.model,
                                                     "trades": briefs}, indent=1, ensure_ascii=False, default=str),
                                         encoding="utf-8")
        total += res.cost_usd
        print(f"{pid} {sheet.first_name}: {res.status}, {len(res.attempts)} attempt(s), ${res.cost_usd:.4f}, "
              f"{len(briefs)} trades, {len(extra)} trade facts")
        for i, at in enumerate(res.attempts):
            for p in at["problems"]:
                print(f"    attempt {i + 1}: {p}")
    print(f"total ${total:.4f}")


if __name__ == "__main__":
    main()
