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
    un = r["steps"][1]
    assert un["actual_hours"] is None and un["actual_cost"] is None
    assert r["actual_cost"] == 1000 and r["steps"][0]["actual_cost"] == 1000


def test_totals_exclude_incomplete_rows_even_with_both_costs():
    sn = {**snap(("A", "X"), [pstep("CNC4", 0, 600)]),
          **snap(("B", "X"), [pstep("CNC4", 0, 600), pstep("CMM", 0, 60)])}
    ac = {("A", "X"): [act("CNC4", 600)], ("B", "X"): [act("CNC4", 600)]}
    t = build_report([order("A"), order("B")], sn, ac, {"CNC4": 60, "CMM": 60})["totals"]
    assert (t["orders"], t["counted"], t["incomplete"]) == (2, 1, 1)
    assert t["planned_cost"] == 600 and t["actual_cost"] == 600


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
    assert t["incomplete"] == 1 and t["counted"] == 2


# ================================================== final-review fix wave (2026-11-11)

def masters_full():
    """masters() plus the non-counted steps the real form saves with a blank machine."""
    procs = [
        Process(1, "CNC FIRST SIDE", 10.0, None, "CNC4", "CNC4"),
        Process(2, "INSP", 5.0, None, "CMM", "CMM"),
        Process(3, "BAND SAW OS", 2880.0, None, "OS", "OS"),
        Process(4, "DEBURING", 1.0, None, "MD1", None),
        Process(5, "DISPATCH", None, None, None, None),
        Process(6, "ALT STEP", 4.0, None, "MD1/CNC4", None),
    ]
    return SimpleNamespace(routings={"X": Routing("X", "", "", "", None, procs)})


def amf(actuals):
    return actual_minutes(actuals, masters_full(), KINDS, WORKING)


@pytest.mark.parametrize("process", ["BAND SAW OS", "DISPATCH", "DEBURING"])
def test_blank_machine_punch_on_a_not_counted_step_is_dropped(process):
    # The real form saves OS / DISPATCH / blank-Allotted manual steps with machine ""
    # and a fixed 630: that must never flag the order.
    assert amf([punch(machine="", mins=630, process=process)]) == {}


@pytest.mark.parametrize("process", ["CNC FIRST SIDE", "INSP", "ALT STEP", "NOT IN ROUTING"])
def test_blank_machine_punch_on_a_counted_step_is_still_flagged(process):
    (r,) = amf([punch(machine="", mins=630, process=process)])[("SO1", "X")]
    assert r["machine"] == "" and r["minutes"] is None


def test_os_punch_with_blank_machine_does_not_hide_the_order():
    acts = amf([punch(machine="CNC4", mins=630),
                punch(machine="", mins=630, process="BAND SAW OS", day=date(2026, 11, 4)),
                punch(machine="", mins=630, process="DISPATCH", day=date(2026, 11, 5))])
    rep = build_report([order("SO1")], snap(("SO1", "X"), [pstep("CNC4", 0, 600)]),
                       acts, {"CNC4": 60})
    assert rep["not_shown"] == [] and len(rep["rows"]) == 1
    assert rep["rows"][0]["actual_hours"] == pytest.approx(10.5)


# ---- item 2: an unstarted order with no counted work gets an empty snapshot
def test_planned_snapshot_records_orders_with_no_counted_steps():
    es = [entry("CNC4", 350, 20, refs=("SO1",)),
          entry("MD1", 60, 20, refs=("SO2",), batch="B2")]
    from engine.profit_loss import planned_snapshot
    out = planned_snapshot(es, [batch({"SO1": 20})], masters(), cfg(), KINDS)
    assert out[("SO2", "X")] == [] and out[("SO1", "X")]


# ---- item 4: partial costs when a rate is missing
def test_partial_cost_sums_the_priced_steps_and_says_so():
    sn = snap(("SO1", "X"), [pstep("CNC4", 0, 600), pstep("CMM", 0, 60)])
    r = build_report([order("SO1")], sn,
                     {("SO1", "X"): [act("CNC4", 600), act("CMM", 60, "INSPECTION")]},
                     {"CNC4": 100})["rows"][0]
    assert r["planned_cost"] == 1000 and r["actual_cost"] == 1000
    assert r["planned_cost_partial"] is True and r["actual_cost_partial"] is True
    assert r["diff_cost"] is None and r["diff_pct"] is None
    assert r["rates_missing"] == ["CMM"]


def test_partial_only_on_the_side_that_lacks_a_rate():
    sn = snap(("SO1", "X"), [pstep("CNC4", 0, 600)])
    r = build_report([order("SO1")], sn, {("SO1", "X"): [act("CMM", 60, "CNC FIRST SIDE")]},
                     {"CNC4": 100})["rows"][0]
    assert r["planned_cost_partial"] is False and r["actual_cost_partial"] is True
    assert r["planned_cost"] == 1000 and r["actual_cost"] is None and r["diff_cost"] is None


def test_complete_rates_are_not_partial():
    sn = snap(("SO1", "X"), [pstep("CNC4", 0, 600)])
    r = build_report([order("SO1")], sn, {("SO1", "X"): [act("CNC4", 600)]},
                     {"CNC4": 100})["rows"][0]
    assert r["planned_cost_partial"] is False and r["actual_cost_partial"] is False
    assert r["diff_cost"] == 0


def test_partial_rows_stay_out_of_the_totals():
    sn = {**snap(("A", "X"), [pstep("CNC4", 0, 600)]),
          **snap(("B", "X"), [pstep("CNC4", 0, 600), pstep("CMM", 0, 60)])}
    ac = {("A", "X"): [act("CNC4", 600)],
          ("B", "X"): [act("CNC4", 600), act("CMM", 60, "INSPECTION")]}
    t = build_report([order("A"), order("B")], sn, ac, {"CNC4": 60})["totals"]
    assert t["counted"] == 1 and t["planned_cost"] == 600


# ---- item 5: an unchanged snapshot keeps its stamp (no store write per plan)
def test_merge_keeps_the_stamp_when_steps_are_unchanged():
    stored = {"A\x1fX": {"steps": [OLD], "at": "t0"}}
    out = merge_snapshot(stored, {("A", "X"): [dict(OLD)]}, set(), "t1")
    assert out == stored and out["A\x1fX"]["at"] == "t0"


# ---- item 6: totals over zero complete orders are not zero
def test_totals_are_none_when_no_order_is_complete():
    sn = snap(("SO1", "X"), [pstep("CNC4", 0, 600)])
    t = build_report([order("SO1")], sn, {("SO1", "X"): [act("CNC4", 600)]}, {})["totals"]
    assert t["counted"] == 0 and t["orders"] == 1
    for f in ("planned_hours", "actual_hours", "planned_cost", "actual_cost",
              "diff_cost", "diff_pct"):
        assert t[f] is None, f


# ---- item 9 T1: a clubbed line's share follows its per-step qty, not its ordered qty
def test_clubbed_share_follows_each_lines_step_qty():
    # SO1 ordered 30 but only 10 still owed on this step; SO2 owes its full 10.
    b = batch({"SO1": 30, "SO2": 10}, pq={"SO1": {"CNC FIRST SIDE": 10}})
    out = planned_steps([entry("CNC4", 90 + 20 * 13, 20, refs=("SO1", "SO2"))],
                        [b], masters(), cfg(), KINDS)
    assert out[("SO1", "X")][0]["setup_min"] == pytest.approx(45)
    assert out[("SO2", "X")][0]["setup_min"] == pytest.approx(45)
    assert out[("SO1", "X")][0]["run_min"] == pytest.approx(100)


# ---- item 9 T3: overbooked-shift weight = master cycle (not the punch's) + setup
def test_overbooked_weight_uses_master_cycle_and_actual_setup():
    a = punch("SO1", qty=10, mins=630, setup=0.0)
    a.cycle_time_min = 100.0               # snapshot differs from the master's 10
    b = punch("SO2", qty=10, mins=630, setup=100.0)
    out = am([a, b])
    # weights: SO1 10*10 = 100, SO2 10*10 + 100 = 200
    assert out[("SO1", "X")][0]["minutes"] == pytest.approx(210)
    assert out[("SO2", "X")][0]["minutes"] == pytest.approx(420)


def test_overbooked_weight_falls_back_to_the_punch_cycle_without_a_master():
    a = punch("SO1", qty=10, mins=630, process="NOT IN ROUTING")
    a.cycle_time_min = 20.0
    b = punch("SO2", qty=10, mins=630)
    out = am([a, b])
    # weights: SO1 10*20 = 200 (punch cycle), SO2 10*10 = 100
    assert out[("SO1", "X")][0]["minutes"] == pytest.approx(420)
