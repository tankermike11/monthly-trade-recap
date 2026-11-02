"""The generator's book of record for one persona.

Behaviour engines tell the ledger what the customer does (orders, with the strategy
label they intend). The ledger prices every fill in the simulated market, keeps cash
and positions, and records the ground truth that becomes the answer key: which story
every fill belongs to, each order's effect, every roll and its types, P&L after fees.

Story, effect and roll definitions live in schemas/trades.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_HALF_UP, Decimal

from mock.market import calendar as cal
from mock.market import quotes as Q
from mock.market.listings import Instrument, split_adjusted
from mock.market.paths import World
from mock.rng import stream
from schemas.trades import ROLL_WINDOW_SECONDS

ZERO = Decimal(0)
CENT = Decimal("0.01")
Q8 = Decimal("0.00000001")


@dataclass(frozen=True)
class LegReq:
    inst: Instrument
    side: str            # buy | sell
    qty: Decimal


@dataclass
class Position:
    inst: Instrument
    qty: Decimal
    cost: Decimal        # signed cost basis: sum of qty * price * multiplier
    opened: date


@dataclass
class StoryMonth:
    """What one story did inside one month."""
    sid: str = ""
    carried_in: bool = False
    label_open: str = ""
    orders: list[list] = field(default_factory=list)   # [order_id, effect, ts, only_closing, only_opening]
    exec_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    rolls: list[dict] = field(default_factory=list)
    realized: Decimal = ZERO          # from the data: fees as reported (missing = 0)
    realized_true: Decimal = ZERO     # what the broker actually charged
    fees: Decimal = ZERO
    opening_cash: Decimal = ZERO
    upfront_cash: Decimal = ZERO      # opening cash plus cash from later 'add' orders
    opening_dte: int | None = None
    open_pnl_start: Decimal = ZERO
    start_opened_date: date | None = None
    asset_class: str | None = None    # carried in: from the month-start snapshot


@dataclass
class Story:
    uid: int
    underlying: str
    asset_class: str
    opened_date: date
    label: str
    status: str = "open"
    closed_ts: datetime | None = None
    days_left_at_close: int | None = None
    label_before_close: str | None = None
    months: dict[str, StoryMonth] = field(default_factory=dict)
    last_order: dict | None = None
    cash_net: Decimal = ZERO          # net option/stock cash in (+) or out (-) since the story opened


class Ledger:
    def __init__(self, world: World, persona: dict, start_cash: Decimal):
        self.world = world
        self.p = persona
        self.pid = persona["id"]
        self.cash = start_cash
        self.positions: dict[str, Position] = {}
        self.stories: dict[str, Story] = {}          # underlying -> open story
        self.all_stories: list[Story] = []
        self.month: str | None = None
        self._uid = 0
        self.last_ts: datetime | None = None
        self.fees_missing_next = False
        self.insts: dict[str, Instrument] = {}
        self.clock: datetime | None = None           # ledger-scheduled items processed up to here
        self.labeler = None                          # optional: underlying -> label, after events

    # ------------------------------------------------------------------ month framing

    def begin_month(self, month: str) -> None:
        self.month = month
        self.fills: list[dict] = []
        self.orders: list[dict] = []
        self.events: list[dict] = []
        self.contexts: list[dict] = []
        self.quotes: dict[str, dict] = {}
        self.month_flows: list[dict] = []
        self.dq_issues: list[tuple[str, str]] = []
        self.n_fill = self.n_order = self.n_event = self.n_story = 0
        self.month_stories: list[Story] = []
        self.start_valued_at = self.valuation_time(cal.prev_month(month))
        if self.clock is None:
            self.clock = self.start_valued_at["exchange"]
        self.start_snapshot = self.snapshot("start", self.start_valued_at)
        self.start_value = self.value(self.start_snapshot)
        def oldest(st):
            return min(l["opened_date"] for l in self.start_snapshot["lines"] if l["underlying"] == st.underlying)
        for st in sorted(self.stories.values(), key=lambda s: (oldest(s), s.underlying)):
            sm = self._story_month(st)
            sm.carried_in = True
            sm.label_open = st.label
            lines = [l for l in self.start_snapshot["lines"] if l["underlying"] == st.underlying]
            # Carried in: the story's first instrument is not in this month's data, so its
            # asset class is that of the oldest month-start line (shares, futures and
            # coins before options on the same day, then by symbol).
            first = min(lines, key=lambda l: (l["opened_date"], l["asset_class"] not in ("stock_etf", "future", "crypto"), l["symbol"]))
            sm.asset_class = first["asset_class"]
            sm.open_pnl_start = sum((l["qty"] * l["mark"] * l["multiplier"] - l["cost_basis"] for l in lines), ZERO)
            sm.opening_cash = -sum((l["cost_basis"] for l in lines if l["asset_class"] != "future"), ZERO)
            sm.start_opened_date = min(l["opened_date"] for l in lines)
            sm.upfront_cash = sm.opening_cash

    def valuation_time(self, month: str) -> dict:
        last_td = cal.last_trading_day(month)
        _, last_cd = cal.month_bounds(month)
        return {"exchange": cal.at_ny_minutes(last_td, cal.regular_close_minute(last_td)),
                "crypto": cal.at_ny_minutes(last_cd, 24 * 60 - 1)}

    def _story_month(self, st: Story) -> StoryMonth:
        sm = st.months.get(self.month)
        if sm is None:
            self.n_story += 1
            sm = StoryMonth(sid=f"{self.pid}-{self.n_story:04d}")
            st.months[self.month] = sm
            self.month_stories.append(st)
        return sm

    # ------------------------------------------------------------------ valuation

    def snapshot(self, as_of: str, at: dict) -> dict:
        lines = []
        for sym in sorted(self.positions):
            pos = self.positions[sym]
            ts = at["crypto"] if pos.inst.asset_class == "crypto" else at["exchange"]
            lines.append({
                "symbol": sym, "asset_class": pos.inst.asset_class, "underlying": pos.inst.underlying,
                "multiplier": pos.inst.multiplier, "strike": pos.inst.strike,
                "expiration": pos.inst.expiration, "put_call": pos.inst.put_call,
                "contract_month": pos.inst.contract_month, "qty": pos.qty,
                "cost_basis": pos.cost.quantize(Decimal("0.0001"), ROUND_HALF_UP),
                "mark": Q.mark(self.world, pos.inst, ts), "opened_date": pos.opened,
                "_cost_exact": pos.cost,
            })
        return {"as_of": as_of, "valued_at_utc": at["exchange"], "cash": self.cash, "lines": lines}

    @staticmethod
    def value(snap: dict, exact: bool = True) -> Decimal:
        v = snap["cash"]
        for l in snap["lines"]:
            mv = l["qty"] * l["mark"] * l["multiplier"]
            cb = l["_cost_exact"] if exact else l["cost_basis"]
            v += (mv - cb) if l["asset_class"] == "future" else mv
        return v

    # ------------------------------------------------------------------ ids

    def _next(self, kind: str) -> str:
        yyyymm = self.month.replace("-", "")
        if kind == "F":
            self.n_fill += 1
            return f"{self.pid}-{yyyymm}-F{self.n_fill:05d}"
        if kind == "O":
            self.n_order += 1
            return f"{self.pid}-{yyyymm}-O{self.n_order:05d}"
        self.n_event += 1
        return f"{self.pid}-{yyyymm}-V{self.n_event:04d}"

    def _check_time(self, ts: datetime) -> None:
        self.advance(ts)
        if self.last_ts is not None and ts < self.last_ts:
            raise RuntimeError(f"{self.pid}: out-of-order action at {ts} (last {self.last_ts})")
        if cal.ny_date(ts).strftime("%Y-%m") != self.month:
            raise RuntimeError(f"{self.pid}: action at {ts} is outside {self.month}")
        self.last_ts = ts

    # ------------------------------------------------------------------ scheduled items

    def advance(self, ts: datetime) -> None:
        """Process everything the market does on its own (corporate actions at the
        04:00 open, expirations at settlement) up to and including `ts`."""
        if self.clock is None:
            self.clock = ts
            return
        while True:
            due = []
            for (sym, d), _ in self.world.splits.items():
                t = cal.at_ny(d, 4)
                if self.clock < t <= ts:
                    due.append((t, 0, d))
            for sym, ex, _ in self.world.dividends:
                t = cal.at_ny(ex, 4)
                if self.clock < t <= ts:
                    due.append((t, 0, ex))
            for p in self.positions.values():
                if p.inst.is_option:
                    t = Q.settlement_utc(p.inst)
                    if t <= ts:
                        due.append((t, 1, p.inst.expiration))
            if not due:
                break
            t, kind, d = min(due)
            if self.last_ts is None or t > self.last_ts:
                self.last_ts = t
            if kind == 0:
                self.clock = t
                self.corporate_actions(d)
            else:
                self.expirations(d)
                self.clock = max(self.clock, t)
        self.clock = max(self.clock, ts)

    # ------------------------------------------------------------------ fees

    def fee_schedule(self, inst: Instrument, side: str, oc: str, qty: Decimal, price: Decimal) -> tuple[Decimal, Decimal]:
        """(commission, fees) for one fill, modeled on tastytrade's schedule.
        commission = the broker's published charge; fees = exchange, clearing and regulatory."""
        f = self.world.defaults["fees"]
        C = lambda k: Decimal(str(f["commission"][k]))
        P = lambda k: Decimal(str(f["pass_through"][k]))
        ac = inst.asset_class
        sec = ZERO
        if side == "sell" and ac != "crypto" and ac != "future":
            sec = (P("sec_fee_rate_on_sells") * qty * price * inst.multiplier).quantize(CENT, ROUND_CEILING)
        if ac in ("equity_option", "index_option"):
            if ac == "equity_option":
                comm = min(C("equity_option_open") * qty, C("equity_option_open_most_per_leg")) \
                    if oc == "open" else C("equity_option_close") * qty
                fees = (P("option_clearing") + P("option_regulatory")) * qty
            else:
                comm = C("index_option_open" if oc == "open" else "index_option_close") * qty
                fees = (P("option_clearing") + P("option_regulatory") + P("index_option_exchange")) * qty
            if side == "sell":
                fees += (P("taf_per_contract_on_sells") * qty).quantize(CENT, ROUND_CEILING) + sec
            return comm.quantize(CENT), fees.quantize(CENT)
        if ac == "future":
            return (C("micro_future") * qty).quantize(CENT), (P("micro_future_exchange_clearing") * qty).quantize(CENT)
        if ac == "future_option":
            return ((C("micro_future_option") * qty).quantize(CENT),
                    (P("micro_future_option_exchange_clearing") * qty).quantize(CENT))
        if ac == "crypto":     # no exchange, clearing or regulatory fees on crypto
            return (C("crypto_rate") * qty * price).quantize(CENT, ROUND_HALF_UP), ZERO.quantize(CENT)
        # stock / ETF
        fees = ZERO
        if side == "sell":
            taf = min((P("taf_per_share_on_sells") * qty).quantize(CENT, ROUND_CEILING), Decimal("8.30"))
            fees = sec + taf
        return C("stock").quantize(CENT), fees.quantize(CENT)

    # ------------------------------------------------------------------ core fill

    def _story_for(self, inst: Instrument, ts: datetime, label: str | None) -> Story:
        st = self.stories.get(inst.underlying)
        if st is None:
            self._uid += 1
            st = Story(self._uid, inst.underlying, inst.asset_class, cal.ny_date(ts), label or "custom")
            self.stories[inst.underlying] = st
            self.all_stories.append(st)
        return st

    def _apply(self, inst: Instrument, side: str, qty: Decimal, price: Decimal, ts: datetime,
               st: Story, sm: StoryMonth, *, order_id: str | None, source: str,
               charge_fees: bool = True) -> dict:
        self.insts[inst.symbol] = inst
        dq = qty if side == "buy" else -qty
        if not inst.is_future:
            st.cash_net += -dq * price * inst.multiplier
        pos = self.positions.get(inst.symbol)
        q0 = pos.qty if pos else ZERO
        m = inst.multiplier
        realized = ZERO
        if q0 == 0 or (q0 > 0) == (dq > 0):
            oc = "open"
            if pos is None:
                pos = Position(inst, ZERO, ZERO, cal.ny_date(ts))
                self.positions[inst.symbol] = pos
            pos.qty += dq
            pos.cost += dq * price * m
            if not inst.is_future:
                self.cash -= dq * price * m
        else:
            oc = "close"
            if abs(dq) > abs(q0):
                raise RuntimeError(f"{self.pid}: fill would flip {inst.symbol} from {q0} by {dq}")
            removed = (pos.cost * abs(dq) / abs(q0)).quantize(Q8)
            proceeds = -dq * price * m
            realized = proceeds - removed
            pos.qty += dq
            pos.cost -= removed
            if pos.qty == 0:
                if pos.cost != 0:           # rounding residue belongs to the realized P&L
                    realized -= pos.cost
                    removed += pos.cost
                    pos.cost = ZERO
                del self.positions[inst.symbol]
            self.cash += realized if inst.is_future else proceeds
        if charge_fees and source == "trade":
            comm, fees = self.fee_schedule(inst, side, oc, qty, price)
        else:
            comm, fees = ZERO.quantize(CENT), ZERO.quantize(CENT)
        reported_fees: Decimal | None = fees
        exec_id = self._next("F")
        if self.fees_missing_next and source == "trade":
            reported_fees = None
            self.fees_missing_next = False
            self.dq_issues.append(("fees_missing", exec_id))
        self.cash -= comm + fees
        sm.realized += realized - comm - (reported_fees or ZERO)
        sm.realized_true += realized - comm - fees
        sm.fees += comm + (reported_fees or ZERO)
        sm.exec_ids.append(exec_id)
        fill = {
            "exec_id": exec_id, "order_id": order_id, "ts_utc": ts, "session": cal.session_of(ts),
            "asset_class": inst.asset_class, "symbol": inst.symbol, "underlying": inst.underlying,
            "side": side, "open_close": oc, "qty": qty, "price": price, "multiplier": m,
            "strike": inst.strike, "expiration": inst.expiration, "put_call": inst.put_call,
            "contract_month": inst.contract_month, "commission": comm, "fees": reported_fees,
            "source": source,
        }
        self.fills.append(fill)
        self._context(exec_id, inst, ts)
        return fill

    def _context(self, exec_id: str, inst: Instrument, ts: datetime) -> None:
        w = self.world
        s, iv = Q.inputs(w, inst, ts) if w.has_iv(inst.price_symbol) else (
            Decimal(repr(w.price_at(inst.price_symbol, ts))).quantize(Q.PRICE_PLACES), None)
        d = w.session_day(inst.price_symbol, ts)[0]
        ivr = (Decimal(repr(w.iv_rank(inst.price_symbol, d))).quantize(Decimal("0.1"), ROUND_HALF_UP)
               if w.has_iv(inst.price_symbol) else None)
        dte = (inst.expiration - cal.ny_date(ts)).days if inst.is_option else None
        self.contexts.append({"exec_id": exec_id, "underlying_price": s, "iv": iv,
                              "iv_rank": ivr, "dte": dte})

    # ------------------------------------------------------------------ orders

    def trade(self, ts: datetime, legs: list[LegReq], label: str | None = None, *,
              order_type: str = "limit", pieces: list[tuple[datetime, Decimal]] | None = None,
              at_mid: bool = False) -> str:
        """Place one order. `label` is the story's strategy label after this order
        (ignored on a closing order); a callable is evaluated after the fills, so a label
        that depends on the cash balance sees it after the order. `at_mid` fills option legs at the tick nearest
        the mid instead of somewhere between mid and the far side. `pieces` executes a single-leg order as several
        fills at the given times and sizes; if the pieces add up to less than the leg's
        qty, the rest never fills (a partially filled order)."""
        self._check_time(ts)
        assert len({l.inst.underlying for l in legs}) == 1, "one order trades one underlying"
        if pieces:
            assert len(legs) == 1 and pieces[0][0] == ts
        for l in legs:
            self._check_tradable(l.inst, ts)
        st = self._story_for(legs[0].inst, ts, None if callable(label) else label)
        self._refresh_label(st)
        sm = self._story_month(st)
        before = self._story_positions(st.underlying)
        was_new = not before
        order_id = self._next("O")
        rng = stream(self.world.seed, self.pid, "fill", ts.isoformat())
        fills = []
        for leg in legs:
            for pts, pq in (pieces or [(ts, leg.qty)]):
                if pts != ts:
                    self._check_time(pts)
                    self._check_tradable(leg.inst, pts)
                price = self._price(leg.inst, leg.side, pts, rng, at_mid)
                fills.append(self._apply(leg.inst, leg.side, pq, price, pts, st, sm,
                                         order_id=order_id, source="trade"))
        if all(l.qty == int(l.qty) for l in legs):
            unit = Decimal(math.gcd(*[int(l.qty) for l in legs]))
        else:
            unit = legs[0].qty
        requested = unit
        filled = sum((f["qty"] for f in fills), ZERO) / sum((l.qty for l in legs), ZERO) * unit
        net = sum(((1 if f["side"] == "sell" else -1) * f["price"] * f["qty"] for f in fills), ZERO) / filled
        if filled < requested:
            self.dq_issues.append(("order_partially_filled", order_id))
        self.orders.append({
            "order_id": order_id, "ts_utc": ts, "leg_exec_ids": [f["exec_id"] for f in fills],
            "order_type": order_type, "net_price": net.quantize(Decimal("0.0001"), ROUND_HALF_UP),
            "credit_debit": "credit" if net > 0 else ("debit" if net < 0 else "even"),
            "filled_status": "partial" if filled < requested else "filled",
            "requested_units": requested, "filled_units": filled,
        })
        after = self._story_positions(st.underlying)
        if callable(label):
            label = label() if after else None
        cash_flow = sum(((1 if f["side"] == "sell" else -1) * f["price"] * f["qty"] * f["multiplier"]
                         for f in fills if f["asset_class"] != "future"), ZERO)
        self._record_effect(st, sm, order_id, ts, before, after, fills, cash_flow, was_new, label)
        return order_id

    def _check_tradable(self, inst: Instrument, ts: datetime) -> None:
        if not self.world.is_open(inst.price_symbol, ts):
            raise RuntimeError(f"{self.pid}: {inst.symbol} is not trading at {cal.ny(ts)}")
        if inst.asset_class in ("equity_option", "index_option"):
            if cal.session_of(ts) != "regular":
                raise RuntimeError(f"{self.pid}: options trade in the regular session only ({cal.ny(ts)})")
        if inst.is_option and cal.ny_date(ts) > inst.expiration:
            raise RuntimeError(f"{self.pid}: {inst.symbol} already expired")
        if cal.ny_date(ts) in cal.HOLIDAYS and inst.asset_class != "crypto":
            raise RuntimeError(f"{self.pid}: exchange closed on {cal.ny_date(ts)}")

    def _price(self, inst: Instrument, side: str, ts: datetime, rng, at_mid: bool = False) -> Decimal:
        if inst.is_option:
            q = Q.quote(self.world, inst, ts)
            price = Q.option_fill_price(self.world, inst, q, side, rng, at_mid)
            return price
        return Q.linear_fill_price(self.world, inst, ts, side)

    def _story_positions(self, underlying: str) -> dict[str, Decimal]:
        return {s: p.qty for s, p in self.positions.items() if p.inst.underlying == underlying}

    # ------------------------------------------------------------------ effects and rolls

    def _record_effect(self, st: Story, sm: StoryMonth, order_id: str, ts: datetime,
                       before: dict, after: dict, fills: list[dict], cash_flow: Decimal,
                       was_new: bool, label: str | None) -> None:
        reduced = {s for s in before if abs(after.get(s, ZERO)) < abs(before[s])}
        removed = {s for s in before if s not in after}
        increased = {s for s in after if abs(after[s]) > abs(before.get(s, ZERO))}
        new = {s for s in after if s not in before}
        only_closing = bool(reduced) and not increased
        only_opening = bool(increased) and not reduced
        for f in fills:
            if f["asset_class"] in ("equity_option", "index_option", "future_option"):
                self.quotes[f["exec_id"]] = None   # filled in by _capture_quotes
        self._capture_quotes(fills)
        if was_new:
            effect = "open"
            sm.label_open = label or st.label
            st.label = label or st.label
            sm.opening_cash = cash_flow
            exps = [f["expiration"] for f in fills if f["expiration"] is not None]
            sm.opening_dte = (min(exps) - cal.ny_date(ts)).days if exps else None
        elif not after:
            effect = "close"
        elif reduced and increased:
            effect = "roll"
        elif only_closing:
            effect = "adjust" if removed else "partial_close"
        elif new:
            effect = "adjust"
        else:
            effect = "add"
        if effect == "open":
            sm.upfront_cash = cash_flow
        elif effect == "add":
            sm.upfront_cash += cash_flow
        rec = [order_id, effect, ts, only_closing, only_opening]
        sm.orders.append(rec)
        roll_before = before
        roll_orders = [order_id]
        roll_fills = fills
        prev = st.last_order
        if (effect in ("adjust", "add") and only_opening and prev is not None
                and prev["only_closing"] and prev["month"] == self.month
                and (ts - prev["ts"]).total_seconds() <= ROLL_WINDOW_SECONDS and prev["after"]):
            prev["rec"][1] = "roll"
            rec[1] = effect = "roll"
            roll_before = prev["before"]
            roll_orders = [prev["rec"][0], order_id]
            roll_fills = prev["fills"] + fills
        if effect == "roll":
            self._record_roll(st, sm, roll_orders, roll_before, after, roll_fills)
        st.last_order = {"rec": rec, "ts": ts, "only_closing": only_closing, "before": before,
                         "after": after, "fills": fills, "month": self.month}
        if not after:
            self._close_story(st, ts, "closed", fills)
        elif label:
            st.label = label

    def _capture_quotes(self, fills: list[dict]) -> None:
        # Re-quote each option fill at its own instant (cheap: theo is cached).
        for f in fills:
            if f["exec_id"] in self.quotes and self.quotes[f["exec_id"]] is None:
                inst = self._inst_from_fill(f)
                q = Q.quote(self.world, inst, f["ts_utc"])
                self.quotes[f["exec_id"]] = {
                    "theo": q.theo, "bid": q.bid, "ask": q.ask,
                    "underlying_price": q.underlying_price, "iv": q.iv, "model_id": q.model_id}

    def _inst_from_fill(self, f: dict) -> Instrument:
        return self.insts[f["symbol"]]

    def _record_roll(self, st, sm, order_ids, before, after, fills) -> None:
        insts = {f["symbol"]: self._inst_from_fill(f) for f in fills}
        for s in before:
            insts.setdefault(s, self.insts[s])
        for s in after:
            insts.setdefault(s, self.insts[s])
        ctx = {c["exec_id"]: c for c in self.contexts}
        spot = ctx[fills[0]["exec_id"]]["underlying_price"]

        def legs(kind: str):
            out = []
            src = before if kind == "closed" else after
            for s, q in src.items():
                q_other = (after if kind == "closed" else before).get(s, ZERO)
                if abs(q_other) < abs(q):
                    inst = insts[s]
                    if inst.is_option:
                        out.append((inst.put_call, "short" if q < 0 else "long", inst))
            return out

        closed, opened = legs("closed"), legs("opened")
        types: set[str] = set()
        shorts_before = {pc: [insts[s] for s, q in before.items() if q < 0 and insts[s].is_option and insts[s].put_call == pc]
                         for pc in ("C", "P")}
        tested = None
        if shorts_before["C"] and shorts_before["P"]:
            kc = min(i.strike for i in shorts_before["C"])
            kp = max(i.strike for i in shorts_before["P"])
            tested = "C" if abs(kc - spot) < abs(spot - kp) else "P"
        for pc in ("C", "P"):
            for side in ("short", "long"):
                olds = sorted([i for p, sd, i in closed if p == pc and sd == side], key=lambda i: (i.strike, i.expiration))
                news = sorted([i for p, sd, i in opened if p == pc and sd == side], key=lambda i: (i.strike, i.expiration))
                for o, n in zip(olds, news):
                    if n.expiration > o.expiration:
                        types.add("out_in_time")
                    if n.strike > o.strike:
                        types.add("up")
                    elif n.strike < o.strike:
                        types.add("down")
                    if tested and side == "short":
                        if pc != tested and abs(n.strike - spot) < abs(o.strike - spot):
                            types.add("untested_toward_money")
                        if pc == tested and abs(n.strike - spot) > abs(o.strike - spot):
                            types.add("tested_away_from_money")
        wb, wa = self._width(before, insts), self._width(after, insts)
        if wb is not None and wa is not None:
            if wa > wb:
                types.add("widened")
            elif wa < wb:
                types.add("narrowed")
        net = sum(((1 if f["side"] == "sell" else -1) * f["price"] * f["qty"] * f["multiplier"]
                   for f in fills if f["asset_class"] != "future"), ZERO)
        sm.rolls.append({"order_ids": list(order_ids), "ts_utc": fills[0]["ts_utc"],
                         "types": sorted(types), "net_cash": net, "credit": net > 0})

    @staticmethod
    def _width(pos: dict, insts: dict) -> Decimal | None:
        sc = [insts[s].strike for s, q in pos.items() if q < 0 and insts[s].is_option and insts[s].put_call == "C"]
        sp = [insts[s].strike for s, q in pos.items() if q < 0 and insts[s].is_option and insts[s].put_call == "P"]
        if sc and sp:
            return min(sc) - max(sp)
        for pc in ("P", "C"):
            sh = sorted(insts[s].strike for s, q in pos.items() if q < 0 and insts[s].is_option and insts[s].put_call == pc)
            lg = sorted(insts[s].strike for s, q in pos.items() if q > 0 and insts[s].is_option and insts[s].put_call == pc)
            if sh and lg:
                return abs(sh[0] - lg[0])
        return None

    def _close_story(self, st: Story, ts: datetime, status: str, fills: list[dict]) -> None:
        st.status = status
        st.closed_ts = ts
        st.label_before_close = st.label
        exps = [f["expiration"] for f in fills if f.get("expiration") is not None]
        if status == "closed":
            st.days_left_at_close = (min(exps) - cal.ny_date(ts)).days if exps else None
        else:
            st.days_left_at_close = 0 if exps or status in ("expired", "assigned") else None
        del self.stories[st.underlying]

    # ------------------------------------------------------------------ events

    def _event(self, etype: str, ts: datetime, inst: Instrument, qty: Decimal, cash: Decimal,
               st: Story, *, settlement: Decimal | None = None, resulting: list[str] | None = None,
               ratio: Decimal | None = None, adjusted: dict | None = None, early: bool = False) -> str:
        eid = self._next("V")
        self.events.append({
            "event_id": eid, "type": etype, "ts_utc": ts, "symbol": inst.symbol,
            "underlying": inst.underlying, "qty": qty, "cash_amount": cash,
            "settlement_price": settlement, "resulting_exec_ids": resulting or [],
            "split_ratio": ratio, "adjusted_symbols": adjusted, "early": early,
        })
        self._story_month(st).event_ids.append(eid)
        return eid

    def _remove_option(self, pos: Position, price: Decimal, st: Story, sm: StoryMonth) -> Decimal:
        """Close an option position at `price` without an order (expiration, assignment)."""
        q0 = pos.qty
        proceeds = q0 * price * pos.inst.multiplier          # long receives, short pays
        realized = proceeds - pos.cost
        self.cash += proceeds
        st.cash_net += proceeds
        sm.realized += realized
        sm.realized_true += realized
        del self.positions[pos.inst.symbol]
        return proceeds

    def expirations(self, day: date) -> None:
        """Settle every option expiring today, after the close."""
        for und in sorted({p.inst.underlying for p in self.positions.values()
                           if p.inst.is_option and p.inst.expiration == day}):
            self._refresh_label(self.stories[und])
        for sym in sorted(self.positions):
            pos = self.positions.get(sym)
            if pos is None or not pos.inst.is_option or pos.inst.expiration != day:
                continue
            inst = pos.inst
            ts = Q.settlement_utc(inst)
            st = self.stories[inst.underlying]
            sm = self._story_month(st)
            s = Decimal(repr(self.world.close(inst.price_symbol, day))).quantize(Q.PRICE_PLACES)
            intrinsic = max(ZERO, s - inst.strike) if inst.put_call == "C" else max(ZERO, inst.strike - s)
            if inst.asset_class == "index_option":
                if intrinsic > 0:
                    cash = self._remove_option(pos, intrinsic, st, sm)
                    self._event("expiration_cash_settled", ts, inst, pos.qty, cash, st, settlement=s)
                else:
                    self._remove_option(pos, ZERO, st, sm)
                    self._event("expiration_worthless", ts, inst, pos.qty, ZERO, st, settlement=s)
                self._after_event(st, ts, "expired")
            elif intrinsic >= CENT:
                self._deliver(pos, ts, st, sm, early=False)
            else:
                self._remove_option(pos, ZERO, st, sm)
                self._event("expiration_worthless", ts, inst, pos.qty, ZERO, st, settlement=s)
                self._after_event(st, ts, "expired")

    def assign_early(self, ts: datetime, symbol: str) -> None:
        self._check_time(ts)
        pos = self.positions[symbol]
        st = self.stories[pos.inst.underlying]
        self._refresh_label(st)
        self._deliver(pos, ts, st, self._story_month(st), early=True)

    def _deliver(self, pos: Position, ts: datetime, st: Story, sm: StoryMonth, early: bool) -> None:
        """Assignment (short) or exercise (long) into shares or a future at the strike."""
        inst = pos.inst
        if inst.asset_class == "future_option":
            from mock.market.listings import future
            under = future(self.world, inst.underlying)
        else:
            from mock.market.listings import stock
            under = stock(inst.underlying)
        n = abs(pos.qty) if inst.asset_class == "future_option" else abs(pos.qty) * inst.multiplier
        short = pos.qty < 0
        if inst.put_call == "P":
            side = "buy" if short else "sell"
        else:
            side = "sell" if short else "buy"
        qty0 = pos.qty
        self._remove_option(pos, ZERO, st, sm)
        fill = self._apply(under, side, n, inst.strike, ts, st, sm, order_id=None, source="event")
        etype = "assignment" if short else "exercise"
        cash = -(n * inst.strike * under.multiplier) if side == "buy" else n * inst.strike * under.multiplier
        if under.is_future:
            cash = ZERO
        self._event(etype, ts, inst, qty0, cash, st, resulting=[fill["exec_id"]], early=early)
        st.last_order = None
        self._after_event(st, ts, "assigned")

    def _refresh_label(self, st: Story) -> None:
        """Labels that depend on the cash balance (cash-secured puts) are re-judged
        just before each order or event on the story and at month end."""
        if self.labeler is not None and self._story_positions(st.underlying):
            st.label = self.labeler(st.underlying) or st.label

    def _after_event(self, st: Story, ts: datetime, status: str) -> None:
        if not self._story_positions(st.underlying):
            self._close_story(st, ts, status, [])
        elif status == "assigned" and self.labeler is not None:
            st.label = self.labeler(st.underlying) or st.label

    def corporate_actions(self, day: date) -> None:
        ts = cal.at_ny(day, 4)
        for sym, ex, amt in self.world.dividends:
            if ex != day:
                continue
            pos = self.positions.get(sym)
            if pos is None:
                continue
            st = self.stories[sym]
            sm = self._story_month(st)
            cash = (pos.qty * amt).quantize(CENT, ROUND_HALF_UP)
            self.cash += cash
            sm.realized += cash
            sm.realized_true += cash
            self._event("dividend", ts, pos.inst, pos.qty, cash, st)
        for (sym, d), ratio in sorted(self.world.splits.items()):
            if d != day:
                continue
            affected = [s for s, p in self.positions.items() if p.inst.underlying == sym]
            if not affected:
                continue
            st = self.stories[sym]
            adjusted = {}
            for s in sorted(affected):
                p = self.positions.pop(s)
                if p.inst.is_option:
                    new_inst = split_adjusted(p.inst, ratio)
                else:
                    new_inst = p.inst
                adjusted[s] = new_inst.symbol
                self.positions[new_inst.symbol] = Position(new_inst, p.qty * ratio, p.cost, p.opened)
            stock_pos = self.positions.get(sym)
            qty = stock_pos.qty / ratio if stock_pos else ZERO
            from mock.market.listings import stock
            eid = self._event("split", ts, stock(sym), qty, ZERO, st, ratio=Decimal(ratio), adjusted=adjusted)
            self.dq_issues.append(("split_adjusted_positions", eid))

    # ------------------------------------------------------------------ cash flows

    def cash_flow(self, d: date, kind: str, amount: Decimal) -> None:
        self.cash += amount if kind == "deposit" else -amount
        self.month_flows.append({"date": d, "kind": kind, "amount": amount})

    # ------------------------------------------------------------------ carry-in (pre-month history)

    def seed_position(self, inst: Instrument, qty: Decimal, price: Decimal, opened: date, label: str) -> None:
        """A position opened before the simulated months begin (no fills are written)."""
        st = self.stories.get(inst.underlying)
        if st is None:
            self._uid += 1
            st = Story(self._uid, inst.underlying, inst.asset_class, opened, label)
            self.stories[inst.underlying] = st
            self.all_stories.append(st)
        st.label = label
        cost = qty * price * inst.multiplier
        self.insts[inst.symbol] = inst
        if not inst.is_future:
            st.cash_net -= cost
        if inst.symbol in self.positions:
            p = self.positions[inst.symbol]
            p.qty += qty
            p.cost += cost
        else:
            self.positions[inst.symbol] = Position(inst, qty, cost, opened)
        if not inst.is_future:
            self.cash -= cost

    # ------------------------------------------------------------------ month end

    def end_month(self) -> dict:
        at = self.valuation_time(self.month)
        self.advance(max(at["exchange"], at["crypto"]))
        for st in self.stories.values():
            self._refresh_label(st)
        end_snap = self.snapshot("end", at)
        end_value = self.value(end_snap)
        flows = sum(((f["amount"] if f["kind"] == "deposit" else -f["amount"]) for f in self.month_flows), ZERO)
        u_start = sum((l["qty"] * l["mark"] * l["multiplier"] - l["_cost_exact"] for l in self.start_snapshot["lines"]), ZERO)
        u_end = sum((l["qty"] * l["mark"] * l["multiplier"] - l["_cost_exact"] for l in end_snap["lines"]), ZERO)
        realized_true = sum((st.months[self.month].realized_true for st in self.month_stories), ZERO)
        gap = (end_value - self.start_value - flows) - (realized_true + u_end - u_start)
        if gap != 0:
            raise AssertionError(f"{self.pid} {self.month}: ledger does not balance by {gap}")
        views = [self._story_view(st, end_snap) for st in self.month_stories]
        return {
            "month": self.month, "persona": self.p, "start_snapshot": self.start_snapshot,
            "end_snapshot": end_snap, "start_value": self.start_value, "end_value": end_value,
            "flows": list(self.month_flows), "fills": list(self.fills), "orders": list(self.orders),
            "events": list(self.events), "contexts": list(self.contexts), "quotes": dict(self.quotes),
            "stories": views, "dq_issues": list(self.dq_issues),
        }

    def _story_view(self, st: Story, end_snap: dict) -> dict:
        sm = st.months[self.month]
        ended = st.status != "open" and st.closed_ts is not None and cal.ny_date(st.closed_ts).strftime("%Y-%m") == self.month             and self.stories.get(st.underlying) is not st
        still_open = self.stories.get(st.underlying) is st
        lines = [l for l in end_snap["lines"] if l["underlying"] == st.underlying] if still_open else []
        opened_date = sm.start_opened_date if sm.carried_in else st.opened_date
        closed_ts = st.closed_ts if ended else None
        time_order = {f["exec_id"]: (f["ts_utc"], f["exec_id"]) for f in self.fills}
        return {
            "story_id": sm.sid, "underlying": st.underlying, "asset_class": sm.asset_class or st.asset_class,
            "carried_in": sm.carried_in, "label_open": sm.label_open,
            "label_end": st.label_before_close if ended else st.label,
            "orders": [{"order_id": o[0], "effect": o[1]} for o in sm.orders],
            "exec_ids": sorted(sm.exec_ids, key=lambda e: time_order[e]),
            "event_ids": list(sm.event_ids),
            "rolls": [dict(r) for r in sm.rolls],
            "opening_cash": sm.opening_cash,
            "upfront_cash": sm.upfront_cash,
            "opening_dte": None if sm.carried_in else sm.opening_dte,
            "opened_date": opened_date,
            "closed_ts": closed_ts,
            "days_held": (cal.ny_date(closed_ts) - opened_date).days if closed_ts else None,
            "days_left_at_close": st.days_left_at_close if ended else None,
            "realized_pnl": sm.realized.quantize(Decimal("0.0001"), ROUND_HALF_UP),
            "fees": sm.fees,
            "status": st.status if ended else "open",
            "end_positions": {l["symbol"]: l["qty"] for l in lines},
            "open_pnl_end": sum((l["qty"] * l["mark"] * l["multiplier"] - l["cost_basis"] for l in lines), ZERO),
            "open_pnl_start": sm.open_pnl_start,
        }

    # ------------------------------------------------------------------ helpers for engines

    def holding(self, symbol: str) -> Decimal:
        p = self.positions.get(symbol)
        return p.qty if p else ZERO

    def story_legs(self, underlying: str) -> list[Position]:
        return [p for s, p in sorted(self.positions.items()) if p.inst.underlying == underlying]

    def story_label(self, underlying: str) -> str | None:
        st = self.stories.get(underlying)
        return st.label if st else None

    def set_label(self, underlying: str, label: str) -> None:
        self.stories[underlying].label = label

    def story_cash_collected(self, underlying: str) -> Decimal:
        """Net cash (before fees) the open story has brought in since it opened."""
        return self.stories[underlying].cash_net
