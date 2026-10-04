"""Planning uses the CNC/VMC cycle time + 30%; everything else uses the Excel value.

Owner's rule (2026-10-04): the Item's process Master holds the ORIGINAL cycle times
and is never changed. Production PLANNING (the plan, the optimizer, Add New Orders,
the Rule 3 priority order) schedules every CNC/VMC step at cycle x 1.30. Production
analysis, operator efficiency and the master as displayed keep the original value.
Manual, inspection and outsourced steps are never padded.
"""

from __future__ import annotations

import io
from datetime import date

import pytest

from engine import book_store, loaders, pipeline, production_analysis
from engine import new_engine
from engine.config import Config
from engine.models import PlanRun
from engine.planning_time import CNC_VMC_PLANNING_FACTOR
from engine.rules import rule3_tiebreak_process_time as r3

from ppc_engine.domain.routing import OperationKind
from ppc_engine.loaders import load_all as new_load
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)


@pytest.fixture()
def wb_bytes():
    raw = build_new_sample_bytes()
    book_store.save_masters_bytes(raw)
    new_engine._MASTERS_CACHE.clear()
    return raw


def _excel_ops(raw):
    """The workbook's own operations, read with no planning adjustment."""
    return new_load(io.BytesIO(raw)).masters.routings


def test_the_factor_is_thirty_percent():
    assert CNC_VMC_PLANNING_FACTOR == pytest.approx(1.30)


def test_planner_masters_pad_cnc_vmc_only(wb_bytes):
    excel = _excel_ops(wb_bytes)
    planned = new_engine._new_masters(False).routings
    kinds = set()
    for code, routing in excel.items():
        for raw_op, plan_op in zip(routing.operations, planned[code].operations):
            assert plan_op.kind == raw_op.kind          # classification never moves
            kinds.add(raw_op.kind)
            if raw_op.kind == OperationKind.MACHINING:
                assert plan_op.cycle_min == pytest.approx(raw_op.cycle_min * 1.30)
            else:
                assert plan_op.cycle_min == raw_op.cycle_min
    # non-vacuous: the sample really carries padded and unpadded kinds
    assert OperationKind.MACHINING in kinds
    assert kinds - {OperationKind.MACHINING, OperationKind.DISPATCH}


def test_flexible_machine_masters_are_padded_too(wb_bytes):
    excel = _excel_ops(wb_bytes)
    planned = new_engine._new_masters(True).routings
    code, routing = next((c, r) for c, r in excel.items()
                         if any(o.kind == OperationKind.MACHINING for o in r.operations))
    raw_op = next(o for o in routing.operations if o.kind == OperationKind.MACHINING)
    plan_op = next(o for o in planned[code].operations if o.seq == raw_op.seq)
    assert plan_op.cycle_min == pytest.approx(raw_op.cycle_min * 1.30)


def test_scheduled_cnc_work_is_cycle_times_qty_plus_thirty_percent(wb_bytes):
    """The plan itself: a CNC/VMC op occupies setup + qty x cycle x 1.3; a manual
    or inspection op occupies qty x cycle, exactly as the Excel says."""
    so_lines, masters = loaders.load_all(io.BytesIO(wb_bytes))
    run = PlanRun(so_lines=so_lines)
    pipeline.run_forward(run, _CONF, masters)
    assert run.schedule
    excel = _excel_ops(wb_bytes)
    checked = {OperationKind.MACHINING: 0, "other": 0}
    for e in run.schedule:
        op = next((o for o in excel[e.item_code].operations
                   if new_engine._norm(o.name) == new_engine._norm(e.process_name)), None)
        if op is None or op.kind in (OperationKind.OUTSOURCED, OperationKind.DISPATCH):
            continue
        if op.kind == OperationKind.MACHINING:
            want = _CONF.setup_time_min + e.qty * op.cycle_min * 1.30
            checked[OperationKind.MACHINING] += 1
        else:
            want = e.qty * op.cycle_min
            checked["other"] += 1
        assert e.occupancy_min == pytest.approx(want, abs=1.0), (e.item_code, e.process_name)
    assert checked[OperationKind.MACHINING] and checked["other"]


def test_production_analysis_reads_the_excel_cycle_time(wb_bytes):
    _, masters = loaders.load_all(io.BytesIO(wb_bytes))
    excel = _excel_ops(wb_bytes)
    code, routing = next((c, r) for c, r in excel.items()
                         if any(o.kind == OperationKind.MACHINING for o in r.operations))
    op = next(o for o in routing.operations if o.kind == OperationKind.MACHINING)
    # plan once so any planning-side caching has happened
    new_engine._new_masters(False)
    assert production_analysis.cycle_time_for(masters, code, op.name) == pytest.approx(op.cycle_min)
    proc = next(p for p in masters.routings[code].processes if p.name == op.name)
    assert proc.cycle_time == pytest.approx(op.cycle_min)   # the displayed master is untouched


def test_rule3_priority_work_uses_planning_time_on_the_new_engine(wb_bytes):
    _, masters = loaders.load_all(io.BytesIO(wb_bytes))
    excel = _excel_ops(wb_bytes)
    code = next(c for c, r in excel.items()
                if any(o.kind == OperationKind.MACHINING for o in r.operations))
    routing = masters.routings[code]
    padded = sum(o.cycle_min * (1.30 if o.kind == OperationKind.MACHINING else 1.0)
                 for o in excel[code].operations
                 if o.kind not in (OperationKind.OUTSOURCED, OperationKind.DISPATCH))
    assert r3._cycle_per_piece(routing, _CONF) == pytest.approx(padded)
    n_work = sum(1 for p in routing.processes if r3._is_machine_work(p))
    assert r3._work_needed(routing, 10, _CONF) == pytest.approx(
        padded * 10 + n_work * _CONF.setup_time_min)


def test_rule3_on_the_retired_classic_engine_is_unchanged(wb_bytes):
    _, masters = loaders.load_all(io.BytesIO(wb_bytes))
    classic = Config(scheduler="classic", plan_start_date=date(2025, 3, 3))
    for routing in masters.routings.values():
        raw = sum(p.cycle_time for p in routing.processes
                  if isinstance(p.cycle_time, (int, float)) and r3._is_machine_work(p))
        assert r3._cycle_per_piece(routing, classic) == pytest.approx(raw)
        assert r3._cycle_per_piece(routing) == pytest.approx(raw)


def test_an_outsourced_thousands_step_is_never_padded():
    """OS detection runs on the ORIGINAL number, and an OS lead time is not a cycle."""
    from engine.planning_time import pad_routings
    from ppc_engine.domain.routing import Operation, Routing
    r = Routing("X", "x", (
        Operation(1, "CNC FIRST SIDE", OperationKind.MACHINING, ("CNC1",), 10.0),
        Operation(2, "PLATING OS", OperationKind.OUTSOURCED, (), 7200.0),
        Operation(3, "DEBURING", OperationKind.MANUAL, ("MD1",), 4.0),
        Operation(4, "INSPECTION", OperationKind.INSPECTION, ("MI1",), 2.0),
        Operation(5, "DISPATCH", OperationKind.DISPATCH, (), 0.0),
    ))
    out = pad_routings({"X": r})["X"].operations
    assert [o.cycle_min for o in out] == pytest.approx([13.0, 7200.0, 4.0, 2.0, 0.0])
    assert r.operations[0].cycle_min == 10.0   # the input is never mutated


def test_rule4_example_prints_the_cycle_the_plan_used(wb_bytes):
    """The Rule 4 tab's worked example must show the padded cycle beside the padded
    occupancy, not the Excel value next to a number it did not produce."""
    from api import main as api_main
    from engine.planning_time import planning_cycle_time
    so_lines, masters = loaders.load_all(io.BytesIO(wb_bytes))
    run = PlanRun(so_lines=so_lines)
    trace = pipeline.run_forward(run, _CONF, masters)
    api_main._augment_helpers(trace, run, _CONF, masters)
    proc = masters.routings[run.schedule[0].item_code].processes[0]
    padded = planning_cycle_time(proc, _CONF)
    assert padded == pytest.approx(proc.cycle_time * 1.30)   # non-vacuous: a CNC/VMC step
    assert f"cycle({padded})" in trace["rule4"]["notes"][0]
