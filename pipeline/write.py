"""Stage 4: one Claude call per customer writes the recap's overall picture and
observations (RecapCopy, SPEC 4). Code writes the rest (pipeline/compose.py).

- The system prompt (prompts/<version>.md) is cached; the user turn is JSON: the
  month's facts, group counts, every trade (numbers as fact IDs) and what code has
  already written. Sources stay out; they are for audit, not for writing.
- Structured output (output_config.format) guarantees the JSON shape; the validator
  (pipeline/validate_copy.py) checks the content.
- Fail once: retry with the failure reasons appended. Fail twice: status "review",
  written to out/<month>/review/<customer>.json, never sent.
- The key comes from ANTHROPIC_API_KEY (or the SDK's other standard credential
  sources); it is never written anywhere.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from pipeline.compose import Composed, compose
from pipeline.link import LinkResult
from pipeline.validate_copy import validate
from schemas.facts import FactsSheet
from schemas.recap import Attempt, WriteResult, output_schema

REPO = Path(__file__).resolve().parent.parent
NO_SERVER_FALLBACK = ("claude-haiku",)   # models without server-side refusal fallback


@dataclass(frozen=True)
class ModelConfig:
    model: str
    effort: str
    max_tokens: int
    prompt_version: str
    fallbacks: str | None
    prices: dict

    @classmethod
    def load(cls, path: Path | None = None, **overrides) -> "ModelConfig":
        d = yaml.safe_load((path or REPO / "config" / "model.yaml").read_text(encoding="utf-8"))
        d.update({k: v for k, v in overrides.items() if v is not None})
        return cls(d["model"], d["effort"], int(d["max_tokens"]), d["prompt_version"], d.get("fallbacks"),
                   d.get("prices", {}))


def system_prompt(version: str) -> str:
    return (REPO / "prompts" / f"{version}.md").read_text(encoding="utf-8")


def trades_by_day(c: Composed) -> list[dict]:
    shown = {f.id: f.display for f in c.sheet.facts}
    days: dict[str, list[dict]] = {}
    for t in c.trades:
        days.setdefault(shown[f"trade.{t['ref']}.opened"], []).append(t)
    return [{"opened": day, "trades": ts} for day, ts in days.items()]


def user_turn(c: Composed) -> str:
    """What the model sees: the month's facts, group counts, every trade, and what code
    already wrote. No audit sources, no data-quality issues."""
    fact = lambda f: {"id": f.id, "display": f.display, "plain": f.plain}  # noqa: E731
    return json.dumps({
        # first_month only means there is no earlier month of data (code writes "what
        # changed"); passed on, it read as "your first month" of trading.
        "first_name": c.sheet.first_name, "month": c.sheet.month,
        "flags": [f for f in c.sheet.flags if f != "first_month"],
        "month_facts": [fact(f) for f in c.sheet.facts if not f.id.startswith(("trade.", "group."))],
        "trade_counts": [fact(f) for f in c.sheet.facts if f.id.startswith("group.")],
        # grouped by opening day, so a claim about a range of days covers every trade
        # opened on each of them (a flat list read as "the first five trades")
        "trades_by_opening_day": trades_by_day(c),
        "already_in_the_recap": c.code_written(),
    }, sort_keys=True, ensure_ascii=False)


@dataclass(frozen=True)
class ModelReply:
    text: str
    stop_reason: str | None
    usage: dict


Caller = Callable[[ModelConfig, str, str], ModelReply]


def anthropic_caller(client=None) -> Caller:
    """The real call. Imported lazily so offline code paths never need the SDK."""
    import anthropic

    client = client or anthropic.Anthropic()

    def call(cfg: ModelConfig, system: str, user: str) -> ModelReply:
        kwargs = dict(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
            output_config={"effort": cfg.effort, "format": {"type": "json_schema", "schema": output_schema()}},
        )
        if cfg.fallbacks and not cfg.model.startswith(NO_SERVER_FALLBACK):
            resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                               fallbacks=cfg.fallbacks, **kwargs)
        else:
            resp = client.messages.create(**kwargs)
        text = next((b.text for b in resp.content if b.type == "text"), "")
        u = resp.usage
        usage = {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                 "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                 "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                 "model": resp.model, "request_id": getattr(resp, "_request_id", None)}
        return ModelReply(text, resp.stop_reason, usage)

    return call


def retry_turn(user: str, previous: str, reasons: list[str]) -> str:
    return (user + "\n\nYour previous draft failed these checks:\n"
            + "\n".join(f"- {r}" for r in reasons)
            + "\n\nPrevious draft:\n" + previous
            + "\n\nWrite a corrected version that passes every check.")


def write_recap(sheet: FactsSheet, linked: LinkResult, cfg: ModelConfig, call: Caller | None = None) -> WriteResult:
    call = call or anthropic_caller()
    system = system_prompt(cfg.prompt_version)
    composed = compose(sheet, linked)
    user = user_turn(composed)
    attempts: list[Attempt] = []
    copy = None
    turn = user
    for _ in range(2):
        reply = call(cfg, system, turn)
        if reply.stop_reason == "refusal":
            from schemas.recap import CheckResult, ValidationResult
            v = ValidationResult(passed=False, checks=[CheckResult(name="model_refused", passed=False,
                                                                   reasons=["the model declined the request"])])
            copy = None
        else:
            copy, v = validate(reply.text, composed.sheet, composed.trades)
        attempts.append(Attempt(raw_output=reply.text, validation=v, stop_reason=reply.stop_reason, usage=reply.usage))
        if v.passed:
            break
        turn = retry_turn(user, reply.text, v.reasons())
    passed = attempts[-1].validation.passed
    return WriteResult(
        customer_id=sheet.customer_id, month=sheet.month, status="passed" if passed else "review",
        recap=copy if passed else None, attempts=attempts, model=cfg.model, effort=cfg.effort,
        max_tokens=cfg.max_tokens, prompt_version=cfg.prompt_version,
        prompt_sha256=hashlib.sha256(system.encode("utf-8")).hexdigest(),
    )


def cost(result: WriteResult, cfg: ModelConfig) -> float:
    p = cfg.prices.get(result.model)
    if not p:
        return 0.0
    total = 0.0
    for a in result.attempts:
        u = a.usage
        total += (u.get("input_tokens", 0) * p["input"] + u.get("output_tokens", 0) * p["output"]
                  + u.get("cache_read_input_tokens", 0) * p.get("cache_read", p["input"])
                  + u.get("cache_creation_input_tokens", 0) * p["input"] * 1.25) / 1e6
    return total
