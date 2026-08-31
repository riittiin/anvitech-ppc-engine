"""A machine out of service must not read as idle-and-available, and the delay
report must not blame the crew for it."""
from datetime import date, datetime, timedelta

from engine import analytics
from engine.config import Config
from engine.models import (Machine, Masters, Process, Routing, ScheduleEntry,
                           WorkCalendar)

CFG = Config(plan_start_date=date(2025, 3, 3), apply_operator_logic=False)
DOWN = [{"machine": "CNC1", "from_date": "2025-03-04", "to_date": "2025-03-05"}]


def _masters():
    return Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe",
                                  available_hrs_per_day=19.5)},
        routings={"X": Routing("X", "", "", "", None, processes=[
            Process(1, "CNC first side", 1.0, 1.0, "CNC1", "CNC1")])},
        calendar=WorkCalendar())


def _entry(start, end):
    return ScheduleEntry(
        batch_id="B001", item_code="X", process_seq=1,
        process_name="CNC first side", machine="CNC1", qty=10,
        occupancy_min=(end - start).total_seconds() / 60.0,
        start=start, end=end, so_refs=["SO1"])


def test_downtime_none_is_byte_identical_to_omitting_it():
    """An unused feature must change nothing (mirrors
    test_analytics.py::test_absences_default_none_is_byte_identical)."""
    m, e = _masters(), _entry(datetime(2025, 3, 3, 8), datetime(2025, 3, 3, 18))
    assert analytics.build_analytics([e], m, CFG) == \
           analytics.build_analytics([e], m, CFG, None, None, None)
    assert analytics.build_analytics([e], m, CFG) == \
           analytics.build_analytics([e], m, CFG, downtime=[])


def test_available_hours_drop_by_the_down_days():
    m = _masters()
    e = _entry(datetime(2025, 3, 3, 8), datetime(2025, 3, 7, 18))
    base = analytics.build_analytics([e], m, CFG)["machines"][0]["Available (hrs)"]
    down = analytics.build_analytics([e], m, CFG, downtime=DOWN)["machines"][0]["Available (hrs)"]
    assert down < base
    # 04-03 and 05-03 are working days; a two-shift machine runs 08:00 -> 05:00 (21h
    # of clock time per anchored day pair, clipped to the window). Pinned to the exact
    # hand-verified figure (not just "> 20.0") so a naive calendar-day subtraction
    # (clipping day D's window to [D 00:00, D+1 00:00), losing the night shift's tail
    # past midnight) is caught: that variant yields 32.0, not 42.0, and both are > 20.
    assert base - down == 42.0


def test_a_machine_down_for_the_whole_window_reads_zero_available():
    m = _masters()
    e = _entry(datetime(2025, 3, 3, 8), datetime(2025, 3, 3, 18))
    rows = analytics.build_analytics(
        [e], m, CFG,
        downtime=[{"machine": "CNC1", "from_date": "2025-03-01",
                   "to_date": "2025-03-31"}])["machines"]
    assert rows[0]["Available (hrs)"] == 0.0
    assert rows[0]["Utilization %"] is None      # no capacity, not 100%


def test_a_break_on_another_machine_changes_nothing():
    m = _masters()
    e = _entry(datetime(2025, 3, 3, 8), datetime(2025, 3, 7, 18))
    assert analytics.build_analytics([e], m, CFG) == analytics.build_analytics(
        [e], m, CFG, downtime=[{"machine": "VMC9", "from_date": "2025-03-04",
                                "to_date": "2025-03-05"}])


def test_a_malformed_row_is_skipped_not_fatal():
    m = _masters()
    e = _entry(datetime(2025, 3, 3, 8), datetime(2025, 3, 7, 18))
    assert analytics.build_analytics(
        [e], m, CFG, downtime=[{"machine": "CNC1", "from_date": "oops",
                                "to_date": "2025-03-05"}]) == \
           analytics.build_analytics([e], m, CFG)


def test_downtime_still_subtracts_when_the_gated_clock_falls_back():
    """With operator logic ON and nobody qualified on CNC1, ``clock_for`` returns an
    empty-window clock (0 minutes) and ``avail_hrs`` rebuilds the clock from the
    machine's physical window (the existing fallback, see ``test_analytics.py::
    test_used_manual_machine_uncovered_still_shows_utilization``). This is the one
    path where the local ``clock`` variable gets reassigned mid-function — if a future
    edit rebuilt the clock without reassigning it, the downtime subtraction would run
    against the ORIGINAL empty-window clock, whose ``_windows_for_day`` never returns a
    window for any day, so nothing would ever be subtracted and this test would catch
    it (down == base instead of down < base)."""
    m = _masters()
    cfg = Config(plan_start_date=date(2025, 3, 3), apply_operator_logic=True)
    e = _entry(datetime(2025, 3, 3, 8), datetime(2025, 3, 7, 18))
    base = analytics.build_analytics([e], m, cfg)["machines"][0]["Available (hrs)"]
    down = analytics.build_analytics([e], m, cfg, downtime=DOWN)["machines"][0]["Available (hrs)"]
    assert down < base
    assert base - down == 42.0    # same anchored-window math as the gated-off case


def test_a_break_spanning_the_weekly_off_day_only_subtracts_working_days():
    """The shop's Thursday weekly off is not a day the machine could have been running
    anyway, so a break spanning one must not double-subtract it. 05-03 (Wed) and 07-03
    (Fri) are working days; 06-03 (Thu) is the weekly off."""
    m = _masters()
    e = _entry(datetime(2025, 3, 3, 8), datetime(2025, 3, 10, 18))
    assert date(2025, 3, 6).strftime("%A") == "Thursday"
    base = analytics.build_analytics([e], m, CFG)["machines"][0]["Available (hrs)"]
    down = analytics.build_analytics(
        [e], m, CFG,
        downtime=[{"machine": "CNC1", "from_date": "2025-03-05",
                   "to_date": "2025-03-07"}])["machines"][0]["Available (hrs)"]
    # Exactly 2 working days' worth (Wed + Fri), not 3 -- if Thursday were wrongly
    # subtracted too this would read 63.0, not 42.0.
    assert base - down == 42.0
