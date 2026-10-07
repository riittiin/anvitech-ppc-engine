"""Fixed plan (2026-10-06): a repair of a plan nobody has punched against reproduces it.

People are booked first come at placement time. Before Task 6b the repair placed
jobs in a different ORDER than the plan it repaired (Giffler-Thompson over the queue
heads) and preferred one operator for every window, so a job took a person the
published plan had given another job and the drift compounded: measured on Test5/8/9
an unpunched plan, repaired, moved up to 20 days with nearly every order later, and
on a copy of the live store 39 of 67 orders moved (up to 10 days). The repair now
places jobs in the published plan's own placement order (``PinnedOp.rank``) and
prefers, window by window, the person the published plan had on the job
(``PinnedOp.staff``)."""
from datetime import date, datetime, timedelta

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import Machine, MachineKind, Operator, Role, Shift
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.scheduler import decode
from ppc_engine.scheduler.schedule import PinnedOp

D1 = date(2025, 3, 3)                      # a Monday
CFG = PlanConfig(plan_start=datetime(2025, 3, 3, 8), overlap=0.0)
MAN = OperationKind.MANUAL


def _publish(sched):
    """A plan as the API publishes it (freeze.schedule_projection -> _ppc_pins): one
    pin per (order, step) with its machine, first person, start, the ORDER it was
    placed in, and who ran it stretch by stretch."""
    first, segs, order = {}, {}, []
    for sg in sched.segments:
        if sg.machine_id is None:
            continue
        ck = (sg.order_key, sg.op_seq)
        if ck not in first:
            first[ck] = sg
            order.append(ck)
        segs.setdefault(ck, []).append((sg.start, sg.end, sg.operator or ""))
    pins = []
    for rank, ck in enumerate(order):
        staff = tuple(sorted(segs[ck]))
        pins.append(PinnedOp(ck[0], ck[1], first[ck].machine_id, staff[0][2], staff[0][0],
                             rank=rank, staff=staff))
    return pins


def _sig(sched):
    return sorted((s.order_key, s.op_seq, s.machine_id, s.operator, s.start, s.end)
                  for s in sched.segments)


def _book(people, lines):
    machines = {m: Machine(id=m, type_text="Manual", kind=MachineKind.MANUAL,
                           available_hrs_per_day=9.5) for m in ("M1", "M2", "M3", "M4")}
    ops = tuple(Operator(n, Role.HELPER, frozenset(ms), Shift.FIRST) for n, ms in people)
    routings, orders = {}, []
    for so, qty, steps in lines:
        item = "I" + so
        seq = [Operation(i + 1, f"S{i + 1}", MAN, opts, 1.0) for i, opts in enumerate(steps)]
        seq.append(Operation(len(seq) + 1, "DISPATCH", OperationKind.DISPATCH))
        routings[item] = Routing(item, item, tuple(seq))
        orders.append(Order(so_no=so, item_code=item, item_name=item, qty=qty,
                            due_date=date(2025, 3, 20)))
    return Masters(machines=machines, operators=ops, routings=routings,
                   calendar=ShopCalendar()), orders


# Three helpers who each cover several stations, so who gets whom depends on the
# order the jobs are placed in (found by search; both mechanisms are load-bearing).
BOOKS = {
    "shared-helpers-a": _book(
        [("Alpha", {"M1", "M3"}), ("Bravo", {"M1", "M3", "M4"}), ("Carol", {"M1", "M2"})],
        [("SO0", 60, [("M2",), ("M3", "M1")]),
         ("SO1", 120, [("M1",), ("M3", "M1")]),
         ("SO2", 120, [("M3", "M1"), ("M4", "M3")]),
         ("SO3", 240, [("M4",)]),
         ("SO4", 600, [("M1", "M2")])]),
    "shared-helpers-b": _book(
        [("Alpha", {"M2", "M4"}), ("Bravo", {"M1", "M3"}), ("Carol", {"M1", "M3"})],
        [("SO0", 60, [("M1",), ("M4", "M3")]),
         ("SO1", 420, [("M3",), ("M3",)]),
         ("SO2", 120, [("M2",), ("M2",), ("M1",)]),
         ("SO3", 600, [("M3",)])]),
}


def test_repair_of_an_unpunched_plan_is_that_plan():
    """Zero tolerance: every step on the same machine, same person, same minutes, so
    every delivery date is the published one."""
    for name, (m, orders) in BOOKS.items():
        seq = [o.key for o in orders]
        free = decode(orders, seq, m, CFG)
        repaired = decode(orders, seq, m, CFG, pins=_publish(free))
        assert _sig(repaired) == _sig(free), name
        assert repaired.completion == free.completion, name


def test_repair_of_a_repair_changes_nothing():
    """Spec 5.4 (idempotence): with no new punches a repair returns the same plan."""
    for name, (m, orders) in BOOKS.items():
        seq = [o.key for o in orders]
        free = decode(orders, seq, m, CFG)
        once = decode(orders, seq, m, CFG, pins=_publish(free))
        twice = decode(orders, seq, m, CFG, pins=_publish(once))
        assert _sig(twice) == _sig(once), name


def test_a_plan_published_without_ranks_still_repairs():
    """A published plan from before ``rank``/``staff`` existed: the repair falls back
    to the published start order and the one planned operator, and still places
    every step on its published machine."""
    m, orders = BOOKS["shared-helpers-a"]
    seq = [o.key for o in orders]
    free = decode(orders, seq, m, CFG)
    old = [PinnedOp(p.order_key, p.op_seq, p.machine_id, p.operator, p.prev_start)
           for p in _publish(free)]
    repaired = decode(orders, seq, m, CFG, pins=old)
    where = {(s.order_key, s.op_seq): s.machine_id for s in free.segments if s.machine_id}
    for s in repaired.segments:
        if s.machine_id:
            assert s.machine_id == where[(s.order_key, s.op_seq)]


def test_contended_book_through_the_pipeline_keeps_every_date():
    """Task 6's contended book (big lines, a reversed job order, overlap 95, flexible
    machines): published through freeze.schedule_projection and repaired through
    run_forward, every order keeps its date. Before the fix two orders moved 31 days
    earlier and two 35 days later."""
    import io
    from dataclasses import replace as _replace

    from engine import book_store, freeze, loaders
    from engine.config import Config
    from engine.models import SOLine
    from engine.optimizer import expected_completion
    from engine.pipeline import PlanRun, run_forward
    from tests.new_sample_workbook import ITEM_A, ITEM_B, SO1, SO2, build_new_sample_bytes

    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    m = loaders.load_all(io.BytesIO(wb))[1]
    cfg = _replace(Config(), scheduler="new", plan_start_date=date(2025, 3, 3),
                   apply_operator_logic=True, overlap_percent=95, flexible_machines=True)
    lines = [SOLine(SO1, ITEM_A, ITEM_A, 50, date(2025, 3, 20)),
             SOLine(SO2, ITEM_B, ITEM_B, 100, date(2025, 3, 21)),
             SOLine("NSO-003", ITEM_A, ITEM_A, 4000, date(2025, 3, 22)),
             SOLine("NSO-004", ITEM_B, ITEM_B, 6000, date(2025, 3, 23))]
    ranks = {f"{l.so_no}\x1f{l.item_code}": -i for i, l in enumerate(lines)}
    free = PlanRun(so_lines=list(lines))
    run_forward(free, cfg, m, priority_rank=ranks)
    rows = freeze.schedule_projection(free.schedule)
    assert sorted(r["placed"] for r in rows) == sorted(set(r["placed"] for r in rows))
    assert all(r["staff"] for r in rows)
    repaired = PlanRun(so_lines=list(lines))
    run_forward(repaired, cfg, m, priority_rank=ranks, published=rows)
    assert expected_completion(repaired.schedule) == expected_completion(free.schedule)
    again = PlanRun(so_lines=list(lines))
    run_forward(again, cfg, m, priority_rank=ranks,
                published=freeze.schedule_projection(repaired.schedule))
    assert expected_completion(again.schedule) == expected_completion(repaired.schedule)
