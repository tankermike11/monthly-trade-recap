"""Stage 1: load one customer's raw month from disk and validate it into RawMonth.

Parquet timestamps are stored as naive values that are always UTC (column names end
in _utc); UTC is attached here.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from schemas.raw import (Account, Event, Fill, FillContext, IndexLevel, MarketContext, Order,
                         PositionSnapshot, RawMonth)


def _json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _rows(path: Path) -> list[dict]:
    rows = pl.read_parquet(path).to_dicts()
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime) and v.tzinfo is None:
                r[k] = v.replace(tzinfo=timezone.utc)
    return rows


def load_month(folder: Path) -> RawMonth:
    """Read data/raw/<month>/<customer_id>/ into a validated RawMonth."""
    folder = Path(folder)
    positions = _json(folder / "positions.json")
    market = _json(folder / "market.json")
    return RawMonth(
        account=Account(**_json(folder / "account.json")),
        fills=[Fill(**r) for r in _rows(folder / "fills.parquet")],
        orders=[Order(**r) for r in _rows(folder / "orders.parquet")],
        positions_start=PositionSnapshot(**positions["start"]),
        positions_end=PositionSnapshot(**positions["end"]),
        events=[Event(**e) for e in _json(folder / "events.json")],
        market=MarketContext(fills=[FillContext(**r) for r in _rows(folder / "market_context.parquet")],
                             indexes=[IndexLevel(**i) for i in market["indexes"]]),
    )


def customers(raw_root: Path, month: str) -> list[str]:
    folder = Path(raw_root) / month
    return sorted(p.name for p in folder.iterdir() if p.is_dir()) if folder.exists() else []
