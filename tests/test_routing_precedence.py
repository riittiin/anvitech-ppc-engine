"""The plan must ALWAYS follow the routing in Item's Process Master (live 2026-08-09).

A director opened the shift-wise schedule and found CNC FIRST SIDE running on 11-08
while CNC SECOND SIDE, VMC FIRST SIDE, DEBURING and INSP — every step that eats its
output — had already run on 09-08 and 10-08. On a clean book the order is perfect;
the violation only appears once work is IN PROGRESS.

Root cause: `flow_scheduler._preplace_frozen` pinned every part-finished op onto its
machine at `machine_free[machine]`, grouped BY MACHINE, and never once consulted the
owning order's own predecessor (`ready_of`). So each in-progress step landed in its
machine's first free slot independently of the routing: a free CNC4 started step 3 on
Saturday while a busy CNC5 could not start step 2 until Monday. The main scheduling
loop has both a precedence gate and a piece-flow guard; the frozen path had neither.

Invariant, per (SO number, item code), for consecutive routing steps a then b:
    start(b) >  start(a)     b cannot begin before, or with, the step that feeds it
    end(b)   >= end(a)       b cannot finish before a finishes
Overlap (Rule 5) deliberately lets b START before a ENDS, and pacing deliberately
lets a fast b END exactly with a. Both stay legal; neither is flagged.
"""
import importlib
from datetime import date, datetime, timedelta

import pytest

from engine import book_store, freeze
from engine.models import Masters, Order, Process, Routing, ScheduleEntry
from engine.new_engine import routing_order_violations
from tests.new_sample_workbook import build_new_sample_bytes, ITEM_A, ITEM_B

T0 = datetime(2025, 3, 3, 8, 0)


def _masters_two_steps():
    return Masters(routings={"X": Routing(
        item_code="X", description="", customer="", rm_type="", moq=None, processes=[
        Process(seq=1, name="CNC FIRST SIDE", cycle_time=5.0, total_time=5.0,
                suggested_machine="CNC1", allotted_machine="CNC1"),
        Process(seq=2, name="CNC SECOND SIDE", cycle_time=5.0, total_time=5.0,
                suggested_machine="CNC2", allotted_machine="CNC2"),
    ])})


def _entry(seq, name, machine, start, end):
    return ScheduleEntry(batch_id="B1", item_code="X", process_seq=seq,
                         process_name=name, machine=machine, qty=10,
                         occupancy_min=(end - start).total_seconds() / 60.0,
                         start=start, end=end, so_refs=["SO1"])


# --------------------------------------------------------------------------- #
# The pure checker
# --------------------------------------------------------------------------- #
def test_a_later_step_running_before_the_step_that_feeds_it_is_flagged():
    m = _masters_two_steps()
    first = _entry(1, "CNC FIRST SIDE", "CNC1", T0 + timedelta(days=2), T0 + timedelta(days=3))
    second = _entry(2, "CNC SECOND SIDE", "CNC2", T0, T0 + timedelta(hours=6))
    hits = routing_order_violations([first, second], m)
    assert hits, "step 2 starting two days before step 1 must be reported"
    assert hits[0]["kind"] == "ROUTING_ORDER_VIOLATION"
    assert "CNC SECOND SIDE" in hits[0]["message"]
    assert "CNC FIRST SIDE" in hits[0]["message"]


def test_two_consecutive_steps_starting_at_the_same_instant_are_flagged():
    """The live shape once machines were free: five in-progress steps all pinned to
    the same minute. Deburring cannot begin the instant CNC begins."""
    m = _masters_two_steps()
    a = _entry(1, "CNC FIRST SIDE", "CNC1", T0, T0 + timedelta(hours=8))
    b = _entry(2, "CNC SECOND SIDE", "CNC2", T0, T0 + timedelta(hours=9))
    assert routing_order_violations([a, b], m)


def test_overlap_is_legal_and_never_flagged():
    """Rule 5 lets a successor start while its predecessor is still cutting."""
    m = _masters_two_steps()
    a = _entry(1, "CNC FIRST SIDE", "CNC1", T0, T0 + timedelta(hours=8))
    b = _entry(2, "CNC SECOND SIDE", "CNC2", T0 + timedelta(hours=2),
               T0 + timedelta(hours=10))
    assert routing_order_violations([a, b], m) == []


def test_a_paced_successor_ending_exactly_with_its_predecessor_is_legal():
    """Overlap pacing deliberately stretches a fast op's end to its predecessor's."""
    m = _masters_two_steps()
    end = T0 + timedelta(hours=8)
    a = _entry(1, "CNC FIRST SIDE", "CNC1", T0, end)
    b = _entry(2, "CNC SECOND SIDE", "CNC2", T0 + timedelta(hours=2), end)
    assert routing_order_violations([a, b], m) == []


def test_a_successor_finishing_before_its_predecessor_is_flagged():
    m = _masters_two_steps()
    a = _entry(1, "CNC FIRST SIDE", "CNC1", T0, T0 + timedelta(hours=8))
    b = _entry(2, "CNC SECOND SIDE", "CNC2", T0 + timedelta(hours=1),
               T0 + timedelta(hours=2))
    hits = routing_order_violations([a, b], m)
    assert any("finishes before" in h["message"] for h in hits)


# --------------------------------------------------------------------------- #
# The real bug: a part-finished order must still run in routing order
# --------------------------------------------------------------------------- #
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient   # noqa: E402


def _api(monkeypatch):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")   # what production runs
    import api.main as m
    importlib.reload(m)
    return m


def _client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return c


def _plan_with_work_in_progress(m, c):
    """Orders part-finished at every step, with an applied plan on file — the state
    that pins in-progress ops. ITEM_B contends for the same CNCs, so the first step
    cannot start immediately while the later steps' machines sit free: exactly the
    shape that produced the live inversion."""
    book_store.save_masters_bytes(build_new_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 120, date(2025, 3, 20)),
        Order("SO2", ITEM_A, ITEM_A, 120, date(2025, 3, 21)),
        Order("SO3", ITEM_B, ITEM_B, 300, date(2025, 3, 20)),
        Order("SO4", ITEM_B, ITEM_B, 300, date(2025, 3, 21)),
    ])
    masters = m._current_masters()
    c.post("/run", json={"persist": False})
    book_store.save_last_applied_schedule(
        freeze.schedule_projection(m._PLAN_CACHE["artifacts"]["plan_run"].schedule))

    op = sorted({o.name for o in masters.operators})[0]
    for so, item in (("SO1", ITEM_A), ("SO2", ITEM_A), ("SO3", ITEM_B), ("SO4", ITEM_B)):
        procs = masters.routings[item].processes
        # A descending ladder: every step part-done, none ahead of the step above it.
        for k, p in enumerate(procs[:-1]):
            r = c.post("/actuals", json={
                "so_no": so, "item_code": item, "item_name": item,
                "entry_date": "2025-03-10", "process": p.name,
                "qty_produced": max(20, 100 - 30 * k), "qty_rejected": 0,
                "operator": op, "shift": "1st shift", "machine": "",
                "downtime_min": 0, "remarks": ""})
            assert r.status_code == 200, r.text
    frozen_rows = m._compute_and_store_frozen()
    assert frozen_rows, "the setup must actually produce frozen in-progress ops"
    m._PLAN_CACHE.update(key=None, result=None)
    c.post("/run", json={"persist": False})
    return masters, m._PLAN_CACHE["artifacts"]["plan_run"].schedule


def test_an_order_already_in_progress_still_runs_in_routing_order():
    with pytest.MonkeyPatch.context() as mp:
        m = _api(mp)
        c = _client(m)
        masters, schedule = _plan_with_work_in_progress(m, c)
        hits = routing_order_violations(schedule, masters)
        assert hits == [], "\n".join(h["message"] for h in hits)


def test_a_clean_book_runs_in_routing_order():
    with pytest.MonkeyPatch.context() as mp:
        m = _api(mp)
        c = _client(m)
        book_store.save_masters_bytes(build_new_sample_bytes())
        book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 120, date(2025, 3, 20)),
                               Order("SO3", ITEM_B, ITEM_B, 300, date(2025, 3, 20))])
        masters = m._current_masters()
        c.post("/run", json={"persist": False})
        schedule = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
        assert routing_order_violations(schedule, masters) == []


def test_the_validation_report_surfaces_a_routing_order_violation():
    """Checked in production too, not only in tests — non-blocking, sitting beside
    the operator-qualification invariant."""
    with pytest.MonkeyPatch.context() as mp:
        m = _api(mp)
        _client(m)
        book_store.save_masters_bytes(build_new_sample_bytes())
        book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])
        masters = m._current_masters()
        bad = [
            ScheduleEntry(batch_id="B1", item_code=ITEM_A, process_seq=1,
                          process_name="CNC FIRST SIDE", machine="CNC1", qty=10,
                          occupancy_min=60, start=T0 + timedelta(days=2),
                          end=T0 + timedelta(days=3), so_refs=["SO1"]),
            ScheduleEntry(batch_id="B1", item_code=ITEM_A, process_seq=2,
                          process_name="VMC FIRST SIDE", machine="VMC1", qty=10,
                          occupancy_min=60, start=T0, end=T0 + timedelta(hours=6),
                          so_refs=["SO1"]),
        ]
        rep = m._report_for_book(masters, [], schedule=bad,
                                 config=m._load_plan_config())
        kinds = [r[0] for r in rep["rows"]]
        assert "ROUTING_ORDER_VIOLATION" in kinds
