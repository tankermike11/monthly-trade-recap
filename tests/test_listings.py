"""Only strikes and expirations that would actually be listed are traded."""

from __future__ import annotations

from mock.market import calendar as cal
from mock.market import listings as L

OPTION_CLASSES = {"equity_option", "index_option", "future_option"}


def _split_symbols(rm) -> set[str]:
    out = set()
    for e in rm.events:
        if e.type == "split" and e.adjusted_symbols:
            out |= set(e.adjusted_symbols.values())
    return out


def test_expirations_are_listed(raw, world):
    for (m, p), rm in raw.items():
        for f in rm.fills:
            if f.asset_class not in OPTION_CLASSES or f.source != "trade":
                continue
            trade_day = cal.ny_date(f.ts_utc)
            listed = L.listed_expirations(world, f.underlying, trade_day)
            assert f.expiration in listed, f"{p} {f.exec_id}: {f.expiration} is not listed on {trade_day}"


def test_strikes_on_grid(raw, world):
    for (m, p), rm in raw.items():
        adjusted = _split_symbols(rm) | {l.symbol for l in rm.positions_start.lines if l.underlying == "SHOP"}
        for f in rm.fills:
            if f.asset_class not in OPTION_CLASSES or f.symbol in adjusted:
                continue
            step = L.strike_step(world, f.underlying)
            assert f.strike % step == 0, f"{p} {f.exec_id}: strike {f.strike} not a multiple of {step}"


def test_weeklies_only_for_liquid_names(raw, world):
    for (m, p), rm in raw.items():
        for f in rm.fills:
            if f.asset_class != "equity_option" or f.source != "trade":
                continue
            if not L.is_monthly(f.expiration):
                assert world.symbols[f.underlying].get("liquid"), f"{f.exec_id}: weekly on illiquid {f.underlying}"


def test_occ_symbols_match_terms(raw):
    for (m, p), rm in raw.items():
        for f in rm.fills:
            if f.asset_class in ("equity_option", "index_option"):
                root = "SPXW" if f.asset_class == "index_option" else f.underlying
                assert f.symbol == L.occ_symbol(root, f.expiration, str(f.put_call), f.strike)
