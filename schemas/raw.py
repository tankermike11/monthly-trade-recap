"""Raw per-customer, per-month data contracts (SPEC 1.1).

Conventions
-----------
- Money, prices and quantities are ``Decimal``. Floats never cross a stage boundary.
- Timestamps are timezone-aware UTC.
- ``commission`` and ``fees`` have no default: a missing value must be written as an
  explicit ``None`` and is a data-quality issue, never a silent zero.
- Account value = cash + sum(qty * mark * multiplier) for every non-futures line
  + sum(qty * mark * multiplier - cost_basis) for futures lines (futures move no cash
  until they are closed).
- ``cost_basis`` on a position line is signed: qty * average cost * multiplier.
  A short option line therefore has a negative cost basis (cash collected).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Session(StrEnum):
    pre_market = "pre_market"
    regular = "regular"
    after_hours = "after_hours"
    overnight = "overnight"


class AssetClass(StrEnum):
    stock_etf = "stock_etf"
    equity_option = "equity_option"
    index_option = "index_option"
    future = "future"
    future_option = "future_option"
    crypto = "crypto"
    event_contract = "event_contract"


OPTION_CLASSES = frozenset({AssetClass.equity_option, AssetClass.index_option, AssetClass.future_option})


class Side(StrEnum):
    buy = "buy"
    sell = "sell"


class OpenClose(StrEnum):
    open = "open"
    close = "close"


class PutCall(StrEnum):
    call = "C"
    put = "P"


class FillSource(StrEnum):
    trade = "trade"      # the customer's own order
    event = "event"      # created by an assignment or exercise


class OrderType(StrEnum):
    market = "market"
    limit = "limit"
    stop = "stop"


class CreditDebit(StrEnum):
    credit = "credit"
    debit = "debit"
    even = "even"


class FilledStatus(StrEnum):
    filled = "filled"
    partial = "partial"   # part of the requested size never filled


class EventType(StrEnum):
    expiration_worthless = "expiration_worthless"
    expiration_cash_settled = "expiration_cash_settled"
    assignment = "assignment"
    exercise = "exercise"
    split = "split"
    dividend = "dividend"


class CashFlowKind(StrEnum):
    deposit = "deposit"
    withdrawal = "withdrawal"


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

class Products(_Frozen):
    options_level: int = Field(ge=0, le=4)
    futures: bool
    crypto: bool
    event_contracts: bool


class CashFlow(_Frozen):
    date: date
    kind: CashFlowKind
    amount: Decimal = Field(gt=0)


class Account(_Frozen):
    customer_id: str
    first_name: str
    time_zone: str
    month: str                      # "2026-10"
    products: Products
    start_value: Decimal
    end_value: Decimal
    cash_flows: list[CashFlow]


# ---------------------------------------------------------------------------
# Instruments, fills, orders
# ---------------------------------------------------------------------------

class Fill(_Frozen):
    exec_id: str
    order_id: str | None            # None only for fills created by an event
    ts_utc: datetime
    session: Session
    asset_class: AssetClass
    symbol: str                     # OCC symbol for options; root + contract month for futures
    underlying: str                 # stock symbol, index, futures root (/MES) or coin (BTC)
    side: Side
    open_close: OpenClose
    qty: Decimal = Field(gt=0)
    price: Decimal = Field(ge=0)
    multiplier: int = Field(gt=0)
    strike: Decimal | None
    expiration: date | None
    put_call: PutCall | None
    contract_month: str | None      # futures and futures options: "2026-12"
    commission: Decimal | None      # explicit None means missing
    fees: Decimal | None            # explicit None means missing
    source: FillSource

    @model_validator(mode="after")
    def _shape(self) -> "Fill":
        is_opt = self.asset_class in OPTION_CLASSES
        opt_fields = (self.strike, self.expiration, self.put_call)
        if is_opt and any(v is None for v in opt_fields):
            raise ValueError(f"{self.exec_id}: option fill needs strike, expiration, put_call")
        if not is_opt and any(v is not None for v in opt_fields):
            raise ValueError(f"{self.exec_id}: non-option fill must not carry option fields")
        is_fut = self.asset_class in (AssetClass.future, AssetClass.future_option)
        if is_fut != (self.contract_month is not None):
            raise ValueError(f"{self.exec_id}: contract_month is required for futures and only for futures")
        if self.ts_utc.tzinfo is None or self.ts_utc.utcoffset().total_seconds() != 0:
            raise ValueError(f"{self.exec_id}: ts_utc must be timezone-aware UTC")
        if (self.source == FillSource.trade) != (self.order_id is not None):
            raise ValueError(f"{self.exec_id}: trade fills need an order_id, event fills must not have one")
        return self


class Order(_Frozen):
    order_id: str
    ts_utc: datetime
    leg_exec_ids: list[str] = Field(min_length=1)
    order_type: OrderType
    net_price: Decimal              # per unit, signed: positive = credit
    credit_debit: CreditDebit
    filled_status: FilledStatus
    requested_units: Decimal        # order size in units of the smallest leg ratio
    filled_units: Decimal


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

class PositionLine(_Frozen):
    symbol: str
    asset_class: AssetClass
    underlying: str
    multiplier: int
    strike: Decimal | None
    expiration: date | None
    put_call: PutCall | None
    contract_month: str | None
    qty: Decimal                    # signed: negative = short
    cost_basis: Decimal             # signed: qty * avg cost * multiplier
    mark: Decimal                   # per unit
    opened_date: date               # date the position was first opened


class PositionSnapshot(_Frozen):
    as_of: str                      # "start" or "end"
    valued_at_utc: datetime
    cash: Decimal
    lines: list[PositionLine]


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

class Event(_Frozen):
    event_id: str
    type: EventType
    ts_utc: datetime
    symbol: str                     # the affected instrument (for a split: the stock)
    underlying: str
    qty: Decimal                    # signed position quantity affected (before the event)
    cash_amount: Decimal            # signed cash to the account (0 if none)
    settlement_price: Decimal | None  # cash-settled expirations: the settlement value used
    resulting_exec_ids: list[str]   # stock or futures fills created by the event
    split_ratio: Decimal | None     # e.g. 2 for a 2-for-1
    adjusted_symbols: dict[str, str] | None  # split: old symbol -> new symbol
    early: bool                     # assignment or exercise before expiration day


# ---------------------------------------------------------------------------
# Market context
# ---------------------------------------------------------------------------

class FillContext(_Frozen):
    exec_id: str
    underlying_price: Decimal
    iv: Decimal | None              # annualised decimal (0.25 == 25%)
    iv_rank: Decimal | None         # 0-100, 52-week min-max rank
    dte: int | None                 # calendar days from the fill's ET date to expiration


class IndexLevel(_Frozen):
    symbol: str                     # SPY, QQQ, /ES
    start: Decimal                  # close of the last trading day of the previous month
    end: Decimal                    # close of the last trading day of this month


class MarketContext(_Frozen):
    fills: list[FillContext]
    indexes: list[IndexLevel]


# ---------------------------------------------------------------------------
# Container
# ---------------------------------------------------------------------------

class RawMonth(_Frozen):
    account: Account
    fills: list[Fill]
    orders: list[Order]
    positions_start: PositionSnapshot
    positions_end: PositionSnapshot
    events: list[Event]
    market: MarketContext

    @model_validator(mode="after")
    def _cross(self) -> "RawMonth":
        ids = [f.exec_id for f in self.fills]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate exec_id")
        id_set = set(ids)
        by_id = {f.exec_id: f for f in self.fills}
        for o in self.orders:
            for e in o.leg_exec_ids:
                if e not in id_set:
                    raise ValueError(f"order {o.order_id} leg {e} has no fill")
                if by_id[e].order_id != o.order_id:
                    raise ValueError(f"fill {e} does not point back to order {o.order_id}")
        order_ids = {o.order_id for o in self.orders}
        for f in self.fills:
            if f.order_id is not None and f.order_id not in order_ids:
                raise ValueError(f"fill {f.exec_id} names unknown order {f.order_id}")
        # Month membership is by New York calendar date (see mock.market.calendar);
        # it is checked in tests rather than here to keep schemas free of clock rules.
        for ev in self.events:
            for e in ev.resulting_exec_ids:
                if e not in id_set:
                    raise ValueError(f"event {ev.event_id} names unknown fill {e}")
        ctx = {c.exec_id for c in self.market.fills}
        if ctx != id_set:
            raise ValueError("market context must cover every fill exactly once")
        return self
