"""The pipeline must not import the mock generator: the answer keys come from the
generator's ledger, and a pipeline that reused it would only agree with itself."""

from __future__ import annotations

import ast
import subprocess
import sys
from datetime import UTC, datetime, timedelta

from pipeline import nytime
from tests.conftest import REPO


def test_no_pipeline_module_imports_mock():
    for path in sorted((REPO / "pipeline").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                assert n.split(".")[0] != "mock", f"{path.name} imports {n}"


def test_importing_the_pipeline_loads_no_mock_module():
    code = ("import sys, pipeline.ingest, pipeline.classify, pipeline.link, pipeline.nytime;"
            "print([m for m in sys.modules if m == 'mock' or m.startswith('mock.')])")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


def test_ny_dates_across_daylight_saving():
    # Nov 1 2026: clocks fall back at 06:00 UTC (02:00 EDT -> 01:00 EST)
    assert nytime.ny(datetime(2026, 11, 1, 5, 59, tzinfo=UTC)) == datetime(2026, 11, 1, 1, 59)
    assert nytime.ny(datetime(2026, 11, 1, 6, 0, tzinfo=UTC)) == datetime(2026, 11, 1, 1, 0)
    # Mar 8 2026: clocks spring forward at 07:00 UTC
    assert nytime.ny(datetime(2026, 3, 8, 6, 59, tzinfo=UTC)).hour == 1
    assert nytime.ny(datetime(2026, 3, 8, 7, 0, tzinfo=UTC)).hour == 3
    # a late-evening trade belongs to the New York day, not the UTC day
    assert str(nytime.ny_date(datetime(2026, 10, 15, 2, 30, tzinfo=UTC))) == "2026-10-14"


def test_ny_midnight():
    for d, hour in [("2026-10-15", 4), ("2026-11-15", 5), ("2026-11-01", 4), ("2026-11-02", 5)]:
        t = nytime.ny_midnight_utc(datetime.fromisoformat(d).date())
        assert t.hour == hour and nytime.ny(t) == datetime.fromisoformat(d)
        assert nytime.ny_date(t - timedelta(seconds=1)) < nytime.ny_date(t)
