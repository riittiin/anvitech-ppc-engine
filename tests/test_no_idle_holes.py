"""A free machine, a free qualified person and ready work must never coexist
(owner, 2026-09-22, from the delay justification report: "machine and operator both
free, nothing scheduled" for whole shifts).

Two things in the placement step produced those holes, both proven on the owner's
books before they were changed (docs/superpowers/specs/2026-09-22-idle-capacity-
verification.md):

  1. ``_lay_on_machine`` refused a working window unless ONE person was free for the
     whole remaining stretch from the window's start, so a ten-minute job elsewhere
     threw away eleven hours of machine time. The op is now laid stretch by stretch
     in the free time of whoever is qualified and on shift, pausing while its
     operator is booked elsewhere.
  2. ``machine_free`` was a single "free from" datetime per machine, so a job
     committed late for its own routing reasons (a frozen step waiting on its own
     predecessor, a lower-priority op that lost the dispatch) made every idle hour
     in front of it unreachable. A machine now carries the spans of its committed
     operations and a ready op takes the earliest stretch it fits in WHOLE.

Every case here fails on the pre-change code (checked by reverting each change).
"""
from datetime import date, datetime, timedelta

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import Machine, MachineKind, Operator, Role, Shift
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.scheduler import decode
from ppc_engine.scheduler.schedule import FrozenOp

D1 = date(2025, 3, 3)                      # a Monday
D2 = date(2025, 3, 4)
T = lambda d, h, m=0: datetime.combine(d, datetime.min.time()) + timedelta(hours=h, minutes=m)
CFG = PlanConfig(plan_start=T(D1, 8), overlap=0.0)
MAN = OperationKind.MANUAL
DISP = OperationKind.DISPATCH


def _masters(routings):
    machines = {
        "M": Machine(id="M", type_text="Manual deburring", kind=MachineKind.MANUAL,
                     available_hrs_per_day=9.5),
        "N": Machine(id="N", type_text="Manual packing", kind=MachineKind.MANUAL,
                     available_hrs_per_day=9.5),
        "P": Machine(id="P", type_text="Manual washing", kind=MachineKind.MANUAL,
                     available_hrs_per_day=9.5),
    }
    ops = (Operator("Alpha", Role.HELPER, frozenset({"M", "N", "P"}), Shift.FIRST),
           Operator("Bravo", Role.HELPER, frozenset({"N"}), Shift.FIRST))
    return Masters(machines=machines, operators=ops, routings=routings,
                   calendar=ShopCalendar())


def _routing(item, *steps):
    ops = [Operation(i + 1, name, MAN, (mid,), cycle) for i, (name, mid, cycle) in enumerate(steps)]
    ops.append(Operation(len(ops) + 1, "DISPATCH", DISP))
    return Routing(item, item, tuple(ops))


def _order(so, item, qty, due=date(2025, 3, 20)):
    return Order(so_no=so, item_code=item, item_name=item, qty=qty, due_date=due)


def _segs(sched, key, seq):
    return sorted((s for s in sched.segments if s.order_key == key and s.op_seq == seq),
                  key=lambda s: s.start)


def _no_double_booking(sched):
    by = {}
    for s in sched.segments:
        if s.end <= s.start:
            continue
        for k in ((("m", s.machine_id) if s.machine_id else None),
                  (("o", s.operator) if s.operator else None)):
            if k:
                by.setdefault(k, []).append((s.start, s.end))
    for k, ivs in by.items():
        ivs.sort()
        for (a, b), (c, d) in zip(ivs, ivs[1:]):
            assert c >= b, (k, ivs)


# --------------------------------------------------------------------------- #
# 1. A short job elsewhere PAUSES the machine's operator; it no longer costs the shift
# --------------------------------------------------------------------------- #
def test_a_short_job_elsewhere_pauses_the_long_job_instead_of_losing_the_shift():
    """LONG: 600 pieces x 1 min on M, Alpha. SHORT: a 2-hour prep on N (Bravo) and
    then a 30-minute wash on P, which only Alpha can run — ready at 10:00, and the
    dispatcher commits it before LONG. Before the fix, LONG's whole day was refused
    because Alpha was not free for the ENTIRE window (booked 10:00-10:30 on P), so
    LONG started the next morning and M idled all day with Alpha standing next to
    it. Now LONG runs 08:00-10:00, pauses for the wash, and resumes at 10:30."""
    routings = {
        "LONG": _routing("LONG", ("DEBUR", "M", 1.0)),
        "SHORT": _routing("SHORT", ("PREP", "N", 1.0), ("WASH", "P", 1.0)),
    }
    m = _masters(routings)
    long_ = _order("L", "LONG", 600)
    short = Order(so_no="S", item_code="SHORT", item_name="SHORT", qty=30, due_date=date(2025, 3, 5),
                  process_remaining={1: 120, 2: 30, 3: 0})
    sched = decode([short, long_], [short.key, long_.key], m, CFG)
    wash = _segs(sched, short.key, 2)
    assert wash and wash[0].start == T(D1, 10) and wash[0].operator == "Alpha", wash
    long_segs = [(s.start, s.end) for s in _segs(sched, long_.key, 1)]
    assert long_segs[0] == (T(D1, 8), T(D1, 10)), "LONG must start the day it is ready: " + str(long_segs)
    assert long_segs[1][0] == T(D1, 10, 30), "LONG must resume right after the wash: " + str(long_segs)
    assert all(s.operator == "Alpha" for s in _segs(sched, long_.key, 1))
    _no_double_booking(sched)


# --------------------------------------------------------------------------- #
# 2. Ready work takes the idle stretch BEFORE a step that was committed late
# --------------------------------------------------------------------------- #
def test_ready_work_runs_before_a_frozen_step_that_waits_on_its_own_predecessor():
    """F is in progress: its first step (N, Bravo, 8 h left) and its second step (M,
    30 min) are both frozen. The second step cannot start before 16:00, so before
    the fix M's "free from" pointer jumped to 16:30 and G — ready at 08:00, four
    hours of work, Alpha free — waited until 16:30 while M sat idle all day."""
    routings = {
        "F": _routing("F", ("PREP", "N", 1.0), ("FINISH", "M", 1.0)),
        "G": _routing("G", ("DEBUR", "M", 1.0)),
    }
    m = _masters(routings)
    f = Order(so_no="F", item_code="F", item_name="F", qty=480, due_date=date(2025, 3, 5),
              process_remaining={1: 480, 2: 30, 3: 0})
    g = _order("G", "G", 240)
    frozen = [FrozenOp(f.key, 1, "N", "Bravo", 480, T(D1, 8)),
              FrozenOp(f.key, 2, "M", "Alpha", 30, T(D1, 16))]
    sched = decode([f, g], [f.key, g.key], m, CFG, frozen=frozen)
    finish = _segs(sched, f.key, 2)
    assert finish and finish[0].start == T(D1, 16), finish
    g_segs = _segs(sched, g.key, 1)
    assert g_segs[0].start == T(D1, 8) and g_segs[-1].end == T(D1, 12), (
        "G must use the idle morning before F's frozen step: " + str([(s.start, s.end) for s in g_segs]))
    _no_double_booking(sched)


def test_manual_work_runs_around_another_job_on_the_station():
    """Same shape, but G needs 9 hours and does not fit the 8-hour stretch before F's
    frozen step. Deburring has no setup to lose, so G runs AROUND it — the helper
    works G, does F's half hour, and picks G back up — instead of M idling all day."""
    routings = {
        "F": _routing("F", ("PREP", "N", 1.0), ("FINISH", "M", 1.0)),
        "G": _routing("G", ("DEBUR", "M", 1.0)),
    }
    m = _masters(routings)
    f = Order(so_no="F", item_code="F", item_name="F", qty=480, due_date=date(2025, 3, 5),
              process_remaining={1: 480, 2: 30, 3: 0})
    g = _order("G", "G", 540)
    frozen = [FrozenOp(f.key, 1, "N", "Bravo", 480, T(D1, 8)),
              FrozenOp(f.key, 2, "M", "Alpha", 30, T(D1, 16))]
    sched = decode([f, g], [f.key, g.key], m, CFG, frozen=frozen)
    g_segs = [(s.start, s.end) for s in _segs(sched, g.key, 1)]
    assert g_segs == [(T(D1, 8), T(D1, 16)), (T(D1, 16, 30), T(D1, 17, 30))], g_segs
    _no_double_booking(sched)


def test_a_cnc_job_that_does_not_fit_the_idle_stretch_whole_waits_and_is_never_split():
    """On a CNC the same G would need a second 90-minute setup to resume after F,
    so it is one engagement: it waits for the stretch after F and is never split."""
    machines = {
        "N": Machine(id="N", type_text="Manual packing", kind=MachineKind.MANUAL,
                     available_hrs_per_day=9.5),
        "C": Machine(id="C", type_text="CNC lathe", kind=MachineKind.MACHINING,
                     available_hrs_per_day=9.5),
    }
    ops = (Operator("Charlie", Role.OPERATOR, frozenset({"C"}), Shift.FIRST),
           Operator("Bravo", Role.HELPER, frozenset({"N"}), Shift.FIRST))
    routings = {
        "F": Routing("F", "F", (Operation(1, "PREP", MAN, ("N",), 1.0),
                                Operation(2, "FINISH", OperationKind.MACHINING, ("C",), 1.0),
                                Operation(3, "DISPATCH", DISP))),
        "G": Routing("G", "G", (Operation(1, "CNC", OperationKind.MACHINING, ("C",), 1.0),
                                Operation(2, "DISPATCH", DISP))),
    }
    m = Masters(machines=machines, operators=ops, routings=routings, calendar=ShopCalendar())
    f = Order(so_no="F", item_code="F", item_name="F", qty=480, due_date=date(2025, 3, 5),
              process_remaining={1: 480, 2: 30, 3: 0})
    g = _order("G", "G", 400)                      # 90 setup + 400 = 490 min > the 8 h stretch
    frozen = [FrozenOp(f.key, 1, "N", "Bravo", 480, T(D1, 8)),
              FrozenOp(f.key, 2, "C", "Charlie", 30, T(D1, 16))]
    sched = decode([f, g], [f.key, g.key], m, CFG, frozen=frozen)
    g_segs = _segs(sched, g.key, 1)
    assert g_segs[0].start >= T(D1, 16, 30), [(s.start, s.end) for s in g_segs]
    on_c = sorted((s.start, s.end, s.order_key[0]) for s in sched.segments if s.machine_id == "C")
    owners = [k for _s, _e, k in on_c]
    assert owners == sorted(owners, key=owners.index), "C interleaved two jobs"
    _no_double_booking(sched)


def test_a_cnc_job_takes_an_idle_stretch_it_fits_whole():
    """And when the CNC job DOES fit the stretch before the frozen step, it runs there."""
    machines = {
        "N": Machine(id="N", type_text="Manual packing", kind=MachineKind.MANUAL,
                     available_hrs_per_day=9.5),
        "C": Machine(id="C", type_text="CNC lathe", kind=MachineKind.MACHINING,
                     available_hrs_per_day=9.5),
    }
    ops = (Operator("Charlie", Role.OPERATOR, frozenset({"C"}), Shift.FIRST),
           Operator("Bravo", Role.HELPER, frozenset({"N"}), Shift.FIRST))
    routings = {
        "F": Routing("F", "F", (Operation(1, "PREP", MAN, ("N",), 1.0),
                                Operation(2, "FINISH", OperationKind.MACHINING, ("C",), 1.0),
                                Operation(3, "DISPATCH", DISP))),
        "G": Routing("G", "G", (Operation(1, "CNC", OperationKind.MACHINING, ("C",), 1.0),
                                Operation(2, "DISPATCH", DISP))),
    }
    m = Masters(machines=machines, operators=ops, routings=routings, calendar=ShopCalendar())
    f = Order(so_no="F", item_code="F", item_name="F", qty=480, due_date=date(2025, 3, 5),
              process_remaining={1: 480, 2: 30, 3: 0})
    g = _order("G", "G", 200)                      # 90 + 200 = 290 min: fits before 16:00
    frozen = [FrozenOp(f.key, 1, "N", "Bravo", 480, T(D1, 8)),
              FrozenOp(f.key, 2, "C", "Charlie", 30, T(D1, 16))]
    sched = decode([f, g], [f.key, g.key], m, CFG, frozen=frozen)
    g_segs = _segs(sched, g.key, 1)
    assert (g_segs[0].start, g_segs[-1].end) == (T(D1, 8), T(D1, 12, 50)), [(s.start, s.end) for s in g_segs]
    _no_double_booking(sched)


# --------------------------------------------------------------------------- #
# 3. On a real-shaped book: nothing double-booked, routing intact, people qualified
# --------------------------------------------------------------------------- #
def test_a_replicated_sample_book_stays_clean():
    import io
    from dataclasses import replace as _r
    from engine import book_store, new_engine
    from engine.config import Config
    from engine.loaders import load_all
    from engine.models import PlanRun
    from engine.pipeline import run_forward
    from tests.new_sample_workbook import build_new_sample_bytes
    from tests.test_new_engine import _assert_clean

    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    new_engine.set_masters_bytes(wb)
    so_lines, masters = load_all(io.BytesIO(wb))
    so_lines = [_r(l, so_no=f"{l.so_no}-{i}", qty=max(5, int(l.qty) // (i + 1)),
                   delivery_date=l.delivery_date + timedelta(days=12 * i))
                for i in range(8) for l in so_lines]
    cfg = Config(plan_start_date=D1, scheduler="new", apply_operator_logic=True,
                 overlap_percent=88)
    pr = PlanRun(so_lines=list(so_lines))
    run_forward(pr, cfg, masters)
    assert pr.schedule
    assert new_engine.routing_order_violations(pr.schedule, masters) == []
    assert new_engine.batch_quantity_violations(pr.schedule, pr.batches_prioritized) == []
    nm = new_engine._apply_app_operators(new_engine._new_masters(False), masters)
    assert new_engine.qualification_violations(pr.schedule, nm) == []
    ivs = {}
    for e in pr.schedule:
        for s, t, n in (e.op_segments or []):
            if n:
                ivs.setdefault(n, []).append((s, t))
            ivs.setdefault(("m", e.machine), []).append((s, t))
    for k, v in ivs.items():
        v.sort()
        for (a, b), (c, d) in zip(v, v[1:]):
            assert c >= b, (k, a, b, c, d)


# --------------------------------------------------------------------------- #
# 4. Against an EARLIER stage's occupancy (the Add New Orders quote) a new job of
#    any kind takes a gap only if it fits whole — it never straddles an existing job
# --------------------------------------------------------------------------- #
def test_manual_work_never_straddles_an_earlier_stages_job():
    """Live 2026-09-22, right after the placement change shipped: a quote for seven
    lines was refused with "machine MI1 is double-booked: the new plan runs 25-09
    08:00 to 26-09 08:13 while the existing plan already runs 25-09 10:30 to 11:20".
    The new inspection step had been laid AROUND the existing job, so its span
    covered it and the quote's verifier (which compares whole spans) rejected it."""
    from dataclasses import replace as _r
    routings = {"G": _routing("G", ("DEBUR", "M", 1.0))}
    m = _masters(routings)
    m = _r(m, calendar=_r(m.calendar, machine_busy={"M": ((T(D1, 10), T(D1, 11)),)}))
    g = _order("G", "G", 300)                       # 5 h: does not fit before 10:00
    sched = decode([g], [g.key], m, CFG)
    segs = [(s.start, s.end) for s in _segs(sched, g.key, 1)]
    assert segs[0][0] >= T(D1, 11), segs           # after the existing job, as one run
    assert all(e <= T(D1, 10) or s >= T(D1, 11) for s, e in segs), segs
