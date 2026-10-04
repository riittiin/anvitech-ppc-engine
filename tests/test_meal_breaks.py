"""Meal breaks (owner, 2026-10-04): lunch 13:00-13:30 in the 1st shift, dinner
22:00-22:30 in the 2nd. Nobody works in a break, on any machine or station, so a
shift has 630 / 570 working minutes, not 660 / 600.

One definition (engine/config.py) feeds the engine's window walk
(ppc_engine.worktime.iter_windows) and every reporting window
(operator_coverage.working_intervals)."""
from datetime import date, datetime, time

import pytest

from engine import operator_coverage as oc
from engine.config import Config
from engine.new_engine import _plan_breaks, _plan_config
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.resources import Machine, MachineKind
from ppc_engine.worktime import iter_windows

NEW = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)
MONDAY = datetime(2025, 3, 3, 0, 0)


def _cnc():
    return Machine(id="CNC1", type_text="CNC lathe", kind=MachineKind.MACHINING,
                   available_hrs_per_day=19.5)


def _windows(machine, config, day=MONDAY):
    out = []
    for w in iter_windows(machine, day, ShopCalendar(), config):
        if w.shift_date != day.date():
            break
        out.append((w.start.strftime("%H:%M"), w.end.strftime("%d %H:%M"), w.shift.name))
    return out


def test_the_engine_walks_each_shift_as_the_pieces_either_side_of_its_break():
    cfg = _plan_config(NEW)
    assert cfg.breaks == ((time(13, 0), time(13, 30)), (time(22, 0), time(22, 30)))
    assert _windows(_cnc(), cfg) == [
        ("08:00", "03 13:00", "FIRST"), ("13:30", "03 19:00", "FIRST"),
        ("19:00", "03 22:00", "SECOND"), ("22:30", "04 05:00", "SECOND")]


def test_a_single_shift_station_also_stops_for_lunch():
    mi = Machine(id="MI1", type_text="Inspection", kind=MachineKind.INSPECTION,
                 available_hrs_per_day=9.5)
    assert _windows(mi, _plan_config(NEW)) == [
        ("08:00", "03 13:00", "FIRST"), ("13:30", "03 19:00", "FIRST")]


def test_no_breaks_means_the_whole_shift_as_before():
    from dataclasses import replace
    cfg = replace(_plan_config(NEW), breaks=())
    assert _windows(_cnc(), cfg) == [("08:00", "03 19:00", "FIRST"),
                                     ("19:00", "04 05:00", "SECOND")]


def test_a_plan_never_puts_work_in_a_break():
    """The whole sample book, planned with the new engine: no segment of work, on
    any machine, overlaps 13:00-13:30 or 22:00-22:30."""
    from tests.test_new_engine import _decode_book, _old_book
    import io
    from ppc_engine.loaders import load_all as new_load
    from tests.new_sample_workbook import build_new_sample_bytes
    from engine import book_store
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    so_lines, _ = _old_book(wb)
    sched = _decode_book(so_lines, new_load(io.BytesIO(wb)).masters)
    work = [s for s in sched.segments if s.machine_id and s.end > s.start]
    assert work
    for s in work:
        d = s.start.date()
        for bs, be in ((time(13, 0), time(13, 30)), (time(22, 0), time(22, 30))):
            b0, b1 = datetime.combine(d, bs), datetime.combine(d, be)
            assert not (s.start < b1 and s.end > b0), (s.machine_id, s.start, s.end)
    # and the plan does use the hours either side of lunch
    assert any(s.end == datetime.combine(s.start.date(), time(13, 0)) for s in work) or \
        any(s.start == datetime.combine(s.start.date(), time(13, 30)) for s in work)


def test_reports_see_630_and_570_working_minutes():
    assert oc.shift_working_minutes(NEW) == {"1st shift": 630, "2nd shift": 570}
    first, second, _ = oc._shift_windows(NEW)
    assert oc.working_intervals(first, NEW) == [(480, 780), (810, 1140)]
    assert oc.working_intervals(second, NEW) == [(1140, 1320), (1350, 1740)]


def test_the_retired_classic_engine_keeps_the_whole_shift():
    classic = Config(scheduler="classic")
    assert oc.shift_working_minutes(classic) == {"1st shift": 660, "2nd shift": 600}
    first, _s, _m = oc._shift_windows(classic)
    assert oc.working_intervals(first, classic) == [first]


def test_analytics_operator_capacity_leaves_the_break_out():
    from engine.analytics import _shift_hours
    assert _shift_hours("First", NEW) == 10.5
    assert _shift_hours("Second", NEW) == 9.5


def test_machine_capacity_windows_leave_the_break_out():
    from engine.loaders import load_all
    import io
    from tests.sample_workbook import build_sample_bytes
    _, masters = load_all(io.BytesIO(build_sample_bytes()))
    mac = next(m for m in masters.machines.values() if m.is_two_shift(NEW.two_shift_threshold_hours))
    iv = oc.eligible_window(mac, NEW)
    assert sum(e - s for s, e in iv) == 630 + 570


def test_a_break_must_start_before_it_ends():
    bad = Config(scheduler="new", lunch_break_start_min=800, lunch_break_end_min=780)
    with pytest.raises(ValueError):
        bad.validate()


def test_the_plan_breaks_follow_the_config():
    cfg = Config(scheduler="new", lunch_break_start_min=12 * 60, lunch_break_end_min=12 * 60 + 45)
    assert _plan_breaks(cfg)[0] == (time(12, 0), time(12, 45))
