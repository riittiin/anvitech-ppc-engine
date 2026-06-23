"""Rule 6 — allocation: machine choice, occupancy, sequencing, machine sharing."""
from datetime import date, datetime

from engine.config import Config, OVERLAP_PERCENT
from engine.models import (
    Batch, Process, Routing, Machine, WorkCalendar, Masters,
)
from engine.rules import rule6_allocate


def _batch(masters, item_code, qty=10):
    return Batch(batch_id="B1", item_code=item_code, item_name=item_code, qty=qty,
                 so_delivery_date=date(2025, 3, 7), source_so_refs=["SO"])


def test_first_process_starts_at_plan_start(loaded):
    _, masters = loaded
    cfg = Config(plan_start_date=date(2025, 3, 5))  # Wednesday
    sched = rule6_allocate.run([_batch(masters, "61240807-01")], config=cfg, masters=masters)
    assert sched[0].start == datetime(2025, 3, 5, 8, 0)
    # 61240807-01 P1: cycle 40 x 10 + 90 = 490 min occupancy.
    assert sched[0].occupancy_min == 490


def test_processes_are_sequenced_within_batch(loaded):
    _, masters = loaded
    cfg = Config(plan_start_date=date(2025, 3, 5))
    sched = rule6_allocate.run([_batch(masters, "61240807-01")], config=cfg, masters=masters)
    for prev, nxt in zip(sched, sched[1:]):
        assert nxt.start >= prev.start  # later processes start no earlier


def test_overlap_starts_next_earlier_than_sequential(loaded):
    _, masters = loaded
    seq = rule6_allocate.run(
        [_batch(masters, "61240807-01")],
        config=Config(plan_start_date=date(2025, 3, 5)), masters=masters,
    )
    ov = rule6_allocate.run(
        [_batch(masters, "61240807-01")],
        config=Config(plan_start_date=date(2025, 3, 5), overlap_mode=OVERLAP_PERCENT, overlap_percent=50),
        masters=masters,
    )
    # The 2nd process should begin sooner under overlap than under sequential.
    assert ov[1].start <= seq[1].start


def _machine(mid):
    return Machine(machine_no=mid, display_name=mid, machine_type="t")


def _synthetic_masters():
    """Two machines (M, N) and two routings that contend for M.

    Batch A (priority 1): P1 on N, P2 on M — A's M-op is NOT ready until A's N-op
    finishes (at 09:40, the same moment B frees M).
    Batch B (priority 2): P1 on M — ready immediately at plan start.
    A non-delay scheduler must run B on M first (M would otherwise sit idle from
    plan start waiting for A's not-yet-ready M-op), then run A's M-op the instant
    it's ready — machine M never waits while work is available.
    """
    masters = Masters(
        machines={"M": _machine("M"), "N": _machine("N")},
        calendar=WorkCalendar(),
    )
    masters.routings["A"] = Routing(
        item_code="A", description="", customer="", rm_type="", moq=None,
        processes=[
            Process(1, "A-on-N", cycle_time=10, total_time=None, suggested_machine="N", allotted_machine=None),
            Process(2, "A-on-M", cycle_time=10, total_time=None, suggested_machine="M", allotted_machine=None),
        ],
    )
    masters.routings["B"] = Routing(
        item_code="B", description="", customer="", rm_type="", moq=None,
        processes=[
            Process(1, "short-on-M", cycle_time=10, total_time=None, suggested_machine="M", allotted_machine=None),
        ],
    )
    return masters


def test_machine_does_not_wait_when_lower_priority_op_is_ready():
    masters = _synthetic_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5))  # Wednesday, 08:00 start
    a = Batch(batch_id="A", item_code="A", item_name="A", qty=1,
              so_delivery_date=date(2025, 3, 7), source_so_refs=["A"])
    b = Batch(batch_id="B", item_code="B", item_name="B", qty=1,
              so_delivery_date=date(2025, 3, 8), source_so_refs=["B"])

    sched = rule6_allocate.run([a, b], config=cfg, masters=masters)  # A is higher priority

    m_ops = sorted([e for e in sched if e.machine == "M"], key=lambda e: e.start)
    b_on_m = next(e for e in m_ops if e.batch_id == "B")
    a_on_m = next(e for e in m_ops if e.batch_id == "A")

    # B fills machine M at plan start instead of M idling until A's op is ready.
    assert b_on_m.start == datetime(2025, 3, 5, 8, 0)
    assert b_on_m.start < a_on_m.start


def test_machine_view_reports_zero_idle_when_continuous():
    masters = _synthetic_masters()
    cfg = Config(plan_start_date=date(2025, 3, 5))
    a = Batch(batch_id="A", item_code="A", item_name="A", qty=1,
              so_delivery_date=date(2025, 3, 7), source_so_refs=["A"])
    b = Batch(batch_id="B", item_code="B", item_name="B", qty=1,
              so_delivery_date=date(2025, 3, 8), source_so_refs=["B"])
    sched = rule6_allocate.run([a, b], config=cfg, masters=masters)

    timeline, summary = rule6_allocate.build_machine_view(sched, masters, cfg)
    # M runs B then A back-to-back -> the gap before A's op on M is ~0.
    m_rows = [r for r in timeline if r["Machine"] == "M"]
    a_row = next(r for r in m_rows if r["Batch"] == "A")
    assert a_row["Idle before (min)"] < 1
    m_summary = next(r for r in summary if r["Machine"] == "M")
    assert m_summary["Utilization %"] >= 99.0


def test_overlap_excludes_setup_and_skips_no_cutting_steps():
    """Under overlap: a machining step's successor starts after % of its CUTTING
    only (setup excluded); a no-cutting finishing step does NOT overlap — its
    successor waits for it to fully complete."""
    masters = Masters(
        machines={"M": _machine("M"), "N": _machine("N"), "O": _machine("O")},
        calendar=WorkCalendar(),
    )
    masters.routings["X"] = Routing(
        item_code="X", description="", customer="", rm_type="", moq=None,
        processes=[
            Process(1, "machine", cycle_time=10, total_time=None, suggested_machine="M", allotted_machine=None),
            Process(2, "deburr", cycle_time=None, total_time=None, suggested_machine="N", allotted_machine=None),
            Process(3, "inspect", cycle_time=None, total_time=None, suggested_machine="O", allotted_machine=None),
        ],
    )
    cfg = Config(plan_start_date=date(2025, 3, 5),
                 overlap_mode=OVERLAP_PERCENT, overlap_percent=50)
    batch = Batch(batch_id="X1", item_code="X", item_name="X", qty=10,
                  so_delivery_date=date(2025, 3, 7), source_so_refs=["SO"])
    sched = rule6_allocate.run([batch], config=cfg, masters=masters)
    p1 = next(e for e in sched if e.process_seq == 1)  # machining (cutting 100 + setup 90)
    p2 = next(e for e in sched if e.process_seq == 2)  # finishing (no cutting)
    p3 = next(e for e in sched if e.process_seq == 3)  # finishing (no cutting)

    # P2 (after a machining step) overlaps: starts before P1 fully finishes.
    assert p2.start < p1.end
    # P3 (after a no-cutting step) does NOT overlap: starts when P2 fully ends.
    assert p3.start == p2.end


def test_no_routing_raises_rule_error(loaded):
    _, masters = loaded
    from engine.pipeline import RuleError
    bad = Batch(batch_id="BX", item_code="DOES-NOT-EXIST", item_name="x", qty=1,
                so_delivery_date=date(2025, 3, 7), source_so_refs=["SO"])
    try:
        rule6_allocate.run([bad], config=Config(), masters=masters)
        assert False, "expected RuleError"
    except RuleError as e:
        assert e.rule == "rule6"
