"""Stage 3: metrics and the facts sheet (SPEC Phase 3).

Every number is computed here from the linked trade stories and the raw month. The
headline facts follow schemas/facts_catalog.HEADLINE_FACTS exactly; the rest of the
catalog (FACTS) adds the "getting in / managing / getting out / still open / what
changed" detail. Facts with no value are left out rather than sent empty.

Rules: "typical" = median; shares and ratios to 4 places; money to cents; halves of
the month are counted in trading days (pipeline.tradingcal).
"""

from __future__ import annotations

import statistics
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from pipeline import display as D
from pipeline.link import LinkResult
from pipeline.nytime import ny_date
from pipeline.tradingcal import half_of
from schemas.facts import Fact, FactsSheet, SheetIssue
from schemas.facts_catalog import CATALOG_VERSION, FACTS, FLAT_BAND_FRACTION
from schemas.raw import RawMonth
from schemas.trades import RollType, TradeStory

CENT = Decimal("0.01")
OPTION_CLASSES = {"equity_option", "index_option", "future_option"}
FUTURE_CLASSES = {"future", "future_option"}
STOCK_LIKE = {"stock_long", "stock_short", "crypto_long", "crypto_short", "future_long", "future_short"}

ROLL_SHORT = {   # display words for rolls_by_type; trader terms per the CLAUDE.md copy rules
    "untested_toward_money": "rolled the side not under pressure toward the stock price",
    "out_in_time": "rolled out to a later expiration",
    "up": "rolled a strike up",
    "down": "rolled a strike down",
    "tested_away_from_money": "rolled the side under pressure further away",
    "widened": "widened the strikes",
    "narrowed": "narrowed the strikes",
}

SECTION_WEIGHT = {"at_a_glance": 0.3, "getting_in": 0.6, "managing": 0.7, "getting_out": 0.6,
                  "results": 0.5, "still_open": 0.5, "what_changed": 0.5, "context": 0.4}


def r4(x) -> float:
    return float(Decimal(str(x)).quantize(Decimal("0.0001"), ROUND_HALF_UP))


def money(x) -> Decimal:
    return Decimal(x).quantize(CENT, ROUND_HALF_UP)


def median(xs):
    return statistics.median(xs) if xs else None


class _Builder:
    def __init__(self, raw: RawMonth, linked: LinkResult, previous: FactsSheet | None, first_month: bool):
        self.raw, self.linked, self.previous, self.first_month = raw, linked, previous, first_month
        self.month = raw.account.month
        self.stories: list[TradeStory] = linked.stories
        self.by_id = {s.story_id: s for s in self.stories}
        self.story_of_exec = {e: s for s in self.stories for e in s.exec_ids}
        self.trade_fills = [f for f in raw.fills if f.source == "trade"]
        self.orders = {o.order_id: o for o in raw.orders}
        self.fills = {f.exec_id: f for f in raw.fills}
        self.ctx = {c.exec_id: c for c in raw.market.fills}
        self.facts: list[Fact] = []
        self.values: dict[str, Any] = {}
        self.red = False

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def sid(s: TradeStory) -> str:
        return f"story:{s.story_id}"

    def add(self, fid: str, value, display: str, sources, weight: float | None = None) -> None:
        if fid not in FACTS:
            raise KeyError(f"{fid} is not in the facts catalog")
        self.values[fid] = value
        sources = sorted(set(sources))
        if value is None or value == {} or value == [] or not sources:
            return
        section, plain = FACTS[fid]
        w = SECTION_WEIGHT[section] if weight is None else weight
        self.facts.append(Fact(id=fid, section=section, value=value, display=display, plain=plain,
                               weight=round(min(1.0, max(0.0, w)), 2), sources=sources))

    def stories_sources(self, group, fallback=()):
        return [self.sid(s) for s in group] or [self.sid(s) for s in fallback]

    # ------------------------------------------------------------------ build

    def build(self) -> FactsSheet:
        st = self.stories
        touched = [s for s in st if s.orders]
        ended = [s for s in st if s.status != "open"]
        open_end = [s for s in st if s.status == "open"]
        opened = [s for s in st if not s.carried_in]
        tf = self.trade_fills
        a = self.raw.account
        flows = sum((c.amount if c.kind == "deposit" else -c.amount for c in a.cash_flows), Decimal(0))
        self.month_pnl = money(a.end_value) - money(a.start_value) - flows
        band = Decimal(FLAT_BAND_FRACTION) * money(a.start_value)
        self.red = self.month_pnl < -band
        fill_sources = [self.sid(self.story_of_exec[f.exec_id]) for f in tf]

        # ---------------------------------------------------------- at a glance
        n_days = len({ny_date(f.ts_utc) for f in tf})
        self.add("active_days", n_days, D.count(n_days, "day"), fill_sources)
        self.add("stories_touched", len(touched), D.count(len(touched), "trade"), self.stories_sources(touched, st))
        self.add("stories_opened", len(opened), D.count(len(opened), "trade"), self.stories_sources(opened, st))
        self.add("stories_ended", len(ended), D.count(len(ended), "trade"), self.stories_sources(ended, st))
        self.add("stories_open_at_end", len(open_end), D.count(len(open_end), "trade"),
                 self.stories_sources(open_end, st))
        n = len(tf)
        mix = Counter(str(f.asset_class) for f in tf)
        mix_v = {k: r4(v / n) for k, v in sorted(mix.items())} if n else {}
        mix_text = D.names([f"{D.percent(v, 0)} {D.NAMES[k]}" for k, v in sorted(mix_v.items(), key=lambda kv: -kv[1])])
        self.add("asset_mix", mix_v, mix_text, fill_sources)
        if len(mix_v) > 1:
            for k, v in mix_v.items():
                self.add(f"asset_mix.{k}", v, D.share(v),
                         [self.sid(self.story_of_exec[f.exec_id]) for f in tf if f.asset_class == k])
        outside = [f for f in tf if f.session != "regular"]
        share_out = r4(len(outside) / n) if n else None
        self.add("outside_hours_share", share_out, D.share(share_out or 0),
                 [self.sid(self.story_of_exec[f.exec_id]) for f in (outside or tf)])

        # ---------------------------------------------------------- getting in
        with_dte = [s for s in opened if s.opening_dte is not None]
        dtes = [s.opening_dte for s in with_dte]
        med = float(statistics.median(dtes)) if dtes else None
        self.add("median_entry_dte", med, D.days(med or 0), [self.sid(s) for s in with_dte])
        halves = {h: [s.opening_dte for s in with_dte if half_of(s.opened_date, self.month) == h] for h in ("first", "second")}
        if all(halves.values()):
            v = {h: float(statistics.median(x)) for h, x in halves.items()}
            self.add("entry_dte_by_half", v,
                     f"{D.days(v['first'])} in the first half of the month, {D.days(v['second'])} in the second half",
                     [self.sid(s) for s in with_dte])
        self._entry_prices(opened)
        self._scaling_in()

        # ---------------------------------------------------------- managing
        rolls = [(s, r) for s in st for r in s.rolls]
        roll_src = [self.sid(s) for s, _ in rolls]
        by_type = {t.value: 0 for t in RollType}
        for _, r in rolls:
            for t in r.types:
                by_type[t.value] += 1
        self.add("rolls_total", len(rolls), D.count(len(rolls), "time"), roll_src or [self.sid(s) for s in touched],
                 None if rolls else 0.2)
        nonzero = sorted(((c, t) for t, c in by_type.items() if c), key=lambda x: (-x[0], x[1]))
        self.add("rolls_by_type", by_type,
                 "; ".join(f"{ROLL_SHORT[t]} {D.count(c, 'time')}" for c, t in nonzero) or "no moves",
                 roll_src or [self.sid(s) for s in touched], None if rolls else 0.2)
        for c, t in nonzero:
            self.add(f"rolls.{t}", c, D.count(c, "time"),
                     [self.sid(s) for s, r in rolls if RollType(t) in r.types],
                     0.8 if t == "untested_toward_money" else None)
        credit_share = r4(sum(1 for _, r in rolls if r.net_cash > 0) / len(rolls)) if rolls else None
        self.add("credit_roll_share", credit_share, D.share(credit_share or 0), roll_src,
                 0.8 if credit_share is not None and (credit_share >= 0.75 or credit_share <= 0.25) else None)
        effects = [(s, o.effect.value) for s in st for o in s.orders]
        adjusts = [s for s, e in effects if e == "adjust"]
        closes = [s for s, e in effects if e == "close"]
        ratio = r4((len(rolls) + len(adjusts)) / len(closes)) if closes else None
        self.add("adjust_to_close_ratio", ratio, D.ratio(ratio or 0),
                 roll_src + [self.sid(s) for s in adjusts + closes],
                 0.2 if not ratio else 0.9 if (ratio >= 2 or ratio <= 0.5) else None)
        changes = {s.story_id: len(s.rolls) + sum(1 for o in s.orders if o.effect.value == "adjust") for s in st}
        busiest = sorted((s for s in st if changes[s.story_id] >= 2), key=lambda s: (-changes[s.story_id], s.story_id))[:3]
        self.add("most_adjusted_stories",
                 [{"story_id": s.story_id, "underlying": s.underlying, "changes": changes[s.story_id]} for s in busiest],
                 D.names([f"{s.underlying} ({D.count(changes[s.story_id], 'time')})" for s in busiest]),
                 [self.sid(s) for s in busiest])

        # ---------------------------------------------------------- getting out
        held = [s for s in ended if s.days_held is not None]
        mh = median([s.days_held for s in held])
        self.add("median_days_held", float(mh) if mh is not None else None, D.days(mh or 0), [self.sid(s) for s in held])
        left = [s for s in ended if s.days_left_at_close is not None]
        ml = median([s.days_left_at_close for s in left])
        self.add("median_days_left_at_close", float(ml) if ml is not None else None,
                 "none" if ml == 0 else D.days(ml or 0), [self.sid(s) for s in left])
        wins = [s for s in ended if s.realized_pnl > 0]
        losses = [s for s in ended if s.realized_pnl < 0]
        kept = [s for s in wins if s.upfront_cash > 0 and s.label_open.value not in STOCK_LIKE]
        mk = median([s.realized_pnl / s.upfront_cash for s in kept])
        self.add("median_upfront_kept_on_winners", r4(mk) if mk is not None else None, D.portion(float(mk or 0)),
                 [self.sid(s) for s in kept], 0.75)
        quick = sorted((s for s in wins if s.days_held is not None), key=lambda s: (s.days_held, s.story_id))[:3]
        self.add("fastest_winners",
                 [{"story_id": s.story_id, "underlying": s.underlying, "days_held": s.days_held} for s in quick],
                 D.names([f"{s.underlying} in {D.days(s.days_held)}" if s.days_held else f"{s.underlying} the same day"
                          for s in quick]),
                 [self.sid(s) for s in quick], 0.4)
        early = [s for s in losses if s.status == "closed" and (s.days_left_at_close or 0) > 0]
        if losses:
            self.add("losers_closed_early", len(early), D.count(len(early), "trade"),
                     self.stories_sources(early, losses))

        # ---------------------------------------------------------- results
        self.add("wins", len(wins), D.count(len(wins), "trade"), self.stories_sources(wins, ended or st))
        self.add("losses", len(losses), D.count(len(losses), "trade"), self.stories_sources(losses, ended or st))
        net = money(sum((s.realized_pnl for s in st), Decimal(0)))
        self.add("net_realized_pnl", net, D.money(net), [self.sid(s) for s in st])
        fees = money(sum(((f.commission or 0) + (f.fees or 0) for f in self.raw.fills), Decimal(0)))
        heavy = net != 0 and fees >= abs(net) * Decimal("0.25")
        self.add("total_fees", fees, D.money(fees),
                 [self.sid(self.story_of_exec[f.exec_id]) for f in self.raw.fills], 0.7 if heavy else 0.3)
        for fid, group, pick in (("biggest_win", wins, max), ("biggest_loss", losses, min)):
            if not group:
                self.add(fid, None, "", [])
                continue
            s = pick(group, key=lambda x: (x.realized_pnl, x.story_id))
            mult = None
            if s.upfront_cash > 0 and s.label_open.value not in STOCK_LIKE:
                mult = r4(s.realized_pnl / s.upfront_cash)
            self.add(fid, {"story_id": s.story_id, "pnl": money(s.realized_pnl), "multiple_of_upfront": mult},
                     f"{D.money(abs(s.realized_pnl))} on {s.underlying}", [self.sid(s)],
                     0.8 if (fid == "biggest_loss" and self.red) else 0.5)
            if mult is not None:
                self.add(f"{fid}_vs_upfront", mult, D.multiple(abs(mult)), [self.sid(s)], 0.5)
        self.add("month_pnl", self.month_pnl, D.money(self.month_pnl), [self.sid(s) for s in st] or ["market:SPY"],
                 0.7 if self.red else 0.5)
        if a.start_value > 0:
            ret = r4(self.month_pnl / money(a.start_value))
            self.add("month_return", ret, D.move(ret), [self.sid(s) for s in st] or ["market:SPY"])

        # ---------------------------------------------------------- still open
        if open_end:
            later = [s for s in open_end if any(RollType.out_in_time in r.types for r in s.rolls)]
            self.add("open_stories",
                     [{"story_id": s.story_id, "underlying": s.underlying, "label": s.label_end.value,
                       "changes": changes[s.story_id], "moved_to_later_date": s in later} for s in open_end],
                     D.names(list(dict.fromkeys(s.underlying for s in open_end))), [self.sid(s) for s in open_end])
            if later:
                self.add("open_moved_to_later_date", len(later), D.count(len(later), "trade"), [self.sid(s) for s in later])

        # ---------------------------------------------------------- what changed
        if rolls:
            rh = {h: sum(1 for _, r in rolls if half_of(ny_date(r.ts_utc), self.month) == h) for h in ("first", "second")}
            self.add("rolls_by_half", rh, f"{rh['first']} in the first half of the month, {rh['second']} in the second half", roll_src,
                     0.6 if abs(rh["first"] - rh["second"]) >= max(3, (rh["first"] + rh["second"]) // 2) else 0.3)
        if opened:
            oh = {h: sum(1 for s in opened if half_of(s.opened_date, self.month) == h) for h in ("first", "second")}
            self.add("stories_opened_by_half", oh, f"{oh['first']} in the first half of the month, {oh['second']} in the second half",
                     [self.sid(s) for s in opened], 0.3)
        self._vs_last_month()

        # ---------------------------------------------------------- context
        idx = {i.symbol: i for i in self.raw.market.indexes}
        for fid, sym in (("spy_move", "SPY"), ("qqq_move", "QQQ")):
            if sym in idx:
                mv = r4(idx[sym].end / idx[sym].start - 1)
                self.add(fid, mv, D.move(mv), [f"market:{sym}"], 0.7 if self.red else 0.4)

        return FactsSheet(customer_id=a.customer_id, month=self.month, first_name=a.first_name,
                          flags=self._flags(touched, open_end), facts=self.facts,
                          data_quality_issues=[SheetIssue(issue=d.issue, refs=list(d.refs))
                                               for d in self.linked.data_quality_issues],
                          catalog_version=CATALOG_VERSION)

    # ------------------------------------------------------------------ pieces

    def _opening_order(self, s: TradeStory):
        o = next((o for o in s.orders if o.effect.value == "open"), None)
        return self.orders[o.order_id] if o else None

    def _entry_prices(self, opened: list[TradeStory]) -> None:
        ranks, credits, debits = [], [], []
        for s in opened:
            o = self._opening_order(s)
            if o is None:
                continue
            legs = [self.fills[e] for e in o.leg_exec_ids]
            opts = [f for f in legs if f.asset_class in OPTION_CLASSES]
            if not opts:
                continue
            rank = self.ctx[opts[0].exec_id].iv_rank
            if rank is not None:
                ranks.append((float(rank), s))
            per = s.opening_cash / o.filled_units if o.filled_units else None
            if per is not None and per > 0:
                credits.append((per, s))
            elif per is not None and per < 0:
                debits.append((-per, s))
        if ranks:
            m = round(float(statistics.median(r for r, _ in ranks)), 1)
            self.add("median_price_level_at_entry", m, D.price_level(m), [self.sid(s) for _, s in ranks])
        for fid, group in (("median_opening_credit_per_contract", credits), ("median_opening_debit_per_contract", debits)):
            if group:
                m = money(statistics.median(v for v, _ in group))
                self.add(fid, m, D.money(m), [self.sid(s) for _, s in group])

    def _scaling_in(self) -> None:
        opens: dict[str, set[str]] = {}
        for f in self.trade_fills:
            if f.asset_class in OPTION_CLASSES and f.open_close == "open":
                opens.setdefault(f.symbol, set()).add(f.order_id)
        hit = sorted(sym for sym, orders in opens.items() if len(orders) >= 3)
        if hit:
            src = {self.sid(self.story_of_exec[f.exec_id]) for f in self.trade_fills if f.symbol in hit}
            self.add("scaling_in", len(hit), D.count(len(hit), "contract"), src, 0.6)

    def _vs_last_month(self) -> None:
        if self.previous is None:
            return
        last = {f.id: f.value for f in self.previous.facts}
        fmt = {"median_entry_dte": D.days, "adjust_to_close_ratio": D.ratio, "credit_roll_share": D.share,
               "outside_hours_share": D.share, "median_days_held": D.days}
        mine = {f.id: f for f in self.facts}
        for fid, f in fmt.items():
            if fid not in mine or last.get(fid) is None:
                continue
            a, b = float(last[fid]), float(mine[fid].value)
            big = (abs(b - a) / abs(a) >= 0.25) if a else b != 0
            same = f(a) == f(b)
            self.add(f"vs_last_month.{fid}", {"last": last[fid], "this": mine[fid].value},
                     f"{f(b)} both months" if same else f"{f(b)} this month, {f(a)} last month",
                     mine[fid].sources, 0.2 if same else 0.8 if big else 0.4)

    def _flags(self, touched, open_end) -> list[str]:
        band = Decimal(FLAT_BAND_FRACTION) * money(self.raw.account.start_value)
        p = self.month_pnl
        flags = ["green_month" if p > band else "red_month" if p < -band else "flat_month"]
        if len(touched) < 3:
            flags.append("low_activity")
        if self.first_month:
            flags.append("first_month")
        if open_end:
            flags.append("has_open_positions")
        if self.linked.data_quality_issues:
            flags.append("data_quality_issue")
        lines = self.raw.positions_start.lines + self.raw.positions_end.lines
        classes = {str(f.asset_class) for f in self.raw.fills} | {str(l.asset_class) for l in lines}
        if not classes & OPTION_CLASSES:
            flags.append("no_options")
        if not classes & FUTURE_CLASSES:
            flags.append("no_futures")
        return sorted(flags)


def build_facts(raw: RawMonth, linked: LinkResult, previous: FactsSheet | None = None,
                first_month: bool = False) -> FactsSheet:
    return _Builder(raw, linked, previous, first_month).build()
