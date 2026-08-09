"""A machine must be allowed to use a gap in its own timeline (live 2026-08-09).

Proven cause of the owner's idle-capacity finding. `machine_free` is a SCALAR — the
machine's last committed end — and `_place_operation` starts every candidate at
`max(ready, machine_free[mid])`. So the moment one operation is committed late for
its OWN routing reasons, the whole span before it becomes permanently unusable, even
for work that was ready and staffable the entire time.

Measured on Test9 with 30 orders part-finished, CNC3:

    18-08 09:45 -> 12:09   SO118 seq2
                           <-- IDLE 101.4 h
    22-08 17:36 -> 20:36   SO118 seq4   (waited on its own seq3, elsewhere)
    22-08 20:36 -> 24-08   SO69  seq1   (ready and staffable the whole time)

Total across the book: 335.6 h (14.0 days) of machine idle inside working hours with
ready work AND a free qualified operator.

The fix is first-fit backfill: try the earliest gap that can hold the WHOLE operation,
falling back to after the last committed op. An operation is never fragmented across
another order's work — that would need a second setup, which the block model does not
charge — so a gap is only used when the op fits in it complete.
"""
import io
from datetime import date, datetime, timedelta

import pytest

from engine.config import Config
from engine.new_engine import _orders_from_batches, _plan_config
from engine.rules import rule1_consolidate
from engine import book_store, loaders
from ppc_engine.loaders import load_all as new_load
from ppc_engine.scheduler import decode
from ppc_engine.scheduler.flow_scheduler import _first_fit_on_machine, _lay_on_machine
from ppc_engine.scheduler.staffing import StaffingBoard, build_machine_pools
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
               apply_operator_logic=True)


@pytest.fixture()
def ctx():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, _ = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, _ = _orders_from_batches(batches, nm)
    return orders, [o.key for o in orders], nm, _plan_config(_CONF)


def _machining_op(order, nm):
    routing = nm.routings[order.item_code]
    return next(op for op in routing.operations
                if op.machine_options and op.cycle_min > 0)


def test_a_ready_operation_uses_an_existing_gap_instead_of_queueing_at_the_end(ctx):
    orders, _seq, nm, cfg = ctx
    o0 = orders[0]
    op = _machining_op(o0, nm)
    mid = op.machine_options[0]
    machine = nm.machines[mid]
    staffing = StaffingBoard(build_machine_pools(nm))

    # The machine is committed LATE, leaving a wide early gap — the shape the live
    # plan produced on CNC3.
    gap_end = cfg.plan_start + timedelta(hours=6)
    busy = [(gap_end, gap_end + timedelta(hours=8))]

    laid = _first_fit_on_machine(machine, cfg.plan_start, 60.0, o0, op, 10,
                                 staffing, nm, cfg, busy)
    assert laid is not None
    assert laid["start"] == cfg.plan_start, (
        "an hour of work, ready now, with six free hours in front of it, must not "
        f"queue behind the committed block: got {laid['start']}")
    assert laid["end"] <= gap_end, "backfilled work must not run into committed work"


def test_work_too_big_for_the_gap_still_queues_after_the_committed_block(ctx):
    """No fragmenting an operation around another order — that would need a second
    setup the block model never charges."""
    orders, _seq, nm, cfg = ctx
    o0 = orders[0]
    op = _machining_op(o0, nm)
    mid = op.machine_options[0]
    machine = nm.machines[mid]
    staffing = StaffingBoard(build_machine_pools(nm))

    gap_end = cfg.plan_start + timedelta(hours=2)
    block_end = gap_end + timedelta(hours=8)
    laid = _first_fit_on_machine(machine, cfg.plan_start, 600.0, o0, op, 100,
                                 staffing, nm, cfg, [(gap_end, block_end)])
    assert laid is not None
    assert laid["start"] >= block_end, (
        f"600 min cannot fit in a 2 h gap; it must start after the block, got "
        f"{laid['start']}")


def test_backfilled_work_never_overlaps_committed_work(ctx):
    orders, _seq, nm, cfg = ctx
    o0 = orders[0]
    op = _machining_op(o0, nm)
    machine = nm.machines[op.machine_options[0]]
    staffing = StaffingBoard(build_machine_pools(nm))
    busy = [(cfg.plan_start + timedelta(hours=1), cfg.plan_start + timedelta(hours=3)),
            (cfg.plan_start + timedelta(hours=5), cfg.plan_start + timedelta(hours=9))]
    for minutes in (30, 60, 90, 120, 240):
        laid = _first_fit_on_machine(machine, cfg.plan_start, float(minutes), o0, op,
                                     10, staffing, nm, cfg, busy)
        assert laid is not None
        for seg in laid["segments"]:
            for bs, be in busy:
                assert seg.end <= bs or seg.start >= be, (
                    f"{minutes} min of work overlaps committed work "
                    f"{bs}-{be}: {seg.start}-{seg.end}")


def test_no_busy_intervals_is_byte_identical_to_the_old_placement(ctx):
    """With an empty machine the new path must reproduce the old one exactly."""
    orders, _seq, nm, cfg = ctx
    o0 = orders[0]
    op = _machining_op(o0, nm)
    machine = nm.machines[op.machine_options[0]]
    a = _lay_on_machine(machine, cfg.plan_start, 300.0, o0, op, 50,
                        StaffingBoard(build_machine_pools(nm)), nm, cfg)
    b = _first_fit_on_machine(machine, cfg.plan_start, 300.0, o0, op, 50,
                              StaffingBoard(build_machine_pools(nm)), nm, cfg, [])
    assert a["start"] == b["start"] and a["end"] == b["end"]
    assert [(s.start, s.end, s.operator) for s in a["segments"]] == \
           [(s.start, s.end, s.operator) for s in b["segments"]]


def test_a_whole_plan_never_double_books_a_machine(ctx):
    """The invariant backfill could break: two operations on one machine at once."""
    orders, seq, nm, cfg = ctx
    sched = decode(orders, seq, nm, cfg)
    by_machine = {}
    for s in sched.segments:
        if s.machine_id is None:
            continue
        by_machine.setdefault(s.machine_id, []).append((s.start, s.end, s.order_key))
    for mid, ivs in by_machine.items():
        ivs.sort()
        for (s1, e1, k1), (s2, e2, k2) in zip(ivs, ivs[1:]):
            assert s2 >= e1, f"{mid} double-booked: {k1} {s1}-{e1} vs {k2} {s2}-{e2}"


def test_a_gap_that_is_long_in_WALL_CLOCK_but_short_in_WORKING_time_is_not_used(ctx):
    """The cheap pre-filter only compares wall-clock length, so a gap straddling the
    night looks big enough and is not. Only the deadline inside `_lay_on_machine`
    stops the work spilling into the committed block on the far side.

    (Mutation-checked: drop the deadline and this test fails while every other one
    still passes — the earlier cases all sat inside a single working day.)
    """
    orders, _seq, nm, cfg = ctx
    o0 = orders[0]
    op = _machining_op(o0, nm)
    machine = nm.machines[op.machine_options[0]]      # two-shift CNC: 08:00 -> 05:00
    staffing = StaffingBoard(build_machine_pools(nm))

    day2 = datetime.combine((cfg.plan_start + timedelta(days=1)).date(), datetime.min.time())
    gap_start = day2 + timedelta(hours=4)             # 04:00, one hour of night shift left
    gap_end = day2 + timedelta(hours=10)              # 10:00 next morning
    busy = [(cfg.plan_start, gap_start), (gap_end, gap_end + timedelta(hours=6))]
    assert (gap_end - gap_start).total_seconds() / 60 >= 240, "wall clock must look big"

    laid = _first_fit_on_machine(machine, cfg.plan_start, 240.0, o0, op, 50,
                                 staffing, nm, cfg, busy)
    assert laid is not None
    for seg in laid["segments"]:
        for bs, be in busy:
            assert seg.end <= bs or seg.start >= be, (
                f"work spilled out of the gap into committed time {bs}-{be}: "
                f"{seg.start}-{seg.end}")
