"""The HTML email (pipeline/render_email.py, templates/recap.html.j2, SPEC 5.1), offline.

- every persona renders with no placeholder left, the required fine print, 640 px wide,
  no scripts and no image but the logo, which has a transparent background (email-safe);
- everything Claude wrote is escaped;
- a red month names its result once, beside the market;
- charts come from code: bar widths add to 100, the dots match wins and losses;
- a card never repeats in a sentence the value it shows big, and a card with a chart
  still shows when its sentences were all repeats.
"""

from __future__ import annotations

import re

import pytest

from pipeline import render_email as E
from pipeline.compose import compose
from pipeline.run import facts_for
from schemas.recap import RecapCopy
from tests.conftest import PERSONA_IDS
from tests.recap_fixtures import good

MONTH = "2026-10"
GENERIC = {"overall": "You traded this month.", "subject_line": "Your October",
           "observations": [{"heading": "You traded steadily", "text": "You opened and closed trades."}]}


@pytest.fixture(scope="module")
def cases(fresh):
    return {p: facts_for(fresh / "raw", MONTH, p) for p in PERSONA_IDS}


def html_for(cases, p, copy=None):
    linked, sheet = cases[p]
    return E.render_html(RecapCopy.model_validate(copy or (good() if p == "p09" else GENERIC)), sheet, linked)


@pytest.mark.parametrize("p", PERSONA_IDS)
def test_every_persona_renders_email_safe(cases, p):
    html = html_for(cases, p)
    assert "{{" not in html
    imgs = re.findall(r"<img\b[^>]*>", html, re.I)
    assert len(imgs) == 1 and 'alt="Lossdog"' in imgs[0] and 'src="data:image/png;base64,' in imgs[0]
    assert len(re.findall(r"<script", html, re.I)) == 0
    assert 'width="640"' in html and "max-width:640px" in html
    for must in ("This isn't your account statement.", "How we counted.", "is not advice",
                 "[Required disclosures]", "Stop monthly recaps", 'name="color-scheme" content="dark"'):
        assert must in html, must


def test_logo_has_a_transparent_background():
    """The logo is an 8-bit palette PNG whose top-left pixel is fully transparent (read with
    the standard library: the first byte of the first row is that pixel's palette index)."""
    import struct  # noqa: PLC0415
    import zlib  # noqa: PLC0415

    data = E.LOGO.read_bytes()
    assert data[:4] == b"\x89PNG"
    chunks, i = {}, 8
    while i < len(data):
        n, kind = struct.unpack(">I4s", data[i:i + 8])
        chunks[kind] = chunks.get(kind, b"") + data[i + 8:i + 8 + n]
        i += 12 + n
    _, _, depth, color_type = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert (depth, color_type) == (8, 3) and b"tRNS" in chunks
    corner = zlib.decompress(chunks[b"IDAT"])[1]
    alpha = chunks[b"tRNS"]
    assert corner < len(alpha) and alpha[corner] == 0


def test_claude_text_is_escaped(cases):
    copy = dict(GENERIC, observations=[{"heading": "<b>bold</b>", "text": "You traded <i>a lot</i>."}])
    html = html_for(cases, "p04", copy)
    assert "<b>bold</b>" not in html and "&lt;b&gt;bold&lt;/b&gt;" in html


def test_red_month_names_the_result_once_beside_the_market(cases):
    linked, sheet = cases["p09"]
    shown = {f.id: f.display for f in sheet.facts}
    html = html_for(cases, "p09")
    assert html.count(shown["month_return"]) == 1
    line = next(l for l in html.splitlines() if shown["month_return"] in l)
    assert shown["spy_move"] in line


def test_charts_come_from_the_facts(cases):
    linked, sheet = cases["p08"]
    facts = {f.id: f for f in compose(sheet, linked).sheet.facts}
    assert sum(a["width"] for a in E.asset_bar(facts)) == 100
    linked, sheet = cases["p06"]
    facts = {f.id: f for f in compose(sheet, linked).sheet.facts}
    dots = E.result_dots(facts)
    assert (dots["win_dots"], dots["loss_dots"]) == (int(facts["wins"].value), int(facts["losses"].value)) == (18, 5)
    h = E.halves(facts)
    assert h["first_width"] + h["second_width"] == 100 and (h["first"], h["second"]) == (11, 12)


def test_many_trades_share_a_dot(cases):
    linked, sheet = cases["p02"]
    facts = {f.id: f for f in compose(sheet, linked).sheet.facts}
    dots = E.result_dots(facts)
    assert dots["win_dots"] + dots["loss_dots"] <= E.MAX_DOTS + 1


def test_cards_do_not_repeat_their_big_number_but_keep_their_charts(cases):
    linked, sheet = cases["p04"]
    ctx = E.context(RecapCopy.model_validate(GENERIC), sheet, linked)
    big = ctx["getting_in"]["big"]["display"]
    assert not any(big in line for line in ctx["getting_in"]["lines"])
    assert ctx["managing"]["bars"] and ctx["managing"]["credit"]
    html = E.render_html(RecapCopy.model_validate(GENERIC), sheet, linked)
    assert "MANAGING" in html and ">13</td>" in html


def test_short_month_has_no_how_you_traded(cases):
    html = html_for(cases, "p01")
    assert "How you traded" not in html and "TRADES THAT ENDED" not in html
