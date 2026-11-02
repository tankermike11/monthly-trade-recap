"""Exact definitions of the headline facts recorded in every answer key.

Phase 3's metrics layer must reproduce these values exactly. If a definition changes
here, regenerate the answer keys in the same change.

General rules
- "This month" = fills, orders and events whose New York calendar date is in the month.
- Trade fills are fills with source == "trade". Event fills (from assignments) are
  never counted as activity.
- "Typical" = median, never mean. Shares and ratios are rounded to 4 decimal places;
  money to cents.
- A story "ended" this month if its status is closed, expired or assigned.
"""

from __future__ import annotations

HEADLINE_FACTS: dict[str, str] = {
    "active_days":
        "Count of distinct New York dates with at least one trade fill.",
    "stories_touched":
        "Stories with at least one of the customer's own orders this month.",
    "stories_opened":
        "Stories whose first opening order is this month (not carried in).",
    "stories_ended":
        "Stories with status closed, expired or assigned.",
    "stories_open_at_end":
        "Stories still holding a position at month end.",
    "asset_mix":
        "Share of trade fills by asset class: {asset_class: share}, only classes present.",
    "outside_hours_share":
        "Share of trade fills whose session is not 'regular'.",
    "median_entry_dte":
        "Median, over stories opened this month that contain options, of the opening "
        "order's nearest expiration in calendar days. None if no such story.",
    "rolls_total":
        "Count of rolls (a two-order roll counts once).",
    "rolls_by_type":
        "{roll_type: count} over all rolls; a roll with several types counts in each. "
        "Every roll type is present, zeros included.",
    "credit_roll_share":
        "Rolls with net_cash > 0 divided by rolls_total. None if no rolls.",
    "adjust_to_close_ratio":
        "(rolls_total + orders with effect 'adjust') divided by orders with effect "
        "'close'. None if there are no closes.",
    "wins":
        "Ended stories with realized_pnl > 0.",
    "losses":
        "Ended stories with realized_pnl < 0.",
    "net_realized_pnl":
        "Sum of realized_pnl over all stories (ended and open), after fees.",
    "total_fees":
        "Sum of commission + fees over this month's fills; a missing value counts as 0 "
        "and raises the data-quality issue 'fees_missing'.",
    "biggest_win":
        "The ended story with the largest positive realized_pnl: {story_id, pnl, "
        "multiple_of_upfront} where multiple_of_upfront = pnl / upfront_cash when the "
        "story is an options story whose upfront_cash > 0 (cash collected by the opening "
        "order and any later 'add' orders), else None. None if no wins.",
    "biggest_loss":
        "Same for the most negative realized_pnl. None if no losses.",
    "month_pnl":
        "Account end value - start value - deposits + withdrawals.",
    "spy_move":
        "SPY close on the month's last trading day / previous month's last close - 1.",
    "qqq_move":
        "Same for QQQ.",
}

# Flags (SPEC 3.3), with the thresholds the answer key uses.
FLAG_RULES: dict[str, str] = {
    "green_month": "month_pnl > flat band",
    "red_month": "month_pnl < -flat band",
    "flat_month": "|month_pnl| <= flat band, where flat band = 0.1% of start value",
    "low_activity": "stories_touched < 3",
    "first_month": "no earlier month of data exists for the customer",
    "has_open_positions": "stories_open_at_end > 0",
    "data_quality_issue": "any data-quality issue is recorded",
    "no_options": "no option fills this month and no option positions at either snapshot",
    "no_futures": "no futures or futures-option fills this month and none held",
}

FLAT_BAND_FRACTION = "0.001"

# Named data-quality issues the generator plants and Phase 2 must detect.
DATA_QUALITY_ISSUES: dict[str, str] = {
    "fees_missing": "A fill whose commission or fees field is None.",
    "order_partially_filled": "An order whose filled_units < requested_units.",
    "split_adjusted_positions": "A split changed open positions without any order.",
    "positions_do_not_reconcile": "Replaying the month's fills and events from the start "
                                  "snapshot does not reach the end snapshot (quantity or cost).",
    "fill_exceeds_position": "A closing fill for more than the position held; it is read as "
                             "a close to flat plus a new position for the rest.",
    "event_without_position": "An expiration, assignment, exercise or dividend names a "
                              "position the account did not hold; the event is skipped.",
}
