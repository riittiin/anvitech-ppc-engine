"""A part-finished operation pinned to a machine that goes down for maintenance.

Owner's rule (2026-08-31): the pin comes OFF, and the scheduler + optimizer decide
whether rerouting to another machine the Item's Process Master allows is actually
worth it, or whether waiting is cheaper. Nothing is forced.

The release is evaluated at PLAN time (``new_engine._ppc_frozen``), not when the
frozen list is built (``freeze.compute_frozen_set``) — the stored list is only
rebuilt on "Done entering", so a build-time rule would leave the immediate re-plan
showing a stale answer.
"""
from datetime import date, datetime

import pytest

from engine import book_store, freeze, loaders, new_engine, optimize_service
from engine.config import Config
from engine.models import SOLine
from engine.rules import rule1_consolidate
from tests.new_sample_workbook import build_new_sample_bytes

CONF = Config(plan_start_date=date(2025, 3, 3), scheduler="new",
              apply_operator_logic=True, overlap_mode="overlap", overlap_percent=50)


@pytest.fixture
def masters():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    import io
    return loaders.load_all(io.BytesIO(wb))[1]


def _ctx(masters, downtime_rows):
    """(frozen rows, orders, batch_by_key, ppc masters) for one part-done line."""
    item = sorted(masters.routings)[0]
    routing = masters.routings[item]
    step = routing.processes[0]
    n = loaders.normalize_process_name(step.name)
    line = SOLine(so_no="SO1", item_code=item, item_name="X", qty=40,
                  delivery_date=date(2025, 4, 1),
                  process_qty={loaders.normalize_process_name(p.name): 40
                               for p in routing.processes})
    batches = rule1_consolidate.run([line], CONF, [], masters)
    applied = [{
        "batch_id": batches[0].batch_id, "item_code": item,
        "process_seq": step.seq, "process_name": step.name,
        "machine": "CNC1", "operator": "Alpha",
        "start": "2025-03-03T08:00:00", "end": "2025-03-04T12:00:00",
        "so_refs": ["SO1"]}]
    rows = freeze.compute_frozen_set(
        applied, [line], {("SO1", item, n): 60}, masters)
    reserved = optimize_service.downtime_reservations(downtime_rows)
    nm = new_engine._with_unavailability(
        new_engine._apply_app_operators(new_engine._new_masters(False), masters),
        reserved)
    orders, batch_by_key = new_engine._orders_from_batches(batches, nm)
    return rows, orders, batch_by_key, nm


def test_compute_frozen_set_now_records_when_the_step_was_due_to_finish(masters):
    rows, _o, _b, _nm = _ctx(masters, [])
    assert rows and rows[0]["prev_end"] == "2025-03-04T12:00:00"
    assert rows[0]["prev_start"] == "2025-03-03T08:00:00"     # unchanged


def test_no_downtime_keeps_the_pin(masters):
    rows, orders, bbk, nm = _ctx(masters, [])
    out = new_engine._ppc_frozen(rows, orders, bbk, nm, date(2025, 3, 3))
    assert [f.machine_id for f in out] == ["CNC1"]


def test_a_break_over_the_step_window_drops_the_pin(masters):
    """CNC1 down 03-03 -> 04-03, exactly across when this step was due to run."""
    rows, orders, bbk, nm = _ctx(
        masters, [{"machine": "CNC1", "from_date": "2025-03-03",
                   "to_date": "2025-03-04"}])
    assert new_engine._ppc_frozen(rows, orders, bbk, nm, date(2025, 3, 3)) == []


def test_a_break_after_the_step_window_keeps_the_pin(masters):
    """Marking CNC1 down weeks later must not unfreeze today's work on it."""
    rows, orders, bbk, nm = _ctx(
        masters, [{"machine": "CNC1", "from_date": "2025-04-10",
                   "to_date": "2025-04-12"}])
    out = new_engine._ppc_frozen(rows, orders, bbk, nm, date(2025, 3, 3))
    assert [f.machine_id for f in out] == ["CNC1"]


def test_a_break_on_a_different_machine_keeps_the_pin(masters):
    rows, orders, bbk, nm = _ctx(
        masters, [{"machine": "CNC2", "from_date": "2025-03-03",
                   "to_date": "2025-03-04"}])
    out = new_engine._ppc_frozen(rows, orders, bbk, nm, date(2025, 3, 3))
    assert [f.machine_id for f in out] == ["CNC1"]


def test_a_row_with_no_prev_end_does_not_crash(masters):
    """A frozen row stored before this deploy has no prev_end: fall back to
    prev_start, then to the plan start. Never a crash."""
    rows, orders, bbk, nm = _ctx(
        masters, [{"machine": "CNC1", "from_date": "2025-03-03",
                   "to_date": "2025-03-03"}])
    for r in rows:
        r.pop("prev_end", None)
    out = new_engine._ppc_frozen(rows, orders, bbk, nm, date(2025, 3, 3))
    assert out == []                     # the break covers the plan-start day


def test_a_released_step_is_scheduled_and_never_lands_on_the_down_machine(masters):
    """End to end: the step is planned, and no segment of it runs on CNC1."""
    item = sorted(masters.routings)[0]
    routing = masters.routings[item]
    step = routing.processes[0]
    n = loaders.normalize_process_name(step.name)
    line = SOLine(so_no="SO1", item_code=item, item_name="X", qty=40,
                  delivery_date=date(2025, 4, 1),
                  process_qty={loaders.normalize_process_name(p.name): 40
                               for p in routing.processes})
    batches = rule1_consolidate.run([line], CONF, [], masters)
    applied = [{"batch_id": batches[0].batch_id, "item_code": item,
                "process_seq": step.seq, "process_name": step.name,
                "machine": "CNC1", "operator": "Alpha",
                "start": "2025-03-03T08:00:00", "end": "2025-03-04T12:00:00",
                "so_refs": ["SO1"]}]
    frozen = freeze.compute_frozen_set(applied, [line], {("SO1", item, n): 20}, masters)
    reserved = optimize_service.downtime_reservations(
        [{"machine": "CNC1", "from_date": "2025-03-03", "to_date": "2025-03-05"}])
    entries = new_engine.run(batches, CONF, None, masters,
                             reserved=reserved, frozen=frozen)
    assert entries, "the order must still be planned"
    on_down = [e for e in entries
               if e.machine == "CNC1"
               and e.start.date() <= date(2025, 3, 5)
               and e.end.date() >= date(2025, 3, 3)]
    assert on_down == [], f"work scheduled on a machine that is out of service: {on_down}"
