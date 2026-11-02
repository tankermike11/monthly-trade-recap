"""Calendar and clock rules, including the November 2026 edge cases."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from mock.market import calendar as cal

UTC = timezone.utc


def test_dst_ends_nov_1_2026():
    # 19:30 New York on Sunday Oct 25 is daylight time (UTC-4), on Sunday Nov 1 standard (UTC-5)
    assert cal.to_utc(datetime(2026, 10, 25, 19, 30), "America/New_York") == datetime(2026, 10, 25, 23, 30, tzinfo=UTC)
    assert cal.to_utc(datetime(2026, 11, 1, 19, 30), "America/New_York") == datetime(2026, 11, 2, 0, 30, tzinfo=UTC)
    # the repeated hour resolves to the earlier (daylight) instant
    assert cal.to_utc(datetime(2026, 11, 1, 1, 30), "America/New_York") == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert cal.to_utc(datetime(2026, 11, 2, 9, 30), "America/Chicago") == datetime(2026, 11, 2, 15, 30, tzinfo=UTC)


def test_dst_start_skipped_hour():
    with pytest.raises(ValueError):
        cal.to_utc(datetime(2026, 3, 8, 2, 30), "America/New_York")


def test_round_trip():
    for tz in ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"):
        for h in range(24):
            ts = datetime(2026, 11, 1, h, 15, tzinfo=UTC)
            assert cal.to_utc(cal.to_local(ts, tz), tz) == ts or cal.to_local(ts, tz).hour == 1


def test_holidays_and_half_days():
    assert not cal.is_trading_day(date(2026, 11, 26))
    assert cal.is_half_day(date(2026, 11, 27))
    assert cal.regular_close_minute(date(2026, 11, 27)) == 13 * 60
    assert date(2026, 11, 26) not in cal.month_trading_days("2026-11")
    assert len(cal.month_trading_days("2026-10")) == 22
    assert len(cal.month_trading_days("2026-11")) == 20


def test_monthly_expirations():
    assert cal.third_friday(2026, 10) == date(2026, 10, 16)
    assert cal.third_friday(2026, 11) == date(2026, 11, 20)
    assert cal.third_friday(2026, 12) == date(2026, 12, 18)


def test_sessions():
    d = date(2026, 10, 14)
    assert cal.session_of(cal.at_ny(d, 7)) == "pre_market"
    assert cal.session_of(cal.at_ny(d, 9, 30)) == "regular"
    assert cal.session_of(cal.at_ny(d, 16)) == "after_hours"
    assert cal.session_of(cal.at_ny(d, 21)) == "overnight"
    assert cal.session_of(cal.at_ny(date(2026, 11, 27), 13, 30)) == "after_hours"
    assert cal.session_of(cal.at_ny(date(2026, 10, 25), 19)) == "overnight"   # Sunday
