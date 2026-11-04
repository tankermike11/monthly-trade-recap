"""Stage 6: the monthly run (SPEC 5.3). Every customer for a month, through the Message
Batches API, archived; nothing is sent.

    python -m pipeline.run --month 2026-11 --batch [--resume run-0003] [--no-wait] [--sample-rate 0.05]

Built for thousands of customers:
- prepare: facts, trades and the model request for each customer, written to the archive
  as it goes;
- round 1: requests go out in batches split by size (a batch holds at most 100,000
  requests or 256 MB; requests carry each customer's trade list), then the results are
  collected and validated;
- claim check: after each writing round, a batch that checks every sentence of the recaps
  that passed the validator against the trades (pipeline/claimcheck.py);
- round 2: one retry batch for the failures (failed checks, the claim check included, get
  the reasons appended, as in pipeline/write.py; errored or expired requests are resent
  unchanged), then its claim check;
- finalize: recap.txt for every recap that passed, a hash of every file, a summary, and a
  review gallery of every recap that needs review plus a seeded random sample of the rest.

Archive: archive/<month>/run-NNNN/, numbered rather than timestamped so nothing written
depends on the clock, and never overwritten: a new run gets the next number. The run's
state lives in manifest.json, so the same command resumes after an interruption (or after
--no-wait). Batch prices are half the standard ones. Server-side refusal fallback is not
available in batches, so a declined request goes to review.
"""

from __future__ import annotations

import hashlib
import json
import random
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from pipeline.compose import compose
from pipeline.ingest import customers
from pipeline.run import facts_for
from pipeline.validate_copy import validate
from pipeline.write import ModelConfig, retry_turn, system_prompt, user_turn
from schemas.recap import Attempt, CheckResult, ValidationResult, WriteResult, output_schema

REPO = Path(__file__).resolve().parent.parent
MAX_BATCH_BYTES = 200 * 1024 * 1024        # under the API's 256 MB, with room for overhead
MAX_BATCH_REQUESTS = 100_000
BATCH_DISCOUNT = 0.5
POLL_SECONDS = 60


def _dump(obj) -> str:
    return json.dumps(obj, sort_keys=True, indent=1, ensure_ascii=False) + "\n"


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# The batch client: the real one wraps the SDK; tests pass a fake with the same methods
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BatchResult:
    custom_id: str
    kind: str                  # succeeded | errored | canceled | expired
    text: str = ""
    stop_reason: str | None = None
    usage: dict | None = None
    error: str = ""


class AnthropicBatches:
    def __init__(self, client=None):
        import anthropic

        self.client = client or anthropic.Anthropic()

    def submit(self, requests: list[dict]) -> str:
        return self.client.messages.batches.create(requests=requests).id

    def ended(self, batch_id: str) -> bool:
        return self.client.messages.batches.retrieve(batch_id).processing_status == "ended"

    def results(self, batch_id: str) -> Iterator[BatchResult]:
        for r in self.client.messages.batches.results(batch_id):
            kind = r.result.type
            if kind != "succeeded":
                err = getattr(getattr(r.result, "error", None), "type", "") or ""
                yield BatchResult(r.custom_id, kind, error=str(err))
                continue
            m = r.result.message
            u = m.usage
            yield BatchResult(r.custom_id, kind, next((b.text for b in m.content if b.type == "text"), ""),
                              m.stop_reason, {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                                              "cache_read_input_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
                                              "cache_creation_input_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
                                              "model": m.model})


# ---------------------------------------------------------------------------
# Run folder and manifest
# ---------------------------------------------------------------------------

def new_run_dir(archive_root: Path, month: str) -> Path:
    month_dir = archive_root / month
    taken = sorted(int(p.name.split("-")[1]) for p in month_dir.glob("run-*") if p.name.split("-")[1].isdigit()) \
        if month_dir.exists() else []
    run_dir = month_dir / f"run-{(taken[-1] if taken else 0) + 1:04d}"
    run_dir.mkdir(parents=True)
    return run_dir


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _save_manifest(run_dir: Path, m: dict) -> None:
    _write(run_dir / "manifest.json", _dump(m))


def _params(cfg: ModelConfig, system: str, user: str) -> dict:
    return {"model": cfg.model, "max_tokens": cfg.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": cfg.effort, "format": {"type": "json_schema", "schema": output_schema()}}}


def chunk(requests: list[dict], max_bytes: int = MAX_BATCH_BYTES, max_count: int = MAX_BATCH_REQUESTS) -> list[list[dict]]:
    out, cur, size = [], [], 0
    for r in requests:
        n = len(json.dumps(r, ensure_ascii=False).encode("utf-8"))
        if cur and (size + n > max_bytes or len(cur) >= max_count):
            out.append(cur)
            cur, size = [], 0
        cur.append(r)
        size += n
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# Phases
# ---------------------------------------------------------------------------

def prepare(run_dir: Path, month: str, raw_root: Path, cfg: ModelConfig, only: list[str] | None = None) -> dict:
    system = system_prompt(cfg.prompt_version)
    ids = only or customers(raw_root, month)
    m = {"month": month, "run_id": run_dir.name, "raw_root": str(raw_root), "model": cfg.model, "effort": cfg.effort,
         "max_tokens": cfg.max_tokens, "prompt_version": cfg.prompt_version,
         "prompt_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
         "customers": {}, "rounds": [], "phase": "prepared"}
    for i, cid in enumerate(ids, start=1):
        folder = run_dir / "customers" / cid
        entry = {"custom_id": f"c{i:06d}"}
        try:
            linked, sheet, c = _customer(run_dir, raw_root, month, cid)
            _write(folder / "facts.json", _dump(sheet.model_dump(mode="json")))
            _write(folder / "stories.json", _dump({"stories": [s.model_dump(mode="json") for s in linked.stories],
                                                    "data_quality_issues": [{"issue": d.issue, "refs": list(d.refs)}
                                                                            for d in linked.data_quality_issues]}))
            _write(folder / "request.txt", user_turn(c))
        except Exception as e:  # noqa: BLE001  (one customer's bad data must not stop the month)
            entry["error"] = f"{type(e).__name__}: {str(e).splitlines()[0][:300] if str(e) else ''}"
        m["customers"][cid] = entry
    _save_manifest(run_dir, m)
    return m


def _phases(cfg: ModelConfig) -> list[tuple[str, int]]:
    """write 1, check 1, write 2, check 2 (no checks when the claim check is off)."""
    return [("write", 1), ("check", 1), ("write", 2), ("check", 2)] if cfg.claim_check else [("write", 1), ("write", 2)]


def _check_params(cfg: ModelConfig, user: str) -> dict:
    from pipeline import claimcheck
    return {"model": cfg.claim_check["model"], "max_tokens": 8000,
            "system": [{"type": "text", "text": claimcheck.system_prompt(), "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {"effort": cfg.claim_check.get("effort", "medium"),
                              "format": {"type": "json_schema", "schema": claimcheck.OUTPUT_SCHEMA}}}


def submit_phase(run_dir: Path, m: dict, cfg: ModelConfig, client, kind: str, round_no: int) -> None:
    """write 1: every prepared customer. check N: every recap from write N that passed the
    validator. write 2: every customer whose round-1 recap failed the validator or the
    claim check (with the reasons), or whose request errored (resent as it was)."""
    from pipeline import claimcheck
    from schemas.recap import RecapCopy
    system = system_prompt(cfg.prompt_version)
    requests = []
    for cid, e in m["customers"].items():
        if "error" in e:
            continue
        folder = run_dir / "customers" / cid
        if kind == "check":
            att = folder / f"attempt-{round_no}.json"
            if not att.exists():
                continue
            rec = _load(att)
            if not rec["passed"] or rec.get("checked"):
                continue
            _, _, c = _customer(run_dir, Path(m["raw_root"]), m["month"], cid)
            copy = RecapCopy.model_validate_json(rec["raw_output"])
            requests.append({"custom_id": e["custom_id"], "params": _check_params(cfg, claimcheck.user_turn(copy, c))})
            continue
        user = (folder / "request.txt").read_text(encoding="utf-8")
        if round_no == 2:
            first = folder / "attempt-1.json"
            if not first.exists():
                continue
            a1 = _load(first)
            if a1["passed"]:
                continue
            if a1["kind"] == "succeeded":
                user = retry_turn(user, a1["raw_output"], a1["reasons"])
        requests.append({"custom_id": e["custom_id"], "params": _params(cfg, system, user)})
    batches = [client.submit(part) for part in chunk(requests)] if requests else []
    m["rounds"].append({"kind": kind, "round": round_no, "requests": len(requests), "batch_ids": batches,
                        "collected": False})
    m["phase"] = f"{kind}{round_no}_submitted"
    _save_manifest(run_dir, m)


def wait(phase: dict, client, poll_seconds: int = POLL_SECONDS, sleep=time.sleep) -> None:
    pending = set(phase["batch_ids"])
    while pending:
        pending = {b for b in pending if not client.ended(b)}
        if pending:
            sleep(poll_seconds)


def collect_phase(run_dir: Path, m: dict, client, index: int) -> None:
    phase = m["rounds"][index]
    kind, round_no = phase["kind"], phase["round"]
    by_custom = {e["custom_id"]: cid for cid, e in m["customers"].items()}
    seen = set()
    for bid in phase["batch_ids"]:
        for r in client.results(bid):
            cid = by_custom[r.custom_id]
            seen.add(cid)
            folder = run_dir / "customers" / cid
            if kind == "check":
                _record_check(run_dir, m, cid, round_no, r)
                continue
            if r.kind != "succeeded":
                rec = {"kind": r.kind, "passed": False, "raw_output": "", "stop_reason": None, "usage": {},
                       "reasons": [f"batch_{r.kind}: {r.error or 'no result'}"], "checks": []}
            elif r.stop_reason == "refusal":
                rec = {"kind": r.kind, "passed": False, "raw_output": r.text, "stop_reason": r.stop_reason,
                       "usage": r.usage or {}, "reasons": ["model_refused: the model declined the request"],
                       "checks": [["model_refused", False]]}
            else:
                full, trades = _composed(run_dir, cid, m)
                _, v = validate(r.text, full, trades)
                rec = {"kind": r.kind, "passed": v.passed, "raw_output": r.text, "stop_reason": r.stop_reason,
                       "usage": r.usage or {}, "reasons": v.reasons(), "checks": [[c.name, c.passed] for c in v.checks]}
            _write(folder / f"attempt-{round_no}.json", _dump(rec))
    if kind == "check":                     # a recap the check never returned for is not sent unchecked
        for cid in m["customers"]:
            att = run_dir / "customers" / cid / f"attempt-{round_no}.json"
            if cid not in seen and att.exists():
                rec = _load(att)
                if rec["passed"] and not rec.get("checked"):
                    _record_check(run_dir, m, cid, round_no, None)
    phase["collected"] = True
    m["phase"] = f"{kind}{round_no}_collected"
    _save_manifest(run_dir, m)


def _record_check(run_dir: Path, m: dict, cid: str, round_no: int, r) -> None:
    from pipeline import claimcheck
    from schemas.recap import RecapCopy
    folder = run_dir / "customers" / cid
    rec = _load(folder / f"attempt-{round_no}.json")
    reasons, usage = None, {}
    if r is not None and r.kind == "succeeded" and r.stop_reason != "refusal":
        try:
            verdicts = claimcheck.parse(r.text)
            _, _, c = _customer(run_dir, Path(m["raw_root"]), m["month"], cid)
            res = claimcheck.result(RecapCopy.model_validate_json(rec["raw_output"]), c, verdicts)
            reasons = res.reasons
        except (ValueError, KeyError, TypeError):
            reasons = ["the claim check returned an unreadable answer"]
        usage = r.usage or {}
    else:
        reasons = [f"the claim check did not return ({r.kind if r is not None else 'no result'})"]
    rec["checks"].append(["claims_supported", not reasons])
    rec["reasons"] += [f"claims_supported: {x}" for x in reasons]
    rec["passed"] = rec["passed"] and not reasons
    rec["usage"]["claim_check"] = usage
    rec["checked"] = True
    _write(folder / f"attempt-{round_no}.json", _dump(rec))


_CACHE: dict[tuple[str, str], tuple] = {}


def _customer(run_dir: Path, raw_root: Path, month: str, cid: str):
    """(linked, sheet, composed) for one customer, from the raw data the run was prepared
    from. Cached per run and customer, so preparing, validating and rendering compute the
    facts once in a process; a resumed run rebuilds them from the same raw data."""
    key = (str(run_dir), cid)
    if key not in _CACHE:
        linked, sheet = facts_for(raw_root, month, cid)
        _CACHE[key] = (linked, sheet, compose(sheet, linked))
    return _CACHE[key]


def _composed(run_dir: Path, cid: str, m: dict):
    _, _, c = _customer(run_dir, Path(m["raw_root"]), m["month"], cid)
    return c.sheet, c.trades


def _attempt(rec: dict) -> Attempt:
    checks = [CheckResult(name=n, passed=p, reasons=[r.split(": ", 1)[1] for r in rec["reasons"] if r.startswith(n + ":")])
              for n, p in rec["checks"]] or [CheckResult(name=rec["reasons"][0].split(":")[0], passed=False,
                                                         reasons=rec["reasons"])]
    return Attempt(raw_output=rec["raw_output"], validation=ValidationResult(passed=rec["passed"], checks=checks),
                   stop_reason=rec["stop_reason"], usage=rec["usage"])


def finalize(run_dir: Path, m: dict, cfg: ModelConfig, sample_rate: float = 0.05, min_sample: int = 10) -> dict:
    from pipeline.gallery import build as build_gallery
    from pipeline.render import render_text
    from pipeline.write import cost

    results: dict[str, WriteResult] = {}
    review: dict[str, list[str]] = {}
    spent = 0.0
    for cid, e in m["customers"].items():
        folder = run_dir / "customers" / cid
        if "error" in e:
            review[cid] = [f"could not prepare: {e['error']}"]
            continue
        recs = [_load(folder / f"attempt-{i}.json") for i in (1, 2) if (folder / f"attempt-{i}.json").exists()]
        if not recs:
            review[cid] = ["no result was returned for this customer"]
            continue
        attempts = [_attempt(r) for r in recs]
        passed = recs[-1]["passed"]
        copy = None
        if passed:
            full, trades = _composed(run_dir, cid, m)
            copy, _ = validate(recs[-1]["raw_output"], full, trades)
        res = WriteResult(customer_id=cid, month=m["month"], status="passed" if passed else "review", recap=copy,
                          attempts=attempts, model=m["model"], effort=m["effort"], max_tokens=m["max_tokens"],
                          prompt_version=m["prompt_version"], prompt_sha256=m["prompt_sha256"])
        results[cid] = res
        spent += cost(res, cfg) * BATCH_DISCOUNT
        _write(folder / "recap.json", _dump(res.model_dump(mode="json")))
        facts = _load(folder / "facts.json")
        issues = [i["issue"] for i in facts.get("data_quality_issues", [])]
        if passed:
            linked, sheet, _ = _customer(run_dir, Path(m["raw_root"]), m["month"], cid)
            _write(folder / "recap.txt", render_text(copy, sheet, linked))
            if issues:
                review[cid] = [f"data quality: {', '.join(issues)}"]
        else:
            review[cid] = recs[-1]["reasons"][:5] or ["failed the checks twice"]
        hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.iterdir())
                  if p.is_file() and p.name != "hashes.json"}
        _write(folder / "hashes.json", _dump(hashes))

    ready = sorted(cid for cid, r in results.items() if r.status == "passed" and cid not in review)
    rng = random.Random(f"{m['month']}-{m['run_id']}")          # seeded: the same run gives the same sample
    k = min(len(ready), max(min_sample, round(len(ready) * sample_rate)))
    sample = sorted(rng.sample(ready, k)) if ready else []
    shown = {cid: results[cid] for cid in sorted(set(review) | set(sample)) if cid in results}
    _write(run_dir / "gallery.html", build_gallery(m["month"], Path(m["raw_root"]), shown,
                                                   only=sorted(set(review) | set(sample))))
    first_try = sum(1 for r in results.values() if r.status == "passed" and len(r.attempts) == 1)
    summary = {"month": m["month"], "run_id": m["run_id"], "customers": len(m["customers"]),
               "passed": sum(r.status == "passed" for r in results.values()), "passed_first_try": first_try,
               "ready": len(ready), "needs_review": len(review), "review_reasons": review,
               "sample_size": len(sample), "sample": sample, "cost_usd": round(spent, 2),
               "model": m["model"], "prompt_version": m["prompt_version"]}
    _write(run_dir / "summary.json", _dump(summary))
    m["phase"] = "finalized"
    _save_manifest(run_dir, m)
    return summary


def run_month(month: str, raw_root: Path, archive_root: Path, cfg: ModelConfig, client=None, resume: str | None = None,
              no_wait: bool = False, sample_rate: float = 0.05, only: list[str] | None = None,
              sleep=time.sleep, poll_seconds: int = POLL_SECONDS) -> dict | None:
    """Run (or resume) a month. Returns the summary when finished, or None when it stopped
    early because of --no-wait (run the same command with --resume to continue)."""
    client = client or AnthropicBatches()
    if resume:
        run_dir = archive_root / month / resume
        m = _load(run_dir / "manifest.json")
    else:
        run_dir = new_run_dir(archive_root, month)
        m = prepare(run_dir, month, raw_root, cfg, only)
    for i, (kind, round_no) in enumerate(_phases(cfg)):
        if m["phase"] == "finalized":
            break
        if len(m["rounds"]) <= i:
            submit_phase(run_dir, m, cfg, client, kind, round_no)
        phase = m["rounds"][i]
        if not phase["collected"]:
            if no_wait and not all(client.ended(b) for b in phase["batch_ids"]):
                print(f"{run_dir.name}: {kind} {round_no} still processing; resume with --resume {run_dir.name}")
                return None
            wait(phase, client, poll_seconds, sleep)
            collect_phase(run_dir, m, client, i)
    return _load(run_dir / "summary.json") if m["phase"] == "finalized" else finalize(run_dir, m, cfg, sample_rate)


def describe(summary: dict) -> str:
    lines = [f"{summary['month']} {summary['run_id']}: {summary['customers']} customers, "
             f"{summary['passed']} passed ({summary['passed_first_try']} on the first try), "
             f"{summary['ready']} ready, {summary['needs_review']} need review, cost ${summary['cost_usd']:.2f}",
             f"review gallery: every recap that needs review plus a sample of {summary['sample_size']} ready ones"]
    for cid, reasons in list(summary["review_reasons"].items())[:20]:
        lines.append(f"  {cid}: {'; '.join(reasons)[:160]}")
    if len(summary["review_reasons"]) > 20:
        lines.append(f"  ... and {len(summary['review_reasons']) - 20} more in summary.json")
    return "\n".join(lines)

