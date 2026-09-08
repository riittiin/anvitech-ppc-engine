"""The engine layer of the Add New Orders quote (2026-09-08 spec).

Occupancy = what an earlier planning stage already committed. It must be able to
reach the placement step without any existing plan changing by a single minute.
"""
from datetime import date, datetime, time, timedelta

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.resources import Shift
from ppc_engine.worktime import shift_key_for

D = datetime
_CFG = PlanConfig(plan_start=D(2025, 3, 3, 8), first_start=time(8, 0),
                  first_end=time(19, 0), second_start=time(19, 0),
                  second_end=time(5, 0))


def test_free_runs_with_no_occupancy_is_one_unbounded_run():
    cal = ShopCalendar()
    assert cal.free_runs("CNC3", D(2025, 3, 3, 8)) == [(D(2025, 3, 3, 8), None)]


def test_free_runs_splits_around_a_busy_block():
    cal = ShopCalendar(machine_busy={"CNC3": ((D(2025, 3, 5, 8), D(2025, 3, 7, 12)),)})
    assert cal.free_runs("CNC3", D(2025, 3, 3, 8)) == [
        (D(2025, 3, 3, 8), D(2025, 3, 5, 8)),
        (D(2025, 3, 7, 12), None),
    ]


def test_free_runs_starting_inside_a_busy_block_starts_after_it():
    cal = ShopCalendar(machine_busy={"CNC3": ((D(2025, 3, 5, 8), D(2025, 3, 7, 12)),)})
    assert cal.free_runs("CNC3", D(2025, 3, 6, 9)) == [(D(2025, 3, 7, 12), None)]


def test_free_runs_ignores_blocks_that_are_already_past():
    cal = ShopCalendar(machine_busy={"CNC3": ((D(2025, 3, 1, 8), D(2025, 3, 2, 8)),)})
    assert cal.free_runs("CNC3", D(2025, 3, 3, 8)) == [(D(2025, 3, 3, 8), None)]


def test_free_runs_merges_overlapping_and_touching_blocks():
    cal = ShopCalendar(machine_busy={"CNC3": (
        (D(2025, 3, 5, 8), D(2025, 3, 6, 8)),
        (D(2025, 3, 6, 8), D(2025, 3, 7, 8)),     # touching
        (D(2025, 3, 6, 20), D(2025, 3, 8, 8)),    # overlapping
    )})
    assert cal.free_runs("CNC3", D(2025, 3, 3, 8)) == [
        (D(2025, 3, 3, 8), D(2025, 3, 5, 8)),
        (D(2025, 3, 8, 8), None),
    ]


def test_free_runs_for_a_machine_with_no_entry_is_unbounded():
    cal = ShopCalendar(machine_busy={"CNC3": ((D(2025, 3, 5, 8), D(2025, 3, 7, 12)),)})
    assert cal.free_runs("VMC1", D(2025, 3, 3, 8)) == [(D(2025, 3, 3, 8), None)]


def test_a_default_calendar_still_answers_the_old_questions():
    cal = ShopCalendar()
    assert cal.is_working_day(date(2025, 3, 3)) is True      # Monday
    assert cal.is_working_day(date(2025, 3, 6)) is False     # Thursday, weekly off
    assert cal.is_machine_available("CNC3", date(2025, 3, 3)) is True
    assert cal.is_operator_available("Anyone", date(2025, 3, 3)) is True


def test_shift_key_for_a_day_shift_moment():
    assert shift_key_for(D(2025, 3, 3, 10), _CFG) == (date(2025, 3, 3), Shift.FIRST)


def test_shift_key_for_an_evening_moment_is_that_days_night_shift():
    assert shift_key_for(D(2025, 3, 3, 21), _CFG) == (date(2025, 3, 3), Shift.SECOND)


def test_shift_key_for_after_midnight_belongs_to_the_previous_days_night_shift():
    assert shift_key_for(D(2025, 3, 4, 2), _CFG) == (date(2025, 3, 3), Shift.SECOND)


def test_shift_key_for_a_moment_in_no_shift_is_none():
    assert shift_key_for(D(2025, 3, 4, 6), _CFG) is None


from ppc_engine.domain.resources import Machine, MachineKind, Operator, Role
from ppc_engine.scheduler.staffing import StaffingBoard


def _two_operator_board():
    m = Machine(id="CNC3", type_text="CNC lathe", kind=MachineKind.MACHINING,
                available_hrs_per_day=19.5)
    ops = (
        Operator(name="Alpha", role=Role.OPERATOR, base_shift=Shift.FIRST,
                 qualified_machines=frozenset({"CNC3"})),
        Operator(name="Bravo", role=Role.OPERATOR, base_shift=Shift.FIRST,
                 qualified_machines=frozenset({"CNC3"})),
    )
    return m, ops


def test_a_seeded_booking_makes_that_person_unavailable():
    board = StaffingBoard(booked={"Alpha": ((D(2025, 3, 3, 8), D(2025, 3, 3, 12)),)})
    assert board.free_during("Alpha", D(2025, 3, 3, 9), D(2025, 3, 3, 10)) is False
    assert board.free_during("Alpha", D(2025, 3, 3, 13), D(2025, 3, 3, 14)) is True
    assert board.free_during("Bravo", D(2025, 3, 3, 9), D(2025, 3, 3, 10)) is True


def test_a_seeded_booking_is_skipped_when_picking_an_operator():
    from ppc_engine.domain.masters import Masters
    m, ops = _two_operator_board()
    masters = Masters(machines={"CNC3": m}, operators=ops, routings={},
                      calendar=ShopCalendar())
    board = StaffingBoard({"CNC3": ops},
                          booked={"Alpha": ((D(2025, 3, 3, 8), D(2025, 3, 3, 12)),)})
    pick = board.candidate_operator(m, date(2025, 3, 3), Shift.FIRST,
                                    D(2025, 3, 3, 9), D(2025, 3, 3, 10), masters, _CFG)
    assert pick == "Bravo"


def test_a_seeded_assignment_is_the_machines_shift_operator():
    board = StaffingBoard(assigned={("CNC3", date(2025, 3, 3), Shift.FIRST): "Alpha"})
    assert board.operator_for("CNC3", date(2025, 3, 3), Shift.FIRST) == "Alpha"


def test_an_unseeded_board_is_unchanged():
    board = StaffingBoard()
    assert board.free_during("Alpha", D(2025, 3, 3, 9), D(2025, 3, 3, 10)) is True
    assert board.operator_for("CNC3", date(2025, 3, 3), Shift.FIRST) is None


import io

import pytest

from engine import book_store, loaders
from engine.config import Config
from engine.new_engine import _orders_from_batches, _plan_config
from engine.rules import rule1_consolidate
from ppc_engine.domain.routing import OperationKind
from ppc_engine.loaders import load_all as new_load
from ppc_engine.scheduler.flow_scheduler import _lay_on_machine
from ppc_engine.scheduler.staffing import StaffingBoard, build_machine_pools
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
               apply_operator_logic=True)

_IN_HOUSE = (OperationKind.MACHINING, OperationKind.MANUAL, OperationKind.INSPECTION)


@pytest.fixture()
def shop():
    """The sample shop: (masters, plan config, first machining order + its first op).

    Picks the first (order, operation) pair where the operation is an in-house kind
    with at least one machine option that actually exists in the Machine master — a
    routing's first step is not always safe to assume that about (it can be an
    outsourced or off-machine step, or name a machine the master never registered).
    """
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, _ = loaders.load_all(io.BytesIO(wb))
    orders, _ = _orders_from_batches(rule1_consolidate.run(so_lines, _CONF), nm)
    cfg = _plan_config(_CONF)

    found = None
    for order in orders:
        routing = nm.routings.get(order.item_code)
        if routing is None:
            continue
        for op in routing.operations:
            if op.kind not in _IN_HOUSE or not op.machine_options:
                continue
            if op.machine_options[0] in nm.machines:
                found = (order, op)
                break
        if found is not None:
            break
    assert found is not None, "no in-house op with a real machine option in the sample book"
    order, op = found
    return nm, cfg, order, op


def _lay(shop, deadline, minutes=600.0):
    nm, cfg, order, op = shop
    mid = op.machine_options[0]
    board = StaffingBoard(build_machine_pools(nm))
    return _lay_on_machine(nm.machines[mid], cfg.plan_start, minutes, order, op,
                           int(order.qty), board, nm, cfg, deadline=deadline)


def test_no_deadline_lays_the_work_exactly_as_before(shop):
    assert _lay(shop, None) is not None


def test_a_deadline_that_cannot_hold_the_work_returns_none(shop):
    _nm, cfg, _o, _op = shop
    assert _lay(shop, cfg.plan_start + timedelta(minutes=30)) is None


def test_a_generous_deadline_gives_the_identical_placement(shop):
    _nm, cfg, _o, _op = shop
    loose = _lay(shop, cfg.plan_start + timedelta(days=90))
    free = _lay(shop, None)
    assert loose is not None
    assert (loose["start"], loose["end"]) == (free["start"], free["end"])
    assert [(s.start, s.end) for s in loose["segments"]] == \
           [(s.start, s.end) for s in free["segments"]]


def test_no_segment_ever_crosses_the_deadline(shop):
    _nm, cfg, _o, _op = shop
    deadline = cfg.plan_start + timedelta(days=2)
    laid = _lay(shop, deadline, minutes=120.0)
    assert laid is not None
    assert all(seg.end <= deadline for seg in laid["segments"])


def _duration_spanning_into_a_later_window(nm, cfg, machine):
    """A duration long enough that laying it walks past two whole working windows
    and lands partway through a THIRD — so a deadline built off its natural finish
    sits strictly inside a later window, never at a window boundary. Derived from
    the fixture's own machine/config, not a hardcoded clock time, so this stays
    correct if the sample workbook's shift lengths ever change."""
    from ppc_engine.worktime import iter_windows
    windows = []
    for win in iter_windows(machine, cfg.plan_start, nm.calendar, cfg):
        windows.append(win)
        if len(windows) == 3:
            break
    assert len(windows) == 3, "the fixture machine needs at least 3 working windows"
    full_two = sum((w.end - w.start).total_seconds() / 60.0 for w in windows[:2])
    third_len = (windows[2].end - windows[2].start).total_seconds() / 60.0
    return full_two + third_len / 2.0  # lands halfway through window 3


def test_a_deadline_strictly_inside_a_later_window_clamps_correctly(shop):
    """The vacuous-fixture trap this repo has been bitten by before: a deadline
    that never actually engages the clamp passes identically whether the clamp
    exists or not. This test spans at least two full windows and lands the
    natural finish strictly inside a THIRD window (no boundary, no premature
    finish), then checks both directions relative to that finish, computed from
    a real deadline=None run rather than a hardcoded clock time — self-
    calibrating, so it cannot go vacuous if the sample workbook changes.
    """
    nm, cfg, _order, op = shop
    machine = nm.machines[op.machine_options[0]]
    minutes = _duration_spanning_into_a_later_window(nm, cfg, machine)

    free = _lay(shop, None, minutes=minutes)
    assert free is not None
    natural_end = free["end"]

    # A deadline just before the natural finish (still inside the same window):
    # the work cannot complete in time.
    too_tight = _lay(shop, natural_end - timedelta(minutes=5), minutes=minutes)
    assert too_tight is None

    # A deadline just after the natural finish (still inside the same window,
    # not beyond it): the clamp never had to bind, so the placement is identical.
    loose = _lay(shop, natural_end + timedelta(minutes=5), minutes=minutes)
    assert loose is not None
    assert loose["end"] == natural_end
    assert [(s.start, s.end) for s in loose["segments"]] == \
           [(s.start, s.end) for s in free["segments"]]


from dataclasses import replace as _replace

from ppc_engine.scheduler import decode


def _decoded(nm, cfg, orders):
    return decode(orders, [o.key for o in orders], nm, cfg)


def _busy_machine_ivs(sched):
    out = {}
    for seg in sched.segments:
        if seg.machine_id and seg.end > seg.start:
            out.setdefault(seg.machine_id, []).append((seg.start, seg.end))
    return {k: tuple(sorted(v)) for k, v in out.items()}


@pytest.fixture()
def book():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, _ = loaders.load_all(io.BytesIO(wb))
    orders, _ = _orders_from_batches(rule1_consolidate.run(so_lines, _CONF), nm)
    return nm, _plan_config(_CONF), orders


def _machine_ids(sched):
    """The set of machines that actually carry real (non-zero-duration) work."""
    return {seg.machine_id for seg in sched.segments
            if seg.machine_id and seg.end > seg.start}


def _stage_split(orders):
    """Split the sample book's two orders so each stage gets one, and confirm the
    split is not degenerate: both stages must place real machine-bound work, and at
    least one machine must be contended by both (the sample workbook's B001 and B002
    both route through CNC1, so the natural half/half split already gives this —
    checked here rather than assumed, so a future change to the sample workbook that
    breaks the overlap fails LOUDLY instead of leaving these tests vacuous)."""
    first, second = orders[: len(orders) // 2], orders[len(orders) // 2:]
    assert first and second
    return first, second


def test_a_plan_with_no_occupancy_is_byte_identical(book):
    """Determinism check, NOT an inertness check (review-flagged, 2026-09-08 fix
    round 1): a freshly loaded ``book`` already has an empty calendar, so ``nm``
    below and the explicitly-re-emptied calendar are already equal — this proves
    ``decode`` gives the same book the same plan twice, nothing about the
    occupancy mechanism actually engaging. The real inertness guarantee — that
    occupancy on one machine leaves an untouched order's plan byte-identical — is
    `test_occupying_one_machine_leaves_untouched_orders_unchanged` below.
    """
    nm, cfg, orders = book
    before = _decoded(nm, cfg, orders)
    after = _decoded(_replace(nm, calendar=_replace(nm.calendar, machine_busy={},
                                                    operator_busy={})), cfg, orders)
    assert [(s.order_key, s.op_seq, s.machine_id, s.operator, s.start, s.end, s.qty)
            for s in before.segments] == \
           [(s.order_key, s.op_seq, s.machine_id, s.operator, s.start, s.end, s.qty)
            for s in after.segments]
    assert before.completion == after.completion


def _routes_through(nm, order, machine_id):
    routing = nm.routings.get(order.item_code)
    if routing is None:
        return False
    return any(machine_id in op.machine_options for op in routing.operations)


def test_occupying_one_machine_leaves_untouched_orders_unchanged(book):
    """The actual inertness guarantee: real occupancy on ONE machine must never
    move an order whose routing never goes near that machine. MW1 is used only by
    B002's routing in the sample book (confirmed below, not assumed) — B001's
    segments must be byte-identical whether or not MW1 carries a busy block.
    """
    nm, cfg, orders = book
    occupied_mid = "MW1"
    touching = [o for o in orders if _routes_through(nm, o, occupied_mid)]
    untouched = [o for o in orders if not _routes_through(nm, o, occupied_mid)]
    assert touching, "fixture needs an order that DOES route through the occupied machine"
    assert untouched, "fixture needs an order that does NOT route through it"

    before = _decoded(nm, cfg, orders)
    busy_block = (cfg.plan_start, cfg.plan_start + timedelta(hours=4))
    nm2 = _replace(nm, calendar=_replace(
        nm.calendar, machine_busy={occupied_mid: (busy_block,)}))
    after = _decoded(nm2, cfg, orders)

    untouched_keys = {o.key for o in untouched}

    def _rows(sched):
        return [(s.order_key, s.op_seq, s.machine_id, s.operator, s.start, s.end, s.qty)
                for s in sched.segments if s.order_key in untouched_keys]

    assert _rows(before) == _rows(after)
    assert {k: v for k, v in before.completion.items() if k in untouched_keys} == \
           {k: v for k, v in after.completion.items() if k in untouched_keys}


def test_new_work_never_lands_on_occupied_machine_time(book):
    """Stage 2 in miniature: plan half the orders, then plan the rest against them."""
    nm, cfg, orders = book
    first, second = _stage_split(orders)
    stage1 = _decoded(nm, cfg, first)
    busy = _busy_machine_ivs(stage1)
    op_busy = {}
    for seg in stage1.segments:
        if seg.operator:
            op_busy.setdefault(seg.operator, []).append((seg.start, seg.end))
    nm2 = _replace(nm, calendar=_replace(
        nm.calendar, machine_busy=busy,
        operator_busy={k: tuple(sorted(v)) for k, v in op_busy.items()}))
    stage2 = _decoded(nm2, cfg, second)

    # Guard against a vacuous split (see _stage_split's docstring): both stages must
    # produce real machine-bound work, and they must share at least one machine —
    # otherwise the assertions below would pass by having nothing to check.
    stage1_machines = _machine_ids(stage1)
    stage2_machines = _machine_ids(stage2)
    assert stage1_machines, "stage 1 produced no machine-bound work — split is degenerate"
    assert stage2_machines, "stage 2 produced no machine-bound work — split is degenerate"
    shared = stage1_machines & stage2_machines
    assert shared, (
        f"stage 1 ({stage1_machines}) and stage 2 ({stage2_machines}) never contend "
        "for the same machine — split is degenerate")

    for seg in stage2.segments:
        if not seg.machine_id:
            continue
        for bs, be in busy.get(seg.machine_id, ()):
            assert seg.end <= bs or seg.start >= be, (
                f"{seg.order_key} op {seg.op_seq} overlaps existing work on "
                f"{seg.machine_id}: {seg.start}-{seg.end} vs {bs}-{be}")


def test_no_operator_is_double_booked_across_the_two_stages(book):
    nm, cfg, orders = book
    first, second = _stage_split(orders)
    stage1 = _decoded(nm, cfg, first)
    op_busy = {}
    for seg in stage1.segments:
        if seg.operator:
            op_busy.setdefault(seg.operator, []).append((seg.start, seg.end))
    nm2 = _replace(nm, calendar=_replace(
        nm.calendar, machine_busy=_busy_machine_ivs(stage1),
        operator_busy={k: tuple(sorted(v)) for k, v in op_busy.items()}))
    stage2 = _decoded(nm2, cfg, second)

    assert op_busy, "stage 1 booked no operator — split is degenerate"
    stage2_ops = {seg.operator for seg in stage2.segments if seg.operator}
    assert stage2_ops, "stage 2 booked no operator — split is degenerate"
    assert op_busy.keys() & stage2_ops, (
        f"stage 1 operators ({sorted(op_busy)}) and stage 2 operators "
        f"({sorted(stage2_ops)}) never overlap — split is degenerate")

    for seg in stage2.segments:
        if not seg.operator:
            continue
        for bs, be in op_busy.get(seg.operator, ()):
            assert seg.end <= bs or seg.start >= be, (
                f"{seg.operator} is in two places at once: "
                f"{seg.start}-{seg.end} vs {bs}-{be}")


def test_a_job_is_never_split_across_two_occupied_blocks(book):
    """The gap rule: one continuous engagement per operation, so the 90-minute
    setup is never paid twice."""
    nm, cfg, orders = book
    first, second = _stage_split(orders)
    stage1 = _decoded(nm, cfg, first)
    busy = _busy_machine_ivs(stage1)
    nm2 = _replace(nm, calendar=_replace(nm.calendar, machine_busy=busy))
    stage2 = _decoded(nm2, cfg, second)

    assert _machine_ids(stage1), "stage 1 produced no machine-bound work — split is degenerate"
    assert _machine_ids(stage2), "stage 2 produced no machine-bound work — split is degenerate"
    assert _machine_ids(stage1) & _machine_ids(stage2), (
        "stage 1 and stage 2 never contend for the same machine — split is degenerate")

    spans = {}
    for seg in stage2.segments:
        if not seg.machine_id:
            continue
        k = (seg.order_key, seg.op_seq)
        lo, hi = spans.get(k, (seg.start, seg.end))
        spans[k] = (min(lo, seg.start), max(hi, seg.end))
    for (key, seq), (lo, hi) in spans.items():
        for bs, be in busy.get(
                next(s.machine_id for s in stage2.segments
                     if (s.order_key, s.op_seq) == (key, seq)), ()):
            assert not (lo < be and bs < hi), (
                f"{key} op {seq} spans an existing job ({lo}-{hi} across {bs}-{be})")


from ppc_engine.scheduler.flow_scheduler import _place_operation


def test_place_operation_routes_through_free_runs_not_a_direct_lay(shop):
    """Isolated proof that ``_place_operation``'s machine loop is wired to
    ``_lay_in_free_run`` (and so consults ``ShopCalendar.free_runs``), independent of
    the decode-level fixture's own trap: the sample workbook's routing timing and
    single-operator-per-shift-per-machine structure mean two REAL orders never
    actually contend for the same machine slot, so a decode()-level test can pass
    unchanged whether or not this wiring exists (measured — confirmed by mutation).

    This test calls ``_place_operation`` directly with a hand-built ``machine_busy``
    block sitting exactly where the op would otherwise land, and a staffing board with
    NO operator-busy time at all — so nothing but the machine-occupancy path can be
    what moves the placement.
    """
    nm, cfg, order, op = shop
    mid = op.machine_options[0]
    machine_free = {mid: cfg.plan_start}
    board = StaffingBoard(build_machine_pools(nm))
    free = _place_operation(op, order, cfg.plan_start, machine_free, board, nm, cfg)
    assert free["machine_id"] == mid

    # Block exactly the window the op would naturally use. Operator-busy time is left
    # completely empty, so an operator is always free — the only thing that can push
    # this placement later is the machine occupancy itself.
    nm2 = _replace(nm, calendar=_replace(
        nm.calendar, machine_busy={mid: ((free["start"], free["end"]),)}))
    board2 = StaffingBoard(build_machine_pools(nm2))
    machine_free2 = {mid: cfg.plan_start}
    laid = _place_operation(op, order, cfg.plan_start, machine_free2, board2, nm2, cfg)

    assert laid["machine_id"] == mid
    assert laid["start"] >= free["end"], (
        "the op landed inside a block an earlier planning stage already committed — "
        "_place_operation is not consulting ShopCalendar.free_runs")


from ppc_engine.scheduler.flow_scheduler import _lay_in_free_run


def test_a_four_hour_op_skips_a_one_hour_gap_and_lands_whole_in_the_next_run(shop):
    """The gap rule itself, proven by a concrete case the review confirmed by hand:
    CNC1 busy 09:00-10:00 and 15:00-16:00 leaves a one-hour run (10:00, 15:00) that
    cannot hold a four-hour op whole, and a wide-open run (16:00, None) after it.
    The op must skip the one-hour run entirely and land at 10:00-14:00 — inside the
    (10:00, 15:00) run, never touching either block.

    This is deliberately a case NEITHER of these two mutations survives (both
    passed 25/25 before this test existed — see task-5-report.md fix round 1):
      - `deadline=run_end` -> `deadline=None` in `_lay_in_free_run`: the op would
        run straight across the 15:00-16:00 block instead of stopping at 14:00.
      - `free_runs(...)` -> `free_runs(...)[-1:]` (only the LAST/open-ended run
        ever tried): the op would jump to 16:00-20:00 instead of using the gap.
    """
    nm, cfg, order, op = shop
    mid = op.machine_options[0]
    machine = nm.machines[mid]
    day = cfg.plan_start.date()
    block_a = (D(day.year, day.month, day.day, 9), D(day.year, day.month, day.day, 10))
    block_b = (D(day.year, day.month, day.day, 15), D(day.year, day.month, day.day, 16))
    nm2 = _replace(nm, calendar=_replace(nm.calendar, machine_busy={mid: (block_a, block_b)}))
    board = StaffingBoard(build_machine_pools(nm2))

    laid = _lay_in_free_run(machine, cfg.plan_start, 240.0, order, op, int(order.qty),
                            board, nm2, cfg)

    assert laid is not None
    assert laid["start"] == D(day.year, day.month, day.day, 10)
    assert laid["end"] == D(day.year, day.month, day.day, 14)
    for seg in laid["segments"]:
        assert seg.start >= block_a[1] and seg.end <= block_b[0], (
            f"segment {seg.start}-{seg.end} spills into a block "
            f"({block_a} or {block_b})")


def test_frozen_and_occupancy_together_is_rejected(book):
    """Stage 2 never has frozen operations (a brand-new order can't be
    half-finished), and neither `_lay_frozen` nor `_preplace_frozen` consult
    `free_runs` — so the combination must be refused rather than silently letting
    a frozen op land on occupied time."""
    nm, cfg, orders = book
    mid = next(iter(nm.machines))
    nm2 = _replace(nm, calendar=_replace(
        nm.calendar, machine_busy={mid: ((cfg.plan_start, cfg.plan_start + timedelta(hours=1)),)}))
    fake_frozen = [{"order_key": orders[0].key, "op_seq": 1, "machine_id": mid,
                    "operator": "Anyone", "remaining_qty": 1,
                    "prev_start": cfg.plan_start, "prev_end": cfg.plan_start}]
    with pytest.raises(ValueError, match="occupancy and frozen"):
        decode(orders, [o.key for o in orders], nm2, cfg, frozen=fake_frozen)


def test_free_runs_returns_identical_output_on_the_second_call(shop):
    """The cache must never change what `free_runs` answers — same query, same
    answer, whether or not the merge has already been cached."""
    nm, cfg, _order, op = shop
    mid = op.machine_options[0]
    cal = ShopCalendar(machine_busy={mid: (
        (D(2025, 3, 5, 8), D(2025, 3, 6, 8)),
        (D(2025, 3, 6, 8), D(2025, 3, 7, 8)),
        (D(2025, 3, 8, 9), D(2025, 3, 8, 12)),
    )})
    first = cal.free_runs(mid, D(2025, 3, 3, 8))
    second = cal.free_runs(mid, D(2025, 3, 3, 8))
    assert first == second
    # A different `after` on the SAME (now-cached) calendar must still be correct.
    third = cal.free_runs(mid, D(2025, 3, 6, 20))
    assert third == [(D(2025, 3, 7, 8), D(2025, 3, 8, 9)), (D(2025, 3, 8, 12), None)]


def test_free_runs_is_correct_and_cache_stable_for_unsorted_input():
    """The cache stores MERGED blocks, computed from whatever order the blocks were
    given in — unsorted input must merge correctly, and the cached (already-sorted)
    result must still be returned identically on a repeat call."""
    cal = ShopCalendar(machine_busy={"CNC3": (
        (D(2025, 3, 6, 20), D(2025, 3, 8, 8)),      # given out of order
        (D(2025, 3, 5, 8), D(2025, 3, 6, 8)),
        (D(2025, 3, 6, 8), D(2025, 3, 7, 8)),
    )})
    expected = [(D(2025, 3, 3, 8), D(2025, 3, 5, 8)), (D(2025, 3, 8, 8), None)]
    assert cal.free_runs("CNC3", D(2025, 3, 3, 8)) == expected
    assert cal.free_runs("CNC3", D(2025, 3, 3, 8)) == expected  # cached, same answer


def test_replacing_machine_busy_never_reuses_a_stale_cache():
    """A `dataclasses.replace()` that changes `machine_busy` must never carry the
    OLD calendar's cached merge along with it — the load-bearing reason
    `_merged_busy_cache` is `init=False` (see calendar.py)."""
    c1 = ShopCalendar(machine_busy={"CNC3": ((D(2025, 3, 3, 9), D(2025, 3, 3, 10)),)})
    assert c1.free_runs("CNC3", D(2025, 3, 3, 8)) == [
        (D(2025, 3, 3, 8), D(2025, 3, 3, 9)), (D(2025, 3, 3, 10), None)]

    c2 = _replace(c1, machine_busy={"CNC3": ((D(2025, 3, 3, 14), D(2025, 3, 3, 15)),)})
    assert c2.free_runs("CNC3", D(2025, 3, 3, 8)) == [
        (D(2025, 3, 3, 8), D(2025, 3, 3, 14)), (D(2025, 3, 3, 15), None)]
    # And the original calendar's own cache is untouched by having built c2.
    assert c1.free_runs("CNC3", D(2025, 3, 3, 8)) == [
        (D(2025, 3, 3, 8), D(2025, 3, 3, 9)), (D(2025, 3, 3, 10), None)]
