# Machine Maintenance Downtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an admin mark a CNC/VMC machine out of service for a date range, and have the plan, the reports and the optimizer all route around it.

**Architecture:** Machine downtime rides the **existing `reserved` dict** that already carries operator absences — the retired classic/flow engines already look machine ids up in it, so they need no change at all. The live engine (`ppc_engine`) learns about it through **one function**: `iter_windows`, the single window source shared by the main decode loop and both frozen-op paths. A separate plan-time rule releases a frozen (part-done) operation whose pinned machine is down during its window, so the scheduler and optimizer decide reroute-vs-wait on the normal objective.

**Tech Stack:** Python 3.12, FastAPI, pytest, plain HTML/JS frontend. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-08-31-machine-maintenance-downtime-design.md` — read it before starting. It carries the reasoning; this plan carries the steps.

## Global Constraints

- **Run tests with `python3.12 -m pytest`.** The system `python3` is 3.14, where the installed `openpyxl` crashes on import (`numpy.short` was removed). This is an environment fact, not a code bug.
- **Baseline before any change: `901 passed, 2 skipped`.** Every task must end with the full suite at least that green. (CLAUDE.md says "508 passing" — stale; Task 12 corrects it.)
- **`web/style.css` has an uncommitted change that is NOT ours** (the 2026-08-18 full-width-tabs edit). Never `git add web/style.css` unless a task explicitly says to, and never `git add -A` / `git commit -a`.
- **Dates are ISO (`YYYY-MM-DD`) in the store and API, `DD-MM-YYYY` in the UI.** The app-wide display format.
- **Downtime is whole days.** A break on date `D` blocks both shifts anchored on `D`: first (08:00–19:00) and second (19:00 `D` → 05:00 `D+1`).
- **Never `git push`.** Committing locally is fine; pushing to `main` is a production deploy and is the owner's call.
- **Do not add a new nav tab, a new xlsx sheet, or a new contest dimension.** Each has an exact-value test that would break, and none is needed.
- Machine ids are canonical/upper (`CNC1`), never display names (`CNC 1`).

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `engine/book_store.py` | Durable list of breaks (`anvitech:machine_downtime`) | 1 |
| `engine/optimize_service.py` | `downtime_reservations`; signature/payload/contest plumbing | 1, 5 |
| `ppc_engine/domain/calendar.py` | `machine_downtime` field + `is_machine_available` | 2 |
| `ppc_engine/worktime.py` | `iter_windows` consults the machine, not just the shop | 2 |
| `engine/new_engine.py` | `_with_unavailability` split; `_ppc_frozen` release; fingerprint v6 | 3, 4 |
| `engine/freeze.py` | copy `prev_end` onto each pin | 4 |
| `api/main.py` | 3 endpoints, plan wiring, orphan report row, report call sites | 6, 7, 8, 9 |
| `engine/analytics.py` | Available-hours subtraction | 8 |
| `engine/delay_report.py` | `MAINTENANCE (machine down)` state + column | 9 |
| `web/index.html`, `web/app.js` | Settings panel | 10 |

New test files: `tests/test_machine_downtime_store.py`, `…_engine.py`, `…_freeze.py`, `…_api.py`, `…_reports.py`.

---

### Task 1: Store the breaks, and turn them into reservations

**Files:**
- Modify: `engine/book_store.py` (add near the absence block, ~line 285)
- Modify: `engine/optimize_service.py` (add after `absence_reservations`, ~line 100)
- Test: `tests/test_machine_downtime_store.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `book_store.MACHINE_DOWNTIME_KEY: str`
  - `book_store.load_machine_downtime() -> list[dict]`
  - `book_store.save_machine_downtime(row: dict) -> dict` — assigns `id`, returns the stored row
  - `book_store.delete_machine_downtime(row_id: str) -> bool`
  - `optimize_service.downtime_reservations(rows) -> dict[str, list[tuple[datetime, datetime]]]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_machine_downtime_store.py`:

```python
"""Machine maintenance breaks: durable storage + the reservation intervals they
become. Exact mirror of the operator-absence pair (book_store.save_absence /
optimize_service.absence_reservations)."""
from datetime import datetime

from engine import book_store, optimize_service as svc


def test_save_assigns_an_id_and_round_trips():
    saved = book_store.save_machine_downtime(
        {"machine": "CNC3", "from_date": "2026-09-05", "to_date": "2026-09-07",
         "reason": "Spindle service"})
    assert saved["id"]
    assert saved["machine"] == "CNC3"
    rows = book_store.load_machine_downtime()
    assert rows == [saved]


def test_empty_store_is_an_empty_list():
    assert book_store.load_machine_downtime() == []


def test_delete_removes_one_row_and_reports_unknown_ids():
    a = book_store.save_machine_downtime(
        {"machine": "CNC3", "from_date": "2026-09-05", "to_date": "2026-09-05"})
    b = book_store.save_machine_downtime(
        {"machine": "VMC1", "from_date": "2026-09-08", "to_date": "2026-09-09"})
    assert book_store.delete_machine_downtime(a["id"]) is True
    assert [r["machine"] for r in book_store.load_machine_downtime()] == ["VMC1"]
    assert book_store.delete_machine_downtime("nope") is False
    assert len(book_store.load_machine_downtime()) == 1
    assert b["reason"] == ""          # optional field defaults, never missing


def test_reservations_cover_00_00_to_00_00_of_the_day_after():
    res = svc.downtime_reservations(
        [{"machine": "CNC3", "from_date": "2026-09-05", "to_date": "2026-09-06"}])
    assert set(res) == {"CNC3"}
    assert res["CNC3"] == [(datetime(2026, 9, 5, 0, 0), datetime(2026, 9, 7, 0, 0))]


def test_reservations_swap_a_reversed_range():
    res = svc.downtime_reservations(
        [{"machine": "CNC3", "from_date": "2026-09-06", "to_date": "2026-09-05"}])
    assert res["CNC3"] == [(datetime(2026, 9, 5, 0, 0), datetime(2026, 9, 7, 0, 0))]


def test_reservations_skip_malformed_and_blank_rows():
    res = svc.downtime_reservations([
        {"machine": "CNC3", "from_date": "not-a-date", "to_date": "2026-09-06"},
        {"machine": "", "from_date": "2026-09-05", "to_date": "2026-09-06"},
        {"from_date": "2026-09-05", "to_date": "2026-09-06"},
        {"machine": "VMC1", "from_date": "2026-09-05", "to_date": "2026-09-05"},
    ])
    assert set(res) == {"VMC1"}


def test_two_breaks_on_one_machine_both_reserve():
    res = svc.downtime_reservations([
        {"machine": "CNC3", "from_date": "2026-09-05", "to_date": "2026-09-05"},
        {"machine": "CNC3", "from_date": "2026-09-20", "to_date": "2026-09-20"},
    ])
    assert len(res["CNC3"]) == 2


def test_none_and_empty_are_an_empty_dict():
    assert svc.downtime_reservations(None) == {}
    assert svc.downtime_reservations([]) == {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3.12 -m pytest tests/test_machine_downtime_store.py -v`
Expected: FAIL — `AttributeError: module 'engine.book_store' has no attribute 'save_machine_downtime'`

- [ ] **Step 3: Add the store accessors**

In `engine/book_store.py`, add the key beside the others (after the `ABSENCES_KEY` line, ~line 27):

```python
MACHINE_DOWNTIME_KEY = "anvitech:machine_downtime"   # kv: json list of machine maintenance breaks
```

Then add this block immediately after `delete_absence` (~line 305):

```python
# --- machine maintenance breaks (a machine out of service for a date range) --- #
def load_machine_downtime() -> list:
    raw = get_store().kv_get(MACHINE_DOWNTIME_KEY)
    return json.loads(raw) if raw else []


def save_machine_downtime(d: dict) -> dict:
    """Record one break. Mirrors ``save_absence``: the id is assigned here so the
    caller never invents one, and only the known fields are persisted."""
    d = {"id": uuid.uuid4().hex, "machine": d["machine"],
         "from_date": d["from_date"], "to_date": d["to_date"],
         "reason": d.get("reason", "") or ""}
    rows = load_machine_downtime() + [d]
    get_store().kv_set(MACHINE_DOWNTIME_KEY, json.dumps(rows))
    return d


def delete_machine_downtime(downtime_id: str) -> bool:
    rows = load_machine_downtime()
    keep = [r for r in rows if r.get("id") != downtime_id]
    if len(keep) == len(rows):
        return False
    get_store().kv_set(MACHINE_DOWNTIME_KEY, json.dumps(keep))
    return True
```

- [ ] **Step 4: Add `downtime_reservations`**

In `engine/optimize_service.py`, immediately after `absence_reservations` (~line 100):

```python
def downtime_reservations(rows):
    """Machine maintenance rows -> MACHINE reservations, keyed by machine id: the
    machine is 'busy' from 00:00 of from_date to 00:00 of the day AFTER to_date
    (inclusive), exactly like ``absence_reservations`` does for a person.

    The result is merged into the SAME ``reserved`` dict operator absences use.
    That is deliberate: ``rule6_allocate._lay_segments`` and
    ``flow_scheduler._mac_next_window`` already look machine ids up in it, so the
    retired engines honour a maintenance break with no change at all."""
    from datetime import datetime, date, timedelta
    res = {}
    for d in rows or []:
        try:
            f = date.fromisoformat(d["from_date"])
            t = date.fromisoformat(d["to_date"])
        except (KeyError, ValueError, TypeError):
            continue                                   # malformed row — skip
        if t < f:
            f, t = t, f
        interval = (datetime.combine(f, datetime.min.time()),
                    datetime.combine(t + timedelta(days=1), datetime.min.time()))
        res.setdefault(d.get("machine", ""), []).append(interval)
    res.pop("", None)
    return res
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3.12 -m pytest tests/test_machine_downtime_store.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Run the full suite — nothing may move**

Run: `python3.12 -m pytest -q`
Expected: `909 passed, 2 skipped` (901 + the 8 new)

- [ ] **Step 7: Commit**

```bash
git add engine/book_store.py engine/optimize_service.py tests/test_machine_downtime_store.py
git commit -m "feat(maintenance): store machine downtime and turn it into reservations"
```

---

### Task 2: Teach the live engine that a machine can be out of service

**Files:**
- Modify: `ppc_engine/domain/calendar.py`
- Modify: `ppc_engine/worktime.py:63-90` (`iter_windows`)
- Test: `tests/test_machine_downtime_engine.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `ShopCalendar.machine_downtime: dict[str, frozenset[date]]` and
  `ShopCalendar.is_machine_available(machine_id: str, day: date) -> bool`.

**Why this file:** `iter_windows` is the ONE window source for all three placement paths in the live engine (`_lay_on_machine`, `_lay_frozen`, `_preplace_frozen`). Narrowing it covers all three; adding a check to each would recreate the 2026-08-09 drift bug.

**Note:** this task adds the **first direct unit test of `ppc_engine.worktime.iter_windows` in the repo**. `tests/test_worktime.py` looks like it covers day-skipping but tests the *classic* `engine.worktime.WorkClock`, a different module.

- [ ] **Step 1: Write the failing test**

Create `tests/test_machine_downtime_engine.py`:

```python
"""A machine marked out of service runs nothing on those days.

Enforced in ONE place — ``ppc_engine.worktime.iter_windows`` — which is the single
window source for the main decode loop AND both frozen-op paths, so all three are
covered by construction.
"""
from datetime import date, datetime

import pytest

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.resources import Machine, MachineKind, Shift
from ppc_engine.worktime import iter_windows

CNC = Machine("CNC3", "CNC lathe", MachineKind.MACHINING, 19.5)
MANUAL = Machine("MW1", "Manual Washing", MachineKind.MANUAL, 9.5)
CFG = PlanConfig(plan_start=datetime(2026, 9, 3, 8, 0), week_anchor=None)

# 2026-09-03 is a Thursday (the weekly off), so the plan really starts Friday the 4th.
MON = date(2026, 9, 7)


def _days(machine, calendar, start, n):
    """The shift_dates of the first ``n`` windows from ``start``."""
    out = []
    for win in iter_windows(machine, start, calendar, CFG):
        out.append((win.shift_date, win.shift))
        if len(out) >= n:
            break
    return out


def test_a_clean_calendar_is_unchanged():
    """The whole-plan guard: no downtime on file == today's behaviour."""
    cal = ShopCalendar()
    assert cal.machine_downtime == {}
    got = _days(CNC, cal, datetime(2026, 9, 4, 8, 0), 4)
    assert got == [(date(2026, 9, 4), Shift.FIRST), (date(2026, 9, 4), Shift.SECOND),
                   (date(2026, 9, 5), Shift.FIRST), (date(2026, 9, 5), Shift.SECOND)]


def test_a_down_day_yields_no_window_at_all():
    cal = ShopCalendar(machine_downtime={"CNC3": frozenset({date(2026, 9, 4)})})
    got = _days(CNC, cal, datetime(2026, 9, 4, 8, 0), 2)
    assert all(d != date(2026, 9, 4) for d, _ in got)
    assert got[0] == (date(2026, 9, 5), Shift.FIRST)


def test_both_shifts_of_a_down_day_are_blocked():
    """A break blocks the first shift AND the night shift anchored on that date —
    the night shift runs 19:00 -> 05:00 the next morning, so this is the one that
    a naive calendar-day check would get wrong."""
    cal = ShopCalendar(machine_downtime={"CNC3": frozenset({date(2026, 9, 4)})})
    got = _days(CNC, cal, datetime(2026, 9, 4, 0, 1), 4)
    assert (date(2026, 9, 4), Shift.FIRST) not in got
    assert (date(2026, 9, 4), Shift.SECOND) not in got


def test_other_machines_are_untouched_on_the_same_day():
    cal = ShopCalendar(machine_downtime={"CNC3": frozenset({date(2026, 9, 4)})})
    other = Machine("CNC4", "CNC lathe", MachineKind.MACHINING, 19.5)
    got = _days(other, cal, datetime(2026, 9, 4, 8, 0), 1)
    assert got[0] == (date(2026, 9, 4), Shift.FIRST)


def test_work_resumes_the_day_the_machine_comes_back():
    """Down 05-09 and 06-09, back on the 7th (a Monday)."""
    cal = ShopCalendar(machine_downtime={
        "CNC3": frozenset({date(2026, 9, 5), date(2026, 9, 6)})})
    got = _days(CNC, cal, datetime(2026, 9, 5, 8, 0), 1)
    assert got[0] == (MON, Shift.FIRST)


def test_a_single_shift_station_can_also_be_marked_down():
    """The engine honours downtime on ANY machine; only the UI picker is filtered
    to CNC/VMC. A manual station has no night shift, so one window disappears."""
    cal = ShopCalendar(machine_downtime={"MW1": frozenset({date(2026, 9, 4)})})
    got = _days(MANUAL, cal, datetime(2026, 9, 4, 8, 0), 1)
    assert got[0] == (date(2026, 9, 5), Shift.FIRST)


def test_is_machine_available_still_respects_the_shop_calendar():
    cal = ShopCalendar(machine_downtime={"CNC3": frozenset({date(2026, 9, 4)})})
    assert cal.is_machine_available("CNC3", date(2026, 9, 5)) is True
    assert cal.is_machine_available("CNC3", date(2026, 9, 4)) is False
    # 2026-09-03 is a Thursday: closed for everyone, downtime or not.
    assert cal.is_machine_available("CNC3", date(2026, 9, 3)) is False
    assert cal.is_machine_available("CNC4", date(2026, 9, 3)) is False


def test_a_holiday_still_closes_every_machine():
    cal = ShopCalendar(holidays=frozenset({date(2026, 9, 4)}))
    assert cal.is_machine_available("CNC3", date(2026, 9, 4)) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3.12 -m pytest tests/test_machine_downtime_engine.py -v`
Expected: FAIL — `TypeError: ShopCalendar.__init__() got an unexpected keyword argument 'machine_downtime'`

- [ ] **Step 3: Add the calendar field and method**

In `ppc_engine/domain/calendar.py`, add the field to `ShopCalendar` **after** `leaves` (appending keeps every existing construction valid — verified: the class is built in only two places repo-wide, both keyword or bare):

```python
    machine_downtime: dict[str, frozenset[date]] = field(default_factory=dict)
```

Extend the class docstring's Attributes block with:

```
        machine_downtime:   Map of machine id -> set of dates that machine is out of
                            service for maintenance (only that machine stops; the shop
                            keeps running). Empty by default, so a shop with no
                            maintenance on file behaves exactly as before.
```

Then add, after `is_operator_available`:

```python
    def is_machine_available(self, machine_id: str, day: date) -> bool:
        """True if ``machine_id`` can run on ``day``.

        A machine runs when the shop is open that day AND it is not out of service
        for maintenance. Mirror of ``is_operator_available`` — a person's leave and
        a machine's maintenance are the same idea applied to the two resources an
        operation needs.
        """
        if not self.is_working_day(day):
            return False
        return day not in self.machine_downtime.get(machine_id, frozenset())
```

- [ ] **Step 4: Narrow `iter_windows`**

In `ppc_engine/worktime.py::iter_windows`, change the single guard:

```python
    day = start_from.date()
    for _ in range(_MAX_DAYS_LOOKAHEAD):
        if calendar.is_machine_available(machine.id, day):
```

(was `if calendar.is_working_day(day):`)

`is_machine_available` already includes `is_working_day`, so this is a strict
narrowing: with no downtime on file the output is byte-identical.

Update the function's docstring "Skips:" list to add:

```
      - days this machine is out of service for maintenance (only this machine;
        the rest of the shop runs), and
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `python3.12 -m pytest tests/test_machine_downtime_engine.py -v`
Expected: PASS (8 tests)

- [ ] **Step 6: Run the full suite — the byte-identity guarantee**

Run: `python3.12 -m pytest -q`
Expected: `917 passed, 2 skipped`. **Any pre-existing failure here means the narrowing was not byte-identical — stop and investigate rather than adjusting a test.**

- [ ] **Step 7: Commit**

```bash
git add ppc_engine/domain/calendar.py ppc_engine/worktime.py tests/test_machine_downtime_engine.py
git commit -m "feat(maintenance): a machine can be out of service for whole days"
```

---

### Task 3: Feed the breaks into the live engine's masters

**Files:**
- Modify: `engine/new_engine.py:143-168` (`_with_absences`), `:628`, `:654`, `:702` (call sites), `:612` (fingerprint)
- Test: `tests/test_machine_downtime_engine.py` (append)

**Interfaces:**
- Consumes: `ShopCalendar.machine_downtime` (Task 2), `downtime_reservations` (Task 1).
- Produces: `new_engine._with_unavailability(masters, reserved)` — replaces `_with_absences`, same call shape. `new_engine.SCHEDULER_FINGERPRINT == "new-engine-v6-machine-downtime"`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_machine_downtime_engine.py`:

```python
# --------------------------------------------------------------------------- #
# The reserved-dict split: one dict carries both operator absences and machine
# downtime, and the new engine sorts them apart by asking the machine master.
# --------------------------------------------------------------------------- #
def test_with_unavailability_splits_machines_from_operators():
    from engine import new_engine
    from ppc_engine.domain.masters import Masters
    from ppc_engine.domain.resources import Operator, Role

    masters = Masters(
        machines={"CNC3": CNC},
        operators=(Operator("Anil", Role.OPERATOR, frozenset({"CNC3"}), Shift.FIRST),),
        calendar=ShopCalendar())
    reserved = {
        "CNC3": [(datetime(2026, 9, 5), datetime(2026, 9, 7))],   # a machine id
        "Anil": [(datetime(2026, 9, 8), datetime(2026, 9, 9))],   # a person
    }
    out = new_engine._with_unavailability(masters, reserved)

    assert out.calendar.machine_downtime == {
        "CNC3": frozenset({date(2026, 9, 5), date(2026, 9, 6)})}
    assert out.calendar.leaves == {"Anil": frozenset({date(2026, 9, 8)})}
    # The cached masters object is never mutated.
    assert masters.calendar.machine_downtime == {}
    assert masters.calendar.leaves == {}


def test_with_unavailability_is_a_no_op_for_an_empty_reserved():
    from engine import new_engine
    from ppc_engine.domain.masters import Masters
    masters = Masters(machines={"CNC3": CNC}, calendar=ShopCalendar())
    assert new_engine._with_unavailability(masters, None) is masters
    assert new_engine._with_unavailability(masters, {}) is masters


def test_an_unknown_key_is_treated_as_an_operator():
    """A key that is neither a known machine nor a known operator falls to the
    operator branch — today's behaviour for a stale absence, unchanged."""
    from engine import new_engine
    from ppc_engine.domain.masters import Masters
    masters = Masters(machines={"CNC3": CNC}, calendar=ShopCalendar())
    out = new_engine._with_unavailability(
        masters, {"Ghost": [(datetime(2026, 9, 8), datetime(2026, 9, 9))]})
    assert out.calendar.machine_downtime == {}
    assert "Ghost" in out.calendar.leaves


def test_the_scheduler_fingerprint_records_the_new_semantics():
    """Real work moves when a break is on file, so saved optimizer ranks were
    scored under different semantics and must be flagged stale."""
    from engine import new_engine
    assert new_engine.SCHEDULER_FINGERPRINT == "new-engine-v6-machine-downtime"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3.12 -m pytest tests/test_machine_downtime_engine.py -k unavailability -v`
Expected: FAIL — `AttributeError: module 'engine.new_engine' has no attribute '_with_unavailability'`

- [ ] **Step 3: Replace `_with_absences` with `_with_unavailability`**

In `engine/new_engine.py`, replace the whole `_with_absences` function (lines ~143-168) with:

```python
def _with_unavailability(masters, reserved):
    """Return a per-plan copy of the new-engine Masters with everything that is
    UNAVAILABLE folded into the calendar: operator absences become per-operator
    leave, machine maintenance breaks become per-machine downtime.

    ``reserved`` is the one ``{key -> [(start_dt, end_dt), ...]}`` dict the whole
    app already threads through — ``optimize_service.absence_reservations`` keys it
    by operator NAME and ``downtime_reservations`` keys it by MACHINE ID, merged by
    ``merge_reservations``. A key is a machine when the machine master knows it;
    anything else is a person (so a stale absence for a departed operator behaves
    exactly as it does today). Cached masters are never mutated.
    """
    from dataclasses import replace as _replace
    if not reserved:
        return masters
    op_days: dict[str, set] = {}
    mac_days: dict[str, set] = {}
    for key, intervals in reserved.items():
        days: set = set()
        for start, end in intervals:
            d = start.date()
            while d < end.date():
                days.add(d)
                d += timedelta(days=1)
        if not days:
            continue
        bucket = mac_days if key in masters.machines else op_days
        bucket.setdefault(key, set()).update(days)
    if not op_days and not mac_days:
        return masters
    cal = masters.calendar
    leaves = dict(getattr(cal, "leaves", {}) or {})
    for op, days in op_days.items():
        leaves[op] = frozenset(leaves.get(op, frozenset()) | days)
    downtime = dict(getattr(cal, "machine_downtime", {}) or {})
    for mid, days in mac_days.items():
        downtime[mid] = frozenset(downtime.get(mid, frozenset()) | days)
    return _replace(masters, calendar=_replace(
        cal, leaves=leaves, machine_downtime=downtime))
```

- [ ] **Step 4: Update the three call sites and the fingerprint**

Rename at `engine/new_engine.py:628` (in `run`), `:654` (in `optimize_sequence`) and
`:702` (in `tune`) — each currently reads `_with_absences(_apply_app_operators(…), reserved)`:

```bash
python3.12 - <<'PY'
import pathlib
p = pathlib.Path("engine/new_engine.py")
s = p.read_text()
assert s.count("_with_absences(") == 3, s.count("_with_absences(")
p.write_text(s.replace("_with_absences(", "_with_unavailability("))
PY
grep -n "_with_absences\|_with_unavailability" engine/new_engine.py
```
Expected: three `_with_unavailability(` hits, zero `_with_absences`.

Then bump the fingerprint (~line 612):

```python
# v6 (2026-08-31) = a machine can be marked out of service for maintenance, so the
# engine no longer offers its windows on those days (`ppc_engine.worktime.iter_windows`)
# and a frozen op pinned to a machine that is down during its window is released back
# to normal scheduling (`_ppc_frozen`). Real work moves.
SCHEDULER_FINGERPRINT = "new-engine-v6-machine-downtime"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3.12 -m pytest tests/test_machine_downtime_engine.py -v`
Expected: PASS (12 tests)

- [ ] **Step 6: Run the full suite**

Run: `python3.12 -m pytest -q`
Expected: `921 passed, 2 skipped`. The three fingerprint tests (`test_new_engine.py:410`, `test_scarce_operator_pick.py:135`, `test_flow_scheduler.py:323`) read the constant live and append a suffix, so a bumped value does not break them.

- [ ] **Step 7: Commit**

```bash
git add engine/new_engine.py tests/test_machine_downtime_engine.py
git commit -m "feat(maintenance): fold machine breaks into the live engine's calendar"
```

---

### Task 4: Release a frozen operation whose machine is down

**Files:**
- Modify: `engine/freeze.py:76` (add `prev_end` to the emitted row)
- Modify: `engine/new_engine.py` (`_ppc_frozen` signature + release rule; its three callers)
- Test: `tests/test_machine_downtime_freeze.py` (create)

**Interfaces:**
- Consumes: `ShopCalendar.machine_downtime` (Task 2), `_with_unavailability` (Task 3).
- Produces: `new_engine._ppc_frozen(rows, orders, batch_by_key, masters, plan_start_date)` — one extra **positional** parameter, a `datetime.date`. Frozen rows now carry `prev_end` (ISO string).

**Why here and not in `freeze.py`:** `compute_frozen_set` runs ONLY on "Done entering — update plan" (`api._compute_and_store_frozen`, called once, inside `_start_optimize:1491`). Every ordinary re-plan reads the stored list as-is. Deciding there would mean the Settings panel's own re-plan shows "waits" while the next Done shows "moved" — two answers to one question. `_ppc_frozen` runs on every plan, so it cannot go stale.

- [ ] **Step 1: Write the failing test**

Create `tests/test_machine_downtime_freeze.py`:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3.12 -m pytest tests/test_machine_downtime_freeze.py -v`
Expected: FAIL — `KeyError: 'prev_end'` on the first test, `TypeError: _ppc_frozen() takes 4 positional arguments but 5 were given` on the rest.

- [ ] **Step 3: Carry `prev_end` on each frozen row**

In `engine/freeze.py`, in `compute_frozen_set`'s emitted dict (~line 76), add one line after `prev_start`:

```python
                "remaining_qty": remaining, "prev_start": row["start"],
                # When this step was due to FINISH in the applied plan. Read by
                # new_engine._ppc_frozen to decide whether a maintenance break on the
                # pinned machine overlaps this step's window (2026-08-31 spec).
                "prev_end": row["end"],
```

The function signature is deliberately unchanged, so all four positional callers
(`tests/test_freeze_logic.py:49,72`, `tests/test_frozen_batch_qty.py:72`,
`api/main.py:1411`) are untouched.

- [ ] **Step 4: Add the release rule to `_ppc_frozen`**

In `engine/new_engine.py`, change the signature and add the check. The new parameter
is the fifth positional:

```python
def _ppc_frozen(rows, orders, batch_by_key, masters, plan_start_date):
```

Add to its docstring, after the existing "A frozen row pins WHERE and WHEN…" paragraph:

```
    **A machine that is OUT OF SERVICE releases its pins** (2026-08-31). If the
    pinned machine has a maintenance day anywhere in [plan_start_date .. the date
    this step was due to finish in the applied plan], the row is dropped and the
    step goes back to normal scheduling -- where the routing's own machine options
    decide where it MAY go and the objective decides whether moving beats waiting.
    Evaluated HERE, at plan time, rather than in freeze.compute_frozen_set: that
    runs only on "Done entering", so a build-time rule would leave every ordinary
    re-plan showing a stale pin.
```

Add this helper immediately above `_ppc_frozen`:

```python
def _machine_down_in_window(masters, mid, start_date, end_date) -> bool:
    """True if machine ``mid`` has a maintenance day in [start_date, end_date]."""
    days = getattr(masters.calendar, "machine_downtime", None) or {}
    days = days.get(mid)
    if not days:
        return False
    d = start_date
    while d <= end_date:
        if d in days:
            return True
        d += timedelta(days=1)
    return False
```

Then, inside `_ppc_frozen`'s row loop, immediately after the existing machine check
(`if not mid or mid not in masters.machines: continue`), insert:

```python
        # A machine out of service for maintenance cannot hold a pin. Judge the
        # break against the window this step was due to occupy, so a break months
        # away never unfreezes today's work.
        try:
            _end = datetime.fromisoformat(r.get("prev_end") or r["prev_start"]).date()
        except (KeyError, ValueError, TypeError):
            _end = plan_start_date
        if _machine_down_in_window(masters, mid, plan_start_date,
                                   max(_end, plan_start_date)):
            continue
```

- [ ] **Step 5: Update the three `_ppc_frozen` call sites**

`engine/new_engine.py::run` — the plan config is built inline at the `decode` call; hoist it so the date is available:

```python
    sched_cfg = _plan_config(config)
    ppc_frozen = (_ppc_frozen(frozen, orders, batch_by_key, new_masters,
                              sched_cfg.plan_start.date()) if frozen else None)
    sched = decode(orders, sequence, new_masters, sched_cfg, frozen=ppc_frozen)
```

`engine/new_engine.py::optimize_sequence` — `cfg` already exists:

```python
    ppc_frozen = (_ppc_frozen(frozen, orders, batch_by_key, nm,
                              cfg.plan_start.date()) if frozen else None)
```

`engine/new_engine.py::tune` — `base` already exists:

```python
    ppc_frozen = (_ppc_frozen(frozen, orders, batch_by_key, new_masters,
                              base.plan_start.date()) if frozen else None)
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `python3.12 -m pytest tests/test_machine_downtime_freeze.py -v`
Expected: PASS (7 tests)

- [ ] **Step 7: Run the freeze and routing suites, then everything**

Run: `python3.12 -m pytest tests/test_freeze_logic.py tests/test_freeze_engine.py tests/test_frozen_batch_qty.py tests/test_routing_precedence.py -q`
Expected: all pass — the freeze signature did not change.

Run: `python3.12 -m pytest -q`
Expected: `928 passed, 2 skipped`

- [ ] **Step 8: Commit**

```bash
git add engine/freeze.py engine/new_engine.py tests/test_machine_downtime_freeze.py
git commit -m "feat(maintenance): release a frozen op when its machine is out of service"
```

---

### Task 5: Carry the breaks through the signature, the payload and the contest

**Files:**
- Modify: `engine/optimize_service.py` — `book_signature`, `build_payload`, `parse_payload`, `ContestSetup`, `prepare_contest`, `run_candidate`
- Modify: `api/main.py:1607`, `:2021` (the two `absence_reserved` readers)
- Modify (rebase): `tests/test_absences_engine.py:49,66-68,98,110-113`, `tests/test_optimize_service.py:45`, `tests/test_freeze_contest.py:26`, `tests/test_operator_wiring.py:209`
- Test: `tests/test_machine_downtime_api.py` (create — the plumbing half)

**Interfaces:**
- Consumes: `downtime_reservations` (Task 1).
- Produces:
  - `book_signature(so_lines, absences=None, frozen=None, downtimes=None)`
  - `build_payload(..., machine_downtime=None)` → adds the `"machine_downtime"` key
  - `parse_payload(payload)` → an **8-tuple**, `machine_downtime` **last**
  - `prepare_contest(..., machine_downtime=None)`
  - `ContestSetup.unavailable_reserved` (renamed from `absence_reserved`) and `ContestSetup.machine_downtime`

> ⚠ **Two deliberate contract changes, both listed in the spec.** `parse_payload`
> becomes an 8-tuple (FOUR assertions rebase), and `ContestSetup.absence_reserved`
> is renamed (nine references move). Neither is a fudge — the field now holds
> machine downtime too, so the old name would lie.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_machine_downtime_api.py`:

```python
"""Machine maintenance: the plumbing that makes a break reach every plan —
the book signature, the cloud payload and the contest setup."""
import io
import json
from datetime import date

from engine import book_store, loaders, optimize_service as svc, orderbook
from engine.config import Config
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A


def _book():
    wb = build_sample_bytes()
    book_store.save_masters_bytes(wb)
    _so, masters = loaders.load_all(io.BytesIO(wb))
    book_store.add_orders([Order("SO1", ITEM_A, "A", 20, date(2025, 3, 20))])
    cfg = Config(plan_start_date=date(2025, 3, 1))
    return book_store.load_active_orders(), cfg, masters


def _lines():
    orders, _cfg, masters = _book()
    return orderbook.active_so_lines(orders, book_store.load_actuals(), masters)


DOWN = [{"machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"}]


def test_book_signature_moves_when_a_break_is_added():
    lines = _lines()
    assert svc.book_signature(lines) != svc.book_signature(lines, downtimes=DOWN)


def test_book_signature_is_byte_identical_for_the_empty_case():
    """No pre-existing caller's signature may move just because the parameter
    now exists (the same guard `frozen` carries)."""
    lines = _lines()
    base = svc.book_signature(lines)
    assert svc.book_signature(lines, downtimes=[]) == base
    assert svc.book_signature(lines, downtimes=None) == base
    assert svc.book_signature(lines, absences=[], frozen=[], downtimes=[]) == base


def test_book_signature_is_order_insensitive_and_restores():
    lines = _lines()
    two = DOWN + [{"machine": "VMC1", "from_date": "2025-03-09", "to_date": "2025-03-09"}]
    assert svc.book_signature(lines, downtimes=two) == \
           svc.book_signature(lines, downtimes=list(reversed(two)))
    a = svc.book_signature(lines, downtimes=two)
    assert svc.book_signature(lines, downtimes=DOWN) != a
    assert svc.book_signature(lines, downtimes=two) == a


def test_a_downtime_only_book_cannot_collide_with_a_frozen_only_book():
    """The appended element is tagged, so the two optional blocks are never
    structurally confusable."""
    lines = _lines()
    frozen = [{"so_no": "SO1", "item_code": ITEM_A, "op_seq": 2,
               "machine": "CNC1", "remaining_qty": 4}]
    assert svc.book_signature(lines, frozen=frozen) != \
           svc.book_signature(lines, downtimes=DOWN)


def test_payload_round_trips_machine_downtime():
    orders, cfg, _m = _book()
    payload = svc.build_payload(orders, [], build_sample_bytes(), cfg, seed=42,
                                machine_downtime=DOWN)
    assert payload["machine_downtime"] == DOWN
    payload = json.loads(json.dumps(payload))          # the network hop
    result = svc.parse_payload(payload)
    assert len(result) == 8
    assert result[-1] == DOWN


def test_an_older_payload_without_the_key_still_parses():
    orders, cfg, _m = _book()
    payload = svc.build_payload(orders, [], build_sample_bytes(), cfg, seed=42)
    payload.pop("machine_downtime")                    # a job dispatched pre-deploy
    assert svc.parse_payload(payload)[-1] == []


def test_prepare_contest_reserves_the_machine_alongside_the_operators():
    orders, cfg, masters = _book()
    setup = svc.prepare_contest(orders, [], masters, cfg, machine_downtime=DOWN)
    assert "CNC1" in setup.unavailable_reserved
    assert setup.machine_downtime == DOWN


def test_prepare_contest_with_no_breaks_reserves_nothing():
    orders, cfg, masters = _book()
    setup = svc.prepare_contest(orders, [], masters, cfg)
    assert setup.unavailable_reserved is None
    assert setup.machine_downtime == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3.12 -m pytest tests/test_machine_downtime_api.py -v`
Expected: FAIL — `TypeError: book_signature() got an unexpected keyword argument 'downtimes'`

- [ ] **Step 3: Extend `book_signature`**

In `engine/optimize_service.py`, change the signature and append the block:

```python
def book_signature(so_lines, absences=None, frozen=None, downtimes=None):
```

and after the existing `if frozen:` block:

```python
    if downtimes:
        # Tagged, not a bare list: a downtime-only book must never be structurally
        # confusable with a frozen-only one. Appended ONLY when non-empty, so every
        # pre-existing caller's hash is byte-identical (same rule `frozen` follows).
        parts.append({"machine_downtime": sorted(
            (d.get("machine", ""), d.get("from_date", ""), d.get("to_date", ""))
            for d in downtimes)})
```

Extend the docstring's first sentence to mention "…the operator absences, any machine
maintenance breaks, and any frozen (in-progress) operations."

- [ ] **Step 4: Extend the payload**

In `build_payload`, add the parameter and the key:

```python
def build_payload(orders: dict, actuals, masters_bytes, config: Config, *,
                  seed: int, candidates=CLOUD_OVERLAP_CANDIDATES,
                  budget_per_candidate=CLOUD_BUDGET_PER_CANDIDATE,
                  absences=None, operator_table=None, frozen=None,
                  machine_downtime=None) -> dict:
```

```python
        "frozen": list(frozen or []),
        "machine_downtime": list(machine_downtime or []),
    }
```

In `parse_payload`, read it and return it last:

```python
    frozen = list(payload.get("frozen") or [])
    # Read with .get so a job dispatched before this key existed still parses.
    machine_downtime = list(payload.get("machine_downtime") or [])
    return (orders, actuals, masters, config, absences, operator_table, frozen,
            machine_downtime)
```

Update its docstring's tuple listing to name the eighth element.

- [ ] **Step 5: Extend `ContestSetup` and `prepare_contest`, and rename the field**

In `ContestSetup`, replace the `absence_reserved` field with:

```python
    # Everything that is UNAVAILABLE, in the one dict the whole app threads through:
    # operator absences keyed by NAME, machine maintenance breaks keyed by MACHINE ID.
    # Named for what it holds — it used to be `absence_reserved`, which stopped being
    # true when machine downtime joined it (2026-08-31).
    unavailable_reserved: object = None
    # The raw maintenance rows, carried so reporting surfaces can reuse them.
    machine_downtime: list = field(default_factory=list)
```

In `prepare_contest`, add the parameter and merge:

```python
def prepare_contest(orders: dict, actuals, masters, config: Config,
                    absences=None, operator_table=None, frozen=None,
                    machine_downtime=None) -> ContestSetup:
```

```python
    ab = merge_reservations(absence_reservations(absences),
                            downtime_reservations(machine_downtime))
```

and in the returned `ContestSetup(...)`:

```python
                        absences=list(absences or []),
                        unavailable_reserved=(ab or None),
                        machine_downtime=list(machine_downtime or []),
```

In `run_candidate`, unpack eight and pass it on:

```python
    (orders, actuals, masters, config, absences, operator_table, frozen,
     machine_downtime) = parse_payload(payload)
```
```python
    setup = prepare_contest(orders, actuals, masters, config, absences=absences,
                            operator_table=operator_table, frozen=frozen,
                            machine_downtime=machine_downtime)
```
```python
    res = optimizer.optimize(setup.target, cfg, setup.masters,
                             reserved=setup.unavailable_reserved,
```

- [ ] **Step 6: Rename the two readers in `api/main.py`**

```bash
python3.12 - <<'PY'
import pathlib
p = pathlib.Path("api/main.py")
s = p.read_text()
assert s.count("setup.absence_reserved") == 2, s.count("setup.absence_reserved")
p.write_text(s.replace("setup.absence_reserved", "setup.unavailable_reserved"))
PY
grep -rn "absence_reserved" api/ engine/ || echo "clean"
```
Expected: `clean`

- [ ] **Step 7: Rebase the four affected existing tests**

```bash
python3.12 - <<'PY'
import pathlib
p = pathlib.Path("tests/test_absences_engine.py")
s = p.read_text()
assert s.count("absence_reserved") == 5, s.count("absence_reserved")
s = s.replace("absence_reserved", "unavailable_reserved")
s = s.replace("assert len(result) == 7", "assert len(result) == 8")
s = s.replace("_, _, _, _, absences2, _, _ = result",
              "_, _, _, _, absences2, _, _, _ = result")
p.write_text(s)

p = pathlib.Path("tests/test_optimize_service.py")
s = p.read_text()
s = s.replace(
    "orders2, actuals2, masters2, cfg2, absences2, optable2, frozen2 = svc.parse_payload(payload)",
    "(orders2, actuals2, masters2, cfg2, absences2, optable2, frozen2,\n"
    "     _downtime2) = svc.parse_payload(payload)")
p.write_text(s)

p = pathlib.Path("tests/test_freeze_contest.py")
s = p.read_text()
s = s.replace(
    "    assert parsed[-1] == frozen  # frozen is the last element of the parse tuple",
    "    # machine_downtime is now the last element; frozen is second-to-last\n"
    "    # (2026-08-31: the payload carries maintenance breaks too).\n"
    "    assert parsed[-2] == frozen\n"
    "    assert parsed[-1] == []")
p.write_text(s)

# A FOURTH arity assertion, found by a repo-wide caller grep rather than by the
# plan's original file list. `parsed[5] == table` stays valid -- operator_table is
# still the sixth element; only the length changes.
p = pathlib.Path("tests/test_operator_wiring.py")
s = p.read_text()
assert s.count("assert len(parsed) == 7") == 1
s = s.replace("assert len(parsed) == 7", "assert len(parsed) == 8")
p.write_text(s)
print("rebased")
PY
```

- [ ] **Step 8: Run the tests**

Run: `python3.12 -m pytest tests/test_machine_downtime_api.py tests/test_absences_engine.py tests/test_optimize_service.py tests/test_freeze_contest.py -v`
Expected: PASS

Run: `python3.12 -m pytest -q`
Expected: `936 passed, 2 skipped`

- [ ] **Step 9: Commit**

```bash
git add engine/optimize_service.py api/main.py tests/test_machine_downtime_api.py \
        tests/test_absences_engine.py tests/test_optimize_service.py tests/test_freeze_contest.py
git commit -m "feat(maintenance): carry breaks through the signature, payload and contest"
```

---

### Task 6: Make every plan honour the breaks

**Files:**
- Modify: `api/main.py` — `_current_book_sig` (~:1262), `_plan` (~:828-880), `_start_optimize` (~:1519-1535), `_incumbent_metrics` (~:2115), `_metrics_for_ranks` (~:2090), `_movement_note` (~:2060), `_plan_run_for_report` (~:2780)
- Test: `tests/test_machine_downtime_api.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1-5.
- Produces: no new names. After this task the feature is **live**: a stored break changes the plan.

**Why all seven call sites:** each builds its own `prepare_contest`/`reserved`. Missing one means a contest scores plans against a shop that has the machine, while the plan runs without it — the exact "two ways to read the same thing" class this codebase keeps paying for.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_machine_downtime_api.py`:

```python
# --------------------------------------------------------------------------- #
# The plan itself
# --------------------------------------------------------------------------- #
import importlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient


def _api():
    import api.main
    return importlib.reload(api.main)


def _seed(m):
    wb = build_sample_bytes()
    book_store.save_masters_bytes(wb)
    book_store.add_orders([Order("SO1", ITEM_A, "A", 20, date(2025, 3, 20))])
    m._current_masters()          # trigger the one-time operator seed


def test_a_break_changes_the_book_signature_the_cache_keys_on():
    m = _api()
    _seed(m)
    before = m._current_book_sig()
    book_store.save_machine_downtime(
        {"machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"})
    assert m._current_book_sig() != before


def test_adding_a_break_invalidates_the_plan_cache():
    """Mirrors test_plan_cache.py::test_absence_invalidates. Without this the
    Settings panel would add a break and the screen would show the old plan."""
    m = _api()
    _seed(m)
    cfg = m._load_plan_config()
    first = m._plan(cfg)
    assert m._plan(cfg)["run_id"] == first["run_id"]        # cache hit
    book_store.save_machine_downtime(
        {"machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"})
    assert m._plan(cfg)["run_id"] != first["run_id"]        # recomputed


def test_the_plan_schedules_nothing_on_a_machine_that_is_out_of_service():
    m = _api()
    _seed(m)
    plain = m._plan(m._load_plan_config())
    used = {e.machine for e in m._PLAN_CACHE["artifacts"]["plan_run"].schedule}
    if "CNC1" not in used:
        pytest.skip("this sample book does not use CNC1; nothing to prove")
    start = m._resolve_config(m._load_plan_config()).plan_start_date
    book_store.save_machine_downtime(
        {"machine": "CNC1", "from_date": start.isoformat(),
         "to_date": (start + timedelta(days=3)).isoformat()})
    m._plan(m._load_plan_config())
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    clash = [e for e in sched
             if e.machine == "CNC1"
             and e.start.date() <= start + timedelta(days=3)]
    assert clash == [], f"work planned on an out-of-service machine: {clash[:3]}"


def test_removing_the_break_restores_the_original_plan():
    m = _api()
    _seed(m)
    cfg = m._load_plan_config()
    before = m._plan(cfg)
    row = book_store.save_machine_downtime(
        {"machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"})
    m._plan(cfg)
    book_store.delete_machine_downtime(row["id"])
    after = m._plan(cfg)
    assert after["expected_end"] == before["expected_end"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3.12 -m pytest tests/test_machine_downtime_api.py -k "book_signature or invalidates or out_of_service or restores" -v`
Expected: FAIL — the signature does not move and the plan is unchanged.

- [ ] **Step 3: Wire `_current_book_sig`**

In `api/main.py::_current_book_sig` (~line 1262):

```python
    absences = book_store.load_absences()
    downtimes = book_store.load_machine_downtime()
    return optimize_service.book_signature(lines, absences=absences,
                                           downtimes=downtimes)
```

- [ ] **Step 4: Wire `_plan`**

In `api/main.py::_plan`, replace the absence-reservation lines (~line 831):

```python
    absences_raw = book_store.load_absences()
    # Machine maintenance breaks reserve the MACHINE, operator absences reserve the
    # PERSON — one merged dict, the same one every engine already reads. Loaded once
    # and reused below for the reports.
    downtime_raw = book_store.load_machine_downtime()
    ab = optimize_service.merge_reservations(
        optimize_service.absence_reservations(absences_raw),
        optimize_service.downtime_reservations(downtime_raw))
```

- [ ] **Step 5: Wire the five contest/metrics call sites**

Each already builds `prepare_contest(...)`; add the new keyword to all five.

`_start_optimize` (~line 1519), which already loads `absences`:
```python
        absences = book_store.load_absences()
        machine_downtime = book_store.load_machine_downtime()
```
```python
            setup = optimize_service.prepare_contest(orders, actuals, masters, config,
                                                     absences=absences,
                                                     operator_table=operator_table,
                                                     frozen=frozen,
                                                     machine_downtime=machine_downtime)
```
and in the cloud payload build in the same function:
```python
                absences=absences, operator_table=operator_table, frozen=frozen,
                machine_downtime=machine_downtime)
```

`_movement_note` (~line 2066), `_metrics_for_ranks` (~line 2099),
`_incumbent_metrics` (~line 2129) and **`_optimize_apply` (~line 2227)** each call
`prepare_contest`; add to each:
```python
            machine_downtime=book_store.load_machine_downtime(),
```

> **`_optimize_apply` is the one that matters most, and the plan's first draft missed
> it** (found by a repo-wide call-site grep before dispatch). It builds the schedule
> that `book_store.save_last_applied_schedule` stores as "the plan the floor is
> following" — and that stored record is exactly what `engine/freeze.compute_frozen_set`
> reads to decide which operation is pinned to which machine. Build it without knowing
> about a maintenance break and it places work on an out-of-service machine, so the
> frozen set derived from it pins operations to a machine that was never running them.
> That is the 2026-08-11 failure shape: a stored artifact quietly disagreeing with the
> plan everyone else sees.

`_plan_run_for_report`'s fallback branch (~line 2800):
```python
    ab = optimize_service.merge_reservations(
        optimize_service.absence_reservations(book_store.load_absences()),
        optimize_service.downtime_reservations(book_store.load_machine_downtime()))
```

- [ ] **Step 6: Verify no call site was missed**

```bash
grep -n "prepare_contest(" api/main.py
grep -c "machine_downtime" api/main.py
```
Expected: **five** `prepare_contest(` call sites in `api/main.py`
(`_start_optimize`, `_movement_note`, `_metrics_for_ranks`, `_incumbent_metrics`,
`_optimize_apply`), **each** with `machine_downtime=`; the `machine_downtime` count is
at least 10. If you find a different number of call sites, stop and report it — a
missed one means some path plans against a shop that still has the machine.

- [ ] **Step 7: Run the tests**

Run: `python3.12 -m pytest tests/test_machine_downtime_api.py -v`
Expected: PASS

Run: `python3.12 -m pytest -q`
Expected: `940 passed, 2 skipped`

- [ ] **Step 8: Commit**

```bash
git add api/main.py tests/test_machine_downtime_api.py
git commit -m "feat(maintenance): every plan and every contest honours the breaks"
```

---

### Task 7: The three endpoints, and the orphan report row

**Files:**
- Modify: `api/main.py` — request model, `_machining_machine_options`, `_machine_downtime_orphans`, `_report_for_book` (~:473), three endpoints (after `/absences`, ~:2477)
- Modify: `tests/test_role_parity.py:234-245` (extend the write-endpoint list)
- Test: `tests/test_machine_downtime_api.py` (append)

**Interfaces:**
- Consumes: `book_store` accessors (Task 1).
- Produces: `GET/POST/DELETE /machine-downtime`; report kind `MACHINE_DOWNTIME_UNKNOWN`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_machine_downtime_api.py`:

```python
# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def _client(m, role="admin"):
    c = TestClient(m.app)
    creds = ({"username": "anvitech", "password": "1930rail"} if role == "admin"
             else {"username": "anvitech_user", "password": "anvitech12345678"})
    c.post("/login", data=creds)
    return c


def test_get_is_open_to_both_roles_and_lists_only_cnc_and_vmc():
    m = _api()
    _seed(m)
    for role in ("admin", "user"):
        r = _client(m, role).get("/machine-downtime")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"downtime", "orphans", "machines"}
        assert body["downtime"] == [] and body["orphans"] == []
        ids = [x["id"] for x in body["machines"]]
        assert ids, "the picker must offer something"
        assert all(i.startswith(("CNC", "VMC")) for i in ids), ids


def test_the_picker_excludes_manual_and_inspection_stations():
    m = _api()
    _seed(m)
    ids = [x["id"] for x in _client(m).get("/machine-downtime").json()["machines"]]
    masters = m._current_masters()
    manual = [k for k in masters.machines if k.startswith(("MW", "MD", "MPK", "MI", "BS"))]
    assert manual, "the sample book should have manual stations to exclude"
    assert not (set(ids) & set(manual))


def test_post_and_delete_are_admin_only():
    m = _api()
    _seed(m)
    user = _client(m, "user")
    body = {"machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"}
    assert user.post("/machine-downtime", json=body).status_code == 403
    assert user.delete("/machine-downtime/whatever").status_code == 403
    assert book_store.load_machine_downtime() == []


def test_admin_can_add_and_remove_a_break():
    m = _api()
    _seed(m)
    admin = _client(m)
    r = admin.post("/machine-downtime", json={
        "machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06",
        "reason": "Spindle service"})
    assert r.status_code == 200
    row = r.json()["downtime"]
    assert row["machine"] == "CNC1" and row["reason"] == "Spindle service"
    assert len(admin.get("/machine-downtime").json()["downtime"]) == 1
    assert admin.delete(f"/machine-downtime/{row['id']}").json() == {"deleted": True}
    assert admin.get("/machine-downtime").json()["downtime"] == []


def test_bad_input_is_rejected_with_a_clear_message():
    m = _api()
    _seed(m)
    admin = _client(m)
    bad_date = admin.post("/machine-downtime", json={
        "machine": "CNC1", "from_date": "05-03-2025", "to_date": "2025-03-06"})
    assert bad_date.status_code == 400
    assert "YYYY-MM-DD" in bad_date.json()["detail"]
    unknown = admin.post("/machine-downtime", json={
        "machine": "NOPE9", "from_date": "2025-03-05", "to_date": "2025-03-06"})
    assert unknown.status_code == 400 and "NOPE9" in unknown.json()["detail"]
    assert admin.delete("/machine-downtime/nope").status_code == 404


def test_a_reversed_range_is_swapped_not_rejected():
    m = _api()
    _seed(m)
    r = _client(m).post("/machine-downtime", json={
        "machine": "CNC1", "from_date": "2025-03-06", "to_date": "2025-03-05"})
    assert r.status_code == 200
    row = r.json()["downtime"]
    assert (row["from_date"], row["to_date"]) == ("2025-03-05", "2025-03-06")


def test_an_absurd_date_range_is_rejected():
    """A fat-fingered to_date must not reach the store: several engine paths walk a
    break day by day, so a year-9999 end date would be millions of iterations per row
    on every plan. Validate at the boundary, where the input enters."""
    m = _api()
    _seed(m)
    r = _client(m).post("/machine-downtime", json={
        "machine": "CNC1", "from_date": "2025-03-05", "to_date": "9999-12-31"})
    assert r.status_code == 400
    assert "longer than a year" in r.json()["detail"]
    assert book_store.load_machine_downtime() == []


def test_a_long_but_plausible_break_is_accepted():
    """The bound must not reject a real, if unusual, outage."""
    m = _api()
    _seed(m)
    r = _client(m).post("/machine-downtime", json={
        "machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-09-05"})
    assert r.status_code == 200


def test_a_break_on_a_machine_that_left_the_master_is_flagged_not_fatal():
    m = _api()
    _seed(m)
    book_store.save_machine_downtime(
        {"machine": "GHOST1", "from_date": "2025-03-05", "to_date": "2025-03-06"})
    admin = _client(m)
    assert admin.get("/machine-downtime").json()["orphans"] == ["GHOST1"]
    rows = admin.post("/run", json={}).json()["report"]["rows"]
    pairs = {(r[0], r[1]) for r in rows}
    assert ("MACHINE_DOWNTIME_UNKNOWN", "GHOST1") in pairs
    # Non-blocking: the plan still ran.
    assert admin.post("/run", json={}).json()["gantt"] is not None


def test_adding_a_break_never_starts_a_contest(monkeypatch):
    """Only the Done button triggers a search — same rule as absences."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "manual")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3")
    m = _api()
    _seed(m)
    starts = []
    monkeypatch.setattr(m, "_start_optimize",
                        lambda *a, **k: starts.append(1))
    admin = _client(m)
    r = admin.post("/machine-downtime", json={
        "machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"})
    admin.delete(f"/machine-downtime/{r.json()['downtime']['id']}")
    assert starts == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3.12 -m pytest tests/test_machine_downtime_api.py -k "picker or admin or bad_input or reversed or orphan or never_starts or both_roles" -v`
Expected: FAIL — 404 on `/machine-downtime`.

- [ ] **Step 3: Add the request model**

In `api/main.py`, after `AbsenceRequest` (~line 580):

```python
class MachineDowntimeRequest(BaseModel):
    machine: str
    from_date: str
    to_date: str
    reason: str = Field(default="", max_length=200)
```

- [ ] **Step 4: Add the picker filter and the orphan helper**

After `_machine_options` (~line 2500):

```python
def _machining_machine_options(masters):
    """The CNC/VMC subset of the Machine master — the machines the maintenance
    picker offers (owner decision, 2026-08-31).

    Classified by the machine's TYPE using the same function the live engine uses
    (`ppc_engine.loaders.normalize.machine_kind_from_type`), so there is one
    definition of "is this a machining station" and a CNC8 added to the Excel
    appears with no code change. A PROVISIONAL machine (referenced by a routing but
    not in the Machine master) has no real type, so it falls back to the id prefix —
    the same rule `ppc_engine…register_provisional_machines` applies. There are none
    in the current workbooks; this keeps a future one from silently vanishing.

    The ENGINE honours downtime on any machine; only this picker is filtered, so
    widening it later is a one-line change here and nothing else."""
    from ppc_engine.domain.resources import MachineKind
    from ppc_engine.loaders.normalize import machine_kind_from_type

    def _is_machining(m):
        if m["provisional"]:
            return m["id"].startswith("CNC") or m["id"].startswith("VMC")
        return machine_kind_from_type(m["type"]) == MachineKind.MACHINING

    return [m for m in _machine_options(masters) if _is_machining(m)]


def _machine_downtime_orphans(masters, downtime=None) -> list:
    """Machine ids on file in maintenance breaks that are no longer in the Machine
    master (e.g. removed on a re-upload). Sorted for stable output. Mirror of
    `_absence_orphans`: ignored by planning, reported, never fatal."""
    if downtime is None:
        downtime = book_store.load_machine_downtime()
    known = set(masters.machines)
    return sorted({d["machine"] for d in downtime
                   if d.get("machine") and d["machine"] not in known})
```

- [ ] **Step 5: Add the report row**

In `_report_for_book`, change the signature to append the parameter:

```python
def _report_for_book(masters, so_lines, absences=None, config=None, schedule=None,
                     batches=None, downtime=None):
```

and after the `ABSENT_OPERATOR_UNKNOWN` loop:

```python
    for mid in _machine_downtime_orphans(masters, downtime=downtime):
        rows.append({"kind": "MACHINE_DOWNTIME_UNKNOWN", "ref": mid,
                     "message": f"maintenance break recorded for machine '{mid}', "
                                f"which is not in the current Machine master: ignored"})
```

In `_plan`'s call to it (~line 999), pass the rows it already loaded:

```python
              "report": _report_for_book(masters, so_lines, absences=absences_raw,
                                         config=config, schedule=plan_run.schedule,
                                         batches=plan_run.batches_prioritized,
                                         downtime=downtime_raw),
```

- [ ] **Step 6: Add the three endpoints**

In `api/main.py`, immediately after `delete_absence_ep` (~line 2477):

```python
@app.get("/machine-downtime")
def get_machine_downtime():
    """Machine maintenance breaks on file, any logged-in role (the list is
    read-only for the user role; the add/remove controls are admin-only).
    `machines` is the CNC/VMC picker list; `orphans` flags breaks whose machine is
    no longer in the Machine master — also surfaced in the validation report as
    MACHINE_DOWNTIME_UNKNOWN."""
    masters = _current_masters()
    downtime = book_store.load_machine_downtime()
    return {"downtime": downtime,
            "orphans": _machine_downtime_orphans(masters, downtime=downtime),
            "machines": _machining_machine_options(masters)}


@app.post("/machine-downtime")
def create_machine_downtime(req: MachineDowntimeRequest, request: Request):
    """Mark a machine out of service for a date range. Admin only. Dates must parse
    (YYYY-MM-DD); a reversed range is accepted and normalized (swapped) rather than
    rejected. The machine must exist in the current Machine master.

    Deliberately validates EXISTENCE, not kind: the engine honours downtime on any
    machine and only the picker is filtered to CNC/VMC, so widening the picker later
    needs no change here. No optimize trigger — only the Done button starts a
    contest (same rule as absences)."""
    require_admin(request)
    try:
        d_from = date.fromisoformat(req.from_date)
        d_to = date.fromisoformat(req.to_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="dates must be YYYY-MM-DD")
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    # Bound the span. A machine out for more than a year is not "on maintenance" —
    # it should leave the Machine master. The cap also protects the engine: several
    # places walk a break day by day, so an absurd stored to_date (a fat-fingered
    # 9999-12-31) would mean millions of iterations per row on every plan. Validate
    # where the input enters rather than hardening every consumer.
    if (d_to - d_from).days > 366:
        raise HTTPException(
            status_code=400,
            detail="a maintenance break cannot be longer than a year — if a machine "
                   "is out for longer, remove it from the Machine master instead")
    machine = req.machine.strip()
    if machine not in _current_masters().machines:
        raise HTTPException(status_code=400,
                            detail=f"unknown machine '{machine}'")
    saved = book_store.save_machine_downtime({
        "machine": machine, "from_date": d_from.isoformat(),
        "to_date": d_to.isoformat(), "reason": req.reason.strip()})
    return {"downtime": saved}


@app.delete("/machine-downtime/{downtime_id}")
def delete_machine_downtime_ep(downtime_id: str, request: Request):
    """Remove a maintenance break. Admin only."""
    require_admin(request)
    if not book_store.delete_machine_downtime(downtime_id):
        raise HTTPException(status_code=404, detail="maintenance break not found")
    return {"deleted": True}
```

- [ ] **Step 7: Extend the role-parity invariant**

In `tests/test_role_parity.py`, add to the list in `test_every_write_endpoint_stays_admin_only`
(after the `/absences` entry):

```python
        ("post", "/machine-downtime", {"machine": "CNC1", "from_date": "2025-03-01",
                                       "to_date": "2025-03-02"}),
```

- [ ] **Step 8: Run the tests**

Run: `python3.12 -m pytest tests/test_machine_downtime_api.py tests/test_role_parity.py -v`
Expected: PASS

Run: `python3.12 -m pytest -q`
Expected: `950 passed, 2 skipped`

- [ ] **Step 9: Commit**

```bash
git add api/main.py tests/test_machine_downtime_api.py tests/test_role_parity.py
git commit -m "feat(maintenance): add the machine-downtime endpoints and orphan row"
```

---

### Task 8: Analytics — a machine in pieces is not "available"

**Files:**
- Modify: `engine/analytics.py` — imports, `_down_days`, `build_analytics`
- Modify: `api/main.py:643-648` (`_augment_helpers`'s `build_analytics` call)
- Test: `tests/test_machine_downtime_reports.py` (create)

**Interfaces:**
- Consumes: the stored rows.
- Produces: `build_analytics(schedule, masters, config, batches=None, absences=None, downtime=None)` — `downtime` **appended last**.

> ⚠ **Append only.** `batches` is passed **positionally** at
> `tests/test_analytics.py:150`, `:345` and `tests/test_plan_consistency.py:254`.
> Inserting a parameter before it silently changes what those tests pass.

- [ ] **Step 1: Write the failing test**

Create `tests/test_machine_downtime_reports.py`:

```python
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
    # of clock time per anchored day pair, clipped to the window).
    assert base - down > 20.0


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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3.12 -m pytest tests/test_machine_downtime_reports.py -v`
Expected: FAIL — `TypeError: build_analytics() got an unexpected keyword argument 'downtime'`

- [ ] **Step 3: Add `_down_days` and widen the import**

In `engine/analytics.py`, change the datetime import:

```python
from datetime import datetime, time, timedelta
```

and add after `_absent_days`:

```python
def _down_days(machine_id, downtime, calendar, win_start_d, win_end_d):
    """The SET of dates ``machine_id`` is out of service on a WORKING day inside the
    window. Mirror of ``_absent_days`` for the other resource an operation needs.
    Tolerates malformed rows (skip), exactly as absences do."""
    from datetime import date as _date
    out = set()
    for d in downtime or []:
        if d.get("machine") != machine_id:
            continue
        try:
            f = _date.fromisoformat(d["from_date"]); t = _date.fromisoformat(d["to_date"])
        except (KeyError, ValueError, TypeError):
            continue
        if t < f:
            f, t = t, f
        cur = max(f, win_start_d)
        while cur <= min(t, win_end_d):
            if calendar.is_working_day(cur):
                out.add(cur)
            cur += timedelta(days=1)
    return out
```

- [ ] **Step 4: Subtract the down days from available hours**

In `build_analytics`, change the signature:

```python
def build_analytics(schedule, masters, config, batches=None, absences=None,
                    downtime=None):
```

and replace the body of `avail_hrs` with:

```python
    def avail_hrs(mid):
        """Available machine-hours in the plan window. Normally the operator-coverage
        clock (matches how the OLD engine scheduled). But the NEW (production) engine
        schedules manual/finishing work regardless of first-shift operator coverage, so
        an uncovered-but-used station would get a 0-capacity clock and show '-' ("no
        capacity") for a machine the plan actually uses (live 2026-07-24 report). When
        the gated clock is empty, fall back to the machine's PHYSICAL window so
        utilization stays honest; a covered machine is unchanged (byte-identical).

        Maintenance breaks are then subtracted: a machine that was in pieces was not
        available, and reporting it as idle-but-available would be a lie about
        capacity. The subtraction uses the machine's own windows ANCHORED on each down
        day (`_windows_for_day`), which is exactly how the engine blocks them — a
        night shift anchored on D runs to 05:00 on D+1 and is removed with D."""
        clock = clock_for(mid)
        mins = clock.working_minutes_between(win_start, win_end)
        if mins == 0:
            mac = masters.machines.get(mid)
            if mac is not None:
                clock = WorkClock(masters.calendar, eligible_window(mac, config))
                mins = clock.working_minutes_between(win_start, win_end)
        for d in _down_days(mid, downtime, masters.calendar,
                            win_start.date(), win_end.date()):
            for ws, we in clock._windows_for_day(d):
                s, e = max(ws, win_start), min(we, win_end)
                if e > s:
                    mins -= (e - s).total_seconds() / 60.0
        return max(mins, 0.0) / 60.0
```

- [ ] **Step 5: Pass the rows from the API**

In `api/main.py::_augment_helpers`, the `build_analytics` call (~line 645) — note
`_augment_helpers` has no downtime in scope, so load it there:

```python
        trace["analytics"] = _an.build_analytics(
            plan_run.schedule, masters, config, plan_run.batches_prioritized,
            absences=book_store.load_absences(),
            downtime=book_store.load_machine_downtime())
```

- [ ] **Step 6: Run the tests**

Run: `python3.12 -m pytest tests/test_machine_downtime_reports.py tests/test_analytics.py tests/test_plan_consistency.py -v`
Expected: PASS — the appended parameter leaves every positional caller intact.

Run: `python3.12 -m pytest -q`
Expected: `955 passed, 2 skipped`

- [ ] **Step 7: Commit**

```bash
git add engine/analytics.py api/main.py tests/test_machine_downtime_reports.py
git commit -m "feat(maintenance): analytics stops counting a down machine as available"
```

---

### Task 9: The delay report names maintenance instead of blaming the crew

**Files:**
- Modify: `engine/delay_report.py` — `_classify_free`, `_why_summary`, `build_delay_report`
- Modify: `api/main.py` — `_DELAY_FILLS`, `_DELAY_SUMMARY_COLS`, the `/delay-report.xlsx` call
- Test: `tests/test_machine_downtime_reports.py` (append)

**Interfaces:**
- Produces: `build_delay_report(schedule, so_lines, batches_prioritized, config, masters, downtime=None)` — **appended last**; new detail state `"MAINTENANCE (machine down)"`; new summary column `"Maintenance (days)"`.

> ⚠ **Append only.** All five parameters are passed **positionally** in every test
> (`tests/test_delay_report.py:48,69,87,105,121`,
> `tests/test_delay_report_attribution.py:83…218`,
> `tests/test_plan_consistency.py:71,281`).
>
> ⚠ **Do not add an xlsx sheet.** `wb.sheetnames == ["Summary", "Detail"]` is asserted
> at `tests/test_delay_report_api.py:36` and `:62`. A new **column** is fine.
>
> ⚠ The new state string must not contain `"unattributed"` or `"(free)"`
> (`tests/test_delay_report.py:90-91`). `"MAINTENANCE (machine down)"` is safe.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_machine_downtime_reports.py`:

```python
# --------------------------------------------------------------------------- #
# Delay report
# --------------------------------------------------------------------------- #
from engine.delay_report import build_delay_report
from engine.models import Batch, SOLine

DR_CFG = Config(plan_start_date=date(2025, 3, 3), apply_operator_logic=True)


def _line(so="SO1", item="X", due=date(2025, 3, 20)):
    return SOLine(so_no=so, item_code=item, item_name="X", qty=10, delivery_date=due)


def _batch():
    return Batch(batch_id="B001", item_code="X", item_name="X", qty=10,
                 so_delivery_date=date(2025, 3, 20), source_so_refs=["SO1"])


def _dr_masters():
    from engine.models import Operator
    return Masters(
        machines={"CNC1": Machine("CNC1", "CNC 1", "CNC lathe",
                                  available_hrs_per_day=19.5)},
        operators=[Operator("Anil", "CNC1", ["CNC1"], "First shift")],
        routings={"X": Routing("X", "", "", "", None, processes=[
            Process(1, "CNC first side", 1.0, 1.0, "CNC1", "CNC1")])},
        calendar=WorkCalendar())


def test_a_gap_on_a_down_day_is_called_maintenance_not_crew():
    """The machine is free and every operator is free, but the machine is in
    pieces. Without this the hours land in WAITING (crew) or IDLE — the report
    would blame the crew for a spindle service."""
    m = _dr_masters()
    e1 = _entry(datetime(2025, 3, 3, 8), datetime(2025, 3, 3, 10))
    e2 = _entry(datetime(2025, 3, 7, 8), datetime(2025, 3, 7, 10))
    down = [{"machine": "CNC1", "from_date": "2025-03-04", "to_date": "2025-03-05"}]
    rep = build_delay_report([e1, e2], [_line()], [_batch()], DR_CFG, m, down)
    states = {r["State"] for r in rep["detail"]}
    assert "MAINTENANCE (machine down)" in states
    maint = [r for r in rep["detail"] if r["State"] == "MAINTENANCE (machine down)"]
    assert all("maintenance" in r["Why"].lower() for r in maint)
    assert all(r["Machine"] == "CNC1" for r in maint)


def test_without_downtime_the_same_plan_reports_no_maintenance():
    m = _dr_masters()
    e1 = _entry(datetime(2025, 3, 3, 8), datetime(2025, 3, 3, 10))
    e2 = _entry(datetime(2025, 3, 7, 8), datetime(2025, 3, 7, 10))
    rep = build_delay_report([e1, e2], [_line()], [_batch()], DR_CFG, m)
    assert not [r for r in rep["detail"]
                if r["State"] == "MAINTENANCE (machine down)"]


def test_every_hour_is_still_accounted_for_with_maintenance():
    """The invariant that must never break: work + every wait == the order's span."""
    m = _dr_masters()
    e1 = _entry(datetime(2025, 3, 3, 8), datetime(2025, 3, 3, 10))
    e2 = _entry(datetime(2025, 3, 7, 8), datetime(2025, 3, 7, 10))
    down = [{"machine": "CNC1", "from_date": "2025-03-04", "to_date": "2025-03-05"}]
    rep = build_delay_report([e1, e2], [_line()], [_batch()], DR_CFG, m, down)
    total = sum(r["Hours"] for r in rep["detail"])
    span = (datetime(2025, 3, 7, 10) - datetime(2025, 3, 3, 8)).total_seconds() / 3600
    assert abs(total - span) < 1e-6


def test_the_summary_reports_maintenance_as_its_own_cause():
    m = _dr_masters()
    e1 = _entry(datetime(2025, 3, 3, 8), datetime(2025, 3, 3, 10))
    e2 = _entry(datetime(2025, 3, 7, 8), datetime(2025, 3, 7, 10))
    down = [{"machine": "CNC1", "from_date": "2025-03-04", "to_date": "2025-03-05"}]
    s = build_delay_report([e1, e2], [_line()], [_batch()], DR_CFG, m, down)["summary"][0]
    assert "Maintenance (days)" in s
    assert s["Maintenance (days)"] > 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3.12 -m pytest tests/test_machine_downtime_reports.py -k "maintenance or accounted" -v`
Expected: FAIL — `build_delay_report() takes 5 positional arguments but 6 were given`

- [ ] **Step 3: Split the machine-free window into down and up pieces**

In `engine/delay_report.py`, replace `_classify_free` with:

```python
def _classify_free(a, b, clock, machine=None, masters=None, config=None, op_busy=None,
                   down_days=None):
    """Split a machine-free interval into off-hours (outside the machine's working
    window) and, inside it, maintenance, a genuine crew shortage, or plain idle
    capacity.

    Windows are bucketed by the day they are ANCHORED on, which is how the engine
    blocks a maintenance day: a two-shift machine's night window opens 19:00 on D and
    closes 05:00 on D+1, and the whole of it belongs to D. Bucketing by anchor day
    (rather than splitting at midnight) is what makes the report agree with the plan
    to the minute."""
    down_days = down_days or set()
    # Start a day early: a two-shift machine's night window (e.g. 19:00→05:00) belongs to
    # the PREVIOUS day but overflows into this one, so it can cover the early morning of `a`.
    up, down, d = [], [], a.date() - timedelta(days=1)
    while datetime.combine(d, datetime.min.time()) < b:
        for ws, we in clock._windows_for_day(d):
            s, e = max(ws, a), min(we, b)
            if e > s:
                (down if d in down_days else up).append((s, e))
        d = d + timedelta(days=1)
    up, down = _merge(up), _merge(down)
    rows = []
    for s, e in down:
        rows.append({
            "State": "MAINTENANCE (machine down)", "Process": "",
            "Machine": machine or "", "Operator": "", "From": s, "To": e,
            "Hours": round(_hours(s, e), 2),
            "Why": "Machine out of service for maintenance"})
    for s, e in up:
        if masters is None or config is None:
            pieces = [(s, e, False)]        # no staffing data — keep the old behaviour
        else:
            pieces = _staffing_split(s, e, machine, masters, config, op_busy or {})
        for ps, pe, someone_free in pieces:
            if pe <= ps:
                continue
            if someone_free:
                rows.append({
                    "State": "IDLE (capacity free)", "Process": "", "Machine": machine or "",
                    "Operator": "", "From": ps, "To": pe,
                    "Hours": round(_hours(ps, pe), 2),
                    "Why": ("Machine free and a qualified operator free — spare "
                            "capacity, nothing was scheduled here")})
            else:
                rows.append({
                    "State": "WAITING (crew)", "Process": "", "Machine": "", "Operator": "",
                    "From": ps, "To": pe, "Hours": round(_hours(ps, pe), 2),
                    "Why": "Machine free — every qualified operator was busy elsewhere"})
    for s, e in _gaps(a, b, up + down):
        rows.append({"State": "WAITING (off-hours)", "Process": "", "Machine": "", "Operator": "",
                     "From": s, "To": e, "Hours": round(_hours(s, e), 2),
                     "Why": "Outside working hours (night / weekly off / holiday)"})
    return rows
```

- [ ] **Step 4: Thread the rows through `build_delay_report`**

Change the signature:

```python
def build_delay_report(schedule, so_lines, batches_prioritized, config, masters,
                       downtime=None):
```

Add near the top, after `op_busy = _operator_bookings(schedule)`:

```python
    # Maintenance days per machine, as dates — so a machine-free window on a day the
    # machine was out of service is attributed to maintenance, never to the crew.
    down_by_machine: dict = {}
    for d in downtime or []:
        try:
            f = date.fromisoformat(d["from_date"]); t = date.fromisoformat(d["to_date"])
        except (KeyError, ValueError, TypeError):
            continue
        if t < f:
            f, t = t, f
        days = down_by_machine.setdefault(d.get("machine", ""), set())
        cur = f
        while cur <= t:
            days.add(cur)
            cur += timedelta(days=1)
    down_by_machine.pop("", None)
```

Add `date` to the datetime import at the top of the file:

```python
from datetime import date, datetime, timedelta
```

Pass it at the `_classify_free` call site:

```python
                rows.extend(_classify_free(fa, fb, clock_for(machine), machine,
                                           masters, config, op_busy,
                                           down_by_machine.get(machine)))
```

Add the bucket — in the `buckets` dict literal add `"maintenance": 0.0,` and in the
state tally add:

```python
            elif r["State"] == "MAINTENANCE (machine down)":
                buckets["maintenance"] += r["Hours"]
```

and in the `summary.append({...})` dict, after `"Idle: capacity free (days)"`:

```python
            "Maintenance (days)": days["maintenance"],
```

- [ ] **Step 5: Name it in the Why summary**

In `_why_summary`, after the `idle` clause:

```python
    if buckets.get("maintenance", 0) > 0:
        parts.append(f"{buckets['maintenance']}d machine out for maintenance")
```

- [ ] **Step 6: Add the column and colour, and pass the rows from the endpoint**

In `api/main.py`:

```python
    "IDLE (capacity free)": "FFC7CE",    # red — machine AND operator free, work waiting
    "MAINTENANCE (machine down)": "D9C2E9",  # purple — the machine was out of service
}
```

```python
                       "Waiting: crew (days)", "Outsourced (days)",
                       "Idle: capacity free (days)", "Maintenance (days)", "Why"]
```

and in `delay_report_xlsx`:

```python
    report = _dr.build_delay_report(plan_run.schedule, so_lines,
                                    plan_run.batches_prioritized, cfg, masters,
                                    book_store.load_machine_downtime())
```

- [ ] **Step 7: Run the tests**

Run: `python3.12 -m pytest tests/test_machine_downtime_reports.py tests/test_delay_report.py tests/test_delay_report_attribution.py tests/test_delay_report_api.py tests/test_plan_consistency.py -v`
Expected: PASS — especially `test_every_hour_is_still_accounted_for` and the two `sheetnames` assertions.

Run: `python3.12 -m pytest -q`
Expected: `959 passed, 2 skipped`

- [ ] **Step 8: Commit**

```bash
git add engine/delay_report.py api/main.py tests/test_machine_downtime_reports.py
git commit -m "feat(maintenance): the delay report names maintenance, not the crew"
```

---

### Task 10: The Settings panel

**Files:**
- Modify: `web/index.html` (after the Operator absences card, ~line 260)
- Modify: `web/app.js` (after the absence block, ~line 1990; plus boot wiring, ~line 2310)
- Test: `tests/test_machine_downtime_api.py` (append — markup assertions, the pattern `test_role_parity.py` already uses)

**Interfaces:**
- Consumes: `GET/POST/DELETE /machine-downtime` (Task 7).
- Produces: no Python names.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_machine_downtime_api.py`:

```python
# --------------------------------------------------------------------------- #
# The Settings panel (markup + role gating, checked the way test_role_parity does)
# --------------------------------------------------------------------------- #
import pathlib
import re

WEB = pathlib.Path(__file__).resolve().parent.parent / "web"


def test_the_settings_panel_exists_with_the_expected_controls():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    for el_id in ("downtime-machine", "downtime-from", "downtime-to",
                  "downtime-reason", "downtime-add", "downtime-list"):
        assert f'id="{el_id}"' in html, el_id
    assert "Machine maintenance" in html


def test_the_add_row_is_admin_only_but_the_list_is_not():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    block = html.split("Machine maintenance", 1)[1].split("</div>\n\n", 1)[0]
    add = block.split('id="downtime-add"')[0]
    assert "admin-only" in add, "the add controls must be admin-only"
    lst = block.split('id="downtime-list"')[0].rsplit("<ul", 1)[-1]
    assert "admin-only" not in lst, "the list itself must be visible to both roles"


def test_the_client_calls_the_right_endpoints():
    js = (WEB / "app.js").read_text(encoding="utf-8")
    assert 'fetch("/machine-downtime"' in js
    assert re.search(r'fetch\(`/machine-downtime/\$\{[^}]+\}`', js)
    assert "loadMachineDowntime" in js and "addMachineDowntime" in js
    assert "removeMachineDowntime" in js
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3.12 -m pytest tests/test_machine_downtime_api.py -k "settings_panel or admin_only_but or right_endpoints" -v`
Expected: FAIL — `assert 'id="downtime-machine"' in html`

- [ ] **Step 3: Add the card**

In `web/index.html`, immediately after the closing `</div>` of the **Operator absences** card:

```html
    <div class="card">
      <div class="card-head">
        <h2>Machine maintenance</h2>
        <p class="explainer">Mark a machine out of service for a date range, for example a
          service or a repair. The plan runs nothing on it for those days, on either shift,
          and moves the work elsewhere if the item's process master allows another machine.</p>
      </div>
      <div class="cfg-row admin-only">
        <select id="downtime-machine"></select>
        <input type="date" id="downtime-from" />
        <input type="date" id="downtime-to" />
        <input type="text" id="downtime-reason" placeholder="Reason (optional)" maxlength="200" />
        <button id="downtime-add" class="ghost-btn" type="button">Mark out of service</button>
      </div>
      <ul id="downtime-list" class="absence-list"></ul>
    </div>
```

- [ ] **Step 4: Add the client code**

In `web/app.js`, after `removeAbsence` (~line 1990):

```javascript
// ---- Machine maintenance (Settings-area block; the list is visible to both
// roles, add/remove controls are admin-only via CSS and the server 403s them). ----
function renderMachineDowntimeList(rows) {
  const ul = $("downtime-list");
  if (!ul) return;
  if (!rows || rows.length === 0) {
    ul.innerHTML = '<li class="muted">No machines are marked out of service.</li>';
    return;
  }
  ul.innerHTML = rows.map((d) => `
    <li>
      <span>${escapeHtml(d.machine)}: ${isoToDdmmyyyy(d.from_date)} to ${isoToDdmmyyyy(d.to_date)}`
      + `${d.reason ? " — " + escapeHtml(d.reason) : ""}</span>
      <button type="button" class="ghost-btn downtime-remove admin-only" data-id="${escapeHtml(d.id)}">✕</button>
    </li>`).join("");
  ul.querySelectorAll(".downtime-remove").forEach((btn) => {
    btn.onclick = () => removeMachineDowntime(btn.dataset.id);
  });
}

async function loadMachineDowntime() {
  try {
    const res = await fetch("/machine-downtime");
    if (!res.ok) return;
    const data = await res.json();
    const sel = $("downtime-machine");
    if (sel) {
      const prev = sel.value;
      // Grouped by machine type, the same shape the operator machine picker uses.
      const groups = new Map();
      (data.machines || []).forEach((mo) => {
        const label = mo.provisional ? "Not in Machine master yet" : (mo.type || "Other");
        if (!groups.has(label)) groups.set(label, []);
        groups.get(label).push(mo);
      });
      sel.innerHTML = Array.from(groups).map(([label, rows]) =>
        `<optgroup label="${escapeHtml(label)}">`
        + rows.map((mo) => `<option value="${escapeHtml(mo.id)}">${escapeHtml(mo.name)}</option>`).join("")
        + `</optgroup>`).join("");
      if (prev) sel.value = prev;
    }
    renderMachineDowntimeList(data.downtime || []);
  } catch (e) { /* a convenience view — a fetch hiccup shouldn't block the page */ }
}

async function addMachineDowntime() {
  const mac = $("downtime-machine"), from = $("downtime-from"),
        to = $("downtime-to"), why = $("downtime-reason");
  if (!mac || !mac.value) { setStatus("Pick a machine to mark out of service.", true); return; }
  if (!from.value || !to.value) { setStatus("Pick both maintenance dates.", true); return; }
  if (new Date(to.value) < new Date(from.value)) {
    setStatus("'To' date must be on or after 'From' date.", true);
    return;
  }
  try {
    const res = await fetch("/machine-downtime", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ machine: mac.value, from_date: from.value,
                             to_date: to.value, reason: why ? why.value : "" }),
    });
    if (!res.ok) { setStatus("Could not mark it out of service: " + (await res.text()), true); return; }
    setStatus(`${mac.value} marked out of service.`);
    from.value = ""; to.value = ""; if (why) why.value = "";
    await loadMachineDowntime();
    await runPlan(false);
  } catch (e) { setStatus("Maintenance error: " + e.message, true); }
}

async function removeMachineDowntime(id) {
  if (!window.confirm("Remove this maintenance break? The machine will be planned as available again.")) return;
  try {
    const res = await fetch(`/machine-downtime/${encodeURIComponent(id)}`, { method: "DELETE" });
    if (!res.ok) { setStatus("Could not remove it: " + (await res.text()), true); return; }
    setStatus("Maintenance break removed.");
    await loadMachineDowntime();
    await runPlan(false);
  } catch (e) { setStatus("Maintenance error: " + e.message, true); }
}
```

- [ ] **Step 5: Wire the button and the boot load**

In `web/app.js`, beside the `_absAdd` wiring (~line 2300):

```javascript
// Machine maintenance: add is admin-only (the row is CSS-hidden for the user role;
// the handler is harmless to wire either way — the server enforces the role).
const _downAdd = $("downtime-add");
if (_downAdd) _downAdd.onclick = addMachineDowntime;
```

and in `boot()`, beside `loadAbsences();`:

```javascript
  loadMachineDowntime();
```

- [ ] **Step 6: Run the tests**

Run: `python3.12 -m pytest tests/test_machine_downtime_api.py -v`
Expected: PASS

Run: `python3.12 -m pytest -q`
Expected: `962 passed, 2 skipped`

- [ ] **Step 7: Commit**

```bash
git add web/index.html web/app.js tests/test_machine_downtime_api.py
git commit -m "feat(maintenance): add the Machine maintenance panel to Settings"
```

---

### Task 11: Prove it — mutation testing, the real books, and the browser

**Files:**
- Create: `/private/tmp/claude-501/.../scratchpad/downtime_audit.py` (throwaway harness, **never committed**)
- Modify: none (unless a defect is found, in which case fix it and re-run)

**Interfaces:** none — this task produces evidence, not code.

- [ ] **Step 1: Mutation-test every part**

For each change below, revert it, run the named suite, record whether **at least one
test fails**, then restore it:

| # | Mutation | Suite to run |
|---|---|---|
| 1 | `iter_windows`: `is_machine_available` → `is_working_day` | `tests/test_machine_downtime_engine.py` |
| 2 | `_with_unavailability`: send every key to the operator branch | `tests/test_machine_downtime_engine.py` |
| 3 | `_ppc_frozen`: delete the release block | `tests/test_machine_downtime_freeze.py` |
| 4 | `freeze.py`: drop `prev_end` | `tests/test_machine_downtime_freeze.py` |
| 5 | `book_signature`: drop the `downtimes` block | `tests/test_machine_downtime_api.py` |
| 6 | `_plan`: revert to `absence_reservations` only | `tests/test_machine_downtime_api.py` |
| 7 | `analytics.avail_hrs`: delete the subtraction loop | `tests/test_machine_downtime_reports.py` |
| 8 | `delay_report._classify_free`: put every window in `up` | `tests/test_machine_downtime_reports.py` |
| 9 | `_machining_machine_options`: drop the provisional fallback | `tests/test_machine_downtime_api.py` |

Record the result of each in the commit message. **Any mutation that fails NO test
must be reported honestly as belt-and-braces, not described as load-bearing.**
Mutation 9 is expected to fail nothing on the current workbooks (there are no
provisional machines) — say so rather than inventing coverage.

- [ ] **Step 2: Write the real-book harness**

Create the scratchpad script:

```python
"""Re-runnable audit: does a maintenance break behave on the OWNER'S books?

For each workbook x WIP level x (no break | CNC3 down 3 days), plan and check:
  * no operation is scheduled on a machine that is out of service   [NEW]
  * routing_order_violations == []
  * qualification_violations == []
  * batch_quantity_violations == []
  * with NO break on file the plan is unchanged, order for order
and report makespan / late-days so the cost is visible, not hidden.
"""
import io, os, sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["STORE_DIR"] = "/tmp/downtime_audit_store"
os.environ["DEFAULT_SCHEDULER"] = "new"

from engine import book_store, loaders, new_engine, optimize_service, optimizer, orderbook
from engine.config import Config
from engine.models import PlanRun
from engine.pipeline import run_forward

BOOKS = ["Test5.xlsx", "Test8.xlsx", "Test9.xlsx"]
WIP = [0, 10, 30]


def plan(masters, lines, cfg, downtime):
    reserved = optimize_service.downtime_reservations(downtime) or None
    pr = PlanRun(so_lines=lines)
    run_forward(pr, cfg, masters, reserved=reserved)
    return pr


def down_days(rows):
    out = {}
    for d in rows:
        f = date.fromisoformat(d["from_date"]); t = date.fromisoformat(d["to_date"])
        cur = f
        while cur <= t:
            out.setdefault(d["machine"], set()).add(cur)
            cur += timedelta(days=1)
    return out


for book in BOOKS:
    if not os.path.exists(book):
        print(f"{book}: missing, skipped"); continue
    so_lines, masters = loaders.load_all(book)
    book_store.save_masters_bytes(open(book, "rb").read())
    new_engine._MASTERS_CACHE.clear()
    cfg = Config(plan_start_date=date(2026, 9, 7), scheduler="new",
                 apply_operator_logic=True, overlap_mode="overlap",
                 overlap_percent=88, consolidation_window_days=1)
    lines = [l for l in so_lines]
    for wip in WIP:
        for label, rows in (("clean", []),
                            ("CNC3 down 3d", [{"machine": "CNC3",
                                               "from_date": "2026-09-09",
                                               "to_date": "2026-09-11"}])):
            pr = plan(masters, lines[:len(lines) - wip] or lines, cfg, rows)
            dd = down_days(rows)
            clash = [e for e in pr.schedule
                     if e.machine in dd
                     and any(d in dd[e.machine]
                             for d in {e.start.date(), e.end.date()})]
            m = optimizer.plan_metrics(pr.schedule, lines[:len(lines) - wip] or lines,
                                       cfg.plan_start_date)
            print(f"{book:12} wip={wip:<3} {label:14} "
                  f"on-down-machine={len(clash):<3} "
                  f"routing={len(new_engine.routing_order_violations(pr.schedule, masters)):<3} "
                  f"batchqty={len(new_engine.batch_quantity_violations(pr.schedule, pr.batches_prioritized)):<3} "
                  f"makespan={m['makespan_days']:<7} late-days={m['total_late_days']}")
```

- [ ] **Step 3: Run it and read the output honestly**

Run: `python3.12 <scratchpad>/downtime_audit.py`

Required: **`on-down-machine=0` on every line**, and every violation count 0.
The `clean` rows must match the pre-feature numbers exactly.
The `CNC3 down 3d` rows will show **higher late-days** — that is capacity genuinely
removed. Record the before/after figures; do not present the rise as a defect, and do
not hide it.

- [ ] **Step 4: Browser pass, both roles**

```bash
DEFAULT_SCHEDULER=new STORE_DIR=/tmp/downtime_ui_store \
  python3.12 -m uvicorn api.main:app --port 877
```

Drive it with the browser tool, as a real user:
1. Log in as **admin**, upload `Test9.xlsx`, wait for the plan.
2. Settings → Machine maintenance. Confirm the dropdown lists exactly CNC1, CNC3-7, VMC1-3 and **no** manual/inspection stations.
3. Mark **CNC3** out of service for 3 days. Screenshot the Gantt before and after; confirm CNC3's bars move off those days.
4. Analytics: CNC3's **Available (hrs)** has dropped.
5. Download the delay justification xlsx; confirm it opens, has **two** sheets, and carries a `Maintenance (days)` column.
6. Remove the break; confirm the Gantt returns.
7. Log in as **user**: the list is visible, the add row and ✕ are **not**.
8. Check the browser console is clean throughout.

Record what was actually observed. If a step could not be completed, say so — do not
report a step as passed that was not run.

- [ ] **Step 5: Full suite + golden trace**

Run: `python3.12 -m pytest -q`
Expected: `962 passed, 2 skipped`

Run: `python3.12 -m pytest -k golden -v`
Expected: PASS — the golden trace is byte-identical (it runs the classic engine on a
book with no breaks).

- [ ] **Step 6: Commit the evidence**

```bash
git commit --allow-empty -F - <<'MSG'
test(maintenance): mutation results and real-book audit

Mutation testing (each part reverted individually):
  <fill in the 9 results, naming any that failed no test>

Real books (Test5/8/9 x wip 0/10/30, with and without a 3-day CNC3 break):
  operations on an out-of-service machine: 0
  routing / qualification / batch-qty violations: 0
  with no break on file: plan unchanged
  honest cost with the break: <before> -> <after> late-days

Browser: <what was actually driven, both roles>
MSG
```

---

### Task 12: Documentation

**Files:**
- Modify: `CLAUDE.md` (banner entry at the top, plus the stale test count)

- [ ] **Step 1: Add the banner entry**

At the top of `CLAUDE.md`'s CURRENT STATE list, above the Daily Entry entry, add an
entry following the established shape: what the owner asked for, the one load-bearing
decision, what was measured, what is deliberately not built, and the rule learned.
It must state:
- downtime is enforced in ONE place (`iter_windows`), covering the main loop and both frozen paths;
- the freeze release is evaluated at PLAN time, and why (the staleness hole);
- `SCHEDULER_FINGERPRINT` is v6;
- `parse_payload` is now an 8-tuple and `ContestSetup.absence_reserved` is now `unavailable_reserved`;
- the picker is CNC/VMC (by machine kind) but the engine honours any machine;
- the mutation-test results, including anything that proved to be belt-and-braces;
- **Rule: an operation needs a machine AND a person. Anything that can make a PERSON unavailable needs the mirror for the MACHINE — in the same dict, through the same gate.**

- [ ] **Step 2: Fix the stale test count**

`CLAUDE.md` ends with "Tests: `pytest` (508 passing)". Replace with the real number
from Step 5 of Task 11, and note the python3.12 requirement:

```
Tests: `python3.12 -m pytest` (960 passing, 2 skipped). NOTE: the system `python3`
is 3.14, where the installed openpyxl crashes on import (`numpy.short` was removed) —
always use python3.12.
```

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record the machine maintenance feature"
```

---

## Self-Review

**Spec coverage** — every section maps to a task:

| Spec section | Task |
|---|---|
| §1 Store | 1 |
| §2 Engine input / `reserved` | 1, 6 |
| §3 `ppc_engine` edits | 2 |
| §4 `new_engine` split + fingerprint | 3 |
| §5 Freeze release at plan time | 4 |
| §6 Signature / payload / contest | 5, 6 |
| §7a Analytics | 8 |
| §7b Delay report | 9 |
| §7c Report orphan row | 7 |
| §7d Shift-wise/Gantt (no change) | — verified in the spec, nothing to do |
| §8 API + picker filter | 7 |
| §9 UI | 10 |
| Test plan: 5 new files | 1, 2, 4, 5, 8 |
| Test plan: 4 rebases + role-parity extension | 5, 7 |
| Test plan: mutation / real books / browser / no-break guarantee | 11 |
| Risks + docs | 12 |

**Placeholder scan:** clean — every code step carries real code; the only free-form
outputs are Task 11's recorded measurements and Task 12's banner prose, both of which
are deliberately findings rather than pre-written text.

**Type consistency:** `_with_unavailability` (Tasks 3, 4), `unavailable_reserved`
(Tasks 5, 6), `downtime_reservations` (Tasks 1, 5, 6, 11), `machine_downtime` as the
payload/`prepare_contest` keyword (Tasks 5, 6), `downtime` as the reporting keyword
(Tasks 8, 9), `prev_end` (Task 4) — each name is used identically wherever it appears.
