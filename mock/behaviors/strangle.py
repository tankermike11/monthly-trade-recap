"""Premium seller (p04): short strangles about 45 days out, managed by rolling."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from mock.behaviors.base import Engine, pct, rng_range
from mock.ledger import LegReq
from mock.market import calendar as cal
from mock.market import listings as L


class PremiumSeller(Engine):
    def setup(self) -> None:
        o = self.p["opening_a_trade"]
        for c in self.p.get("already_open_on_oct_1", []):
            for pc in ("P", "C"):
                self.seed_option(c["stock"], c["end_date"], pc, float(o["how_far_from_price"]),
                                 c["opened"], int(c["contracts"]))
            self.ledger.set_label(c["stock"], "short_strangle")

    def _exp(self, sym: str, day: date, spec: dict):
        return L.pick_expiration(self.world, sym, day, spec["aim"], spec["at_least"], spec["at_most"],
                                 monthly_only=self.p["opening_a_trade"].get("end_date") == "monthly")

    def on_day(self, day: date) -> None:
        if not cal.is_trading_day(day):
            return
        rng = self.rng(day, "strangle")
        start, end = self.window(day, self.p["opening_a_trade"]["trading_hours"])
        # manage open trades first, each at its own minute, in time order
        held = [s for s in self.p["stocks"] if self.ledger.story_legs(s)]
        plan = sorted((self.random_minute(rng, start, end), s) for s in held)
        for ts, sym in plan:
            self._manage(sym, day, ts, rng)
        # then maybe open new ones
        o = self.p["opening_a_trade"]
        if rng.random() < float(o["new_trades_per_week"]) / 5:
            free = [s for s in self.p["stocks"] if not self.ledger.story_legs(s)]
            if free:
                sym = free[int(rng.integers(len(free)))]
                ts = max(self.random_minute(rng, start, end), self.ledger.last_ts or start)
                if cal.ny_date(ts) == day and ts < self.regular_cutoff(day):
                    self._open(sym, day, ts, rng)

    def _open(self, sym, day, ts, rng) -> None:
        o = self.p["opening_a_trade"]
        exp = self._exp(sym, day, o["days_until_end"])
        if exp is None:
            return
        d = float(o["how_far_from_price"])
        n = Decimal(rng_range(rng, o["contracts"]))
        kp = self.strike_by_delta(sym, exp, "P", d, ts)
        kc = self.strike_by_delta(sym, exp, "C", d, ts)
        self.ledger.trade(ts, [LegReq(self.opt(sym, exp, "P", kp), "sell", n),
                               LegReq(self.opt(sym, exp, "C", kc), "sell", n)], "short_strangle")

    def _manage(self, sym, day, ts, rng) -> None:
        legs = self.ledger.story_legs(sym)
        put = next(p for p in legs if p.inst.put_call == "P")
        call = next(p for p in legs if p.inst.put_call == "C")
        collected = float(self.ledger.story_cash_collected(sym))
        cost_to_close = -self.legs_value(sym, ts)
        keep = pct(self.p["closing"]["take_profit_at"])
        if collected > 0 and cost_to_close <= (1 - keep) * collected:
            self.close_all(sym, ts)
            return
        days_left = (min(put.inst.expiration, call.inst.expiration) - day).days
        move = self.p["moving_to_next_month"]
        if days_left <= int(move["when_days_left"]):
            self._roll_out(sym, day, ts, put, call)
            return
        adj = self.p["adjusting"]
        dp, dc = abs(self.delta(put.inst, ts)), abs(self.delta(call.inst, ts))
        trigger = float(adj["slide_other_half_when"])
        if max(dp, dc) < trigger:
            return
        tested, untested = (put, call) if dp > dc else (call, put)
        if rng.random() < float(adj.get("sometimes_widen_instead", 0)):
            self._replace(sym, ts, tested, float(self.p["opening_a_trade"]["how_far_from_price"]), "short_strangle")
            return
        target = float(adj["slide_it_to"])
        self._replace(sym, ts, untested, target, "short_strangle", credit_only=adj.get("for_cash_only", False))

    def _replace(self, sym, ts, pos, target_delta, label, credit_only=False) -> None:
        inst = pos.inst
        other = [p for p in self.ledger.story_legs(sym) if p.inst.symbol != inst.symbol]
        bound = {}
        if other:  # never cross the other half's strike
            ok = other[0].inst.strike
            bound = {"at_least": ok} if inst.put_call == "C" else {"at_most": ok}
        k = self.strike_by_delta(sym, inst.expiration, inst.put_call, target_delta, ts, **bound)
        if k == inst.strike:
            return
        new = self.opt(sym, inst.expiration, inst.put_call, k)
        if credit_only and self.fair(new, ts) <= self.fair(inst, ts):
            return
        n = -pos.qty
        self.ledger.trade(ts, [LegReq(inst, "buy", n), LegReq(new, "sell", n)], label)

    def _roll_out(self, sym, day, ts, put, call) -> None:
        spec = self.p["moving_to_next_month"]["new_days_until_end"]
        exp = self._exp(sym, day, spec)
        if exp is None:
            return
        d = float(self.p["opening_a_trade"]["how_far_from_price"])
        kp = self.strike_by_delta(sym, exp, "P", d, ts)
        kc = self.strike_by_delta(sym, exp, "C", d, ts)
        legs = [LegReq(put.inst, "buy", -put.qty), LegReq(call.inst, "buy", -call.qty),
                LegReq(self.opt(sym, exp, "P", kp), "sell", -put.qty),
                LegReq(self.opt(sym, exp, "C", kc), "sell", -call.qty)]
        self.ledger.trade(ts, legs, "short_strangle")
