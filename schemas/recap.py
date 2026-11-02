"""RecapCopy (SPEC 4.2): the words the model writes, with {{fact_id}} placeholders where
numbers go. Code fills the placeholders from the facts sheet's display strings.

One addition to SPEC 4.2: `results_line`, a single sentence on how the month turned
out. The red-month rule (name the loss once, next to the market move) needs a place
for it; outside red months it may be empty.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Sections(_Frozen):
    getting_in: list[str]
    managing: list[str]
    getting_out: list[str]


class Teaser(_Frozen):
    fact: str          # a fact ID; its display is shown before the line
    line: str


class RecapCopy(_Frozen):
    one_liner: str
    glance_tiles: list[str]          # fact IDs
    sections: Sections
    results_line: str
    what_changed: str                # empty when nothing changed or no history
    teasers: list[Teaser]
    subject_line: str


class CheckResult(_Frozen):
    name: str
    passed: bool
    reasons: list[str] = Field(default_factory=list)


class ValidationResult(_Frozen):
    passed: bool
    checks: list[CheckResult]

    def reasons(self) -> list[str]:
        return [f"{c.name}: {r}" for c in self.checks if not c.passed for r in c.reasons]


class Attempt(_Frozen):
    raw_output: str
    validation: ValidationResult
    stop_reason: str | None
    usage: dict[str, Any]


class WriteResult(_Frozen):
    customer_id: str
    month: str
    status: str                      # "passed" | "review"
    recap: RecapCopy | None
    attempts: list[Attempt]
    model: str
    effort: str
    max_tokens: int
    prompt_version: str
    prompt_sha256: str


def output_schema() -> dict:
    """JSON schema for structured output: every object closed, every field required."""
    s = {"type": "string"}
    lines = {"type": "array", "items": s}
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["one_liner", "glance_tiles", "sections", "results_line", "what_changed",
                     "teasers", "subject_line"],
        "properties": {
            "one_liner": s,
            "glance_tiles": {"type": "array", "items": s},
            "sections": {
                "type": "object", "additionalProperties": False,
                "required": ["getting_in", "managing", "getting_out"],
                "properties": {"getting_in": lines, "managing": lines, "getting_out": lines},
            },
            "results_line": s,
            "what_changed": s,
            "teasers": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["fact", "line"],
                "properties": {"fact": s, "line": s}}},
            "subject_line": s,
        },
    }
