"""WorkClock — shifts and the Thursday/holiday skip used by Rule 6."""
from datetime import datetime, date

from engine.config import Config
from engine.models import WorkCalendar
from engine.worktime import WorkClock


def test_within_single_window():
    clock = WorkClock(WorkCalendar(), Config())
    # 2025-03-05 is a Wednesday (working). 120 min from 08:00 -> 10:00.
    end = clock.advance(datetime(2025, 3, 5, 8, 0), 120)
    assert end == datetime(2025, 3, 5, 10, 0)


def test_skips_thursday():
    cfg = Config()
    clock = WorkClock(WorkCalendar(), cfg)
    # Wed 2025-03-05 08:00, the working window runs to Thu 05:00 (21h = 1260 min,
    # 2nd-shift spillover). 1260 + 60 more min must skip Thu (off) to Fri 09:00.
    end = clock.advance(datetime(2025, 3, 5, 8, 0), 1260 + 60)
    assert end == datetime(2025, 3, 7, 9, 0)   # Friday
    assert end.weekday() != 3                  # not Thursday


def test_skips_holiday():
    cfg = Config()
    cal = WorkCalendar(holidays=[date(2025, 3, 5)])  # make that Wednesday a holiday
    clock = WorkClock(cal, cfg)
    end = clock.advance(datetime(2025, 3, 5, 8, 0), 60)
    # Holiday -> first work happens next working day (Fri; Thu is weekly off).
    assert end == datetime(2025, 3, 7, 9, 0)


def test_advance_zero_snaps_to_working_window():
    clock = WorkClock(WorkCalendar(), Config())
    # 06:00 is before the 08:00 shift start -> snaps forward to 08:00.
    snapped = clock.advance(datetime(2025, 3, 5, 6, 0), 0)
    assert snapped == datetime(2025, 3, 5, 8, 0)
