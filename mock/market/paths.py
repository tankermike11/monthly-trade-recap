"""The shared simulated market: minute price paths, daily bars and daily implied vol.

Price paths are geometric Brownian motion at one-minute steps across each symbol's
whole session, with planted overnight gaps, dividends and splits from events.yaml.
Each day's path comes from its own seeded stream, so it is reproducible on its own.

Sessions (New York wall time)
- stock / etf : 04:00 to 20:00 (to 17:00 on half days); regular session 09:30-16:00
- index (SPX) : the regular session of the ETF it follows, times `ratio`
- future      : 18:00 the evening before to 17:00 (to 13:15 on half days)
- crypto      : every calendar day, 00:00 to 24:00
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from pathlib import Path

import numpy as np
import yaml

from mock.market import calendar as cal
from mock.rng import stream

PATH_START = date(2026, 8, 3)
PATH_END = date(2026, 11, 30)
MARKET_DIR = Path(__file__).parent
DEFAULTS_PATH = MARKET_DIR.parent / "personas" / "_defaults.yaml"
EVENTS_PATH = MARKET_DIR / "events.yaml"

IV_KAPPA = 0.08
IV_ETA = 0.07
OFF_HOURS_WEIGHT = 0.08   # variance of a pre-market or after-hours minute vs a regular-hours minute


@dataclass(frozen=True)
class DayPath:
    day: date
    start_minute: int        # New York minutes after midnight of `day` (may be negative)
    prices: np.ndarray       # one value per minute, inclusive of both ends
    close_index: int         # index of the official daily close


def load_yaml(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _d(x: float, places: int = 6) -> Decimal:
    return Decimal(repr(round(float(x), places))).quantize(Decimal(1).scaleb(-places))


def floor_to(x: float, step: Decimal) -> Decimal:
    return (Decimal(repr(x)) / step).to_integral_value(ROUND_FLOOR) * step


def ceil_to(x: float, step: Decimal) -> Decimal:
    return (Decimal(repr(x)) / step).to_integral_value(ROUND_CEILING) * step


class World:
    def __init__(self, seed: int):
        self.seed = seed
        self.defaults = load_yaml(DEFAULTS_PATH)
        self.events = load_yaml(EVENTS_PATH)
        self.rate = float(self.defaults["interest_rate"])
        self.symbols: dict[str, dict] = self.defaults["symbols"]
        self.futures: dict[str, dict] = self.defaults["futures"]
        self.gaps = {(g["symbol"], g["date"]): float(g["move"]) for g in self.events["gaps"]}
        self.splits = {(s["symbol"], s["date"]): int(s["ratio"]) for s in self.events["splits"]}
        self.dividends = [(d["symbol"], d["ex_date"], Decimal(str(d["amount"])))
                          for d in self.events["dividends"]]
        self._paths: dict[str, dict[date, DayPath]] = {}
        self._iv: dict[str, dict[date, float]] = {}
        self._ivr: dict[str, dict[date, float]] = {}
        for sym, spec in self.symbols.items():
            kind = spec["kind"]
            if kind in ("stock", "etf"):
                self._paths[sym] = self._equity_paths(sym, spec)
            elif kind == "future":
                self._paths[sym] = self._future_paths(sym, spec)
            elif kind == "crypto":
                self._paths[sym] = self._crypto_paths(sym, spec)
        for sym, spec in self.symbols.items():
            if spec["kind"] == "index":
                self._paths[sym] = self._index_paths(sym, spec)
        for sym, spec in self.symbols.items():
            if "iv" in spec:
                self._build_iv(sym, float(spec["iv"]))
        for sym, spec in self.symbols.items():
            if spec["kind"] == "index":
                self._iv[sym] = self._iv[spec["follows"]]
                self._ivr[sym] = self._ivr[spec["follows"]]

    # ------------------------------------------------------------------ paths

    @staticmethod
    def _weights(start_minute: int, n: int, reg_open: int, reg_close: int, off: float) -> np.ndarray:
        minutes = start_minute + np.arange(n)
        w = np.where((minutes >= reg_open) & (minutes < reg_close), 1.0, off)
        return w / w.sum()

    def _gbm(self, rng, s0: float, day_sigma: float, weights: np.ndarray) -> np.ndarray:
        z = rng.standard_normal(len(weights))
        inc = day_sigma * np.sqrt(weights) * z - 0.5 * day_sigma ** 2 * weights
        return s0 * np.exp(np.concatenate([[0.0], np.cumsum(inc)]))

    def _equity_paths(self, sym: str, spec: dict) -> dict[date, DayPath]:
        out: dict[date, DayPath] = {}
        last = float(spec["start"])
        sigma_d = float(spec["vol"]) / math.sqrt(252)
        for d in cal.trading_days(PATH_START, PATH_END):
            rng = stream(self.seed, "path", sym, d.isoformat())
            end_min = 17 * 60 if cal.is_half_day(d) else 20 * 60
            start_min = 4 * 60
            n = end_min - start_min
            gap = self.gaps.get((sym, d))
            factor = (1 + gap) if gap is not None else math.exp(0.25 * sigma_d * rng.standard_normal())
            for s, ex, amt in self.dividends:
                if s == sym and ex == d:
                    factor *= 1 - float(amt) / last
            ratio = self.splits.get((sym, d))
            if ratio:
                factor /= ratio
            w = self._weights(start_min, n, 9 * 60 + 30, cal.regular_close_minute(d), OFF_HOURS_WEIGHT)
            prices = self._gbm(rng, last * factor, sigma_d, w)
            out[d] = DayPath(d, start_min, prices, cal.regular_close_minute(d) - start_min)
            last = float(prices[-1])
        return out

    def _index_paths(self, sym: str, spec: dict) -> dict[date, DayPath]:
        base = self._paths[spec["follows"]]
        ratio = float(spec["ratio"])
        out = {}
        for d, p in base.items():
            a = 9 * 60 + 30 - p.start_minute
            b = cal.regular_close_minute(d) - p.start_minute
            prices = p.prices[a:b + 1] * ratio
            out[d] = DayPath(d, 9 * 60 + 30, prices, len(prices) - 1)
        return out

    def _future_paths(self, sym: str, spec: dict) -> dict[date, DayPath]:
        out = {}
        last = float(spec["start"])
        sigma_d = float(spec["vol"]) / math.sqrt(252)
        for d in cal.trading_days(PATH_START, PATH_END):
            rng = stream(self.seed, "path", sym, d.isoformat())
            start_min = -6 * 60
            end_min = 13 * 60 + 15 if cal.is_half_day(d) else 17 * 60
            n = end_min - start_min
            factor = math.exp(0.1 * sigma_d * rng.standard_normal())
            w = self._weights(start_min, n, 9 * 60 + 30, 16 * 60, 0.15)
            prices = self._gbm(rng, last * factor, sigma_d, w)
            close_min = cal.regular_close_minute(d) if cal.is_half_day(d) else 16 * 60
            out[d] = DayPath(d, start_min, prices, close_min - start_min)
            last = float(prices[-1])
        return out

    def _crypto_paths(self, sym: str, spec: dict) -> dict[date, DayPath]:
        out = {}
        last = float(spec["start"])
        sigma_d = float(spec["vol"]) / math.sqrt(365)
        for d in cal.calendar_days(PATH_START, PATH_END):
            rng = stream(self.seed, "path", sym, d.isoformat())
            w = np.full(1440, 1 / 1440)
            prices = self._gbm(rng, last, sigma_d, w)
            out[d] = DayPath(d, 0, prices, 1440)
            last = float(prices[-1])
        return out

    # ------------------------------------------------------------------ lookup

    def kind(self, sym: str) -> str:
        return self.symbols[sym]["kind"]

    def session_day(self, sym: str, ts_utc: datetime) -> tuple[date, int]:
        """(session date, index into that day's path) for an instant."""
        local = cal.ny(ts_utc)
        minute = local.hour * 60 + local.minute
        kind = self.kind(sym)
        if kind == "future" and minute >= 18 * 60:
            d = cal.next_trading_day(local.date())
            minute -= 24 * 60 * (d - local.date()).days
        else:
            d = local.date()
        p = self._paths[sym].get(d)
        if p is None:
            raise ValueError(f"{sym} has no session on {d} ({local})")
        idx = minute - p.start_minute
        if not 0 <= idx < len(p.prices):
            raise ValueError(f"{sym} is not trading at {local} New York time")
        return d, idx

    def price_at(self, sym: str, ts_utc: datetime) -> float:
        d, idx = self.session_day(sym, ts_utc)
        return float(self._paths[sym][d].prices[idx])

    def is_open(self, sym: str, ts_utc: datetime) -> bool:
        try:
            self.session_day(sym, ts_utc)
            return True
        except ValueError:
            return False

    def day_path(self, sym: str, d: date) -> DayPath:
        return self._paths[sym][d]

    def close(self, sym: str, d: date) -> float:
        p = self._paths[sym][d]
        return float(p.prices[p.close_index])

    def close_ts(self, sym: str, d: date) -> datetime:
        p = self._paths[sym][d]
        return cal.at_ny_minutes(d, p.start_minute + p.close_index)

    def bar(self, sym: str, d: date, tick: Decimal) -> dict:
        p = self._paths[sym][d]
        return {
            "symbol": sym, "date": d,
            "open": _d(p.prices[0], 4), "close": _d(p.prices[p.close_index], 4),
            "low": floor_to(float(p.prices.min()), tick),
            "high": ceil_to(float(p.prices.max()), tick),
            "session_start_utc": cal.at_ny_minutes(d, p.start_minute),
            "session_end_utc": cal.at_ny_minutes(d, p.start_minute + len(p.prices) - 1),
        }

    def days(self, sym: str) -> list[date]:
        return sorted(self._paths[sym])

    # ------------------------------------------------------------------ vol

    def _build_iv(self, sym: str, base: float) -> None:
        rng = stream(self.seed, "iv", sym)
        days = cal.trading_days()
        x = math.log(base)
        ivs: dict[date, float] = {}
        for d in days:
            x += IV_KAPPA * (math.log(base) - x) + IV_ETA * rng.standard_normal()
            gap = self.gaps.get((sym, d))
            if gap is not None:
                x += 3 * abs(gap)
            ivs[d] = math.exp(x)
        ranks: dict[date, float] = {}
        seq = [ivs[d] for d in days]
        for i, d in enumerate(days):
            window = seq[max(0, i - 251): i + 1]
            lo, hi = min(window), max(window)
            ranks[d] = 0.0 if hi == lo else 100.0 * (seq[i] - lo) / (hi - lo)
        self._iv[sym], self._ivr[sym] = ivs, ranks

    def iv(self, sym: str, d: date) -> float:
        return self._iv[sym][self._vol_day(d)]

    def iv_rank(self, sym: str, d: date) -> float:
        return self._ivr[sym][self._vol_day(d)]

    @staticmethod
    def _vol_day(d: date) -> date:
        return d if cal.is_trading_day(d) else cal.prev_trading_day(d)

    def has_iv(self, sym: str) -> bool:
        return sym in self._iv

    # ------------------------------------------------------------------ corporate actions

    def dividends_between(self, sym: str, after: date, through: date) -> list[tuple[date, Decimal]]:
        return [(ex, amt) for s, ex, amt in self.dividends if s == sym and after < ex <= through]

    def split_on(self, sym: str, d: date) -> int | None:
        return self.splits.get((sym, d))
