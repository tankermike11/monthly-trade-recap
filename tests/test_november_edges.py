"""The November 2026 calendar edge cases are present in the data."""

from __future__ import annotations

from datetime import date, datetime, timezone

from mock.market import calendar as cal

NOV = "2026-11"


def test_sunday_evening_futures_trade_after_clock_change(raw):
    fills = raw[(NOV, "p07")].fills
    sunday = [f for f in fills if cal.ny_date(f.ts_utc) == date(2026, 11, 1)]
    assert sunday, "p07 should trade on Sunday evening Nov 1"
    first = min(sunday, key=lambda f: f.ts_utc)
    assert first.ts_utc == datetime(2026, 11, 2, 0, 30, tzinfo=timezone.utc)   # 18:30 Chicago = 19:30 New York, UTC-5
    assert first.session == "overnight"
    oct_sunday = [f for f in raw[("2026-10", "p07")].fills if cal.ny_date(f.ts_utc) == date(2026, 10, 25)]
    assert min(f.ts_utc for f in oct_sunday) == datetime(2026, 10, 25, 23, 30, tzinfo=timezone.utc)  # UTC-4


def test_pre_market_trades_on_nov_2(raw):
    pre = [f for f in raw[(NOV, "p02")].fills
           if cal.ny_date(f.ts_utc) == date(2026, 11, 2) and f.session == "pre_market"]
    assert len(pre) >= 2


def test_nothing_trades_on_thanksgiving(raw):
    for (m, p), rm in raw.items():
        for f in rm.fills:
            if f.asset_class != "crypto":
                assert cal.ny_date(f.ts_utc) != date(2026, 11, 26), f"{p} traded on a holiday"


def test_half_day_spx(raw, keys):
    rm = raw[(NOV, "p06")]
    day = [f for f in rm.fills if cal.ny_date(f.ts_utc) == date(2026, 11, 27)]
    assert day, "p06 should trade on the half day"
    assert all(cal.ny(f.ts_utc).hour < 13 for f in day)
    settles = [e for e in rm.events if cal.ny_date(e.ts_utc) == date(2026, 11, 27)]
    for e in settles:
        assert cal.ny(e.ts_utc).strftime("%H:%M") == "13:00"


def test_trade_opened_oct_30_closed_nov_3(keys):
    oct_story = next(s for s in keys[("2026-10", "p05")]["stories"]
                     if s["underlying"] == "IWM" and s["opened_date"] == "2026-10-30")
    assert oct_story["status"] == "open"
    nov_story = next(s for s in keys[(NOV, "p05")]["stories"] if s["underlying"] == "IWM" and s["carried_in"])
    assert nov_story["status"] == "closed"
    assert nov_story["closed_ts"].startswith("2026-11-03")
    assert nov_story["opened_date"] == "2026-10-30" and nov_story["days_held"] == 4
