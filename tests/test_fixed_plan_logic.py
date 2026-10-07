from datetime import date

from engine import fixed_plan
from engine.new_engine import FIXED_PLAN_PREFIX


def _row(machine, start, end, so):
    """A published-plan row (freeze.schedule_projection shape, the fields read here)."""
    return {"machine": machine, "start": start + ":00", "end": end + ":00", "so_refs": [so]}


def test_downtime_waits_names_orders_queued_on_the_machine():
    rows = [_row("CNC3", "2026-10-07T08:00", "2026-10-07T12:00", "SO1"),
            _row("CNC3", "2026-10-05T08:00", "2026-10-05T12:00", "SO0"),   # done before the break
            _row("CNC6", "2026-10-07T08:00", "2026-10-07T12:00", "SO2")]
    down = [{"id": "d1", "machine": "CNC3", "from_date": "2026-10-07", "to_date": "2026-10-08"}]
    w = fixed_plan.downtime_waits(rows, rows, down, known_ids=set(), today=date(2026, 10, 6))
    assert w == [{"machine": "CNC3", "from_date": "2026-10-07", "to_date": "2026-10-08",
                  "orders": ["SO1"]}]


def test_a_job_published_after_the_break_is_not_waiting_on_it():
    """Final review I7: only a job whose PUBLISHED run on that machine overlaps the
    break is held up by it; one published to start after the break ends is not."""
    rows = [_row("CNC3", "2026-10-09T08:00", "2026-10-09T12:00", "SO1"),       # after
            _row("CNC3", "2026-10-06T20:00", "2026-10-07T02:00", "SO2"),       # runs into it
            _row("CNC3", "2026-10-08T22:00", "2026-10-09T03:00", "SO3")]       # runs out of it
    down = [{"id": "d1", "machine": "CNC3", "from_date": "2026-10-07", "to_date": "2026-10-08"}]
    w = fixed_plan.downtime_waits(rows, rows, down, known_ids=set(), today=date(2026, 10, 6))
    assert w[0]["orders"] == ["SO2", "SO3"]


def test_a_break_known_when_the_plan_was_published_raises_nothing():
    rows = [_row("CNC3", "2026-10-07T08:00", "2026-10-07T12:00", "SO1")]
    down = [{"id": "d1", "machine": "CNC3", "from_date": "2026-10-07", "to_date": "2026-10-08"}]
    assert fixed_plan.downtime_waits(rows, rows, down, known_ids={"d1"}, today=date(2026, 10, 6)) == []


def test_a_finished_break_raises_nothing():
    rows = [_row("CNC3", "2026-10-01T08:00", "2026-10-01T12:00", "SO1")]
    down = [{"id": "d1", "machine": "CNC3", "from_date": "2026-10-01", "to_date": "2026-10-02"}]
    assert fixed_plan.downtime_waits(rows, rows, down, known_ids=set(), today=date(2026, 10, 6)) == []


def test_malformed_downtime_rows_are_skipped():
    down = [{"id": "x", "machine": "CNC3", "from_date": "bad", "to_date": "2026-10-08"}]
    assert fixed_plan.downtime_waits([], [], down, known_ids=set(), today=date(2026, 10, 6)) == []


def test_malformed_published_rows_are_skipped():
    rows = [{"machine": "CNC3", "start": "bad", "end": None, "so_refs": ["SO1"]}]
    down = [{"id": "d1", "machine": "CNC3", "from_date": "2026-10-07", "to_date": "2026-10-08"}]
    assert fixed_plan.downtime_waits(rows, rows, down, known_ids=set(), today=date(2026, 10, 6)) == []


def _first_shift(machine, after):
    """The machine's first working window at or after ``after``: 08:00 to 19:00."""
    from datetime import datetime, timedelta
    d = after.date() if after.hour < 19 else after.date() + timedelta(days=1)
    return datetime.combine(d, datetime.min.time()) + timedelta(hours=8), \
        datetime.combine(d, datetime.min.time()) + timedelta(hours=19)


def test_a_job_that_slid_into_the_break_is_named():
    """A0 review I1: the published plan ages between Optimize clicks. A job published
    well before the break whose CURRENT (repaired) run falls inside it is waiting on it."""
    pub = [_row("CNC3", "2026-10-02T08:00", "2026-10-02T12:00", "SO1")]
    rep = [_row("CNC3", "2026-10-07T08:00", "2026-10-07T12:00", "SO1")]
    down = [{"id": "d1", "machine": "CNC3", "from_date": "2026-10-07", "to_date": "2026-10-08"}]
    w = fixed_plan.downtime_waits(pub, rep, down, known_ids=set(), today=date(2026, 10, 6),
                                  first_window=_first_shift)
    assert w and w[0]["orders"] == ["SO1"]


def test_a_job_the_break_pushed_past_its_end_is_named():
    """The repair cannot run a job on a down machine, so a job that was due before the
    break ends now starts in the first working window after it: the break pushed it."""
    pub = [_row("CNC3", "2026-10-07T08:00", "2026-10-07T12:00", "SO1"),
           _row("CNC3", "2026-10-10T08:00", "2026-10-10T12:00", "SO2"),   # after it anyway
           _row("CNC3", "2026-10-09T12:00", "2026-10-09T15:00", "SO3")]   # was due right after it
    rep = [_row("CNC3", "2026-10-09T08:00", "2026-10-09T12:00", "SO1"),
           _row("CNC3", "2026-10-10T08:00", "2026-10-10T12:00", "SO2"),
           _row("CNC3", "2026-10-09T12:00", "2026-10-09T15:00", "SO3")]
    down = [{"id": "d1", "machine": "CNC3", "from_date": "2026-10-07", "to_date": "2026-10-08"}]
    w = fixed_plan.downtime_waits(pub, rep, down, known_ids=set(), today=date(2026, 10, 6),
                                  first_window=_first_shift)
    assert w[0]["orders"] == ["SO1"]


def test_a_job_already_finished_before_the_break_is_not_named():
    """Its published run overlapped the break, but it is done: the current plan has
    nothing left of it on that machine."""
    pub = [_row("CNC3", "2026-10-07T08:00", "2026-10-07T12:00", "SO1")]
    rep = [_row("CNC6", "2026-10-07T08:00", "2026-10-07T12:00", "SO1")]   # its next step
    down = [{"id": "d1", "machine": "CNC3", "from_date": "2026-10-07", "to_date": "2026-10-08"}]
    assert fixed_plan.downtime_waits(pub, rep, down, known_ids=set(), today=date(2026, 10, 6),
                                     first_window=_first_shift) == []


def test_alerts_from_notes_keeps_only_fixed_plan_lines_once():
    notes = ["other", FIXED_PLAN_PREFIX + "a", FIXED_PLAN_PREFIX + "b", FIXED_PLAN_PREFIX + "a"]
    assert fixed_plan.alerts_from_notes(notes) == ["a", "b"]


def test_date_changes_lists_only_moved_orders_biggest_first():
    now = {"S1\x1fI": "2026-10-10", "S2\x1fI": "2026-10-12", "S3\x1fI": "2026-10-20"}
    after = {"S1\x1fI": "2026-10-08", "S2\x1fI": "2026-10-12", "S3\x1fI": "2026-10-25",
             "S4\x1fI": "2026-10-01"}
    assert fixed_plan.date_changes(now, after) == [
        {"so": "S3", "item": "I", "now": "2026-10-20", "after": "2026-10-25", "days": 5},
        {"so": "S1", "item": "I", "now": "2026-10-10", "after": "2026-10-08", "days": -2}]


def test_downtime_alert_text_is_plain():
    t = fixed_plan.downtime_alert_text({"machine": "CNC3", "from_date": "2026-10-07",
                                        "to_date": "2026-10-08", "orders": ["SO1", "SO2"]})
    assert t == ("CNC3 is marked down 07-10-2026 to 08-10-2026 and 2 orders are waiting "
                 "on it (SO1, SO2). Press Optimize to decide whether to wait or move them.")
    assert "—" not in t


def test_downtime_alert_text_for_one_order_is_singular():
    """Browser finding B1: '1 order is waiting ... move them' read wrong."""
    t = fixed_plan.downtime_alert_text({"machine": "CNC3", "from_date": "2026-10-07",
                                        "to_date": "2026-10-08", "orders": ["SO1"]})
    assert t == ("CNC3 is marked down 07-10-2026 to 08-10-2026 and 1 order is waiting "
                 "on it (SO1). Press Optimize to decide whether to wait or move it.")
