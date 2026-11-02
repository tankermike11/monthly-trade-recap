"""Put sellers: the wheel (p03) and the losing month (p09).

Sell puts; take profits early; if assigned, either sell calls on the shares (wheel)
or sell the shares after a few trading days (p09).
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from mock.behaviors.base import Engine, pct
from mock.ledger import LegReq
from mock.market import calendar as cal
from mock.market import listings as L


class PutSeller(Engine):
    def setup(self) -> None:
        for c in self.p.get("already_open_on_oct_1", []):
            self.seed_option(c["stock"], c["end_date"], "P", float(c["how_far_from_price"]),
                             c["opened"], int(c["contracts"]))
            self.ledger.set_label(c["stock"], self._put_label(c["stock"]))
        self.cooldown: dict[str, date] = {}
        self.last_sale_px: dict[str, float] = {
            c["stock"]: self.world.price_at(c["stock"], cal.at_ny(c["opened"], 11))
            for c in self.p.get("already_open_on_oct_1", [])}
        self.adds: dict[str, int] = {}
        self.assigned_on: dict[str, date] = {}

    # ------------------------------------------------------------------ labels

    def _put_label(self, sym: str) -> str:
        need = sum(-p.qty * p.inst.strike * p.inst.multiplier
                   for p in self.ledger.story_legs(sym) if p.inst.put_call == "P" and p.qty < 0)
        return "cash_secured_put" if self.ledger.cash >= need else "short_put"

    def _structure_label(self, sym: str) -> str | None:
        legs = self.ledger.story_legs(sym)
        shares = sum((p.qty for p in legs if not p.inst.is_option), Decimal(0))
        calls = [p for p in legs if p.inst.put_call == "C" and p.qty < 0]
        puts = [p for p in legs if p.inst.put_call == "P" and p.qty < 0]
        if shares > 0 and calls and not puts:
            return "covered_call"
        if shares > 0 and not calls and not puts:
            return "stock_long"
        if puts and not calls and shares == 0:
            return self._put_label(sym)
        return "custom" if legs else None

    # ------------------------------------------------------------------ daily

    def on_day(self, day: date) -> None:
        if not cal.is_trading_day(day):
            return
        rng = self.rng(day, "wheel")
        start, end = self.window(day, self.p["trading_hours"])
        for ts, sym in sorted((self.random_minute(rng, start, end), s) for s in self.p["stocks"]):
            legs = self.ledger.story_legs(sym)
            if legs and self.ledger.story_label(sym) != self._structure_label(sym):
                self.ledger.set_label(sym, self._structure_label(sym))
            shares = sum((p.qty for p in legs if not p.inst.is_option), Decimal(0))
            puts = [p for p in legs if p.inst.put_call == "P"]
            calls = [p for p in legs if p.inst.put_call == "C"]
            if shares > 0:
                if sym not in self.assigned_on:
                    self.assigned_on[sym] = day
                self._manage_shares(sym, shares, calls, day, ts)
            elif puts:
                self._manage_puts(sym, puts, day, ts)
            else:
                self.assigned_on.pop(sym, None)
                if day >= self.cooldown.get(sym, day):
                    self._sell_puts(sym, day, ts, rng)

    def _sell_puts(self, sym: str, day: date, ts, rng) -> None:
        rule = self.p["selling_puts"]
        dte = rule["days_until_end"]
        exp = L.pick_expiration(self.world, sym, day, dte["aim"], dte["at_least"], dte["at_most"],
                                monthly_only=rule.get("end_date", "monthly") == "monthly")
        if exp is None:
            return
        k = self.strike_by_delta(sym, exp, "P", float(rule["how_far_from_price"]), ts)
        inst = self.opt(sym, exp, "P", k)
        n = Decimal(int(rule["contracts"]))
        need = n * k * 100
        label = "cash_secured_put" if self.ledger.cash >= need else "short_put"
        self.ledger.trade(ts, [LegReq(inst, "sell", n)], label)
        self.last_sale_px[sym] = self.spot(sym, ts)
        self.adds[sym] = 0

    def _manage_puts(self, sym, puts, day, ts) -> None:
        rule = self.p["selling_puts"]
        collected = float(self.ledger.story_cash_collected(sym))
        cost_to_close = -self.legs_value(sym, ts)
        dte_left = min((p.inst.expiration - day).days for p in puts)
        if collected > 0 and cost_to_close <= (1 - pct(self.p["take_profit_at"])) * collected and dte_left > 0:
            self.close_all(sym, ts)
            self.cooldown[sym] = self._after_trading_days(day, int(self.p.get("wait_trading_days_after_close", 2)))
            return
        drop = rule.get("add_more_when_stock_falls")
        if drop and self.adds.get(sym, 0) < int(rule.get("most_adds", 0)) and dte_left > 0:
            last = self.last_sale_px.get(sym) or self.spot(sym, ts)
            now = self.spot(sym, ts)
            if now <= last * (1 - pct(drop)):
                p = max(puts, key=lambda x: -x.qty)
                self.ledger.trade(ts, [LegReq(p.inst, "sell", Decimal(int(rule["contracts"])))],
                                  self._put_label(sym))
                self.last_sale_px[sym] = now
                self.adds[sym] = self.adds.get(sym, 0) + 1

    def _manage_shares(self, sym, shares, calls, day, ts) -> None:
        after = self.p.get("if_assigned", {})
        if "sell_shares_after_trading_days" in after:
            due = self._after_trading_days(self.assigned_on[sym], int(after["sell_shares_after_trading_days"]))
            if day >= due:
                self.close_all(sym, ts)
                self.cooldown[sym] = self._after_trading_days(day, int(self.p.get("wait_trading_days_after_close", 2)))
            return
        rule = self.p["selling_calls"]
        if calls:
            collected_calls = -float(sum((c.cost for c in calls), Decimal(0)))
            cost_to_close = -sum(float(c.qty) * self.fair(c.inst, ts) * 100 for c in calls)
            if cost_to_close <= (1 - pct(self.p["take_profit_at"])) * collected_calls:
                legs = [LegReq(c.inst, "buy", -c.qty) for c in calls]
                self.ledger.trade(ts, legs, "stock_long")
            return
        dte = rule["days_until_end"]
        exp = L.pick_expiration(self.world, sym, day, dte["aim"], dte["at_least"], dte["at_most"], monthly_only=True)
        if exp is None:
            return
        stock_pos = next(p for p in self.ledger.story_legs(sym) if not p.inst.is_option)
        floor = (stock_pos.cost / stock_pos.qty) if rule.get("never_below_cost") else None
        step = L.strike_step(self.world, sym)
        floor = (floor / step).to_integral_value("ROUND_CEILING") * step if floor else None
        try:
            k = self.strike_by_delta(sym, exp, "C", float(rule["how_far_from_price"]), ts, at_least=floor)
        except RuntimeError:
            return
        n = Decimal(int(shares // 100))
        if n > 0:
            self.ledger.trade(ts, [LegReq(self.opt(sym, exp, "C", k), "sell", n)], "covered_call")

    @staticmethod
    def _after_trading_days(day: date, n: int) -> date:
        d = day
        for _ in range(n):
            d = cal.next_trading_day(d)
        return d
