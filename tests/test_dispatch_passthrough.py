"""DISPATCH / OS pass-through + alternatives-everywhere.

A step with NO machine and NO cycle time (DISPATCH, an outside-service OS step) is
passed over — no machine, no operator, no time ("consider it done"). A blank-machine
step that DOES have a cycle time is NOT passed over (missing data must fail loud).
Alternative-machine '/' logic + parallel split apply to ANY step (e.g. inspection),
not just CNC."""
from datetime import date

from engine.config import Config
from engine.models import (Batch, Process, Routing, Machine, WorkCalendar, Masters, Operator)
from engine.rules import rule6_allocate


def _masters(processes, machines=("M",), operators=None):
    ms = {m: Machine(m, m, "CNC lathe", available_hrs_per_day=19.5) for m in machines}
    masters = Masters(machines=ms, calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="", customer="",
                                    rm_type="", moq=None, processes=processes)
    if operators is not None:
        masters.operators = operators
    return masters


def _batch(qty=10):
    return Batch(batch_id="B", item_code="X", item_name="x", qty=qty,
                 so_delivery_date=date(2025, 3, 20), source_so_refs=["B"])


def _cfg(**kw):
    return Config(plan_start_date=date(2025, 3, 5), **kw)


def test_dispatch_step_is_passed_over():
    procs = [Process(1, "OP", 10, 10, "M", None),
             Process(2, "DISPATCH", None, None, None, None)]   # blank machine, no time
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=_masters(procs))
    seqs = {e.process_seq for e in sched}
    assert 1 in seqs and 2 not in seqs                # OP scheduled, DISPATCH skipped


def test_leading_os_step_is_passed_over():
    procs = [Process(1, "BANDSAW OS", None, None, None, None),  # outsourced, no time
             Process(2, "OP", 10, 10, "M", None)]
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=_masters(procs))
    seqs = {e.process_seq for e in sched}
    assert 1 not in seqs and 2 in seqs                # OS skipped, real op runs


def test_dispatch_does_not_block_under_operator_logic():
    ops = [Operator("op", "M", machines=["M"], shift="First shift"),
           Operator("op2", "M", machines=["M"], shift="Second shift")]
    procs = [Process(1, "OP", 10, 10, "M", None),
             Process(2, "DISPATCH", None, None, None, None)]
    sched = rule6_allocate.run([_batch()],
                               config=_cfg(apply_operator_logic=True),
                               masters=_masters(procs, operators=ops))
    assert any(e.process_seq == 1 for e in sched)      # planned, not blocked


def test_blank_machine_with_cycle_time_is_NOT_passed_over():
    # Missing-data guard: a blank machine but a real cycle time must still appear
    # (here it falls back to a process-named station) — not silently dropped.
    procs = [Process(1, "MYSTERY", 30, 30, None, None)]   # blank machine, HAS time
    sched = rule6_allocate.run([_batch()], config=_cfg(), masters=_masters(procs))
    assert any(e.process_seq == 1 for e in sched)


def test_inspection_alternatives_split_in_parallel():
    # Alternatives + split are generic — inspection MI1/MI2/MI3 behaves like CNC.
    machines = {m: Machine(m, m, "Manual Inspection", available_hrs_per_day=9.5)
                for m in ("MI1", "MI2", "MI3")}
    masters = Masters(machines=machines, calendar=WorkCalendar())
    masters.routings["X"] = Routing(item_code="X", description="", customer="",
                                    rm_type="", moq=None,
                                    processes=[Process(1, "INSPECTION", 10, 10, "MI1/MI2/MI3", None)])
    masters.operators = [Operator(m, m, machines=[m], shift="First shift") for m in ("MI1", "MI2", "MI3")]
    sched = rule6_allocate.run([_batch(60)],
                               config=_cfg(apply_operator_logic=True, split_parallel=True),
                               masters=masters)
    used = {e.machine for e in sched}
    assert len(sched) >= 2                              # split happened
    assert used <= {"MI1", "MI2", "MI3"} and len(used) >= 2
