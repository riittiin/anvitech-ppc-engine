"""Fixed plan (2026-10-06 spec): a published op stays on its machine and in the published
order; a late job lets the next ready job on its machine go first (spec section 8)."""
from datetime import date, datetime, timedelta

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import Machine, MachineKind, Operator, Role, Shift
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.scheduler import decode
from ppc_engine.scheduler.schedule import FrozenOp, PinnedOp

D1 = date(2025, 3, 3)                      # a Monday
T = lambda d, h, m=0: datetime.combine(d, datetime.min.time()) + timedelta(hours=h, minutes=m)
CFG = PlanConfig(plan_start=T(D1, 8), overlap=0.0)
MAN = OperationKind.MANUAL
DISP = OperationKind.DISPATCH


def _masters(routings):
    machines = {mid: Machine(id=mid, type_text="Manual", kind=MachineKind.MANUAL,
                             available_hrs_per_day=9.5) for mid in ("X", "Y", "Z")}
    ops = (Operator("Alpha", Role.HELPER, frozenset({"X"}), Shift.FIRST),
           Operator("Bravo", Role.HELPER, frozenset({"Y"}), Shift.FIRST),
           Operator("Carol", Role.HELPER, frozenset({"Z"}), Shift.FIRST))
    return Masters(machines=machines, operators=ops, routings=routings,
                   calendar=ShopCalendar())


def _routing(item, *steps):
    """steps: (name, machine_options tuple, cycle_min)"""
    ops = [Operation(i + 1, n, MAN, opts, c) for i, (n, opts, c) in enumerate(steps)]
    ops.append(Operation(len(ops) + 1, "DISPATCH", DISP))
    return Routing(item, item, tuple(ops))


def _order(so, item, qty, due=date(2025, 3, 20)):
    return Order(so_no=so, item_code=item, item_name=item, qty=qty, due_date=due)


def _ops(sched, key, seq):
    return sorted((s for s in sched.segments if s.order_key == key and s.op_seq == seq),
                  key=lambda s: s.start)


def test_no_pins_is_byte_identical():
    r = {"A": _routing("A", ("S1", ("X", "Y"), 1.0)), "B": _routing("B", ("S1", ("X", "Y"), 1.0))}
    m = _masters(r)
    a, b = _order("A1", "A", 60), _order("B1", "B", 60)
    assert decode([a, b], [a.key, b.key], m, CFG) == \
        decode([a, b], [a.key, b.key], m, CFG, pins=None) == \
        decode([a, b], [a.key, b.key], m, CFG, pins=[])


def test_pinned_op_stays_on_its_machine_even_when_another_is_free_sooner():
    """X is busy with A all morning; Y is idle. Free dispatch puts B on Y.
    B is published on X, so it must wait for X."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0)), "B": _routing("B", ("S1", ("X", "Y"), 1.0))}
    m = _masters(r)
    a, b = _order("A1", "A", 120), _order("B1", "B", 60)
    free = decode([a, b], [a.key, b.key], m, CFG)
    assert _ops(free, b.key, 1)[0].machine_id == "Y"
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8)),
            PinnedOp(b.key, 1, "X", "Alpha", T(D1, 10))]
    s = decode([a, b], [a.key, b.key], m, CFG, pins=pins)
    assert {seg.machine_id for seg in _ops(s, b.key, 1)} == {"X"}
    assert _ops(s, b.key, 1)[0].start >= _ops(s, a.key, 1)[-1].end


def test_next_ready_job_goes_first_on_the_same_machine():
    """Owner's example (spec section 8, D6 amended; was the strict-turn test). Z's
    published queue is A then B. A was due on Z at 08:00 but its step on X now runs
    until 11:00; B is ready at 08:00. B runs first on Z, A runs on Z after it. Both
    stay on Z."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0)),
         "B": _routing("B", ("S1", ("Z",), 1.0))}
    m = _masters(r)
    a, b = _order("A1", "A", 180), _order("B1", "B", 300)
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 5)),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 8)),
            PinnedOp(b.key, 1, "Z", "Carol", T(D1, 11))]
    s = decode([a, b], [a.key, b.key], m, CFG, pins=pins)
    a2, b1 = _ops(s, a.key, 2), _ops(s, b.key, 1)
    assert b1[0].start == T(D1, 8), (a2, b1)
    assert a2[0].start >= b1[-1].end, (a2, b1)
    assert {g.machine_id for g in a2 + b1} == {"Z"}


def test_a_job_on_time_keeps_its_turn():
    """The published plan held Z for A, due at 11:00 when its step on X ends, and put
    the long job B after it. A is on time, so A keeps its turn: it starts at 11:00 and
    B never delays it (the published plan chose that; a repair must reproduce it).
    B may use the hours Z would stand idle before A, since a manual station is laid
    around other jobs (2026-09-22), but it cannot take A's slot."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0)),
         "B": _routing("B", ("S1", ("Z",), 1.0))}
    m = _masters(r)
    a, b = _order("A1", "A", 180), _order("B1", "B", 300)
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8)),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 11)),
            PinnedOp(b.key, 1, "Z", "Carol", T(D1, 14))]
    s = decode([a, b], [b.key, a.key], m, CFG, pins=pins)   # sequence favours B
    a2, b1 = _ops(s, a.key, 2), _ops(s, b.key, 1)
    assert (a2[0].start, a2[-1].end) == (T(D1, 11), T(D1, 14)), a2
    assert all(g.end <= T(D1, 11) or g.start >= T(D1, 14) for g in b1), b1


def test_a_published_start_stored_to_the_second_is_still_on_time():
    """The published plan stores times to the whole second (``schedule_projection``),
    while the engine works in fractions of a minute. A's step on X ends at 11:00:10.8
    and A was published on Z at 11:00:10: A is on time, it keeps its turn. (Measured
    on the live copy: without a tolerance 226 such sub-second "late" jobs were jumped
    in a repair of a plan nobody had punched against, and it no longer reproduced.)"""
    r = {"A": _routing("A", ("S1", ("X",), 1.001), ("S2", ("Z",), 1.0)),
         "B": _routing("B", ("S1", ("Z",), 1.0))}
    m = _masters(r)
    a, b = _order("A1", "A", 180), _order("B1", "B", 300)
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8)),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 11) + timedelta(seconds=10)),
            PinnedOp(b.key, 1, "Z", "Carol", T(D1, 14))]
    s = decode([a, b], [b.key, a.key], m, CFG, pins=pins)
    a2 = _ops(s, a.key, 2)
    assert a2[0].start == T(D1, 11) + timedelta(seconds=10.8), a2


def test_a_job_that_only_looks_ready_early_does_not_jump_a_late_one():
    """Found on the live copy (variant A). A is late on Z (published 08:00, its step on
    X releases it at 10:30). B's step on Z looks startable at 10:00 (overlap), but the
    piece-flow guard pushes it to 11:30, after A is ready. "Can start before A is
    ready" must be judged on where B really lands, or B takes the slot in front of a
    ready A and splits it."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 0.6)),
         "B": _routing("B", ("S1", ("Y",), 2.0), ("S2", ("Z",), 0.25))}
    m = _masters(r)
    cfg = PlanConfig(plan_start=T(D1, 8), overlap=0.5)
    a, b = _order("A1", "A", 300), _order("B1", "B", 120)
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8), rank=0),
            PinnedOp(b.key, 1, "Y", "Bravo", T(D1, 8), rank=1),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 8), rank=2),
            PinnedOp(b.key, 2, "Z", "Carol", T(D1, 13), rank=3)]
    s = decode([a, b], [a.key, b.key], m, cfg, pins=pins)
    a2, b2 = _ops(s, a.key, 2), _ops(s, b.key, 2)
    assert (a2[0].start, a2[-1].end) == (T(D1, 10, 30), T(D1, 13, 30)), (a2, b2)
    assert b2[0].start >= a2[-1].end, (a2, b2)


def _masters4(routings):
    machines = {mid: Machine(id=mid, type_text="Manual", kind=MachineKind.MANUAL,
                             available_hrs_per_day=9.5) for mid in ("X", "Y", "Z", "W")}
    ops = tuple(Operator(n, Role.HELPER, frozenset({mid}), Shift.FIRST)
                for n, mid in (("Alpha", "X"), ("Bravo", "Y"), ("Carol", "Z"), ("Dave", "W")))
    return Masters(machines=machines, operators=ops, routings=routings,
                   calendar=ShopCalendar())


def test_a_late_job_never_reorders_another_machines_queue():
    """Fix round I1 (reviewer's probe). A is late on Z. On W, E (on time, ready 12:00)
    is ahead of D (ready 08:00) in the published order. The give-way applies on the
    late job's OWN machine only (spec section 8: "on the same machine"), so W runs
    exactly as it would if A were on time."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0)),
         "E": _routing("E", ("S1", ("Y",), 1.0), ("S2", ("W",), 1.0)),
         "D": _routing("D", ("S1", ("W",), 1.0))}
    m = _masters4(r)
    a, e, d = _order("A1", "A", 180), _order("E1", "E", 240), _order("D1", "D", 300)

    def w_segments(a2_published):
        pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8), rank=0),
                PinnedOp(e.key, 1, "Y", "Bravo", T(D1, 8), rank=1),
                PinnedOp(a.key, 2, "Z", "Carol", a2_published, rank=2),
                PinnedOp(e.key, 2, "W", "Dave", T(D1, 12), rank=3),
                PinnedOp(d.key, 1, "W", "Dave", T(D1, 13), rank=4)]
        s = decode([a, e, d], [a.key, e.key, d.key], m, CFG, pins=pins)
        return sorted((g.order_key, g.op_seq, g.start, g.end) for g in s.segments
                      if g.machine_id == "W")
    assert w_segments(T(D1, 8)) == w_segments(T(D1, 11))


def test_behind_a_late_job_the_job_that_can_start_first_goes_first():
    """Fix round I2 (owner: "if A's previous process is pending and B's processes are
    done, B should come in front of A"). Z: A is late (ready 11:00), B is on time but
    not ready until 10:00, C is ready at 08:00. C goes first at 08:00; Z is not left
    idle waiting for B."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0)),
         "B": _routing("B", ("S1", ("Y",), 1.0), ("S2", ("Z",), 1.0)),
         "C": _routing("C", ("S1", ("Z",), 1.0))}
    m = _masters(r)
    a, b, c = _order("A1", "A", 180), _order("B1", "B", 120), _order("C1", "C", 180)
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 5), rank=0),
            PinnedOp(b.key, 1, "Y", "Bravo", T(D1, 8), rank=1),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 8), rank=2),
            PinnedOp(b.key, 2, "Z", "Carol", T(D1, 10), rank=3),
            PinnedOp(c.key, 1, "Z", "Carol", T(D1, 12), rank=4)]
    s = decode([a, b, c], [a.key, b.key, c.key], m, CFG, pins=pins)
    c1 = _ops(s, c.key, 1)
    assert (c1[0].start, c1[-1].end) == (T(D1, 8), T(D1, 11)), c1


def test_behind_a_late_job_two_ready_jobs_go_in_published_order():
    """Z: A is late; B and C are both ready at 08:00. B is ahead of C in the published
    order, so B goes first even though the sequence favours C."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0)),
         "B": _routing("B", ("S1", ("Z",), 1.0)), "C": _routing("C", ("S1", ("Z",), 1.0))}
    m = _masters(r)
    a, b, c = _order("A1", "A", 180), _order("B1", "B", 60), _order("C1", "C", 60)
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 5), rank=0),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 8), rank=1),
            PinnedOp(b.key, 1, "Z", "Carol", T(D1, 11), rank=2),
            PinnedOp(c.key, 1, "Z", "Carol", T(D1, 12), rank=3)]
    s = decode([a, b, c], [c.key, a.key, b.key], m, CFG, pins=pins)
    assert _ops(s, b.key, 1)[0].start == T(D1, 8)
    assert _ops(s, c.key, 1)[0].start == T(D1, 9)


def test_behind_a_late_job_a_ready_job_is_not_jumped_by_one_that_starts_sooner():
    """Live copy, variant A, after fix round I2. K is late on Z. A and B are both ready
    at 10:30 (their previous steps release them then); A is ahead in the published
    order but its run cannot start until 12:30 (piece flow: it must not finish before
    its previous step). B could start at 10:30. Both are READY, so the published order
    wins: A runs at 12:30 and is not pushed back by B. (Earliest start only decides
    between a ready job and one that is not ready yet.)"""
    r = {"K": _routing("K", ("S1", ("W",), 1.0), ("S2", ("Z",), 0.1)),
         "A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 0.1)),
         "B": _routing("B", ("S1", ("Y",), 1.0), ("S2", ("Z",), 1.0))}
    m = _masters4(r)
    cfg = PlanConfig(plan_start=T(D1, 8), overlap=0.5)
    k, a, b = _order("K1", "K", 600), _order("A1", "A", 300), _order("B1", "B", 300)
    pins = [PinnedOp(k.key, 1, "W", "Dave", T(D1, 8), rank=0),
            PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8), rank=1),
            PinnedOp(b.key, 1, "Y", "Bravo", T(D1, 8), rank=2),
            PinnedOp(k.key, 2, "Z", "Carol", T(D1, 8), rank=3),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 12, 30), rank=4),
            PinnedOp(b.key, 2, "Z", "Carol", T(D1, 13), rank=5)]
    s = decode([k, a, b], [b.key, a.key, k.key], m, cfg, pins=pins)
    a2 = _ops(s, a.key, 2)
    assert a2[0].start == T(D1, 12, 30), (a2, _ops(s, b.key, 2))


def test_behind_a_late_job_the_next_ready_job_goes_even_if_it_starts_after_the_late_one_is_ready():
    """Live copy, variant A (CNC7). K is late on Z (ready 13:00). A is ready from 11:00
    and is next in the published order, but its run can only start at 13:24, so it
    ends with its previous step at 14:00 (piece flow). B is ready at 11:20 and could start then. When Z can next start a job
    (about 11:20), A and B are both ready, so A, the next READY job in the queue, goes first;
    B does not take the machine in front of it."""
    r = {"K": _routing("K", ("S1", ("W",), 1.0), ("S2", ("Z",), 0.1)),
         "A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 0.1)),
         "B": _routing("B", ("S1", ("Y",), 1.0), ("S2", ("Z",), 1.0))}
    m = _masters4(r)
    cfg = PlanConfig(plan_start=T(D1, 8), overlap=0.5)
    k, a, b = _order("K1", "K", 600), _order("A1", "A", 360), _order("B1", "B", 180)
    b = Order(so_no="B1", item_code="B", item_name="B", qty=400, due_date=date(2025, 3, 20),
              process_remaining={1: 400, 2: 180, 3: 400})
    pins = [PinnedOp(k.key, 1, "W", "Dave", T(D1, 8), rank=0),
            PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8), rank=1),
            PinnedOp(b.key, 1, "Y", "Bravo", T(D1, 8), rank=2),
            PinnedOp(k.key, 2, "Z", "Carol", T(D1, 8), rank=3),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 13, 30), rank=4),
            PinnedOp(b.key, 2, "Z", "Carol", T(D1, 14), rank=5)]
    s = decode([k, a, b], [b.key, a.key, k.key], m, cfg, pins=pins)
    a2 = _ops(s, a.key, 2)
    assert a2[-1].end == T(D1, 14), (a2, _ops(s, b.key, 2))      # as soon as A1 ends


def test_a_late_job_ready_before_the_next_ready_job_can_start_goes_first():
    """Fix round 2 (the re-reviewer's theoretical case, constructed). As above, but K2
    is a long run with no piece-flow wait: K is ready, and can start, at 13:00. A is
    picked behind K (ready first) but its run cannot start before 13:24; by then K is
    ready too and is earlier in the published order, so K goes first, in one piece,
    and A follows. Without this A, laid first, would take Z in front of K."""
    r = {"K": _routing("K", ("S1", ("W",), 1.0), ("S2", ("Z",), 0.5)),
         "A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 0.1)),
         "B": _routing("B", ("S1", ("Y",), 1.0), ("S2", ("Z",), 1.0))}
    m = _masters4(r)
    cfg = PlanConfig(plan_start=T(D1, 8), overlap=0.5)
    k, a, b = _order("K1", "K", 600), _order("A1", "A", 360), _order("B1", "B", 180)
    b = Order(so_no="B1", item_code="B", item_name="B", qty=400, due_date=date(2025, 3, 20),
              process_remaining={1: 400, 2: 180, 3: 400})
    pins = [PinnedOp(k.key, 1, "W", "Dave", T(D1, 8), rank=0),
            PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8), rank=1),
            PinnedOp(b.key, 1, "Y", "Bravo", T(D1, 8), rank=2),
            PinnedOp(k.key, 2, "Z", "Carol", T(D1, 8), rank=3),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 13, 30), rank=4),
            PinnedOp(b.key, 2, "Z", "Carol", T(D1, 14), rank=5)]
    s = decode([k, a, b], [b.key, a.key, k.key], m, cfg, pins=pins)
    a2, k2 = _ops(s, a.key, 2), _ops(s, k.key, 2)
    assert k2[0].start == T(D1, 13), (k2, a2)
    assert all(g.start >= k2[-1].end for g in a2), (k2, a2)


def test_upstream_work_elsewhere_runs_before_a_later_job_takes_the_machine():
    """Live copy, variant A (CNC7). K is late on Z. A is ahead of B on Z in the
    published order and becomes ready at 10:00, once its step on X (published before
    B's step on Z) has run. B is ready at 11:00. The repair must place A's step on X
    first (it is earlier in published order and happens before B could start), so
    that when Z next takes a job A is known to be ready and goes before B."""
    r = {"K": _routing("K", ("S1", ("W",), 1.0), ("S2", ("Z",), 1.0)),
         "A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0)),
         "B": _routing("B", ("S1", ("Y",), 1.0), ("S2", ("Z",), 1.0))}
    m = _masters4(r)
    k, a, b = _order("K1", "K", 300), _order("A1", "A", 120), _order("B1", "B", 180)
    b = Order(so_no="B1", item_code="B", item_name="B", qty=240, due_date=date(2025, 3, 20),
              process_remaining={1: 180, 2: 240, 3: 240})
    pins = [PinnedOp(k.key, 1, "W", "Dave", T(D1, 8), rank=0),
            PinnedOp(b.key, 1, "Y", "Bravo", T(D1, 8), rank=1),
            PinnedOp(k.key, 2, "Z", "Carol", T(D1, 8), rank=2),
            PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8), rank=3),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 10), rank=4),
            PinnedOp(b.key, 2, "Z", "Carol", T(D1, 12), rank=5)]
    s = decode([k, a, b], [b.key, a.key, k.key], m, CFG, pins=pins)
    a2 = _ops(s, a.key, 2)
    assert (a2[0].start, a2[-1].end) == (T(D1, 10), T(D1, 12)), (a2, _ops(s, b.key, 2))


def test_a_ready_job_is_not_jumped_by_one_whose_run_starts_after_it_is_ready():
    """Live copy, variant A (MW1, MPK1). K is late on Z. When Z can next start a job
    (09:00), B is ready and A is not (A is ready at 09:30). But B's run cannot really
    start before 09:30 either (piece flow): at that moment both are ready, and A is
    ahead of B in the published order, so A goes first."""
    machines = {mid: Machine(id=mid, type_text="Manual", kind=MachineKind.MANUAL,
                             available_hrs_per_day=9.5) for mid in ("V", "W", "X", "Y", "Z")}
    ops = tuple(Operator(n, Role.HELPER, frozenset({mid}), Shift.FIRST) for n, mid in
                (("Vic", "V"), ("Dave", "W"), ("Alpha", "X"), ("Bravo", "Y"), ("Carol", "Z")))
    r = {"K": _routing("K", ("S1", ("V",), 1.0), ("S2", ("Z",), 0.1)),
         "A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0)),
         "B": _routing("B", ("S1", ("Y",), 1.0), ("S2", ("Z",), 0.25)),
         "X": _routing("X", ("S1", ("W",), 1.0), ("S2", ("Z",), 1.0))}
    m = Masters(machines=machines, operators=ops, routings=r, calendar=ShopCalendar())
    cfg = PlanConfig(plan_start=T(D1, 8), overlap=0.5)
    k, a, b, x = (_order("K1", "K", 600), _order("A1", "A", 180), _order("B1", "B", 120),
                  _order("X1", "X", 120))
    pins = [PinnedOp(k.key, 1, "V", "Vic", T(D1, 8), rank=0),
            PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8), rank=1),
            PinnedOp(b.key, 1, "Y", "Bravo", T(D1, 8), rank=2),
            PinnedOp(x.key, 1, "W", "Dave", T(D1, 8), rank=3),
            PinnedOp(k.key, 2, "Z", "Carol", T(D1, 8), rank=4),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 9, 30), rank=5),
            PinnedOp(b.key, 2, "Z", "Carol", T(D1, 12), rank=6),
            PinnedOp(x.key, 2, "Z", "Carol", T(D1, 13), rank=7)]
    s = decode([k, a, b, x], [x.key, b.key, a.key, k.key], m, cfg, pins=pins)
    a2, b2 = _ops(s, a.key, 2), _ops(s, b.key, 2)
    assert a2[0].start == T(D1, 9, 30), (a2, b2)
    assert b2[0].start >= a2[-1].end, (a2, b2)


def test_upstream_work_first_never_reorders_another_machine():
    """Fix round 2 (re-reviewer's probe). K2 is late on Z; J waits behind it. On W, G
    (published 10:00, ready 10:00, on time) is ahead of C. Placing upstream work first
    is only for work that FEEDS a job queued on Z; it must not move C in front of G on
    W. W runs exactly as it would if K2 were on time."""
    r = {"K": _routing("K", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0)),
         "G": _routing("G", ("S1", ("Y",), 1.0), ("S2", ("W",), 1.0)),
         "C": _routing("C", ("S1", ("W",), 1.0)),
         "J": _routing("J", ("S1", ("Z",), 1.0))}
    m = _masters4(r)
    k, g, c, j = (_order("K1", "K", 180), _order("G1", "G", 120), _order("C1", "C", 180),
                  _order("J1", "J", 180))

    def w(k2_published):
        pins = [PinnedOp(k.key, 1, "X", "Alpha", T(D1, 8), rank=0),
                PinnedOp(g.key, 1, "Y", "Bravo", T(D1, 8), rank=1),
                PinnedOp(k.key, 2, "Z", "Carol", k2_published, rank=2),
                PinnedOp(g.key, 2, "W", "Dave", T(D1, 10), rank=3),
                PinnedOp(c.key, 1, "W", "Dave", T(D1, 13), rank=4),
                PinnedOp(j.key, 1, "Z", "Carol", T(D1, 14), rank=5)]
        s = decode([k, g, c, j], [k.key, g.key, c.key, j.key], m, CFG, pins=pins)
        return sorted((x.order_key, x.start, x.end) for x in s.segments if x.machine_id == "W")
    assert w(T(D1, 8)) == w(T(D1, 11))


def test_upstream_work_first_never_jumps_a_ready_job_on_its_own_machine():
    """Fix round 2. K2 is late on Z, so B (ready now) is about to take Z. A1 on W feeds
    A2, which is ahead of B on Z, so it would be placed first, but on W the job ahead
    of A1 in the published order, G2, is on time and ready at 10:00. A1 must not jump
    G2: G2 starts on W at 10:00."""
    r = {"K": _routing("K", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0)),
         "G": _routing("G", ("S1", ("Y",), 1.0), ("S2", ("W",), 1.0)),
         "A": _routing("A", ("S1", ("W",), 1.0), ("S2", ("Z",), 1.0)),
         "B": _routing("B", ("S1", ("Z",), 1.0))}
    m = _masters4(r)
    k, g, a, b = (_order("K1", "K", 180), _order("G1", "G", 120), _order("A1", "A", 180),
                  _order("B1", "B", 180))
    pins = [PinnedOp(k.key, 1, "X", "Alpha", T(D1, 8), rank=0),
            PinnedOp(g.key, 1, "Y", "Bravo", T(D1, 8), rank=1),
            PinnedOp(k.key, 2, "Z", "Carol", T(D1, 8), rank=2),
            PinnedOp(g.key, 2, "W", "Dave", T(D1, 10), rank=3),
            PinnedOp(a.key, 1, "W", "Dave", T(D1, 12), rank=4),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 15), rank=5),
            PinnedOp(b.key, 1, "Z", "Carol", T(D1, 16), rank=6)]
    s = decode([k, g, a, b], [k.key, g.key, a.key, b.key], m, CFG, pins=pins)
    assert _ops(s, g.key, 2)[0].start == T(D1, 10), _ops(s, g.key, 2)


def _masters5(routings, people):
    machines = {mid: Machine(id=mid, type_text="Manual", kind=MachineKind.MANUAL,
                             available_hrs_per_day=9.5) for mid in ("V", "W", "X", "Y", "Z")}
    ops = tuple(Operator(n, Role.HELPER, frozenset(ms), Shift.FIRST) for n, ms in people)
    return Masters(machines=machines, operators=ops, routings=routings,
                   calendar=ShopCalendar())


def test_upstream_work_first_is_only_work_that_can_make_a_job_ahead_ready():
    """Fix round 2. K2 is late on Z and B (ready now) takes Z. C on W is earlier in the
    published order than B, but nothing queued on Z ahead of B is waiting for a
    feeding step, so C is not placed first: B is staffed at 08:00 (Pat runs both W
    and Z)."""
    r = {"K": _routing("K", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0)),
         "C": _routing("C", ("S1", ("W",), 1.0)),
         "B": _routing("B", ("S1", ("Z",), 1.0))}
    m = _masters5(r, [("Alpha", {"X"}), ("Pat", {"W", "Z"})])
    k, c, b = _order("K1", "K", 180), _order("C1", "C", 180), _order("B1", "B", 120)
    pins = [PinnedOp(k.key, 1, "X", "Alpha", T(D1, 8), rank=0),
            PinnedOp(k.key, 2, "Z", "Pat", T(D1, 8), rank=1),
            PinnedOp(c.key, 1, "W", "Pat", T(D1, 12), rank=2),
            PinnedOp(b.key, 1, "Z", "Pat", T(D1, 14), rank=3)]
    s = decode([k, c, b], [k.key, c.key, b.key], m, CFG, pins=pins)
    assert _ops(s, b.key, 1)[0].start == T(D1, 8), _ops(s, b.key, 1)


def test_feeding_work_may_pass_a_late_job_on_its_own_machine():
    """Fix round 2 (live copy, CNC7). A1 on X feeds A2, which is ahead of B on the late
    machine Z. On X, E2 is ahead of A1 in the published order but is itself late and
    not ready until 12:00, so A1 may go first (the give-way X would make anyway), and
    A2 runs on Z as soon as it is ready, in one piece."""
    r = {"K": _routing("K", ("S1", ("W",), 1.0), ("S2", ("Z",), 1.0)),
         "B": _routing("B", ("S1", ("Y",), 1.0), ("S2", ("Z",), 1.0)),
         "E": _routing("E", ("S1", ("V",), 1.0), ("S2", ("X",), 1.0)),
         "A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0))}
    m = _masters5(r, [("Vic", {"V"}), ("Dave", {"W"}), ("Alpha", {"X"}),
                      ("Bravo", {"Y"}), ("Carol", {"Z"})])
    k, b, e, a = (_order("K1", "K", 300), _order("B1", "B", 180), _order("E1", "E", 240),
                  _order("A1", "A", 120))
    pins = [PinnedOp(k.key, 1, "W", "Dave", T(D1, 8), rank=0),
            PinnedOp(b.key, 1, "Y", "Bravo", T(D1, 8), rank=1),
            PinnedOp(e.key, 1, "V", "Vic", T(D1, 8), rank=2),
            PinnedOp(k.key, 2, "Z", "Carol", T(D1, 8), rank=3),
            PinnedOp(e.key, 2, "X", "Alpha", T(D1, 8), rank=4),
            PinnedOp(a.key, 1, "X", "Alpha", T(D1, 9), rank=5),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 11), rank=6),
            PinnedOp(b.key, 2, "Z", "Carol", T(D1, 13), rank=7)]
    s = decode([k, b, e, a], [k.key, b.key, e.key, a.key], m, CFG, pins=pins)
    a2 = _ops(s, a.key, 2)
    assert (a2[0].start, a2[-1].end) == (T(D1, 10), T(D1, 12)), (a2, _ops(s, b.key, 2))


def test_a_job_ready_before_the_picked_job_starts_goes_first():
    """Fix round 2 (live copy, variant A, DTC2 / MW1). K is late on Z. When Z can next
    start a job (Q, 10:11) B is ready and A is not yet (10:15), so B is picked; but B's
    run cannot start before 11:20 (piece flow), and by then A, earlier in the published
    order, is ready too. A goes first: it runs at 12:10 as planned, not after B."""
    r = {"K": _routing("K", ("S1", ("V",), 1.0), ("S2", ("Z",), 0.5)),
         "Q": _routing("Q", ("S1", ("W",), 1.0), ("S2", ("Z",), 1.0)),
         "A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 20 / 270)),
         "B": _routing("B", ("S1", ("Y",), 1.0), ("S2", ("Z",), 60 / 260))}
    m = _masters5(r, [("Vic", {"V"}), ("Dave", {"W"}), ("Alpha", {"X"}),
                      ("Bravo", {"Y"}), ("Carol", {"Z"})])
    cfg = PlanConfig(plan_start=T(D1, 8), overlap=0.5)
    k, q, a, b = (_order("K1", "K", 600), _order("Q1", "Q", 262), _order("A1", "A", 270),
                  _order("B1", "B", 260))
    pins = [PinnedOp(k.key, 1, "V", "Vic", T(D1, 8), rank=0),
            PinnedOp(q.key, 1, "W", "Dave", T(D1, 8), rank=1),
            PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8), rank=2),
            PinnedOp(b.key, 1, "Y", "Bravo", T(D1, 8), rank=3),
            PinnedOp(k.key, 2, "Z", "Carol", T(D1, 8), rank=4),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 12, 10), rank=5),
            PinnedOp(b.key, 2, "Z", "Carol", T(D1, 12, 30), rank=6),
            PinnedOp(q.key, 2, "Z", "Carol", T(D1, 13), rank=7)]
    s = decode([k, q, a, b], [b.key, q.key, a.key, k.key], m, cfg, pins=pins)
    a2, b2 = _ops(s, a.key, 2), _ops(s, b.key, 2)
    assert a2[0].start == T(D1, 12, 10), (a2, b2)


def test_published_order_elsewhere_is_followed_even_past_a_job_that_starts_later():
    """Fix round 2 (live copy, variant A, CMM). K2 is late on Z and B2 (ready 10:00)
    would take Z. A2, ahead of B2 on Z, is ready at 09:00 once A1 (on X) is placed,
    but A1 sits behind G2 (on W, starting only at 13:00) in the published order. The
    repair follows the published order elsewhere (G2, then A1) before B2 takes Z, so
    A2 runs 09:00-11:00 in one piece."""
    r = {"K": _routing("K", ("S1", ("V",), 1.0), ("S2", ("Z",), 1.0)),
         "B": _routing("B", ("S1", ("Y",), 1.0), ("S2", ("Z",), 1.0)),
         "G": _routing("G", ("S1", ("Y",), 1.0), ("S2", ("W",), 1.0)),
         "A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 2.0))}
    m = _masters5(r, [("Vic", {"V"}), ("Dave", {"W"}), ("Alpha", {"X"}),
                      ("Bravo", {"Y"}), ("Carol", {"Z"})])
    k, b, g, a = (_order("K1", "K", 300), _order("B1", "B", 120), _order("G1", "G", 180),
                  _order("A1", "A", 60))
    pins = [PinnedOp(k.key, 1, "V", "Vic", T(D1, 8), rank=0),
            PinnedOp(b.key, 1, "Y", "Bravo", T(D1, 8), rank=1),
            PinnedOp(g.key, 1, "Y", "Bravo", T(D1, 10), rank=2),
            PinnedOp(k.key, 2, "Z", "Carol", T(D1, 8), rank=3),
            PinnedOp(g.key, 2, "W", "Dave", T(D1, 13), rank=4),
            PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8), rank=5),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 9), rank=6),
            PinnedOp(b.key, 2, "Z", "Carol", T(D1, 10), rank=7)]
    s = decode([k, b, g, a], [b.key, k.key, g.key, a.key], m, CFG, pins=pins)
    a2 = _ops(s, a.key, 2)
    assert (a2[0].start, a2[-1].end) == (T(D1, 9), T(D1, 11)), (a2, _ops(s, b.key, 2))


def test_a_late_job_never_lets_its_give_way_reorder_another_machine():
    """Fix round 2 (live copy, variant B, CNC7 / VMC2). K2 is late on Z; B (ready now)
    takes Z. K's next step K3 is queued on W ahead of C. Nothing on Z is waiting for a
    feeding step, so B simply goes first on Z, and W keeps its published order: C is
    not placed in front of K3 (it only uses W's time K3 cannot use)."""
    r = {"K": _routing("K", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0), ("S3", ("W",), 1.0)),
         "C": _routing("C", ("S1", ("W",), 1.0)),
         "B": _routing("B", ("S1", ("Z",), 1.0))}
    m = _masters4(r)
    k, c, b = _order("K1", "K", 180), _order("C1", "C", 300), _order("B1", "B", 240)
    pins = [PinnedOp(k.key, 1, "X", "Alpha", T(D1, 8), rank=0),
            PinnedOp(k.key, 2, "Z", "Carol", T(D1, 8), rank=1),
            PinnedOp(k.key, 3, "W", "Dave", T(D1, 11), rank=2),
            PinnedOp(c.key, 1, "W", "Dave", T(D1, 14), rank=3),
            PinnedOp(b.key, 1, "Z", "Carol", T(D1, 15), rank=4)]
    s = decode([k, c, b], [k.key, c.key, b.key], m, CFG, pins=pins)
    b1, k2, k3 = _ops(s, b.key, 1), _ops(s, k.key, 2), _ops(s, k.key, 3)
    assert (b1[0].start, b1[-1].end) == (T(D1, 8), T(D1, 12)), b1        # B goes first
    assert k2[0].start == T(D1, 12), k2
    assert k3[0].start == k2[-1].end, k3       # K3 not delayed by C on W


def test_no_give_way_when_the_work_that_could_feed_it_is_queued_behind_the_late_order():
    """Fix round 2 (live copy, CNC7 cases). K2 is late on Z and B is ready. A2, ahead of
    B on Z, waits for A1 on X, but A1 is queued on X behind K's own next step K3: A1
    cannot be placed first without jumping X's queue, so whether A becomes ready in
    time is unknown. Then there is no give-way: K2 keeps its place (published order,
    as if it were on time), and B only uses Z's time K2 and A2 cannot use."""
    r = {"K": _routing("K", ("S1", ("V",), 1.0), ("S2", ("Z",), 1.0), ("S3", ("X",), 1.0)),
         "A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0)),
         "B": _routing("B", ("S1", ("Z",), 1.0))}
    m = _masters5(r, [("Vic", {"V"}), ("Alpha", {"X"}), ("Carol", {"Z"})])
    k, a, b = _order("K1", "K", 180), _order("A1", "A", 60), _order("B1", "B", 240)
    pins = [PinnedOp(k.key, 1, "V", "Vic", T(D1, 8), rank=0),
            PinnedOp(k.key, 2, "Z", "Carol", T(D1, 8), rank=1),
            PinnedOp(k.key, 3, "X", "Alpha", T(D1, 11), rank=2),
            PinnedOp(a.key, 1, "X", "Alpha", T(D1, 14), rank=3),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 15), rank=4),
            PinnedOp(b.key, 1, "Z", "Carol", T(D1, 16), rank=5)]
    s = decode([k, a, b], [b.key, a.key, k.key], m, CFG, pins=pins)
    k2 = _ops(s, k.key, 2)
    assert (k2[0].start, k2[-1].end) == (T(D1, 11), T(D1, 14)), (k2, _ops(s, b.key, 1))


def test_when_both_are_ready_the_published_order_wins():
    """Z's published queue is A then B, both ready at 08:00, and the priority sequence
    favours B. A runs first."""
    r = {"A": _routing("A", ("S1", ("Z",), 1.0)), "B": _routing("B", ("S1", ("Z",), 1.0))}
    m = _masters(r)
    a, b = _order("A1", "A", 120), _order("B1", "B", 60)
    pins = [PinnedOp(a.key, 1, "Z", "Carol", T(D1, 8), rank=0),
            PinnedOp(b.key, 1, "Z", "Carol", T(D1, 10), rank=1)]
    s = decode([a, b], [b.key, a.key], m, CFG, pins=pins)
    assert _ops(s, a.key, 1)[0].start == T(D1, 8)
    assert _ops(s, b.key, 1)[0].start >= _ops(s, a.key, 1)[-1].end


def test_a_job_at_a_vendor_lets_the_next_ready_job_go_first():
    """A's step before Z is outsourced (three days at the vendor). A was published on
    Z at 08:00; B, published after it, is ready now and is a long job (50 h, longer
    than the time A is away). B goes first; A keeps Z and runs after B."""
    r = {"A": Routing("A", "A", (
            Operation(1, "PLATING OS", OperationKind.OUTSOURCED, (), 72 * 60.0),
            Operation(2, "S2", MAN, ("Z",), 1.0),
            Operation(3, "DISPATCH", DISP))),
         "B": _routing("B", ("S1", ("Z",), 1.0))}
    m = _masters(r)
    a, b = _order("A1", "A", 60), _order("B1", "B", 3000)
    pins = [PinnedOp(a.key, 2, "Z", "Carol", T(D1, 8), rank=0),
            PinnedOp(b.key, 1, "Z", "Carol", T(D1, 9), rank=1)]
    s = decode([a, b], [a.key, b.key], m, CFG, pins=pins)
    a2, b1 = _ops(s, a.key, 2), _ops(s, b.key, 1)
    assert b1[0].start == T(D1, 8), b1
    assert {g.machine_id for g in a2} == {"Z"}
    assert a2[0].start >= max(T(D1 + timedelta(days=3), 8), b1[-1].end), (a2, b1[-1])


def test_early_finish_moves_the_next_job_up():
    """Pins carry no times: when A needs less work than published, B starts right after."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0)), "B": _routing("B", ("S1", ("X",), 1.0))}
    m = _masters(r)
    a = Order(so_no="A1", item_code="A", item_name="A", qty=240, due_date=date(2025, 3, 20),
              process_remaining={1: 60, 2: 0})                 # 3 of 4 hours already done
    b = _order("B1", "B", 60)
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8)),
            PinnedOp(b.key, 1, "X", "Alpha", T(D1, 12))]
    s = decode([a, b], [a.key, b.key], m, CFG, pins=pins)
    assert _ops(s, b.key, 1)[0].start == T(D1, 9)


def test_zero_remaining_pinned_op_does_not_hold_the_queue():
    r = {"A": _routing("A", ("S1", ("Y",), 1.0), ("S2", ("X",), 1.0)),
         "B": _routing("B", ("S1", ("X",), 1.0))}
    m = _masters(r)
    a = Order(so_no="A1", item_code="A", item_name="A", qty=60, due_date=date(2025, 3, 20),
              process_remaining={1: 60, 2: 0, 3: 0})          # A's X step already done
    b = _order("B1", "B", 60)
    pins = [PinnedOp(a.key, 2, "X", "Alpha", T(D1, 8)),
            PinnedOp(b.key, 1, "X", "Alpha", T(D1, 10))]
    s = decode([a, b], [a.key, b.key], m, CFG, pins=pins)
    assert _ops(s, b.key, 1)[0].start == T(D1, 8)


def test_preferred_operator_kept_when_free():
    r = {"A": _routing("A", ("S1", ("X",), 1.0))}
    m = _masters(r)
    m = Masters(machines=m.machines,
                operators=m.operators + (Operator("Dan", Role.HELPER, frozenset({"X"}), Shift.FIRST),),
                routings=r, calendar=ShopCalendar())
    a = _order("A1", "A", 60)
    s = decode([a], [a.key], m, CFG, pins=[PinnedOp(a.key, 1, "X", "Dan", T(D1, 8))])
    assert {seg.operator for seg in _ops(s, a.key, 1)} == {"Dan"}


def test_a_cyclic_published_queue_never_stalls():
    """X queue: A2 then B1. Y queue: B2 then A1. A1<A2 and B1<B2 by routing: a cycle
    under the old strict turn, which needed a deadlock guard. With next ready job in
    published order (spec section 8) nothing waits for a turn, so every step is
    planned on its published machine and no guard is needed."""
    r = {"A": _routing("A", ("S1", ("Y",), 1.0), ("S2", ("X",), 1.0)),
         "B": _routing("B", ("S1", ("X",), 1.0), ("S2", ("Y",), 1.0))}
    m = _masters(r)
    a, b = _order("A1", "A", 60), _order("B1", "B", 60)
    pins = [PinnedOp(a.key, 2, "X", "", T(D1, 8)), PinnedOp(b.key, 1, "X", "", T(D1, 9)),
            PinnedOp(b.key, 2, "Y", "", T(D1, 8)), PinnedOp(a.key, 1, "Y", "", T(D1, 9))]
    s = decode([a, b], [a.key, b.key], m, CFG, pins=pins)
    assert set(s.completion) == {a.key, b.key}
    for key, seq, mid in ((a.key, 1, "Y"), (a.key, 2, "X"), (b.key, 1, "X"), (b.key, 2, "Y")):
        assert {g.machine_id for g in _ops(s, key, seq)} == {mid}


def test_frozen_op_out_of_turn_does_not_block():
    """Published X queue: A then B. The floor started B first (punched, so B is frozen).
    B runs first; A keeps its machine and runs after."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0)), "B": _routing("B", ("S1", ("X",), 1.0))}
    m = _masters(r)
    a = _order("A1", "A", 60)
    b = Order(so_no="B1", item_code="B", item_name="B", qty=120, due_date=date(2025, 3, 20),
              process_remaining={1: 60, 2: 0})
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8)),
            PinnedOp(b.key, 1, "X", "Alpha", T(D1, 9))]
    frozen = [FrozenOp(b.key, 1, "X", "Alpha", 60, T(D1, 9))]
    s = decode([a, b], [a.key, b.key], m, CFG, frozen=frozen, pins=pins)
    assert _ops(s, b.key, 1)[0].start == T(D1, 8)
    assert {seg.machine_id for seg in _ops(s, a.key, 1)} == {"X"}
    assert _ops(s, a.key, 1)[0].start >= _ops(s, b.key, 1)[-1].end


def test_a_pin_to_a_machine_outside_the_options_is_ignored():
    r = {"A": _routing("A", ("S1", ("Y",), 1.0))}
    m = _masters(r)
    a = _order("A1", "A", 60)
    s = decode([a], [a.key], m, CFG, pins=[PinnedOp(a.key, 1, "X", "", T(D1, 8))])
    assert {seg.machine_id for seg in _ops(s, a.key, 1)} == {"Y"}


def test_zero_remaining_op_ahead_in_the_queue_does_not_deadlock():
    """X queue: A2 (nothing left to make) then B1. Y queue: B2 then A1. If the finished
    A2 held X's turn, A2 waits for A1, A1 for B2, B2 for B1, B1 for A2: a cycle that
    would need a release. A finished step must not hold the queue."""
    r = {"A": _routing("A", ("S1", ("Y",), 1.0), ("S2", ("X",), 1.0)),
         "B": _routing("B", ("S1", ("X",), 1.0), ("S2", ("Y",), 1.0))}
    m = _masters(r)
    a = Order(so_no="A1", item_code="A", item_name="A", qty=60, due_date=date(2025, 3, 20),
              process_remaining={1: 60, 2: 0, 3: 0})
    b = _order("B1", "B", 60)
    pins = [PinnedOp(a.key, 2, "X", "", T(D1, 8)), PinnedOp(b.key, 1, "X", "", T(D1, 9)),
            PinnedOp(b.key, 2, "Y", "", T(D1, 8)), PinnedOp(a.key, 1, "Y", "", T(D1, 9))]
    s = decode([a, b], [a.key, b.key], m, CFG, pins=pins)
    assert set(s.completion) == {a.key, b.key}


def test_frozen_op_ahead_in_the_queue_does_not_hold_it():
    """Published X queue: B (frozen, already running) then A. The frozen op is laid first
    by the frozen path, so it must not sit in the queue as a head that never clears."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0)), "B": _routing("B", ("S1", ("X",), 1.0))}
    m = _masters(r)
    a = _order("A1", "A", 60)
    b = Order(so_no="B1", item_code="B", item_name="B", qty=120, due_date=date(2025, 3, 20),
              process_remaining={1: 60, 2: 0})
    pins = [PinnedOp(b.key, 1, "X", "Alpha", T(D1, 8)),
            PinnedOp(a.key, 1, "X", "Alpha", T(D1, 9))]
    frozen = [FrozenOp(b.key, 1, "X", "Alpha", 60, T(D1, 8))]
    s = decode([a, b], [a.key, b.key], m, CFG, frozen=frozen, pins=pins)
    assert _ops(s, a.key, 1)[0].start >= _ops(s, b.key, 1)[-1].end


def test_frozen_op_gated_by_a_frozen_predecessor_is_not_jumped():
    """B step 1 frozen on Y (1 h left), B step 2 frozen on X (2 h left, so it can only
    start at 09:00). A is published on X after B. A must not take the 08:00 gap in front
    of the frozen B2: frozen ops run first on their machine."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0)),
         "B": _routing("B", ("S1", ("Y",), 1.0), ("S2", ("X",), 1.0))}
    m = _masters(r)
    a = _order("A1", "A", 60)
    b = Order(so_no="B1", item_code="B", item_name="B", qty=120, due_date=date(2025, 3, 20),
              process_remaining={1: 60, 2: 120, 3: 0})
    pins = [PinnedOp(b.key, 1, "Y", "Bravo", T(D1, 8)),
            PinnedOp(b.key, 2, "X", "Alpha", T(D1, 9)),
            PinnedOp(a.key, 1, "X", "Alpha", T(D1, 11))]
    frozen = [FrozenOp(b.key, 1, "Y", "Bravo", 60, T(D1, 8)),
              FrozenOp(b.key, 2, "X", "Alpha", 120, T(D1, 9))]
    s = decode([a, b], [a.key, b.key], m, CFG, frozen=frozen, pins=pins)
    assert _ops(s, a.key, 1)[0].start >= _ops(s, b.key, 2)[-1].end


def test_a_pin_to_a_machine_nobody_can_man_is_ignored():
    r = {"A": _routing("A", ("S1", ("X", "Y"), 1.0))}
    m = _masters(r)
    m = Masters(machines=m.machines,
                operators=(Operator("Alpha", Role.HELPER, frozenset({"X"}), Shift.FIRST),),
                routings=r, calendar=ShopCalendar())
    a = _order("A1", "A", 60)
    s = decode([a], [a.key], m, CFG, pins=[PinnedOp(a.key, 1, "Y", "", T(D1, 8))])
    assert {seg.machine_id for seg in _ops(s, a.key, 1)} == {"X"}
