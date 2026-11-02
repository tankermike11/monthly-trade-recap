"""Multi-asset (p08) and the messy account (p10, a scripted timeline)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from mock.behaviors.base import Engine, pct
from mock.ledger import LegReq
from mock.market import calendar as cal
from mock.market import listings as L


class MultiAsset(Engine):
    """Stocks on a schedule, a few long calls, short puts on /MES, and crypto."""

    def on_day(self, day: date) -> None:
        rng = self.rng(day, "multi")
        actions = []   # (ts, order, callable)
        for f in self.p.get("money_moves", []):
            if f["date"] == day:
                kind = "deposit" if "deposit" in f else "withdrawal"
                self.ledger.cash_flow(day, kind, Decimal(str(f[kind])))
        for c in self.p.get("crypto", []):
            if day.day in c.get("buy_on_days", []):
                ts = self.at_local(day, c["at"])
                actions.append((ts, 0, lambda ts=ts, c=c: self._crypto(ts, c, "buy")))
            if day.day == c.get("sell_on_day"):
                ts = self.at_local(day, c["at"])
                actions.append((ts, 0, lambda ts=ts, c=c: self._crypto(ts, c, "sell")))
        if cal.is_trading_day(day):
            n = cal.month_trading_days(day.strftime("%Y-%m")).index(day) + 1
            start, end = self.window(day, self.p["trading_hours"])
            for b in self.p.get("stock_trades", []):
                for side in ("buy", "sell"):
                    if b.get(f"{side}_on_trading_day") == n:
                        ts = self.random_minute(rng, start, end)
                        actions.append((ts, 1, lambda ts=ts, b=b, side=side: self._stock(ts, b, side)))
            cb = self.p.get("call_buying")
            if cb:
                ts = self.random_minute(rng, start, end)
                actions.append((ts, 2, lambda ts=ts, cb=cb, n=n: self._calls(ts, day, cb, n)))
            fp = self.p.get("futures_put_selling")
            if fp:
                ts = self.random_minute(rng, start, end)
                actions.append((ts, 3, lambda ts=ts, fp=fp, n=n: self._fut_puts(ts, day, fp, n)))
        for ts, _, fn in sorted(actions, key=lambda a: (a[0], a[1])):
            fn()

    def _crypto(self, ts, c, side) -> None:
        inst = L.crypto(c["coin"])
        if side == "buy":
            self.ledger.trade(ts, [LegReq(inst, "buy", Decimal(str(c["buy"])))], "crypto_long", order_type="market")
        else:
            held = self.ledger.holding(inst.symbol)
            if held > 0:
                self.ledger.trade(ts, [LegReq(inst, "sell", held)], order_type="market")

    def _stock(self, ts, b, side) -> None:
        inst = L.stock(b["symbol"])
        if side == "buy":
            self.ledger.trade(ts, [LegReq(inst, "buy", Decimal(b["shares"]))], "stock_long", order_type="market")
        elif self.ledger.holding(inst.symbol) > 0:
            self.ledger.trade(ts, [LegReq(inst, "sell", self.ledger.holding(inst.symbol))], order_type="market")

    def _calls(self, ts, day, cb, n) -> None:
        sym = cb["stock"]
        if self.ledger.story_legs(sym):
            legs = self.ledger.story_legs(sym)
            share = self._open_share(sym, ts)
            days_left = min((p.inst.expiration - day).days for p in legs)
            if share >= pct(cb["take_profit_at"]) or share <= -pct(cb["cut_loss_at"]) \
                    or days_left <= int(cb["close_when_days_left"]):
                self.close_all(sym, ts)
            return
        if n not in cb["buy_on_trading_days"]:
            return
        d = cb["days_until_end"]
        exp = L.pick_expiration(self.world, sym, day, d["aim"], d["at_least"], d["at_most"], monthly_only=True)
        k = self.strike_by_delta(sym, exp, "C", float(cb["how_far_from_price"]), ts)
        self.ledger.trade(ts, [LegReq(self.opt(sym, exp, "C", k), "buy", Decimal(int(cb["contracts"])))], "long_call")

    def _fut_puts(self, ts, day, fp, n) -> None:
        root = fp["contract"]
        if self.ledger.story_legs(root):
            legs = self.ledger.story_legs(root)
            share = self._open_share(root, ts)
            days_left = min((p.inst.expiration - day).days for p in legs)
            if share >= pct(fp["take_profit_at"]) or days_left <= int(fp["close_when_days_left"]):
                self.close_all(root, ts)
            return
        if n not in fp["sell_on_trading_days"]:
            return
        d = fp["days_until_end"]
        exp = L.pick_expiration(self.world, root, day, d["aim"], d["at_least"], d["at_most"], monthly_only=True)
        if exp is None:
            return
        k = self.strike_by_delta(root, exp, "P", float(fp["how_far_from_price"]), ts)
        self.ledger.trade(ts, [LegReq(self.opt(root, exp, "P", k), "sell", Decimal(int(fp["contracts"])))], "short_put")

    def _open_share(self, sym, ts) -> float:
        opened = float(self.ledger.story_cash_collected(sym))
        return (opened + self.legs_value(sym, ts)) / abs(opened) if opened else 0.0


class Scripted(Engine):
    """p10: a dated timeline of specific things that happen to the account."""

    def setup(self) -> None:
        for h in self.p.get("already_owned_on_oct_1", []):
            if "shares" in h:
                self.seed_stock(h["symbol"], h["shares"], h["bought_on"])
            else:
                o = h["sold_option"]
                inst = self.opt(h["symbol"], o["end_date"], o["type"], Decimal(str(o["strike"])))
                ts = cal.at_ny(h["bought_on"], 11)
                from mock.market import quotes as Q
                q = Q.quote(self.world, inst, ts)
                self.ledger.seed_position(inst, Decimal(-int(o["contracts"])), q.theo, h["bought_on"], "custom")
            self.ledger.set_label(h["symbol"], h["label"])

    def on_day(self, day: date) -> None:
        for item in self.p.get("timeline", []):
            if item["date"] != day:
                continue
            if "withdraw" in item:
                self.ledger.cash_flow(day, "withdrawal", Decimal(str(item["withdraw"])))
                continue
            ts = cal.at_ny(day, *(int(x) for x in item["at"].split(":")))
            self._do(item, ts, day)

    def _do(self, item: dict, ts, day) -> None:
        if "buy_calls" in item or "sell_calls" in item:
            side = "buy" if "buy_calls" in item else "sell"
            o = item[f"{side}_calls"]
            inst = self.opt(o["stock"], o["end_date"], "C", Decimal(str(o["strike"])))
            n = Decimal(int(o["contracts"]))
            pieces = None
            if "fills_in_pieces" in item:
                gap = int(item["minutes_apart"])
                pieces = [(ts + timedelta(minutes=gap * i), Decimal(q)) for i, q in enumerate(item["fills_in_pieces"])]
            if item.get("fees_missing"):
                self.ledger.fees_missing_next = True
            self.ledger.trade(ts, [LegReq(inst, side, n)], "long_call", pieces=pieces)
        elif "buy_shares" in item or "sell_shares" in item:
            side = "buy" if "buy_shares" in item else "sell"
            o = item[f"{side}_shares"]
            n = Decimal(int(o["shares"]))
            pieces = None
            if "only_filled" in item:
                parts, gap = item["only_filled"], int(item.get("minutes_apart", 10))
                pieces = [(ts + timedelta(minutes=gap * i), Decimal(q)) for i, q in enumerate(parts)]
            if item.get("fees_missing"):
                self.ledger.fees_missing_next = True
            self.ledger.trade(ts, [LegReq(L.stock(o["stock"]), side, n)], item.get("label", "stock_long"),
                              pieces=pieces)
        elif "early_assignment" in item:
            o = item["early_assignment"]
            inst = self.opt(o["stock"], o["end_date"], o["type"], Decimal(str(o["strike"])))
            self.ledger.assign_early(ts, inst.symbol)
        elif "roll_in_two_orders" in item:
            o = item["roll_in_two_orders"]
            old = self.opt(o["stock"], o["close"]["end_date"], o["close"]["type"], Decimal(str(o["close"]["strike"])))
            new = self.opt(o["stock"], o["open"]["end_date"], o["open"]["type"], Decimal(str(o["open"]["strike"])))
            n = -self.ledger.holding(old.symbol)
            self.ledger.trade(ts, [LegReq(old, "buy", n)], item.get("label"))
            self.ledger.trade(ts + timedelta(minutes=int(o["minutes_apart"])), [LegReq(new, "sell", n)], item.get("label"))
        else:
            raise ValueError(f"unknown timeline item {item}")
