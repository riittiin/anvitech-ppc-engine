"""Production analysis report (2026-10-03): the owner's "Format Production
analysis" sheet, columns B..S + U..X, computed from Daily Entry punches.

Formulas (Sheet1, with the owner's one change: efficiency deducts the ACTUAL
setting time L, not the standard K):
    U Planned qty          = H / G
    V Total actual qty     = I + J
    W Overall productivity = V / U
    X Operator efficiency  = V / ((H - L - M - N - O - P - Q - R) / G)
"""
from datetime import date
import io

import pytest

from engine import production_analysis as pa
from engine.models import Actual


# --- the formulas ---------------------------------------------------------- #
def test_the_four_columns_follow_the_sheet():
    # G=5, H=330, I=40 OK, J=2 rejected, L=100 actual setting, 30 min no power.
    planned, total, prod, eff = pa.metrics(5, 330, 40, 2, 100, 30)
    assert planned == pytest.approx(66)                 # 330 / 5
    assert total == 42                                  # 40 + 2
    assert prod == pytest.approx(42 / 66)               # V / U
    assert eff == pytest.approx(42 / ((330 - 100 - 30) / 5))


def test_efficiency_deducts_the_actual_setting_time_not_the_standard():
    """Owner's example: half a shift producing, half a shift setting a job whose
    standard is 90 but which took 330. The overrun must not lower efficiency."""
    producing = Actual(so_no="S", item_code="I", entry_date=date(2026, 9, 1),
                       qty_produced=60, cycle_time_min=5, shift_minutes=330)
    setting = Actual(so_no="S", item_code="J", entry_date=date(2026, 9, 1),
                     qty_produced=0, cycle_time_min=5, shift_minutes=330,
                     std_setup_min=90, actual_setup_min=330)
    (_, _, prod_a, eff_a), _ = pa.actual_metrics(producing)
    (_, _, prod_b, eff_b), _ = pa.actual_metrics(setting)
    assert eff_a == pytest.approx(300 / 330)
    assert prod_b == 0
    assert eff_b is None          # no production time at all: "-", never 0%


def test_nothing_to_divide_by_is_blank_never_zero():
    assert pa.metrics(None, 600, 10, 0, 0, 0)[0] is None      # no cycle time
    assert pa.metrics(None, 600, 10, 0, 0, 0)[3] is None
    assert pa.metrics(5, 0, 10, 0, 0, 0)[2] is None           # no minutes entered
    assert pa.metrics(5, 100, 10, 0, 60, 40)[3] is None       # all time lost


def test_ok_qty_is_produced_minus_rejected():
    """The order book has always counted good = produced - rejected, so a punch's
    OK qty (I) is produced - rejected and Total actual (V) is produced."""
    a = Actual(so_no="S", item_code="I", entry_date=date(2026, 9, 1),
               qty_produced=50, qty_rejected=5, cycle_time_min=2, shift_minutes=600)
    row = pa.report_row(a)
    assert row["Actual OK Qty"] == 45
    assert row["Rejected qty"] == 5
    assert row["Total Actual Qty"] == 50


def test_new_fields_round_trip_and_legacy_rows_default():
    a = Actual(so_no="S", item_code="I", entry_date=date(2026, 9, 1), machine="CNC1",
               cycle_time_min=4.5, shift_minutes=330, std_setup_min=90)
    b = Actual.from_json(a.to_json())
    assert (b.machine, b.cycle_time_min, b.shift_minutes, b.std_setup_min) == ("CNC1", 4.5, 330, 90)
    legacy = {k: v for k, v in a.to_json().items()
              if k not in ("machine", "cycle_time_min", "shift_minutes", "std_setup_min")}
    c = Actual.from_json(legacy)
    assert (c.machine, c.cycle_time_min, c.shift_minutes, c.std_setup_min) == ("", None, 0.0, 0.0)


def test_report_columns_match_the_sheet_order():
    assert pa.REPORT_COLUMNS[0] == "Date" and pa.REPORT_COLUMNS[1] == "Machine"
    assert pa.REPORT_COLUMNS[-4:] == ("Planned qty", "Total Actual Qty",
                                      "Overall Productivity (Planned vs actual Qty)",
                                      "Operator efficiency")
    assert "Rate" not in pa.REPORT_COLUMNS


# --- the API ---------------------------------------------------------------- #
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from engine import book_store  # noqa: E402
from engine.models import Order  # noqa: E402
from tests.sample_workbook import build_sample_bytes, ITEM_A  # noqa: E402

DAY = "2026-09-15"


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 100, date(2026, 12, 1))])
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return m, c


def _post(c, **kw):
    body = dict(so_no="SO1", item_code=ITEM_A, entry_date=DAY, shift="1st shift",
                process="BANDSAW", operator="Operator One", qty_produced=10)
    body.update(kw)
    return c.post("/actuals", json=body)


def test_save_snapshots_the_cycle_time_from_the_process_master():
    _, c = _api()
    r = _post(c, machine="BS1", shift_minutes=330, cycle_time_min=999)  # browser value ignored
    assert r.status_code == 200, r.text
    a = book_store.load_actuals()[-1]
    assert a.cycle_time_min == 3.0          # BANDSAW's cycle time in the sample master
    assert a.machine == "BS1" and a.shift_minutes == 330


def test_standard_setting_time_defaults_by_machine_kind():
    _, c = _api()
    assert _post(c, machine="BS1", qty_produced=5).status_code == 200
    assert book_store.load_actuals()[-1].std_setup_min == 0.0        # manual station
    assert _post(c, process="CNC OS", machine="CNC1", qty_produced=5).status_code == 200
    assert book_store.load_actuals()[-1].std_setup_min == 90.0       # CNC
    assert _post(c, process="CNC OS", machine="CNC1", qty_produced=0,
                 std_setup_min=120).status_code == 200
    assert book_store.load_actuals()[-1].std_setup_min == 120.0      # typed value kept


def test_an_unknown_machine_is_refused():
    _, c = _api()
    r = _post(c, machine="CNC77")
    assert r.status_code == 400 and "machine list" in r.text


def test_items_feeds_the_form_its_defaults():
    _, c = _api()
    body = c.get("/items").json()
    assert body["shift_minutes"] == {"1st shift": 660, "2nd shift": 600}
    info = body["items"][ITEM_A]["process_info"]
    assert info["BANDSAW"] == {"cycle_time": 3.0, "machines": ["BS1"]}
    assert info["CNC OS"]["machines"] == ["CNC1", "CNC2"]
    setups = {m["id"]: m["std_setup_min"] for m in body["machines"]}
    assert setups["CNC1"] == 90 and setups["BS1"] == 0


def test_monthly_report_json_and_excel():
    _, c = _api()
    _post(c, machine="BS1", shift_minutes=330, qty_produced=12, qty_rejected=2,
          actual_setup_min=0, no_power_min=30)
    r = c.get("/production-analysis", params={"year": 2026, "month": 9})
    assert r.status_code == 200
    (row,) = r.json()["rows"]
    assert row["Machine"] == "BS1" and row["Cycle time in Min"] == 3.0
    assert row["Planned qty"] == 110                         # 330 / 3
    assert row["Total Actual Qty"] == 12                     # 10 OK + 2 rejected
    assert row["Overall Productivity (Planned vs actual Qty)"] == pytest.approx(10.9, abs=0.05)
    assert row["Operator efficiency"] == pytest.approx(12 / (300 / 3) * 100, abs=0.05)
    assert c.get("/production-analysis", params={"year": 2026, "month": 8}).json()["rows"] == []

    x = c.get("/production-analysis.xlsx", params={"year": 2026, "month": 9})
    assert x.status_code == 200
    from openpyxl import load_workbook
    ws = load_workbook(io.BytesIO(x.content))["Sheet1"]
    assert ws["B6"].value == "Date" and ws["C6"].value == "Machine"
    last = ws.cell(row=6, column=1 + len(pa.REPORT_COLUMNS))
    assert last.value == "Operator efficiency"
    assert ws.cell(row=7, column=1 + len(pa.REPORT_COLUMNS)).value == pytest.approx(0.12, abs=0.001)


def test_report_fields_never_reach_the_plan():
    """The new fields are report-only: two punches that differ ONLY in them give
    the planner identical remaining quantities."""
    from engine import orderbook
    base = dict(so_no="SO1", item_code=ITEM_A, entry_date=date(2026, 9, 1),
                process="BANDSAW", operator="Operator One", qty_produced=10)
    plain = Actual(**base)
    rich = Actual(**base, machine="BS1", cycle_time_min=3, shift_minutes=330, std_setup_min=0)
    orders = {("SO1", ITEM_A): Order("SO1", ITEM_A, ITEM_A, 100, date(2026, 12, 1))}
    lines = lambda acts: [(l.so_no, l.item_code, l.qty, l.process_qty)
                          for l in orderbook.active_so_lines(orders, acts)]
    assert lines([plain]) == lines([rich])
