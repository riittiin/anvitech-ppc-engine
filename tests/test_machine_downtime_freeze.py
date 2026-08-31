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


def _release_scenario(masters):
    """Shared end-to-end scenario: one part-done step, applied to CNC1, which then
    goes down 03-03 -> 03-05 -- exactly across when the step was due to run."""
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
    return line, batches, step, frozen, reserved


def test_a_released_step_is_scheduled_and_never_lands_on_the_down_machine(masters, monkeypatch):
    """End to end: the step is planned, no segment of it runs on CNC1, and the RELEASE
    itself is what caused it -- not merely that the calendar-level reservation alone
    kept work off CNC1 during the break, which is all the original version of this
    test proved (code review finding, 2026-08-31: it passed unchanged with the
    release rule reverted, because ``run()``'s only alternative machine here IS CNC1,
    so the step simply waits out the break either way -- see the reverted-check below
    for what actually distinguishes the two).

    Intercepts the exact call ``run()`` makes to ``_ppc_frozen`` -- proving (a) it
    passed the plan's OWN start date, not a stale or default one, and (b) calling the
    real function again with exactly those inputs returns an empty frozen set, i.e.
    the pin is provably gone, not just coincidentally unused this run.

    Also pins the 2026-08-11 batch-quantity class across a release: the step must be
    sized from the BATCH's remaining (40 -- no actuals were recorded against the
    batch itself), never the punched SO line's own remainder (20)."""
    line, batches, step, frozen, reserved = _release_scenario(masters)

    captured = {}
    real_ppc_frozen = new_engine._ppc_frozen
    def spy(rows, orders, batch_by_key, m, plan_start_date):
        captured["args"] = (rows, orders, batch_by_key, m, plan_start_date)
        return real_ppc_frozen(rows, orders, batch_by_key, m, plan_start_date)
    monkeypatch.setattr(new_engine, "_ppc_frozen", spy)

    entries = new_engine.run(batches, CONF, None, masters,
                             reserved=reserved, frozen=frozen)
    assert entries, "the order must still be planned"

    assert "args" in captured, "run() never called _ppc_frozen with a frozen set present"
    c_rows, c_orders, c_bbk, c_masters, c_plan_start = captured["args"]
    assert c_plan_start == CONF.plan_start_date, (
        "run() passed the wrong plan-start date into _ppc_frozen")
    # The discriminating check: re-run the REAL release logic on run()'s own inputs.
    # (Proven to actually discriminate: with `_machine_down_in_window` stubbed to
    # always return False -- i.e. the release rule reverted -- this same call
    # returns a non-empty FrozenOp list for CNC1.)
    assert real_ppc_frozen(c_rows, c_orders, c_bbk, c_masters, c_plan_start) == []

    on_down = [e for e in entries
               if e.machine == "CNC1"
               and e.start.date() <= date(2025, 3, 5)
               and e.end.date() >= date(2025, 3, 3)]
    assert on_down == [], f"work scheduled on a machine that is out of service: {on_down}"

    step_entries = [e for e in entries
                    if e.batch_id == batches[0].batch_id and e.process_seq == step.seq]
    assert step_entries, "the released step must still appear in the plan"
    assert max(e.qty for e in step_entries) >= 40, (
        f"released step sized from the punched line's remainder (20), not the "
        f"batch's remaining (40): {[e.qty for e in step_entries]}")


def test_optimize_sequence_and_tune_release_the_pin_from_their_own_wiring(masters, monkeypatch):
    """optimize_sequence() and tune() each build their OWN nm/orders/batch_by_key and
    call _ppc_frozen independently of run() -- the run() test above exercises none of
    that. Pins that each call (a) uses the configured plan-start date and (b) actually
    empties the frozen set for the down machine, from each function's own wiring."""
    line, batches, step, frozen, reserved = _release_scenario(masters)

    captured = []
    real_ppc_frozen = new_engine._ppc_frozen
    def spy(rows, orders, batch_by_key, m, plan_start_date):
        out = real_ppc_frozen(rows, orders, batch_by_key, m, plan_start_date)
        captured.append((plan_start_date, out))
        return out
    monkeypatch.setattr(new_engine, "_ppc_frozen", spy)

    new_engine.optimize_sequence([line], CONF, masters, reserved=reserved,
                                 budget_evals=5, frozen=frozen)
    assert captured, "optimize_sequence() never called _ppc_frozen"
    assert all(d == CONF.plan_start_date for d, _ in captured)
    assert all(out == [] for _, out in captured)

    captured.clear()
    new_engine.tune([line], CONF, masters, reserved=reserved,
                    budget_per_eval=5, frozen=frozen)
    assert captured, "tune() never called _ppc_frozen"
    assert all(d == CONF.plan_start_date for d, _ in captured)
    assert all(out == [] for _, out in captured)
