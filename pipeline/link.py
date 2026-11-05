"""Stage 2b: the trade linker. Turns one customer's RawMonth into TradeStory[].

Replays the month in time order (events before orders at the same instant, each kind
in ID order), keeping per-instrument positions at average cost:

- carried-in positions open a story each (per underlying) from the month-start
  snapshot, with the snapshot's cost basis;
- each order is applied as a whole at its own timestamp, even when its legs filled in
  pieces later on;
- expirations, assignments, exercises, splits and dividends change the open story
  without an order. Assignment shares stay in the same story (the wheel is one story);
- a story ends when its underlying's position is flat.

Definitions (stories, effects, rolls, P&L) are in schemas/trades.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal

from pipeline.classify import Confidence, Instrument, Leg, change, classify, order_effect, roll_types
from pipeline.nytime import ny_date, ny_midnight_utc
from schemas.raw import Event, Fill, Order, RawMonth
from schemas.trades import ROLL_WINDOW_SECONDS, OrderEffect, StrategyLabel, TradeStory

ZERO = Decimal(0)
Q8 = Decimal("0.00000001")
Q4 = Decimal("0.0001")


@dataclass
class Position:
    inst: Instrument
    qty: Decimal
    cost: Decimal            # signed: sum of qty * price * multiplier at average cost


@dataclass
class _Story:
    sid: str
    underlying: str
    asset_class: str
    carried_in: bool
    opened_date: object
    label_open: StrategyLabel | None = None
    label: StrategyLabel | None = None
    label_confidence: Confidence | None = None
    orders: list[list] = field(default_factory=list)      # [order_id, effect]
    exec_ids: list[str] = field(default_factory=list)
    event_ids: list[str] = field(default_factory=list)
    rolls: list[dict] = field(default_factory=list)
    realized: Decimal = ZERO
    fees: Decimal = ZERO
    opening_cash: Decimal = ZERO
    upfront_cash: Decimal = ZERO
    opening_dte: int | None = None
    open_pnl_start: Decimal = ZERO
    status: str = "open"
    closed_ts: datetime | None = None
    days_left_at_close: int | None = None
    last_order: dict | None = None
    label_ts: datetime | None = None


@dataclass(frozen=True)
class DataQualityIssue:
    issue: str
    refs: tuple[str, ...]


@dataclass
class LinkResult:
    stories: list[TradeStory]
    data_quality_issues: list[DataQualityIssue]
    labels_confidence: dict[str, Confidence]      # story_id -> confidence of label_end
    unknown_open_dates: frozenset[str] = frozenset()   # story_ids whose opening date is a stand-in


def forget_open_dates(linked: LinkResult, underlyings) -> LinkResult:
    """Carried-in trades whose start positions came without an opened date (an upload
    default): keep the stand-in date for ordering, but drop how long they were held and
    mark them so the recap does not state when they were opened."""
    under = set(underlyings)
    hit = {s.story_id for s in linked.stories if s.carried_in and s.underlying in under}
    if not hit:
        return linked
    stories = [s.model_copy(update={"days_held": None}) if s.story_id in hit else s for s in linked.stories]
    return LinkResult(stories, linked.data_quality_issues, linked.labels_confidence, frozenset(hit))


def _inst_from_fill(f: Fill) -> Instrument:
    return Instrument(f.symbol, str(f.asset_class), f.underlying, f.multiplier, f.strike,
                      f.expiration, str(f.put_call) if f.put_call else None, f.contract_month)


def _inst_from_line(l) -> Instrument:
    return Instrument(l.symbol, str(l.asset_class), l.underlying, l.multiplier, l.strike,
                      l.expiration, str(l.put_call) if l.put_call else None, l.contract_month)


class Linker:
    def __init__(self, raw: RawMonth):
        self.raw = raw
        self.cid = raw.account.customer_id
        self.fills = {f.exec_id: f for f in raw.fills}
        self.ctx = {c.exec_id: c for c in raw.market.fills}
        self.insts: dict[str, Instrument] = {}
        for l in raw.positions_start.lines + raw.positions_end.lines:
            self.insts[l.symbol] = _inst_from_line(l)
        for f in raw.fills:
            self.insts[f.symbol] = _inst_from_fill(f)
        self.positions: dict[str, Position] = {}
        self.open: dict[str, _Story] = {}                 # underlying -> open story
        self.stories: list[_Story] = []
        self.cash = raw.positions_start.cash
        self.issues: dict[str, list[str]] = {}
        self.flows = sorted(raw.account.cash_flows, key=lambda c: c.date)
        self.relabel_pending: set[str] = set()

    # ------------------------------------------------------------------ helpers

    def _new_story(self, underlying: str, asset_class: str, carried: bool, opened) -> _Story:
        st = _Story(f"{self.cid}-{len(self.stories) + 1:04d}", underlying, asset_class, carried, opened)
        self.stories.append(st)
        self.open[underlying] = st
        return st

    def _held(self, underlying: str) -> dict[str, Decimal]:
        return {s: p.qty for s, p in self.positions.items() if p.inst.underlying == underlying}

    def _classify(self, underlying: str) -> tuple[StrategyLabel, Confidence]:
        legs = [Leg(p.inst, p.qty) for p in self.positions.values() if p.inst.underlying == underlying]
        return classify(legs, self.cash)

    def _issue(self, kind: str, ref: str) -> None:
        self.issues.setdefault(kind, []).append(ref)

    def _apply_fill(self, f: Fill, st: _Story) -> None:
        dq = f.qty if f.side == "buy" else -f.qty
        pos = self.positions.get(f.symbol)
        if pos is not None and pos.qty != 0 and (pos.qty > 0) != (dq > 0) and abs(dq) > abs(pos.qty):
            # closes more than is held: close to flat, then open the rest
            self._issue("fill_exceeds_position", f.exec_id)
            self._book(f, -pos.qty, st)
            self._book(f, dq + pos.qty, st, with_fees=False)
        else:
            self._book(f, dq, st)
        st.exec_ids.append(f.exec_id)

    def _book(self, f: Fill, dq: Decimal, st: _Story, with_fees: bool = True) -> None:
        inst = self.insts[f.symbol]
        m = Decimal(inst.multiplier)
        is_future = inst.asset_class == "future"
        pos = self.positions.get(f.symbol)
        q0 = pos.qty if pos else ZERO
        realized = ZERO
        if q0 == 0 or (q0 > 0) == (dq > 0):
            if pos is None:
                pos = self.positions[f.symbol] = Position(inst, ZERO, ZERO)
            pos.qty += dq
            pos.cost += dq * f.price * m
            if not is_future:
                self.cash -= dq * f.price * m
        else:
            removed = (pos.cost * abs(dq) / abs(q0)).quantize(Q8)
            proceeds = -dq * f.price * m
            realized = proceeds - removed
            pos.qty += dq
            pos.cost -= removed
            if pos.qty == 0:
                realized -= pos.cost          # rounding residue belongs to the realized P&L
                del self.positions[f.symbol]
            self.cash += realized if is_future else proceeds
        st.realized += realized
        if not with_fees:
            return
        if f.commission is None or f.fees is None:
            self._issue("fees_missing", f.exec_id)
        paid = (f.commission or ZERO) + (f.fees or ZERO)
        self.cash -= paid
        st.realized -= paid
        st.fees += paid

    def _close(self, st: _Story, ts: datetime, status: str, days_left: int | None) -> None:
        st.status, st.closed_ts, st.days_left_at_close = status, ts, days_left
        del self.open[st.underlying]

    # ------------------------------------------------------------------ carried in

    def carry_in(self) -> None:
        by_under: dict[str, list] = {}
        for l in self.raw.positions_start.lines:
            by_under.setdefault(l.underlying, []).append(l)
        for under, lines in sorted(by_under.items(), key=lambda kv: (min(l.opened_date for l in kv[1]), kv[0])):
            first = min(lines, key=lambda l: (l.opened_date, l.asset_class not in ("stock_etf", "future", "crypto"), l.symbol))
            st = self._new_story(under, str(first.asset_class), True, min(l.opened_date for l in lines))
            for l in lines:
                self.positions[l.symbol] = Position(self.insts[l.symbol], l.qty, l.cost_basis)
            st.open_pnl_start = sum((l.qty * l.mark * l.multiplier - l.cost_basis for l in lines), ZERO)
            st.opening_cash = st.upfront_cash = -sum((l.cost_basis for l in lines if l.asset_class != "future"), ZERO)
            st.label_open, st.label_confidence = self._classify(under)
            st.label = st.label_open

    # ------------------------------------------------------------------ orders

    def apply_order(self, o: Order) -> None:
        fills = [self.fills[e] for e in o.leg_exec_ids]
        unders = {f.underlying for f in fills}
        if len(unders) != 1:
            raise ValueError(f"{o.order_id}: one order must trade one underlying, got {sorted(unders)}")
        under = unders.pop()
        st = self.open.get(under)
        if st is not None:
            self._refresh(st, o.ts_utc)
        if st is None:
            st = self._new_story(under, str(fills[0].asset_class), False, ny_date(o.ts_utc))
        before = self._held(under)
        for f in fills:
            self._apply_fill(f, st)
        after = self._held(under)
        c = change(before, after)
        effect = order_effect(c)
        cash_flow = sum(((1 if f.side == "sell" else -1) * f.price * f.qty * f.multiplier
                         for f in fills if f.asset_class != "future"), ZERO)
        if effect == OrderEffect.open:
            st.opening_cash = st.upfront_cash = cash_flow
            exps = [f.expiration for f in fills if f.expiration is not None]
            st.opening_dte = (min(exps) - ny_date(o.ts_utc)).days if exps else None
        elif effect == OrderEffect.add:
            st.upfront_cash += cash_flow
        rec = [o.order_id, effect]
        st.orders.append(rec)

        roll_before, roll_orders, roll_fills = before, [o.order_id], fills
        prev = st.last_order
        if (effect in (OrderEffect.adjust, OrderEffect.add) and c.only_opening and prev is not None
                and prev["only_closing"] and prev["after"]
                and (o.ts_utc - prev["ts"]).total_seconds() <= ROLL_WINDOW_SECONDS):
            prev["rec"][1] = effect = OrderEffect.roll
            rec[1] = effect
            roll_before, roll_orders, roll_fills = prev["before"], [prev["rec"][0], o.order_id], prev["fills"] + fills
        if effect == OrderEffect.roll:
            spot = self.ctx[roll_fills[0].exec_id].underlying_price
            net = sum(((1 if f.side == "sell" else -1) * f.price * f.qty * f.multiplier
                       for f in roll_fills if f.asset_class != "future"), ZERO)
            st.rolls.append({"order_ids": roll_orders, "ts_utc": roll_fills[0].ts_utc,
                             "types": roll_types(roll_before, after, self.insts, spot),
                             "net_cash": net, "credit": net > 0})
        st.last_order = {"rec": rec, "ts": o.ts_utc, "only_closing": c.only_closing,
                         "before": before, "after": after, "fills": fills}
        if c.flat_after:
            exps = [f.expiration for f in fills if f.expiration is not None]
            self._close(st, o.ts_utc, "closed", (min(exps) - ny_date(o.ts_utc)).days if exps else None)
        else:
            st.label, st.label_confidence = self._classify(under)
            if effect == OrderEffect.open:
                st.label_open = st.label
        if o.filled_status == "partial":
            self._issue("order_partially_filled", o.order_id)

    # ------------------------------------------------------------------ events

    def apply_event(self, ev: Event) -> None:
        st = self.open.get(ev.underlying)
        t = str(ev.type)
        if st is None or (t not in ("dividend", "split") and ev.symbol not in self.positions):
            self._issue("event_without_position", ev.event_id)
            return
        st.event_ids.append(ev.event_id)
        if t not in ("dividend", "split"):
            self._refresh(st, ev.ts_utc)
        if t == "dividend":
            self.cash += ev.cash_amount
            st.realized += ev.cash_amount
            return
        if t == "split":
            ratio = ev.split_ratio
            for old, new in sorted((ev.adjusted_symbols or {}).items()):
                p = self.positions.pop(old)
                inst = self.insts.get(new) or p.inst
                self.positions[new] = Position(inst, p.qty * ratio, p.cost)
            self._issue("split_adjusted_positions", ev.event_id)
            return
        pos = self.positions.pop(ev.symbol)
        # the option leaves the book: cash-settled at its settlement value, otherwise at 0
        proceeds = ev.cash_amount if t == "expiration_cash_settled" else ZERO
        self.cash += proceeds
        st.realized += proceeds - pos.cost
        for e in ev.resulting_exec_ids:
            self._apply_fill(self.fills[e], st)
        if not self._held(st.underlying):
            self._close(st, ev.ts_utc, "assigned" if t in ("assignment", "exercise") else "expired", 0)
        elif t in ("assignment", "exercise"):
            st.label, st.label_confidence = self._classify(st.underlying)
            st.last_order = None
        else:
            # legs that expire together settle one event at a time; relabel once the
            # whole instant is processed (see run)
            self.relabel_pending.add(st.underlying)

    # ------------------------------------------------------------------ run

    def run(self) -> LinkResult:
        self.carry_in()
        timeline = [(e.ts_utc, 0, e.event_id, e) for e in self.raw.events] + \
                   [(o.ts_utc, 1, o.order_id, o) for o in self.raw.orders]
        flows = list(self.flows)
        last_ts = None
        for ts, kind, _, item in sorted(timeline, key=lambda x: x[:3]):
            if ts != last_ts or kind == 1:
                self._relabel()
            last_ts = ts
            while flows and ny_midnight_utc(flows[0].date) <= ts:
                fl = flows.pop(0)
                self.cash += fl.amount if fl.kind == "deposit" else -fl.amount
            if kind == 0:
                self.apply_event(item)
            else:
                self.apply_order(item)
        self._relabel()
        self.cash = self.raw.positions_end.cash          # month-end labels use month-end cash
        for st in list(self.open.values()):
            st.label, st.label_confidence = self._classify(st.underlying)
        stories = self._finish()
        return LinkResult(stories, self._issues(), {s.sid: s.label_confidence for s in self.stories})

    def _refresh(self, st: _Story, ts: datetime) -> None:
        """Re-judge the label (cash-secured or not depends on cash) just before an
        order or event touches the story. Legs expiring together settle one event at a
        time, so this runs once per story per instant, before the first of them."""
        if st.label_ts != ts and self._held(st.underlying):
            st.label, st.label_confidence = self._classify(st.underlying)
        st.label_ts = ts

    def _relabel(self) -> None:
        for under in sorted(self.relabel_pending):
            st = self.open.get(under)
            if st is not None:
                st.label, st.label_confidence = self._classify(under)
        self.relabel_pending.clear()

    def _issues(self) -> list[DataQualityIssue]:
        return [DataQualityIssue(k, tuple(v)) for k, v in sorted(self.issues.items())]

    def _check_end(self) -> None:
        snap = {l.symbol: (l.qty, l.cost_basis) for l in self.raw.positions_end.lines}
        mine = {s: (p.qty, p.cost.quantize(Q4, ROUND_HALF_UP)) for s, p in self.positions.items()}
        for sym in sorted(set(snap) | set(mine)):
            if snap.get(sym) != mine.get(sym):
                self._issue("positions_do_not_reconcile", sym)

    def _finish(self) -> list[TradeStory]:
        self._check_end()
        marks = {l.symbol: l.mark for l in self.raw.positions_end.lines}
        order_of = {f.exec_id: (f.ts_utc, f.exec_id) for f in self.raw.fills}
        out = []
        for st in self.stories:
            is_open = self.open.get(st.underlying) is st
            held = sorted((s, p) for s, p in self.positions.items() if p.inst.underlying == st.underlying) if is_open else []
            closed_ts = None if is_open else st.closed_ts
            out.append(TradeStory(
                story_id=st.sid, underlying=st.underlying, asset_class=st.asset_class,
                carried_in=st.carried_in, label_open=st.label_open, label_end=st.label,
                orders=[{"order_id": o, "effect": e} for o, e in st.orders],
                exec_ids=sorted(st.exec_ids, key=lambda e: order_of[e]),
                event_ids=list(st.event_ids),
                rolls=st.rolls,
                opening_cash=st.opening_cash, upfront_cash=st.upfront_cash,
                opening_dte=None if st.carried_in else st.opening_dte,
                opened_date=st.opened_date, closed_ts=closed_ts,
                days_held=(ny_date(closed_ts) - st.opened_date).days if closed_ts else None,
                days_left_at_close=None if is_open else st.days_left_at_close,
                realized_pnl=st.realized.quantize(Q4, ROUND_HALF_UP),
                fees=st.fees, status="open" if is_open else st.status,
                end_positions={s: p.qty for s, p in held},
                open_pnl_end=sum((p.qty * marks.get(s, ZERO) * p.inst.multiplier - p.cost.quantize(Q4, ROUND_HALF_UP)
                                  for s, p in held), ZERO),
                open_pnl_start=st.open_pnl_start,
            ))
        return out


def link(raw: RawMonth) -> LinkResult:
    return Linker(raw).run()
