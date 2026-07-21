"""Tests for the new operator-stable engine driving the old build (engine/new_engine.py).

Runs on a fully-staffed synthetic workbook (tests/new_sample_workbook.py) so the new
engine actually schedules it. Headline guarantees — true on any input:
  * NO fragmentation: every operation runs on ONE machine.
  * NO operator ping-pong: one operator per (machine, shift); an operator is never on two
    machines in the same shift (the exact rule the old system broke).
  * NO double-booking of a machine or an operator.
  * per-process feedback: a finished step re-plans as a zero-time milestone.
  * the optimizer's rank map replays to the same plan it found.
  * the whole old UI (pipeline -> Gantt + Analytics) builds from the new engine's output.
"""

from __future__ import annotations

import io
from collections import defaultdict
from datetime import date, timedelta

import pytest

from engine import analytics, book_store, gantt, loaders, pipeline
from engine.config import Config
from engine.models import Batch, PlanRun
from engine.new_engine import _norm, _orders_from_batches, _plan_config, run, sweep_optimize
from engine.rules import rule1_consolidate

from ppc_engine.domain.routing import OperationKind
from ppc_engine.loaders import load_all as new_load
from ppc_engine.optimize import optimize as new_optimize
from ppc_engine.scheduler import decode
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)


@pytest.fixture()
def wb_bytes():
    return build_new_sample_bytes()


@pytest.fixture()
def new_masters(wb_bytes):
    """New-engine Masters + the workbook seeded into the store (so new_engine finds it)."""
    book_store.save_masters_bytes(wb_bytes)
    return new_load(io.BytesIO(wb_bytes)).masters


@pytest.fixture()
def old_book(wb_bytes):
    """(so_lines, masters) via the OLD loaders — what the pipeline/optimizer consume."""
    return loaders.load_all(io.BytesIO(wb_bytes))


def _shift_of(dt):
    if 8 <= dt.hour < 19:
        return (dt.date(), "1")
    return (dt.date() if dt.hour >= 19 else dt.date() - timedelta(days=1), "2")


def _assert_clean(segments):
    work = [s for s in segments if s.machine_id and s.operator]

    by_op = defaultdict(set)
    for s in segments:
        if s.machine_id:
            by_op[(s.order_key, s.op_seq)].add(s.machine_id)
    assert all(len(v) == 1 for v in by_op.values()), "an operation was split across machines"

    mach_shift, op_shift = defaultdict(set), defaultdict(set)
    for s in work:
        mach_shift[(s.machine_id, _shift_of(s.start))].add(s.operator)
        op_shift[(s.operator, _shift_of(s.start))].add(s.machine_id)
    assert all(len(v) == 1 for v in mach_shift.values()), "two operators on one machine in a shift"
    assert all(len(v) == 1 for v in op_shift.values()), "an operator on two machines in a shift"

    def _overlaps(intervals):
        iv = sorted(intervals)
        return any(iv[i][0] < iv[i - 1][1] for i in range(1, len(iv)))

    mach_iv, op_iv = defaultdict(list), defaultdict(list)
    for s in work:
        mach_iv[s.machine_id].append((s.start, s.end))
        op_iv[s.operator].append((s.start, s.end))
    assert not any(_overlaps(v) for v in mach_iv.values()), "a machine was double-booked"
    assert not any(_overlaps(v) for v in op_iv.values()), "an operator was double-booked"


def _decode_book(so_lines, masters):
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, _ = _orders_from_batches(batches, masters)
    seq = [(b.batch_id, b.item_code) for b in batches]
    return decode(orders, seq, masters, _plan_config(_CONF))


def test_plan_has_no_fragmentation_or_pingpong(old_book, new_masters):
    so_lines, _ = old_book
    _assert_clean(_decode_book(so_lines, new_masters).segments)


def test_optimized_order_is_also_clean(old_book, new_masters):
    so_lines, _ = old_book
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, _ = _orders_from_batches(batches, new_masters)
    res = new_optimize(orders, new_masters, _plan_config(_CONF), budget=40, seed=1)
    _assert_clean(decode(orders, res.best_sequence, new_masters, _plan_config(_CONF)).segments)


def test_per_process_feedback_finishes_step_as_milestone(new_masters):
    item = next(c for c, r in new_masters.routings.items()
                if sum(1 for o in r.operations if o.kind == OperationKind.MACHINING) >= 2)
    ops = new_masters.routings[item].operations
    done = next(o for o in ops if o.kind == OperationKind.MACHINING)
    pq = {_norm(o.name): (0 if o.seq == done.seq else 100) for o in ops}
    b = Batch(batch_id="T1", item_code=item, item_name="x", qty=100,
              so_delivery_date=date(2025, 4, 1), source_so_refs=["SO_T1"], process_qty=pq)

    orders, _ = _orders_from_batches([b], new_masters)
    assert orders[0].process_remaining[done.seq] == 0

    entries = {e.process_seq: e for e in run([b], _CONF, None)}
    milestone = entries[done.seq]
    assert milestone.qty == 0
    assert (milestone.end - milestone.start).total_seconds() == 0  # finished step, zero time


def test_optimizer_ranks_replay_to_the_same_plan(old_book, new_masters):
    from engine import optimizer
    so_lines, masters = old_book
    sw = sweep_optimize(so_lines, _CONF, masters, budget_evals=40, seed=1)
    assert sw.result.ranks and "makespan_days" in sw.result.best

    # Replaying the ranks through the app's normal path reproduces the reported metrics.
    pr = PlanRun(so_lines=so_lines)
    pipeline.run_forward(pr, _CONF, masters, priority_rank=sw.result.ranks)
    replayed = optimizer.plan_metrics(pr.schedule, so_lines, _CONF.plan_start_date)
    assert replayed["makespan_days"] == sw.result.best["makespan_days"]


def test_old_ui_builds_from_new_engine(old_book, new_masters):
    # The whole point: pipeline -> Gantt + Analytics build from the new engine's output.
    so_lines, masters = old_book
    pr = PlanRun(so_lines=so_lines)
    pipeline.run_forward(pr, _CONF, masters)
    assert pr.schedule  # ScheduleEntry list produced

    g = gantt.build_gantt(pr.schedule, pr.batches, masters, {})
    assert g["rows"] and g["num_days"] > 0

    a = analytics.build_analytics(pr.schedule, masters, _CONF, pr.batches, [])
    assert a["machines"] and a["headline"]


def test_unrouted_order_is_skipped_not_crashing(old_book, new_masters):
    """Regression: an order whose item has no routing must be SKIPPED (it still shows in the
    book/report), never crash the plan. The classic engine tolerated this; the new engine
    once raised KeyError, which would have blanked the whole schedule."""
    from engine.models import SOLine
    so_lines, masters = old_book
    bad = SOLine(so_no="X9", item_code="NO_SUCH_ITEM", item_name="?", qty=5,
                 delivery_date=date(2025, 5, 1))
    pr = PlanRun(so_lines=list(so_lines) + [bad])
    pipeline.run_forward(pr, _CONF, masters)  # must not raise
    assert "NO_SUCH_ITEM" not in {e.item_code for e in pr.schedule}
    assert pr.schedule  # the routed orders still schedule


def test_operator_absence_is_honoured(old_book, new_masters):
    """The app's operator-absence feature must apply under the new engine: an absent
    operator is never assigned (regression — it was silently ignored)."""
    from engine import optimize_service
    so_lines, masters = old_book
    if not masters.operators:
        pytest.skip("no operators in sample")
    op = masters.operators[0].name
    reserved = optimize_service.absence_reservations(
        [{"operator": op, "from_date": "2025-03-01", "to_date": "2025-12-31"}])
    pr = PlanRun(so_lines=so_lines)
    pipeline.run_forward(pr, _CONF, masters, reserved=reserved)
    assert all(e.operator != op for e in pr.schedule)
