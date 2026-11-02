"""Seeded mock data generator.

    python -m mock.generator --month 2026-10 --seed 42
    python -m mock.generator --month 2026-10 --month 2026-11 --seed 42

The simulation always starts at October 2026 (persona YAMLs describe what was already
open on Oct 1) and runs month by month, so November's carried-in positions are exactly
October's month-end positions. Only the requested months are written.
"""

from __future__ import annotations

import argparse
from decimal import Decimal
from pathlib import Path

import yaml

from mock import answer_key as AK
from mock import io as IO
from mock.behaviors.multi import MultiAsset, Scripted
from mock.behaviors.simple import DayTrader, SteadyInvestor
from mock.behaviors.spreads import SameDayIndex, SpreadTrader
from mock.behaviors.strangle import PremiumSeller
from mock.behaviors.wheel import PutSeller
from mock.ledger import Ledger
from mock.persona import PersonaConfig
from mock.market import calendar as cal
from mock.market.paths import World

PERSONA_DIR = Path(__file__).parent / "personas"
REPO_ROOT = Path(__file__).resolve().parent.parent
FIRST_MONTH = AK.FIRST_MONTH

STYLES = {
    "steady_investor": SteadyInvestor,
    "day_trader": DayTrader,
    "futures_day_trader": DayTrader,
    "wheel": PutSeller,
    "put_seller": PutSeller,
    "premium_seller": PremiumSeller,
    "spreads": SpreadTrader,
    "same_day_index": SameDayIndex,
    "multi_asset": MultiAsset,
    "scripted": Scripted,
}


def persona_files() -> list[Path]:
    return sorted(p for p in PERSONA_DIR.glob("p*.yaml"))


def load_persona(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    PersonaConfig.model_validate(data)
    if data["style"] not in STYLES:
        raise ValueError(f"{path.name}: unknown style {data['style']!r}")
    return data


def months_between(first: str, last: str) -> list[str]:
    out, m = [first], first
    while m < last:
        y, mm = (int(x) for x in m.split("-"))
        m = f"{y + (mm == 12)}-{mm % 12 + 1:02d}"
        out.append(m)
    return out


def simulate_persona(world: World, persona: dict, months: list[str]) -> dict[str, dict]:
    ledger = Ledger(world, persona, Decimal(str(persona["account"]["starting_value"])))
    engine = STYLES[persona["style"]](world, ledger, persona)
    engine.setup()
    results = {}
    for month in months_between(FIRST_MONTH, max(months)):
        ledger.begin_month(month)
        a, b = cal.month_bounds(month)
        for day in cal.calendar_days(a, b):
            # settle yesterday's expirations and today's 04:00 corporate actions first,
            # so the engine sees the account as it stands this morning
            ledger.advance(cal.at_ny(day, 4))
            engine.on_day(day)
        results[month] = ledger.end_month()
    return {m: results[m] for m in months}


def generate(seed: int, months: list[str], out_root: Path, personas: list[str] | None = None) -> list[Path]:
    world = World(seed)
    written = []
    for month in months:
        IO.write_market(out_root, world, month)
    for path in persona_files():
        persona = load_persona(path)
        if personas and persona["id"] not in personas:
            continue
        for month, res in simulate_persona(world, persona, months).items():
            levels = AK.index_levels(world, month)
            written.append(IO.write_raw_month(out_root, res, world, levels))
            written.append(IO.write_answer_key(out_root, AK.build(res, world, seed)))
    if FIRST_MONTH in months and not personas:
        from mock.readme import write_readme
        write_readme(out_root, FIRST_MONTH)
    return written


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--month", action="append", required=True, help="YYYY-MM; repeat for several")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=REPO_ROOT / "data")
    ap.add_argument("--persona", action="append", help="limit to these ids (p01 ...)")
    args = ap.parse_args(argv)
    written = generate(args.seed, sorted(set(args.month)), args.out, args.persona)
    print(f"wrote {len(written)} customer-month outputs under {args.out}")


if __name__ == "__main__":
    main()
