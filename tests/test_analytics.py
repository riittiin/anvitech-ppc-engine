"""Analytics — utilization computed from a plan (hand-verified numbers)."""
import os
from datetime import date

import pytest

from engine.config import Config, OVERLAP_PERCENT
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters, Operator
from engine.rules import rule6_allocate
from engine import analytics


def _masters(procs, machines):
    ms = {m: Machine(machine_no=m, display_name=m, machine_type=t, available_hrs_per_day=hrs)
          for m, t, hrs in machines}
    mm = Masters(machines=ms, calendar=WorkCalendar())
    mm.routings["X"] = Routing(item_code="X", description="RING", customer="", rm_type="",
                               moq=None, processes=procs)
    return mm


def _batch(qty=50):
    return Batch(batch_id="B", item_code="X", item_name="RING", qty=qty,
                 so_delivery_date=date(2025, 3, 20), source_so_refs=["SO"])


def test_machine_utilization_is_busy_over_available_in_window():
    # One two-shift machine M; P1 = 10 min/pc x 50 + 90 setup = 590 min busy.
    procs = [Process(1, "CNC", 10, 10, "M", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))          # Wed; sequential; op-logic off
    sched = rule6_allocate.run([_batch(50)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    m = next(r for r in a["machines"] if r["Machine"] == "M")
    assert m["Busy (hrs)"] == round(590 / 60.0, 1)          # hand-computed busy
    clock_for, _ = rule6_allocate._clock_factory(masters, cfg)
    win_start = min(e.start for e in sched)
    win_end = max(e.end for e in sched)
    avail = clock_for("M").working_minutes_between(win_start, win_end) / 60.0
    assert m["Available (hrs)"] == round(avail, 1)
    assert m["Utilization %"] == round(m["Busy (hrs)"] / m["Available (hrs)"] * 100.0, 1)
    assert m["Ops"] == 1 and m["Pieces"] == 50


def test_machine_busy_matches_build_machine_view():
    # Cross-check: analytics Busy (hrs)*60 must equal build_machine_view's Busy (min).
    procs = [Process(1, "CNC", 4, 4, "M", None), Process(2, "VMC", 3, 3, "N", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5), ("N", "VMC", 19.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(30)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    _, summary = rule6_allocate.build_machine_view(sched, masters, cfg)
    mv = {r["Machine"]: r["Busy (min)"] for r in summary}
    for r in a["machines"]:
        assert round(r["Busy (hrs)"] * 60.0) == round(mv[r["Machine"]])   # two paths agree


def test_machine_group_rollup_by_type():
    procs = [Process(1, "CNC A", 5, 5, "M1", None), Process(2, "CNC B", 5, 5, "M2", None)]
    masters = _masters(procs, [("M1", "CNC lathe", 19.5), ("M2", "CNC lathe", 19.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(40)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    g = next(r for r in a["machine_groups"] if r["Type"] == "CNC lathe")
    assert g["Machines"] == 2
    # Group util = EXACT total busy / EXACT total available across the two CNCs
    # (computed from raw minutes, not the rounded per-machine display values).
    clock_for, _ = rule6_allocate._clock_factory(masters, cfg)
    win_s = min(e.start for e in sched)
    win_e = max(e.end for e in sched)
    busy = sum(e.occupancy_min for e in sched) / 60.0
    avail = sum(clock_for(m).working_minutes_between(win_s, win_e) for m in ("M1", "M2")) / 60.0
    assert g["Utilization %"] == round(busy / avail * 100.0, 1)
    assert g["Busy (hrs)"] == round(busy, 1)


def test_process_work_share_sums_to_machine_busy():
    procs = [Process(1, "CNC", 4, 4, "M", None), Process(2, "WASH", 2, 2, "N", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5), ("N", "Manual Washing", 9.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(30)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    total_proc = round(sum(p["Work (hrs)"] for p in a["processes"]), 1)
    total_machine = round(sum(m["Busy (hrs)"] for m in a["machines"]), 1)
    assert total_proc == total_machine                      # all work accounted for
    assert round(sum(p["Share %"] for p in a["processes"])) == 100


def test_operator_utilization_uses_shift_capacity():
    procs = [Process(1, "CNC", 4, 4, "M", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5)])
    masters.operators = [Operator("Op One", "M", machines=["M"], shift="First shift")]
    cfg = Config(plan_start_date=date(2025, 3, 5), apply_operator_logic=True)
    sched = rule6_allocate.run([_batch(30)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    assert a["operators"], "operator section populated when operator logic is on"
    o = a["operators"][0]
    assert o["Operator"] == "Op One"
    assert o["Busy (hrs)"] > 0 and 0 <= o["Utilization %"] <= 100


def test_headline_flags_bottleneck_and_underused():
    procs = [Process(1, "HEAVY", 20, 20, "M", None), Process(2, "LIGHT", 1, 1, "N", None)]
    masters = _masters(procs, [("M", "CNC lathe", 19.5), ("N", "Manual Washing", 9.5)])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(200)], config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg)
    assert a["headline"]["bottleneck"]["Machine"] == a["machines"][0]["Machine"]
    assert all(m["Utilization %"] <= 30 for m in a["headline"]["underused"])


def test_empty_plan_returns_empty_analytics():
    masters = _masters([Process(1, "CNC", 4, 4, "M", None)], [("M", "CNC lathe", 19.5)])
    a = analytics.build_analytics([], masters, Config())
    assert a["machines"] == [] and a["headline"] == {} and a["window"] is None


@pytest.mark.skipif(not os.path.exists("Test4.xlsx"), reason="real data file not present")
def test_analytics_invariants_on_real_plan():
    from engine.loaders import load_all
    so, masters = load_all("Test4.xlsx")
    cfg = Config(plan_start_date=date(2026, 7, 1), overlap_mode=OVERLAP_PERCENT,
                 overlap_percent=50, split_parallel=True)
    batches = [Batch(batch_id=f"B{i}", item_code=s.item_code, item_name=s.item_name,
                     qty=s.qty or 10, so_delivery_date=s.delivery_date, source_so_refs=[s.so_no])
               for i, s in enumerate(so[:20])]
    sched = rule6_allocate.run(batches, config=cfg, masters=masters)
    a = analytics.build_analytics(sched, masters, cfg, batches)
    for m in a["machines"]:
        u = m["Utilization %"]
        assert u is None or 0 <= u <= 100                       # in range
        assert m["Busy (hrs)"] <= m["Available (hrs)"] + 0.1    # busy never exceeds capacity
    assert round(sum(p["Work (hrs)"] for p in a["processes"]), 0) == \
           round(sum(m["Busy (hrs)"] for m in a["machines"]), 0)   # no work lost/invented
