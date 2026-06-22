"""Rule 8 — capture daily entry (full form): round-trip + per-item aggregation."""
from datetime import date

from engine.models import Actual
from engine.rules import rule8_capture_actuals


def _entry(**kw):
    base = dict(so_no="24-25SO214", item_code="61240807-01", entry_date=date(2025, 8, 1))
    base.update(kw)
    return Actual(**base)


def test_full_form_round_trip(tmp_path):
    store = tmp_path / "actuals.json"
    a = _entry(
        shift="1st shift", item_name="RING NX910", process="CNC FIRST SIDE",
        qty_produced=82, qty_rejected=3, actual_setup_min=120,
        no_power_min=80, no_operator_min=30, tool_problem_min=15,
        machine_breakdown_min=0, no_load_min=0, other_work_min=0,
        remarks="late start",
    )
    rule8_capture_actuals.run(a, store_path=store)

    r = rule8_capture_actuals.load_actuals(store)[0]
    assert r.shift == "1st shift" and r.process == "CNC FIRST SIDE"
    assert r.qty_produced == 82 and r.qty_rejected == 3
    assert r.good_qty() == 79                      # produced − rejected
    assert r.actual_setup_min == 120
    assert r.no_power_min == 80 and r.no_operator_min == 30 and r.tool_problem_min == 15
    assert r.total_downtime_min() == 125           # 80 + 30 + 15
    assert r.remarks == "late start"


def test_appends(tmp_path):
    store = tmp_path / "actuals.json"
    rule8_capture_actuals.run(_entry(qty_produced=5), store_path=store)
    after = rule8_capture_actuals.run(_entry(qty_produced=3, entry_date=date(2025, 8, 2)), store_path=store)
    assert len(after) == 2


def test_aggregate_by_item_sums_downtime_per_item():
    actuals = [
        _entry(qty_produced=40, qty_rejected=1, no_operator_min=30, tool_problem_min=10),
        _entry(qty_produced=42, qty_rejected=2, no_operator_min=20, no_power_min=80,
               entry_date=date(2025, 8, 2)),
    ]
    rows = rule8_capture_actuals.aggregate_by_item(actuals)
    assert len(rows) == 1                            # same item code -> one rolled-up row
    row = rows[0]
    assert row["Item Code"] == "61240807-01"
    assert row["Qty Produced"] == 82 and row["Qty Rejected"] == 3
    assert row["Good Qty"] == 79
    assert row["No Operator"] == 50                  # 30 + 20 summed across entries
    assert row["Total Downtime (min)"] == 140        # (30+10) + (20+80)
