"""An in-progress step must never let its batch skip a step the batch still owes.

Live bug, 2026-09-27 (owner escalation). ``26-27SO206`` and ``26-27SO207`` (item
9611443650) had finished CNC FIRST SIDE; SO207 was part-way through CNC SECOND SIDE.
New orders ``26-27SO219/220/221`` for the same item arrived, untouched. Rule 1 clubbed
all five into one batch. The plan then showed CNC SECOND SIDE for 375 pieces starting
at once, and **no CNC FIRST SIDE at all**: the 266 pieces the three new orders need
cut on CNC FIRST SIDE were in no plan, and their CNC SECOND SIDE was scheduled before
the step that makes them.

Root cause: ``flow_scheduler._preplace_frozen`` pins the in-progress step and then
advances the order past it (``idx_of[key] = oi + 1``), assuming every step before a
step already in progress is finished. For a batch that clubs lines at different
stages that is false, and the skipped step is silently never scheduled. Both
checked invariants stayed quiet: ``batch_quantity_violations`` only looked at steps
that HAD entries, and ``routing_order_violations`` cannot order a step that is absent.

The rule now: the pinned step runs only the pieces that physically exist for it
(owed at this step minus owed at the earliest earlier step the plan has not laid);
the rest go through that earlier step first, in the normal loop.

This fixture passes vacuously under the OLD code only if it is built wrong; each
test below was run against the unfixed scheduler and failed.
"""
from __future__ import annotations

import io
from datetime import date

import pytest

from engine import book_store, freeze, loaders, new_engine
from engine.config import Config
from engine.models import SOLine
from engine.rules import rule1_consolidate
from tests.new_sample_workbook import build_new_sample_bytes, ITEM_A

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
               apply_operator_logic=True, consolidation_window_days=10)


@pytest.fixture()
def masters():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    return loaders.load_all(io.BytesIO(wb))[1]


def _steps(masters):
    return masters.routings[ITEM_A].processes


def _line(masters, so_no, qty, delivery, done=None):
    """An SO line for ITEM_A. ``done`` maps routing position -> pieces punched there;
    None/{} is an untouched line (``process_qty`` None, as ``active_so_lines`` emits)."""
    if not done:
        return SOLine(so_no=so_no, item_code=ITEM_A, item_name="x", qty=qty,
                      delivery_date=delivery, process_qty=None)
    pq = {loaders.normalize_process_name(p.name): qty - done.get(i, 0)
          for i, p in enumerate(_steps(masters))}
    return SOLine(so_no=so_no, item_code=ITEM_A, item_name="x", qty=qty,
                  delivery_date=delivery, process_qty=pq)


def _frozen(masters, batch, lines, pos, machine, good):
    """The frozen set the app derives: the applied plan ran routing position ``pos``
    of this batch on ``machine`` with Alpha; ``good`` maps SO -> pieces punched there."""
    step = _steps(masters)[pos]
    nkey = loaders.normalize_process_name(step.name)
    applied = [{"batch_id": batch.batch_id, "item_code": ITEM_A,
                "process_seq": step.seq, "process_name": step.name,
                "machine": machine, "operator": "Alpha",
                "start": "2025-03-03T08:00:00", "end": "2025-03-03T12:00:00",
                "so_refs": list(batch.source_so_refs)}]
    return freeze.compute_frozen_set(applied, lines,
                                     {(so, ITEM_A, nkey): n for so, n in good.items()},
                                     masters)


def _live_shape(masters):
    """SO-0207: 100 ordered, step 1 finished, 40 through step 2 (in progress).
    SO-0219: 80 ordered, untouched. Same item, same week -> one batch."""
    old = _line(masters, "SO-0207", 100, date(2025, 4, 1), done={0: 100, 1: 40})
    new = _line(masters, "SO-0219", 80, date(2025, 4, 2))
    batches = rule1_consolidate.run([old, new], _CONF, [], masters)
    assert len(batches) == 1, "fixture must club both lines into ONE batch"
    frozen = _frozen(masters, batches[0], [old, new], 1, "VMC1", {"SO-0207": 40})
    assert frozen and frozen[0]["op_seq"] == _steps(masters)[1].seq, \
        "fixture must freeze step 2 (the in-progress step), not step 1"
    entries = new_engine.run(batches, _CONF, None, masters, frozen=frozen)
    return batches, entries


def _at(masters, entries, pos):
    seq = _steps(masters)[pos].seq
    return [e for e in entries if e.process_seq == seq]


def test_the_step_the_new_line_still_owes_is_scheduled(masters):
    """The reported bug: step 1 must run the 80 pieces SO-0219 has not started."""
    _b, entries = _live_shape(masters)
    first = _at(masters, entries, 0)
    assert sum(e.qty for e in first) == 80, (
        f"step 1 plans {sum(e.qty for e in first)} pieces; the new line still owes 80 "
        "on it (before the fix: 0 -- the step was skipped)")


def test_the_pinned_step_runs_only_pieces_that_exist(masters):
    """Step 2 owes 60 (SO-0207) + 80 (SO-0219) = 140. Only SO-0207's 60 are cut and
    waiting; they resume on the pinned machine at once. SO-0219's 80 run after they
    come off step 1 -- never before step 1 has started on them."""
    _b, entries = _live_shape(masters)
    second = sorted(_at(masters, entries, 1), key=lambda e: e.start)
    first = _at(masters, entries, 0)
    assert sum(e.qty for e in second) == 140
    resumed, later = second[0], second[-1]
    assert resumed.qty == 60 and resumed.machine == "VMC1"
    assert resumed.so_refs == ["SO-0207"]
    assert later.qty == 80 and later.so_refs == ["SO-0219"]
    assert later.start > min(e.start for e in first), \
        "the new line's step 2 starts before its step 1 -- the reported inversion"
    assert later.end >= max(e.end for e in first)


def test_the_checks_are_clean_and_not_blind(masters):
    batches, entries = _live_shape(masters)
    assert new_engine.routing_order_violations(entries, masters) == []
    assert new_engine.batch_quantity_violations(entries, batches, masters) == []
    # Non-vacuous: the plan the bug produced (step 1 absent) must be flagged. Before
    # this fix the checker only examined steps that had entries, so it said nothing.
    seq1 = _steps(masters)[0].seq
    broken = [e for e in entries if e.process_seq != seq1]
    rows = new_engine.batch_quantity_violations(broken, batches, masters)
    assert len(rows) == 1 and rows[0]["kind"] == "BATCH_QTY_SHORT"
    assert "80" in rows[0]["message"]


def test_a_single_in_progress_line_is_unchanged(masters):
    """Guard: nothing is owed before the pinned step, so it runs its whole remaining
    as ONE bar, exactly as before the fix."""
    old = _line(masters, "SO-0207", 100, date(2025, 4, 1), done={0: 100, 1: 40})
    batches = rule1_consolidate.run([old], _CONF, [], masters)
    frozen = _frozen(masters, batches[0], [old], 1, "VMC1", {"SO-0207": 40})
    entries = new_engine.run(batches, _CONF, None, masters, frozen=frozen)
    assert sum(e.qty for e in _at(masters, entries, 0)) == 0 or not _at(masters, entries, 0)
    second = _at(masters, entries, 1)
    assert len(second) == 1 and second[0].qty == 60 and second[0].machine == "VMC1"


# --------------------------------------------------------------------------- #
# Guards on each part of the fix (each was reverted alone and this test failed)
# --------------------------------------------------------------------------- #
def test_the_resumed_bar_is_not_stretched_to_the_step_before_it(masters):
    """Display pacing stretches a step's end to its predecessor's. A resumed part is
    fed by pieces that already exist, so its bar ends when its own work ends."""
    _b, entries = _live_shape(masters)
    resumed = next(e for e in _at(masters, entries, 1) if e.resumed)
    assert resumed.end == max(end for _s, end, _o in resumed.op_segments)
    assert "(resume)" in resumed.process_label() and "60" in resumed.notes


def test_two_resumed_steps_in_a_row_follow_the_routing(masters):
    """SO-0207 part-way through step 2 AND step 3, SO-0219 untouched: both resumed
    parts run, and the step-3 part never starts before the step-2 part feeding it."""
    old = _line(masters, "SO-0207", 100, date(2025, 4, 1), done={0: 100, 1: 60, 2: 20})
    new = _line(masters, "SO-0219", 80, date(2025, 4, 2))
    batches = rule1_consolidate.run([old, new], _CONF, [], masters)
    frozen = (_frozen(masters, batches[0], [old, new], 1, "VMC1", {"SO-0207": 60})
              + _frozen(masters, batches[0], [old, new], 2, "MI1", {"SO-0207": 20}))
    assert len(frozen) == 2
    entries = new_engine.run(batches, _CONF, None, masters, frozen=frozen)
    r2 = next(e for e in _at(masters, entries, 1) if e.resumed)
    r3 = next(e for e in _at(masters, entries, 2) if e.resumed)
    assert (r2.qty, r3.qty) == (40, 80)
    assert r3.start > r2.start and r3.end >= r2.end
    assert sum(e.qty for e in _at(masters, entries, 0)) == 80
    assert new_engine.batch_quantity_violations(entries, batches, masters) == []


def test_the_next_step_waits_for_the_resumed_pieces_too(masters):
    """ITEM_B: BANDSAW OS -> CNC SECOND SIDE (CNC1 or CNC2) -> WASHING. 190 pieces are
    back from the vendor and waiting at CNC, 10 are still out. The 190 resume on CNC2
    behind another job; the 10 come back and finish on CNC1 first. WASHING runs the
    whole batch, so it cannot finish before the 190 do."""
    from ppc_engine.scheduler import decode, FrozenOp
    from engine.new_engine import _orders_from_batches, _plan_config
    from ppc_engine.loaders import load_all as new_load
    from tests.new_sample_workbook import ITEM_B
    from dataclasses import replace as _replace
    nm = new_load(io.BytesIO(build_new_sample_bytes())).masters
    # A second CNC1 person on first shift, so CNC1 can run while CNC2 runs (the
    # sample crew has one CNC operator per shift, which serialises the two).
    alpha = next(o for o in nm.operators if o.name == "Alpha")
    nm = _replace(nm, operators=nm.operators + (
        _replace(alpha, name="Echo", qualified_machines=frozenset({"CNC1"})),))
    routing = masters.routings[ITEM_B]
    pq = {loaders.normalize_process_name(p.name): (10 if i == 0 else 200)
          for i, p in enumerate(routing.processes)}
    line = SOLine(so_no="SO-B", item_code=ITEM_B, item_name="x", qty=200,
                  delivery_date=date(2025, 4, 1), process_qty=pq)
    # Another order's job already running on CNC2 (earlier in the previous plan),
    # so SO-B's 190 resume only after it -- well after the 10 have gone through CNC1.
    busy = SOLine(so_no="SO-C", item_code=ITEM_B, item_name="x", qty=500,
                  delivery_date=date(2025, 7, 1),
                  process_qty={k: (0 if i == 0 else 500) for i, k in enumerate(pq)})
    batches = rule1_consolidate.run([line, busy], _CONF, [], masters)
    assert len(batches) == 2
    orders, _ = _orders_from_batches(batches, nm)
    by_so = {b.source_so_refs[0]: (b.batch_id, b.item_code) for b in batches}
    cfg = _plan_config(_CONF)
    cnc = routing.processes[1].seq
    from datetime import timedelta
    fos = [FrozenOp(by_so["SO-C"], cnc, "CNC2", "", 500, cfg.plan_start - timedelta(hours=1)),
           FrozenOp(by_so["SO-B"], cnc, "CNC2", "", 200, cfg.plan_start)]
    sched = decode(orders, [o.key for o in orders], nm, cfg, frozen=fos)
    mine = by_so["SO-B"]
    sched_segments = [s for s in sched.segments if s.order_key == mine]
    resumed = [s for s in sched_segments if s.resume_from is not None]
    assert resumed and {s.machine_id for s in resumed} == {"CNC2"}
    assert resumed[0].qty == 190
    held = [s for s in sched_segments if s.op_seq == cnc and s.resume_from is None]
    assert max(s.end for s in held) < max(s.end for s in resumed), \
        "fixture must make the 10 finish CNC before the 190 do"
    wash = [s for s in sched_segments if s.op_seq == routing.processes[2].seq]
    assert max(s.end for s in wash) >= max(s.end for s in resumed), (
        "washing finished before the 190 resumed pieces left CNC")


def test_no_line_vanishes_from_its_own_plan(masters):
    """A line with every in-house step done (only dispatch left) owes nothing on any
    bar of a narrowed batch; it must still appear, or it would have no date."""
    done = _line(masters, "SO-0100", 50, date(2025, 4, 1), done={0: 50, 1: 50, 2: 50})
    old = _line(masters, "SO-0207", 100, date(2025, 4, 1), done={0: 100, 1: 100, 2: 30})
    new = _line(masters, "SO-0219", 80, date(2025, 4, 2))
    batches = rule1_consolidate.run([done, old, new], _CONF, [], masters)
    assert len(batches) == 1
    frozen = _frozen(masters, batches[0], [done, old, new], 2, "MI1", {"SO-0207": 30})
    entries = new_engine.run(batches, _CONF, None, masters, frozen=frozen)
    assert any(e.resumed for e in entries), "fixture must produce a resumed part"
    listed = {so for e in entries for so in e.so_refs}
    assert {"SO-0100", "SO-0207", "SO-0219"} <= listed
