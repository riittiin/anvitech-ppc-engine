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
