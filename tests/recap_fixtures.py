"""A known-good RecapCopy for p09 (Riley, the red month), October 2026, built from real
per-trade fact IDs: t1 is the NKE put carried in from September, t2 the first SBUX put."""

from __future__ import annotations

import copy
import json

GOOD_P09 = {
    "overall": "You sold puts on SBUX and NKE this month and held or closed each trade without rolling it.",
    "observations": [
        {"heading": "Your SBUX puts were bought back early",
         "text": "The SBUX put opened {{trade.t2.opened}} was bought back {{trade.t2.ended}} for a gain of {{trade.t2.result}}."},
        {"heading": "The NKE put became shares",
         "text": "The NKE put from September ended as shares, which you closed {{trade.t1.ended}} for a loss of {{trade.t1.result}}."},
    ],
    "subject_line": "Your October, trade by trade",
}


def good() -> dict:
    return copy.deepcopy(GOOD_P09)


def good_json() -> str:
    return json.dumps(GOOD_P09)
