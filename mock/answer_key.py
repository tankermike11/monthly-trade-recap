"""Turn one persona-month from the ledger into its answer key (headline facts + flags).

Definitions: schemas/facts_catalog.py. This module is deliberately separate from the
future pipeline/metrics.py: the tests compare two independent implementations.
"""

from __future__ import annotations

import statistics
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal

from mock.market import calendar as cal
from schemas.answer_key import AnswerKey
from schemas.facts_catalog import FLAT_BAND_FRACTION
from schemas.trades import RollType

GENERATOR_VERSION = "1.0.0"
FIRST_MONTH = "2026-10"
CENT = Decimal("0.01")
OPTION_CLASSES = {"equity_option", "index_option", "future_option"}
FUTURE_CLASSES = {"future", "future_option"}
STOCKISH = {"stock_long", "stock_short", "crypto_long", "crypto_short", "future_long", "future_short"}


def r4(x) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.0001"), ROUND_HALF_UP))


def money(x: Decimal) -> Decimal:
    return Decimal(x).quantize(CENT, ROUND_HALF_UP)


def index_levels(world, month: str) -> list[dict]:
    out = []
    a = cal.last_trading_day(cal.prev_month(month))
    b = cal.last_trading_day(month)
    for label, sym in (("SPY", "SPY"), ("QQQ", "QQQ"), ("/ES", "ES")):
        out.append({"symbol": label,
                    "start": money(Decimal(repr(world.close(sym, a)))),
                    "end": money(Decimal(repr(world.close(sym, b))))})
    return out


def build(res: dict, world, seed: int) -> dict:
    month = res["month"]
    pid = res["persona"]["id"]
    stories = res["stories"]
    trade_fills = [f for f in res["fills"] if f["source"] == "trade"]
    orders_by_story = {s["story_id"]: s["orders"] for s in stories}
    sid = lambda s: f"story:{s['story_id']}"
    facts = []

    def fact(fid, value, sources):
        facts.append({"id": fid, "value": value, "sources": sorted(set(sources))})

    story_of_exec = {e: s for s in stories for e in s["exec_ids"]}
    touched = [s for s in stories if s["orders"]]
    ended = [s for s in stories if s["status"] != "open"]
    open_end = [s for s in stories if s["status"] == "open"]
    opened = [s for s in stories if not s["carried_in"]]

    fact("active_days", len({cal.ny_date(f["ts_utc"]) for f in trade_fills}),
         [sid(story_of_exec[f["exec_id"]]) for f in trade_fills])
    fact("stories_touched", len(touched), [sid(s) for s in touched])
    fact("stories_opened", len(opened), [sid(s) for s in opened])
    fact("stories_ended", len(ended), [sid(s) for s in ended])
    fact("stories_open_at_end", len(open_end), [sid(s) for s in open_end])

    n = len(trade_fills)
    mix = Counter(f["asset_class"] for f in trade_fills)
    fact("asset_mix", {k: r4(v / n) for k, v in sorted(mix.items())} if n else {},
         [sid(story_of_exec[f["exec_id"]]) for f in trade_fills])
    outside = [f for f in trade_fills if f["session"] != "regular"]
    fact("outside_hours_share", r4(len(outside) / n) if n else None,
         [sid(story_of_exec[f["exec_id"]]) for f in (outside or trade_fills)])

    dtes = [s["opening_dte"] for s in opened if s["opening_dte"] is not None]
    fact("median_entry_dte", float(statistics.median(dtes)) if dtes else None,
         [sid(s) for s in opened if s["opening_dte"] is not None])

    rolls = [(s, r) for s in stories for r in s["rolls"]]
    by_type = {t.value: 0 for t in RollType}
    for _, r in rolls:
        for t in r["types"]:
            by_type[t] += 1
    roll_sources = [sid(s) for s, _ in rolls]
    fact("rolls_total", len(rolls), roll_sources)
    fact("rolls_by_type", by_type, roll_sources)
    credits = [1 for _, r in rolls if r["net_cash"] > 0]
    fact("credit_roll_share", r4(len(credits) / len(rolls)) if rolls else None, roll_sources)
    effects = [(s, o["effect"]) for s in stories for o in s["orders"]]
    adjusts = [s for s, e in effects if e == "adjust"]
    closes = [s for s, e in effects if e == "close"]
    ratio = r4((len(rolls) + len(adjusts)) / len(closes)) if closes else None
    fact("adjust_to_close_ratio", ratio, roll_sources + [sid(s) for s in adjusts + closes])

    wins = [s for s in ended if s["realized_pnl"] > 0]
    losses = [s for s in ended if s["realized_pnl"] < 0]
    fact("wins", len(wins), [sid(s) for s in wins])
    fact("losses", len(losses), [sid(s) for s in losses])
    fact("net_realized_pnl", money(sum((s["realized_pnl"] for s in stories), Decimal(0))), [sid(s) for s in stories])
    all_unknown = res["fills"] and all(f["commission"] is None or f["fees"] is None for f in res["fills"])
    fact("total_fees", None if all_unknown else
         money(sum(((f["commission"] or 0) + (f["fees"] or 0) for f in res["fills"]), Decimal(0))),
         [sid(story_of_exec[f["exec_id"]]) for f in res["fills"]])

    def extreme(group, pick):
        if not group:
            return None, []
        s = pick(group, key=lambda x: (x["realized_pnl"], x["story_id"]))
        mult = None
        if s["upfront_cash"] > 0 and s["label_open"] not in STOCKISH:
            mult = r4(s["realized_pnl"] / s["upfront_cash"])
        return {"story_id": s["story_id"], "pnl": money(s["realized_pnl"]), "multiple_of_upfront": mult}, [sid(s)]

    v, src = extreme(wins, max)
    fact("biggest_win", v, src)
    v, src = extreme(losses, min)
    fact("biggest_loss", v, src)

    start_v, end_v = money(res["start_value"]), money(res["end_value"])
    flows = sum(((f["amount"] if f["kind"] == "deposit" else -f["amount"]) for f in res["flows"]), Decimal(0))
    month_pnl = end_v - start_v - flows
    fact("month_pnl", month_pnl, [sid(s) for s in stories])
    levels = {l["symbol"]: l for l in index_levels(world, month)}
    for fid, sym in (("spy_move", "SPY"), ("qqq_move", "QQQ")):
        fact(fid, r4(levels[sym]["end"] / levels[sym]["start"] - 1), [f"market:{sym}"])

    # flags
    band = Decimal(FLAT_BAND_FRACTION) * start_v
    flags = ["green_month" if month_pnl > band else "red_month" if month_pnl < -band else "flat_month"]
    if len(touched) < 3:
        flags.append("low_activity")
    if month == FIRST_MONTH:
        flags.append("first_month")
    if open_end:
        flags.append("has_open_positions")
    if res["dq_issues"]:
        flags.append("data_quality_issue")
    lines = res["start_snapshot"]["lines"] + res["end_snapshot"]["lines"]
    classes = {f["asset_class"] for f in res["fills"]} | {l["asset_class"] for l in lines}
    if not classes & OPTION_CLASSES:
        flags.append("no_options")
    if not classes & FUTURE_CLASSES:
        flags.append("no_futures")

    issues: dict[str, list[str]] = {}
    for issue, ref in res["dq_issues"]:
        issues.setdefault(issue, []).append(ref)

    realized = sum((s["realized_pnl"] for s in stories), Decimal(0))
    open_change = sum((s["open_pnl_end"] - s["open_pnl_start"] for s in stories), Decimal(0))
    recon = {"start_value": start_v, "end_value": end_v, "net_cash_flows": flows,
             "realized_pnl": realized, "open_pnl_change": open_change,
             "difference": (end_v - start_v - flows) - (realized + open_change)}

    key = AnswerKey(
        customer_id=pid, month=month, seed=seed, generator_version=GENERATOR_VERSION,
        persona=res["persona"]["style"],
        stories=[_story_model(s) for s in stories],
        headline_facts=facts, flags=sorted(flags),
        data_quality_issues=[{"issue": k, "refs": v} for k, v in sorted(issues.items())],
        reconciliation=recon,
        option_quotes={k: v for k, v in sorted(res["quotes"].items())},
    )
    return key


def _story_model(s: dict) -> dict:
    out = dict(s)
    out["rolls"] = [{"order_ids": r["order_ids"], "ts_utc": r["ts_utc"], "types": r["types"],
                     "net_cash": r["net_cash"], "credit": r["credit"]} for r in s["rolls"]]
    return out
