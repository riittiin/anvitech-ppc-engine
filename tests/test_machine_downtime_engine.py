"""A machine marked out of service runs nothing on those days.

Enforced in ONE place — ``ppc_engine.worktime.iter_windows`` — which is the single
window source for the main decode loop AND both frozen-op paths, so all three are
covered by construction.
"""
from datetime import date, datetime

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.resources import Machine, MachineKind, Shift
from ppc_engine.worktime import iter_windows

CNC = Machine("CNC3", "CNC lathe", MachineKind.MACHINING, 19.5)
MANUAL = Machine("MW1", "Manual Washing", MachineKind.MANUAL, 9.5)
CFG = PlanConfig(plan_start=datetime(2026, 9, 3, 8, 0), week_anchor=None)

# 2026-09-03 is a Thursday (the weekly off), so the plan really starts Friday the 4th.
MON = date(2026, 9, 7)


def _days(machine, calendar, start, n):
    """The shift_dates of the first ``n`` windows from ``start``."""
    out = []
    for win in iter_windows(machine, start, calendar, CFG):
        out.append((win.shift_date, win.shift))
        if len(out) >= n:
            break
    return out


def test_a_clean_calendar_is_unchanged():
    """The whole-plan guard: no downtime on file == today's behaviour."""
    cal = ShopCalendar()
    assert cal.machine_downtime == {}
    got = _days(CNC, cal, datetime(2026, 9, 4, 8, 0), 4)
    assert got == [(date(2026, 9, 4), Shift.FIRST), (date(2026, 9, 4), Shift.SECOND),
                   (date(2026, 9, 5), Shift.FIRST), (date(2026, 9, 5), Shift.SECOND)]


def test_a_down_day_yields_no_window_at_all():
    cal = ShopCalendar(machine_downtime={"CNC3": frozenset({date(2026, 9, 4)})})
    got = _days(CNC, cal, datetime(2026, 9, 4, 8, 0), 2)
    assert all(d != date(2026, 9, 4) for d, _ in got)
    assert got[0] == (date(2026, 9, 5), Shift.FIRST)


def test_both_shifts_of_a_down_day_are_blocked():
    """A break blocks the first shift AND the night shift anchored on that date —
    the night shift runs 19:00 -> 05:00 the next morning, so this is the one that
    a naive calendar-day check would get wrong."""
    cal = ShopCalendar(machine_downtime={"CNC3": frozenset({date(2026, 9, 4)})})
    got = _days(CNC, cal, datetime(2026, 9, 4, 0, 1), 4)
    assert (date(2026, 9, 4), Shift.FIRST) not in got
    assert (date(2026, 9, 4), Shift.SECOND) not in got


def test_other_machines_are_untouched_on_the_same_day():
    cal = ShopCalendar(machine_downtime={"CNC3": frozenset({date(2026, 9, 4)})})
    other = Machine("CNC4", "CNC lathe", MachineKind.MACHINING, 19.5)
    got = _days(other, cal, datetime(2026, 9, 4, 8, 0), 1)
    assert got[0] == (date(2026, 9, 4), Shift.FIRST)


def test_work_resumes_the_day_the_machine_comes_back():
    """Down 05-09 and 06-09, back on the 7th (a Monday)."""
    cal = ShopCalendar(machine_downtime={
        "CNC3": frozenset({date(2026, 9, 5), date(2026, 9, 6)})})
    got = _days(CNC, cal, datetime(2026, 9, 5, 8, 0), 1)
    assert got[0] == (MON, Shift.FIRST)


def test_a_single_shift_station_can_also_be_marked_down():
    """The engine honours downtime on ANY machine; only the UI picker is filtered
    to CNC/VMC. A manual station has no night shift, so one window disappears."""
    cal = ShopCalendar(machine_downtime={"MW1": frozenset({date(2026, 9, 4)})})
    got = _days(MANUAL, cal, datetime(2026, 9, 4, 8, 0), 1)
    assert got[0] == (date(2026, 9, 5), Shift.FIRST)


def test_is_machine_available_still_respects_the_shop_calendar():
    cal = ShopCalendar(machine_downtime={"CNC3": frozenset({date(2026, 9, 4)})})
    assert cal.is_machine_available("CNC3", date(2026, 9, 5)) is True
    assert cal.is_machine_available("CNC3", date(2026, 9, 4)) is False
    # 2026-09-03 is a Thursday: closed for everyone, downtime or not.
    assert cal.is_machine_available("CNC3", date(2026, 9, 3)) is False
    assert cal.is_machine_available("CNC4", date(2026, 9, 3)) is False


def test_a_holiday_still_closes_every_machine():
    cal = ShopCalendar(holidays=frozenset({date(2026, 9, 4)}))
    assert cal.is_machine_available("CNC3", date(2026, 9, 4)) is False


# --------------------------------------------------------------------------- #
# The reserved-dict split: one dict carries both operator absences and machine
# downtime, and the new engine sorts them apart by asking the machine master.
# --------------------------------------------------------------------------- #
def test_with_unavailability_splits_machines_from_operators():
    from engine import new_engine
    from ppc_engine.domain.masters import Masters
    from ppc_engine.domain.resources import Operator, Role

    masters = Masters(
        machines={"CNC3": CNC},
        operators=(Operator("Anil", Role.OPERATOR, frozenset({"CNC3"}), Shift.FIRST),),
        calendar=ShopCalendar())
    reserved = {
        "CNC3": [(datetime(2026, 9, 5), datetime(2026, 9, 7))],   # a machine id
        "Anil": [(datetime(2026, 9, 8), datetime(2026, 9, 9))],   # a person
    }
    out = new_engine._with_unavailability(masters, reserved)

    assert out.calendar.machine_downtime == {
        "CNC3": frozenset({date(2026, 9, 5), date(2026, 9, 6)})}
    assert out.calendar.leaves == {"Anil": frozenset({date(2026, 9, 8)})}
    # The cached masters object is never mutated.
    assert masters.calendar.machine_downtime == {}
    assert masters.calendar.leaves == {}


def test_with_unavailability_is_a_no_op_for_an_empty_reserved():
    from engine import new_engine
    from ppc_engine.domain.masters import Masters
    masters = Masters(machines={"CNC3": CNC}, calendar=ShopCalendar())
    assert new_engine._with_unavailability(masters, None) is masters
    assert new_engine._with_unavailability(masters, {}) is masters


def test_an_unknown_key_is_treated_as_an_operator():
    """A key that is neither a known machine nor a known operator falls to the
    operator branch — today's behaviour for a stale absence, unchanged."""
    from engine import new_engine
    from ppc_engine.domain.masters import Masters
    masters = Masters(machines={"CNC3": CNC}, calendar=ShopCalendar())
    out = new_engine._with_unavailability(
        masters, {"Ghost": [(datetime(2026, 9, 8), datetime(2026, 9, 9))]})
    assert out.calendar.machine_downtime == {}
    assert "Ghost" in out.calendar.leaves


def test_the_scheduler_fingerprint_records_the_new_semantics():
    """Real work moves when a break is on file, so saved optimizer ranks were
    scored under different semantics and must be flagged stale.

    The exact string is pinned on purpose: any change to plan-affecting semantics
    must bump it, and breaking this test is how a future change is forced to notice.
    Last bumped 2026-09-05 for the revised on-time objective (band 4 -> 0, early
    discounted, linear tardiness term) — ranks saved under the old objective were
    chosen by a different rule and must be re-searched."""
    from engine import new_engine
    # Last bumped 2026-09-22: the placement step no longer leaves a machine idle
    # while a qualified person is free and work is ready (stretch-based staffing,
    # per-machine committed spans), so ranks scored under the old placement are
    # stale on every book.
    assert new_engine.SCHEDULER_FINGERPRINT == "new-engine-v8-no-idle-holes"
