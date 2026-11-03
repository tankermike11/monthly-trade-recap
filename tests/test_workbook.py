"""Demo-site workbooks (pipeline/workbook.py).

Gate for the demo site's stage 1:
- every persona workbook reads back to exactly the raw month it was written from, and
  to the same facts sheet the pipeline builds from the mock data;
- the same input gives the same workbook bytes;
- a minimal workbook (Account and Trades only) still gives a valid facts sheet, with
  each default it relied on listed;
- bad workbooks fail with every problem named by sheet, row and column.
"""

from __future__ import annotations

import io
import json
from decimal import Decimal

import pytest
from openpyxl import Workbook, load_workbook

from pipeline.ingest import load_month
from pipeline.link import link
from pipeline.metrics import build_facts
from pipeline.nytime import ny, ny_to_utc
from pipeline.run import facts_for
from pipeline.workbook import SHEETS, WorkbookError, index_levels, read_workbook, write_workbook
from tests.conftest import PERSONA_IDS


@pytest.fixture(scope="module")
def raws(fresh):
    return {(m, p): load_month(fresh / "raw" / m / p) for m in ("2026-10", "2026-11") for p in PERSONA_IDS}


def test_ny_wall_time_round_trips(raws):
    for raw in raws.values():
        for f in raw.fills:
            assert ny_to_utc(ny(f.ts_utc)) == f.ts_utc


def test_built_in_index_levels_match_the_mock_data(raws):
    for (month, _), raw in raws.items():
        assert index_levels(month) == raw.market.indexes


@pytest.mark.parametrize("month", ["2026-10", "2026-11"])
@pytest.mark.parametrize("p", PERSONA_IDS)
def test_persona_workbook_reads_back_exactly(raws, month, p):
    raw = raws[(month, p)]
    res = read_workbook(write_workbook(raw))
    # no defaults applied; the only possible note is p10's deliberately missing fees
    assert all("no commission or fees" in a for a in res.assumptions), res.assumptions
    assert res.raw == raw


@pytest.mark.parametrize("p", PERSONA_IDS)
def test_persona_workbook_gives_the_same_facts(fresh, raws, p):
    raw = read_workbook(write_workbook(raws[("2026-10", p)])).raw
    sheet = build_facts(raw, link(raw), previous=None, first_month=True)
    assert sheet == facts_for(fresh / "raw", "2026-10", p)[1]


def test_same_input_same_bytes(raws):
    import time
    raw = raws[("2026-10", "p04")]
    first = write_workbook(raw)
    time.sleep(1.1)                       # openpyxl stamps the save time to the second
    assert write_workbook(raw) == first
    assert write_workbook(None) == write_workbook(None)


def test_template_has_every_sheet_and_sample_market_levels():
    wb = load_workbook(io.BytesIO(write_workbook(None)))
    assert wb.sheetnames == ["Read me", "Account"] + list(SHEETS)
    market = [r for r in wb["Market"].iter_rows(min_row=2, values_only=True) if r[0]]
    assert [r[0] for r in market] == ["SPY", "QQQ", "/ES"]


def _minimal(raw) -> bytes:
    """Account (required fields only) and Trades (required and option columns only)."""
    wb = Workbook()
    a = wb.active
    a.title = "Account"
    for label, value in (("First name", raw.account.first_name), ("Month (YYYY-MM)", raw.account.month),
                         ("Account value at start", float(raw.account.start_value)),
                         ("Account value at end", float(raw.account.end_value))):
        a.append([label, value])
    t = wb.create_sheet("Trades")
    cols = ["Time (New York)", "Underlying", "Side", "Open/close", "Quantity", "Price", "Expiration", "Strike",
            "Put/call", "Underlying price", "Commission", "Fees", "Order ID"]
    t.append(cols)
    ctx = {c.exec_id: c for c in raw.market.fills}
    for f in raw.fills:
        if f.source != "trade":
            continue
        t.append([ny(f.ts_utc), f.underlying, str(f.side), str(f.open_close), float(f.qty), float(f.price),
                  f.expiration, float(f.strike) if f.strike is not None else None,
                  str(f.put_call) if f.put_call else None, float(ctx[f.exec_id].underlying_price),
                  float(f.commission or 0), float(f.fees or 0), f.order_id])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.mark.parametrize("p", ["p04", "p05", "p09"])
def test_minimal_workbook_gives_a_facts_sheet_and_lists_its_defaults(raws, p):
    raw = raws[("2026-10", p)]
    res = read_workbook(_minimal(raw))
    sheet = build_facts(res.raw, link(res.raw), previous=None, first_month=True)
    ids = {f.id for f in sheet.facts}
    assert {"month_return", "spy_move", "stories_touched"} <= ids
    text = " ".join(res.assumptions)
    assert "no Orders sheet" in text and "no Market sheet" in text and "only cash at the start" in text
    assert "replaying the trades" in text
    # the trades themselves are unchanged
    assert [(f.underlying, f.side, f.qty, f.price) for f in res.raw.fills] == \
           [(f.underlying, f.side, f.qty, f.price) for f in raw.fills if f.source == "trade"]


def test_options_past_expiration_expire_worthless_without_an_events_sheet():
    wb = Workbook()
    a = wb.active
    a.title = "Account"
    for row in (("First name", "Sam"), ("Month (YYYY-MM)", "2026-10"),
                ("Account value at start", 10000), ("Account value at end", 10100)):
        a.append(row)
    t = wb.create_sheet("Trades")
    t.append(["Time (New York)", "Underlying", "Side", "Open/close", "Quantity", "Price", "Expiration",
              "Strike", "Put/call", "Underlying price", "Commission", "Fees"])
    t.append(["2026-10-01 10:00", "AAPL", "sell", "open", 1, 1.25, "2026-10-16", 200, "put", 210, 1, 0.1])
    buf = io.BytesIO()
    wb.save(buf)
    res = read_workbook(buf.getvalue())
    assert [e.type for e in res.raw.events] == ["expiration_worthless"]
    assert res.raw.positions_end.lines == []
    assert any("expiring worthless" in n for n in res.assumptions)
    assert any("its own order" in n for n in res.assumptions)


def _bad(change) -> list[str]:
    wb = load_workbook(io.BytesIO(write_workbook(None)))
    acct = wb["Account"]
    for row in acct.iter_rows(min_row=2):
        label = str(row[0].value).replace(" *", "")
        row[1].value = {"First name": "Sam", "Account value at start": 10000, "Account value at end": 9000}.get(
            label, row[1].value)
    change(wb)
    buf = io.BytesIO()
    wb.save(buf)
    with pytest.raises(WorkbookError) as e:
        read_workbook(buf.getvalue())
    return e.value.problems


def test_bad_rows_are_named_by_sheet_row_and_column():
    def rows(wb):
        t = wb["Trades"]
        t.append(["2026-10-01 10:00", "AAPL", "hold", "open", 1, 1.25, "2026-10-16", None, "put", 210])
        t.append(["2026-11-03 10:00", "MSFT", "buy", "open", "ten", 400])
    problems = _bad(rows)
    text = "\n".join(problems)
    assert "Trades row 2, Side: expected one of buy, sell" in text
    assert "Trades row 2, Strike: required for an option" in text
    assert "Trades row 3, Quantity: expected a number" in text


def test_missing_trades_sheet_and_non_excel_files_are_refused():
    assert "The Trades sheet is missing" in _bad(lambda wb: wb.remove(wb["Trades"]))
    with pytest.raises(WorkbookError) as e:
        read_workbook(b"time,symbol\n2026-10-01,AAPL\n")
    assert "not an Excel workbook" in e.value.problems[0]
    assert "Trades: no rows" in " ".join(_bad(lambda wb: None))


def test_numbers_typed_as_text_are_read():
    from pipeline.workbook import _convert
    assert _convert("$1,240.50", "dec") == Decimal("1240.50")
    assert _convert(0.1, "dec") == Decimal("0.1")
    assert _convert("Call", "put_call") == "C"
    assert _convert("Equity option", "asset_class") == "equity_option"
