from datetime import date, datetime
from engine.config import Config
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters
from engine.rules import rule6_allocate


def _masters():
    m = Masters(machines={"M": Machine("M", "M", "mill")}, calendar=WorkCalendar())
    m.routings["X"] = Routing("X", "", "", "", None, processes=[
        Process(1, "op", cycle_time=10, total_time=None, suggested_machine="M",
                allotted_machine=None)])
    return m


def _batch():
    return Batch("B1", "X", "X", 1, date(2026, 3, 7), source_so_refs=["SO"])


def test_reserved_none_is_unchanged():
    cfg = Config(plan_start_date=date(2025, 3, 5))
    base = rule6_allocate.run([_batch()], config=cfg, masters=_masters())
    same = rule6_allocate.run([_batch()], config=cfg, masters=_masters(), reserved=None)
    assert [(e.machine, e.start, e.end) for e in base] == \
           [(e.machine, e.start, e.end) for e in same]


def test_op_is_pushed_past_a_reserved_block_on_its_machine():
    cfg = Config(plan_start_date=date(2025, 3, 5))          # Wed 08:00 start
    # Reserve machine M for the first 2 hours; the 10-min op must start at/after 10:00.
    reserved = {"M": [(datetime(2025, 3, 5, 8, 0), datetime(2025, 3, 5, 10, 0))]}
    sched = rule6_allocate.run([_batch()], config=cfg, masters=_masters(), reserved=reserved)
    op = sched[0]
    assert op.start >= datetime(2025, 3, 5, 10, 0)
    assert op.end <= datetime(2025, 3, 5, 10, 30)          # ran right after the block
