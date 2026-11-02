"""Acceptance: every persona's YAML is under 60 lines and readable by a non-engineer."""

from __future__ import annotations

import re

import pytest

from mock.generator import load_persona, persona_files
from mock.persona import PersonaConfig

# Copy rules from CLAUDE.md, applied to the plain-English summaries.
BANNED = ["annotation", "invalidation", "calibration", "thesis", "immutable", "conviction score",
          "resolution event", "came due", "dishonesty", "bite", "sharp", "quiet", "expensive"]
JARGON = ["dte", "ivr", "delta", "untested", "debit diagonal"]
FILES = persona_files()


def test_ten_personas():
    assert [p.name[:3] for p in FILES] == [f"p{i:02d}" for i in range(1, 11)]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_under_60_lines(path):
    n = len(path.read_text(encoding="utf-8").splitlines())
    assert n < 60, f"{path.name} has {n} lines"


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_loads_and_validates(path):
    data = load_persona(path)
    PersonaConfig.model_validate(data)
    assert path.name.startswith(data["id"])
    assert path.read_text(encoding="utf-8").startswith(f"# Persona {int(data['id'][1:])}:")


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_summary_is_plain_english(path):
    s = " ".join(load_persona(path)["summary"].split()).lower()
    for w in BANNED + JARGON:
        assert not re.search(rf"\b{re.escape(w)}\b", s), f"{path.name}: summary uses '{w}'"
    assert "—" not in s and "!" not in s


def _keys(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k
            yield from _keys(v)
    elif isinstance(node, list):
        for v in node:
            yield from _keys(v)


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_keys_are_plain_words(path):
    """Every setting name is lowercase words a non-engineer can read (no codes or abbreviations)."""
    for key in _keys(load_persona(path)):
        assert re.fullmatch(r"[a-z]+(_[a-z0-9]+)*", str(key)), f"{path.name}: unclear key {key!r}"
        assert str(key) not in {"dte", "ivr", "tp", "sl", "qty", "px"}, f"{path.name}: jargon key {key!r}"
