# Add New Orders (quote a delivery date) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a director a tab where he enters new orders, gets a delivery date computed against the plan the shop is actually running — without moving a single existing order — and either accepts that date or asks for an earlier one, which drops the freeze and re-optimizes the whole book.

**Architecture:** The plan is computed in two stages. Stage 1 is today's plan over today's book, untouched. Stage 2 plans the new orders against a shop whose machines and people are already occupied by stage 1. Existing orders cannot move because they are not in the second calculation. Occupancy rides on `ShopCalendar` (where holidays, leave and maintenance already live), reaches the placement step through a per-machine list of free runs, and reaches staffing by seeding the board's existing booking structures. Every new input defaults to empty, so a plan with no new orders is byte-identical.

**Tech Stack:** Python 3.12 (`python3.12 -m pytest` — the system `python3` is 3.14 where openpyxl fails to import), FastAPI, plain HTML/JS front end, pytest.

**Spec:** `docs/superpowers/specs/2026-09-08-add-new-orders-quote-design.md` — read it before Task 1. This plan argues from it.

## Global Constraints

- **Run tests with `python3.12 -m pytest`.** Never bare `pytest` or `python3`.
- **Byte-identical guarantee.** Any plan computed with no occupancy and no queue must be identical to the pre-change plan. Every new parameter defaults to `None`/empty. This is checked in Task 5 and again in Task 15; if it fails, stop and fix rather than proceed.
- **`ppc_engine/worktime.py::iter_windows` is not touched.** Neither is `_lay_frozen`, `merge_upload`, the objective, the optimizer's search, Rules 1–6, the delay report, analytics, efficiency, Daily Entry, machine maintenance, operators or absences.
- **`SCHEDULER_FINGERPRINT` is not bumped.** No existing plan's placement changes. If a real book's plan does change, that is a bug in the work, not a reason to bump.
- **The tab and every write endpoint are admin-only, enforced server-side** (`require_admin`), not only in CSS.
- **One definition rule.** The quoted date comes from `optimizer.expected_completion`. Never derive a completion date locally.
- **No em dashes in user-visible copy** (existing UI convention: plain English, operator-readable).
- **Commit after every task.** Do not batch commits.

---

## File Structure

**Created**

| File | Responsibility |
|---|---|
| `engine/quote.py` | Pure: given a finished plan's entries, the masters, the config and new order lines, produce stage-2 results and the "nothing moved" verification. No store, no HTTP. |
| `tests/test_quote_occupancy.py` | The engine layer: calendar free runs, staffing seeding, the stop line, free-run placement, byte-identical guard. |
| `tests/test_quote_engine.py` | `engine/quote.py`: the two-stage quote, the no-split rule, the search. |
| `tests/test_new_orders_api.py` | Endpoints: drafts, quote, add, queue clearing, role gating. |
| `tests/test_arrival_queue.py` | `_plan`'s two-stage behaviour and first-come-first-served. |

**Modified**

| File | Change |
|---|---|
| `ppc_engine/domain/calendar.py` | `ShopCalendar` gains `machine_busy`, `operator_busy`, `machine_shift_operator` + `free_runs()`. |
| `ppc_engine/worktime.py` | New pure helper `shift_key_for(dt, config)`. `iter_windows` untouched. |
| `ppc_engine/scheduler/staffing.py` | `StaffingBoard.__init__` accepts `booked` and `assigned`. |
| `ppc_engine/scheduler/flow_scheduler.py` | `_lay_on_machine(..., deadline=None)`; new `_lay_in_free_run`; `_place_operation` uses it; `decode` seeds the board. |
| `engine/new_engine.py` | `OFF_LANES`, `occupancy_from_entries()`, `run(..., occupancy=None)`. |
| `engine/freeze.py` | One line: `_OS_LANES` aliases `new_engine.OFF_LANES` so the two cannot drift. |
| `engine/pipeline.py` | `run_forward(..., occupancy=None)`, passed to the scheduler only when set. |
| `engine/book_store.py` | Draft lines and arrival-queue keys. |
| `engine/orderbook.py` | `validate_new_order_line()` — pure duplicate/routing/quantity checks. |
| `api/main.py` | Draft CRUD, `/new-orders/quote`, `/new-orders/add`, `/new-orders/prepone`, two-stage `_plan`, fingerprint, queue clearing. |
| `web/index.html`, `web/app.js`, `web/style.css` | The Add New Orders tab. |
| `CLAUDE.md` | Banner bullet recording the feature. |

---

## Task 1: The calendar carries occupancy

**Files:**
- Modify: `ppc_engine/domain/calendar.py`
- Test: `tests/test_quote_occupancy.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `ShopCalendar.machine_busy: dict[str, tuple[tuple[datetime, datetime], ...]]` (default `{}`)
  - `ShopCalendar.operator_busy: dict[str, tuple[tuple[datetime, datetime], ...]]` (default `{}`)
  - `ShopCalendar.machine_shift_operator: dict[tuple[str, date, Shift], str]` (default `{}`)
  - `ShopCalendar.free_runs(machine_id: str, after: datetime) -> list[tuple[datetime, datetime | None]]` — the stretches on that machine not already occupied, in time order, starting at/after `after`. The last run's end is `None` (unbounded). With no occupancy: `[(after, None)]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_quote_occupancy.py`:

```python
"""The engine layer of the Add New Orders quote (2026-09-08 spec).

Occupancy = what an earlier planning stage already committed. It must be able to
reach the placement step without any existing plan changing by a single minute.
"""
from datetime import date, datetime

from ppc_engine.domain.calendar import ShopCalendar

D = datetime


def test_free_runs_with_no_occupancy_is_one_unbounded_run():
    cal = ShopCalendar()
    assert cal.free_runs("CNC3", D(2025, 3, 3, 8)) == [(D(2025, 3, 3, 8), None)]


def test_free_runs_splits_around_a_busy_block():
    cal = ShopCalendar(machine_busy={"CNC3": ((D(2025, 3, 5, 8), D(2025, 3, 7, 12)),)})
    assert cal.free_runs("CNC3", D(2025, 3, 3, 8)) == [
        (D(2025, 3, 3, 8), D(2025, 3, 5, 8)),
        (D(2025, 3, 7, 12), None),
    ]


def test_free_runs_starting_inside_a_busy_block_starts_after_it():
    cal = ShopCalendar(machine_busy={"CNC3": ((D(2025, 3, 5, 8), D(2025, 3, 7, 12)),)})
    assert cal.free_runs("CNC3", D(2025, 3, 6, 9)) == [(D(2025, 3, 7, 12), None)]


def test_free_runs_ignores_blocks_that_are_already_past():
    cal = ShopCalendar(machine_busy={"CNC3": ((D(2025, 3, 1, 8), D(2025, 3, 2, 8)),)})
    assert cal.free_runs("CNC3", D(2025, 3, 3, 8)) == [(D(2025, 3, 3, 8), None)]


def test_free_runs_merges_overlapping_and_touching_blocks():
    cal = ShopCalendar(machine_busy={"CNC3": (
        (D(2025, 3, 5, 8), D(2025, 3, 6, 8)),
        (D(2025, 3, 6, 8), D(2025, 3, 7, 8)),     # touching
        (D(2025, 3, 6, 20), D(2025, 3, 8, 8)),    # overlapping
    )})
    assert cal.free_runs("CNC3", D(2025, 3, 3, 8)) == [
        (D(2025, 3, 3, 8), D(2025, 3, 5, 8)),
        (D(2025, 3, 8, 8), None),
    ]


def test_free_runs_for_a_machine_with_no_entry_is_unbounded():
    cal = ShopCalendar(machine_busy={"CNC3": ((D(2025, 3, 5, 8), D(2025, 3, 7, 12)),)})
    assert cal.free_runs("VMC1", D(2025, 3, 3, 8)) == [(D(2025, 3, 3, 8), None)]


def test_a_default_calendar_still_answers_the_old_questions():
    cal = ShopCalendar()
    assert cal.is_working_day(date(2025, 3, 3)) is True      # Monday
    assert cal.is_working_day(date(2025, 3, 6)) is False     # Thursday, weekly off
    assert cal.is_machine_available("CNC3", date(2025, 3, 3)) is True
    assert cal.is_operator_available("Anyone", date(2025, 3, 3)) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_quote_occupancy.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'machine_busy'`.

- [ ] **Step 3: Write minimal implementation**

In `ppc_engine/domain/calendar.py`, extend the imports and the dataclass:

```python
from datetime import date, datetime

from ppc_engine.domain.resources import Shift
```

Add the three fields after `machine_downtime` (docstring entries too):

```python
    # --- Occupancy: what an EARLIER planning stage already committed -------------
    # Empty on every ordinary plan, so nothing below changes. Populated only by the
    # two-stage plan behind the Add New Orders quote (2026-09-08 spec): stage 1's
    # placements become stage 2's occupied time, which is how a new order can be
    # fitted in without any existing order moving.
    machine_busy: dict[str, tuple[tuple[datetime, datetime], ...]] = field(default_factory=dict)
    operator_busy: dict[str, tuple[tuple[datetime, datetime], ...]] = field(default_factory=dict)
    machine_shift_operator: dict[tuple[str, date, Shift], str] = field(default_factory=dict)
```

Add the method:

```python
    def free_runs(self, machine_id: str, after: datetime) -> list[tuple[datetime, datetime | None]]:
        """The stretches of time ``machine_id`` is NOT already occupied, on/after
        ``after``, in time order. The final run is open-ended (``end is None``).

        With no occupancy on file this is a single unbounded run starting at
        ``after`` — which is exactly "no restriction", so every ordinary plan behaves
        as it always has.

        A job is laid inside ONE run and never across two (see the spec's gap rule):
        crossing a run boundary would mean the machine was torn down for another job
        in between, and the setup is not paid twice.
        """
        blocks = self.machine_busy.get(machine_id) or ()
        if not blocks:
            return [(after, None)]
        merged: list[list[datetime]] = []
        for start, end in sorted(blocks):
            if end <= after:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        runs: list[tuple[datetime, datetime | None]] = []
        cursor = after
        for start, end in merged:
            if start > cursor:
                runs.append((cursor, start))
            cursor = max(cursor, end)
        runs.append((cursor, None))
        return runs
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_quote_occupancy.py -v`
Expected: 7 passed.

Then the regression guard: `python3.12 -m pytest -q`
Expected: the existing suite still passes (967 passed, 2 skipped, plus the 7 new).

- [ ] **Step 5: Commit**

```bash
git add ppc_engine/domain/calendar.py tests/test_quote_occupancy.py
git commit -m "feat(quote): the shop calendar can say a machine is already busy

Occupancy joins holidays, operator leave and machine maintenance in the one
place that says when a resource is unavailable. Empty by default, so no plan
changes. free_runs() is the gap rule's data: the stretches between existing
jobs, the last one unbounded.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Which shift a moment belongs to

**Files:**
- Modify: `ppc_engine/worktime.py`
- Test: `tests/test_quote_occupancy.py`

**Interfaces:**
- Consumes: `ppc_engine.worktime.shift_window(day, shift, config)` (existing).
- Produces: `shift_key_for(dt: datetime, config: PlanConfig) -> tuple[date, Shift] | None` — the (shift_date, shift) whose window contains `dt`, or `None` if it falls in non-working time. Used to seed the staffing board's per-shift assignment record from a finished plan.

**Why this exists:** the assignment record is keyed by (machine, shift_date, shift), and a night segment that starts at 21:00 belongs to the shift anchored to the *previous* day. This asks the engine's own `shift_window` rather than re-deriving shift hours anywhere (CLAUDE.md: no feature may re-derive the working-hours model).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_quote_occupancy.py`:

```python
from datetime import time

from ppc_engine.config import PlanConfig
from ppc_engine.domain.resources import Shift
from ppc_engine.worktime import shift_key_for

_CFG = PlanConfig(plan_start=D(2025, 3, 3, 8), first_start=time(8, 0),
                  first_end=time(19, 0), second_start=time(19, 0),
                  second_end=time(5, 0))


def test_shift_key_for_a_day_shift_moment():
    assert shift_key_for(D(2025, 3, 3, 10), _CFG) == (date(2025, 3, 3), Shift.FIRST)


def test_shift_key_for_an_evening_moment_is_that_days_night_shift():
    assert shift_key_for(D(2025, 3, 3, 21), _CFG) == (date(2025, 3, 3), Shift.SECOND)


def test_shift_key_for_after_midnight_belongs_to_the_previous_days_night_shift():
    assert shift_key_for(D(2025, 3, 4, 2), _CFG) == (date(2025, 3, 3), Shift.SECOND)


def test_shift_key_for_a_moment_in_no_shift_is_none():
    assert shift_key_for(D(2025, 3, 4, 6), _CFG) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_quote_occupancy.py -k shift_key -v`
Expected: FAIL — `ImportError: cannot import name 'shift_key_for'`.

- [ ] **Step 3: Write minimal implementation**

In `ppc_engine/worktime.py`, directly after `shift_window`:

```python
def shift_key_for(dt: datetime, config: PlanConfig) -> tuple[date, Shift] | None:
    """The (shift_date, shift) whose window contains ``dt``, or None outside both.

    The night shift is anchored to the day it STARTED, so 02:00 on the 4th belongs
    to the 3rd's second shift. Asks ``shift_window`` rather than comparing clock
    times here, so there is exactly one definition of a shift's hours.
    """
    for day in (dt.date(), dt.date() - timedelta(days=1)):
        for shift in (Shift.FIRST, Shift.SECOND):
            win = shift_window(day, shift, config)
            if win.start <= dt < win.end:
                return (day, shift)
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_quote_occupancy.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add ppc_engine/worktime.py tests/test_quote_occupancy.py
git commit -m "feat(quote): name the shift a moment falls in

Needed to carry a finished plan's per-shift operator assignments into the next
planning stage. Asks shift_window rather than comparing clock times, so shift
hours keep exactly one definition.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: The staffing board can start with people already booked

**Files:**
- Modify: `ppc_engine/scheduler/staffing.py`
- Test: `tests/test_quote_occupancy.py`

**Interfaces:**
- Consumes: Task 1's calendar fields.
- Produces: `StaffingBoard(pools=None, booked=None, assigned=None)` where `booked: dict[str, tuple[tuple[datetime, datetime], ...]]` (operator name → busy intervals) and `assigned: dict[tuple[str, date, Shift], str]`. Both default `None` = today's behaviour exactly.

**Why no logic changes:** `free_during`, `candidate_operator` and `operator_for` already read `_intervals` and `_assign`. Seeding them is how qualification, shift, scarce-first picking and Rule 1's one-operator-per-machine-per-shift stability all carry across the two stages without a second copy of any rule.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_quote_occupancy.py`:

```python
from ppc_engine.domain.resources import Machine, MachineKind, Operator, Role
from ppc_engine.scheduler.staffing import StaffingBoard


def _two_operator_board():
    m = Machine(id="CNC3", name="CNC3", kind=MachineKind.MACHINING)
    ops = (
        Operator(name="Alpha", role=Role.OPERATOR, base_shift=Shift.FIRST,
                 qualified_machines=frozenset({"CNC3"})),
        Operator(name="Bravo", role=Role.OPERATOR, base_shift=Shift.FIRST,
                 qualified_machines=frozenset({"CNC3"})),
    )
    return m, ops


def test_a_seeded_booking_makes_that_person_unavailable():
    board = StaffingBoard(booked={"Alpha": ((D(2025, 3, 3, 8), D(2025, 3, 3, 12)),)})
    assert board.free_during("Alpha", D(2025, 3, 3, 9), D(2025, 3, 3, 10)) is False
    assert board.free_during("Alpha", D(2025, 3, 3, 13), D(2025, 3, 3, 14)) is True
    assert board.free_during("Bravo", D(2025, 3, 3, 9), D(2025, 3, 3, 10)) is True


def test_a_seeded_booking_is_skipped_when_picking_an_operator(monkeypatch):
    from ppc_engine.domain.masters import Masters
    m, ops = _two_operator_board()
    masters = Masters(machines={"CNC3": m}, operators=ops, routings={},
                      calendar=ShopCalendar())
    board = StaffingBoard({"CNC3": ops},
                          booked={"Alpha": ((D(2025, 3, 3, 8), D(2025, 3, 3, 12)),)})
    pick = board.candidate_operator(m, date(2025, 3, 3), Shift.FIRST,
                                    D(2025, 3, 3, 9), D(2025, 3, 3, 10), masters, _CFG)
    assert pick == "Bravo"


def test_a_seeded_assignment_is_the_machines_shift_operator():
    board = StaffingBoard(assigned={("CNC3", date(2025, 3, 3), Shift.FIRST): "Alpha"})
    assert board.operator_for("CNC3", date(2025, 3, 3), Shift.FIRST) == "Alpha"


def test_an_unseeded_board_is_unchanged():
    board = StaffingBoard()
    assert board.free_during("Alpha", D(2025, 3, 3, 9), D(2025, 3, 3, 10)) is True
    assert board.operator_for("CNC3", date(2025, 3, 3), Shift.FIRST) is None
```

Note: check `Operator`'s real constructor in `ppc_engine/domain/resources.py` before writing this and match it exactly (field names, whether `flexibility` is derived or passed). Adjust the two helper constructions only — never the assertions.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_quote_occupancy.py -k seeded -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'booked'`.

- [ ] **Step 3: Write minimal implementation**

In `ppc_engine/scheduler/staffing.py`, replace `StaffingBoard.__init__`:

```python
    def __init__(self, pools: dict[str, tuple[Operator, ...]] | None = None,
                 booked: dict[str, tuple[tuple[datetime, datetime], ...]] | None = None,
                 assigned: dict[tuple[str, date, Shift], str] | None = None) -> None:
        # (machine_id, shift_date, shift) -> operator name that (last) manned it — a soft
        # preference for machine stability, not a hard lock (short jobs may share).
        self._assign: dict[tuple[str, date, Shift], str] = dict(assigned or {})
        # operator name -> list of committed busy (start, end) intervals.
        self._intervals: dict[str, list[tuple[datetime, datetime]]] = {
            name: list(ivs) for name, ivs in (booked or {}).items()
        }
        # machine id -> pre-sorted eligible operators (scarce-first). See build_machine_pools.
        self._pools: dict[str, tuple[Operator, ...]] = pools or {}
        # operator name -> cumulative committed busy minutes (for the "balanced" pick).
        self._load: dict[str, float] = {}
```

Extend the class docstring with one paragraph:

```
    ``booked`` / ``assigned`` seed the board with what an EARLIER planning stage
    already committed (the Add New Orders quote, 2026-09-08 spec). Both default to
    empty, which is every ordinary plan. Nothing else changes: the availability and
    picking rules below read these two structures either way, so a second stage
    inherits qualification, shift, scarce-first picking and the
    one-operator-per-machine-per-shift preference without a second copy of any rule.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_quote_occupancy.py -v`
Expected: 15 passed.

Run: `python3.12 -m pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add ppc_engine/scheduler/staffing.py tests/test_quote_occupancy.py
git commit -m "feat(quote): the staffing board can start with people already booked

Seeds the existing interval and assignment structures instead of adding a rule,
so a second planning stage inherits qualification, shift, scarce-first picking
and per-shift operator stability unchanged.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: A stop line on `_lay_on_machine`

**Files:**
- Modify: `ppc_engine/scheduler/flow_scheduler.py` (`_lay_on_machine` only)
- Test: `tests/test_quote_occupancy.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_lay_on_machine(machine, earliest, dur_min, order, op, op_qty, staffing, masters, config, deadline=None)`. With `deadline=None` the behaviour is exactly today's. With a deadline, the work is laid only if it finishes on or before it; otherwise `None` is returned, meaning "not here".

- [ ] **Step 1: Write the failing test**

Append to `tests/test_quote_occupancy.py`:

```python
import io

import pytest

from engine import book_store, loaders
from engine.config import Config
from engine.new_engine import _orders_from_batches, _plan_config
from engine.rules import rule1_consolidate
from ppc_engine.loaders import load_all as new_load
from ppc_engine.scheduler.flow_scheduler import _lay_on_machine
from ppc_engine.scheduler.staffing import StaffingBoard, build_machine_pools
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
               apply_operator_logic=True)


@pytest.fixture()
def shop():
    """The sample shop: (masters, plan config, first machining order + its first op)."""
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, _ = loaders.load_all(io.BytesIO(wb))
    orders, _ = _orders_from_batches(rule1_consolidate.run(so_lines, _CONF), nm)
    cfg = _plan_config(_CONF)
    order = orders[0]
    op = nm.routings[order.item_code].operations[0]
    return nm, cfg, order, op


def _lay(shop, deadline, minutes=600.0):
    nm, cfg, order, op = shop
    mid = op.machine_options[0]
    board = StaffingBoard(build_machine_pools(nm))
    return _lay_on_machine(nm.machines[mid], cfg.plan_start, minutes, order, op,
                           int(order.qty), board, nm, cfg, deadline=deadline)


def test_no_deadline_lays_the_work_exactly_as_before(shop):
    assert _lay(shop, None) is not None


def test_a_deadline_that_cannot_hold_the_work_returns_none(shop):
    _nm, cfg, _o, _op = shop
    assert _lay(shop, cfg.plan_start + timedelta(minutes=30)) is None


def test_a_generous_deadline_gives_the_identical_placement(shop):
    _nm, cfg, _o, _op = shop
    loose = _lay(shop, cfg.plan_start + timedelta(days=90))
    free = _lay(shop, None)
    assert loose is not None
    assert (loose["start"], loose["end"]) == (free["start"], free["end"])
    assert [(s.start, s.end) for s in loose["segments"]] == \
           [(s.start, s.end) for s in free["segments"]]


def test_no_segment_ever_crosses_the_deadline(shop):
    _nm, cfg, _o, _op = shop
    deadline = cfg.plan_start + timedelta(days=2)
    laid = _lay(shop, deadline, minutes=120.0)
    assert laid is not None
    assert all(seg.end <= deadline for seg in laid["segments"])
```

Add `from datetime import timedelta` to the test file's imports.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_quote_occupancy.py -k deadline -v`
Expected: FAIL — `TypeError: _lay_on_machine() got an unexpected keyword argument 'deadline'`.

- [ ] **Step 3: Write minimal implementation**

In `ppc_engine/scheduler/flow_scheduler.py`, add the parameter to `_lay_on_machine`'s signature after `config: PlanConfig,`:

```python
    deadline: datetime | None = None,
```

Add to its docstring:

```
    ``deadline`` (optional) is a stop line: the work must finish on or before it, or
    None is returned meaning "not in this stretch of time". It exists so a job can be
    fitted into a gap between jobs an earlier planning stage already placed, without
    ever running into them (Add New Orders quote, 2026-09-08 spec). ``None`` — every
    ordinary plan — is exactly today's behaviour.
```

Inside the window loop, immediately after `if remaining <= _EPS_MIN: break`:

```python
        if deadline is not None and win.start >= deadline:
            return None  # out of room in this stretch; the caller tries the next one
```

and change the two lines that measure the window:

```python
        seg_start = max(cursor, win.start)
        win_end = win.end if deadline is None else min(win.end, deadline)
        avail = (win_end - seg_start).total_seconds() / 60.0
```

The rest of the loop is unchanged (`cursor = win.end` on the skip branches stays as it is — with a deadline the next iteration returns `None` anyway).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_quote_occupancy.py -v`
Expected: 19 passed.

Run: `python3.12 -m pytest -q`
Expected: full suite green — no caller passes a deadline yet, so nothing can have moved.

- [ ] **Step 5: Commit**

```bash
git add ppc_engine/scheduler/flow_scheduler.py tests/test_quote_occupancy.py
git commit -m "feat(quote): a stop line on the machine laying step

Lay the work, but never past this moment; report 'not here' rather than overrun.
None on every existing path, so no plan changes.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Placement walks the free runs — and the byte-identical guard

**Files:**
- Modify: `ppc_engine/scheduler/flow_scheduler.py` (`_place_operation`, `decode`, new `_lay_in_free_run`)
- Test: `tests/test_quote_occupancy.py`

**Interfaces:**
- Consumes: `ShopCalendar.free_runs` (Task 1), `StaffingBoard(booked=, assigned=)` (Task 3), `_lay_on_machine(..., deadline=)` (Task 4).
- Produces: `decode` honours `masters.calendar`'s occupancy with no signature change. New private `_lay_in_free_run(machine, earliest, dur_min, order, op, op_qty, staffing, masters, config)` returning the same placement dict as `_lay_on_machine`, or `None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_quote_occupancy.py`:

```python
from dataclasses import replace as _replace

from ppc_engine.scheduler import decode


def _decoded(nm, cfg, orders):
    return decode(orders, [o.key for o in orders], nm, cfg)


def _busy_machine_ivs(sched):
    out = {}
    for seg in sched.segments:
        if seg.machine_id and seg.end > seg.start:
            out.setdefault(seg.machine_id, []).append((seg.start, seg.end))
    return {k: tuple(sorted(v)) for k, v in out.items()}


@pytest.fixture()
def book():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, _ = loaders.load_all(io.BytesIO(wb))
    orders, _ = _orders_from_batches(rule1_consolidate.run(so_lines, _CONF), nm)
    return nm, _plan_config(_CONF), orders


def test_a_plan_with_no_occupancy_is_byte_identical(book):
    """The guarantee the whole feature rests on: an empty calendar changes nothing."""
    nm, cfg, orders = book
    before = _decoded(nm, cfg, orders)
    after = _decoded(_replace(nm, calendar=_replace(nm.calendar, machine_busy={},
                                                    operator_busy={})), cfg, orders)
    assert [(s.order_key, s.op_seq, s.machine_id, s.operator, s.start, s.end, s.qty)
            for s in before.segments] == \
           [(s.order_key, s.op_seq, s.machine_id, s.operator, s.start, s.end, s.qty)
            for s in after.segments]
    assert before.completion == after.completion


def test_new_work_never_lands_on_occupied_machine_time(book):
    """Stage 2 in miniature: plan half the orders, then plan the rest against them."""
    nm, cfg, orders = book
    first, second = orders[: len(orders) // 2], orders[len(orders) // 2:]
    assert first and second
    stage1 = _decoded(nm, cfg, first)
    busy = _busy_machine_ivs(stage1)
    op_busy = {}
    for seg in stage1.segments:
        if seg.operator:
            op_busy.setdefault(seg.operator, []).append((seg.start, seg.end))
    nm2 = _replace(nm, calendar=_replace(
        nm.calendar, machine_busy=busy,
        operator_busy={k: tuple(sorted(v)) for k, v in op_busy.items()}))
    stage2 = _decoded(nm2, cfg, second)

    for seg in stage2.segments:
        if not seg.machine_id:
            continue
        for bs, be in busy.get(seg.machine_id, ()):
            assert seg.end <= bs or seg.start >= be, (
                f"{seg.order_key} op {seg.op_seq} overlaps existing work on "
                f"{seg.machine_id}: {seg.start}-{seg.end} vs {bs}-{be}")


def test_no_operator_is_double_booked_across_the_two_stages(book):
    nm, cfg, orders = book
    first, second = orders[: len(orders) // 2], orders[len(orders) // 2:]
    stage1 = _decoded(nm, cfg, first)
    op_busy = {}
    for seg in stage1.segments:
        if seg.operator:
            op_busy.setdefault(seg.operator, []).append((seg.start, seg.end))
    nm2 = _replace(nm, calendar=_replace(
        nm.calendar, machine_busy=_busy_machine_ivs(stage1),
        operator_busy={k: tuple(sorted(v)) for k, v in op_busy.items()}))
    stage2 = _decoded(nm2, cfg, second)

    for seg in stage2.segments:
        if not seg.operator:
            continue
        for bs, be in op_busy.get(seg.operator, ()):
            assert seg.end <= bs or seg.start >= be, (
                f"{seg.operator} is in two places at once: "
                f"{seg.start}-{seg.end} vs {bs}-{be}")


def test_a_job_is_never_split_across_two_occupied_blocks(book):
    """The gap rule: one continuous engagement per operation, so the 90-minute
    setup is never paid twice."""
    nm, cfg, orders = book
    first, second = orders[: len(orders) // 2], orders[len(orders) // 2:]
    stage1 = _decoded(nm, cfg, first)
    busy = _busy_machine_ivs(stage1)
    nm2 = _replace(nm, calendar=_replace(nm.calendar, machine_busy=busy))
    stage2 = _decoded(nm2, cfg, second)

    spans = {}
    for seg in stage2.segments:
        if not seg.machine_id:
            continue
        k = (seg.order_key, seg.op_seq)
        lo, hi = spans.get(k, (seg.start, seg.end))
        spans[k] = (min(lo, seg.start), max(hi, seg.end))
    for (key, seq), (lo, hi) in spans.items():
        for bs, be in busy.get(
                next(s.machine_id for s in stage2.segments
                     if (s.order_key, s.op_seq) == (key, seq)), ()):
            assert not (lo < be and bs < hi), (
                f"{key} op {seq} spans an existing job ({lo}-{hi} across {bs}-{be})")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_quote_occupancy.py -k "occupied or split or double_booked" -v`
Expected: FAIL — stage-2 segments land on top of stage-1 work (occupancy is not consulted yet). `test_a_plan_with_no_occupancy_is_byte_identical` should already PASS; if it does not, stop — something is wrong before the feature even starts.

- [ ] **Step 3: Write minimal implementation**

In `ppc_engine/scheduler/flow_scheduler.py`, add the helper directly after `_lay_on_machine`:

```python
def _lay_in_free_run(machine, earliest, dur_min, order, op, op_qty, staffing,
                     masters, config):
    """Lay the op in the first stretch of ``machine``'s time that can hold it WHOLE.

    A "free run" is a stretch not already occupied by work an earlier planning stage
    committed (Add New Orders quote, 2026-09-08 spec). With no occupancy on file there
    is exactly ONE run — [earliest, forever) — and this is a single unbounded call to
    _lay_on_machine, i.e. byte-identical to what every plan does today.

    The op is never split across two runs: the machine would have been torn down for
    another job in between, and the 90-minute setup is not paid twice (owner rule).
    """
    for run_start, run_end in masters.calendar.free_runs(machine.id, earliest):
        laid = _lay_on_machine(machine, run_start, dur_min, order, op, op_qty,
                               staffing, masters, config, deadline=run_end)
        if laid is not None:
            return laid
    return None
```

In `_place_operation`, replace the single `_lay_on_machine` call in the machine loop:

```python
        laid = _lay_in_free_run(machine, earliest, dur, order, op, int(op_qty),
                                staffing, masters, config)
```

In `decode`, replace the board construction:

```python
    staffing = StaffingBoard(build_machine_pools(masters),
                             booked=masters.calendar.operator_busy,
                             assigned=masters.calendar.machine_shift_operator)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_quote_occupancy.py -v`
Expected: 23 passed.

Run: `python3.12 -m pytest -q`
Expected: full suite green, **including the golden trace**. If the golden trace moved, the byte-identical guarantee is broken — stop and fix, do not regenerate it.

- [ ] **Step 5: Commit**

```bash
git add ppc_engine/scheduler/flow_scheduler.py tests/test_quote_occupancy.py
git commit -m "feat(quote): place new work only in genuinely free stretches

_place_operation now walks the machine's free runs and lays each operation whole
inside one of them. With no occupancy there is a single unbounded run, so every
existing plan is byte-identical (asserted, and the golden trace is unchanged).

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Turn a finished plan into occupancy, and let a plan be told about it

**Files:**
- Modify: `engine/new_engine.py`, `engine/freeze.py` (one line), `engine/pipeline.py`
- Test: `tests/test_quote_engine.py`

**Interfaces:**
- Consumes: Tasks 1–5.
- Produces:
  - `engine.new_engine.OFF_LANES: frozenset[str]` = `{"OS / Outsourced", "Off-machine"}`.
  - `engine.new_engine.occupancy_from_entries(entries, config) -> dict` with keys `"machine"`, `"operator"`, `"assign"`, shaped exactly as `ShopCalendar.machine_busy` / `operator_busy` / `machine_shift_operator`.
  - `engine.new_engine.run(batches, config=None, ..., occupancy=None)`.
  - `engine.pipeline.run_forward(plan_run, config, masters, ..., occupancy=None)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_quote_engine.py`:

```python
"""engine/quote.py and its adapter layer (Add New Orders, 2026-09-08 spec)."""
import io
from datetime import date

import pytest

from engine import book_store, loaders, new_engine
from engine.config import Config
from engine.models import PlanRun
from engine.pipeline import run_forward
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
               apply_operator_logic=True)


@pytest.fixture()
def loaded():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    new_engine.set_masters_bytes(wb)
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    return so_lines, masters


def _plan(so_lines, masters, occupancy=None):
    pr = PlanRun(so_lines=list(so_lines))
    run_forward(pr, _CONF, masters, occupancy=occupancy)
    return pr.schedule


def test_off_lanes_have_one_definition():
    from engine import freeze
    assert freeze._OS_LANES is new_engine.OFF_LANES


def test_occupancy_lists_every_machine_block_and_skips_outsourcing(loaded):
    so_lines, masters = loaded
    entries = _plan(so_lines, masters)
    occ = new_engine.occupancy_from_entries(entries, _CONF)
    real = [e for e in entries if e.machine not in new_engine.OFF_LANES]
    assert sum(len(v) for v in occ["machine"].values()) == len(real)
    assert not (set(occ["machine"]) & new_engine.OFF_LANES)


def test_occupancy_lists_every_operator_segment(loaded):
    so_lines, masters = loaded
    entries = _plan(so_lines, masters)
    occ = new_engine.occupancy_from_entries(entries, _CONF)
    expected = sum(1 for e in entries for (_s, _e, name) in (e.op_segments or [])
                   if name)
    assert sum(len(v) for v in occ["operator"].values()) == expected


def test_planning_with_occupancy_keeps_new_work_off_occupied_machines(loaded):
    so_lines, masters = loaded
    half = max(1, len(so_lines) // 2)
    stage1 = _plan(so_lines[:half], masters)
    occ = new_engine.occupancy_from_entries(stage1, _CONF)
    stage2 = _plan(so_lines[half:], masters, occupancy=occ)

    busy = {}
    for e in stage1:
        if e.machine not in new_engine.OFF_LANES:
            busy.setdefault(e.machine, []).append((e.start, e.end))
    for e in stage2:
        for bs, be in busy.get(e.machine, ()):
            assert e.end <= bs or e.start >= be


def test_no_occupancy_plans_exactly_as_before(loaded):
    so_lines, masters = loaded
    a = _plan(so_lines, masters)
    b = _plan(so_lines, masters, occupancy=None)
    assert [(e.batch_id, e.process_seq, e.machine, e.operator, e.start, e.end)
            for e in a] == \
           [(e.batch_id, e.process_seq, e.machine, e.operator, e.start, e.end)
            for e in b]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_quote_engine.py -v`
Expected: FAIL — `AttributeError: module 'engine.new_engine' has no attribute 'OFF_LANES'`.

- [ ] **Step 3: Write minimal implementation**

**(a)** In `engine/new_engine.py`, near the top-level constants:

```python
# The two lanes that are not a machine: an outsourced step and an off-machine
# milestone. They occupy no in-house capacity, so they never appear in occupancy or
# in a frozen set. ONE definition — engine/freeze.py imports this one.
OFF_LANES = frozenset({"OS / Outsourced", "Off-machine"})
```

**(b)** In `engine/freeze.py`, replace `_OS_LANES = {"OS / Outsourced", "Off-machine"}` with:

```python
from engine.new_engine import OFF_LANES as _OS_LANES
```

**(c)** In `engine/new_engine.py`, add after `_with_unavailability`:

```python
def occupancy_from_entries(entries, config) -> dict:
    """A finished plan's placements, as the occupancy the NEXT planning stage must
    work around (Add New Orders quote, 2026-09-08 spec).

    Returns ``{"machine": {...}, "operator": {...}, "assign": {...}}`` shaped exactly
    like ShopCalendar's three occupancy fields. Outsourced and off-machine lanes are
    skipped: they hold no in-house machine and no person, so they occupy nothing.

    Reads the plan's own published output (machine, start, end and the per-shift
    operator segments) — it never re-derives a placement.
    """
    from ppc_engine.worktime import shift_key_for
    sched_cfg = _plan_config(config)
    machine: dict[str, list] = {}
    operator: dict[str, list] = {}
    assign: dict[tuple, str] = {}
    for e in entries or []:
        if e.machine in OFF_LANES or e.end <= e.start:
            continue
        machine.setdefault(e.machine, []).append((e.start, e.end))
        for seg_start, seg_end, name in (e.op_segments or []):
            if not name or seg_end <= seg_start:
                continue
            operator.setdefault(name, []).append((seg_start, seg_end))
            key = shift_key_for(seg_start, sched_cfg)
            if key is not None:
                assign[(e.machine, key[0], key[1])] = name
    return {
        "machine": {k: tuple(sorted(v)) for k, v in machine.items()},
        "operator": {k: tuple(sorted(v)) for k, v in operator.items()},
        "assign": assign,
    }
```

**(d)** In `engine/new_engine.py`, `run(...)`: add `occupancy=None` to the signature and fold it into the calendar right after `_with_unavailability`:

```python
def run(batches, config=None, notes=None, masters=None, machine_lost_min=None,
        reserved=None, frozen=None, occupancy=None, **kw):
```

```python
    if occupancy:
        from dataclasses import replace as _replace
        new_masters = _replace(new_masters, calendar=_replace(
            new_masters.calendar,
            machine_busy=occupancy.get("machine") or {},
            operator_busy=occupancy.get("operator") or {},
            machine_shift_operator=occupancy.get("assign") or {}))
```

Add to `run`'s docstring:

```
    ``occupancy`` (optional) is what an EARLIER planning stage already committed —
    see occupancy_from_entries. Present only on stage 2 of the Add New Orders quote;
    ``None`` everywhere else, which is byte-identical to before.
```

**(e)** In `engine/pipeline.py`, `run_forward`: add `occupancy: dict | None = None` to the signature, document it in the docstring alongside `frozen`, and build the Rule 6 kwargs so classic/flow never see it:

```python
        _sched_kw = dict(config=config, masters=masters,
                         machine_lost_min=machine_lost_min,
                         reserved=reserved, frozen=frozen)
        if occupancy:
            # New engine only. The retired classic/flow schedulers do not accept it,
            # and never receive it: occupancy is set only by the two-stage plan.
            _sched_kw["occupancy"] = occupancy
        plan_run.schedule = run_rule(
            trace, "rule6", scheduler_for(config), plan_run.batches_prioritized,
            **_sched_kw,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_quote_engine.py -v`
Expected: 5 passed.

Run: `python3.12 -m pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add engine/new_engine.py engine/freeze.py engine/pipeline.py tests/test_quote_engine.py
git commit -m "feat(quote): a finished plan becomes the next stage's occupancy

occupancy_from_entries reads the plan's own published output and hands it to the
next stage through the calendar. run_forward carries it only when set, so the
retired engines never see it. OFF_LANES now has one definition, shared with
engine/freeze.py.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: `engine/quote.py` — the two-stage quote and its search

**Files:**
- Create: `engine/quote.py`
- Test: `tests/test_quote_engine.py`

**Interfaces:**
- Consumes: `new_engine.occupancy_from_entries`, `run_forward(occupancy=)`, `optimizer.expected_completion`.
- Produces:

```python
@dataclass(frozen=True)
class QuoteLine:
    so_no: str
    item_code: str
    item_name: str
    qty: float
    target_date: date | None = None   # set only on the preponed path

@dataclass(frozen=True)
class QuoteResult:
    lines: list[dict]        # {so_no, item_code, item_name, qty, completion (date|None), error (str|None)}
    moved: list[dict]        # existing orders whose date changed — MUST be empty
    verified: bool           # True when moved == []
    existing_count: int

def quote(existing_entries, existing_expected, so_lines_existing, new_lines,
          config, masters, *, reserved=None, frozen=None) -> QuoteResult
```

`quote()` runs stage 2 over the new lines only, with stage 1's occupancy, trying each candidate arrangement, and returns the best. It re-checks stage 1's expected dates against `existing_expected` and refuses (`verified=False`) if any moved.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_quote_engine.py`:

```python
from engine import optimizer, quote as quote_mod


def _so_line_for(masters, item_code, so_no, qty, delivery):
    from engine.models import SOLine
    return SOLine(so_no=so_no, item_code=item_code,
                  item_name=masters.routings[item_code].description,
                  qty=qty, delivery_date=delivery)


def test_a_quote_returns_a_completion_date_for_each_new_line(loaded):
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    item = so_lines[0].item_code
    new = [quote_mod.QuoteLine("NEW-1", item, "x", 10)]
    res = quote_mod.quote(stage1, optimizer.expected_completion(stage1), so_lines,
                          new, _CONF, masters)
    assert len(res.lines) == 1
    assert res.lines[0]["completion"] is not None
    assert res.lines[0]["error"] is None


def test_a_quote_moves_no_existing_order(loaded):
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    before = optimizer.expected_completion(stage1)
    item = so_lines[0].item_code
    new = [quote_mod.QuoteLine("NEW-1", item, "x", 50),
           quote_mod.QuoteLine("NEW-2", so_lines[-1].item_code, "y", 20)]
    res = quote_mod.quote(stage1, before, so_lines, new, _CONF, masters)
    assert res.moved == []
    assert res.verified is True
    assert res.existing_count == len(before)


def test_a_new_order_never_finishes_before_the_plan_starts(loaded):
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    new = [quote_mod.QuoteLine("NEW-1", so_lines[0].item_code, "x", 10)]
    res = quote_mod.quote(stage1, optimizer.expected_completion(stage1), so_lines,
                          new, _CONF, masters)
    assert res.lines[0]["completion"] >= _CONF.plan_start_date


def test_an_item_with_no_routing_is_reported_not_crashed(loaded):
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    new = [quote_mod.QuoteLine("NEW-1", "NO-SUCH-ITEM", "ghost", 5)]
    res = quote_mod.quote(stage1, optimizer.expected_completion(stage1), so_lines,
                          new, _CONF, masters)
    assert res.lines[0]["completion"] is None
    assert "routing" in res.lines[0]["error"].lower()


def test_more_new_orders_never_pull_an_existing_order_earlier_or_later(loaded):
    """Stage 1 is literally the same calculation whatever stage 2 is asked to do."""
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    before = optimizer.expected_completion(stage1)
    item = so_lines[0].item_code
    for n in (1, 3, 6):
        new = [quote_mod.QuoteLine(f"NEW-{i}", item, "x", 25) for i in range(n)]
        res = quote_mod.quote(stage1, before, so_lines, new, _CONF, masters)
        assert res.moved == [], f"{n} new orders disturbed the book"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_quote_engine.py -k quote -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.quote'`.

- [ ] **Step 3: Write minimal implementation**

Create `engine/quote.py`:

```python
"""Quote a delivery date for a new order, against the plan actually in force.

Pure: no store, no HTTP, no side effects. See
docs/superpowers/specs/2026-09-08-add-new-orders-quote-design.md.

The one idea: the plan is computed in TWO stages. Stage 1 is today's plan over
today's book — the caller passes it in, already computed. Stage 2 plans the new
orders against a shop whose machines and people stage 1 has already occupied.

**Existing orders cannot move because they are not in stage 2 at all.** That is a
guarantee by construction, not a hope. `verify_unmoved` re-checks it anyway and the
quote refuses to show a date if it ever fails, because a promise nobody checks is
how the delay report ended up blaming the crew for outsourcing (2026-08-09).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field, replace
from datetime import date

from engine import new_engine, optimizer
from engine.models import PlanRun, SOLine
from engine.pipeline import run_forward

# Above this many new lines, trying every arrangement costs more than it is worth
# (5 lines = 120 arrangements). Below it the answer is provably the best available
# under the freeze; above it we try the typed order and a few rotations.
_EXHAUSTIVE_MAX = 4
_SAMPLED_ARRANGEMENTS = 24


@dataclass(frozen=True)
class QuoteLine:
    """One new order line the director typed. ``target_date`` is set only on the
    preponed path, where it becomes the line's delivery date."""
    so_no: str
    item_code: str
    item_name: str
    qty: float
    target_date: date | None = None


@dataclass(frozen=True)
class QuoteResult:
    lines: list = field(default_factory=list)
    moved: list = field(default_factory=list)
    verified: bool = True
    existing_count: int = 0


def _as_so_line(line: QuoteLine, plan_start: date) -> SOLine:
    """A new order line as the SOLine the rules consume. The delivery date is the
    director's target when he has one, else the plan start — a placeholder that
    affects only Rule 2's sort among the NEW lines (stage 2 has nothing else in it),
    never a promise."""
    return SOLine(so_no=line.so_no, item_code=line.item_code,
                  item_name=line.item_name, qty=float(line.qty),
                  delivery_date=line.target_date or plan_start)


def _arrangements(lines: list[QuoteLine]) -> list[list[QuoteLine]]:
    """The orderings of the new lines to try. Exhaustive while that is cheap."""
    if len(lines) <= 1:
        return [list(lines)]
    if len(lines) <= _EXHAUSTIVE_MAX:
        return [list(p) for p in itertools.permutations(lines)]
    out = [list(lines)]
    for i in range(1, min(_SAMPLED_ARRANGEMENTS, len(lines))):
        out.append(lines[i:] + lines[:i])
    return out


def _stage2(lines, occupancy, config, masters, flexible):
    """Plan the new lines only, around the occupancy. Returns (entries, expected)."""
    cfg = replace(config, flexible_machines=flexible)
    pr = PlanRun(so_lines=[_as_so_line(l, config.plan_start_date) for l in lines])
    run_forward(pr, cfg, masters, occupancy=occupancy)
    return pr.schedule, optimizer.expected_completion(pr.schedule)


def _score(lines, expected):
    """Lower is better: total days late against each line's target (0 when he has
    not named one), then the latest completion, then the sum — so an arrangement
    that finishes everything sooner wins."""
    late = 0
    latest = date.min
    total = 0
    for line in lines:
        got = expected.get((line.so_no, line.item_code))
        if got is None:
            return (10**9, date.max, 10**9)
        if line.target_date and got > line.target_date:
            late += (got - line.target_date).days
        latest = max(latest, got)
        total += got.toordinal()
    return (late, latest, total)


def verify_unmoved(existing_expected, existing_expected_now):
    """Every existing order whose expected completion changed. MUST be empty."""
    moved = []
    for key, before in existing_expected.items():
        after = existing_expected_now.get(key)
        if after is not None and after != before:
            moved.append({"so_no": key[0], "item_code": key[1],
                          "before": before, "after": after,
                          "days": (after - before).days})
    moved.sort(key=lambda m: abs(m["days"]), reverse=True)
    return moved


def quote(existing_entries, existing_expected, so_lines_existing, new_lines,
          config, masters, *, reserved=None, frozen=None) -> QuoteResult:
    """Quote each new line's completion date against the plan already in force.

    ``existing_entries`` is stage 1's finished schedule (the plan on screen) and
    ``existing_expected`` its per-order completion dates, both computed by the caller
    so the quote never plans the existing book a second way.
    """
    routable, unroutable = [], []
    for line in new_lines:
        (routable if line.item_code in masters.routings else unroutable).append(line)

    rows = {l.so_no + "\x1f" + l.item_code:
            {"so_no": l.so_no, "item_code": l.item_code, "item_name": l.item_name,
             "qty": l.qty, "target_date": l.target_date, "completion": None,
             "error": "no routing found for this item — it cannot be scheduled"}
            for l in unroutable}

    best_score, best_expected, failure = None, None, None
    if routable:
        occupancy = new_engine.occupancy_from_entries(existing_entries, config)
        for flexible in (False, True):
            for arrangement in _arrangements(routable):
                try:
                    _entries, expected = _stage2(arrangement, occupancy, config,
                                                 masters, flexible)
                except Exception as exc:            # noqa: BLE001 — report, never 500
                    failure = failure or str(exc)
                    continue
                score = _score(arrangement, expected)
                if best_score is None or score < best_score:
                    best_score, best_expected = score, expected
        for line in routable:
            got = best_expected.get((line.so_no, line.item_code)) if best_expected else None
            rows[line.so_no + "\x1f" + line.item_code] = {
                "so_no": line.so_no, "item_code": line.item_code,
                "item_name": line.item_name, "qty": line.qty,
                "target_date": line.target_date, "completion": got,
                "error": None if got else (
                    failure or "could not be scheduled: no machine with a "
                               "qualified operator is available for one of its steps"),
            }

    moved = verify_unmoved(existing_expected,
                           optimizer.expected_completion(existing_entries))
    ordered = [rows[l.so_no + "\x1f" + l.item_code] for l in new_lines]
    return QuoteResult(lines=ordered, moved=moved, verified=not moved,
                       existing_count=len(existing_expected))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_quote_engine.py -v`
Expected: 10 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/quote.py tests/test_quote_engine.py
git commit -m "feat(quote): the pure two-stage quote

Plans the new lines against stage 1's occupancy, tries the arrangements that are
free (sequence and machine set), and verifies that not one existing order moved
before it will show a date.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Storage for the typed lines and the arrival queue

**Files:**
- Modify: `engine/book_store.py`
- Test: `tests/test_new_orders_api.py`

**Interfaces:**
- Produces:
  - `DRAFT_ORDERS_KEY = "anvitech:new_order_drafts"`, `NEW_ORDER_QUEUE_KEY = "anvitech:new_order_queue"`
  - `load_new_order_drafts() -> list[dict]` / `save_new_order_drafts(rows: list[dict]) -> None`
  - `load_new_order_queue() -> list[list[str]]` (list of `[so_no, item_code]`) / `save_new_order_queue(keys) -> None` / `clear_new_order_queue() -> None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_new_orders_api.py`:

```python
"""Add New Orders: storage, endpoints, role gating (2026-09-08 spec)."""
from engine import book_store


def test_drafts_round_trip():
    book_store.save_new_order_drafts([{"so_no": "NEW-1", "item_code": "X",
                                       "item_name": "x", "qty": 10}])
    assert book_store.load_new_order_drafts() == [
        {"so_no": "NEW-1", "item_code": "X", "item_name": "x", "qty": 10}]


def test_drafts_default_to_empty():
    book_store.save_new_order_drafts([])
    assert book_store.load_new_order_drafts() == []


def test_queue_round_trips_and_clears():
    book_store.save_new_order_queue([("NEW-1", "X"), ("NEW-2", "Y")])
    assert book_store.load_new_order_queue() == [["NEW-1", "X"], ["NEW-2", "Y"]]
    book_store.clear_new_order_queue()
    assert book_store.load_new_order_queue() == []
```

Check `tests/conftest.py` for how the store is isolated per test and follow it — do not write to a real store.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_new_orders_api.py -v`
Expected: FAIL — `AttributeError: module 'engine.book_store' has no attribute 'save_new_order_drafts'`.

- [ ] **Step 3: Write minimal implementation**

In `engine/book_store.py`, beside the other key constants and loaders (match the file's existing JSON-blob pattern, e.g. `load_absences`/`save_absence`):

```python
DRAFT_ORDERS_KEY = "anvitech:new_order_drafts"
NEW_ORDER_QUEUE_KEY = "anvitech:new_order_queue"


def load_new_order_drafts() -> list:
    """The order lines a director has typed into Add New Orders but not yet added.
    Server-side so a refresh or a sleeping instance never loses half-typed work."""
    raw = get_store().get(DRAFT_ORDERS_KEY)
    return json.loads(raw) if raw else []


def save_new_order_drafts(rows) -> None:
    get_store().set(DRAFT_ORDERS_KEY, json.dumps(list(rows)))


def load_new_order_queue() -> list:
    """(SO number, item code) pairs planned BEHIND the existing book — the arrival
    queue (first come, first served). Cleared by the next full optimization."""
    raw = get_store().get(NEW_ORDER_QUEUE_KEY)
    return json.loads(raw) if raw else []


def save_new_order_queue(keys) -> None:
    get_store().set(NEW_ORDER_QUEUE_KEY, json.dumps([list(k) for k in keys]))


def clear_new_order_queue() -> None:
    get_store().delete(NEW_ORDER_QUEUE_KEY)
```

If the store interface has no `delete`, follow whatever `clear_frozen_ops` does and mirror it exactly.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_new_orders_api.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/book_store.py tests/test_new_orders_api.py
git commit -m "feat(quote): store the typed lines and the arrival queue

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Validating a typed line

**Files:**
- Modify: `engine/orderbook.py`
- Test: `tests/test_new_orders_api.py`

**Interfaces:**
- Produces: `validate_new_order_line(so_no, item_code, qty, active, completed, masters) -> str | None` — the error message for the director, or `None` when the line is good. Pure.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_new_orders_api.py`:

```python
import io
from datetime import date

from engine import loaders, orderbook
from engine.models import Order
from tests.new_sample_workbook import build_new_sample_bytes


def _masters():
    _so, masters = loaders.load_all(io.BytesIO(build_new_sample_bytes()))
    return masters


def _an_item(masters):
    return sorted(masters.routings)[0]


def test_a_good_line_validates():
    m = _masters()
    assert orderbook.validate_new_order_line("NEW-1", _an_item(m), 10, {}, {}, m) is None


def test_a_blank_so_number_is_refused():
    m = _masters()
    assert "SO number" in orderbook.validate_new_order_line("  ", _an_item(m), 10, {}, {}, m)


def test_a_zero_or_negative_quantity_is_refused():
    m = _masters()
    for bad in (0, -5):
        assert "quantity" in orderbook.validate_new_order_line("NEW-1", _an_item(m),
                                                               bad, {}, {}, m).lower()


def test_an_unknown_item_is_refused():
    m = _masters()
    assert "routing" in orderbook.validate_new_order_line("NEW-1", "GHOST", 10,
                                                          {}, {}, m).lower()


def test_a_duplicate_names_where_it_already_is():
    m = _masters()
    item = _an_item(m)
    existing = Order(so_no="NEW-1", item_code=item, item_name="x", ordered_qty=300,
                     delivery_date=date(2025, 3, 20))
    msg = orderbook.validate_new_order_line("new-1", item, 10,
                                            {existing.key: existing}, {}, m)
    assert "already in the book" in msg
    assert "300" in msg


def test_a_duplicate_of_a_COMPLETED_order_is_also_refused():
    m = _masters()
    item = _an_item(m)
    done = Order(so_no="NEW-1", item_code=item, item_name="x", ordered_qty=300,
                 delivery_date=date(2025, 3, 20), completed=True)
    assert "already in the book" in orderbook.validate_new_order_line(
        "NEW-1", item, 10, {}, {done.key: done}, m)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_new_orders_api.py -k validate -v`
Expected: FAIL — `AttributeError: module 'engine.orderbook' has no attribute 'validate_new_order_line'`.

- [ ] **Step 3: Write minimal implementation**

In `engine/orderbook.py`:

```python
def validate_new_order_line(so_no, item_code, qty, active, completed, masters):
    """The message to show the director for a line typed into Add New Orders, or
    None when it is good. Pure (2026-09-08 spec).

    A duplicate is refused rather than warned: (SO number, item code) is the identity
    of an order everywhere in this system, so a second one would overwrite the first,
    including its recorded production.
    """
    so = (so_no or "").strip()
    item = (item_code or "").strip()
    if not so:
        return "Enter an SO number."
    if not item:
        return "Pick an item."
    try:
        q = float(qty)
    except (TypeError, ValueError):
        return "Enter a quantity."
    if q <= 0 or q != int(q):
        return "Enter a whole quantity greater than zero."
    if item not in masters.routings:
        return (f"Item {item} has no routing in the Item's Process Master, "
                f"so it cannot be scheduled.")
    for book in (active or {}), (completed or {}):
        for order in book.values():
            if (order.so_no.strip().lower() == so.lower()
                    and order.item_code.strip().lower() == item.lower()):
                return (f"{order.so_no} / {order.item_code} is already in the book — "
                        f"{int(order.ordered_qty)} pieces, delivery "
                        f"{order.delivery_date.strftime('%d-%b')}.")
    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_new_orders_api.py -v`
Expected: 9 passed.

- [ ] **Step 5: Commit**

```bash
git add engine/orderbook.py tests/test_new_orders_api.py
git commit -m "feat(quote): validate a typed order line

A duplicate is refused, naming where it already is: that pair is an order's
identity, so a second one would overwrite recorded production.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Draft endpoints and the quote endpoint

**Files:**
- Modify: `api/main.py`
- Test: `tests/test_new_orders_api.py`

**Interfaces:**
- Produces:
  - `GET /new-orders/drafts` (admin) → `{"drafts": [...], "items": [{"item_code","item_name"}], "errors": {index: message}}`
  - `PUT /new-orders/drafts` (admin, body `{"drafts": [{so_no, item_code, qty}]}`) → validated rows or 400 naming the first bad line
  - `POST /new-orders/quote` (admin) → `{"lines": [...], "verified": bool, "existing_count": int, "moved": [...], "stamp": "<plan fingerprint>"}`

Follow the existing endpoint style: `require_admin(request)` first, Pydantic request models beside the others.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_new_orders_api.py` (copy the client/login fixture pattern from `tests/test_machine_downtime_api.py` — reuse it, do not invent a new one):

```python
def test_drafts_are_admin_only(user_client):
    assert user_client.get("/new-orders/drafts").status_code == 403
    assert user_client.put("/new-orders/drafts", json={"drafts": []}).status_code == 403
    assert user_client.post("/new-orders/quote").status_code == 403


def test_saving_and_reading_back_drafts(admin_client, uploaded_masters):
    item = uploaded_masters
    r = admin_client.put("/new-orders/drafts",
                         json={"drafts": [{"so_no": "NEW-1", "item_code": item,
                                           "qty": 10}]})
    assert r.status_code == 200
    got = admin_client.get("/new-orders/drafts").json()
    assert got["drafts"][0]["so_no"] == "NEW-1"
    assert got["drafts"][0]["item_name"]
    assert any(i["item_code"] == item for i in got["items"])


def test_a_bad_draft_line_is_refused_with_a_message(admin_client, uploaded_masters):
    r = admin_client.put("/new-orders/drafts",
                         json={"drafts": [{"so_no": "", "item_code": uploaded_masters,
                                           "qty": 10}]})
    assert r.status_code == 400
    assert "SO number" in r.json()["detail"]


def test_a_quote_returns_a_date_and_confirms_nothing_moved(admin_client,
                                                           uploaded_masters):
    admin_client.put("/new-orders/drafts",
                     json={"drafts": [{"so_no": "NEW-1",
                                       "item_code": uploaded_masters, "qty": 10}]})
    r = admin_client.post("/new-orders/quote")
    assert r.status_code == 200
    body = r.json()
    assert body["verified"] is True
    assert body["moved"] == []
    assert body["lines"][0]["completion"]
    assert body["existing_count"] >= 0
    assert body["stamp"]


def test_quoting_with_no_drafts_is_a_clear_400(admin_client, uploaded_masters):
    admin_client.put("/new-orders/drafts", json={"drafts": []})
    r = admin_client.post("/new-orders/quote")
    assert r.status_code == 400
    assert "no new orders" in r.json()["detail"].lower()
```

Add fixtures `admin_client`, `user_client` and `uploaded_masters` (uploads the sample workbook and returns one item code) at the top of the file, modelled on the existing API test files.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_new_orders_api.py -k "draft or quote" -v`
Expected: FAIL — 404 on the new routes.

- [ ] **Step 3: Write minimal implementation**

In `api/main.py`, beside the other request models:

```python
class DraftLine(BaseModel):
    so_no: str
    item_code: str
    qty: float


class DraftsRequest(BaseModel):
    drafts: list[DraftLine] = []
```

Helpers (place near `_orders_table`):

```python
def _quote_item_options(masters):
    """Every item that can actually be scheduled, for the entry form's dropdown."""
    return [{"item_code": code, "item_name": routing.description}
            for code, routing in sorted(masters.routings.items())]


def _draft_quote_lines(drafts, masters):
    """Stored draft rows -> QuoteLine list (item name filled in from the master)."""
    from engine.quote import QuoteLine
    out = []
    for row in drafts:
        code = row.get("item_code", "")
        routing = masters.routings.get(code)
        out.append(QuoteLine(
            so_no=row.get("so_no", ""), item_code=code,
            item_name=routing.description if routing else row.get("item_name", ""),
            qty=float(row.get("qty") or 0),
            target_date=(date.fromisoformat(row["target_date"])
                         if row.get("target_date") else None)))
    return out
```

Endpoints:

```python
@app.get("/new-orders/drafts")
def new_order_drafts(request: Request):
    require_admin(request)
    masters = _current_masters()
    drafts = book_store.load_new_order_drafts()
    for row in drafts:
        routing = masters.routings.get(row.get("item_code", ""))
        row["item_name"] = routing.description if routing else ""
    return {"drafts": drafts, "items": _quote_item_options(masters),
            "queued": book_store.load_new_order_queue()}


@app.put("/new-orders/drafts")
def save_new_order_drafts_ep(req: DraftsRequest, request: Request):
    require_admin(request)
    masters = _current_masters()
    active = book_store.load_active_orders()
    completed = book_store.load_completed_orders()
    rows = []
    for line in req.drafts:
        err = orderbook.validate_new_order_line(line.so_no, line.item_code, line.qty,
                                                active, completed, masters)
        if err:
            raise HTTPException(status_code=400, detail=err)
        rows.append({"so_no": line.so_no.strip(), "item_code": line.item_code.strip(),
                     "qty": int(line.qty)})
    seen = set()
    for row in rows:
        key = (row["so_no"].lower(), row["item_code"].lower())
        if key in seen:
            raise HTTPException(
                status_code=400,
                detail=f"{row['so_no']} / {row['item_code']} is entered twice.")
        seen.add(key)
    book_store.save_new_order_drafts(rows)
    return {"drafts": rows}


@app.post("/new-orders/quote")
def new_order_quote(request: Request):
    """Quote each draft line against the plan in force. Read-only: nothing is saved."""
    require_admin(request)
    from engine import quote as quote_mod
    drafts = book_store.load_new_order_drafts()
    if not drafts:
        raise HTTPException(status_code=400, detail="There are no new orders to quote.")
    config = _load_plan_config()
    _plan(config)                                  # ensures the artifacts are fresh
    art = _PLAN_CACHE.get("artifacts") or {}
    plan_run = art.get("plan_run")
    if plan_run is None:
        raise HTTPException(status_code=503,
                            detail="The plan is not available right now. Try again.")
    masters = art.get("masters") or _current_masters()
    cfg = art.get("config") or _resolve_config(config)
    existing_expected = optimizer.expected_completion(plan_run.schedule)
    res = quote_mod.quote(plan_run.schedule, existing_expected,
                          art.get("so_lines") or [],
                          _draft_quote_lines(drafts, masters), cfg, masters)
    return {
        "lines": [{**row,
                   "completion": row["completion"].isoformat() if row["completion"] else None,
                   "target_date": row["target_date"].isoformat() if row["target_date"] else None}
                  for row in res.lines],
        "moved": [{**m, "before": m["before"].isoformat(), "after": m["after"].isoformat()}
                  for m in res.moved],
        "verified": res.verified,
        "existing_count": res.existing_count,
        "stamp": _plan_fingerprint(config),
    }
```

If `_PLAN_CACHE["artifacts"]` does not already carry `plan_run`, `masters`, `so_lines` and `config`, extend the dict where `_plan` writes it — it is documented in CLAUDE.md as carrying exactly these.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_new_orders_api.py -v`
Expected: all passed.

Run: `python3.12 -m pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_new_orders_api.py
git commit -m "feat(quote): draft lines and the quote endpoint

Admin only, server-enforced. The quote is read-only and carries a stamp of the
plan it was computed against.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: The two-stage plan and the arrival queue

**Files:**
- Modify: `api/main.py` (`_plan`, `_plan_fingerprint`)
- Test: `tests/test_arrival_queue.py`

**Interfaces:**
- Consumes: `book_store.load_new_order_queue()`, `new_engine.occupancy_from_entries`, `run_forward(occupancy=)`.
- Produces: `_plan` returns a response whose schedule is stage 1 + stage 2 merged whenever the queue is non-empty; `_plan_fingerprint` includes the queue.

- [ ] **Step 1: Write the failing test**

Create `tests/test_arrival_queue.py`:

```python
"""First come, first served: a queued new order never moves the book (2026-09-08)."""
from engine import book_store


def test_with_an_empty_queue_the_plan_is_unchanged(admin_client, uploaded_masters):
    book_store.clear_new_order_queue()
    a = admin_client.post("/run").json()
    b = admin_client.post("/run").json()
    assert {r["SO No"]: r.get("Expected completion") for r in a["orders"]} == \
           {r["SO No"]: r.get("Expected completion") for r in b["orders"]}


def test_a_queued_order_does_not_move_any_existing_order(admin_client,
                                                         uploaded_masters,
                                                         add_new_order):
    before = {(r["SO No"], r["Item Code"]): r.get("Expected completion")
              for r in admin_client.post("/run").json()["orders"]}
    add_new_order("NEW-1", uploaded_masters, 25)
    after = {(r["SO No"], r["Item Code"]): r.get("Expected completion")
             for r in admin_client.post("/run").json()["orders"]}
    for key, date_before in before.items():
        assert after.get(key) == date_before, f"{key} moved"


def test_a_queued_order_appears_in_the_plan_with_a_date(admin_client,
                                                        uploaded_masters,
                                                        add_new_order):
    quoted = add_new_order("NEW-1", uploaded_masters, 25)
    rows = {(r["SO No"], r["Item Code"]): r
            for r in admin_client.post("/run").json()["orders"]}
    assert ("NEW-1", uploaded_masters) in rows
    assert rows[("NEW-1", uploaded_masters)].get("Expected completion") == quoted


def test_orders_added_one_at_a_time_never_disturb_each_other(admin_client,
                                                             uploaded_masters,
                                                             add_new_order):
    first = add_new_order("NEW-1", uploaded_masters, 25)
    add_new_order("NEW-2", uploaded_masters, 25)
    rows = {(r["SO No"], r["Item Code"]): r
            for r in admin_client.post("/run").json()["orders"]}
    assert rows[("NEW-1", uploaded_masters)].get("Expected completion") == first


def test_the_plan_cache_notices_the_queue(admin_client, uploaded_masters,
                                          add_new_order):
    admin_client.post("/run")
    add_new_order("NEW-1", uploaded_masters, 25)
    rows = {r["SO No"] for r in admin_client.post("/run").json()["orders"]}
    assert "NEW-1" in rows, "a cached plan was served after the book changed"


def test_a_deleted_queued_order_drops_out_without_special_handling(
        admin_client, uploaded_masters, add_new_order):
    add_new_order("NEW-1", uploaded_masters, 25)
    admin_client.post("/orders/delete",
                      json={"keys": [["NEW-1", uploaded_masters]],
                            "password": "1930rail"})
    body = admin_client.post("/run").json()
    assert all(r["SO No"] != "NEW-1" for r in body["orders"])
```

Match `/orders/delete`'s real request shape and the test password used by the existing
delete tests — read `tests/test_api.py` rather than assuming.

Add an `add_new_order(so_no, item_code, qty) -> str` fixture that PUTs a draft, POSTs the quote, POSTs the add, and returns the quoted completion date string.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_arrival_queue.py -v`
Expected: FAIL — the add endpoint does not exist yet (Task 12) and `_plan` is single-stage. Write Task 12's endpoint first if the fixture cannot run; the two tasks may be done in either order but both must be green before moving on.

- [ ] **Step 3: Write minimal implementation**

In `_plan_fingerprint`, add the queue alongside `book_rows`:

```python
    parts.append("queue=" + json.dumps(book_store.load_new_order_queue(),
                                       sort_keys=True))
```

In `_plan`, after `so_lines = orderbook.active_so_lines(...)` split the book, and after the existing `run_forward` call add stage 2:

```python
    # First come, first served (2026-09-08 spec): lines added through Add New Orders
    # are planned BEHIND the book that was already there, so accepting a quote can
    # never move an existing order's date. The queue is cleared by the next full
    # optimization (Done entering / an applied deep search / an upload), after which
    # these orders are ranked on merit like everything else.
    queued_keys = {tuple(k) for k in book_store.load_new_order_queue()}
    queued_lines = [l for l in so_lines if l.key in queued_keys]
    if queued_lines:
        so_lines = [l for l in so_lines if l.key not in queued_keys]
```

A queued key that is no longer an active line — the order was deleted, or produced and
marked complete — simply does not match, so it drops out of stage 2 with no special
handling. That is the spec's "it clears itself" behaviour, by construction.

...then, after the main `run_forward(...)` call and before `_augment_helpers`:

```python
    if queued_lines:
        stage2 = PlanRun(so_lines=queued_lines)
        occupancy = new_engine.occupancy_from_entries(plan_run.schedule, config)
        run_forward(stage2, ranked_config, masters, reserved=ab or None,
                    frozen=None, occupancy=occupancy)
        plan_run.schedule = list(plan_run.schedule) + list(stage2.schedule)
        trace["rule6"]["output"] = to_table(plan_run.schedule)
        trace["rule6"]["notes"].append(
            f"{len(queued_lines)} newly added order(s) are planned behind the "
            f"existing book (first come, first served) until the next plan update.")
```

`frozen=None` on stage 2 is deliberate: a brand-new order cannot be half-finished, and the frozen set only ever describes in-progress work.

Import `to_table` from `engine.pipeline` if it is not already imported in `api/main.py`.

Add the note the Orders tab shows, in the same dict the response already carries:

```python
    queue_note = (f"{len(queued_lines)} new order(s) are planned behind the existing "
                  f"book until the next plan update.") if queued_lines else ""
```

and include `"queue_note": queue_note` in the `/run` response body (and in the cache-hit branch, rebuilt live like `orders` and `auto_note`).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_arrival_queue.py -v`
Expected: 5 passed.

Run: `python3.12 -m pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_arrival_queue.py
git commit -m "feat(quote): plan newly added orders behind the existing book

First come, first served. Stage 1 is today's plan, untouched; the queued orders
are planned against its occupancy, so accepting a quote moves nothing. The queue
is part of the plan fingerprint, or a cached plan would hide it.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Accepting a quote, and clearing the queue

**Files:**
- Modify: `api/main.py`
- Test: `tests/test_new_orders_api.py`

**Interfaces:**
- Produces: `POST /new-orders/add` (admin, body `{"stamp": "<from the quote>"}`) → `{"added": n, "orders": [...]}`; 409 when the stamp no longer matches the current plan fingerprint.
- Queue clearing wired into `POST /optimize/done`, `_optimize_apply()` and `POST /upload`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_new_orders_api.py`:

```python
def test_adding_saves_the_orders_with_the_quoted_delivery_date(admin_client,
                                                               uploaded_masters):
    admin_client.put("/new-orders/drafts",
                     json={"drafts": [{"so_no": "NEW-1",
                                       "item_code": uploaded_masters, "qty": 25}]})
    quote = admin_client.post("/new-orders/quote").json()
    r = admin_client.post("/new-orders/add", json={"stamp": quote["stamp"]})
    assert r.status_code == 200
    rows = {(o["SO No"], o["Item Code"]): o
            for o in admin_client.get("/orders").json()["orders"]}
    row = rows[("NEW-1", uploaded_masters)]
    assert row["Delivery Date"] == quote["lines"][0]["completion"]


def test_adding_clears_the_drafts(admin_client, uploaded_masters):
    admin_client.put("/new-orders/drafts",
                     json={"drafts": [{"so_no": "NEW-1",
                                       "item_code": uploaded_masters, "qty": 25}]})
    quote = admin_client.post("/new-orders/quote").json()
    admin_client.post("/new-orders/add", json={"stamp": quote["stamp"]})
    assert admin_client.get("/new-orders/drafts").json()["drafts"] == []


def test_a_stale_quote_is_refused_and_says_to_requote(admin_client, uploaded_masters):
    admin_client.put("/new-orders/drafts",
                     json={"drafts": [{"so_no": "NEW-1",
                                       "item_code": uploaded_masters, "qty": 25}]})
    admin_client.post("/new-orders/quote")
    r = admin_client.post("/new-orders/add", json={"stamp": "not-the-real-stamp"})
    assert r.status_code == 409
    assert "Finish and Optimize" in r.json()["detail"]


def test_adding_is_admin_only(user_client):
    assert user_client.post("/new-orders/add", json={"stamp": "x"}).status_code == 403


def test_done_entering_clears_the_queue(admin_client, uploaded_masters, add_new_order):
    add_new_order("NEW-1", uploaded_masters, 25)
    assert book_store.load_new_order_queue()
    admin_client.post("/optimize/done")
    assert book_store.load_new_order_queue() == []


def test_an_upload_clears_the_queue(admin_client, uploaded_masters, add_new_order,
                                    upload_sample):
    add_new_order("NEW-1", uploaded_masters, 25)
    upload_sample()
    assert book_store.load_new_order_queue() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_new_orders_api.py -k "add or clears or stale" -v`
Expected: FAIL — 404 on `/new-orders/add`.

- [ ] **Step 3: Write minimal implementation**

```python
class AddNewOrdersRequest(BaseModel):
    stamp: str


@app.post("/new-orders/add")
def add_new_orders(req: AddNewOrdersRequest, request: Request):
    """Save the quoted lines into the order book with the quoted date as their SO
    delivery date, and queue them behind the book that was already there."""
    require_admin(request)
    from engine import quote as quote_mod
    config = _load_plan_config()
    if req.stamp != _plan_fingerprint(config):
        raise HTTPException(
            status_code=409,
            detail=("The plan has changed since this quote. "
                    "Press Finish and Optimize again to get a fresh date."))
    drafts = book_store.load_new_order_drafts()
    if not drafts:
        raise HTTPException(status_code=400, detail="There are no new orders to add.")

    _plan(config)
    art = _PLAN_CACHE.get("artifacts") or {}
    plan_run, masters = art.get("plan_run"), art.get("masters") or _current_masters()
    cfg = art.get("config") or _resolve_config(config)
    res = quote_mod.quote(plan_run.schedule,
                          optimizer.expected_completion(plan_run.schedule),
                          art.get("so_lines") or [],
                          _draft_quote_lines(drafts, masters), cfg, masters)
    if not res.verified:
        raise HTTPException(
            status_code=409,
            detail="The quote could not be confirmed: existing orders moved. "
                   "Press Finish and Optimize again.")
    bad = [r for r in res.lines if r["completion"] is None]
    if bad:
        raise HTTPException(status_code=400, detail=bad[0]["error"])

    today = _ist_today().isoformat()
    orders = [Order(so_no=r["so_no"], item_code=r["item_code"],
                    item_name=r["item_name"], ordered_qty=float(r["qty"]),
                    delivery_date=r["completion"], first_seen=today)
              for r in res.lines]
    book_store.add_orders(orders)
    book_store.save_new_order_queue(
        [tuple(k) for k in book_store.load_new_order_queue()]
        + [(o.so_no, o.item_code) for o in orders])
    book_store.save_new_order_drafts([])
    _PLAN_CACHE["key"] = None      # the book changed; never serve the old response
    return {"added": len(orders),
            "orders": [{"so_no": o.so_no, "item_code": o.item_code,
                        "delivery_date": o.delivery_date.isoformat()} for o in orders]}
```

Then clear the queue in three places, each with a one-line comment saying why:

- in `optimize_done_ep`, immediately before `_try_start_auto(...)`:
  ```python
  # A full re-optimization treats every order equally, so arrival positions end here.
  book_store.clear_new_order_queue()
  ```
- at the end of `_optimize_apply()`, same comment.
- at the end of `upload(...)`, after `book_store.add_orders(...)`:
  ```python
  # A fresh upload changes the book the queue's positions referred to.
  book_store.clear_new_order_queue()
  ```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_new_orders_api.py tests/test_arrival_queue.py -v`
Expected: all passed.

Run: `python3.12 -m pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_new_orders_api.py
git commit -m "feat(quote): accept a quote, and end the queue at the next full search

The quoted date is saved as the SO delivery date. A quote computed against a plan
that has since moved is refused, not silently used. Done entering, an applied
deep search and an upload each clear the queue.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: The preponed path

**Files:**
- Modify: `api/main.py`
- Test: `tests/test_new_orders_api.py`

**Interfaces:**
- Produces:
  - `_start_optimize(..., extra_orders=None)` — draft lines joined into the contest **in memory only**.
  - `POST /new-orders/prepone` (admin, body `{"targets": {"<so>\x1f<item>": "YYYY-MM-DD"}}`) → starts the deep search, `409` when one is already running.
  - `POST /new-orders/prepone/accept` (admin) → saves the orders with the **typed** dates and calls `_optimize_apply()`.
  - The optimize status payload gains `"kind": "quote" | "plan"` so the Settings panel never offers Apply for a quote run.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_new_orders_api.py`:

```python
def test_prepone_is_admin_only(user_client):
    assert user_client.post("/new-orders/prepone", json={"targets": {}}).status_code == 403


def test_prepone_refuses_when_a_search_is_already_running(admin_client,
                                                          uploaded_masters,
                                                          running_optimize):
    r = admin_client.post("/new-orders/prepone",
                          json={"targets": {"NEW-1\x1f" + uploaded_masters: "2025-03-20"}})
    assert r.status_code == 409


def test_prepone_stores_the_typed_target_on_the_draft(admin_client, uploaded_masters,
                                                      monkeypatch):
    monkeypatch.setattr("api.main._start_optimize", lambda *a, **k: None)
    admin_client.put("/new-orders/drafts",
                     json={"drafts": [{"so_no": "NEW-1",
                                       "item_code": uploaded_masters, "qty": 25}]})
    admin_client.post("/new-orders/prepone",
                      json={"targets": {"NEW-1\x1f" + uploaded_masters: "2025-03-20"}})
    assert book_store.load_new_order_drafts()[0]["target_date"] == "2025-03-20"


def test_accepting_a_preponed_result_uses_the_typed_date_not_the_achieved_one(
        admin_client, uploaded_masters, finished_quote_optimize):
    admin_client.post("/new-orders/prepone/accept")
    rows = {(o["SO No"], o["Item Code"]): o
            for o in admin_client.get("/orders").json()["orders"]}
    assert rows[("NEW-1", uploaded_masters)]["Delivery Date"] == "2025-03-20"


def test_accepting_a_preponed_result_creates_no_queue(admin_client, uploaded_masters,
                                                      finished_quote_optimize):
    admin_client.post("/new-orders/prepone/accept")
    assert book_store.load_new_order_queue() == []
```

Fixtures `running_optimize` and `finished_quote_optimize` set `api.main._OPTIMIZE` directly, as the existing optimize tests do — copy that pattern rather than running a real contest.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_new_orders_api.py -k prepone -v`
Expected: FAIL — 404.

- [ ] **Step 3: Write minimal implementation**

**(a)** `_start_optimize(budget_evals, label, background=True, auto=False, extra_orders=None)`. After `orders = book_store.load_active_orders()`:

```python
        if extra_orders:
            # Draft lines from Add New Orders, joined to the book IN MEMORY ONLY for
            # the length of this search (2026-09-08 spec). Nothing is saved unless the
            # director accepts the result.
            orders = {**orders, **{o.key: o for o in extra_orders}}
```

Record the run's kind so the Settings panel cannot apply it:

```python
        _OPTIMIZE["kind"] = "quote" if extra_orders else "plan"
```

and include `"kind": _OPTIMIZE.get("kind", "plan")` in `_optimize_status()`.

**(b)**

```python
class PreponeRequest(BaseModel):
    targets: dict[str, str] = {}


@app.post("/new-orders/prepone")
def prepone_new_orders(req: PreponeRequest, request: Request):
    """Drop the freeze: re-optimize the whole book with the new orders carrying the
    dates the director typed. Existing orders may move; that is the point, and the
    result screen reports every one that does."""
    require_admin(request)
    drafts = book_store.load_new_order_drafts()
    if not drafts:
        raise HTTPException(status_code=400, detail="There are no new orders.")
    masters = _current_masters()
    for row in drafts:
        target = req.targets.get(row["so_no"] + "\x1f" + row["item_code"])
        if not target:
            raise HTTPException(
                status_code=400,
                detail=f"Give {row['so_no']} a date, or accept the quoted one.")
        try:
            date.fromisoformat(target)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{target} is not a date.")
        row["target_date"] = target
    book_store.save_new_order_drafts(drafts)

    today = _ist_today().isoformat()
    extra = [Order(so_no=r["so_no"], item_code=r["item_code"],
                   item_name=(masters.routings[r["item_code"]].description
                              if r["item_code"] in masters.routings else ""),
                   ordered_qty=float(r["qty"]),
                   delivery_date=date.fromisoformat(r["target_date"]),
                   first_seen=today)
             for r in drafts]
    _start_optimize(_OPT_BUDGETS["deep"], "deep search (new orders)", extra_orders=extra)
    return {"started": True}


@app.post("/new-orders/prepone/accept")
def accept_prepone(request: Request):
    """Save the new orders with the dates the director asked for, and put the plan
    that was just searched into force."""
    require_admin(request)
    drafts = book_store.load_new_order_drafts()
    if not drafts:
        raise HTTPException(status_code=400, detail="There are no new orders to add.")
    if _OPTIMIZE.get("kind") != "quote" or not _OPTIMIZE.get("result"):
        raise HTTPException(status_code=409,
                            detail="There is no finished search to accept.")
    masters = _current_masters()
    today = _ist_today().isoformat()
    orders = [Order(so_no=r["so_no"], item_code=r["item_code"],
                    item_name=(masters.routings[r["item_code"]].description
                               if r["item_code"] in masters.routings else ""),
                    ordered_qty=float(r["qty"]),
                    delivery_date=date.fromisoformat(r["target_date"]),
                    first_seen=today)
              for r in drafts]
    book_store.add_orders(orders)
    book_store.save_new_order_drafts([])
    # No queue: after a full re-optimization every order is equal by definition.
    book_store.clear_new_order_queue()
    _optimize_apply()
    _PLAN_CACHE["key"] = None
    return {"added": len(orders)}
```

**(c)** The result screen's "what moved" data. Build it from the helpers that already exist — `_expected_by_order`, `_movers` — never a second comparison. Add to `api/main.py`:

```python
def _quote_movement(ranks):
    """For a finished quote search: what the book looks like before and after.

    'Before' is the plan actually in force (_incumbent_metrics's own definition,
    2026-08-07: never 'the book with no optimization at all'), 'after' is the
    searched ranks replayed over the same book. Both sides use
    optimizer.expected_completion, so this can never disagree with the Orders tab.
    """
    inc = _incumbent_metrics()
    after = _metrics_for_ranks(ranks)
    exp_old = inc.get("expected") or {}
    exp_new = after.get("expected") or {}
    moved = [{"so_no": k[0], "item_code": k[1],
              "before": exp_old[k].isoformat(), "after": nd.isoformat(), "days": d}
             for k, d, nd in _movers(exp_old, exp_new, 0)]
    return {"moved": moved,
            "late_days_before": inc.get("late_days"),
            "late_days_after": after.get("late_days")}
```

`_incumbent_metrics` and `_metrics_for_ranks` must return the per-order expected map under `"expected"`; if they do not yet, add it there (computed with `_expected_by_order`) rather than recomputing it here. Include `_quote_movement(_OPTIMIZE["result"].ranks)` in `_optimize_status()` when `kind == "quote"` and the state is `done`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3.12 -m pytest tests/test_new_orders_api.py -v`
Expected: all passed.

Run: `python3.12 -m pytest -q`
Expected: full suite green.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_new_orders_api.py
git commit -m "feat(quote): the preponed path drops the freeze and re-optimizes

The draft lines join the book in memory only, carrying the dates the director
typed. Accepting saves them and applies the searched plan, so the slip figures he
was shown are the ones the floor will run. A quote run is marked so the Settings
panel never applies it by accident.

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: The Add New Orders tab

**Files:**
- Modify: `web/index.html`, `web/app.js`, `web/style.css`
- Test: manual, scripted in Task 15

**Interfaces:**
- Consumes: every endpoint from Tasks 10, 12, 13, plus `GET /optimize/status`.
- Produces: the tab described in spec §3.

- [ ] **Step 1: Add the nav item and the view**

In `web/index.html`, after the Orders nav item:

```html
      <a class="nav-item admin-only" data-view="neworders" href="#neworders">Add New Orders</a>
```

and a `<section id="view-neworders" class="view">` holding: a lines table (`#no-lines`), an "Add another line" button, "Finish and Optimize", a result panel (`#no-result`), and a prepone panel (`#no-prepone`), all inside an `admin-only` card. Follow the markup and class conventions of the existing Settings cards exactly.

- [ ] **Step 2: Wire it in `web/app.js`**

The three pieces that are easy to get wrong, written out. Everything else follows the file's existing style.

**The role guard.** `.admin-only` is a CSS rule and cannot reach markup that JS builds at runtime, so every entry point checks the role itself (2026-08-09 lesson):

```js
function newOrdersAllowed() {
  return currentRole === 'admin';
}
```

Call it first in every function below and in `showView()`'s `#neworders` branch, redirecting a non-admin to `#orders`.

**Rendering the quote.** The refusal path is not decoration — it is the promise the whole feature makes:

```js
function renderQuote(body) {
  const el = document.getElementById('no-result');
  if (!body.verified) {
    el.innerHTML = '<p class="warn">This quote could not be confirmed: '
      + body.moved.length + ' existing order(s) moved. No date is shown. '
      + 'Press Finish and Optimize again.</p>';
    return;
  }
  const rows = body.lines.map(function (l) {
    if (!l.completion) {
      return '<tr><td>' + l.so_no + '</td><td>' + l.item_code
        + '</td><td class="warn">' + l.error + '</td></tr>';
    }
    return '<tr><td>' + l.so_no + '</td><td>' + l.item_code + ' ' + l.item_name
      + '</td><td>' + l.qty + '</td><td><strong>' + fmtDate(l.completion)
      + '</strong></td></tr>';
  }).join('');
  el.innerHTML = '<table class="tbl">' + rows + '</table>'
    + '<p class="ok">All ' + body.existing_count
    + ' existing orders keep the completion date they have today.</p>';
  quoteStamp = body.stamp;
}
```

**The stale quote.** A 409 on add is a normal outcome, not an error to swallow:

```js
async function addNewOrders() {
  if (!newOrdersAllowed()) return;
  const res = await fetch('/new-orders/add', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ stamp: quoteStamp })
  });
  if (res.status === 409) {
    const body = await res.json();
    document.getElementById('no-result').innerHTML =
      '<p class="warn">' + body.detail + '</p>';
    return;
  }
  if (!res.ok) { showError(await res.text()); return; }
  await loadNewOrders();
  await runPlan(false);
}
```

The remaining functions, named in the file's existing style:

- `loadNewOrders()` — `GET /new-orders/drafts`, render the rows and fill the item `<select>` (grouped, filterable by typing).
- `saveNewOrderDrafts()` — `PUT /new-orders/drafts`; on 400, show the message against the offending row and do not clear it.
- `quoteNewOrders()` — `POST /new-orders/quote`; render one row per line, the "all N existing orders keep their current completion date" confirmation, and the three buttons. If `verified` is false, show the refusal instead of any date.
- `addNewOrders()` — `POST /new-orders/add` with the stamp; on 409 show the message and leave the Finish and Optimize button ready.
- `showPreponePanel()` — a date box per line, pre-filled with the quoted date.
- `startPrepone()` — the confirmation warning, then `POST /new-orders/prepone`, then poll `GET /optimize/status` with the existing progress-bar code.
- `renderPreponeResult()` — targets vs achieved, the movers list, the totals, then Accept / Cancel.
- `acceptPrepone()` — `POST /new-orders/prepone/accept`, then `runPlan(false)`.

Guard every one of these on the admin role in JS as well as CSS — `.admin-only` cannot reach markup that JS builds at runtime (2026-08-09 lesson).

Route `#neworders` in `showView()` and redirect a non-admin away from it.

- [ ] **Step 3: Style it**

In `web/style.css`, reuse the existing card, table and button classes. Add only what genuinely does not exist. Copy must be plain English, no em dashes.

- [ ] **Step 4: Check it renders**

Run the app (`python3.12 -m uvicorn api.main:app --reload`), log in as admin, open the tab, and confirm: the item dropdown is populated, a duplicate is refused with the message, a quote returns dates, and the user role cannot see the tab.

- [ ] **Step 5: Commit**

```bash
git add web/index.html web/app.js web/style.css
git commit -m "feat(quote): the Add New Orders tab

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 15: Prove it on the real books

**Files:**
- Create: scratch harness in the session scratchpad (not committed)
- Test: the whole suite, plus the harness

This is the task the feature lives or dies on. **Do not skip a single check, and report the numbers, not an impression.**

- [ ] **Step 1: The byte-identical guard on all three real books**

Write a scratch script that, for `Test5.xlsx`, `Test8.xlsx`, `Test9.xlsx` at work-in-progress levels 0 / 30 / full, plans the book on `main` and on the feature branch and hashes the entry list `(batch_id, process_seq, machine, operator, start, end, qty)`.
Expected: **identical hashes on all nine runs.** Anything else stops the work.

- [ ] **Step 2: The freeze invariant**

On the same nine runs, quote 1, 3, 10 and 20 new orders (real item codes, quantities 10–500) and assert for every existing order: same completion date, same machine, same operator. Also assert `routing_order_violations`, `qualification_violations` and `batch_quantity_violations` are all empty.
Report: the count of existing orders checked per run.

- [ ] **Step 3: The gap rule**

On the same runs, assert no new-order operation overlaps an existing block and no new-order operation spans one.
Report: how many new operations landed in a gap versus after the machine's last job — this is the number that says whether the feature does anything useful.

- [ ] **Step 4: First come, first served**

Add 5 orders one at a time through the API, re-planning between each, and assert nothing already added ever moves.

- [ ] **Step 5: Mutation testing**

Revert each of these individually and confirm at least one test fails. Record the result for each:
1. The stop line in `_lay_on_machine` (drop the `deadline` clamp).
2. The free-run walk in `_place_operation` (call `_lay_on_machine` directly).
3. The operator seeding in `decode` (`booked=None`).
4. The assignment seeding in `decode` (`assigned=None`).
5. Skipping `OFF_LANES` in `occupancy_from_entries`.
6. The queue split in `_plan` (plan everything in one stage).
7. The queue in `_plan_fingerprint`.
8. The stamp check in `/new-orders/add`.

**State plainly which of these fail no test.** This fixture family passes vacuously by default (CLAUDE.md) — an uncovered part is belt-and-braces and must be reported as such, never dressed up as covered.

- [ ] **Step 6: Live, through the browser**

A throwaway local instance with the real workbook, driven as admin: quote → accept → Orders tab, Gantt and shift-wise export all show the quoted date → press Done entering → the queue clears and the orders are ranked normally. Also check the user role cannot reach the tab or any of its endpoints.

- [ ] **Step 7: The cross-surface audit**

Re-run the seven-surface audit (engine plan, Gantt, Schedule tab, shift-wise export, delay report, analytics, optimizer) with orders in the queue. Expected: zero date disagreements, zero routing violations.

- [ ] **Step 8: Commit the findings**

```bash
git commit --allow-empty -m "test(quote): verification on the real books

$(report the actual numbers: hashes, orders checked, gap placements, mutation results)

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Task 16: Record it

**Files:**
- Modify: `CLAUDE.md`, `docs/superpowers/specs/2026-09-08-add-new-orders-quote-design.md`

- [ ] **Step 1: Write the banner bullet**

Add a bullet at the top of CLAUDE.md's CURRENT STATE banner, in the established voice: what the feature is, the one load-bearing decision (existing orders are not in stage 2 at all), the second decision (the arrival queue, and what ends it), the measured results from Task 15 including the mutation findings stated honestly, what was deliberately not built, and the rule it establishes:

> **Rule: a date promised to a customer must be computed against the plan in force, and a new order may never move an order that is already promised. Anything that plans a subset of the book must plan it against the occupancy of the rest.**

- [ ] **Step 2: Update the spec's status line**

Change `Status:` to record that it is implemented, with the commit range.

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md docs/superpowers/specs/2026-09-08-add-new-orders-quote-design.md
git commit -m "docs: record the Add New Orders quote feature

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

## Self-review notes for the executor

- **If the byte-identical check in Task 5 or Task 15 fails, stop.** Do not regenerate the golden trace, do not bump `SCHEDULER_FINGERPRINT`, do not "explain" the difference. Something in the new code is running on the old path.
- **The tests in Task 5 must be checked against a mutation before you trust them.** Break `_lay_in_free_run` on purpose and confirm they go red. This fixture family passes vacuously by default; two earlier versions of a comparable fixture in this repo passed under every mutation.
- **Never derive a completion date locally.** `optimizer.expected_completion` is the only definition.
- **Stage 2 never gets a frozen set.** If you find yourself passing one, the design has been misunderstood.
