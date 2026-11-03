"""Demo site: an Excel workbook <-> one customer's raw month (RawMonth).

Two sheets are required: Account (field and value pairs) and Trades (one row per
fill). Orders, Positions, Events, Cash flows and Market are optional. Whatever a
workbook leaves out gets a stated default, and every default used is returned as an
assumption so the site can show it beside the recap:

- no Order ID: each fill is its own order; no Orders sheet: order details come from
  the fills (limit order, fully filled, net price from the legs);
- no Cash at start and no start positions: the account held only cash at the start;
- no Cash at end and no end positions: month-end holdings come from replaying the
  trades with the pipeline's own linker, marked at each instrument's last price;
- no Events sheet: options still held after their expiration expired worthless;
- no Market sheet: the built-in index levels for the month (config/index_levels.yaml).

Persona workbooks fill in every sheet and column and read back to exactly the raw
month they came from (tests/test_workbook.py). Times are New York wall-clock times.
Uploads are read from bytes in memory; nothing is written to disk.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

import yaml

from pipeline.link import Linker
from pipeline.nytime import ny, ny_date, ny_to_utc
from pipeline.tradingcal import month_bounds, month_trading_days, prev_month
from schemas.raw import (Account, AssetClass, CashFlow, CashFlowKind, CreditDebit, Event, EventType,
                         Fill, FillContext, FillSource, FilledStatus, IndexLevel, MarketContext,
                         OpenClose, Order, OrderType, PositionLine, PositionSnapshot, Products,
                         PutCall, RawMonth, Session, Side)

REPO = Path(__file__).resolve().parent.parent
Q4 = Decimal("0.0001")
OPTION = {"equity_option", "index_option", "future_option"}
FUTURE = {"future", "future_option"}
INDEX_ROOTS = {"SPX", "SPXW", "XSP", "NDX", "NDXP", "RUT", "VIX", "DJX"}
CRYPTO_ROOTS = {"BTC", "ETH", "SOL", "LTC", "DOGE", "XRP", "ADA", "AVAX"}
MAX_ROWS = 5000                       # per sheet; a demo upload, not a statement archive
FIXED_TIME = datetime(2026, 1, 1)     # workbook metadata, so the same input gives the same bytes


# ---------------------------------------------------------------------------
# Sheet layouts. A header ending in " *" is required. Reading ignores case, the
# " *" marker and columns it does not know.
# ---------------------------------------------------------------------------

ACCOUNT_FIELDS = [   # (label, key, required, kind)
    ("First name", "first_name", True, "str"),
    ("Month (YYYY-MM)", "month", True, "month"),
    ("Account value at start", "start_value", True, "dec"),
    ("Account value at end", "end_value", True, "dec"),
    ("Time zone", "time_zone", False, "str"),
    ("Customer ID", "customer_id", False, "str"),
    ("Options level (0-4)", "options_level", False, "int"),
    ("Futures approved", "futures", False, "bool"),
    ("Crypto approved", "crypto", False, "bool"),
    ("Event contracts approved", "event_contracts", False, "bool"),
    ("Cash at start", "cash_start", False, "dec"),
    ("Positions valued at, start (New York time)", "valued_start", False, "dt"),
    ("Cash at end", "cash_end", False, "dec"),
    ("Positions valued at, end (New York time)", "valued_end", False, "dt"),
]

SHEETS: dict[str, list[tuple[str, str, bool, str]]] = {   # sheet -> [(header, key, required, kind)]
    "Trades": [
        ("Time (New York)", "time", True, "dt"), ("Underlying", "underlying", True, "str"),
        ("Side", "side", True, "side"), ("Open/close", "open_close", True, "open_close"),
        ("Quantity", "qty", True, "dec"), ("Price", "price", True, "dec"),
        ("Expiration", "expiration", False, "date"), ("Strike", "strike", False, "dec"),
        ("Put/call", "put_call", False, "put_call"), ("Underlying price", "underlying_price", False, "dec"),
        ("Commission", "commission", False, "dec"), ("Fees", "fees", False, "dec"),
        ("Asset class", "asset_class", False, "asset_class"), ("Multiplier", "multiplier", False, "int"),
        ("Contract month", "contract_month", False, "month"), ("Symbol", "symbol", False, "str"),
        ("Session", "session", False, "session"), ("Order ID", "order_id", False, "str"),
        ("Fill ID", "exec_id", False, "str"), ("Source", "source", False, "source"),
        ("Implied volatility", "iv", False, "dec"), ("Implied volatility rank (0-100)", "iv_rank", False, "dec"),
        ("Days to expiration", "dte", False, "int"),
    ],
    "Orders": [
        ("Order ID", "order_id", True, "str"), ("Time (New York)", "time", False, "dt"),
        ("Order type", "order_type", False, "order_type"), ("Net price (credit is positive)", "net_price", False, "dec"),
        ("Credit/debit", "credit_debit", False, "credit_debit"), ("Filled status", "filled_status", False, "filled_status"),
        ("Requested units", "requested_units", False, "dec"), ("Filled units", "filled_units", False, "dec"),
    ],
    "Positions": [
        ("As of (start/end)", "as_of", True, "as_of"), ("Underlying", "underlying", True, "str"),
        ("Quantity (negative is short)", "qty", True, "dec"), ("Cost basis", "cost_basis", True, "dec"),
        ("Mark", "mark", True, "dec"), ("Opened date", "opened_date", False, "date"),
        ("Expiration", "expiration", False, "date"), ("Strike", "strike", False, "dec"),
        ("Put/call", "put_call", False, "put_call"), ("Asset class", "asset_class", False, "asset_class"),
        ("Multiplier", "multiplier", False, "int"), ("Contract month", "contract_month", False, "month"),
        ("Symbol", "symbol", False, "str"),
    ],
    "Events": [
        ("Time (New York)", "time", True, "dt"), ("Type", "type", True, "event_type"),
        ("Underlying", "underlying", True, "str"), ("Quantity (before the event)", "qty", True, "dec"),
        ("Cash amount", "cash_amount", False, "dec"), ("Expiration", "expiration", False, "date"),
        ("Strike", "strike", False, "dec"), ("Put/call", "put_call", False, "put_call"),
        ("Asset class", "asset_class", False, "asset_class"), ("Contract month", "contract_month", False, "month"),
        ("Symbol", "symbol", False, "str"), ("Settlement price", "settlement_price", False, "dec"),
        ("Resulting fill IDs", "resulting_exec_ids", False, "list"), ("Split ratio", "split_ratio", False, "dec"),
        ("Adjusted symbols (old=new; ...)", "adjusted_symbols", False, "map"), ("Early", "early", False, "bool"),
        ("Event ID", "event_id", False, "str"),
    ],
    "Cash flows": [
        ("Date", "date", True, "date"), ("Kind (deposit/withdrawal)", "kind", True, "cash_kind"),
        ("Amount", "amount", True, "dec"),
    ],
    "Market": [
        ("Symbol", "symbol", True, "str"), ("Close at start of month", "start", True, "dec"),
        ("Close at end of month", "end", True, "dec"),
    ],
}

ENUMS = {
    "side": (Side, {"b": "buy", "bot": "buy", "bought": "buy", "s": "sell", "sld": "sell", "sold": "sell"}),
    "open_close": (OpenClose, {"o": "open", "opening": "open", "c": "close", "closing": "close"}),
    "put_call": (PutCall, {"c": "C", "call": "C", "p": "P", "put": "P"}),
    "asset_class": (AssetClass, {"stock": "stock_etf", "etf": "stock_etf", "option": "equity_option",
                                 "future option": "future_option", "futures": "future"}),
    "session": (Session, {}), "source": (FillSource, {}), "order_type": (OrderType, {}),
    "credit_debit": (CreditDebit, {}), "filled_status": (FilledStatus, {}), "event_type": (EventType, {}),
    "cash_kind": (CashFlowKind, {}), "as_of": (None, {"start": "start", "end": "end"}),
}


class WorkbookError(ValueError):
    """Every problem found in a workbook, each naming its sheet, row and column."""

    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__(f"{len(problems)} problem(s) in the workbook: " + "; ".join(problems[:5]))


@dataclass
class ImportResult:
    raw: RawMonth
    assumptions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Cell conversion
# ---------------------------------------------------------------------------

def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _convert(v, kind: str):
    if _blank(v):
        return None
    if kind == "str":
        return str(v).strip() if not isinstance(v, float) or not v.is_integer() else str(int(v))
    if kind == "dec":
        if isinstance(v, bool):
            raise ValueError("expected a number")
        if isinstance(v, int):
            return Decimal(v)
        if isinstance(v, float):
            return Decimal(repr(v))
        try:
            return Decimal(str(v).strip().replace(",", "").replace("$", ""))
        except InvalidOperation:
            raise ValueError(f"expected a number, got {v!r}") from None
    if kind == "int":
        d = _convert(v, "dec")
        if d != d.to_integral_value():
            raise ValueError(f"expected a whole number, got {v!r}")
        return int(d)
    if kind == "bool":
        s = str(v).strip().lower()
        if s in ("yes", "y", "true", "1", "x"):
            return True
        if s in ("no", "n", "false", "0"):
            return False
        raise ValueError(f"expected yes or no, got {v!r}")
    if kind == "date":
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        try:
            return date.fromisoformat(str(v).strip()[:10])
        except ValueError:
            raise ValueError(f"expected a date like 2026-10-16, got {v!r}") from None
    if kind == "dt":
        if isinstance(v, datetime):
            wall = v.replace(tzinfo=None)
        elif isinstance(v, date):
            wall = datetime.combine(v, time())
        else:
            try:
                wall = datetime.fromisoformat(str(v).strip())
            except ValueError:
                raise ValueError(f"expected a date and time like 2026-10-01 10:30, got {v!r}") from None
            if wall.tzinfo is not None:
                raise ValueError("give New York wall-clock time without a time zone")
        wall = (wall + timedelta(microseconds=500_000)).replace(microsecond=0)   # Excel stores fractions of a day
        return wall
    if kind == "month":
        if isinstance(v, (datetime, date)):
            return f"{v.year}-{v.month:02d}"
        s = str(v).strip()
        try:
            y, m = s.split("-")
            if len(y) == 4 and 1 <= int(m) <= 12:
                return f"{int(y)}-{int(m):02d}"
        except ValueError:
            pass
        raise ValueError(f"expected a month like 2026-10, got {v!r}")
    if kind == "list":
        return [x.strip() for x in str(v).replace(";", ",").split(",") if x.strip()]
    if kind == "map":
        out = {}
        for pair in str(v).split(";"):
            if pair.strip():
                old, sep, new = pair.partition("=")
                if not sep:
                    raise ValueError(f"expected old=new pairs separated by ';', got {v!r}")
                out[old.strip()] = new.strip()
        return out
    enum, aliases = ENUMS[kind]
    s = str(v).strip()
    key = aliases.get(s.lower(), s if kind == "put_call" else s.lower().replace(" ", "_"))
    allowed = [e.value for e in enum] if enum else sorted(set(aliases.values()))
    if key.upper() in allowed and kind == "put_call":
        key = key.upper()
    if key not in allowed:
        raise ValueError(f"expected one of {', '.join(allowed)}, got {v!r}")
    return key


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def _norm(h) -> str:
    return " ".join(str(h or "").replace("*", "").split()).lower()


def _read_table(ws, sheet: str, problems: list[str]) -> list[tuple[int, dict]]:
    cols = SHEETS[sheet]
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None) or ()
    index = {_norm(h): i for i, h in enumerate(header) if not _blank(h)}
    where = {}
    for label, key, required, _ in cols:
        if _norm(label) in index:
            where[key] = index[_norm(label)]
        elif required:
            problems.append(f"{sheet}: the column {label!r} is missing")
    if any(req and key not in where for _, key, req, _ in cols):
        return []
    out = []
    for n, row in enumerate(rows, start=2):
        if all(_blank(c) for c in row):
            continue
        if len(out) >= MAX_ROWS:
            problems.append(f"{sheet}: more than {MAX_ROWS} rows; split the month into a smaller sample")
            break
        rec = {"_bad": False}
        for label, key, required, kind in cols:
            v = row[where[key]] if key in where and where[key] < len(row) else None
            try:
                rec[key] = _convert(v, kind)
            except ValueError as e:
                problems.append(f"{sheet} row {n}, {label}: {e}")
                rec[key], rec["_bad"] = None, True
                continue
            if required and rec[key] is None:
                problems.append(f"{sheet} row {n}, {label}: required")
                rec["_bad"] = True
        out.append((n, rec))
    return out


def _read_account(ws, problems: list[str]) -> dict:
    found = {}
    for row in ws.iter_rows(values_only=True):
        if row and not _blank(row[0]):
            found[_norm(row[0])] = row[1] if len(row) > 1 else None
    acct = {}
    for label, key, required, kind in ACCOUNT_FIELDS:
        try:
            acct[key] = _convert(found.get(_norm(label)), kind)
        except ValueError as e:
            problems.append(f"Account, {label}: {e}")
            acct[key] = None
            continue
        if required and acct[key] is None:
            problems.append(f"Account, {label}: required")
    return acct


def _strike_text(k: Decimal) -> str:
    s = format(k.normalize(), "f")
    return s


def instrument_key(underlying: str, asset_class: str, expiration, strike, put_call, contract_month) -> str:
    """The symbol used when a row gives none: readable, and the same for every row
    that names the same instrument."""
    if asset_class in OPTION:
        parts = [underlying] + ([contract_month] if asset_class == "future_option" else []) + \
                [expiration.isoformat(), _strike_text(strike), put_call]
        return " ".join(parts)
    if asset_class == "future":
        return f"{underlying} {contract_month}"
    return underlying


def _asset_class(rec: dict) -> str:
    if rec.get("asset_class"):
        return rec["asset_class"]
    u = (rec.get("underlying") or "").upper()
    if rec.get("put_call") or rec.get("strike") is not None:
        return "index_option" if u in INDEX_ROOTS else "future_option" if u.startswith("/") else "equity_option"
    if u.startswith("/"):
        return "future"
    return "crypto" if u in CRYPTO_ROOTS else "stock_etf"


def _instrument(rec: dict, sheet: str, n: int, problems: list[str], multiplier_needed: bool = True) -> dict | None:
    ac = _asset_class(rec)
    out = {"asset_class": ac, "underlying": rec["underlying"], "strike": None, "expiration": None,
           "put_call": None, "contract_month": None}
    ok = True
    if ac in OPTION:
        for k, label in (("expiration", "Expiration"), ("strike", "Strike"), ("put_call", "Put/call")):
            if rec.get(k) is None:
                problems.append(f"{sheet} row {n}, {label}: required for an option")
                ok = False
            out[k] = rec.get(k)
    if ac in FUTURE:
        if rec.get("contract_month") is None:
            problems.append(f"{sheet} row {n}, Contract month: required for futures and futures options")
            ok = False
        out["contract_month"] = rec.get("contract_month")
    if multiplier_needed:
        m = rec.get("multiplier")
        if m is None:
            if ac in FUTURE:
                problems.append(f"{sheet} row {n}, Multiplier: required for futures and futures options")
                ok = False
            m = 100 if ac in OPTION else 1
        out["multiplier"] = m
    if not ok:
        return None
    out["symbol"] = rec.get("symbol") or instrument_key(out["underlying"], ac, out["expiration"], out["strike"],
                                                        out["put_call"], out["contract_month"])
    return out


def _session(wall: datetime) -> str:
    t = wall.time()
    if time(9, 30) <= t < time(16, 0):
        return "regular"
    if time(4, 0) <= t < time(9, 30):
        return "pre_market"
    if time(16, 0) <= t < time(20, 0):
        return "after_hours"
    return "overnight"


def _close_utc(d: date) -> datetime:
    return ny_to_utc(datetime.combine(d, time(16, 0)))


def index_levels(month: str) -> list[IndexLevel] | None:
    table = yaml.safe_load((REPO / "config" / "index_levels.yaml").read_text(encoding="utf-8")) or {}
    levels = table.get(month)
    if not levels:
        return None
    return [IndexLevel(symbol=s, start=Decimal(str(v["start"])), end=Decimal(str(v["end"]))) for s, v in levels.items()]


class _Replay(Linker):
    """The pipeline's own linker, keeping the cash it computed before month end."""

    def _relabel(self) -> None:
        super()._relabel()
        self.replayed_cash = self.cash


def read_workbook(data: bytes) -> ImportResult:
    """Read an uploaded workbook (bytes) into a validated RawMonth plus the defaults used."""
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise WorkbookError(["This file is not an Excel workbook (.xlsx). Start from the template."]) from None
    names = {_norm(n): n for n in wb.sheetnames}
    problems: list[str] = []
    for required in ("Account", "Trades"):
        if _norm(required) not in names:
            problems.append(f"The {required} sheet is missing")
    if problems:
        raise WorkbookError(problems)
    acct = _read_account(wb[names["account"]], problems)
    tables = {s: _read_table(wb[names[_norm(s)]], s, problems) for s in SHEETS if _norm(s) in names}
    has = {s: _norm(s) in names for s in SHEETS}
    wb.close()
    if any(p.startswith("Account") for p in problems):
        raise WorkbookError(problems)
    return _build(acct, tables, has, problems)


def _build(acct: dict, tables: dict, has: dict, problems: list[str]) -> ImportResult:
    """`problems` holds the cell problems found while reading; rows that had one are
    still checked for missing fields, so every problem is reported in one pass."""
    notes: list[str] = []
    month = acct["month"]
    first_day, last_day = month_bounds(month)
    try:
        days = month_trading_days(month)
        prev_days = month_trading_days(prev_month(month))
    except ValueError:
        raise WorkbookError([f"Account, Month: no market calendar for {month}; use a month in 2025 to 2027"]) from None

    # -- trades -> fills and market context
    fills, ctx = [], []
    trade_rows = tables.get("Trades", [])
    if not trade_rows:
        problems.append("Trades: no rows; add at least one trade")
    used_ids = {r["exec_id"] for _, r in trade_rows if r.get("exec_id")}
    auto_orders = 0
    for n, r in trade_rows:
        inst = _instrument(r, "Trades", n, problems)
        if inst is None or r["_bad"]:
            continue
        try:
            ts = ny_to_utc(r["time"])
        except ValueError as e:
            problems.append(f"Trades row {n}, Time (New York): {e}")
            continue
        if not first_day <= r["time"].date() <= last_day:
            problems.append(f"Trades row {n}, Time (New York): {r['time']:%Y-%m-%d} is outside {month}")
        exec_id = r.get("exec_id") or f"F{n:05d}"
        if not r.get("exec_id") and exec_id in used_ids:
            exec_id = f"F{n:05d}-row"
        source = r.get("source") or "trade"
        order_id = None
        if source == "trade":
            order_id = r.get("order_id")
            if order_id is None:
                order_id, auto_orders = f"O-{exec_id}", auto_orders + 1
        price = r["price"]
        under_px = r.get("underlying_price")
        if under_px is None:
            if inst["asset_class"] in OPTION:
                problems.append(f"Trades row {n}, Underlying price: required for an option "
                                "(the price of the stock, index or future when the trade filled)")
                continue
            under_px = price
        dte = r.get("dte")
        if dte is None and inst["expiration"] is not None:
            dte = (inst["expiration"] - r["time"].date()).days
        try:
            fills.append(Fill(exec_id=exec_id, order_id=order_id, ts_utc=ts, session=r.get("session") or _session(r["time"]),
                              side=r["side"], open_close=r["open_close"], qty=r["qty"], price=price,
                              commission=r.get("commission"), fees=r.get("fees"), source=source, **inst))
            ctx.append(FillContext(exec_id=exec_id, underlying_price=under_px, iv=r.get("iv"), iv_rank=r.get("iv_rank"), dte=dte))
        except ValueError as e:
            problems.append(f"Trades row {n}: {str(e).splitlines()[-1].strip()}")
    if auto_orders:
        notes.append(f"{auto_orders} trade row(s) had no Order ID, so each was treated as its own order. "
                     "Give the legs of a spread or a roll the same Order ID to keep them together.")
    if any(f.commission is None or f.fees is None for f in fills):
        notes.append("Some trades have no commission or fees, so the recap is marked for review. Enter 0 where there were none.")
    if problems:
        raise WorkbookError(problems)

    # -- orders
    legs: dict[str, list[Fill]] = {}
    for f in fills:
        if f.order_id is not None:
            legs.setdefault(f.order_id, []).append(f)
    given = {r["order_id"]: (n, r) for n, r in tables.get("Orders", []) if not r["_bad"]}
    for oid in given:
        if oid not in legs:
            problems.append(f"Orders row {given[oid][0]}, Order ID: no trade row has Order ID {oid!r}")
    orders, derived = [], 0
    for oid in [o for o in given if o in legs] + [o for o in legs if o not in given]:
        lf = legs[oid]
        units = min(f.qty for f in lf)
        net = sum(((1 if f.side == "sell" else -1) * f.price * f.qty for f in lf), Decimal(0)) / units
        r = given.get(oid, (0, {}))[1]
        derived += oid not in given
        ts = ny_to_utc(r["time"]) if r.get("time") else min(f.ts_utc for f in lf)
        net_price = r.get("net_price") if r.get("net_price") is not None else net
        orders.append(Order(
            order_id=oid, ts_utc=ts, leg_exec_ids=[f.exec_id for f in lf],
            order_type=r.get("order_type") or "limit", net_price=net_price,
            credit_debit=r.get("credit_debit") or ("credit" if net_price > 0 else "debit" if net_price < 0 else "even"),
            filled_status=r.get("filled_status") or "filled",
            requested_units=r.get("requested_units") if r.get("requested_units") is not None else units,
            filled_units=r.get("filled_units") if r.get("filled_units") is not None else units))
    if derived and has.get("Orders"):
        notes.append(f"{derived} order(s) were not on the Orders sheet; their details came from the trades.")
    elif derived and len(orders) > auto_orders:
        notes.append("There is no Orders sheet, so each order was taken as a fully filled limit order with "
                     "its net price worked out from the legs.")

    # -- cash flows, market, account
    flows = [CashFlow(date=r["date"], kind=r["kind"], amount=r["amount"]) for _, r in tables.get("Cash flows", [])
             if not r["_bad"]]
    if has.get("Market"):
        indexes = [IndexLevel(symbol=r["symbol"], start=r["start"], end=r["end"]) for _, r in tables["Market"]
                   if not r["_bad"]]
    else:
        indexes = index_levels(month)
        if indexes is None:
            raise WorkbookError([f"Market: add a Market sheet with SPY closes for {month} "
                                 "(built-in levels exist only for the sample months)"])
        notes.append(f"There is no Market sheet, so the built-in sample index levels for {month} were used.")
    if not any(i.symbol == "SPY" for i in indexes):
        notes.append("The Market sheet has no SPY row, so the recap cannot compare the month with the broad market.")
    kinds = {f.asset_class for f in fills}
    products = Products(
        options_level=acct.get("options_level") if acct.get("options_level") is not None else (4 if kinds & OPTION else 0),
        futures=acct["futures"] if acct.get("futures") is not None else bool(kinds & FUTURE),
        crypto=acct["crypto"] if acct.get("crypto") is not None else "crypto" in kinds,
        event_contracts=acct["event_contracts"] if acct.get("event_contracts") is not None else "event_contract" in kinds)
    account = Account(customer_id=acct.get("customer_id") or "upload", first_name=acct["first_name"],
                      time_zone=acct.get("time_zone") or "America/New_York", month=month, products=products,
                      start_value=acct["start_value"], end_value=acct["end_value"], cash_flows=flows)

    # -- positions
    lines = {"start": [], "end": []}
    for n, r in tables.get("Positions", []):
        inst = _instrument(r, "Positions", n, problems)
        if inst is None or r["_bad"]:
            continue
        opened = r.get("opened_date")
        if opened is None:
            opened = prev_days[-1] if r["as_of"] == "start" else first_day
            if r["as_of"] == "start":
                notes.append(f"Positions row {n} has no opened date; {opened:%Y-%m-%d} was used.")
        lines[r["as_of"]].append(PositionLine(qty=r["qty"], cost_basis=r["cost_basis"], mark=r["mark"],
                                              opened_date=opened, **inst))
    if problems:
        raise WorkbookError(problems)

    def snapshot(as_of: str, cash, valued, value) -> PositionSnapshot:
        held = sum((l.qty * l.mark * l.multiplier for l in lines[as_of] if l.asset_class != "future"), Decimal(0))
        if cash is None:
            cash = value - held
            if lines[as_of]:
                notes.append(f"No cash at {as_of} was given, so it was taken as the account value at {as_of} "
                             "minus the positions held.")
        return PositionSnapshot(as_of=as_of, valued_at_utc=valued, cash=cash, lines=lines[as_of])

    start_valued = ny_to_utc(acct["valued_start"]) if acct.get("valued_start") else _close_utc(prev_days[-1])
    end_valued = ny_to_utc(acct["valued_end"]) if acct.get("valued_end") else _close_utc(days[-1])
    start = snapshot("start", acct.get("cash_start"), start_valued, acct["start_value"])
    if acct.get("cash_start") is None and not lines["start"]:
        notes.append("There are no start positions, so the account held only cash at the start of the month.")
    end_given = acct.get("cash_end") is not None or bool(lines["end"])

    # -- events
    events = []
    for n, r in tables.get("Events", []):
        inst = _instrument(r, "Events", n, problems, multiplier_needed=False)
        if inst is None or r["_bad"]:
            continue
        events.append(Event(event_id=r.get("event_id") or f"V{n:04d}", type=r["type"], ts_utc=ny_to_utc(r["time"]),
                            symbol=inst["symbol"], underlying=r["underlying"], qty=r["qty"],
                            cash_amount=r.get("cash_amount") if r.get("cash_amount") is not None else Decimal(0),
                            settlement_price=r.get("settlement_price"), resulting_exec_ids=r.get("resulting_exec_ids") or [],
                            split_ratio=r.get("split_ratio"), adjusted_symbols=r.get("adjusted_symbols"),
                            early=bool(r.get("early"))))
    if problems:
        raise WorkbookError(problems)

    def raw_with(end: PositionSnapshot, evs: list[Event]) -> RawMonth:
        return RawMonth(account=account, fills=fills, orders=orders, positions_start=start, positions_end=end,
                        events=evs, market=MarketContext(fills=ctx, indexes=indexes))

    try:
        if end_given:
            end = snapshot("end", acct.get("cash_end"), end_valued, acct["end_value"])
            return ImportResult(raw_with(end, events), notes)
        empty_end = PositionSnapshot(as_of="end", valued_at_utc=end_valued, cash=Decimal(0), lines=[])
        rep = _Replay(raw_with(empty_end, events))
        rep.run()
        if not has.get("Events"):
            expired = sorted((s, p) for s, p in rep.positions.items()
                             if p.inst.expiration is not None and p.inst.expiration <= last_day)
            if expired:
                events = [Event(event_id=f"V-auto-{i + 1:04d}", type="expiration_worthless",
                                ts_utc=_close_utc(p.inst.expiration), symbol=s, underlying=p.inst.underlying,
                                qty=p.qty, cash_amount=Decimal(0), settlement_price=None, resulting_exec_ids=[],
                                split_ratio=None, adjusted_symbols=None, early=False)
                          for i, (s, p) in enumerate(expired)]
                notes.append(f"There is no Events sheet, so {len(expired)} option position(s) still held after "
                             f"expiration were taken as expiring worthless: {', '.join(s for s, _ in expired)}.")
                rep = _Replay(raw_with(empty_end, events))
                rep.run()
        last_price, first_open = {}, {}
        for f in sorted(fills, key=lambda f: f.ts_utc):
            last_price[f.symbol] = f.price
            first_open.setdefault(f.symbol, ny_date(f.ts_utc))
        for l in lines["start"]:
            first_open[l.symbol] = l.opened_date
            last_price.setdefault(l.symbol, l.mark)
        end_lines = [PositionLine(symbol=s, asset_class=p.inst.asset_class, underlying=p.inst.underlying,
                                  multiplier=p.inst.multiplier, strike=p.inst.strike, expiration=p.inst.expiration,
                                  put_call=p.inst.put_call, contract_month=p.inst.contract_month, qty=p.qty,
                                  cost_basis=p.cost.quantize(Q4, ROUND_HALF_UP), mark=last_price.get(s, Decimal(0)),
                                  opened_date=first_open.get(s, first_day))
                     for s, p in sorted(rep.positions.items())]
        end = PositionSnapshot(as_of="end", valued_at_utc=end_valued, cash=rep.replayed_cash, lines=end_lines)
        notes.append("There are no end positions, so month-end holdings came from replaying the trades, "
                     "each marked at its last traded price.")
        return ImportResult(raw_with(end, events), notes)
    except ValueError as e:
        raise WorkbookError([str(e).splitlines()[0]]) from None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

README = [
    "Monthly trade recap: workbook template",
    "",
    "Sample or made-up trades only. Never put real account data in this workbook.",
    "",
    "Required: the Account sheet (fill in the values next to each field) and the Trades sheet",
    "(one row per fill). Columns marked * are required; every other column and sheet is optional.",
    "Times are New York wall-clock times, for example 2026-10-01 10:30.",
    "",
    "Options rows need Expiration, Strike, Put/call and Underlying price (the price of the stock,",
    "index or future when the trade filled). Futures rows need Contract month and Multiplier.",
    "Give the legs of a spread or a roll the same Order ID so they count as one order.",
    "Enter 0 for commission or fees when there were none; a blank means the value is missing.",
    "",
    "Optional sheets: Orders (order details), Positions (holdings at the start and end of the",
    "month), Events (expirations, assignments, exercises, splits, dividends), Cash flows",
    "(deposits and withdrawals), Market (SPY and QQQ closes). When one is left out, the site",
    "uses a stated default and lists it next to the recap.",
]


def _cell(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() and abs(v) < 10 ** 15 else float(v)
    if hasattr(v, "value") and not isinstance(v, (str, int)):
        return v.value
    if isinstance(v, list):
        return ", ".join(v)
    if isinstance(v, dict):
        return "; ".join(f"{a}={b}" for a, b in v.items())
    if isinstance(v, bool):
        return "yes" if v else "no"
    return v


def _wall(ts: datetime) -> datetime:
    return ny(ts)


def _style_sheet(ws, headers: list[str], required: list[bool]) -> None:
    from openpyxl.styles import Font, PatternFill

    bold = Font(bold=True)
    req_fill = PatternFill("solid", fgColor="DCE9F5")
    for i, (h, req) in enumerate(zip(headers, required), start=1):
        c = ws.cell(row=1, column=i, value=f"{h} *" if req else h)
        c.font = bold
        if req:
            c.fill = req_fill
        ws.column_dimensions[c.column_letter].width = max(12, min(34, len(h) + 4))
    ws.freeze_panes = "A2"


def _rows_for(raw: RawMonth) -> dict[str, list[dict]]:
    ctx = {c.exec_id: c for c in raw.market.fills}
    trades = []
    for f in raw.fills:
        c = ctx[f.exec_id]
        trades.append({"time": _wall(f.ts_utc), "underlying": f.underlying, "side": f.side, "open_close": f.open_close,
                       "qty": f.qty, "price": f.price, "expiration": f.expiration, "strike": f.strike,
                       "put_call": f.put_call, "underlying_price": c.underlying_price, "commission": f.commission,
                       "fees": f.fees, "asset_class": f.asset_class, "multiplier": f.multiplier,
                       "contract_month": f.contract_month, "symbol": f.symbol, "session": f.session,
                       "order_id": f.order_id, "exec_id": f.exec_id, "source": f.source, "iv": c.iv,
                       "iv_rank": c.iv_rank, "dte": c.dte})
    orders = [{"order_id": o.order_id, "time": _wall(o.ts_utc), "order_type": o.order_type, "net_price": o.net_price,
               "credit_debit": o.credit_debit, "filled_status": o.filled_status,
               "requested_units": o.requested_units, "filled_units": o.filled_units} for o in raw.orders]
    positions = [{"as_of": snap.as_of, "underlying": l.underlying, "qty": l.qty, "cost_basis": l.cost_basis,
                  "mark": l.mark, "opened_date": l.opened_date, "expiration": l.expiration, "strike": l.strike,
                  "put_call": l.put_call, "asset_class": l.asset_class, "multiplier": l.multiplier,
                  "contract_month": l.contract_month, "symbol": l.symbol}
                 for snap in (raw.positions_start, raw.positions_end) for l in snap.lines]
    fills = {f.symbol: f for f in raw.fills}
    lines = {l.symbol: l for snap in (raw.positions_start, raw.positions_end) for l in snap.lines}
    events = []
    for e in raw.events:
        src = lines.get(e.symbol) or fills.get(e.symbol)
        events.append({"time": _wall(e.ts_utc), "type": e.type, "underlying": e.underlying, "qty": e.qty,
                       "cash_amount": e.cash_amount, "symbol": e.symbol,
                       "asset_class": src.asset_class if src else "stock_etf",
                       "expiration": src.expiration if src else None, "strike": src.strike if src else None,
                       "put_call": src.put_call if src else None, "contract_month": src.contract_month if src else None,
                       "settlement_price": e.settlement_price, "resulting_exec_ids": e.resulting_exec_ids or None,
                       "split_ratio": e.split_ratio, "adjusted_symbols": e.adjusted_symbols, "early": e.early,
                       "event_id": e.event_id})
    flows = [{"date": c.date, "kind": c.kind, "amount": c.amount} for c in raw.account.cash_flows]
    market = [{"symbol": i.symbol, "start": i.start, "end": i.end} for i in raw.market.indexes]
    return {"Trades": trades, "Orders": orders, "Positions": positions, "Events": events,
            "Cash flows": flows, "Market": market}


def _account_values(raw: RawMonth | None, month: str) -> dict:
    if raw is None:
        return {"month": month}
    a, s, e = raw.account, raw.positions_start, raw.positions_end
    return {"first_name": a.first_name, "month": a.month, "start_value": a.start_value, "end_value": a.end_value,
            "time_zone": a.time_zone, "customer_id": a.customer_id, "options_level": a.products.options_level,
            "futures": a.products.futures, "crypto": a.products.crypto, "event_contracts": a.products.event_contracts,
            "cash_start": s.cash, "valued_start": _wall(s.valued_at_utc), "cash_end": e.cash,
            "valued_end": _wall(e.valued_at_utc)}


def write_workbook(raw: RawMonth | None = None, month: str = "2026-10") -> bytes:
    """A filled workbook for `raw`, or the blank template (with the month's sample
    market levels) when raw is None. The same input always gives the same bytes."""
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()
    ws = wb.active
    ws.title = "Read me"
    for i, line in enumerate(README, start=1):
        c = ws.cell(row=i, column=1, value=line)
        if i == 1:
            c.font = Font(bold=True, size=14)
    ws.column_dimensions["A"].width = 100

    acct = wb.create_sheet("Account")
    values = _account_values(raw, month)
    acct.cell(row=1, column=1, value="Field").font = Font(bold=True)
    acct.cell(row=1, column=2, value="Value").font = Font(bold=True)
    for i, (label, key, required, kind) in enumerate(ACCOUNT_FIELDS, start=2):
        acct.cell(row=i, column=1, value=f"{label} *" if required else label)
        v = values.get(key)
        acct.cell(row=i, column=2, value=_cell(v) if v is not None else None)
    acct.column_dimensions["A"].width = 44
    acct.column_dimensions["B"].width = 22

    rows = _rows_for(raw) if raw is not None else {"Market": [{"symbol": i.symbol, "start": i.start, "end": i.end}
                                                               for i in (index_levels(month) or [])]}
    for sheet, cols in SHEETS.items():
        ws = wb.create_sheet(sheet)
        _style_sheet(ws, [c[0] for c in cols], [c[2] for c in cols])
        for r, rec in enumerate(rows.get(sheet, []), start=2):
            for col, (_, key, _, kind) in enumerate(cols, start=1):
                v = rec.get(key)
                if v is None:
                    continue
                cell = ws.cell(row=r, column=col, value=_cell(v))
                if kind == "dt":
                    cell.number_format = "yyyy-mm-dd hh:mm:ss"
                elif kind == "date":
                    cell.number_format = "yyyy-mm-dd"
    wb.properties.created = wb.properties.modified = FIXED_TIME
    wb.properties.creator = wb.properties.lastModifiedBy = "monthly_trade_recap"
    buf = io.BytesIO()
    wb.save(buf)
    return _stable_zip(buf.getvalue())


def _stable_zip(data: bytes) -> bytes:
    """Rewrite the .xlsx zip with fixed entry timestamps (zip entries otherwise carry
    the time of writing)."""
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            fixed = zipfile.ZipInfo(info.filename, date_time=(1980, 1, 1, 0, 0, 0))
            fixed.compress_type = zipfile.ZIP_DEFLATED
            fixed.external_attr = info.external_attr
            dst.writestr(fixed, src.read(info.filename))
    return out.getvalue()
