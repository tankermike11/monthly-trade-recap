"""Render data/README.md: one page, each persona's month in plain English.

The words come from each persona's YAML `summary`; every number comes from that
month's answer key, so the page cannot drift from the data.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import jinja2

from mock.generator import PERSONA_DIR, load_persona, persona_files

TEMPLATE = """\
# Mock trade data: each customer's {{ month_name }}

Ten made-up customers trading in a simulated market (seed {{ seed }}). Ticker names are real
so the trades read naturally; none of these price moves happened. In this {{ month_name }},
SPY moved {{ spy }} and QQQ moved {{ qqq }}.

Raw files: `raw/{{ month }}/<id>/`. Answer keys the tests must reproduce:
`expected/{{ month }}/<id>.json`. A "trade" is one position from first order to fully
closed, however many orders that took. "Account up/down" is the change in account value
after fees, with open positions at month-end value and deposits or withdrawals taken out.

{% for p in personas -%}
**{{ p.id }} {{ p.name }}, "{{ p.title }}".** {{ p.summary }}
*{{ month_name }}:* {{ p.numbers }}

{% endfor -%}
**November set** (`raw/2026-11/`): all ten continue from their October positions. Clocks
fall back Sunday Nov 1 (p07 trades that evening, p02 before the open Nov 2); markets are
closed Nov 26; Nov 27 closes at 1 pm New York time (p06 trades it); p05's IWM spread
opened Oct 30 closes Nov 3.
"""

MONTH_NAMES = {"10": "October 2026", "11": "November 2026"}


def _money(x) -> str:
    v = Decimal(str(x))
    return f"${abs(v):,.0f}"


def _pct(x) -> str:
    v = float(x) * 100
    return f"{'up' if v >= 0 else 'down'} {abs(v):.1f}%"


def _numbers(key: dict) -> str:
    f = {x["id"]: x["value"] for x in key["headline_facts"]}
    pnl = Decimal(f["month_pnl"])
    parts = [f"{f['stories_touched']} trades on {f['active_days']} days"]
    if f["stories_open_at_end"]:
        parts.append(f"{f['stories_open_at_end']} open at month end")
    if f["rolls_total"]:
        parts.append(f"{f['rolls_total']} roll" + ("" if f["rolls_total"] == 1 else "s"))
    parts.append(f"{f['wins']} gain" + ("" if f['wins'] == 1 else "s") + f", {f['losses']} loss" + ("" if f['losses'] == 1 else "es"))
    parts.append(f"{_money(f['total_fees'])} fees")
    parts.append(("account up " if pnl > 0 else "account down ") + _money(pnl))
    return ", ".join(parts) + "."


def render(root: Path, month: str) -> str:
    personas = []
    seed = None
    spy = qqq = None
    for path in persona_files():
        p = load_persona(path)
        key = json.loads((root / "expected" / month / f"{p['id']}.json").read_text(encoding="utf-8"))
        seed = key["seed"]
        f = {x["id"]: x["value"] for x in key["headline_facts"]}
        spy, qqq = f["spy_move"], f["qqq_move"]
        title = path.read_text(encoding="utf-8").splitlines()[0].split(":", 1)[1].strip()
        personas.append({"id": p["id"], "name": p["first_name"], "title": title,
                         "summary": " ".join(p["summary"].split()), "numbers": _numbers(key)})
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)
    return env.from_string(TEMPLATE).render(
        month=month, month_name=MONTH_NAMES[month[-2:]], seed=seed, personas=personas,
        spy=_pct(spy), qqq=_pct(qqq))


def write_readme(root: Path, month: str) -> Path:
    path = Path(root) / "README.md"
    path.write_bytes(render(Path(root), month).encode("utf-8"))
    return path
