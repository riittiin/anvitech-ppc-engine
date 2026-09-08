"""The engine layer of the Add New Orders quote (2026-09-08 spec).

Occupancy = what an earlier planning stage already committed. It must be able to
reach the placement step without any existing plan changing by a single minute.
"""
from datetime import date, datetime, time

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
