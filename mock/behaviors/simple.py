"""Steady investor (p01) and the two day traders (p02 stocks, p07 micro futures)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from mock.behaviors.base import Engine, pct, rng_range
from mock.ledger import LegReq
from mock.market import calendar as cal
from mock.market import listings as L


class SteadyInvestor(Engine):
    """Holds a few funds, adds a few shares on fixed trading days each month."""

    def setup(self) -> None:
        for h in self.p.get("already_owned_on_oct_1", []):
            self.seed_stock(h["symbol"], h["shares"], h["bought_on"])

    def on_day(self, day: date) -> None:
        if not cal.is_trading_day(day):
            return
        n = cal.month_trading_days(day.strftime("%Y-%m")).index(day) + 1
        rng = self.rng(day, "buys")
        for b in self.p.get("monthly_buys", []):
            if b["on_trading_day"] == n:
                ts = self.random_minute(rng, *self.window(day, self.p["trading_hours"]))
                self.ledger.trade(ts, [LegReq(L.stock(b["symbol"]), "buy", Decimal(b["shares"]))],
                                  "stock_long", order_type="market")


class DayTrader(Engine):
    """Same-day round trips with a profit target, a stop and a time limit.

    Works for stocks (p02) and futures (p07). One position at a time; the next trip
    starts only after the previous one is closed.
    """

    def _instruments(self):
        if "contracts" in self.p:
            return [L.future(self.world, root) for root in self.p["contracts"]]
        return [L.stock(s) for s in self.p["symbols"]]

    def _size(self, inst, price: float, rng) -> Decimal:
        if inst.asset_class == "future":
            return Decimal(rng_range(rng, self.p["size"]))
        dollars = float(self.p["dollars_per_trade"]) * float(rng.uniform(0.6, 1.4))
        return Decimal(max(10, int(round(dollars / price / 10)) * 10))

    def _planned_starts(self, day: date, rng) -> list:
        starts = []
        planted = {pl["date"]: pl for pl in self.p.get("planted", [])}
        pl = planted.get(day)
        if pl and "overnight_trade_at" in pl:
            starts.append(("overnight", self.at_local(day, pl["overnight_trade_at"])))
        if not cal.is_trading_day(day):
            return starts
        n = rng_range(rng, self.p["round_trips_per_day"])
        outside_key = "pre_market" if "pre_market_hours" in self.p else "overnight"
        share = pct(self.p.get("pre_market_share", self.p.get("overnight_share", 0)))
        forced = pl.get("pre_market_trades", 0) if pl else 0
        for i in range(n):
            if i < forced or rng.random() < share:
                hours = self.p.get("pre_market_hours") or self.p.get("overnight_hours")
                starts.append((outside_key, self.random_minute(rng, *self.window(day, hours))))
            else:
                starts.append(("regular", self.random_minute(rng, *self.window(day, self.p["regular_hours"]))))
        return sorted(starts, key=lambda x: x[1])

    def _flat_by(self, day: date, start, inst):
        if inst.asset_class == "future":
            # a futures session ends at 17:00 New York (13:15 on half days)
            sess_day, _ = self.world.session_day(inst.price_symbol, start)
            p = self.world.day_path(inst.price_symbol, sess_day)
            end = cal.at_ny_minutes(sess_day, p.start_minute + len(p.prices) - 6)
            return min(end, start + timedelta(hours=float(self.p["max_hold_hours"])))
        cutoff = self.at_local(day, self.p["must_be_flat_by"])
        if cal.is_half_day(day):
            cutoff = min(cutoff, self.regular_cutoff(day, 5))
        return min(cutoff, start + timedelta(minutes=int(self.p["max_hold_minutes"])))

    def on_day(self, day: date) -> None:
        rng = self.rng(day, "trips")
        insts = self._instruments()
        free_at = None
        for kind, start in self._planned_starts(day, rng):
            if free_at is not None and start < free_at:
                start = free_at + timedelta(minutes=int(rng.integers(3, 20)))
            inst = insts[int(rng.integers(len(insts)))]
            if cal.ny_date(start) != day or not self.tradable(inst.price_symbol, start):
                continue
            end = self._flat_by(day, start, inst)
            if end <= start + timedelta(minutes=2):
                continue
            entry_px = self.world.price_at(inst.price_symbol, start)
            short = rng.random() < pct(self.p.get("short_share", 0))
            qty = self._size(inst, entry_px, rng)
            tp, sl = pct(self.p["take_profit"]), pct(self.p["stop_loss"])
            exit_ts = end
            t = start + timedelta(minutes=1)
            while t < end:
                move = self.world.price_at(inst.price_symbol, t) / entry_px - 1
                if short:
                    move = -move
                if move >= tp or move <= -sl:
                    exit_ts = t
                    break
                t += timedelta(minutes=1)
            if inst.asset_class == "future":
                label = "future_short" if short else "future_long"
            else:
                label = "stock_short" if short else "stock_long"
            self.ledger.trade(start, [LegReq(inst, "sell" if short else "buy", qty)], label, order_type="market")
            self.ledger.trade(exit_ts, [LegReq(inst, "buy" if short else "sell", qty)],
                              order_type="limit" if exit_ts < end else "market")
            free_at = exit_ts
