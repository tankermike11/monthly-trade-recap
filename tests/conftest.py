"""Shared fixtures. One fresh generation (seed 42, October + November) is built per test
session in a temp folder; tests read from it rather than trusting the committed data/."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mock import generator
from mock.io import read_raw_month
from mock.market import quotes as Q
from mock.market.paths import World

REPO = Path(__file__).resolve().parent.parent
MONTHS = ["2026-10", "2026-11"]
SEED = 42
PERSONA_IDS = [f"p{i:02d}" for i in range(1, 11)]


@pytest.fixture(scope="session")
def fresh(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("gen_a")
    Q._theo_cache.clear()
    generator.generate(SEED, MONTHS, out)
    return out


@pytest.fixture(scope="session")
def world() -> World:
    return World(SEED)


@pytest.fixture(scope="session")
def raw(fresh):
    """{(month, pid): RawMonth}"""
    return {(m, p): read_raw_month(fresh / "raw" / m / p) for m in MONTHS for p in PERSONA_IDS}


@pytest.fixture(scope="session")
def keys(fresh):
    """{(month, pid): answer-key dict}"""
    return {(m, p): json.loads((fresh / "expected" / m / f"{p}.json").read_text(encoding="utf-8"))
            for m in MONTHS for p in PERSONA_IDS}


def facts(key: dict) -> dict:
    return {f["id"]: f["value"] for f in key["headline_facts"]}
