"""Acceptance: a one-page data/README.md describes each persona's month in plain English.
Also: the validated pricer is used unchanged."""

from __future__ import annotations

import hashlib
import re

from tests.conftest import PERSONA_IDS, REPO

BANNED = ["annotation", "invalidation", "calibration", "thesis", "immutable", "conviction score",
          "resolution event", "came due", "dishonesty", "bite", "sharp", "quiet", "expensive"]
PRICER_SHA256 = "dd59be5881fbb8a6f5414523a78e1a878773a8a3d645aa035452243f0064c292"


def test_readme_one_page_all_personas(fresh):
    text = (fresh / "README.md").read_text(encoding="utf-8")
    assert len(text.splitlines()) <= 60
    assert len(text.split()) <= 950, "keep it to about one page"
    for pid in PERSONA_IDS:
        assert f"**{pid} " in text
    assert "—" not in text and "!" not in text
    for w in BANNED:
        assert not re.search(rf"\b{re.escape(w)}\b", text.lower()), w


def test_pricer_is_the_validated_copy():
    assert hashlib.sha256((REPO / "pricing_reference.py").read_bytes()).hexdigest() == PRICER_SHA256
