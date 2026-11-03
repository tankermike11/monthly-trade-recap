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


# ---------------------------------------------------------------------------
# The full facts-sheet catalog (SPEC 3.1). Phase 3's metrics layer may emit only these
# fact IDs. `plain` is the fixed phrase shown with each fact. It is drafted here for
# review and must be approved by a person; the model never writes or changes it.
# ---------------------------------------------------------------------------

CATALOG_VERSION = "1.1.0-draft"   # 1.1: trader terms, no shorthand (CLAUDE.md copy rules)

ROLL_TYPE_PLAIN = {
    "out_in_time": "times you rolled out to a later expiration",
    "up": "times you rolled a strike up",
    "down": "times you rolled a strike down",
    "untested_toward_money": "times you rolled the side that was not under pressure toward the stock price",
    "tested_away_from_money": "times you rolled the side under pressure further from the stock price",
    "widened": "times a roll widened the distance between your strikes",
    "narrowed": "times a roll narrowed the distance between your strikes",
}

ASSET_PLAIN = {
    "stock_etf": "share of your orders that were stocks and funds",
    "equity_option": "share of your orders that were options on stocks and funds",
    "index_option": "share of your orders that were index options",
    "future": "share of your orders that were futures",
    "future_option": "share of your orders that were options on futures",
    "crypto": "share of your orders that were crypto",
    "event_contract": "share of your orders that were event contracts",
}

# id -> (section, plain phrase)
FACTS: dict[str, tuple[str, str]] = {
    # at a glance
    "active_days": ("at_a_glance", "days you placed at least one trade"),
    "stories_touched": ("at_a_glance", "trades you placed an order on this month"),
    "stories_opened": ("at_a_glance", "new trades you started"),
    "stories_ended": ("at_a_glance", "trades that ended this month"),
    "stories_open_at_end": ("at_a_glance", "trades still open at the end of the month"),
    "asset_mix": ("at_a_glance", "how your orders split across what you traded"),
    **{f"asset_mix.{k}": ("at_a_glance", v) for k, v in ASSET_PLAIN.items()},
    "outside_hours_share": ("at_a_glance", "share of your orders placed outside regular market hours"),
    # getting in
    "median_entry_dte": ("getting_in", "how far out the expiration usually was when you opened an options trade"),
    "entry_dte_by_half": ("getting_in", "how far out your expirations usually were in the first and second half of the month"),
    "median_price_level_at_entry": ("getting_in", "where implied volatility usually sat, compared with its past year, when you opened a trade"),
    "median_opening_credit_per_contract": ("getting_in", "premium you usually collected per contract when you opened a trade"),
    "median_opening_debit_per_contract": ("getting_in", "debit you usually paid per contract when you opened a trade"),
    "scaling_in": ("getting_in", "contracts you sold or bought three or more times at the same strike and expiration"),
    # managing
    "adjust_to_close_ratio": ("managing", "times you adjusted a trade for every time you closed one"),
    "rolls_total": ("managing", "times you rolled a trade to a new strike or expiration"),
    "rolls_by_type": ("managing", "the kinds of rolls you made"),
    **{f"rolls.{k}": ("managing", v) for k, v in ROLL_TYPE_PLAIN.items()},
    "credit_roll_share": ("managing", "share of your rolls done for a credit"),
    "most_adjusted_stories": ("managing", "the trades you changed most often"),
    # getting out
    "median_days_held": ("getting_out", "how long you usually kept a trade"),
    "median_days_left_at_close": ("getting_out", "how much time was usually left before expiration when you closed an options trade"),
    "median_upfront_kept_on_winners": ("getting_out", "how much of the premium collected your winning trades usually kept"),
    "fastest_winners": ("getting_out", "your quickest trades that ended with a gain"),
    "losers_closed_early": ("getting_out", "trades that ended with a loss that you closed before expiration"),
    # results
    "wins": ("results", "trades that ended with a gain"),
    "losses": ("results", "trades that ended with a loss"),
    "net_realized_pnl": ("results", "gains and losses locked in this month, after fees"),
    "total_fees": ("results", "fees and commissions you paid"),
    "biggest_win": ("results", "your largest gain on a single trade"),
    "biggest_loss": ("results", "your largest loss on a single trade"),
    "biggest_win_vs_upfront": ("results", "how your largest gain compared with the premium collected when that trade was opened"),
    "biggest_loss_vs_upfront": ("results", "how your largest loss compared with the premium collected when that trade was opened"),
    "month_pnl": ("results", "change in your account value, not counting deposits or withdrawals"),
    "month_return": ("results", "change in your account value as a share of where it started"),
    # still open
    "open_stories": ("still_open", "trades still open going into next month"),
    "open_moved_to_later_date": ("still_open", "open trades you rolled out to a later expiration this month"),
    # what changed
    "rolls_by_half": ("what_changed", "rolls you made in the first and second half of the month"),
    "stories_opened_by_half": ("what_changed", "new trades you started in the first and second half of the month"),
    "vs_last_month.median_entry_dte": ("what_changed", "how far out your expirations usually were, this month and last"),
    "vs_last_month.adjust_to_close_ratio": ("what_changed", "adjustments for every close, this month and last"),
    "vs_last_month.credit_roll_share": ("what_changed", "share of rolls done for a credit, this month and last"),
    "vs_last_month.outside_hours_share": ("what_changed", "share of orders outside regular market hours, this month and last"),
    "vs_last_month.median_days_held": ("what_changed", "how long you usually kept a trade, this month and last"),
    # context
    "spy_move": ("context", "how a fund that tracks the broad US market moved this month"),
    "qqq_move": ("context", "how a fund that tracks large technology companies moved this month"),
}

# SPEC 3.4: fact types that must never exist. The metrics layer may emit only IDs in
# FACTS, and no ID in FACTS may match these.
FORBIDDEN_FACT_WORDS = ("streak", "record", "most_trades", "milestone", "volume", "percentile",
                        "rank_vs", "leaderboard", "best_ever", "personal_best", "trade_count_change")
