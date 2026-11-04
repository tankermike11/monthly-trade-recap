"""The monthly run (pipeline/monthly.py, SPEC 5.3), offline with a fake batch client.

- every persona is prepared, sent in batches, validated, retried once and archived;
  p10 (data-quality issues) needs review;
- round 2 retries failed checks with their reasons, and resends errored requests as they
  were;
- a run stopped with --no-wait resumes to the same result;
- a new run gets the next number and leaves earlier runs untouched;
- batches split by size; batch requests carry no refusal fallback (not supported);
- every archived file is hashed; the review sample is the same for the same run.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from pipeline.monthly import BatchResult, chunk, run_month
from pipeline.write import ModelConfig
from tests.conftest import PERSONA_IDS

MONTH = "2026-10"
CFG = ModelConfig.load()


def narrative(user: str) -> str:
    """A valid recap for any persona: short in a low-activity month."""
    flags = json.loads(user.split("\n\nYour previous draft")[0])["flags"]
    obs = [] if "low_activity" in flags else [{"heading": "You traded steadily", "text": "You opened and closed trades."},
                                                {"heading": "You held some trades", "text": "Some trades stayed open."}]
    return json.dumps({"overall": "You traded this month.", "observations": obs, "subject_line": "Your October"})


class FakeBatches:
    """Behaves like the Batches API: results in any order, done after a few polls."""

    def __init__(self, bad_first=(), errored_first=(), polls_needed=0):
        self.batches, self.polls, self.sent = {}, {}, []
        self.bad_first, self.errored_first, self.polls_needed = set(bad_first), set(errored_first), polls_needed

    def submit(self, requests):
        bid = f"batch_{len(self.batches) + 1}"
        self.batches[bid] = requests
        self.polls[bid] = 0
        self.sent.append(requests)
        return bid

    def ended(self, bid):
        self.polls[bid] += 1
        return self.polls[bid] > self.polls_needed

    def results(self, bid):
        first_round = bid == "batch_1"
        for r in reversed(self.batches[bid]):                      # any order
            user = r["params"]["messages"][0]["content"]
            name = json.loads(user.split("\n\nYour previous draft")[0])["first_name"]
            if first_round and name in self.errored_first:
                yield BatchResult(r["custom_id"], "errored", error="api_error")
            elif first_round and name in self.bad_first:
                yield BatchResult(r["custom_id"], "succeeded", json.dumps(
                    {"overall": "You made 5 trades.", "observations": json.loads(narrative(user))["observations"],
                     "subject_line": "Your October"}), "end_turn", {"input_tokens": 1000, "output_tokens": 200, "model": CFG.model})
            else:
                yield BatchResult(r["custom_id"], "succeeded", narrative(user), "end_turn",
                                  {"input_tokens": 1000, "output_tokens": 200, "model": CFG.model})


@pytest.fixture()
def run(fresh, tmp_path):
    def go(client, **kw):
        return run_month(MONTH, fresh / "raw", tmp_path / "archive", CFG, client=client, sleep=lambda s: None, **kw)
    go.archive = tmp_path / "archive" / MONTH
    return go


def test_full_run_archives_every_customer(run):
    client = FakeBatches()
    s = run(client)
    assert s["customers"] == len(PERSONA_IDS) and s["passed"] == len(PERSONA_IDS)
    assert list(s["review_reasons"]) == ["p10"] and "data quality" in s["review_reasons"]["p10"][0]
    run_dir = run.archive / "run-0001"
    for cid in PERSONA_IDS:
        folder = run_dir / "customers" / cid
        for name in ("facts.json", "stories.json", "request.txt", "attempt-1.json", "recap.json", "recap.txt", "hashes.json"):
            assert (folder / name).exists(), (cid, name)
        hashes = json.loads((folder / "hashes.json").read_text(encoding="utf-8"))
        for name, digest in hashes.items():
            assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == digest
    assert (run_dir / "gallery.html").exists() and (run_dir / "summary.json").exists()
    assert json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))["phase"] == "finalized"


def test_batch_requests_have_no_fallback_and_cache_the_prompt(run):
    client = FakeBatches()
    run(client)
    params = client.sent[0][0]["params"]
    assert "fallbacks" not in params and "betas" not in params
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["output_config"]["format"]["schema"]["required"] == ["overall", "observations", "subject_line"]
    assert len({r["custom_id"] for r in client.sent[0]}) == len(PERSONA_IDS)


def test_round_two_retries_failures(run):
    client = FakeBatches(bad_first={"Jordan"}, errored_first={"Alex"})
    s = run(client)
    assert s["passed"] == len(PERSONA_IDS) and s["passed_first_try"] == len(PERSONA_IDS) - 2
    second = {json.loads(r["params"]["messages"][0]["content"].split("\n\nYour previous draft")[0])["first_name"]:
              r["params"]["messages"][0]["content"] for r in client.sent[1]}
    assert set(second) == {"Jordan", "Alex"}
    assert "failed these checks" in second["Jordan"] and "no_raw_numbers" in second["Jordan"]
    assert "failed these checks" not in second["Alex"]                 # errored: resent as it was


def test_no_wait_then_resume(run):
    client = FakeBatches(polls_needed=3)
    assert run(client, no_wait=True) is None
    m = json.loads((run.archive / "run-0001" / "manifest.json").read_text(encoding="utf-8"))
    assert m["phase"] == "round1_submitted"
    s = run(client, resume="run-0001")
    assert s["passed"] == len(PERSONA_IDS) and s["run_id"] == "run-0001"


def test_a_new_run_leaves_earlier_runs_untouched(run):
    run(FakeBatches(), only=["p09"])
    first = run.archive / "run-0001" / "customers" / "p09" / "recap.json"
    before = first.read_bytes()
    s = run(FakeBatches(), only=["p09"])
    assert s["run_id"] == "run-0002" and first.read_bytes() == before


def test_review_sample_is_seeded_by_run(run):
    a = run(FakeBatches(), sample_rate=0.3)
    sample = a["sample"]
    assert len(sample) == 9 and "p10" not in sample          # at least 10 are sampled; only 9 are ready
    b = json.loads((run.archive / "run-0001" / "summary.json").read_text(encoding="utf-8"))
    assert b["sample"] == sample


def test_batches_split_by_size():
    reqs = [{"custom_id": f"c{i}", "params": {"x": "y" * 1000}} for i in range(10)]
    parts = chunk(reqs, max_bytes=3500)
    assert [len(p) for p in parts] == [3, 3, 3, 1]
    assert chunk(reqs, max_count=4) == [reqs[:4], reqs[4:8], reqs[8:]]
