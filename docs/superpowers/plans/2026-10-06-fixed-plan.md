# Fixed Plan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Between two admin Optimize clicks, every job keeps its machine and its turn on that machine; "Done entering" only recalculates times from the punches.

**Architecture:** The applied plan's per-op projection (`anvitech:last_applied_schedule`, already written on Apply) becomes the **published plan**. A new `pins=` input to the one decoder (`flow_scheduler.decode`) restricts each published op to its published machine and enforces each machine's published order (strict turn), with a deadlock guard. Every ordinary plan (`_plan`, the incumbent measurement, the report) runs WITH pins; only the Optimize contest runs without them. "Done entering" stops starting a contest: it refreshes the frozen set and re-plans.

**Tech Stack:** Python 3.12, FastAPI, pytest, vanilla JS. Always `python3.12 -m pytest` (system python3 is 3.14 and openpyxl breaks).

**Spec:** `docs/superpowers/specs/2026-10-06-fixed-plan-design.md` (owner-approved 2026-10-06). Read it before Task 1.

## Global Constraints

- Work ONLY in the worktree `~/Desktop/anvitech-fixed-plan` (branch `fixed-plan`, from `origin/main` 9f8e7b3). Never in `~/Desktop/Anvitech Rebuilt` (stale branch with the owner's uncommitted optimizer work).
- Before Task 1 install the missing local dependency: `python3.12 -m pip install xlsxwriter` (the only baseline failure on 9f8e7b3).
- Never push. Pushing to `main` deploys to the live site; the owner decides.
- The one rule: only an applied Optimize (`_optimize_apply`) or an accepted earlier date (`/new-orders/prepone/accept`) changes a job's machine or turn.
- Strict turn (D6): a machine waits for its next published job; the software never reorders a queue.
- Any new placement path goes through `_ready_after` and the piece-flow guard (CLAUDE.md 2026-08-09 rule) — here by reusing `_place_operation`.
- Quantities come from the batch (`Order.process_remaining`), never a published row (2026-08-11 rule).
- `pins=None` / no published plan must be byte-identical to today's plan.
- `SCHEDULER_FINGERPRINT` becomes `"new-engine-v12-fixed-plan"`.
- Copy: plain operator English, no em dashes in user-visible text (2026-08-05 rule).
- A new order may never move an existing order (2026-09-08 rule): queued new orders stay in stage 2.
- Every test that pins a fix must be mutation-checked: revert the fix, see it fail.

## Review Focus

1. **The floor runs a job out of turn** (B punched before A on CNC6): B becomes in-progress (frozen, runs first), A keeps its turn after it, the plan never deadlocks. Test in Task 1 (`test_frozen_op_out_of_turn_does_not_block`).
2. **A routing edit removes the published machine** from a step's options: that step is planned normally on an allowed machine and a "FIXED PLAN:" alert is raised; the plan does not crash. Test in Task 2.
3. **Published rows that reference completed / deleted orders or unknown machines** are ignored without error. Test in Task 2.
4. **A clubbed batch whose membership changed** after publishing (one SO completed, a new line clubbed in) still finds its pin through any shared SO ref. Test in Task 2.
5. **Empty / missing published plan** (fresh install, test stores): plan exactly as today, no alerts. Test in Task 2 and Task 5.

---

## File Structure

- `ppc_engine/scheduler/schedule.py` — add `PinnedOp`; `Schedule.turn_released`.
- `ppc_engine/scheduler/flow_scheduler.py` — `decode(..., pins=None)`: machine pin, strict turn, deadlock release.
- `ppc_engine/scheduler/__init__.py` — export `PinnedOp`.
- `engine/new_engine.py` — `_ppc_pins`, `run(..., published=None)`, `_ppc_frozen(..., release_on_downtime=True)`, fingerprint bump.
- `engine/pipeline.py` — `run_forward(..., published=None)`.
- `engine/fixed_plan.py` (new, pure) — `downtime_waits`, `alerts_from_notes`, `date_changes`.
- `engine/book_store.py` — `published_plan_meta` key.
- `api/main.py` — `_plan` repair, Done, Apply snapshot at winning settings, date list, new-orders pins, go-live seed, alerts.
- `web/index.html`, `web/app.js` — Optimize button + warning, Done flow, date list, alerts.
- Tests: `tests/test_fixed_plan_engine.py`, `tests/test_fixed_plan_adapter.py`, `tests/test_fixed_plan_logic.py`, `tests/test_fixed_plan_api.py`; rebase listed old tests.
- `CLAUDE.md` banner; verification report `docs/superpowers/specs/2026-10-06-fixed-plan-verification.md`.

---

### Task 1: Engine — pins, strict turn, deadlock guard

**Files:**
- Modify: `ppc_engine/scheduler/schedule.py`, `ppc_engine/scheduler/flow_scheduler.py`, `ppc_engine/scheduler/__init__.py`
- Test: `tests/test_fixed_plan_engine.py`

**Interfaces:**
- Produces: `PinnedOp(order_key: tuple[str,str], op_seq: int, machine_id: str, operator: str, prev_start: datetime)`; `decode(orders, sequence, masters, config, dispatch="gt", frozen=None, pins=None) -> Schedule`; `Schedule.turn_released: tuple[tuple[tuple[str,str], int], ...]` (default `()`).

- [ ] **Step 1: Write the failing tests**

```python
"""Fixed plan (2026-10-06 spec): a published op stays on its machine and in its turn."""
from datetime import date, datetime, timedelta

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import Machine, MachineKind, Operator, Role, Shift
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.scheduler import decode
from ppc_engine.scheduler.schedule import FrozenOp, PinnedOp

D1 = date(2025, 3, 3)                      # a Monday
T = lambda d, h, m=0: datetime.combine(d, datetime.min.time()) + timedelta(hours=h, minutes=m)
CFG = PlanConfig(plan_start=T(D1, 8), overlap=0.0)
MAN = OperationKind.MANUAL
DISP = OperationKind.DISPATCH


def _masters(routings):
    machines = {mid: Machine(id=mid, type_text="Manual", kind=MachineKind.MANUAL,
                             available_hrs_per_day=9.5) for mid in ("X", "Y", "Z")}
    ops = (Operator("Alpha", Role.HELPER, frozenset({"X"}), Shift.FIRST),
           Operator("Bravo", Role.HELPER, frozenset({"Y"}), Shift.FIRST),
           Operator("Carol", Role.HELPER, frozenset({"Z"}), Shift.FIRST))
    return Masters(machines=machines, operators=ops, routings=routings,
                   calendar=ShopCalendar())


def _routing(item, *steps):
    """steps: (name, machine_options tuple, cycle_min)"""
    ops = [Operation(i + 1, n, MAN, opts, c) for i, (n, opts, c) in enumerate(steps)]
    ops.append(Operation(len(ops) + 1, "DISPATCH", DISP))
    return Routing(item, item, tuple(ops))


def _order(so, item, qty, due=date(2025, 3, 20)):
    return Order(so_no=so, item_code=item, item_name=item, qty=qty, due_date=due)


def _ops(sched, key, seq):
    return sorted((s for s in sched.segments if s.order_key == key and s.op_seq == seq),
                  key=lambda s: s.start)


def test_no_pins_is_byte_identical():
    r = {"A": _routing("A", ("S1", ("X", "Y"), 1.0)), "B": _routing("B", ("S1", ("X", "Y"), 1.0))}
    m = _masters(r)
    a, b = _order("A1", "A", 60), _order("B1", "B", 60)
    assert decode([a, b], [a.key, b.key], m, CFG) == \
        decode([a, b], [a.key, b.key], m, CFG, pins=None) == \
        decode([a, b], [a.key, b.key], m, CFG, pins=[])


def test_pinned_op_stays_on_its_machine_even_when_another_is_free_sooner():
    """X is busy with A all morning; Y is idle. Free dispatch puts B on Y.
    B is published on X, so it must wait for X."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0)), "B": _routing("B", ("S1", ("X", "Y"), 1.0))}
    m = _masters(r)
    a, b = _order("A1", "A", 120), _order("B1", "B", 60)
    free = decode([a, b], [a.key, b.key], m, CFG)
    assert _ops(free, b.key, 1)[0].machine_id == "Y"
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8)),
            PinnedOp(b.key, 1, "X", "Alpha", T(D1, 10))]
    s = decode([a, b], [a.key, b.key], m, CFG, pins=pins)
    assert {seg.machine_id for seg in _ops(s, b.key, 1)} == {"X"}
    assert _ops(s, b.key, 1)[0].start >= _ops(s, a.key, 1)[-1].end


def test_strict_turn_machine_waits_for_its_next_job():
    """Z's published queue is A then B. A's step 2 needs its step 1 (on X, 3 h) first;
    B is ready at 08:00. Strict turn: Z idles until A runs, B goes after A."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0), ("S2", ("Z",), 1.0)),
         "B": _routing("B", ("S1", ("Z",), 1.0))}
    m = _masters(r)
    a, b = _order("A1", "A", 180), _order("B1", "B", 60)
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8)),
            PinnedOp(a.key, 2, "Z", "Carol", T(D1, 11)),
            PinnedOp(b.key, 1, "Z", "Carol", T(D1, 14))]
    s = decode([a, b], [b.key, a.key], m, CFG, pins=pins)   # sequence favours B
    a2, b1 = _ops(s, a.key, 2), _ops(s, b.key, 1)
    assert b1[0].start >= a2[-1].end, (a2, b1)


def test_early_finish_moves_the_next_job_up():
    """Pins carry no times: when A needs less work than published, B starts right after."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0)), "B": _routing("B", ("S1", ("X",), 1.0))}
    m = _masters(r)
    a = Order(so_no="A1", item_code="A", item_name="A", qty=240, due_date=date(2025, 3, 20),
              process_remaining={1: 60, 2: 0})                 # 3 of 4 hours already done
    b = _order("B1", "B", 60)
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8)),
            PinnedOp(b.key, 1, "X", "Alpha", T(D1, 12))]
    s = decode([a, b], [a.key, b.key], m, CFG, pins=pins)
    assert _ops(s, b.key, 1)[0].start == T(D1, 9)


def test_zero_remaining_pinned_op_does_not_hold_the_queue():
    r = {"A": _routing("A", ("S1", ("Y",), 1.0), ("S2", ("X",), 1.0)),
         "B": _routing("B", ("S1", ("X",), 1.0))}
    m = _masters(r)
    a = Order(so_no="A1", item_code="A", item_name="A", qty=60, due_date=date(2025, 3, 20),
              process_remaining={1: 60, 2: 0, 3: 0})          # A's X step already done
    b = _order("B1", "B", 60)
    pins = [PinnedOp(a.key, 2, "X", "Alpha", T(D1, 8)),
            PinnedOp(b.key, 1, "X", "Alpha", T(D1, 10))]
    s = decode([a, b], [a.key, b.key], m, CFG, pins=pins)
    assert _ops(s, b.key, 1)[0].start == T(D1, 8)


def test_preferred_operator_kept_when_free():
    r = {"A": _routing("A", ("S1", ("X",), 1.0))}
    m = _masters(r)
    m = Masters(machines=m.machines,
                operators=m.operators + (Operator("Dan", Role.HELPER, frozenset({"X"}), Shift.FIRST),),
                routings=r, calendar=ShopCalendar())
    a = _order("A1", "A", 60)
    s = decode([a], [a.key], m, CFG, pins=[PinnedOp(a.key, 1, "X", "Dan", T(D1, 8))])
    assert {seg.operator for seg in _ops(s, a.key, 1)} == {"Dan"}


def test_deadlock_releases_one_turn_and_schedules_everything():
    """X queue: A2 then B1. Y queue: B2 then A1. A1<A2 and B1<B2 by routing: a cycle.
    Unreachable from a feasible published plan, but the guard must never stall."""
    r = {"A": _routing("A", ("S1", ("Y",), 1.0), ("S2", ("X",), 1.0)),
         "B": _routing("B", ("S1", ("X",), 1.0), ("S2", ("Y",), 1.0))}
    m = _masters(r)
    a, b = _order("A1", "A", 60), _order("B1", "B", 60)
    pins = [PinnedOp(a.key, 2, "X", "", T(D1, 8)), PinnedOp(b.key, 1, "X", "", T(D1, 9)),
            PinnedOp(b.key, 2, "Y", "", T(D1, 8)), PinnedOp(a.key, 1, "Y", "", T(D1, 9))]
    s = decode([a, b], [a.key, b.key], m, CFG, pins=pins)
    assert set(s.completion) == {a.key, b.key}
    assert len(s.turn_released) >= 1


def test_frozen_op_out_of_turn_does_not_block():
    """Published X queue: A then B. The floor started B first (punched, so B is frozen).
    B runs first; A keeps its machine and runs after."""
    r = {"A": _routing("A", ("S1", ("X",), 1.0)), "B": _routing("B", ("S1", ("X",), 1.0))}
    m = _masters(r)
    a = _order("A1", "A", 60)
    b = Order(so_no="B1", item_code="B", item_name="B", qty=120, due_date=date(2025, 3, 20),
              process_remaining={1: 60, 2: 0})
    pins = [PinnedOp(a.key, 1, "X", "Alpha", T(D1, 8)),
            PinnedOp(b.key, 1, "X", "Alpha", T(D1, 9))]
    frozen = [FrozenOp(b.key, 1, "X", "Alpha", 60, T(D1, 9))]
    s = decode([a, b], [a.key, b.key], m, CFG, frozen=frozen, pins=pins)
    assert _ops(s, b.key, 1)[0].start == T(D1, 8)
    assert {seg.machine_id for seg in _ops(s, a.key, 1)} == {"X"}
    assert _ops(s, a.key, 1)[0].start >= _ops(s, b.key, 1)[-1].end
    assert s.turn_released == ()


def test_a_pin_to_a_machine_outside_the_options_is_ignored():
    r = {"A": _routing("A", ("S1", ("Y",), 1.0))}
    m = _masters(r)
    a = _order("A1", "A", 60)
    s = decode([a], [a.key], m, CFG, pins=[PinnedOp(a.key, 1, "X", "", T(D1, 8))])
    assert {seg.machine_id for seg in _ops(s, a.key, 1)} == {"Y"}
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3.12 -m pytest tests/test_fixed_plan_engine.py -v`
Expected: ImportError `cannot import name 'PinnedOp'`.

- [ ] **Step 3: Add `PinnedOp` and `turn_released`** in `ppc_engine/scheduler/schedule.py`, after `FrozenOp`:

```python
@dataclass(frozen=True)
class PinnedOp:
    """A published operation held to its machine and its turn (fixed plan, 2026-10-06).

    ``machine_id`` is where the admin's last applied plan put it; ``prev_start`` is
    when it was due to start there, which orders the machine's queue (its TURN).
    Times are never pinned: a repair recomputes them from the punches. ``operator``
    is preferred while they may still man the machine; otherwise whoever qualified
    is free takes it (people are swapped, never machines)."""
    order_key: tuple[str, str]
    op_seq: int
    machine_id: str
    operator: str
    prev_start: datetime
```

In `Schedule`, add after `completion` (keep the default so every existing constructor still works):

```python
    turn_released: tuple = ()
```

and document it in the class docstring: "(order_key, op_seq) of published ops whose turn the deadlock guard released (fixed plan)."

Export it in `ppc_engine/scheduler/__init__.py`:

```python
from ppc_engine.scheduler.schedule import Schedule, Segment, FrozenOp, PinnedOp

__all__ = ["decode", "Schedule", "Segment", "FrozenOp", "PinnedOp"]
```

- [ ] **Step 4: Thread pins through `_place_operation`.** Change its signature and the machine loop:

```python
def _place_operation(
    op: Operation,
    order: Order,
    ready: datetime,
    machine_spans: dict[str, list],
    staffing: StaffingBoard,
    masters: Masters,
    config: PlanConfig,
    pin=None,
) -> dict:
```

Replace the loop header `for opt_idx, mid in enumerate(op.machine_options):` and its `_lay_around` call with:

```python
    options = list(enumerate(op.machine_options))
    if pin is not None:
        # Fixed plan: the published machine only, the published person preferred.
        options = [(i, mid) for i, mid in options if mid == pin.machine_id]
    for opt_idx, mid in options:
        machine = masters.machines.get(mid)
        if machine is None:
            continue  # unknown machine id (provisional handling comes with the loader)
        laid = _lay_around(machine, max(ready, config.plan_start), dur, order, op,
                           int(op_qty), machine_spans, staffing, masters, config,
                           planned_operator=(pin.operator or "") if pin is not None else None)
```

(Rest of the loop unchanged.)

- [ ] **Step 5: Pins and the queue in `decode`.** Add `pins=None` to the signature (after `frozen=None`) and to the docstring ("``pins``: published ops held to their machine and turn, fixed plan 2026-10-06; None or empty is byte-identical"). In the consolidation branch, refuse the combination:

```python
    if getattr(config, "consolidation_window", 0) and config.consolidation_window > 0:
        if pins:
            raise ValueError("pins are keyed by batch and cannot be combined with "
                             "ppc consolidation (never enabled live)")
        return _decode_consolidated(orders, sequence, masters, config, dispatch, frozen)
```

Right after the `if frozen: segments.extend(_preplace_frozen(...))` block, build the queues:

```python
    pin_of, queue = _pin_queues(pins, order_by_key, ops_of, masters,
                                skip={(f.order_key, f.op_seq) for f in (frozen or ())})
    queued = {k for q in queue.values() for k in q}
    pos = {mid: 0 for mid in queue}
    done_pins: set = set()
    released: list = []

    def _head(mid):
        q, i = queue[mid], pos[mid]
        while i < len(q) and q[i] in done_pins:
            i += 1
        pos[mid] = i
        return q[i] if i < len(q) else None

    def _eligible(k):
        ck = (k, ops_of[k][idx_of[k]].seq)
        if ck not in queued or ck in done_pins:
            return True
        return _head(pin_of[ck].machine_id) == ck

    def _pin(k):
        return pin_of.get((k, ops_of[k][idx_of[k]].seq))
```

Add the helper at module level (after `_occupy`):

```python
def _pin_queues(pins, order_by_key, ops_of, masters, skip):
    """Fixed plan: (pin_of, queue). ``pin_of`` maps (order_key, op_seq) to its
    PinnedOp when the pin is still possible (the order is in this plan, the step is
    in its routing, and the machine exists AND is one of the step's options; any
    other pin is ignored and the step is planned normally). ``queue`` maps a machine
    to its pinned steps in TURN order (published start, then key) and leaves out
    frozen steps (they run first, `_preplace_frozen`) and steps with nothing left to
    make (a finished step must never hold a machine's turn)."""
    pin_of, queue = {}, {}
    for p in pins or ():
        order = order_by_key.get(p.order_key)
        if order is None or p.machine_id not in masters.machines:
            continue
        op = next((o for o in ops_of.get(p.order_key, ()) if o.seq == p.op_seq), None)
        if op is None or p.machine_id not in op.machine_options:
            continue
        pin_of[(p.order_key, p.op_seq)] = p
    for ck, p in sorted(pin_of.items(), key=lambda kv: (kv[1].prev_start, kv[0])):
        if ck in skip:
            continue
        order = order_by_key[ck[0]]
        pr = order.process_remaining
        if (pr.get(ck[1], order.qty) if pr is not None else order.qty) <= 0:
            continue
        queue.setdefault(p.machine_id, []).append(ck)
    return pin_of, queue
```

- [ ] **Step 6: Use them in the main loop.** At the top of `while remaining:` replace the `placements = {...}` dict with:

```python
        cands = [k for k in remaining if _eligible(k)]
        if not cands:
            # Deadlock guard (fixed plan): every remaining next step waits for a turn
            # that can only come after it. Release the turn of the one that was due
            # earliest in the published plan, record it, and carry on. Fail loud,
            # never stall, never drop work.
            k0 = min(remaining, key=lambda k: (_pin(k).prev_start, k))
            ck0 = (k0, ops_of[k0][idx_of[k0]].seq)
            done_pins.add(ck0)
            released.append(ck0)
            cands = [k0]
        placements = {
            key: _place_operation(
                ops_of[key][idx_of[key]], order_by_key[key], ready_of[key],
                machine_spans, staffing, masters, config, pin=_pin(key),
            )
            for key in cands
        }
```

In the dispatch selection, replace `remaining` with `cands` in the three places (`min(remaining, ...)` for nondelay, `crit = min(remaining, ...)`, and `conflict = [k for k in remaining ...]`).

In the piece-flow guard re-lay call, add `pin=_pin(key)`:

```python
                placement = _place_operation(
                    ops_of[key][idx_of[key]], order_by_key[key], _r,
                    machine_spans, staffing, masters, config, pin=_pin(key))
```

After `segments.extend(placement["segments"])`, before `just = ...`, mark the turn taken:

```python
        done_pins.add((key, ops_of[key][idx_of[key]].seq))
```

Strict turn needs nothing else: a later op on the machine only becomes eligible after this one is placed, and `machine_spans` keeps it from landing in front.

Return the releases:

```python
    return Schedule(tuple(segments), completion, tuple(released))
```

- [ ] **Step 7: Run the new tests and the whole engine suite**

Run: `python3.12 -m pytest tests/test_fixed_plan_engine.py tests/test_new_engine.py tests/test_no_idle_holes.py tests/test_routing_precedence.py tests/test_frozen_batch_qty.py tests/test_frozen_step_skips_owed_work.py -v`
Expected: all PASS.

- [ ] **Step 8: Mutation-check.** One at a time, each must make at least one new test fail; restore after each (`python3.12 -B` or `sleep 1` before re-running, stale bytecode masked a mutation on 2026-09-22):
  1. `_eligible` always returns `True` → `test_strict_turn_machine_waits_for_its_next_job` fails.
  2. In `_place_operation` drop the `options = [...]` filter → `test_pinned_op_stays_on_its_machine...` fails.
  3. In `_pin_queues` drop the `<= 0` skip → `test_zero_remaining_pinned_op...` fails.
  4. In `_pin_queues` drop `if ck in skip` → `test_frozen_op_out_of_turn...` fails (turn released or B blocked).

- [ ] **Step 9: Commit**

```bash
git add ppc_engine/scheduler/ tests/test_fixed_plan_engine.py
git commit -m "feat(engine): pins hold a published op to its machine and turn

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 2: Adapter — published rows to pins, through the pipeline

**Files:**
- Modify: `engine/new_engine.py` (`_ppc_frozen`, new `_ppc_pins`, `run`, `SCHEDULER_FINGERPRINT`), `engine/pipeline.py` (`run_forward`)
- Test: `tests/test_fixed_plan_adapter.py`

**Interfaces:**
- Consumes: `PinnedOp`, `decode(..., pins=)`, `Schedule.turn_released` (Task 1).
- Produces: `run_forward(plan_run, config, masters, ..., published: list | None = None)`; `new_engine._ppc_pins(rows, orders, batch_by_key, masters) -> (list[PinnedOp], list[str])`; rule6 notes lines beginning with `FIXED_PLAN_PREFIX = "FIXED PLAN: "` (exported from `engine/new_engine.py`).

Published rows are exactly `freeze.schedule_projection` rows: `{batch_id, item_code, process_seq, process_name, machine, operator, start, end, so_refs}`.

- [ ] **Step 1: Write the failing tests**

```python
"""Fixed plan through the pipeline: published rows hold machines; problems are named."""
from dataclasses import replace
from datetime import date

from engine import freeze, new_engine
from engine.config import Config
from engine.loaders import load_all
from engine.models import SOLine
from engine.pipeline import PlanRun, run_forward
from tests.new_sample_workbook import ITEM_A, ITEM_B, SO1, SO2, build_new_sample_bytes
import io


def _setup():
    masters = load_all(io.BytesIO(build_new_sample_bytes()))
    cfg = replace(Config(), scheduler="new", plan_start_date=date(2025, 3, 3))
    lines = [SOLine(SO1, ITEM_A, ITEM_A, 50, date(2025, 3, 20)),
             SOLine(SO2, ITEM_B, ITEM_B, 100, date(2025, 3, 21))]
    return masters, cfg, lines


def _plan(masters, cfg, lines, published=None, ranks=None):
    pr = PlanRun(so_lines=list(lines))
    trace = run_forward(pr, cfg, masters, published=published, priority_rank=ranks)
    return pr.schedule, trace


def _machine_of(sched, item, step):
    return {e.machine for e in sched if e.item_code == item and e.process_name == step}


def test_no_published_plan_is_byte_identical():
    m, cfg, lines = _setup()
    a, _ = _plan(m, cfg, lines)
    b, _ = _plan(m, cfg, lines, published=[])
    assert [e.as_row() for e in a] == [e.as_row() for e in b]


def test_a_published_machine_is_kept():
    """ITEM_B's CNC SECOND SIDE may run on CNC1 or CNC2. Publish it on whichever the
    free plan did NOT choose; the repair must keep the published one."""
    m, cfg, lines = _setup()
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free)
    chosen = _machine_of(free, ITEM_B, "CNC SECOND SIDE").pop()
    other = "CNC2" if chosen == "CNC1" else "CNC1"
    for r in rows:
        if r["item_code"] == ITEM_B and r["process_name"] == "CNC SECOND SIDE":
            r["machine"] = other
    fixed, _ = _plan(m, cfg, lines, published=rows)
    assert _machine_of(fixed, ITEM_B, "CNC SECOND SIDE") == {other}


def test_reversed_priority_does_not_move_a_published_job():
    m, cfg, lines = _setup()
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free)
    rev = {f"{SO2}\x1f{ITEM_B}": 1, f"{SO1}\x1f{ITEM_A}": 2}
    fixed, _ = _plan(m, cfg, lines, published=rows, ranks=rev)
    before = {(e.item_code, e.process_name): e.machine for e in free if e.machine not in new_engine.OFF_LANES}
    after = {(e.item_code, e.process_name): e.machine for e in fixed if e.machine not in new_engine.OFF_LANES}
    assert before == after


def test_a_published_machine_no_longer_allowed_is_named_not_fatal():
    m, cfg, lines = _setup()
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free)
    for r in rows:
        if r["item_code"] == ITEM_A and r["process_name"] == "VMC FIRST SIDE":
            r["machine"] = "CNC2"          # not an option for that step
    fixed, trace = _plan(m, cfg, lines, published=rows)
    assert _machine_of(fixed, ITEM_A, "VMC FIRST SIDE") == {"VMC1"}
    notes = [n for n in trace["rule6"]["notes"] if n.startswith(new_engine.FIXED_PLAN_PREFIX)]
    assert any("VMC FIRST SIDE" in n and "CNC2" in n for n in notes), notes


def test_rows_for_unknown_orders_and_machines_are_ignored():
    m, cfg, lines = _setup()
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free) + [
        {"batch_id": "B999", "item_code": "GONE", "process_seq": 1, "process_name": "X",
         "machine": "CNC1", "operator": "", "start": "2025-03-03T08:00:00",
         "end": "2025-03-03T09:00:00", "so_refs": ["NSO-999"]},
        {"batch_id": "B998", "item_code": ITEM_A, "process_seq": 1,
         "process_name": "CNC FIRST SIDE", "machine": "CNC99", "operator": "",
         "start": "2025-03-03T08:00:00", "end": "2025-03-03T09:00:00", "so_refs": ["NSO-998"]}]
    fixed, trace = _plan(m, cfg, lines, published=rows)
    assert trace["rule6"]["error"] is None
    assert fixed


def test_a_clubbed_batch_finds_its_pin_through_any_shared_so():
    """Published when SO1 alone; now SO3 (same item) clubs into the batch. The batch's
    so_refs are {SO1, SO3}; the pin is still found through SO1."""
    m, cfg, lines = _setup()
    free, _ = _plan(m, cfg, lines)
    rows = freeze.schedule_projection(free)
    chosen = _machine_of(free, ITEM_B, "CNC SECOND SIDE").pop()
    other = "CNC2" if chosen == "CNC1" else "CNC1"
    for r in rows:
        if r["item_code"] == ITEM_B and r["process_name"] == "CNC SECOND SIDE":
            r["machine"] = other
    more = lines + [SOLine("NSO-003", ITEM_B, ITEM_B, 10, date(2025, 3, 21))]
    fixed, _ = _plan(m, cfg, more, published=rows)
    assert _machine_of(fixed, ITEM_B, "CNC SECOND SIDE") == {other}


def test_frozen_pin_on_a_down_machine_waits_in_a_repair():
    """D11: a half-finished job on a machine marked down is NOT released by a repair."""
    rows = [{"so_no": "S", "item_code": "I", "process": "P", "op_seq": 1, "machine": "M",
             "operator": "", "remaining_qty": 5, "prev_start": "2025-03-03T08:00:00",
             "prev_end": "2025-03-04T08:00:00"}]

    class _Cal:
        machine_downtime = {"M": {date(2025, 3, 3)}}

    class _M:
        machines = {"M": object()}
        calendar = _Cal()
        routings = {}

    class _B:
        source_so_refs = ["S"]
        item_code = "I"

    class _O:
        key = ("B1", "I")
        item_code = "I"
        process_remaining = {1: 5}
        qty = 5

    kept = new_engine._ppc_frozen(rows, [_O()], {("B1", "I"): _B()}, _M(), date(2025, 3, 3),
                                  release_on_downtime=False)
    gone = new_engine._ppc_frozen(rows, [_O()], {("B1", "I"): _B()}, _M(), date(2025, 3, 3))
    assert len(kept) == 1 and gone == []
```

Before writing `test_frozen_pin_on_a_down_machine_waits_in_a_repair`, read `tests/test_machine_downtime_freeze.py` and copy its real fixture for `_ppc_frozen` instead of the stub classes above if the stub does not satisfy `_ppc_frozen` (it reads `masters.routings[...]` by item; the stub's empty `routings` makes `ops=()` and drops the row by name — if so, give the stub a routing exactly as that file does). The assertion pair (kept with `release_on_downtime=False`, dropped by default) is the contract.

- [ ] **Step 2: Run to verify they fail**

Run: `python3.12 -m pytest tests/test_fixed_plan_adapter.py -v`
Expected: FAIL — `run_forward() got an unexpected keyword argument 'published'`.

- [ ] **Step 3: `_ppc_frozen` release flag.** Add `release_on_downtime=True` as the last parameter, document it ("False in a fixed-plan repair: the job waits and the admin decides, D11"), and gate the downtime `continue`:

```python
        if release_on_downtime and _machine_down_in_window(masters, mid, plan_start_date,
                                                           max(_end, plan_start_date)):
            continue
```

- [ ] **Step 4: `_ppc_pins`.** Add after `_ppc_frozen`:

```python
FIXED_PLAN_PREFIX = "FIXED PLAN: "


def _ppc_pins(rows, orders, batch_by_key, masters):
    """Published-plan rows (``freeze.schedule_projection``) -> ppc PinnedOp[], plus
    plain-English problem lines (fixed plan, 2026-10-06 spec).

    A row maps to the scheduled batch that covers ANY of its SO refs for that item
    (batch ids restart on every plan, and a batch's membership changes as lines
    finish or club in, so the SO refs are the stable key). The step is resolved by
    process NAME, its seq trusted only while it still names that step (routings are
    editable). Several rows for one (batch, step) — a step split into parts — are ONE
    pin: the earliest-started row's machine, operator and start. Rows for orders not
    in this plan, OS lanes and machines not in the master are dropped silently. A
    pin whose machine is no longer one of the step's options is dropped and NAMED:
    the step is planned normally and the admin is told (D8)."""
    from datetime import datetime
    from ppc_engine.scheduler import PinnedOp
    so_to_key = {}
    for key, batch in batch_by_key.items():
        for so in (getattr(batch, "source_so_refs", None) or []):
            so_to_key[(so, batch.item_code)] = key
    order_by_key = {o.key: o for o in orders}
    best, problems = {}, []
    for r in rows or []:
        key = next((so_to_key[(so, r.get("item_code"))] for so in (r.get("so_refs") or [])
                    if (so, r.get("item_code")) in so_to_key), None)
        if key is None or key not in order_by_key:
            continue
        mid = r.get("machine")
        if not mid or mid in OFF_LANES or mid not in masters.machines:
            continue
        routing = masters.routings.get(order_by_key[key].item_code)
        want = _norm(r.get("process_name", ""))
        ops = routing.operations if routing is not None else ()
        op = (next((o for o in ops if o.seq == r.get("process_seq") and _norm(o.name) == want), None)
              or next((o for o in ops if _norm(o.name) == want), None))
        if op is None:
            continue
        if mid not in op.machine_options:
            problems.append(
                f"{FIXED_PLAN_PREFIX}{', '.join(r.get('so_refs') or [])} {r.get('item_code')} "
                f"'{op.name}' was planned on {mid}, which is no longer allowed for this step. "
                f"It was moved to an allowed machine. Press Optimize to plan it properly.")
            continue
        try:
            start = datetime.fromisoformat(r["start"])
        except (KeyError, ValueError, TypeError):
            continue
        stamp = (start, mid, r.get("operator", "") or "")
        cur = best.get((key, op.seq))
        if cur is None or stamp < cur:
            best[(key, op.seq)] = stamp
    pins = [PinnedOp(order_key=k, op_seq=s, machine_id=mid, operator=opr, prev_start=st)
            for (k, s), (st, mid, opr) in best.items()]
    pins.sort(key=lambda p: (p.prev_start, p.order_key, p.op_seq))
    return pins, sorted(set(problems))
```

- [ ] **Step 5: `run(..., published=None)`.** Add the parameter after `occupancy=None` and, replacing the two lines that build `ppc_frozen` and call `decode`:

```python
    sched_cfg = _plan_config(config)
    ppc_frozen = (_ppc_frozen(frozen, orders, batch_by_key, new_masters,
                              sched_cfg.plan_start.date(),
                              release_on_downtime=not published) if frozen else None)
    ppc_pins = None
    if published:
        ppc_pins, problems = _ppc_pins(published, orders, batch_by_key, new_masters)
        if notes is not None:
            notes.extend(problems)
    sched = decode(orders, sequence, new_masters, sched_cfg, frozen=ppc_frozen,
                   pins=ppc_pins)
    if published and notes is not None:
        refs = {k: ", ".join(getattr(b, "source_so_refs", None) or [k[0]])
                for k, b in batch_by_key.items()}
        names = {(o.key, op.seq): op.name for o in orders
                 for op in new_masters.routings[o.item_code].operations}
        for k, seq in sched.turn_released:
            notes.append(f"{FIXED_PLAN_PREFIX}{refs.get(k, k[0])} {k[1]} "
                         f"'{names.get((k, seq), seq)}' could not wait for its turn without "
                         f"stopping the plan, so it was let through out of turn. "
                         f"Press Optimize to re-plan.")
    return _entries_from_schedule(sched, batch_by_key)
```

Update the docstring: "``published`` (optional) is the published plan (``freeze.schedule_projection`` rows of the last applied plan): every op it covers keeps its machine and its turn (fixed plan, 2026-10-06). ``None``/empty is byte-identical to before."

Bump the fingerprint:

```python
SCHEDULER_FINGERPRINT = "new-engine-v12-fixed-plan"
```

- [ ] **Step 6: `run_forward(..., published=None)`.** In `engine/pipeline.py` add the parameter after `occupancy`, document it next to `occupancy`, refuse it off the new engine (same reason as occupancy: the retired engines swallow `**kw`):

```python
    if published and getattr(config, "scheduler", "classic") != "new":
        raise OccupancyRequiresNewEngineError(
            f"a published plan was passed to run_forward, but config.scheduler is "
            f"{config.scheduler!r}, not 'new'; only the new engine holds published "
            f"machines and turns.")
```

and after the occupancy branch:

```python
        if published:
            _sched_kw["published"] = published
```

- [ ] **Step 7: Run tests**

Run: `python3.12 -m pytest tests/test_fixed_plan_adapter.py tests/test_freeze_adapter.py tests/test_machine_downtime_freeze.py tests/test_freeze_pipeline.py tests/test_new_engine.py -v`
Expected: PASS.

- [ ] **Step 8: Mutation-check.** (a) `release_on_downtime=not published` → `True` fails the down-machine test; (b) in `_ppc_pins` look up only `so_refs[0]` → clubbed test fails when the new SO sorts first (if it doesn't fail, reorder `so_refs` in the test so it does — the test must discriminate); (c) drop the `problems.append` → named-not-fatal test fails.

- [ ] **Step 9: Commit**

```bash
git add engine/new_engine.py engine/pipeline.py tests/test_fixed_plan_adapter.py
git commit -m "feat(engine): plan from the published plan; name what cannot stay

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 3: Pure helpers — alerts and the date list

**Files:**
- Create: `engine/fixed_plan.py`
- Test: `tests/test_fixed_plan_logic.py`

**Interfaces:**
- Produces:
  - `downtime_waits(schedule, downtime_rows, known_ids, today) -> list[dict]` — `{"machine","from_date","to_date","orders":[str]}` per break not known when the plan was published, still running on/after `today`, with planned work on that machine ending on/after its start.
  - `alerts_from_notes(notes) -> list[str]` — rule6 notes starting with `new_engine.FIXED_PLAN_PREFIX`, prefix stripped, de-duplicated, order kept.
  - `downtime_alert_text(w) -> str`.
  - `date_changes(now: dict[str,str], after: dict[str,str]) -> list[dict]` — keys `"<so>\x1f<item>"` → ISO date; rows `{"so","item","now","after","days"}` for keys in both with a different date, sorted by `abs(days)` desc then key.

- [ ] **Step 1: Write the failing tests**

```python
from datetime import date, datetime

from engine import fixed_plan
from engine.models import ScheduleEntry
from engine.new_engine import FIXED_PLAN_PREFIX


def _e(machine, start, end, so):
    return ScheduleEntry(batch_id="B1", item_code="I", process_seq=1, process_name="P",
                         machine=machine, qty=1, occupancy_min=60,
                         start=datetime.fromisoformat(start), end=datetime.fromisoformat(end),
                         so_refs=[so])


def test_downtime_waits_names_orders_queued_on_the_machine():
    sched = [_e("CNC3", "2026-10-09T08:00", "2026-10-09T12:00", "SO1"),
             _e("CNC3", "2026-10-05T08:00", "2026-10-05T12:00", "SO0"),   # done before the break
             _e("CNC6", "2026-10-09T08:00", "2026-10-09T12:00", "SO2")]
    rows = [{"id": "d1", "machine": "CNC3", "from_date": "2026-10-07", "to_date": "2026-10-08"}]
    w = fixed_plan.downtime_waits(sched, rows, known_ids=set(), today=date(2026, 10, 6))
    assert w == [{"machine": "CNC3", "from_date": "2026-10-07", "to_date": "2026-10-08",
                  "orders": ["SO1"]}]


def test_a_break_known_when_the_plan_was_published_raises_nothing():
    sched = [_e("CNC3", "2026-10-09T08:00", "2026-10-09T12:00", "SO1")]
    rows = [{"id": "d1", "machine": "CNC3", "from_date": "2026-10-07", "to_date": "2026-10-08"}]
    assert fixed_plan.downtime_waits(sched, rows, known_ids={"d1"}, today=date(2026, 10, 6)) == []


def test_a_finished_break_raises_nothing():
    sched = [_e("CNC3", "2026-10-09T08:00", "2026-10-09T12:00", "SO1")]
    rows = [{"id": "d1", "machine": "CNC3", "from_date": "2026-10-01", "to_date": "2026-10-02"}]
    assert fixed_plan.downtime_waits(sched, rows, known_ids=set(), today=date(2026, 10, 6)) == []


def test_malformed_downtime_rows_are_skipped():
    rows = [{"id": "x", "machine": "CNC3", "from_date": "bad", "to_date": "2026-10-08"}]
    assert fixed_plan.downtime_waits([], rows, known_ids=set(), today=date(2026, 10, 6)) == []


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
```

- [ ] **Step 2: Run, verify FAIL** (`ModuleNotFoundError: engine.fixed_plan`): `python3.12 -m pytest tests/test_fixed_plan_logic.py -v`

- [ ] **Step 3: Implement `engine/fixed_plan.py`**

```python
"""Fixed plan (2026-10-06 spec): pure helpers for what the admin is told.

Nothing here changes a plan. ``downtime_waits`` finds machine breaks entered AFTER
the plan was published that hold up planned work (the "Optimize recommended"
banner, D5); ``alerts_from_notes`` lifts the engine's fixed-plan problem lines out
of the rule-6 notes; ``date_changes`` is the delivery-date list an Optimize result
shows before Apply (D4)."""
from __future__ import annotations

from datetime import date


def _dmy(iso: str) -> str:
    return date.fromisoformat(iso).strftime("%d-%m-%Y")


def downtime_waits(schedule, downtime_rows, known_ids, today) -> list[dict]:
    out = []
    for d in downtime_rows or []:
        if d.get("id") in known_ids:
            continue
        try:
            f = date.fromisoformat(d["from_date"])
            t = date.fromisoformat(d["to_date"])
        except (KeyError, ValueError, TypeError):
            continue
        if t < f:
            f, t = t, f
        if t < today:
            continue
        mid = d.get("machine", "")
        orders = sorted({so for e in schedule if e.machine == mid and e.end.date() >= f
                         for so in (e.so_refs or [])})
        if orders:
            out.append({"machine": mid, "from_date": f.isoformat(),
                        "to_date": t.isoformat(), "orders": orders})
    return out


def downtime_alert_text(w: dict) -> str:
    n = len(w["orders"])
    return (f"{w['machine']} is marked down {_dmy(w['from_date'])} to {_dmy(w['to_date'])} "
            f"and {n} order{'s are' if n != 1 else ' is'} waiting on it "
            f"({', '.join(w['orders'])}). Press Optimize to decide whether to wait or "
            f"move them.")


def alerts_from_notes(notes) -> list[str]:
    from engine.new_engine import FIXED_PLAN_PREFIX
    seen, out = set(), []
    for n in notes or []:
        if isinstance(n, str) and n.startswith(FIXED_PLAN_PREFIX):
            text = n[len(FIXED_PLAN_PREFIX):]
            if text not in seen:
                seen.add(text)
                out.append(text)
    return out


def date_changes(now: dict, after: dict) -> list[dict]:
    rows = []
    for k in now.keys() & after.keys():
        a, b = date.fromisoformat(now[k]), date.fromisoformat(after[k])
        if a != b:
            so, item = k.split("\x1f", 1)
            rows.append({"so": so, "item": item, "now": now[k], "after": after[k],
                         "days": (b - a).days})
    rows.sort(key=lambda r: (-abs(r["days"]), r["so"], r["item"]))
    return rows
```

- [ ] **Step 4: Run, verify PASS.** `python3.12 -m pytest tests/test_fixed_plan_logic.py -v`

- [ ] **Step 5: Commit**

```bash
git add engine/fixed_plan.py tests/test_fixed_plan_logic.py
git commit -m "feat: fixed-plan alerts and the Optimize date list (pure)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 4: Publishing — who writes the published plan, and when

**Files:**
- Modify: `engine/book_store.py`, `api/main.py` (`_optimize_apply`, `add_new_orders`, new `_publish`, new `_ensure_published_plan`)
- Test: `tests/test_fixed_plan_api.py` (first half)

**Interfaces:**
- Consumes: `freeze.schedule_projection`, `run_forward(published=)`.
- Produces: `book_store.save_published_meta(dict)`, `book_store.load_published_meta() -> dict` (key `anvitech:published_plan_meta`, `{"at": iso, "downtime_ids": [str]}`); `api.main._publish(rows: list) -> None`; `api.main._ensure_published_plan() -> None`.

The published plan IS `anvitech:last_applied_schedule` (same rows `compute_frozen_set` already reads). Three writers, and only three: Apply (also used by prepone-accept), go-live seed, Add New Orders append.

- [ ] **Step 1: Write the failing tests** (fixture copied from `tests/test_freeze_api.py:27-83`)

```python
"""Fixed plan through the API (2026-10-06 spec)."""
import importlib
import time
from datetime import date

import pytest
from fastapi.testclient import TestClient

from engine import book_store
from engine.models import Order
from tests.new_sample_workbook import ITEM_A, ITEM_B, SO1, SO2, build_new_sample_bytes


def _api_with_new_engine(monkeypatch):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    import api.main as m
    importlib.reload(m)
    return m


@pytest.fixture
def api(monkeypatch):
    m = _api_with_new_engine(monkeypatch)
    book_store.save_masters_bytes(build_new_sample_bytes())
    book_store.add_orders([Order(SO1, ITEM_A, ITEM_A, 50, date(2025, 3, 20)),
                           Order(SO2, ITEM_B, ITEM_B, 100, date(2025, 3, 21))])
    client = TestClient(m.app)
    client.post("/login", data={"username": "anvitech", "password": "1930rail"})
    client.get("/operators")
    m._OPT_BUDGETS = {"quick": 15, "deep": 15}
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    monkeypatch.delenv("OPTIMIZE_WORKER_SECRET", raising=False)
    return m, client


def _machines(m):
    m._PLAN_CACHE["key"] = None
    res = m._plan(m._load_plan_config())
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    return {(e.item_code, e.process_name): e.machine for e in sched
            if e.machine not in ("OS / Outsourced", "Off-machine")}, res


def test_first_plan_seeds_the_published_plan_once(api):
    m, client = api
    # A stale snapshot from before go-live must be REPLACED, not adopted.
    book_store.save_last_applied_schedule([{"batch_id": "B1", "item_code": ITEM_A,
        "process_seq": 1, "process_name": "CNC FIRST SIDE", "machine": "CNC2",
        "operator": "", "start": "2020-01-01T08:00:00", "end": "2020-01-01T09:00:00",
        "so_refs": [SO1]}])
    assert book_store.load_published_meta() == {}
    client.post("/run", json={"persist": False})
    rows = book_store.load_last_applied_schedule()
    assert rows and book_store.load_published_meta().get("at")
    client.post("/run", json={"persist": False})
    assert book_store.load_last_applied_schedule() == rows


def test_apply_snapshots_the_plan_at_the_winning_settings(api):
    """Apply used to snapshot with the OLD overlap/flexible settings (written to the
    config only afterwards), so the published plan was not the plan approved."""
    m, client = api
    m._start_optimize(budget_evals=15, label="quick", background=False)
    m._optimize_apply()
    rows = book_store.load_last_applied_schedule()
    m._PLAN_CACHE["key"] = None
    m._plan(m._load_plan_config())
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    from engine import freeze
    assert {(r["item_code"], r["process_name"], r["machine"]) for r in rows} == \
        {(r["item_code"], r["process_name"], r["machine"])
         for r in freeze.schedule_projection(sched)}
```

- [ ] **Step 2: Run, verify FAIL** (`load_published_meta` missing): `python3.12 -m pytest tests/test_fixed_plan_api.py -v`

- [ ] **Step 3: Store key** in `engine/book_store.py` (next to `LAST_APPLIED_SCHEDULE_KEY`):

```python
PUBLISHED_META_KEY = "anvitech:published_plan_meta"  # kv: json {at, downtime_ids} of the published plan (fixed plan, 2026-10-06)
```

```python
def save_published_meta(meta: dict) -> None:
    """When the published plan was written and which machine breaks were already on
    file then (a break entered later raises the "Optimize recommended" banner)."""
    get_store().kv_set(PUBLISHED_META_KEY, json.dumps(meta))


def load_published_meta() -> dict:
    raw = get_store().kv_get(PUBLISHED_META_KEY)
    return json.loads(raw) if raw else {}
```

- [ ] **Step 4: `_publish` and `_ensure_published_plan`** in `api/main.py`, next to `_compute_and_store_frozen`:

```python
def _publish(rows: list) -> None:
    """Write the published plan (fixed plan, 2026-10-06). The ONE writer: an applied
    Optimize (also behind an accepted earlier date), the go-live seed, and Add New
    Orders appending its new lines. Nothing else may change a job's machine or turn."""
    book_store.save_last_applied_schedule(rows)
    book_store.save_published_meta({
        "at": _ist_now().isoformat(timespec="seconds"),
        "downtime_ids": [d.get("id") for d in book_store.load_machine_downtime()
                         if d.get("id")]})


def _ensure_published_plan() -> None:
    """Go-live seed: with no published plan on file, the plan the floor sees right
    now (today's planner + applied ranks, no pins) becomes the published plan, once.
    New engine only; a no-op whenever a plan is already published."""
    # Keyed on the META, not the rows: the live store already holds a snapshot from
    # the last auto-applied search (days old, taken at the old settings). Go-live must
    # replace it with what the floor sees at that moment (spec 4.5).
    if book_store.load_published_meta():
        return
    config = _load_plan_config()
    if getattr(config, "scheduler", "classic") != "new":
        return
    _PLAN_CACHE["key"] = None
    _plan(config, _seeding=True)
    art = _PLAN_CACHE.get("artifacts") or {}
    plan_run = art.get("plan_run")
    if plan_run is not None and plan_run.schedule:
        _publish(freeze.schedule_projection(plan_run.schedule))
        _PLAN_CACHE["key"] = None
```

- [ ] **Step 5: Apply snapshots at the winning settings.** In `_optimize_apply`, MOVE the config-persist block (`best_ov = ...` through `book_store.save_plan_config(...)`) ABOVE the `try: setup = optimize_service.prepare_contest(...)` snapshot block, and replace the snapshot block with:

```python
        # Snapshot AFTER the winning overlap / machine set are saved, so the published
        # plan is the plan approved (it was taken with the old settings until
        # 2026-10-06). No `published=` here: an Optimize result is free by definition.
        try:
            setup = optimize_service.prepare_contest(
                book_store.load_active_orders(), book_store.load_actuals(),
                _current_masters(), _resolve_config(_load_plan_config()),
                absences=book_store.load_absences(),
                operator_table=book_store.load_operator_table(),
                frozen=book_store.load_frozen_ops(),
                machine_downtime=book_store.load_machine_downtime())
            sched, _ = _all_lines_schedule(setup, setup.masters, res["ranks"])
            _publish(freeze.schedule_projection(sched))
        except Exception:
            pass  # never let schedule-snapshotting break an apply
```

`/new-orders/prepone/accept` calls `_optimize_apply()`, so it publishes too (D10); no change there.

- [ ] **Step 6: Run, verify the two tests PASS** (seed test needs Task 5's `_plan` hook; if run before Task 5, mark it `xfail` here and remove the mark in Task 5).

- [ ] **Step 7: Commit**

```bash
git add engine/book_store.py api/main.py tests/test_fixed_plan_api.py
git commit -m "feat(api): one writer for the published plan; snapshot at the winning settings

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 5: Every plan is a repair; Done only repairs

**Files:**
- Modify: `api/main.py` (`_plan`, `_plan_fingerprint`, `_all_lines_schedule`, `_incumbent_metrics`, `optimize_done_ep`, `add_new_orders`, `_plan_run_for_report` fallback, `create_machine_downtime` docstring)
- Test: `tests/test_fixed_plan_api.py` (second half); rebase old tests listed in Step 8.

**Interfaces:**
- Consumes: `_publish`, `_ensure_published_plan`, `fixed_plan.*`, `run_forward(published=)`.
- Produces: `/run` response gains `"fixed_plan_alerts": [str]`; `POST /optimize/done` returns `{"started": False, "reason": "repaired", "state": <str>}`; `_all_lines_schedule(setup, masters, ranks, published=None)`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_fixed_plan_api.py`)

```python
def test_done_never_starts_a_contest_and_keeps_machines(api, monkeypatch):
    m, client = api
    client.post("/run", json={"persist": False})               # seeds the published plan
    before, _ = _machines(m)
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    book_store.save_plan_priority({f"{SO2}\x1f{ITEM_B}": 1, f"{SO1}\x1f{ITEM_A}": 2},
                                  {"saved_at": "x"})            # a different job order
    r = client.post("/optimize/done")
    assert r.status_code == 200 and r.json()["started"] is False
    assert starts == []
    after, _ = _machines(m)
    assert after == before
    note = book_store.load_auto_note()["text"]
    assert "keep their machines" in note


def test_done_refreshes_the_frozen_set(api, monkeypatch):
    m, client = api
    client.post("/run", json={"persist": False})
    called = []
    real = m._compute_and_store_frozen
    monkeypatch.setattr(m, "_compute_and_store_frozen", lambda: called.append(1) or real())
    client.post("/optimize/done")
    assert called == [1]


def test_user_role_can_press_done(api):
    m, client = api
    u = TestClient(m.app)
    u.post("/login", data={"username": "anvitech_user", "password": "anvitech12345678"})
    assert u.post("/optimize/done").status_code == 200


def test_a_break_entered_after_publishing_raises_the_banner(api):
    m, client = api
    client.post("/run", json={"persist": False})
    rows = book_store.load_last_applied_schedule()
    mid = next(r["machine"] for r in rows)
    first = min(r["start"] for r in rows if r["machine"] == mid)[:10]
    client.post("/machine-downtime", json={"machine": mid, "from_date": first,
                                           "to_date": first, "reason": "service"})
    alerts = client.post("/run", json={"persist": False}).json()["fixed_plan_alerts"]
    assert any(a.startswith(f"{mid} is marked down") for a in alerts), alerts


def test_published_plan_is_in_the_cache_key(api):
    m, client = api
    client.post("/run", json={"persist": False})
    fp = m._plan_fingerprint(m._load_plan_config())
    rows = book_store.load_last_applied_schedule()
    rows[0]["machine"] = rows[0]["machine"] + "X"
    book_store.save_last_applied_schedule(rows)
    assert m._plan_fingerprint(m._load_plan_config()) != fp
```

(The test seeds with `plan_start_date` = auto/today; if the downtime test's chosen date is in the past relative to today, set `book_store.save_plan_config` with a fixed `plan_start_date` of `date(2025, 3, 3)` in the fixture and pass `today` consistently — check `_resolve_config`; `downtime_waits` takes `today` = the resolved plan start date, not the wall clock, so test and code agree.)

- [ ] **Step 2: Run, verify FAIL.**

- [ ] **Step 3: `_plan` repairs.** Change the signature to `def _plan(config: Config, _seeding: bool = False):`. Before the cache check:

```python
    if not _seeding:
        _ensure_published_plan()
```

After `frozen = book_store.load_frozen_ops()` add:

```python
    # Fixed plan (2026-10-06): every job the published plan covers keeps its machine
    # and its turn; only times move. Empty while seeding / before go-live.
    published = [] if _seeding else book_store.load_last_applied_schedule()
```

Pass it to BOTH run_forward calls (stage 1 and every stage-2 group, so queued new orders keep their machines too and still never move an existing order):

```python
    trace = run_forward(plan_run, ranked_config, masters, reserved=ab or None,
                        priority_rank=ranks, frozen=frozen or None,
                        published=published or None)
```

```python
            run_forward(stage_i, ranked_config, masters, reserved=ab or None,
                        frozen=None, occupancy=occupancy, priority_rank=group_rank,
                        published=published or None)
```

Collect alerts right before `result = {...}`:

```python
    fixed_alerts = fixed_plan.alerts_from_notes((trace.get("rule6") or {}).get("notes"))
    if published:
        fixed_alerts += [fixed_plan.downtime_alert_text(w) for w in fixed_plan.downtime_waits(
            plan_run.schedule, downtime_raw,
            set(book_store.load_published_meta().get("downtime_ids") or []),
            config.plan_start_date)]
```

and add `"fixed_plan_alerts": fixed_alerts,` to the `result` dict. Add `from engine import fixed_plan` and `from engine import freeze` (if not already imported) at the top of `api/main.py`.

- [ ] **Step 4: Fingerprint.** In `_plan_fingerprint`'s `parts`:

```python
        "published": hashlib.sha256(json.dumps(
            book_store.load_last_applied_schedule(), sort_keys=True,
            default=str).encode("utf-8")).hexdigest(),
        "published_meta": book_store.load_published_meta(),
```

- [ ] **Step 5: The incumbent is the repaired plan.** `_all_lines_schedule(setup, masters, ranks, published=None)` passes `published=published or None` to its `run_forward`. `_incumbent_metrics` and `_movement_note`'s CURRENT side call it with `published=book_store.load_last_applied_schedule()`; candidate sides (`_metrics_for_ranks`, contest code, Apply's snapshot) stay without it. In `_plan_run_for_report`'s fallback rebuild, pass `published=book_store.load_last_applied_schedule() or None` to its `run_forward`. Grep to be sure nothing else replays the current plan: `grep -n "run_forward(" api/main.py engine/*.py`.

- [ ] **Step 6: Done repairs.** Replace `optimize_done_ep`'s body:

```python
@app.post("/optimize/done")
def optimize_done_ep(request: Request):
    """'Done entering — update plan'. Any logged-in role. Fixed plan (2026-10-06):
    never starts a search. It refreshes the in-progress (frozen) set from the punches
    and re-plans: every job keeps its machine and its turn, only times move. Only the
    admin's Optimize changes machines or turns. `_try_start_auto` is kept,
    unreferenced, so scheduled auto-optimize could return by owner decision."""
    by = getattr(request.state, "user", "") or ""
    try:
        _compute_and_store_frozen()
        _PLAN_CACHE["key"] = None
        res = _plan(_load_plan_config())
        n = len(res.get("fixed_plan_alerts") or [])
        tail = (f" {n} notice{'s' if n != 1 else ''} at the top need your admin."
                if n else "")
        _auto_note_write(f"{_who(by)}pressed \"Done entering\" at {_hhmm()}: plan updated "
                         f"from today's entries. Jobs keep their machines and their turn; "
                         f"only times changed.{tail}")
    except Exception as e:  # noqa: BLE001 — never silent (2026-08-09 rule)
        _auto_note_write(f"{_who(by)}pressed \"Done entering\" at {_hhmm()} but the plan "
                         f"could NOT be updated: {e}. Please try again, and tell your "
                         "admin if it keeps happening.")
        raise HTTPException(status_code=500, detail=str(e))
    return {"started": False, "reason": "repaired", "state": _optimize_status()["state"]}
```

The queue is no longer cleared here (only Apply / prepone-accept clear it).

- [ ] **Step 7: Add New Orders pins its new lines (D9).** In `add_new_orders`, after `book_store.save_new_order_drafts([])` and `_PLAN_CACHE["key"] = None`:

```python
    # Fixed plan (D9): the new lines keep the machine and turn their quote gave them.
    # Planned now (stage 2, around the existing plan), then appended to the published
    # plan; existing rows are untouched.
    try:
        _plan(config)
        sched = (_PLAN_CACHE.get("artifacts") or {}).get("plan_run").schedule
        new_keys = {(o.so_no, o.item_code) for o in orders}
        add = [r for r in freeze.schedule_projection(sched)
               if any((so, r["item_code"]) in new_keys for so in r["so_refs"])]
        if add:
            book_store.save_last_applied_schedule(
                book_store.load_last_applied_schedule() + add)
            _PLAN_CACHE["key"] = None
    except Exception:  # noqa: BLE001 — the order is saved; pinning is best effort
        pass
```

Add a test: after the existing `add_new_order` helper flow from `tests/test_new_orders_api.py:158-206` (copy it), the published plan contains rows whose `so_refs` include the new SO, and `/optimize/done` leaves that row's machine unchanged.

- [ ] **Step 8: Rebase the tests whose premise was "Done starts a contest".** Run `python3.12 -m pytest tests/test_auto_optimize.py tests/test_plan_update_visibility.py tests/test_new_orders_api.py tests/test_freeze_api.py tests/test_machine_downtime_api.py -v`. For each failure, decide and record in the commit message:
  - Tests driving `/optimize/done` and asserting a contest starts / the queue clears / an auto note about searching (`test_done_starts_contest_when_book_changed`, `test_done_fires_optimize_on_thursday`, `test_done_runs_on_non_thursday_too`, `test_done_entering_clears_the_queue_when_a_contest_actually_starts`, `test_done_computes_frozen_set_from_partial_punch`, `test_pressing_done_says_a_search_started_and_who_started_it`): rewrite to the new contract (no start, queue kept, frozen set refreshed, note says jobs keep their machines). Deliberate behaviour change, not a fudge.
  - Tests calling `_try_start_auto` / `_auto_apply_result` directly: keep unchanged.
  - Anything else failing: STOP and investigate; it is not expected.

- [ ] **Step 9: Full suite.** `python3.12 -m pytest -q` → all pass. Baseline on 9f8e7b3 (measured 2026-10-06): 1282 passed, 4 skipped, 1 failed — `test_production_analysis.py::test_monthly_report_json_and_excel`, `ModuleNotFoundError: xlsxwriter` in this local environment only (install it with `python3.12 -m pip install xlsxwriter`, or treat that one as pre-existing).

- [ ] **Step 10: Mutation-check.** (a) `published = []` always in `_plan` → `test_done_never_starts...keeps_machines` fails; (b) restore `_try_start_auto` call in Done → same test fails; (c) drop the `published` part of the fingerprint → cache test fails; (d) drop `known_ids` filtering → write a test that a break on file at publish raises nothing through `/run` and see it fail.

- [ ] **Step 11: Commit**

```bash
git add api/main.py tests/
git commit -m "feat(api): Done entering repairs the published plan; it never re-plans machines

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6: The Optimize result shows which delivery dates move

**Files:**
- Modify: `api/main.py` (`_finalize_optimize`, `_optimize_status`)
- Test: `tests/test_fixed_plan_api.py`

**Interfaces:**
- Produces: `/optimize/status` field `"date_changes": [{"so","item","now","after","days"}]` when `state == "done"` and `kind == "plan"`.

"After" must be what the floor will actually get: the candidate is published, then repaired. So: candidate schedule → projection → re-run WITH that projection as `published` → expected completion.

- [ ] **Step 1: Failing test**

```python
def test_optimize_result_lists_moved_delivery_dates(api):
    m, client = api
    client.post("/run", json={"persist": False})
    m._start_optimize(budget_evals=15, label="quick", background=False)
    st = client.get("/optimize/status").json()
    assert "date_changes" in st
    now = client.post("/run", json={"persist": False}).json()["expected_end"]
    for row in st["date_changes"]:
        assert row["now"] == now[f"{row['so']}\x1f{row['item']}"]
        assert row["days"] != 0
```

(Check the shape of `expected_end` in `_plan` — keyed `so\x1fitem` with ISO dates per the agent map; adapt the lookup if it carries DD-MM-YYYY.)

- [ ] **Step 2: Run, FAIL.**

- [ ] **Step 3: Implement.** Add near `_metrics_for_ranks`:

```python
def _candidate_dates(ranks, overlap, flexible):
    """Delivery dates the floor would see if this result were applied: the candidate
    plan, published, then repaired exactly as every later plan will be (fixed plan,
    2026-10-06), so the list the admin approves is the plan the floor gets."""
    config = _resolve_config(_load_plan_config())
    knob = optimizer.knob_for(config)[0]
    if overlap is not None:
        config = replace(config, **{knob: overlap})
    if flexible is not None:
        config = replace(config, flexible_machines=bool(flexible))
    setup = optimize_service.prepare_contest(
        book_store.load_active_orders(), book_store.load_actuals(), _current_masters(),
        config, absences=book_store.load_absences(),
        operator_table=book_store.load_operator_table(),
        frozen=book_store.load_frozen_ops(),
        machine_downtime=book_store.load_machine_downtime())
    cand, _ = _all_lines_schedule(setup, setup.masters, ranks or None)
    repaired, _ = _all_lines_schedule(setup, setup.masters, ranks or None,
                                      published=freeze.schedule_projection(cand))
    return {f"{so}{KEY_SEP}{item}": d.isoformat()
            for (so, item), d in _expected_by_order(repaired).items()}
```

In `_finalize_optimize`, when `kind == "plan"` and `ranks`, compute:

```python
    date_list = None
    if kind == "plan" and ranks:
        try:
            _cur = _plan(_load_plan_config()).get("expected_end") or {}
            date_list = fixed_plan.date_changes(
                _cur, _candidate_dates(ranks, winner_overlap, winner_flexible))
        except Exception:  # noqa: BLE001 — the list is advisory; never fail a result
            date_list = None
```

(compute it before taking `_OPTIMIZE_LOCK`), store `"date_changes": date_list` in `result`, and in `_optimize_status` add `out["date_changes"] = res.get("date_changes")` when `state == "done"`.

- [ ] **Step 4: Run, PASS. Step 5: Commit** `feat(api): an Optimize result lists the delivery dates it moves`.

---

### Task 7: The screens

**Files:**
- Modify: `web/index.html`, `web/app.js`, `web/style.css`
- Test: `tests/test_fixed_plan_ui.py` (static checks, the pattern `tests/test_role_parity.py` uses on the HTML/JS text)

**Interfaces:**
- Consumes: `/run` `fixed_plan_alerts`; `/optimize/status` `date_changes`; `/optimize/done` `{started:false, reason:"repaired"}`.

- [ ] **Step 1: Failing static tests**

```python
from pathlib import Path

HTML = Path("web/index.html").read_text()
JS = Path("web/app.js").read_text()


def test_button_is_called_optimize():
    assert '<button id="optimize-start" class="primary admin-only">Optimize</button>' in HTML


def test_optimize_warns_before_starting():
    body = JS.split("async function startOptimize()")[1].split("\nasync function ")[0]
    assert "window.confirm(" in body and "different machines" in body


def test_alerts_live_in_the_data_gaps_card_for_both_roles():
    card = HTML.split('id="data-gaps-card"')[1].split("</div>\n    </div>")[0]
    assert 'id="fixed-plan-alerts"' in card
    assert "admin-only" not in card.split('id="fixed-plan-alerts"')[0][-80:]
    assert '"fixed-plan-alerts"' in JS.split("function syncDataGapsCard()")[1][:400]


def test_done_no_longer_promises_a_search():
    body = JS.split("async function doneOptimize()")[1].split("\nasync function ")[0]
    assert "15 to" not in body and "pollDoneOptimize" not in body


def test_result_renders_the_date_list():
    body = JS.split("function renderOptimizeResult(st)")[1].split("\nfunction ")[0]
    assert "date_changes" in body


def test_no_em_dashes_in_new_copy():
    for fn in ("async function doneOptimize()", "async function startOptimize()",
               "function renderFixedPlanAlerts("):
        body = JS.split(fn)[1].split("\nasync function ")[0].split("\nfunction ")[0]
        assert "\u2014" not in body, fn
```

- [ ] **Step 2: Run, FAIL.**

- [ ] **Step 3: HTML.** `index.html:121` → `<button id="optimize-start" class="primary admin-only">Optimize</button>`. Inside `#data-gaps-card`, first child:

```html
      <div id="fixed-plan-alerts" class="fixed-plan-alerts hidden"></div>
```

Update any Settings help text next to the button that says "deep search" to: "Optimize finds a better job order for the whole book. It is the only thing that moves jobs to other machines or changes their order. You see which delivery dates change, then Apply or Discard."

- [ ] **Step 4: JS.**

`syncDataGapsCard`: `["fixed-plan-alerts", "report-panel", "report-noroute"]`.

New renderer, called from `runPlan` right after `renderReport(data.report)`:

```js
function renderFixedPlanAlerts(alerts) {
  const el = $("fixed-plan-alerts");
  if (!el) return;
  if (!alerts || !alerts.length) { el.classList.add("hidden"); el.innerHTML = ""; syncDataGapsCard(); return; }
  el.innerHTML = "<p><strong>Optimize recommended</strong></p><ul>"
    + alerts.map((a) => `<li>${escapeHtml(a)}</li>`).join("") + "</ul>";
  el.classList.remove("hidden");
  syncDataGapsCard();
}
```

`startOptimize`, first lines:

```js
  if (!window.confirm(
    "Optimize may move jobs to different machines and change their order and delivery " +
    "dates. The floor will see a new plan once you press Apply. Continue?"
  )) return;
```

`doneOptimize` rewritten:

```js
async function doneOptimize() {
  if (!window.confirm(
    "Have you finished entering ALL of today's production updates?\n\n" +
    "This updates the times in the plan from what was entered. Jobs keep their machines " +
    "and their order. If you still have more to enter, click Cancel and finish first."
  )) return;
  const st = $("optimize-done-status");
  const doneBtn = $("optimize-done");
  if (doneBtn) doneBtn.disabled = true;
  if (st) st.textContent = "Updating the plan…";
  try {
    const res = await fetch("/optimize/done", { method: "POST" });
    if (!res.ok) {
      if (st) st.textContent = "Could not update the plan: " + (await res.text());
      return;
    }
    await runPlan(false);
    if (st) st.textContent = "Plan updated. Jobs keep their machines and their order; only times changed.";
  } catch (e) {
    if (st) st.textContent = "Could not update the plan: " + e.message;
  } finally {
    if (doneBtn) doneBtn.disabled = false;
  }
}
```

Delete `pollDoneOptimize` if nothing else references it (`grep -n pollDoneOptimize web/app.js`).

`renderOptimizeResult`: after the comparison table, before the worst-orders block:

```js
  const dc = st.date_changes || [];
  if (st.improved || dc.length) {
    const earlier = dc.filter((r) => r.days < 0).length;
    const later = dc.filter((r) => r.days > 0).length;
    h += dc.length
      ? `<p><strong>If you apply this plan, ${dc.length} delivery date${dc.length === 1 ? "" : "s"} change: ${earlier} earlier, ${later} later.</strong></p>`
        + '<div class="table-wrap"><table><thead><tr><th>SO</th><th>Item</th><th>Now</th><th>After</th><th>Change</th></tr></thead><tbody>'
        + dc.map((r) => `<tr><td>${escapeHtml(r.so)}</td><td>${escapeHtml(r.item)}</td><td>${fmtDate(r.now)}</td><td>${fmtDate(r.after)}</td><td>${r.days < 0 ? Math.abs(r.days) + " days earlier" : r.days + " days later"}</td></tr>`).join("")
        + "</tbody></table></div>"
      : "<p>No delivery date changes.</p>";
  }
```

(`fmtDate` — use whatever DD-MM-YYYY formatter app.js already uses for ISO dates; grep `function fmt` and reuse it, do not add a second one.) Leave the `st.auto` branch in place (dead but harmless; an old in-memory auto result after deploy still renders).

- [ ] **Step 5: CSS** `.fixed-plan-alerts` mirroring the existing data-gaps warning colours (copy the `.report` / `.report-noroute` rules' colour tokens; no new colours).

- [ ] **Step 6: Run tests + full suite, PASS. Step 7: Commit** `feat(web): Optimize button with a warning, date list, fixed-plan notices, Done updates times`.

---

### Task 8: Verify on the owner's real data, then document

**Files:**
- Create: `docs/superpowers/specs/2026-10-06-fixed-plan-verification.md` (harness included, as the 2026-09-22 report does)
- Modify: `CLAUDE.md` (new top banner bullet)

- [ ] **Step 1: Ask the owner for `MONGODB_URI`** (memory `reading-live-anvitech-data.md`). Copy every key into a LOCAL store (`STORE_DIR`), never write back. Production-like config: whatever `anvitech:plan_config` holds, `DEFAULT_SCHEDULER=new`.

- [ ] **Step 2: Harness** `scratchpad/verify_fixed_plan.py` (python3.12), run on the copy and on Test5/8/9 at WIP 10/30/all:
  1. First `/run` seeds the published plan. Record per op (item, step, so_refs) → machine; per machine the op order.
  2. Simulate 10 working days: each day, punch the work the plan scheduled for that day (good qty = planned qty, plus on two of the days a 50% shortfall on one CNC job and an early finish on another), then `POST /optimize/done`.
  3. After each day assert: 0 ops changed machine; per machine, remaining ops keep their relative order; 0 `ROUTING_ORDER_VIOLATION`, `OPERATOR_NOT_QUALIFIED`, `BATCH_QTY_SHORT`; no segment inside 13:00-13:30 or 22:00-22:30, on a weekly-off day, on a holiday, on a down machine, or on a second shift for a single-shift machine; no operator double-booked, none booked while absent; Orders / Gantt / delay report expected dates identical.
  4. Idempotence: `/optimize/done` twice with no punch in between → identical plan (hash machine, operator, start, end, qty per entry).
  5. Dates move only downstream: on the shortfall day, every order whose date moved shares a machine queue (transitively) with the short job.
  6. Mark one CNC down 2 days → no op moves, banner names it; run an Optimize (budget 15), `date_changes` present; Apply → banner gone; repair right after Apply → dates equal the `after` column for every listed order (the list told the truth).
  7. Add New Orders on the copy: quote → add → Done → the quoted date unchanged and no existing order's date or machine moved (the 2026-09-22 lesson: run placement changes through `/new-orders/quote`).
  8. Measure late-days: published-at-day-0 vs after 10 days of repairs vs a fresh Optimize on day 10. Report all three; this is the cost the owner accepted (spec §7), stated as numbers.

- [ ] **Step 3: Mutation sweep** — re-run each Task's mutations together, record "N of N load-bearing" honestly; any mutation that fails no test is reported as belt-and-braces, not dressed up.

- [ ] **Step 4: Browser pass**, both roles, local instance on the store copy: Done note, Optimize warning (native `confirm()` blocks automation — accept it manually or drive the POST and check the text in JS), date list, the notice, user role sees the notice and the read-only result.

- [ ] **Step 5: Write the verification report and a CLAUDE.md banner bullet** in the house style (what changed, the one load-bearing decision, measured numbers, mutation results, what was NOT run, deliberate non-builds, the rule: "Only the admin's Optimize moves a job to another machine or changes its turn. Done entering only moves times.").

- [ ] **Step 6: Commit** `docs: fixed plan verification and CLAUDE.md banner`. Do NOT push. Report to the owner with the numbers and ask whether to deploy.
