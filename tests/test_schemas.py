"""Every raw folder loads into the validated schemas; answer keys load too."""

from __future__ import annotations

import json
from decimal import Decimal

from mock.market import calendar as cal
from schemas.answer_key import AnswerKey
from tests.conftest import MONTHS, PERSONA_IDS


def test_raw_months_validate(raw):
    assert len(raw) == len(MONTHS) * len(PERSONA_IDS)


def test_fills_fall_inside_their_month(raw):
    for (m, p), rm in raw.items():
        for f in rm.fills:
            assert cal.ny_date(f.ts_utc).strftime("%Y-%m") == m, f"{f.exec_id} outside {m}"


def test_missing_fees_only_where_planted(raw):
    missing = [(m, p, f.exec_id) for (m, p), rm in raw.items() for f in rm.fills
               if f.fees is None or f.commission is None]
    assert len(missing) == 1
    assert missing[0][:2] == ("2026-10", "p10")


def test_sessions_match_clock(raw):
    for rm in raw.values():
        for f in rm.fills:
            assert f.session == cal.session_of(f.ts_utc)


def test_answer_keys_validate(fresh):
    for m in MONTHS:
        for p in PERSONA_IDS:
            AnswerKey.model_validate(json.loads((fresh / "expected" / m / f"{p}.json").read_text(encoding="utf-8")))


def test_account_values_match_snapshots(raw):
    """start/end value = cash + marked positions (futures count only their open P&L)."""
    for rm in raw.values():
        for snap, value in ((rm.positions_start, rm.account.start_value), (rm.positions_end, rm.account.end_value)):
            v = snap.cash
            for l in snap.lines:
                mv = l.qty * l.mark * l.multiplier
                v += (mv - l.cost_basis) if l.asset_class == "future" else mv
            assert abs(v - value) < Decimal("0.02"), f"{rm.account.customer_id}: {v} vs {value}"


def test_month_chain(raw):
    """November starts exactly where October ended."""
    for p in PERSONA_IDS:
        oct_, nov = raw[("2026-10", p)], raw[("2026-11", p)]
        assert oct_.account.end_value == nov.account.start_value
        assert oct_.positions_end.lines == nov.positions_start.lines
        assert oct_.positions_end.cash == nov.positions_start.cash
