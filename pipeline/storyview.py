"""A team page for checking a month's trade stories by eye: for every story, a chart of the
legs held over time (strike on the vertical axis, one column per order or event) and the
order-by-order ledger behind it, with the realized gain or loss of each step and the
running total. A summary at the top ties the stories back to the facts sheet and the
account.

It replays the month with the same Linker the recap uses, recording each step on the way,
so what it shows is exactly what the facts were built from. Team-only: nothing here is
customer copy.

    python -m pipeline.storyview <workbook.xlsx> <out.html>
"""

from __future__ import annotations

import html
import math
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

from pipeline.link import Linker, LinkResult
from pipeline.nytime import ny
from schemas.facts import FactsSheet
from schemas.raw import RawMonth

ZERO = Decimal(0)
CENT = Decimal("0.01")

EFFECT_WORDS = {"open": "Opened", "close": "Closed", "roll": "Roll", "adjust": "Adjusted",
                "add": "Added to", "partial_close": "Partly closed"}
EVENT_WORDS = {"expiration": "Expired", "expiration_worthless": "Expired worthless",
               "expiration_cash_settled": "Expired, cash settled", "assignment": "Assigned",
               "exercise": "Exercised", "dividend": "Dividend", "split": "Split"}
ROLL_WORDS = {"out_in_time": "out in time", "up": "up", "down": "down",
              "untested_toward_money": "side not under pressure moved toward the price",
              "tested_away_from_money": "side under pressure moved away from the price",
              "widened": "widened", "narrowed": "narrowed"}
ROLL_SHORT = {"out_in_time": "out", "up": "up", "down": "down", "untested_toward_money": "toward",
              "tested_away_from_money": "away", "widened": "wider", "narrowed": "narrower"}


# --------------------------------------------------------------------------- recording

@dataclass
class Step:
    kind: str                     # "start" | "order" | "event"
    ref: str                      # order id, event id, or "start"
    ts: object
    sid: str
    fills: list[dict] = field(default_factory=list)
    held: dict = field(default_factory=dict)      # symbol -> (qty, average cost per unit)
    realized: Decimal = ZERO      # the story's running realized total after this step
    delta: Decimal = ZERO         # realized by this step, after fees
    fees: Decimal = ZERO
    cash: Decimal | None = None   # option and share premium, signed (+ credit)
    spot: Decimal | None = None
    event_type: str | None = None


class _Recorder(Linker):
    """The recap's Linker, unchanged, with a log of every booking and step."""

    def __init__(self, raw: RawMonth):
        super().__init__(raw)
        self.steps: list[Step] = []
        self._booked: list[dict] = []

    def _snapshot(self, under: str) -> dict:
        out = {}
        for s, p in sorted(self.positions.items()):
            if p.inst.underlying == under and p.qty != 0:
                out[s] = (p.qty, p.cost / p.qty / Decimal(p.inst.multiplier))
        return out

    def _book(self, f, dq, st, with_fees=True):
        pos = self.positions.get(f.symbol)
        q0 = pos.qty if pos else ZERO
        closing = q0 != 0 and (q0 > 0) != (dq > 0)
        avg = pos.cost / q0 / Decimal(pos.inst.multiplier) if closing else None
        before = st.realized
        super()._book(f, dq, st, with_fees)
        paid = ((f.commission or ZERO) + (f.fees or ZERO)) if with_fees else ZERO
        self._booked.append({"fill": f, "dq": dq, "avg_cost": avg, "closing": closing,
                             "realized": st.realized - before + paid, "fees": paid,
                             "fees_missing": with_fees and (f.commission is None or f.fees is None)})

    def carry_in(self):
        super().carry_in()
        for st in self.stories:
            self.steps.append(Step("start", "start", None, st.sid, held=self._snapshot(st.underlying)))

    def _story_of_order(self, order_id: str):
        for st in reversed(self.stories):
            if st.orders and st.orders[-1][0] == order_id:
                return st
        raise KeyError(order_id)

    def apply_order(self, o):
        self._booked = []
        st0 = None
        fills = [self.fills[e] for e in o.leg_exec_ids]
        if fills:
            st0 = self.open.get(fills[0].underlying)
        before = st0.realized if st0 is not None else ZERO
        super().apply_order(o)
        st = self._story_of_order(o.order_id)
        if st is not st0:
            before = ZERO
        cash = sum(((1 if f.side == "sell" else -1) * f.price * f.qty * f.multiplier
                    for f in fills if f.asset_class != "future"), ZERO)
        self.steps.append(Step("order", o.order_id, o.ts_utc, st.sid, self._booked,
                               self._snapshot(st.underlying), st.realized, st.realized - before,
                               sum((b["fees"] for b in self._booked), ZERO), cash,
                               self.ctx[fills[0].exec_id].underlying_price if fills[0].exec_id in self.ctx else None))

    def apply_event(self, ev):
        self._booked = []
        st = self.open.get(ev.underlying)
        before = st.realized if st is not None else ZERO
        super().apply_event(ev)
        if st is None or ev.event_id not in st.event_ids:
            return
        self.steps.append(Step("event", ev.event_id, ev.ts_utc, st.sid, self._booked,
                               self._snapshot(st.underlying), st.realized, st.realized - before,
                               sum((b["fees"] for b in self._booked), ZERO), ev.cash_amount or None,
                               event_type=str(ev.type)))


# --------------------------------------------------------------------------- formatting

def esc(x) -> str:
    return html.escape(str(x), quote=True)


def money(x, sign: bool = False) -> str:
    x = Decimal(x).quantize(CENT)
    s = f"${abs(x):,.2f}"
    if x < 0:
        return "-" + s
    return ("+" + s) if sign and x > 0 else s


def num(x) -> str:
    x = Decimal(x)
    q = x.quantize(Decimal("0.0001")).normalize()
    if q == q.to_integral():
        return f"{q:,.0f}"
    s = f"{q:,f}"
    return s if len(s.split(".")[1]) >= 2 else f"{q:,.2f}"


def qty(x) -> str:
    return num(abs(x))


def leg_name(inst) -> str:
    if inst.strike is not None and inst.put_call:
        exp = f"{inst.expiration:%b} {inst.expiration.day}" if inst.expiration else ""
        return f"{num(inst.strike)} {'put' if inst.put_call == 'P' else 'call'} {exp}".strip()
    if inst.asset_class == "future":
        return f"{inst.underlying} {inst.contract_month or ''}".strip()
    return inst.underlying


def leg_short(inst, q) -> str:
    side = "short" if q < 0 else "long"
    if inst.strike is not None and inst.put_call:
        exp = f"{inst.expiration:%m/%d}" if inst.expiration else ""
        return f"{side} {qty(q)} {num(inst.strike)}{inst.put_call} {exp}".strip()
    return f"{side} {qty(q)} {leg_name(inst)}"


def when(ts) -> str:
    if ts is None:
        return "Start of month"
    t = ny(ts)
    return f"{t:%a} {t:%b} {t.day}, {t:%H:%M}"


def kind_of(inst) -> str:
    if inst.put_call == "P":
        return "put"
    if inst.put_call == "C":
        return "call"
    return "under"


# --------------------------------------------------------------------------- the chart

COL, LEFT, RIGHT, TOP, BOTTOM, PLOT_H = 34, 64, 170, 130, 46, 300


def _ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    span = hi - lo or 1.0
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw), default=mag * 10)
    first = math.ceil(lo / step) * step
    out, v = [], first
    while v <= hi + 1e-9:
        out.append(round(v, 6))
        v += step
    return out


def _yval(inst, avg) -> float:
    return float(inst.strike) if inst.strike is not None else float(avg)


def chart(steps: list[Step], insts: dict, rolls: list[dict], number: dict[str, int]) -> str:
    n = len(steps)
    open_at_end = bool(steps[-1].held)
    cols = n + (1 if open_at_end else 0)
    width = LEFT + COL * cols + RIGHT
    height = TOP + PLOT_H + BOTTOM
    x = lambda i: LEFT + COL * (i + 0.5)

    vals = [_yval(insts[s], a) for st in steps for s, (q, a) in st.held.items()]
    vals += [float(b["fill"].price) for st in steps for b in st.fills if insts[b["fill"].symbol].strike is None]
    vals += [float(st.spot) for st in steps if st.spot is not None]
    if not vals:
        return ""
    lo, hi = min(vals), max(vals)
    pad = (hi - lo) * 0.08 or max(abs(hi) * 0.02, 1.0)
    lo, hi = lo - pad, hi + pad
    y = lambda v: TOP + PLOT_H - (float(v) - lo) / (hi - lo) * PLOT_H

    out = [f'<svg class="chart" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" '
           f'aria-label="Legs held over the month, by strike">']
    # roll bands and labels (behind everything)
    for r in rolls:
        idx = [number[o] for o in r["order_ids"] if o in number]
        if not idx:
            continue
        a, b = min(idx), max(idx)
        out.append(f'<rect class="rollband" x="{x(a) - COL / 2 + 1:.1f}" y="{TOP}" width="{COL * (b - a + 1) - 2:.1f}" height="{PLOT_H}"/>')
        label = ", ".join(ROLL_SHORT[t] for t in r["types"]) or "roll"
        out.append(f'<text class="rolltxt" transform="translate({x(b) + 3:.1f},{TOP - 6}) rotate(-40)">{esc(label)}</text>')
    # grid and y axis
    for t in _ticks(lo, hi):
        out.append(f'<line class="grid" x1="{LEFT}" x2="{width - RIGHT + 10}" y1="{y(t):.1f}" y2="{y(t):.1f}"/>'
                   f'<text class="tick" x="{LEFT - 8}" y="{y(t) + 4:.1f}" text-anchor="end">{esc(num(Decimal(str(t))))}</text>')
    # x axis: step numbers, and the date under the first step of each day
    last_day = None
    for i, st in enumerate(steps):
        out.append(f'<text class="tick" x="{x(i):.1f}" y="{TOP + PLOT_H + 16}" text-anchor="middle">{i}</text>')
        day = ny(st.ts).date() if st.ts else "start"
        if day != last_day:
            txt = "start" if st.ts is None else f"{ny(st.ts):%b} {ny(st.ts).day}"
            out.append(f'<line class="daytick" x1="{x(i) - COL / 2:.1f}" x2="{x(i) - COL / 2:.1f}" y1="{TOP}" y2="{TOP + PLOT_H + 34}"/>'
                       f'<text class="day" x="{x(i) - COL / 2 + 4:.1f}" y="{TOP + PLOT_H + 34}">{esc(txt)}</text>')
            last_day = day
    if open_at_end:
        out.append(f'<text class="tick" x="{x(n):.1f}" y="{TOP + PLOT_H + 16}" text-anchor="middle">end</text>')
    # underlying price recorded with each order
    pts = [(x(i), y(st.spot)) for i, st in enumerate(steps) if st.spot is not None]
    if len(pts) > 1:
        out.append('<polyline class="spot" points="' + " ".join(f"{a:.1f},{b:.1f}" for a, b in pts) + '"/>')
    for i, st in enumerate(steps):
        if st.spot is not None:
            out.append(f'<circle class="spotdot" cx="{x(i):.1f}" cy="{y(st.spot):.1f}" r="2.5">'
                       f'<title>Step {i}: underlying price {esc(num(st.spot))}</title></circle>')
    # roll arrows: each closed leg to the opened leg it was paired with (same put/call, same side)
    for r in rolls:
        idx = sorted(number[o] for o in r["order_ids"] if o in number)
        if not idx:
            continue
        a, b = idx[0], idx[-1]
        before = steps[a - 1].held if a > 0 else {}
        after = steps[b].held
        closed = [s for s in before if s not in after or abs(after[s][0]) < abs(before[s][0])]
        opened = [s for s in after if s not in before or abs(after[s][0]) > abs(before[s][0])]
        for k in ("put", "call", "under"):
            for side in (-1, 1):
                c = sorted((s for s in closed if kind_of(insts[s]) == k and (before[s][0] > 0) == (side > 0)),
                           key=lambda s: (insts[s].strike or 0, insts[s].expiration or 0))
                o = sorted((s for s in opened if kind_of(insts[s]) == k and (after[s][0] > 0) == (side > 0)),
                           key=lambda s: (insts[s].strike or 0, insts[s].expiration or 0))
                for cs, os_ in zip(c, o):
                    y1, y2 = y(_yval(insts[cs], before[cs][1])), y(_yval(insts[os_], after[os_][1]))
                    out.append(f'<path class="arrow {k}" d="M{x(a):.1f},{y1:.1f} C{x(a) + 10:.1f},{y1:.1f} {x(b) - 10:.1f},{y2:.1f} {x(b):.1f},{y2:.1f}" marker-end="url(#ah-{k})"/>')
    # legs: one segment per stretch at a constant quantity
    symbols = sorted({s for st in steps for s in st.held})
    end_labels: list[tuple[float, str]] = []
    for s in symbols:
        inst = insts[s]
        k = kind_of(inst)
        i = 0
        while i < n:
            h = steps[i].held.get(s)
            if not h:
                i += 1
                continue
            j = i + 1
            while j < n and steps[j].held.get(s) and steps[j].held[s][0] == h[0]:
                j += 1
            end = j if j < n else (n if open_at_end else n - 1)
            yy = y(_yval(inst, h[1]))
            cls = f"leg {k} {'short' if h[0] < 0 else 'long'}"
            tip = (f"{leg_short(inst, h[0])} · average {num(h[1])} · held from step {i} "
                   + (f"to step {j}" if j < n else "to month end"))
            out.append(f'<g class="legg"><title>{esc(tip)}</title>'
                       f'<line class="{cls}" x1="{x(i):.1f}" x2="{x(end):.1f}" y1="{yy:.1f}" y2="{yy:.1f}"/>'
                       f'<line class="hit" x1="{x(i):.1f}" x2="{x(end):.1f}" y1="{yy:.1f}" y2="{yy:.1f}"/>')
            prev = steps[i - 1].held.get(s) if i > 0 else None
            if not prev:
                out.append(f'<circle class="dot {k}" cx="{x(i):.1f}" cy="{yy:.1f}" r="4"/>')
            if j < n and not steps[j].held.get(s):
                out.append(f'<path class="xmark {k}" d="M{x(j) - 4:.1f},{yy - 4:.1f}l8,8m0,-8l-8,8"/>')
            if j >= n and open_at_end:
                end_labels.append((yy, leg_short(inst, h[0])))
            elif j - i >= 3:
                out.append(f'<text class="legtxt" x="{x(i) + 6:.1f}" y="{yy - 6:.1f}">{esc(leg_short(inst, h[0]))}</text>')
            out.append("</g>")
            i = j
    # legs still open at month end: labelled in the right margin, nudged apart
    last = -1e9
    for yy, txt in sorted(end_labels):
        ly = max(yy + 3, last + 12)
        out.append(f'<text class="legtxt" x="{x(n) + 10:.1f}" y="{ly:.1f}">{esc(txt)}</text>')
        last = ly
    # hover columns, linked to the ledger rows
    for i, st in enumerate(steps):
        out.append(f'<rect class="col" data-step="{i}" x="{x(i) - COL / 2:.1f}" y="{TOP}" width="{COL}" height="{PLOT_H}"/>')
    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------- the page

def _fill_line(b: dict, insts: dict) -> str:
    f = b["fill"]
    inst = insts[f.symbol]
    verb = "Buy" if f.side == "buy" else "Sell"
    txt = f"{verb} {qty(b['dq'])} {leg_name(inst)} @ {num(f.price)}"
    if b["closing"]:
        txt += f" · closes against average {num(b['avg_cost'])} · {money(b['realized'], True)}"
    if f.source != "trade":
        txt = "(from the event) " + txt
    if b["fees_missing"]:
        txt += " · fees not given"
    return esc(txt)


def _held_line(held: dict, insts: dict) -> str:
    if not held:
        return '<span class="muted">flat</span>'
    return "<br>".join(esc(leg_short(insts[s], q)) for s, (q, a) in held.items())


def _story_section(story, steps: list[Step], insts: dict) -> str:
    effects = {o.order_id: str(o.effect) for o in story.orders}
    number = {st.ref: i for i, st in enumerate(steps) if st.kind == "order"}
    rolls = [{"order_ids": r.order_ids, "types": [str(t) for t in r.types], "net_cash": r.net_cash} for r in story.rolls]
    roll_at = {r["order_ids"][-1]: r for r in rolls}
    in_roll = {o for r in rolls for o in r["order_ids"]}
    rows = []
    for i, st in enumerate(steps):
        if st.kind == "start":
            what = "Carried in"
        elif st.kind == "event":
            what = EVENT_WORDS.get(st.event_type, st.event_type)
        else:
            what = EFFECT_WORDS.get(effects.get(st.ref, ""), effects.get(st.ref, ""))
            r = roll_at.get(st.ref)
            if r:
                what += (f'<div class="rollnote">{esc(", ".join(ROLL_WORDS[t] for t in r["types"]) or "roll")}'
                         f'<br>net {esc(money(r["net_cash"], True))} {"credit" if r["net_cash"] > 0 else "debit"}'
                         + (" over both orders" if len(r["order_ids"]) > 1 else "") + "</div>")
            elif st.ref in in_roll:
                what += '<div class="rollnote">first half of the roll</div>'
        legs = "<br>".join(_fill_line(b, insts) for b in st.fills) or '<span class="muted">-</span>'
        cash = "" if st.cash is None or st.kind == "start" else money(st.cash, True)
        delta = "" if st.kind == "start" else money(st.delta, True)
        cls = ' class="roll"' if st.ref in in_roll else ""
        rows.append(f'<tr data-step="{i}"{cls}><td class="n">{i}</td><td class="t">{esc(when(st.ts))}<div class="muted">{esc(st.ref if st.kind != "start" else "")}</div></td>'
                    f'<td>{what}</td><td class="legs">{legs}</td><td class="r">{esc(cash)}</td>'
                    f'<td class="r">{esc(money(st.fees) if st.fees else "")}</td>'
                    f'<td class="r {"pos" if st.delta > 0 else "neg" if st.delta < 0 else ""}">{esc(delta)}</td>'
                    f'<td class="r">{esc(money(st.realized, True))}</td><td class="held">{_held_line(st.held, insts)}</td></tr>')
    total = steps[-1].realized if steps else ZERO
    ok = Decimal(total).quantize(CENT) == Decimal(story.realized_pnl).quantize(CENT)
    check = (f'<span class="{"ok" if ok else "bad"}">{"✓" if ok else "✗"} The steps add to {esc(money(total, True))}; '
             f'the story records {esc(money(story.realized_pnl, True))}.</span>')
    status = {"open": "still open at month end", "closed": "closed", "expired": "expired", "assigned": "assigned"}[str(story.status)]
    head = (f'<summary><span class="und">{esc(story.underlying)}</span> <span class="sid">{esc(story.story_id)}</span>'
            f'<span class="meta">{esc(str(story.label_open).replace("_", " "))} → {esc(str(story.label_end).replace("_", " "))} · '
            f'{len(story.orders)} orders · {len(story.rolls)} rolls · {esc(status)}</span>'
            f'<span class="pl {"pos" if story.realized_pnl > 0 else "neg" if story.realized_pnl < 0 else ""}">{esc(money(story.realized_pnl, True))}</span></summary>')
    extra = (f'<p class="muted">Open gain or loss on what is still held: {esc(money(story.open_pnl_end, True))} at month end'
             + (f", {esc(money(story.open_pnl_start, True))} at month start" if story.carried_in else "") + ".</p>")
    return (f'<details class="story" id="{esc(story.story_id)}" data-rolls="{len(story.rolls)}">{head}<div class="body">'
            f'<div class="scroll">{chart(steps, insts, rolls, number)}</div>'
            f'<p class="checkline">{check}</p>{extra}'
            f'<div class="scroll"><table class="ledger"><thead><tr><th>#</th><th>When (New York)</th><th>What happened</th>'
            f'<th>Fills</th><th class="r">Premium</th><th class="r">Fees</th><th class="r">Realized</th>'
            f'<th class="r">Running</th><th>Held after</th></tr></thead><tbody>{"".join(rows)}</tbody></table></div></div></details>')


def _fact(sheet: FactsSheet | None, fid: str):
    if sheet is None:
        return None
    for f in sheet.facts:
        if f.id == fid:
            return f
    return None


def _summary(raw: RawMonth, linked: LinkResult, sheet: FactsSheet | None) -> str:
    stories = linked.stories
    realized = sum((s.realized_pnl for s in stories), ZERO)
    open_change = sum((s.open_pnl_end - s.open_pnl_start for s in stories), ZERO)
    flows = sum(((c.amount if str(c.kind) == "deposit" else -c.amount) for c in raw.account.cash_flows), ZERO)
    change = raw.account.end_value - raw.account.start_value - flows
    gap = change - (realized + open_change)
    rolls = sum(len(s.rolls) for s in stories)
    by_type: dict[str, int] = {}
    for s in stories:
        for r in s.rolls:
            for t in r.types:
                by_type[str(t)] = by_type.get(str(t), 0) + 1
    fees = sum((s.fees for s in stories), ZERO)
    no_fees = bool(raw.fills) and all(f.commission is None or f.fees is None for f in raw.fills)

    def row(label, value, note=""):
        return f"<tr><td>{label}</td><td class=\"r\">{value}</td><td class=\"muted\">{note}</td></tr>"

    def tick(ok):
        return '<span class="ok">✓ matches</span>' if ok else '<span class="bad">✗ does not match</span>'

    rows = []
    f = _fact(sheet, "net_realized_pnl")
    rows.append(row("Gains and losses locked in, all stories added up", esc(money(realized, True)),
                    (f"facts sheet says {esc(f.display)} " + tick(Decimal(str(f.value)).quantize(CENT) == realized.quantize(CENT))) if f else ""))
    f = _fact(sheet, "rolls_total")
    rows.append(row("Rolls, all stories added up", str(rolls),
                    (f"facts sheet says {esc(f.display)} " + tick(int(f.value) == rolls)) if f else ""))
    rows.append(row("Rolls by kind (a roll can count in several)",
                    esc(", ".join(f"{ROLL_WORDS[k]} {v}" for k, v in sorted(by_type.items())) or "none"), ""))
    rows.append(row("Fees in the data", "not given" if no_fees else esc(money(fees)), ""))
    rows.append(row("Account change (end - start - deposits + withdrawals)", esc(money(change, True)),
                    f"{esc(money(raw.account.start_value))} to {esc(money(raw.account.end_value))}"))
    rows.append(row("Locked in + change in open gains and losses", esc(money(realized + open_change, True)),
                    f"{esc(money(realized, True))} locked in, {esc(money(open_change, True))} open"))
    rows.append(row("Gap between the two", esc(money(gap, True)),
                    "not explained by the trades: check the start and end marks, cash interest and any fees missing from the data"
                    if gap.quantize(CENT) else tick(True)))
    issues = "".join(f"<li>{esc(i.issue.replace('_', ' '))}: {len(i.refs)}</li>" for i in linked.data_quality_issues)
    index = "".join(
        f'<tr><td><a href="#{esc(s.story_id)}" data-open="{esc(s.story_id)}">{esc(s.underlying)}</a></td>'
        f'<td>{esc(str(s.label_end).replace("_", " "))}</td><td class="r">{len(s.orders)}</td><td class="r">{len(s.rolls)}</td>'
        f'<td>{esc(str(s.status))}</td><td class="r {"pos" if s.realized_pnl > 0 else "neg" if s.realized_pnl < 0 else ""}">{esc(money(s.realized_pnl, True))}</td></tr>'
        for s in sorted(stories, key=lambda s: (-len(s.orders), s.story_id)))
    return (f'<section class="card"><h2>Do the numbers add up?</h2><table class="sum">{"".join(rows)}</table>'
            + (f'<p class="muted">Data notes:</p><ul class="issues">{issues}</ul>' if issues else "")
            + f'</section><section class="card"><h2>Stories, most orders first</h2><div class="scroll"><table class="idx"><thead><tr>'
              f'<th>Underlying</th><th>At the end</th><th class="r">Orders</th><th class="r">Rolls</th><th>Status</th><th class="r">Locked in</th>'
              f'</tr></thead><tbody>{index}</tbody></table></div></section>')


CSS = """
:root{--bg:#FFF6F3;--card:#FFFFFF;--ink:#1E0B08;--muted:#6E5F5B;--rule:#EADBD6;--band:#FDE4E0;
--put:#2a78d6;--call:#eb6834;--under:#1baf7a;--spot:#52514e;--ok:#0B7A55;--bad:#C0392B;--accent:#D42A1F;color-scheme:light;
--font:"Manrope",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,Consolas,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#110706;--card:#1F0F0C;--ink:#FFF3F0;--muted:#A59997;
--rule:#2E2322;--band:#320E0C;--put:#3987e5;--call:#d95926;--under:#199e70;--spot:#c3c2b7;--ok:#6EF3C0;--bad:#FF6B57;--accent:#FF3B30;color-scheme:dark}}
:root[data-theme="dark"]{--bg:#110706;--card:#1F0F0C;--ink:#FFF3F0;--muted:#A59997;--rule:#2E2322;--band:#320E0C;
--put:#3987e5;--call:#d95926;--under:#199e70;--spot:#c3c2b7;--ok:#6EF3C0;--bad:#FF6B57;--accent:#FF3B30;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 var(--font)}
main{max-width:1280px;margin:0 auto;padding:24px 16px 64px;display:grid;gap:18px}
h1{font-size:26px;margin:0}h2{font-size:17px;margin:0 0 10px}a{color:var(--accent)}
.card{background:var(--card);border:1px solid var(--rule);border-radius:12px;padding:16px}
.muted{color:var(--muted)}.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
.pos{color:var(--ok)}.neg{color:var(--bad)}
table{border-collapse:collapse;width:100%}td,th{padding:6px 8px;border-bottom:1px solid var(--rule);vertical-align:top;text-align:left}
th{font-size:12px;color:var(--muted);font-weight:600}.r{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
.scroll{overflow-x:auto;max-width:100%}
.legend{display:flex;flex-wrap:wrap;gap:6px 18px;align-items:center;font-size:13px}
.legend svg{vertical-align:middle}
.tools{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
button{font:inherit;cursor:pointer;padding:6px 12px;border-radius:6px;border:1px solid var(--rule);background:var(--card);color:var(--ink)}
details.story{background:var(--card);border:1px solid var(--rule);border-radius:12px}
details.story>summary{cursor:pointer;padding:12px 16px;display:flex;flex-wrap:wrap;gap:6px 12px;align-items:baseline}
.und{font-weight:700;font-size:16px}.sid{font-family:var(--mono);font-size:12px;color:var(--muted)}
.meta{color:var(--muted)}.pl{margin-left:auto;font-weight:700;font-variant-numeric:tabular-nums}
.body{padding:0 16px 16px;display:grid;gap:10px}
.chart text{font-family:var(--mono);font-size:10px;fill:var(--muted)}
.chart .legtxt{fill:var(--ink)}.chart .rolltxt{fill:var(--ink);font-weight:500}
.chart .grid{stroke:var(--rule);stroke-width:1}.chart .daytick{stroke:var(--rule);stroke-dasharray:2 3}
.chart .rollband{fill:var(--band)}
.chart .spot{fill:none;stroke:var(--spot);stroke-width:1.5;stroke-dasharray:1 3;stroke-linecap:round}
.chart .spotdot{fill:var(--spot)}
.chart .leg{stroke-width:3;stroke-linecap:round}.chart .leg.long{stroke-width:2;stroke-dasharray:5 4}
.chart .leg.put,.chart .dot.put,.chart .xmark.put{stroke:var(--put)}.chart .leg.call,.chart .dot.call,.chart .xmark.call{stroke:var(--call)}
.chart .leg.under,.chart .dot.under,.chart .xmark.under{stroke:var(--under)}
.chart .dot{stroke-width:2;fill:var(--card)}.chart .xmark{stroke-width:2;fill:none}
.chart .hit{stroke:transparent;stroke-width:12}
.chart .arrow{fill:none;stroke-width:1.5;opacity:.75}.chart .arrow.put{stroke:var(--put)}.chart .arrow.call{stroke:var(--call)}.chart .arrow.under{stroke:var(--under)}
.chart .col{fill:transparent}.chart .col.on{fill:var(--ink);opacity:.06}
.ledger td{font-size:13px}.ledger .n{font-family:var(--mono);color:var(--muted)}.ledger .t{white-space:nowrap}
.ledger .legs{font-family:var(--mono);font-size:12px;min-width:340px}.ledger .held{font-family:var(--mono);font-size:12px;white-space:nowrap}
.ledger tr.roll td{background:color-mix(in srgb,var(--band) 55%,transparent)}
.ledger tr.on td{outline:2px solid var(--accent);outline-offset:-2px}
.rollnote{font-size:12px;color:var(--muted)}.checkline{margin:0}
.sum td:first-child{width:46%}
"""

JS = """
const root=document.documentElement;
document.getElementById('theme').onclick=()=>{const d=root.dataset.theme==='dark'||(!root.dataset.theme&&matchMedia('(prefers-color-scheme: dark)').matches);root.dataset.theme=d?'light':'dark';};
document.getElementById('openall').onclick=()=>document.querySelectorAll('details.story').forEach(d=>d.open=true);
document.getElementById('closeall').onclick=()=>document.querySelectorAll('details.story').forEach(d=>d.open=false);
document.getElementById('rollsonly').onchange=(e)=>document.querySelectorAll('details.story').forEach(d=>d.hidden=e.target.checked&&d.dataset.rolls==='0');
document.querySelectorAll('[data-open]').forEach(a=>a.addEventListener('click',()=>{const d=document.getElementById(a.dataset.open);if(d)d.open=true;}));
document.querySelectorAll('details.story').forEach(d=>{
  const mark=(i,on)=>{d.querySelectorAll('[data-step="'+i+'"]').forEach(n=>n.classList.toggle('on',on));};
  d.querySelectorAll('.col').forEach(c=>{c.addEventListener('mouseenter',()=>mark(c.dataset.step,true));c.addEventListener('mouseleave',()=>mark(c.dataset.step,false));
    c.addEventListener('click',()=>{const r=d.querySelector('tr[data-step="'+c.dataset.step+'"]');if(r)r.scrollIntoView({block:'center',behavior:'smooth'});});});
  d.querySelectorAll('tr[data-step]').forEach(r=>{r.addEventListener('mouseenter',()=>mark(r.dataset.step,true));r.addEventListener('mouseleave',()=>mark(r.dataset.step,false));});
});
"""

LEGEND = ('<div class="legend">'
          '<span><svg width="30" height="10"><line x1="2" x2="28" y1="5" y2="5" stroke="var(--put)" stroke-width="3" stroke-linecap="round"/></svg>short put</span>'
          '<span><svg width="30" height="10"><line x1="2" x2="28" y1="5" y2="5" stroke="var(--call)" stroke-width="3" stroke-linecap="round"/></svg>short call</span>'
          '<span><svg width="30" height="10"><line x1="2" x2="28" y1="5" y2="5" stroke="var(--put)" stroke-width="2" stroke-dasharray="5 4"/></svg>long (dashed)</span>'
          '<span><svg width="30" height="10"><line x1="2" x2="28" y1="5" y2="5" stroke="var(--under)" stroke-width="3" stroke-linecap="round"/></svg>futures or shares, at average price</span>'
          '<span><svg width="30" height="10"><line x1="2" x2="28" y1="5" y2="5" stroke="var(--spot)" stroke-width="1.5" stroke-dasharray="1 3" stroke-linecap="round"/></svg>underlying price with each order</span>'
          '<span><svg width="16" height="10"><circle cx="8" cy="5" r="4" fill="none" stroke="var(--ink)" stroke-width="2"/></svg>opened</span>'
          '<span><svg width="16" height="10"><path d="M4,1l8,8m0,-8l-8,8" stroke="var(--ink)" stroke-width="2"/></svg>closed</span>'
          '<span><svg width="22" height="12"><rect width="22" height="12" fill="var(--band)"/></svg>roll (curves join each closed leg to its new leg)</span>'
          '</div>')

DEFS = ('<svg width="0" height="0" style="position:absolute"><defs>'
        + "".join(f'<marker id="ah-{k}" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto">'
                  f'<path d="M0,0L8,4L0,8z" fill="var(--{k})"/></marker>' for k in ("put", "call", "under"))
        + "</defs></svg>")


def replay_steps(raw: RawMonth) -> tuple[LinkResult, dict[str, list[Step]], dict]:
    """The month replayed by the recap's Linker: (its result, story_id -> steps, symbol -> instrument)."""
    rec = _Recorder(raw)
    replay = rec.run()
    by_sid: dict[str, list[Step]] = {}
    for st in rec.steps:
        by_sid.setdefault(st.sid, []).append(st)
    return replay, by_sid, rec.insts


def render_story_view(raw: RawMonth, linked: LinkResult, sheet: FactsSheet | None = None) -> str:
    replay, by_sid, insts = replay_steps(raw)
    stories = {s.story_id: s for s in replay.stories}
    sections = "".join(_story_section(stories[s.story_id], by_sid.get(s.story_id, []), insts)
                       for s in linked.stories if by_sid.get(s.story_id))
    a = raw.account
    title = f"Trade stories · {a.first_name} · {a.month}"
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\"><meta name=\"robots\" content=\"noindex, nofollow\">"
            f"<title>{esc(title)}</title><link rel=\"stylesheet\" href=\"https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=Manrope:wght@400;600;700&display=swap\">"
            f"<style>{CSS}</style></head><body>{DEFS}<main>"
            f"<header class=\"tools\"><div style=\"margin-right:auto\"><h1>{esc(a.first_name)}'s trade stories, {esc(a.month)}</h1>"
            "<p class=\"muted\">The month replayed order by order, exactly as the recap's facts were built. "
            "For checking by eye; team only.</p></div>"
            "<button id=\"openall\">Open all</button><button id=\"closeall\">Close all</button>"
            "<label><input type=\"checkbox\" id=\"rollsonly\"> Only stories with rolls</label><button id=\"theme\">Light / dark</button></header>"
            f"{_summary(raw, linked, sheet)}<section class=\"card\"><h2>How to read a story</h2>{LEGEND}"
            "<p class=\"muted\" style=\"margin-top:8px\">One column per order or event, numbered to match the ledger below the chart. "
            "Hover a column or a ledger row to see them together; click a column to jump to its row. "
            "Realized = what a closing fill locked in against the leg's average cost, after fees.</p></section>"
            f"{sections}</main><script>{JS}</script></body></html>")


def main(argv: list[str]) -> None:
    from pipeline.link import forget_open_dates, link
    from pipeline.metrics import build_facts
    from pipeline.workbook import read_workbook
    src, dst = Path(argv[0]), Path(argv[1])
    imported = read_workbook(src.read_bytes())
    linked = forget_open_dates(link(imported.raw), imported.unknown_open)
    sheet = build_facts(imported.raw, linked, previous=None, first_month=True)
    dst.write_text(render_story_view(imported.raw, linked, sheet), encoding="utf-8", newline="\n")
    print(f"wrote {dst} ({len(linked.stories)} stories)")


if __name__ == "__main__":
    main(sys.argv[1:])
