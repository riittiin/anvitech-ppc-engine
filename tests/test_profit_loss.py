"""Planned machine minutes per order, from a plan (engine/profit_loss.py)."""
from datetime import datetime, date, timedelta
from types import SimpleNamespace

import pytest

from engine.config import Config
from engine.models import Actual, Batch, Process, Routing, ScheduleEntry
from engine.models import Order
from engine.profit_loss import (COUNTED_KINDS, actual_minutes, build_report, machine_kinds,
                                planned_steps)

T0 = datetime(2026, 11, 2, 8, 0)
DOC = {"machines": {
    "CNC4": {"name": "CNC4", "type": "CNC lathe", "hours": 20, "version": 1},
    "CMM": {"name": "CMM", "type": "Inspection CMM", "hours": 9, "version": 1},
    "MD1": {"name": "MD1", "type": "Deburring", "hours": 9, "version": 1},
}}
KINDS = machine_kinds(DOC)


def cfg():
    return Config(scheduler="new")


def masters():
    procs = [
        Process(1, "CNC FIRST SIDE", 10.0, None, "CNC4", "CNC4"),
        Process(2, "INSP", 5.0, None, "CMM", "CMM"),
    ]
    return SimpleNamespace(routings={"X": Routing("X", "", "", "", None, procs)})


def entry(machine, occ, qty, name="CNC FIRST SIDE", seq=1, refs=("SO1",), piece=(),
          resumed=False, batch="B1"):
    return ScheduleEntry(batch, "X", seq, name, machine, qty, occ, T0,
                         T0 + timedelta(minutes=occ), so_refs=list(refs),
                         piece_refs=list(piece), resumed=resumed)


def batch(lines, pq=None):
    lpq = {so: (q, (pq or {}).get(so)) for so, q in lines.items()}
    return Batch("B1", "X", "X", sum(lines.values()), date(2026, 12, 1),
                 source_so_refs=list(lines), line_process_qty=lpq)


def test_kinds():
    assert KINDS == {"CNC4": "machining", "CMM": "inspection", "MD1": "manual"}
    assert COUNTED_KINDS == {"machining", "inspection"}


def test_single_line_cnc_step_unpads_run_and_keeps_setup():
    out = planned_steps([entry("CNC4", 350, 20)], [batch({"SO1": 20})], masters(), cfg(), KINDS)
    (s,) = out[("SO1", "X")]
    assert s["machine"] == "CNC4" and s["setup_min"] == pytest.approx(90)
    assert s["run_min"] == pytest.approx(200)
    assert s["step"] == "CNC FIRST SIDE" or s["process"] == "CNC FIRST SIDE"


def test_inspection_step_is_counted_and_never_unpadded():
    e = entry("CMM", 100, 20, name="INSP", seq=2)
    (s,) = planned_steps([e], [batch({"SO1": 20})], masters(), cfg(), KINDS)[("SO1", "X")]
    assert s["run_min"] == pytest.approx(100) and s["setup_min"] == pytest.approx(0)


def test_manual_step_is_not_counted():
    assert planned_steps([entry("MD1", 60, 20)], [batch({"SO1": 20})], masters(), cfg(), KINDS) == {}


def test_unknown_machine_not_counted():
    assert planned_steps([entry("ZZ9", 60, 20)], [batch({"SO1": 20})], masters(), cfg(), KINDS) == {}


def test_clubbed_batch_splits_by_each_lines_pieces():
    out = planned_steps([entry("CNC4", 90 + 40 * 13, 40, refs=("SO1", "SO2"))],
                        [batch({"SO1": 30, "SO2": 10})], masters(), cfg(), KINDS)
    assert out[("SO1", "X")][0]["setup_min"] == pytest.approx(67.5)
    assert out[("SO2", "X")][0]["setup_min"] == pytest.approx(22.5)
    assert out[("SO1", "X")][0]["run_min"] == pytest.approx(300)
    assert out[("SO2", "X")][0]["run_min"] == pytest.approx(100)


def test_one_operation_in_two_blocks_pays_setup_once():
    es = [entry("CNC4", 200, 20), entry("CNC4", 150, 20)]
    (s,) = planned_steps(es, [batch({"SO1": 20})], masters(), cfg(), KINDS)[("SO1", "X")]
    assert s["setup_min"] == pytest.approx(90)


def test_piece_refs_limit_the_lines_on_a_bar():
    out = planned_steps([entry("CNC4", 350, 20, refs=("SO1", "SO2"), piece=("SO2",))],
                        [batch({"SO1": 10, "SO2": 10})], masters(), cfg(), KINDS)
    assert ("SO1", "X") not in out and ("SO2", "X") in out


def test_missing_routing_step_uses_fallback_formula():
    e = entry("CNC4", 130, 10, name="UNKNOWN STEP")
    (s,) = planned_steps([e], [batch({"SO1": 10})], masters(), cfg(), KINDS)[("SO1", "X")]
    assert s["setup_min"] == 0 and s["run_min"] == pytest.approx(100)


# ---- snapshot merge (frozen at first punch) ---- #
from engine.profit_loss import merge_snapshot

OLD = {"process": "P", "step": "p", "machine": "CNC4", "setup_min": 1, "run_min": 2}
NEW = {"process": "P", "step": "p", "machine": "CNC4", "setup_min": 9, "run_min": 9}


def test_merge_overwrites_unstarted_orders():
    out = merge_snapshot({"A\x1fX": {"steps": [OLD], "at": "t0"}}, {("A", "X"): [NEW]}, set(), "t1")
    assert out["A\x1fX"] == {"steps": [NEW], "at": "t1"}


def test_merge_never_touches_a_started_order():
    out = merge_snapshot({"A\x1fX": {"steps": [OLD], "at": "t0"}}, {("A", "X"): [NEW]}, {("A", "X")}, "t1")
    assert out["A\x1fX"] == {"steps": [OLD], "at": "t0"}


def test_an_order_started_before_any_snapshot_never_gets_one():
    assert "A\x1fX" not in merge_snapshot({}, {("A", "X"): [NEW]}, {("A", "X")}, "t1")


def test_merge_keeps_orders_no_longer_planned():
    stored = {"A\x1fX": {"steps": [OLD], "at": "t0"}}
    assert merge_snapshot(stored, {}, set(), "t1") == stored


WORKING = {"1st shift": 630, "2nd shift": 570}


def punch(so="SO1", item="X", machine="CNC4", mins=600, qty=10, setup=0.0,
          shift="1st shift", day=date(2026, 11, 3), process="CNC FIRST SIDE", **down):
    return Actual(so_no=so, item_code=item, entry_date=day, qty_produced=qty,
                  shift=shift, process=process, machine=machine, shift_minutes=mins,
                  actual_setup_min=setup, **down)


def am(actuals):
    return actual_minutes(actuals, masters(), KINDS, WORKING)


def test_minutes_are_shift_minutes_minus_downtime_with_setup_kept():
    a = punch(mins=600, setup=90, no_power_min=30, machine_breakdown_min=20)
    (r,) = am([a])[("SO1", "X")]
    assert r["minutes"] == 550 and r["machine"] == "CNC4"
    assert r["step"] == "CNC FIRST SIDE"


def test_minutes_never_count_past_the_shift():
    assert am([punch(mins=700)])[("SO1", "X")][0]["minutes"] == 630


def test_two_default_punches_on_one_machine_share_the_shift():
    out = am([punch("SO149", qty=30, mins=630), punch("SO150", qty=10, mins=630)])
    assert out[("SO149", "X")][0]["minutes"] == pytest.approx(472.5)
    assert out[("SO150", "X")][0]["minutes"] == pytest.approx(157.5)


def test_punches_that_fit_inside_the_shift_are_used_as_typed():
    out = am([punch("SO1", mins=300), punch("SO2", mins=200)])
    assert [out[k][0]["minutes"] for k in (("SO1", "X"), ("SO2", "X"))] == [300, 200]


def test_downtime_larger_than_minutes_floors_at_zero():
    assert am([punch(mins=100, no_power_min=500)]) == {}


def test_blank_minutes_punch_is_dropped():
    assert am([punch(mins=0)]) == {}


def test_manual_machine_punch_is_not_counted():
    assert am([punch(machine="MD1")]) == {}


def test_unknown_machine_not_counted_but_shares_the_shift():
    out = am([punch("SO1", machine="ZZ9", qty=10, mins=630),
              punch("SO2", machine="ZZ9", qty=10, mins=630)])
    assert out == {}


def test_blank_machine_punch_is_flagged_with_minutes_none():
    (r,) = am([punch(machine="")])[("SO1", "X")]
    assert r["machine"] == "" and r["minutes"] is None


def test_different_days_or_shifts_do_not_share():
    out = am([punch("SO1", mins=630), punch("SO2", mins=630, day=date(2026, 11, 4)),
              punch("SO3", mins=570, shift="2nd shift")])
    assert [out[(s, "X")][0]["minutes"] for s in ("SO1", "SO2", "SO3")] == [630, 630, 570]


def test_unknown_shift_is_not_capped():
    assert am([punch(mins=900, shift="weird")])[("SO1", "X")][0]["minutes"] == 900


# ---------------------------------------------------------------- build_report
def order(so, item="X", done=date(2026, 11, 5), name="Part", qty=10):
    return Order(so, item, name, qty, date(2026, 12, 1), completed=True, completed_on=done)


def pstep(machine, setup=0, run=0, process=None, step=None):
    p = process or {"CNC4": "CNC FIRST SIDE", "CMM": "INSPECTION"}.get(machine, machine)
    return {"process": p, "step": step or p, "machine": machine,
            "setup_min": setup, "run_min": run}


def snap(key, steps):
    return {f"{key[0]}\x1f{key[1]}": {"steps": steps, "at": "2026-11-01T00:00:00"}}


def act(machine, minutes, process="CNC FIRST SIDE"):
    return {"step": process, "process": process, "machine": machine, "minutes": minutes}


def test_owner_example_planned_and_actual_cost():
    r = build_report([order("SO1")], snap(("SO1", "X"), [pstep("CNC4", 0, 6000)]),
                     {("SO1", "X"): [act("CNC4", 7200)]}, {"CNC4": 100})["rows"][0]
    assert (r["planned_cost"], r["actual_cost"], r["diff_cost"]) == (10000, 12000, 2000)
    assert r["diff_pct"] == pytest.approx(20)
    assert r["completed_on"] == "05-11-2026" and r["planned_hours"] == 100


def test_unpunched_step_flags_the_order_incomplete():
    sn = snap(("SO1", "X"), [pstep("CNC4", 0, 600), pstep("CMM", 0, 60)])
    rep = build_report([order("SO1")], sn, {("SO1", "X"): [act("CNC4", 600)]},
                       {"CNC4": 100, "CMM": 50})
    r = rep["rows"][0]
    assert r["not_punched"] == ["INSPECTION"] and r["actual_hours"] == pytest.approx(10)
    assert [s["status"] for s in r["steps"]] == ["ok", "not punched"]
    assert rep["totals"]["incomplete"] == 1


def test_machine_without_rate_is_rate_not_set_never_zero():
    sn = snap(("SO1", "X"), [pstep("CNC4", 0, 600)])
    r = build_report([order("SO1")], sn, {("SO1", "X"): [act("CNC4", 600)]}, {})["rows"][0]
    assert r["planned_cost"] is None and r["actual_cost"] is None
    assert r["rates_missing"] == ["CNC4"] and r["steps"][0]["status"] == "rate not set"
    assert r["planned_hours"] == 10 and r["diff_cost"] is None and r["diff_pct"] is None


def test_order_without_snapshot_is_counted_as_older_not_shown():
    rep = build_report([order("OLD")], {}, {}, {})
    assert rep["rows"] == [] and rep["older_count"] == 1 and rep["not_shown"] == []


def test_blank_machine_punch_moves_order_to_not_shown():
    sn = snap(("SO1", "X"), [pstep("CNC4", 0, 600)])
    rep = build_report([order("SO1")], sn,
                       {("SO1", "X"): [{"step": "A", "process": "A", "machine": "", "minutes": None}]},
                       {"CNC4": 100})
    assert rep["rows"] == []
    assert rep["not_shown"] == [{"so_no": "SO1", "item_code": "X",
                                 "reason": "a punch has no machine, so its time cannot be priced"}]


def test_order_with_no_counted_work_is_not_a_zero_row():
    rep = build_report([order("SO1")], snap(("SO1", "X"), []), {}, {})
    assert rep["rows"] == []
    assert rep["not_shown"][0]["reason"] == "no CNC, VMC or inspection work"


def test_actual_only_step_is_appended_and_same_names_sum():
    sn = snap(("SO1", "X"), [pstep("CNC4", 30, 30), pstep("CNC4", 0, 60)])
    r = build_report([order("SO1")], sn,
                     {("SO1", "X"): [act("CNC4", 60), act("CMM", 120, "INSPECTION")]},
                     {"CNC4": 60, "CMM": 30})["rows"][0]
    assert [s["process"] for s in r["steps"]] == ["CNC FIRST SIDE", "INSPECTION"]
    assert r["steps"][0]["planned_hours"] == 2 and r["steps"][0]["actual_hours"] == 1
    last = r["steps"][-1]
    assert last["planned_hours"] == 0 and last["planned_machine"] == "" and last["actual_cost"] == 60


def test_totals_skip_rows_without_both_costs_and_sort_newest_first():
    sn = {**snap(("A", "X"), [pstep("CNC4", 0, 600)]), **snap(("B", "X"), [pstep("CMM", 0, 600)]),
          **snap(("C", "X"), [pstep("CNC4", 0, 600)])}
    ac = {(s, "X"): [act("CNC4" if s != "B" else "CMM", 600)] for s in "ABC"}
    rep = build_report([order("A", done=date(2026, 11, 1)), order("B", done=date(2026, 11, 9)),
                        order("C", done=None)], sn, ac, {"CNC4": 60})
    assert [r["so_no"] for r in rep["rows"]] == ["B", "A", "C"]
    assert rep["rows"][2]["completed_on"] == ""
    t = rep["totals"]
    assert t["orders"] == 3 and t["planned_cost"] == 1200 and t["actual_cost"] == 1200
    assert t["incomplete"] == 1
