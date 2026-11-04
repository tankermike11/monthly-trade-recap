"""Build evals/claims.jsonl: recaps from the fact-checked runs, every sentence labeled.

    python scripts/build_claims_eval.py

Labels come from the manual fact checks of 2026-11-04 (every claim compared with the
trades): "bad" (false, overstated or not in the data), "good", or "unknown" (not fully
verified, or ambiguous; left out of scoring). Each line is self-contained: the month,
the customer and Claude's RecapCopy; the facts and trades are rebuilt from data/raw.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from pipeline.claimcheck import sentences  # noqa: E402
from pipeline.compose import compose  # noqa: E402
from pipeline.run import facts_for  # noqa: E402
from pipeline.validate_copy import filled  # noqa: E402
from schemas.recap import RecapCopy, WriteResult  # noqa: E402

RUNS = [  # (run name, month, source)
    ("oct-v4", "2026-10", "out/compare_v4/2026-10/claude-opus-5-5.json"),
    ("oct-v4b", "2026-10", "out/compare_v4b/2026-10/claude-opus-5-5.json"),
    ("oct-v4b-p07-rerun", "2026-10", "out/compare_v4b/2026-10/p07_rerun.json"),
    ("oct-v4c", "2026-10", "out/compare_v4c/2026-10/claude-opus-5-5.json"),
    ("nov-batch", "2026-11", "archive/2026-11/run-0001/customers"),
]
# (run, customer) -> substrings of sentences that are false, overstated or not in the data
BAD = {
    ("oct-v4", "p06"): ["In your first month", "Winners were taken late in the day"],
    ("oct-v4", "p07"): ["On Oct 8 alone you closed two short /MNQ positions"],
    ("oct-v4b", "p07"): ["Your trades from Oct 1 through Oct 5 were all in /MNQ"],
    ("oct-v4b", "p09"): ["SBUX puts came off well ahead of expiration"],
    ("oct-v4b", "p04"): ["Winners were taken off with most of the time left"],
    ("oct-v4b-p07-rerun", "p07"): ["Your trades from Oct 1 through Oct 5 were all on /MNQ"],
    ("oct-v4c", "p09"): ["SBUX puts were bought back well before expiration",
                         "you bought it back for $308"],                # $308 was the gain, not the price paid
                                                                        # (missed by hand, caught by the checker)
    ("nov-batch", "p06"): ["Nov 11, Nov 12 and Nov 17"],
    ("nov-batch", "p09"): ["The replacement puts went further out"],
    ("nov-batch", "p10"): ["which cost $7,344 when opened"],
}
# not fully verified or ambiguous: left out of scoring
UNKNOWN = {
    ("oct-v4", "p06"): ["so 15 of 23 trades were finished", "Wins and losses both came from"],
    ("oct-v4b-p07-rerun", "p07"): ["*"],                      # only the bad sentence was checked
    ("nov-batch", "p06"): ["*"],
    ("nov-batch", "p07"): ["*"],
    ("nov-batch", "p02"): ["*"],
    ("nov-batch", "p08"): ["*"],
}


def _with_old_amounts(c, linked):
    """The sheet the recap was written against: older runs cite carried-in trades'
    amounts, which compose no longer provides; add them back so the text fills."""
    from pipeline import display as D
    from schemas.facts import Fact
    shown = {f.id for f in c.sheet.facts}
    extra = []
    for t in c.trades:
        st = next(s for s in linked.stories if f"story:{s.story_id}" in
                  next(f.sources for f in c.sheet.facts if f.id == f"trade.{t['ref']}.opened"))
        fid = f"trade.{t['ref']}.upfront"
        if st.carried_in and st.upfront_cash and fid not in shown:
            extra.append(Fact(id=fid, section="managing", value=str(st.upfront_cash), display=D.money(abs(st.upfront_cash)),
                              plain="recorded cost at the start of this month", weight=0.5, sources=[f"story:{st.story_id}"]))
    return c.sheet.model_copy(update={"facts": list(c.sheet.facts) + extra})


def load(source: str) -> list[WriteResult]:
    p = REPO / source
    if p.is_dir():
        return [WriteResult.model_validate(json.loads(f.read_text(encoding="utf-8"))) for f in sorted(p.glob("*/recap.json"))]
    d = json.loads(p.read_text(encoding="utf-8"))
    return [WriteResult.model_validate(x) for x in (d if isinstance(d, list) else [d])]


def main() -> None:
    lines, n_bad, n_good, n_unknown, found = [], 0, 0, 0, set()
    for run, month, source in RUNS:
        for res in load(source):
            if res.status != "passed":
                continue
            cid = res.customer_id
            linked, sheet = facts_for(REPO / "data" / "raw", month, cid)
            c = compose(sheet, linked)
            # amounts of trades carried in from an earlier month: their recorded cost cannot
            # confirm or deny "what it cost when opened" (left out of scoring)
            from pipeline import display as D
            carried = {D.money(abs(st.upfront_cash)) for st in linked.stories if st.carried_in and st.upfront_cash}
            labels = []
            for s in sentences(res.recap):
                text = filled(s["text"], _with_old_amounts(c, linked))
                bad = [b for b in BAD.get((run, cid), []) if b in text]
                unknown = UNKNOWN.get((run, cid), [])
                label = ("bad" if bad else "unknown" if ("*" in unknown or any(u in text for u in unknown)
                                                         or any(a in text for a in carried)) else "good")
                found |= {(run, cid, b) for b in bad}
                labels.append({"id": s["id"], "text": text, "label": label})
                n_bad += label == "bad"
                n_good += label == "good"
                n_unknown += label == "unknown"
            lines.append({"run": run, "month": month, "customer": cid, "copy": res.recap.model_dump(mode="json"),
                          "sentences": labels})
    missing = {(r, c, b) for (r, c), bs in BAD.items() for b in bs} - found
    assert not missing, f"bad sentences not found: {missing}"
    out = REPO / "evals" / "claims.jsonl"
    out.parent.mkdir(exist_ok=True)
    out.write_bytes(("\n".join(json.dumps(x, ensure_ascii=False, sort_keys=True) for x in lines) + "\n").encode("utf-8"))
    print(f"{len(lines)} recaps; {n_bad} bad, {n_good} good, {n_unknown} unknown sentences -> {out}")


if __name__ == "__main__":
    main()
