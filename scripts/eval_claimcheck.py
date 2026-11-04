"""Measure the claim check against evals/claims.jsonl.

    python scripts/eval_claimcheck.py claude-haiku-5-5 claude-sonnet-5-5 claude-opus-5-5 [--effort medium]

For each model: bad sentences caught (of the labeled bad ones), good sentences wrongly
flagged, recaps with no bad sentence that would be wrongly held, and cost per recap at
standard and batch prices. Details go to out/eval/claimcheck_<model>_<effort>.json.
Calls the API (a few cents to about a dollar per model).
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline.claimcheck import anthropic_check, check  # noqa: E402
from pipeline.compose import compose  # noqa: E402
from pipeline.run import facts_for  # noqa: E402
from pipeline.write import ModelConfig  # noqa: E402
from schemas.recap import RecapCopy  # noqa: E402


def usage_cost(u: dict, prices: dict) -> float:
    p = prices.get(u.get("model", "")) or {}
    if not p:
        return 0.0
    return (u.get("input_tokens", 0) * p["input"] + u.get("output_tokens", 0) * p["output"]
            + u.get("cache_read_input_tokens", 0) * p.get("cache_read", p["input"])
            + u.get("cache_creation_input_tokens", 0) * p["input"] * 1.25) / 1e6


def run_model(model: str, effort: str, items: list[dict], prices: dict) -> dict:
    call = anthropic_check(model, effort)

    def one(item):
        linked, sheet = facts_for(REPO / "data" / "raw", item["month"], item["customer"])
        c = compose(sheet, linked)
        from scripts.build_claims_eval import _with_old_amounts
        c = c.__class__(_with_old_amounts(c, linked), c.trades, c.sections, c.tiles, c.results_line, c.what_changed)
        try:
            res, reply = check(RecapCopy.model_validate(item["copy"]), c, call)
            return item, reply.verdicts, reply.usage, None
        except Exception as e:  # noqa: BLE001
            return item, {}, {}, f"{type(e).__name__}: {e}"

    with ThreadPoolExecutor(max_workers=6) as pool:
        outs = list(pool.map(one, items))
    caught, missed, false_flags, held_clean, clean, cost, errors, detail = 0, [], [], 0, 0, 0.0, 0, []
    for item, verdicts, usage, err in outs:
        cost += usage_cost(usage, prices)
        errors += err is not None
        has_bad = any(s["label"] == "bad" for s in item["sentences"])
        flagged_any_known = False
        for s in item["sentences"]:
            v = verdicts.get(s["id"], {"verdict": "unsupported", "reason": "not returned"})
            flagged = v["verdict"] != "supported"
            if s["label"] == "bad":
                caught += flagged
                if not flagged:
                    missed.append((item["run"], item["customer"], s["text"]))
            elif s["label"] == "good" and flagged:
                false_flags.append((item["run"], item["customer"], s["text"], v["reason"]))
            flagged_any_known |= flagged and s["label"] != "unknown"
        if not has_bad and all(s["label"] != "unknown" for s in item["sentences"]):
            clean += 1
            held_clean += flagged_any_known
        detail.append({"run": item["run"], "customer": item["customer"], "verdicts": verdicts, "error": err})
    n_bad = sum(s["label"] == "bad" for i in items for s in i["sentences"])
    n_good = sum(s["label"] == "good" for i in items for s in i["sentences"])
    summary = {"model": model, "effort": effort, "bad_caught": caught, "bad_total": n_bad, "missed": missed,
               "good_flagged": len(false_flags), "good_total": n_good, "false_flags": false_flags,
               "clean_recaps_held": held_clean, "clean_recaps": clean, "errors": errors,
               "cost_per_recap": round(cost / len(items), 4), "cost_per_recap_batch": round(cost / len(items) / 2, 4)}
    out = REPO / "out" / "eval" / f"claimcheck_{model}_{effort}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "detail": detail}, indent=1, ensure_ascii=False), encoding="utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("models", nargs="+")
    ap.add_argument("--effort", default="medium")
    a = ap.parse_args()
    items = [json.loads(l) for l in (REPO / "evals" / "claims.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    prices = ModelConfig.load().prices
    for model in a.models:
        s = run_model(model, a.effort, items, prices)
        print(f"{model} ({a.effort}): caught {s['bad_caught']}/{s['bad_total']} bad; flagged {s['good_flagged']}/{s['good_total']} good; "
              f"held {s['clean_recaps_held']}/{s['clean_recaps']} clean recaps; {s['errors']} errors; "
              f"${s['cost_per_recap']:.4f}/recap (${s['cost_per_recap_batch']:.4f} batch)")
        for m in s["missed"]:
            print("   MISSED:", m[0], m[1], "|", m[2][:110])
        for f in s["false_flags"]:
            print("   FALSE FLAG:", f[0], f[1], "|", f[2][:90], "||", f[3][:140])


if __name__ == "__main__":
    main()
