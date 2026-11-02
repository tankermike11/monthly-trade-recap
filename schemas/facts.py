"""The facts sheet (SPEC 3.2): every number the recap may use, computed in code.

The model chooses among facts and inserts their `display` strings; it never changes a
value or types a number of its own.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Section(StrEnum):
    at_a_glance = "at_a_glance"
    getting_in = "getting_in"
    managing = "managing"
    getting_out = "getting_out"
    results = "results"
    still_open = "still_open"
    what_changed = "what_changed"
    context = "context"


class Fact(_Frozen):
    id: str
    section: Section
    value: Any                       # number, Decimal string, dict or list: exactly as computed
    display: str                     # formatted in code: "$1,240", "3 to 1", "1 in 4"
    plain: str                       # fixed, pre-approved phrase for this fact type
    weight: float = Field(ge=0, le=1)  # code-side guess at how notable the fact is
    sources: list[str] = Field(min_length=1)   # "story:<id>", "fill:<exec_id>", "market:<symbol>"


class SheetIssue(_Frozen):
    issue: str
    refs: list[str]


class FactsSheet(_Frozen):
    customer_id: str
    month: str
    first_name: str
    flags: list[str]
    facts: list[Fact]
    data_quality_issues: list[SheetIssue]
    catalog_version: str
