"""Defined-risk traders: spreads and diagonals (p05) and same-day SPX spreads (p06)."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from mock.behaviors.base import Engine, pct, rng_range
from mock.ledger import LegReq
from mock.market import calendar as cal
from mock.market import listings as L


class SpreadBuilder(Engine):
    """Builds the order legs for each structure."""

    def build(self, kind: str, sym: str, day: date, ts, rng) -> tuple[list[LegReq], str] | None:
        c = self.p.get("credit_trades", {})
        dbt = self.p.get("debit_trades", {})
        step = L.strike_step(self.world, sym)
        n = Decimal(rng_range(rng, self.p["contracts"]))
        if kind in ("put_credit_spread", "call_credit_spread", "iron_condor"):
            exp = self._exp(sym, day, c["days_until_end"])
            if exp is None:
                return None
            width = step * int(c["width_strikes"]) if "width_strikes" in c else Decimal(
                int(rng.choice(c["width_points"])))
            legs = []
            if kind in ("put_credit_spread", "iron_condor"):
                ks = self.strike_by_delta(sym, exp, "P", float(c["short_leg"]), ts)
                legs += [LegReq(self.opt(sym, exp, "P", ks), "sell", n),
                         LegReq(self.opt(sym, exp, "P", ks - width), "buy", n)]
            if kind in ("call_credit_spread", "iron_condor"):
                ks = self.strike_by_delta(sym, exp, "C", float(c["short_leg"]), ts)
                legs += [LegReq(self.opt(sym, exp, "C", ks), "sell", n),
                         LegReq(self.opt(sym, exp, "C", ks + width), "buy", n)]
            return legs, ("iron_condor" if kind == "iron_condor" else "credit_vertical")
        if kind == "call_debit_spread":
            exp = self._exp(sym, day, dbt["days_until_end"])
            if exp is None:
                return None
            kl = self.strike_by_delta(sym, exp, "C", float(dbt["long_leg"]), ts)
            ks = kl + step * int(dbt["width_strikes"])
            return [LegReq(self.opt(sym, exp, "C", kl), "buy", n),
                    LegReq(self.opt(sym, exp, "C", ks), "sell", n)], "debit_vertical"
        if kind == "call_diagonal":
            dg = self.p["diagonal"]
            e_short = self._exp(sym, day, dg["short_days"], monthly_only=False)
            e_long = self._exp(sym, day, dg["long_days"])
            if e_short is None or e_long is None or e_long <= e_short:
                return None
            kl = self.strike_by_delta(sym, e_long, "C", float(dg["long_leg"]), ts)
            ks = self.strike_by_delta(sym, e_short, "C", float(dg["short_leg"]), ts, at_least=kl + step)
            return [LegReq(self.opt(sym, e_long, "C", kl), "buy", n),
                    LegReq(self.opt(sym, e_short, "C", ks), "sell", n)], "diagonal"
        raise ValueError(kind)

    def _exp(self, sym, day, spec, monthly_only=None):
        if monthly_only is None:
            monthly_only = self.p.get("end_date", "any") == "monthly"
        return L.pick_expiration(self.world, sym, day, spec["aim"], spec["at_least"], spec["at_most"],
                                 monthly_only=monthly_only)

    def pnl_share(self, sym: str, ts) -> float:
        """Open profit as a share of the upfront cash (credit trades) or cost (debit trades)."""
        opened = float(self.ledger.story_cash_collected(sym))
        value = self.legs_value(sym, ts)          # what the legs are worth now, signed
        if opened == 0:
            return 0.0
        return (opened + value) / abs(opened)


class SpreadTrader(SpreadBuilder):
    """p05: defined-risk spreads, quick profit taking, one planted trade across months."""

    def setup(self) -> None:
        self.special = {}
        for pl in self.p.get("planted", []):
            self.special[pl["symbol"]] = pl

    def _blocked(self, sym: str, day: date) -> bool:
        pl = self.special.get(sym)
        return bool(pl) and pl["open_on"] - timedelta(days=10) <= day <= pl["close_on"]

    def on_day(self, day: date) -> None:
        if not cal.is_trading_day(day):
            return
        rng = self.rng(day, "spreads")
        start, end = self.window(day, self.p["trading_hours"])
        held = [s for s in self.p["symbols"] if self.ledger.story_legs(s)]
        for ts, sym in sorted((self.random_minute(rng, start, end), s) for s in held):
            self._manage(sym, day, ts)
        for sym, pl in sorted(self.special.items()):
            if day == pl["open_on"] and not self.ledger.story_legs(sym):
                ts = max(self.random_minute(rng, start, end), self.ledger.last_ts or start)
                built = self.build(pl["open"], sym, day, ts, rng)
                self.ledger.trade(ts, built[0], built[1])
        if rng.random() < float(self.p["new_trades_per_week"]) / 5:
            free = [s for s in self.p["symbols"] if not self.ledger.story_legs(s) and not self._blocked(s, day)]
            if free:
                sym = free[int(rng.integers(len(free)))]
                mix = self.p["trade_mix"]
                kinds = sorted(mix)
                weights = [float(mix[k]) for k in kinds]
                kind = kinds[int(rng.choice(len(kinds), p=[w / sum(weights) for w in weights]))]
                ts = max(self.random_minute(rng, start, end), self.ledger.last_ts or start)
                if cal.ny_date(ts) == day and ts < self.regular_cutoff(day):
                    built = self.build(kind, sym, day, ts, rng)
                    if built:
                        self.ledger.trade(ts, built[0], built[1])

    def _manage(self, sym: str, day: date, ts) -> None:
        pl = self.special.get(sym)
        if pl and pl["open_on"] <= day <= pl["close_on"]:
            if day == pl["close_on"]:
                self.close_all(sym, ts)
            return
        legs = self.ledger.story_legs(sym)
        days_left = min((p.inst.expiration - day).days for p in legs)
        share = self.pnl_share(sym, ts)
        credit = self.ledger.story_cash_collected(sym) > 0
        target = pct(self.p["take_profit_at"])
        stop = pct(self.p["stop_loss_at"]) if credit else pct(self.p.get("debit_stop_loss_at", "50%"))
        if share >= target or share <= -stop or days_left <= int(self.p["close_when_days_left"]):
            self.close_all(sym, ts)


class SameDayIndex(SpreadBuilder):
    """p06: SPX credit spreads ending today or the next trading day, checked every few
    minutes, closed at a profit target or a stop, otherwise held to cash settlement.
    Some trades are left alone entirely until they settle."""

    left_alone_until = None

    def on_day(self, day: date) -> None:
        if not cal.is_trading_day(day):
            return
        sym = self.p["index"]
        rng = self.rng(day, "spx")
        step = timedelta(minutes=int(self.p["check_every_minutes"]))
        first_start, first_end = self.window(day, self.p["trading_hours"])
        last_new = min(self.at_local(day, self.p["last_new_trade"]), self.regular_cutoff(day, 45))
        stop_checks = self.regular_cutoff(day, 10)
        if "stops_watching_at" in self.p:
            stop_checks = min(stop_checks, self.at_local(day, self.p["stops_watching_at"]))
        target_n = rng_range(rng, self.p["trades_per_day"])
        opened_today = 0
        t = self.random_minute(rng, first_start, first_end)
        if self.ledger.story_legs(sym):            # a trade carried from yesterday
            t = cal.at_ny(day, 9, 45)
        while t < stop_checks:
            legs = self.ledger.story_legs(sym)
            if legs and self.left_alone_until == min(p.inst.expiration for p in legs):
                break                                   # left alone until it ends
            if self.ledger.story_legs(sym):
                share = self.pnl_share(sym, t)
                winner = share >= pct(self.p["take_profit_at"])
                if winner or share <= -pct(self.p["stop_loss_at"]):
                    patient = winner and rng.random() < float(self.p.get("winners_closed_at_mid_price", 0))
                    self.close_all(sym, t, at_mid=patient)
                    t += timedelta(minutes=int(rng.integers(5, 30)))
                    continue
                t += step
                continue
            if opened_today >= target_n or t > last_new:
                break
            kind = "put_credit_spread" if rng.random() < float(self.p["sides"]["put_credit_spread"]) else "call_credit_spread"
            next_day = rng.random() < float(self.p["next_day_share"])
            spec = {"aim": 1, "at_least": 1, "at_most": 4} if next_day else {"aim": 0, "at_least": 0, "at_most": 0}
            self.p.setdefault("credit_trades", {})
            self.p["credit_trades"] = {"days_until_end": spec, "short_leg": self.p["short_leg"],
                                       "width_points": self.p["width_points"]}
            built = self.build(kind, sym, day, t, rng)
            if built is None:
                break
            exp = built[0][0].inst.expiration
            if (exp - day).days > 0 and cal.next_trading_day(day) != exp:
                break
            self.ledger.trade(t, built[0], built[1])
            opened_today += 1
            let_run = rng.random() < float(self.p.get("lets_it_run_share", 0))
            self.left_alone_until = exp if let_run else None
            t += step
