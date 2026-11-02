"""Answer key written by the mock generator next to each customer's raw month.

It is the ground truth for Phase 2 (stories) and Phase 3 (headline facts). It is built
from the generator's own ledger: what the generator did, not what an analysis of the
fills infers.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from schemas.trades import TradeStory


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class OptionQuote(_Frozen):
    """The simulated market at the moment of an option fill (mock only)."""
    theo: Decimal
    bid: Decimal
    ask: Decimal
    underlying_price: Decimal
    iv: Decimal
    model_id: str


class ExpectedFact(_Frozen):
    id: str
    value: Any                       # number, dict or None, exactly as defined in facts_catalog
    sources: list[str]               # "story:<id>" and "fill:<exec_id>"


class DataQualityIssue(_Frozen):
    issue: str                       # a key of facts_catalog.DATA_QUALITY_ISSUES
    refs: list[str]                  # exec, order or event IDs


class Reconciliation(_Frozen):
    start_value: Decimal
    end_value: Decimal
    net_cash_flows: Decimal          # deposits - withdrawals
    realized_pnl: Decimal            # sum over stories, fees from the data
    open_pnl_change: Decimal         # sum over stories of open_pnl_end - open_pnl_start
    difference: Decimal              # (end - start - flows) - (realized + open change)


class AnswerKey(_Frozen):
    customer_id: str
    month: str
    seed: int
    generator_version: str
    persona: str
    stories: list[TradeStory]
    headline_facts: list[ExpectedFact]
    flags: list[str]
    data_quality_issues: list[DataQualityIssue]
    reconciliation: Reconciliation
    option_quotes: dict[str, OptionQuote]   # exec_id -> quote at the fill
