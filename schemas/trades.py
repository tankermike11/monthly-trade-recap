"""Trade-story vocabulary shared by the mock answer keys and the Phase 2 linker.

Definitions (the generator's ledger and the Phase 2 pipeline must both follow these):

Trade story
    All orders and events on one underlying from the moment the customer's net position
    on that underlying stops being empty until it is empty again. Two positions on the
    same underlying at the same time are one story.

Order effect (per order, judged on the story's position just before and after it)
    open           the story had no position before this order
    close          the story has no position after this order
    roll           the order closes or reduces some instruments and opens or increases
                   others, and the story stays open. Two orders on the same underlying
                   within ROLL_WINDOW_SECONDS, one only closing and one only opening,
                   form one roll and both carry the effect "roll".
    adjust         adds a new instrument without reducing any, or fully removes some
                   instruments without opening any, and the story stays open
    add            only increases instruments the story already holds
    partial_close  only reduces instruments, none is removed completely, story stays open

Roll types (one roll may carry several; each is counted separately)
    Closed legs are paired with opened legs of the same put/call type and the same
    side (short with short, long with long).
    out_in_time            a new leg expires later than the leg it replaces
    up / down              a new leg's strike is above / below the leg it replaces
    untested_toward_money  the position held a short put and a short call; the
                           untested short leg (strike farther from the underlying price
                           at the time of the roll) was replaced by one closer to it
    tested_away_from_money the tested short leg (strike closer to the price) was
                           replaced by one farther from it
    widened / narrowed     width grew / shrank. Width = lowest short call strike minus
                           highest short put strike when both are short; otherwise the
                           strike distance between the short and long leg of one type.

Realized P&L uses average cost per instrument. Fees and commissions are subtracted
from the story they belong to when they are paid. Dividends count toward the story
that holds the shares. An assigned, exercised or worthless option closes at 0; a
cash-settled option closes at its settlement value.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

ROLL_WINDOW_SECONDS = 300


class StrategyLabel(StrEnum):
    stock_long = "stock_long"
    stock_short = "stock_short"
    long_call = "long_call"
    long_put = "long_put"
    short_call = "short_call"
    short_put = "short_put"
    covered_call = "covered_call"
    cash_secured_put = "cash_secured_put"
    credit_vertical = "credit_vertical"
    debit_vertical = "debit_vertical"
    short_strangle = "short_strangle"
    long_strangle = "long_strangle"
    short_straddle = "short_straddle"
    long_straddle = "long_straddle"
    iron_condor = "iron_condor"
    iron_butterfly = "iron_butterfly"
    butterfly = "butterfly"
    broken_wing_butterfly = "broken_wing_butterfly"
    calendar = "calendar"
    diagonal = "diagonal"
    future_long = "future_long"
    future_short = "future_short"
    crypto_long = "crypto_long"
    crypto_short = "crypto_short"
    custom = "custom"


class OrderEffect(StrEnum):
    open = "open"
    close = "close"
    roll = "roll"
    adjust = "adjust"
    add = "add"
    partial_close = "partial_close"


class RollType(StrEnum):
    out_in_time = "out_in_time"
    up = "up"
    down = "down"
    untested_toward_money = "untested_toward_money"
    tested_away_from_money = "tested_away_from_money"
    widened = "widened"
    narrowed = "narrowed"


class StoryStatus(StrEnum):
    open = "open"
    closed = "closed"        # made flat by the customer's order
    expired = "expired"      # made flat by an expiration
    assigned = "assigned"    # made flat by an assignment or exercise


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class StoryOrder(_Frozen):
    order_id: str
    effect: OrderEffect


class Roll(_Frozen):
    order_ids: list[str]            # one order, or two within the roll window
    ts_utc: datetime
    types: list[RollType]           # sorted
    net_cash: Decimal               # signed, before fees: positive = credit
    credit: bool


class TradeStory(_Frozen):
    story_id: str
    underlying: str
    asset_class: str                # asset class of the story's first instrument
    carried_in: bool
    label_open: StrategyLabel       # at the first order (carried in: at month start)
    label_end: StrategyLabel        # at month end, or just before the story closed
    orders: list[StoryOrder]        # this month's orders, in time order
    exec_ids: list[str]             # this month's fills (trade and event), in time order
    event_ids: list[str]
    rolls: list[Roll]
    opening_cash: Decimal           # signed, before fees: positive = credit collected.
                                    # Futures legs count 0 (no cash changes hands).
                                    # Carried in: minus the month-start cost basis.
    upfront_cash: Decimal           # opening_cash plus the cash of later 'add' orders
    opening_dte: int | None         # options only: nearest expiration in the opening order
    opened_date: date               # first opening date (may be before the month)
    closed_ts: datetime | None
    days_held: int | None           # calendar days, opened_date to close date
    days_left_at_close: int | None  # options only: days to the nearest closed expiration
    realized_pnl: Decimal           # this month, after this month's fees, incl. dividends
    fees: Decimal                   # this month's commission + fees in the data (missing = 0)
    status: StoryStatus
    end_positions: dict[str, Decimal]   # symbol -> signed qty open at month end
    open_pnl_end: Decimal           # sum(qty * mark * mult) - cost basis at month end
    open_pnl_start: Decimal         # same at month start (carried-in stories only, else 0)
