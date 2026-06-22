"""Working-time calendar used by Rule 6.

Models the shop's working windows so the scheduler advances real machine-busy
time across only working periods:
  * Two shifts run back-to-back: 1st 08:00-19:00, 2nd 19:00-05:00 (next day),
    i.e. a continuous 21h window from 08:00 to 05:00 the following morning.
  * Thursdays are weekly off; holidays are off (WorkCalendar).

``WorkClock.advance(start, minutes)`` returns the datetime reached after
consuming ``minutes`` of working time from ``start`` (clamping ``start`` forward
to the next working window first). ``advance(start, 0)`` therefore snaps a time
onto the next valid working instant.
"""
from __future__ import annotations

from datetime import datetime, timedelta


class WorkClock:
    def __init__(self, calendar, config):
        self.cal = calendar
        self.start_hour = config.first_shift_start_hour     # 8
        self.end_hour = config.second_shift_end_hour        # 5 (next day)

    def _window_for_day(self, d):
        """Window opened on working day ``d``: [d 08:00, (d+1) 05:00]."""
        if not self.cal.is_working_day(d):
            return None
        start = datetime(d.year, d.month, d.day, self.start_hour)
        end = datetime(d.year, d.month, d.day) + timedelta(days=1, hours=self.end_hour)
        return start, end

    def _next_window(self, cursor):
        """First working window whose end is strictly after ``cursor``.

        Starts one day back to catch the early-morning (00:00-05:00) spillover of
        the previous working day's 2nd shift."""
        d = (cursor - timedelta(days=1)).date()
        for _ in range(800):  # generous cap; guards against an all-off calendar
            w = self._window_for_day(d)
            if w and w[1] > cursor:
                return w
            d = d + timedelta(days=1)
        raise RuntimeError("No working window found within horizon")

    def advance(self, start: datetime, minutes: float) -> datetime:
        cursor = start
        remaining = float(minutes)
        # Clamp the start onto a working window.
        win_start, win_end = self._next_window(cursor)
        if cursor < win_start:
            cursor = win_start
        while True:
            avail = (win_end - cursor).total_seconds() / 60.0
            if remaining <= avail + 1e-9:
                return cursor + timedelta(minutes=remaining)
            remaining -= avail
            win_start, win_end = self._next_window(win_end)
            cursor = win_start

    def working_minutes_between(self, a: datetime, b: datetime) -> float:
        """Working minutes in [a, b] — the time a machine was *available* between
        two points (used to measure idle/utilization, skipping nights & off days)."""
        if a is None or b is None or b <= a:
            return 0.0
        total = 0.0
        win_start, win_end = self._next_window(a)
        guard = 0
        while win_start < b and guard < 2000:
            guard += 1
            s = max(win_start, a)
            e = min(win_end, b)
            if e > s:
                total += (e - s).total_seconds() / 60.0
            win_start, win_end = self._next_window(win_end)
        return total
