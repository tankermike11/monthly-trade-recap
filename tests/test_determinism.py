"""Acceptance: the same seed produces byte-identical output."""

from __future__ import annotations

import hashlib
from pathlib import Path

from mock import generator
from mock.market import quotes as Q
from tests.conftest import MONTHS, REPO, SEED


def digest_tree(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_same_seed_is_byte_identical(fresh, tmp_path):
    Q._theo_cache.clear()                    # recompute every price from scratch
    generator.generate(SEED, MONTHS, tmp_path)
    a, b = digest_tree(fresh), digest_tree(tmp_path)
    assert a.keys() == b.keys()
    diff = [k for k in a if a[k] != b[k]]
    assert not diff, f"files differ between two runs with seed {SEED}: {diff[:5]}"
    assert len(a) >= 2 * 10 * 8, "expected raw files and answer keys for 10 personas x 2 months"


def test_committed_data_matches_generator(fresh):
    """data/ in the repo is exactly what `python -m mock.generator` writes today."""
    committed = digest_tree(REPO / "data")
    produced = digest_tree(fresh)
    assert committed == produced, "data/ is stale: rerun python -m mock.generator --month 2026-10 --month 2026-11"


def test_different_seed_differs(tmp_path):
    generator.generate(SEED + 1, ["2026-10"], tmp_path / "s43", personas=["p04"])
    generator.generate(SEED, ["2026-10"], tmp_path / "s42", personas=["p04"])
    a = (tmp_path / "s43" / "raw/2026-10/p04/fills.parquet").read_bytes()
    b = (tmp_path / "s42" / "raw/2026-10/p04/fills.parquet").read_bytes()
    assert a != b
