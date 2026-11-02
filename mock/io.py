"""Deterministic writers for raw months, answer keys and market files, plus a reader
that loads a raw month back into the validated RawMonth model."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import polars as pl

from mock.market import calendar as cal
from schemas.raw import (Account, Event, FillContext, IndexLevel, MarketContext, PositionSnapshot,
                         RawMonth, Fill, Order)

DEC = pl.Decimal(20, 6)
# Timestamps are stored as naive microsecond datetimes that are always UTC (the column
# names end in _utc). Polars needs the third-party tzdata package on Windows to hand
# back zone-aware values, so readers attach UTC themselves.
TS = pl.Datetime("us")

FILL_SCHEMA = {
    "exec_id": pl.Utf8, "order_id": pl.Utf8, "ts_utc": TS, "session": pl.Utf8,
    "asset_class": pl.Utf8, "symbol": pl.Utf8, "underlying": pl.Utf8, "side": pl.Utf8,
    "open_close": pl.Utf8, "qty": DEC, "price": DEC, "multiplier": pl.Int64,
    "strike": DEC, "expiration": pl.Date, "put_call": pl.Utf8, "contract_month": pl.Utf8,
    "commission": DEC, "fees": DEC, "source": pl.Utf8,
}
ORDER_SCHEMA = {
    "order_id": pl.Utf8, "ts_utc": TS, "leg_exec_ids": pl.List(pl.Utf8), "order_type": pl.Utf8,
    "net_price": DEC, "credit_debit": pl.Utf8, "filled_status": pl.Utf8,
    "requested_units": DEC, "filled_units": DEC,
}
CONTEXT_SCHEMA = {
    "exec_id": pl.Utf8, "underlying_price": DEC, "iv": DEC, "iv_rank": DEC, "dte": pl.Int64,
}


def _jsonable(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat().replace("+00:00", "Z")
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(type(o))


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(obj, sort_keys=True, indent=2, default=_jsonable, ensure_ascii=False) + "\n"
    path.write_bytes(text.encode("utf-8"))


def _naive(v):
    if isinstance(v, datetime) and v.tzinfo is not None:
        if v.utcoffset().total_seconds() != 0:
            raise ValueError(f"non-UTC timestamp {v}")
        return v.replace(tzinfo=None)
    return v


def write_parquet(path: Path, rows: list[dict], schema: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = {k: [_naive(r.get(k)) for r in rows] for k in schema}
    df = pl.DataFrame(cols, schema=schema)
    df.write_parquet(path, compression="zstd", compression_level=3, statistics=True)


def _snapshot_model(snap: dict) -> PositionSnapshot:
    lines = [{k: v for k, v in l.items() if not k.startswith("_")} for l in snap["lines"]]
    return PositionSnapshot(as_of=snap["as_of"], valued_at_utc=snap["valued_at_utc"],
                            cash=snap["cash"].quantize(Decimal("0.0001")), lines=lines)


def write_raw_month(root: Path, res: dict, world, index_levels: list[dict]) -> Path:
    p = res["persona"]
    month = res["month"]
    out = root / "raw" / month / p["id"]
    acct = p["account"]
    account = Account(
        customer_id=p["id"], first_name=p["first_name"], time_zone=p["time_zone"], month=month,
        products={"options_level": int(acct["options_level"]), "futures": bool(acct["futures"]),
                  "crypto": bool(acct["crypto"]), "event_contracts": False},
        start_value=res["start_value"].quantize(Decimal("0.01")),
        end_value=res["end_value"].quantize(Decimal("0.01")),
        cash_flows=res["flows"],
    )
    write_json(out / "account.json", account.model_dump(mode="json"))
    write_parquet(out / "fills.parquet", res["fills"], FILL_SCHEMA)
    write_parquet(out / "orders.parquet", res["orders"], ORDER_SCHEMA)
    write_parquet(out / "market_context.parquet", res["contexts"], CONTEXT_SCHEMA)
    write_json(out / "positions.json", {
        "start": _snapshot_model(res["start_snapshot"]).model_dump(mode="json"),
        "end": _snapshot_model(res["end_snapshot"]).model_dump(mode="json"),
    })
    write_json(out / "events.json", [Event(**e).model_dump(mode="json") for e in res["events"]])
    write_json(out / "market.json", {"indexes": [IndexLevel(**l).model_dump(mode="json") for l in index_levels]})
    return out


def write_answer_key(root: Path, key) -> Path:
    path = root / "expected" / key.month / f"{key.customer_id}.json"
    write_json(path, key.model_dump(mode="json"))
    return path


def write_market(root: Path, world, month: str) -> None:
    a, b = cal.month_bounds(month)
    bars, ivs = [], []
    for sym in sorted(world.symbols):
        if sym not in world._paths:
            continue
        kind = world.kind(sym)
        tick = Decimal(str(world.symbols[sym].get("tick", "0.01")))
        for d in world.days(sym):
            if a <= d <= b:
                bars.append(world.bar(sym, d, tick))
                if world.has_iv(sym) and cal.is_trading_day(d):
                    ivs.append({"symbol": sym, "date": d,
                                "iv": Decimal(repr(world.iv(sym, d))).quantize(Decimal("0.000001")),
                                "iv_rank": Decimal(repr(world.iv_rank(sym, d))).quantize(Decimal("0.1"))})
    write_parquet(root / "market" / month / "bars.parquet", bars, {
        "symbol": pl.Utf8, "date": pl.Date, "open": DEC, "high": DEC, "low": DEC, "close": DEC,
        "session_start_utc": TS, "session_end_utc": TS})
    write_parquet(root / "market" / month / "iv.parquet", ivs, {
        "symbol": pl.Utf8, "date": pl.Date, "iv": DEC, "iv_rank": DEC})


# ---------------------------------------------------------------------------
# Reader
# ---------------------------------------------------------------------------

def _rows(path: Path) -> list[dict]:
    rows = pl.read_parquet(path).to_dicts()
    for r in rows:
        for k, v in r.items():
            if isinstance(v, datetime):
                r[k] = v.replace(tzinfo=timezone.utc)
    return rows


def read_raw_month(folder: Path) -> RawMonth:
    folder = Path(folder)
    account = json.loads((folder / "account.json").read_text(encoding="utf-8"))
    positions = json.loads((folder / "positions.json").read_text(encoding="utf-8"))
    events = json.loads((folder / "events.json").read_text(encoding="utf-8"))
    market = json.loads((folder / "market.json").read_text(encoding="utf-8"))
    return RawMonth(
        account=Account(**account),
        fills=[Fill(**r) for r in _rows(folder / "fills.parquet")],
        orders=[Order(**r) for r in _rows(folder / "orders.parquet")],
        positions_start=PositionSnapshot(**positions["start"]),
        positions_end=PositionSnapshot(**positions["end"]),
        events=[Event(**e) for e in events],
        market=MarketContext(fills=[FillContext(**r) for r in _rows(folder / "market_context.parquet")],
                             indexes=[IndexLevel(**l) for l in market["indexes"]]),
    )
