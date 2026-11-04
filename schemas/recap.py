"""RecapCopy: the words Claude writes, with {{fact_id}} placeholders where numbers go.

Since prompt recap_v4 (2026-11-04) code writes every single-fact sentence, the tiles,
the results line and what changed (pipeline/compose.py); Claude writes only:
- overall: the overall picture of how the customer traded this month;
- observations: two or three patterns (none or one in a low-activity month), each a
  short heading and a few sentences backed by specific trades;
- subject_line.
Outputs from prompts recap_v1 to v3 used an earlier shape and are not read by this code.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Observation(_Frozen):
    heading: str       # a short sentence naming the pattern; no placeholders
    text: str          # one to three sentences backing it with specific trades


class RecapCopy(_Frozen):
    overall: str
    observations: list[Observation]
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
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["overall", "observations", "subject_line"],
        "properties": {
            "overall": s,
            "observations": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["heading", "text"],
                "properties": {"heading": s, "text": s}}},
            "subject_line": s,
        },
    }
