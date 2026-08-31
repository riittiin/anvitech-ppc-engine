# Machine maintenance: a machine out of service for a date range

**Date:** 2026-08-31
**Status:** design approved by the owner, not yet implemented
**Owner request:** CNC and VMC machines go down for maintenance for whole days at a
time. The software has no way to say so, and the plan keeps scheduling work onto a
machine that is in pieces.

---

## The problem, in the owner's words

> Machines like CNC1, CNC3, CNC4, CNC5, CNC6, CNC7, VMC1, VMC2 and VMC3 sometimes get
> maintenance breaks. That means they are out of service for a user-defined amount of
> time — 1 day, 3 days, 4 days, 7 days. Right now there is no such setting. That is
> causing problems when we plan things.

Today the only comparable feature is **operator absences**: mark a person away for a
date range and the plan gives their work to someone else. There is no equivalent for a
machine. So when CNC3 goes down for five days, the plan keeps loading CNC3, the floor
knows the schedule is wrong, and the schedule stops being the thing people follow.

This is not a reporting problem. It is a **capacity** problem: the plan must know the
machine is not there.

---

## Decisions taken (owner, 2026-08-31)

| Question | Decision | Why it was asked |
|---|---|---|
| How precise is a break? | **Whole days.** A From date and a To date, exactly like an operator absence. Both shifts of each day are blocked. | Half-day maintenance is expressible in principle but adds a second time model to the engine's window walker for a case the owner does not have. |
| Which machines can be marked down? | **CNC and VMC only** in the picker. | The owner's real cases. See "The picker is filtered, the engine is not" below — the restriction lives in one place and widens with a one-line change. |
| A job is half-finished on a machine that goes down — what happens to the rest? | **The pin comes off. The scheduler and the optimizer decide.** If the Item's Process Master allows another machine, the work may move there; if it does not, it waits. Whether moving is actually *worth* it is scored by the contest on the existing Allotted-vs-Allotted+Suggested machine-set dimension. | This is the one genuinely load-bearing behaviour question. The owner's exact words: *"the optimizer has to decide honestly — rerouting could be more costly than waiting."* |

---

## What the owner will see

A **Machine maintenance** card in Settings, directly under Operator absences:

```
Machine ▾  [CNC3]   From [05-09-2026]  To [09-09-2026]
Reason (optional) [Spindle service]        [ Mark out of service ]

  CNC3   05-09-2026 → 09-09-2026   Spindle service              ✕
  VMC1   12-09-2026 → 12-09-2026                                ✕
```

* The dropdown lists CNC and VMC machines from the uploaded **Machine master**,
  grouped by machine type. A CNC8 added to the Excel appears on its own, with no
  code change.
* Both roles see the list. Only an admin can add or remove (CSS **and** server-side).
* Adding or removing re-plans immediately, so the Gantt, Schedule, Orders, Analytics
  and the delay report all move at once.

Nothing else in the UI changes. There is no new nav tab, no new banner.

---

## What the plan does

**The machine runs nothing on those days — both shifts.** A break on date `D` blocks the
first shift anchored on `D` (08:00–19:00) *and* the second shift anchored on `D`
(19:00 `D` → 05:00 `D+1`). Everything else routes around it exactly as it routes
around a Thursday or a holiday.

For half-finished work, the pin is released — see **The freeze rule** below.

---

## Architecture

### The load-bearing decision: one window source, not a new code path

`ppc_engine.worktime.iter_windows(machine, start_from, calendar, config)` is the
**single** source of "when can this machine run?" for every placement path in the live
engine:

| Path | Call site |
|---|---|
| main decode loop | `ppc_engine/scheduler/flow_scheduler.py::_lay_on_machine` (line 334) |
| frozen op, first lay | `…::_lay_frozen` (line 391) |
| frozen pre-placement | `…::_preplace_frozen` → `_lay_frozen` |

So machine downtime is enforced by **narrowing that one function**, not by adding a
check to three places. This is the direct lesson of the 2026-08-09 routing-order bug,
where the frozen path carried its own copy of a rule and drifted from the main loop:
*any new code path that PLACES an operation must go through the shared gate.* Here we
are not adding a path at all.

`iter_windows` already receives the `Machine` object, so it can ask the calendar about
that machine by id.

### 1. Store — `engine/book_store.py`

```python
MACHINE_DOWNTIME_KEY = "anvitech:machine_downtime"   # kv: json list

load_machine_downtime() -> list
save_machine_downtime(row: dict) -> dict      # assigns the uuid
delete_machine_downtime(row_id: str) -> bool  # False on unknown id -> 404
```

Row shape: `{id, machine, from_date, to_date, reason}`, ISO dates. An exact mirror of
`load_absences` / `save_absence` / `delete_absence`, including returning `False` for an
unknown id.

### 2. Engine input — the existing `reserved` dict, no new plumbing

`reserved` is already a `{key: [(start, end), …]}` map that the retired engines look up
**by machine id as well as by operator name**:

* `engine/rules/rule6_allocate.py::_lay_segments` reads `reserved_intervals.get(machine)`
* `engine/flow_scheduler.py::_mac_next_window` reads `self.reserved.get(mid)`

So classic and flow honour machine downtime **with no change at all**, and the whole
existing chain — `run_forward` → `optimizer.optimize` → `sweep_optimize` → the contest →
the cloud payload — carries it without a new parameter at any of ~12 call sites.

New pure helper, sibling of `absence_reservations`:

```python
# engine/optimize_service.py
def downtime_reservations(rows):
    """Downtime rows -> machine reservations: the machine is 'busy' from 00:00 of
    from_date to 00:00 of the day AFTER to_date (inclusive)."""
```

Merged into the single `reserved` dict via the existing `merge_reservations`.

**Known, accepted ambiguity:** one dict keyed by both machine ids and operator names
means a machine id that is literally also a person's name would be read as both. Machine
ids are `CNC1`-shaped and operator names are people's names; this is pre-existing
semantics, not something this feature introduces.

### 3. The live engine — two surgical `ppc_engine` edits

**`ppc_engine/domain/calendar.py`** — one new field, one new method:

```python
machine_downtime: dict[str, frozenset[date]] = field(default_factory=dict)

def is_machine_available(self, machine_id: str, day: date) -> bool:
    """True if `machine_id` can run on `day`: the shop is open AND the machine is
    not out of service for maintenance. Mirror of is_operator_available."""
    if not self.is_working_day(day):
        return False
    return day not in self.machine_downtime.get(machine_id, frozenset())
```

Verified safe: `ShopCalendar` is constructed in exactly **two** places repo-wide
(`ppc_engine/loaders/masters_loader.py:116` keyword, `tests/test_optimize_cancel.py:39`
bare), so a defaulted field breaks no constructor. `engine/new_engine.py:167` and
`tests/test_freeze_engine.py:172` both use keyword `dataclasses.replace`.

**`ppc_engine/worktime.py::iter_windows`** — one line:

```python
- if calendar.is_working_day(day):
+ if calendar.is_machine_available(machine.id, day):
```

`is_machine_available` includes `is_working_day`, so this is a strict narrowing:
with an empty `machine_downtime` the output is byte-identical.

`ppc_engine/` has been edited exactly once before — the 2026-08-07
operator-qualification fix, which touched three files. These two edits are the second
such occasion, and they are deliberate rather than accidental drift.

### 4. `engine/new_engine.py`

`_with_absences(masters, reserved)` becomes `_with_unavailability(masters, reserved)`
and splits the reserved keys:

* key **in `masters.machines`** → folded into `calendar.machine_downtime`
* everything else → folded into `calendar.leaves` (today's behaviour, unchanged)

Three internal call sites move together (`run`, `optimize_sequence`, `tune`). No test
names the function, but a missed call site is a `NameError` that fails every
new-engine test — which is the point of renaming rather than adding a second function.

**`SCHEDULER_FINGERPRINT` → `"new-engine-v6-machine-downtime"`.** Real work moves, so
saved optimizer ranks were scored under different semantics. Forgetting this replays
old ranks under new semantics behind a green banner.

### 5. The freeze rule (the owner's reroute decision)

**The rule, stated precisely:**

> A step is **not** pinned to machine `M` when `M` has a maintenance day anywhere in the
> date range `[plan start … the date that step was due to finish in the last applied
> plan]`, clamped so the range is never empty.

#### Where the decision is made, and why it matters

The decision belongs at **plan time**, in `engine/new_engine._ppc_frozen` — the one
place that turns stored pins into engine instructions — **not** in
`engine/freeze.compute_frozen_set`, which builds the stored list.

This was caught by the owner's question, *"what happens when CNC3 comes back after two
days?"*, and it is a genuine staleness hole, not a detail:

`compute_frozen_set` runs **only** on "Done entering — update plan"
(`api._compute_and_store_frozen`, called inside `_start_optimize`). Every ordinary
re-plan reads the stored list as-is (`_plan` → `book_store.load_frozen_ops()`). So had
the release lived there, marking CNC3 down would have produced:

* **immediately** (the re-plan the Settings panel triggers) — the stored pin still says
  CNC3, so the half-done job shows as **waiting for CNC3 to return**;
* **after the next Done** — the pin is finally rebuilt, drops, and the job may **move to
  CNC4**.

Two different answers to the same question, hours apart, with nothing on screen to
explain the change. That is precisely how a schedule loses the floor's trust.

Deciding at plan time cannot go stale: the immediate re-plan and every contest candidate
evaluate the identical rule against the identical inputs.

#### What this needs

`_ppc_frozen` already has everything except one date:

* the breaks — `masters.calendar.machine_downtime`, populated by
  `_with_unavailability` on the masters it is handed (verified in all three callers:
  `run`, `optimize_sequence`, `tune`);
* the machine list — it already filters on `mid not in masters.machines`;
* the plan start — passed in from the `PlanConfig` each caller already builds.

The missing date is **when that step was due to finish**. The applied plan records it
(`schedule_projection` writes `end`), it is simply not copied onto the frozen row today.
So `compute_frozen_set` copies one more field:

```python
"prev_start": row["start"],
"prev_end":   row["end"],     # new — the window the release rule is judged against
```

**No signature change**, so every positional caller is untouched:
`tests/test_frozen_batch_qty.py:72`, `tests/test_freeze_logic.py:49,72`,
`api/main.py:1411`. `book_signature` folds only
`(so_no, item_code, op_seq, machine, remaining_qty)`, so the extra field does not move
any hash. `tests/test_freeze_logic.py:52-54` asserts key **membership**, so an added key
is safe.

A frozen row stored before this deploy has no `prev_end`; it falls back to `prev_start`,
then to the plan start. Degrades to "judge the release on the plan-start day alone" —
never a crash, and self-corrects on the next Done.

#### What the rule buys

* **A break that overlaps the work → the pin drops.** The step returns to the normal
  pool, where `_place_operation` tries every machine the routing allows and keeps the one
  that **finishes soonest**, and the contest then scores the whole book. If the routing
  allows only the down machine, it waits. **Nothing is forced and no heuristic decides:**
  reroute-vs-wait is settled by the same objective that settles everything else.
* **A break scheduled well after that op's window → the pin stays.** Marking CNC3 down
  three months out does not unfreeze today's work on CNC3.
* **A step that later slips into a break anyway → it simply waits**, keeping its machine,
  its operator and its no-setup treatment, because `iter_windows` has already removed
  those days.

#### Two consequences of the pin dropping, both physically correct

1. **The step pays its 90-minute setup again.** A frozen op is laid as
   `remaining_qty × cycle` with no setup, on the grounds that the machine is already set
   up mid-run. After a maintenance break that is no longer true — the machine was
   stripped — so the setup is right, not a regression.
2. **The operator pin drops with it.** The step takes whoever is qualified and on shift
   when the work actually runs, rather than the person who was standing at a machine
   that no longer exists.

The **quantity** is unaffected: a released step is scheduled at
`Order.process_remaining[op_seq]` — the whole clubbed batch's remaining — which is the
batch-level number the 2026-08-11 rule requires, not the punched SO line's.

### 6. Cache, staleness, contests

**`optimize_service.book_signature(so_lines, absences=None, frozen=None, downtimes=None)`.**
Follows the `frozen` pattern exactly — appended to the hash blob **only when non-empty**,
so every existing call stays byte-identical and the guard at
`tests/test_freeze_contest.py:42-43` (`book_signature(lines) == book_signature(lines,
frozen=[])`) still holds for the new parameter too.

The appended element is a **tagged dict**, `{"machine_downtime": sorted(...)}`, not a
bare list. A bare list would make a downtime-only book structurally indistinguishable
from a frozen-only book.

`api._current_book_sig()` passes the downtime rows. That feeds:

* `_plan_fingerprint` → the plan cache self-invalidates when a break is added or removed;
* the auto-optimize "nothing material changed" skip → a Done click after a break is
  entered actually re-sequences.

Downtime goes into `book_signature`, **not** `_inputs_signature` — same as absences.
`_inputs_signature` means "the masters/settings an applied optimization was computed
on"; putting a break there would falsely flag the applied plan stale.

**Cloud payload.** `build_payload(..., machine_downtime=None)` adds one key;
`parse_payload` returns it. `prepare_contest(..., machine_downtime=None)` merges it into
the contest's reserved dict so every candidate plan — local, GitHub, Oracle — pins the
same breaks.

> ⚠ **Deliberate contract change.** `parse_payload` currently returns a 7-tuple and
> three tests pin that: `tests/test_absences_engine.py:67` (`len(result) == 7`),
> `tests/test_optimize_service.py:45` (7-name unpack), `tests/test_freeze_contest.py:26`
> (`parsed[-1] == frozen`). Making it an 8-tuple is a **rebase of those three
> assertions, not a fudge** — the alternative (reading the key off the raw payload
> inside `run_candidate`) leaves `parse_payload`'s docstring claiming it rebuilds "the
> exact objects the API planned with" while silently dropping one, which is precisely
> the class of bug this codebase keeps paying for. An older in-flight payload still
> parses: the key is read with `.get(...)` defaulting to `[]`.

**`ContestSetup.absence_reserved` → `unavailable_reserved`.** The field now holds
operator absences *and* machine downtime in one merged dict, so the old name would lie
— exactly the trap CLAUDE.md documents with `masters.operators` ("the name is
misleading — it is the Settings table, not the workbook sheet"). Nine references move
together: four in source (`engine/optimize_service.py:225,269,310`, `api/main.py:1607`,
`:2021`) and five in `tests/test_absences_engine.py:49,98,110,111,113`. All mechanical,
and a missed one is an immediate `AttributeError`.

**One merged field, not two.** The alternative — keeping `absence_reserved` and adding a
separate `machine_downtime_reserved` — would force all three consumers to remember to
merge, and forgetting one would silently drop downtime from a contest while every other
surface honoured it. That is the exact bug class this feature exists to avoid.

`tests/test_absences_engine.py:113` asserts `set(setup.unavailable_reserved) ==
{"Nobody-Else"}` — nothing else reserves time. That book has no maintenance breaks, so
it stays green **and** becomes a useful guard: it now proves downtime adds no key when
none is on file.

### 7. Reporting — no feature may attribute a cause it did not check

Five surfaces would otherwise describe a shop that does not exist.

**a. `engine/analytics.py`** — a down machine's **Available hrs** must drop, or a
machine that was in pieces reads as idle-and-available and the utilization number is a
lie. New appended kwarg `downtime=None`; `_down_days(...)` mirrors `_absent_days`.

For each down working day `D`, subtract the machine's own windows **anchored on `D`**,
via `WorkClock._windows_for_day(D)` — the same per-day window list `delay_report`
already uses. This is **exact**, not an approximation: a two-shift machine's night
window is anchored on `D` and runs to 05:00 on `D+1`, so anchoring the subtraction the
same way the engine anchors the block means the two agree to the minute. (Subtracting a
plain calendar day `[D 00:00, D+1 00:00)` would have mis-attributed those ≤5 hours; it
is not used.)

**b. `engine/delay_report.py`** — new state **`MAINTENANCE (machine down)`**. Without
it, hours on a down machine fall into `WAITING (crew)` or `IDLE (capacity free)` and
the report blames the crew for a machine being serviced — the exact defect the owner
caught on 2026-08-09. `_classify_free` splits its in-window `work` intervals into
down/up pieces: down pieces become MAINTENANCE, up pieces run the existing staffing
split. Off-hours is untouched.

New `Maintenance (days)` column appended to `_DELAY_SUMMARY_COLS`, a bucket in the
summary, a clause in `_why_summary`, and a fill colour in `_DELAY_FILLS`.

The **accounting invariant holds by construction**: MAINTENANCE *splits* an existing
bucket rather than adding time, so `RUNNING + OUTSOURCED + every wait == the order's
span` is unchanged. This is asserted, not assumed
(`tests/test_delay_report_attribution.py:190`, tolerance 1e-6).

Two constraints the new state must respect, both already asserted:
* it must not contain the substrings `"unattributed"` or `"(free)"`
  (`tests/test_delay_report.py:90-91`);
* the xlsx must stay **two sheets** (`wb.sheetnames == ["Summary", "Detail"]`, asserted
  at `tests/test_delay_report_api.py:36` and `:62`). A new column is fine; a new sheet
  is not.

**c. `api.main._report_for_book`** — one new non-blocking kind,
**`MACHINE_DOWNTIME_UNKNOWN`**: a break on a machine that is no longer in the Machine
master after a re-upload. Exact mirror of `ABSENT_OPERATOR_UNKNOWN` — ignored by
planning, never fatal, surfaced so it cannot rot silently.

> **Deliberately NOT added:** an informational "CNC3 is down" row. The data-gaps card
> is for *problems*; a scheduled maintenance break is not one, and putting it there
> would train the owner to ignore that banner. The Settings panel is where breaks
> live.

**d. Shift-wise export / machine-wise view / Gantt** — need no change. All three are
derived from the schedule, so a machine with no work on a down day simply has no rows.
Verified by reading, not assumed: `build_shiftwise_timeline`'s fast path trusts
`ScheduleEntry.op_segments` verbatim.

### 8. API — `api/main.py`

```
GET    /machine-downtime         any role  -> {downtime, orphans, machines}
POST   /machine-downtime         admin
DELETE /machine-downtime/{id}    admin
```

An exact mirror of `/absences`: dates must parse as `YYYY-MM-DD`, a reversed range is
**swapped rather than rejected**, an unknown id is a 404, no password re-auth
(non-destructive and reversible), and **no `_try_start_auto` call** — only the Done
button starts a contest.

`reason` is optional, stripped, capped at 200 characters.

**The picker is filtered, the engine is not.** `machines` in the GET response is the
Machine master filtered to **machining** stations, using
`ppc_engine.loaders.normalize.machine_kind_from_type` — the same function the live
engine uses to decide what a CNC/VMC is, so there is one definition and a CNC8 in the
Excel appears with no code change.

**Verified against the real books, not assumed.** On both `Test9.xlsx` and
`Test5.xlsx` the filter yields exactly:

```
['CNC1','CNC3','CNC4','CNC5','CNC6','CNC7','VMC1','VMC2','VMC3']
```

— precisely the nine machines the owner named, from the types `CNC lathe` and
`Vertical Machining center`. Nothing else in either master is MACHINING.

> **Note — CLAUDE.md is stale here.** Its "Known data quirks" section says routings
> reference `CNC6`, `CNC7`, `VMC3` that are "not yet in `Machine master`". That was true
> of the Test3/Test4-era workbooks; in **Test5 and Test9 all three are real Machine
> master rows** and there are **zero** provisional machines. Checked, not assumed.
> The filter still carries a provisional fallback — for a provisional machine, use the
> id prefix (`CNC…` / `VMC…`), mirroring
> `ppc_engine.loaders.masters_loader.register_provisional_machines` — because a future
> workbook can reintroduce one and the fallback costs two lines. Today it is a
> **no-op**, and the plan's test asserts that it is exercised anyway. The POST validates only that the machine **exists**
in the master, not its kind: the engine honours downtime on any machine, so if the
owner later wants a deburring bench markable, it is a one-line change to the filter and
nothing in the engine has to be revisited. This is the same forgiving-master principle
the loader uses everywhere.

### 9. UI — `web/index.html`, `web/app.js`, `web/style.css`

A Settings card mirroring Operator absences: machine `<select>` (`<optgroup>` by type),
From/To dates, optional reason, "Mark out of service", and a list with ✕ per row.
`.admin-only` on the add row and the ✕. Add/remove → `loadMachineDowntime()` then
`runPlan(false)`.

The role gate is CSS **plus** the server 403. Nothing here is built at runtime in JS, so
the `.admin-only` rule reaches it — unlike the Optimize Apply/Discard buttons, which
needed a JS check (2026-08-09).

---

## Deliberately NOT built

* **No partial-day breaks.** Owner's call; see Decisions.
* **No informational "machine is down" row in the data-gaps banner.** See 7c.
* **No new nav tab.** The panel lives in Settings, so
  `tests/test_role_parity.py::test_no_tab_is_hidden_from_the_user_role` stays green by
  construction.
* **No new contest dimension.** Downtime is a constraint, not a knob to search.
  `contest_jobs` stays exactly `(overlap × machine-set)`, so
  `tests/test_optimize_shard.py`'s exact-ordering assertions are untouched.
* **No automatic re-optimize when a break is entered.** Same as absences: the plan
  changes immediately, and the next "Done entering — update plan" re-sequences around it.
* **No history/audit of past breaks.** Rows are deleted outright, like absences.

---

## Test plan

### New files

**`tests/test_machine_downtime_engine.py`** — the pure engine.
* `iter_windows` yields no window on a down day and resumes the next day.
* **Both shifts** of a down day are blocked (a two-shift CNC gets neither
  08:00–19:00 nor 19:00→05:00).
* Other machines on the same day are untouched.
* An empty `machine_downtime` is **byte-identical** to today (the whole-plan guard).
* `is_machine_available` returns False on a Thursday/holiday regardless of downtime.
* **Plus the missing coverage this feature exposes:** there is currently **no direct
  unit test of `ppc_engine.worktime.iter_windows` anywhere in the suite.**
  `tests/test_worktime.py` looks like it covers day-skipping but tests the *classic*
  `WorkClock`, a different module. This file adds the first direct one.

**`tests/test_machine_downtime_freeze.py`** — the reroute rule.
* A break overlapping the op's window drops the pin; the step is rescheduled.
* A break entirely after the op's window leaves the pin in place.
* With an alternative machine in the routing, the work can move there.
* With no alternative, it waits — and never lands on the down machine.
* A released step is scheduled at the **whole batch's** remaining, not the punched SO
  line's (guards the 2026-08-11 rule through the new path).
* **The staleness guard — the test this design exists for:** add a break and re-plan
  **without** pressing Done, so `book_store.load_frozen_ops()` still holds the stale
  pin. The plan must already reflect the release. This test fails against the
  build-time version of the rule and is the reason the decision moved to plan time.
* A frozen row with **no `prev_end`** (stored before this deploy) is handled without
  crashing.

**`tests/test_machine_downtime_api.py`** — endpoints, roles, validation, wiring.
Modelled on `tests/test_absences_api.py`, including its
`test_post_and_delete_do_not_start_a_contest` guard.
* GET shape `{downtime, orphans, machines}`; `machines` contains CNC/VMC and excludes
  manual/inspection stations.
* POST/DELETE are 403 for the user role; GET is 200 for both.
* Bad dates → 400; reversed range → swapped; unknown machine → 400; unknown id → 404.
* Orphan row appears as `MACHINE_DOWNTIME_UNKNOWN` after the machine leaves the master.
* **Adding a break invalidates the plan cache** (mirrors
  `tests/test_plan_cache.py::test_absence_invalidates`).
* `book_signature` moves when a break is added and is byte-identical for
  `downtimes=None` vs `downtimes=[]`.
* Cloud payload round-trips the breaks.

**`tests/test_machine_downtime_reports.py`** — the three reporting surfaces.
* Analytics Available hrs drop by exactly the down days; `downtime=None` is
  byte-identical to omitting it (mirrors `test_absences_default_none_is_byte_identical`).
* A machine down for the whole window reads 0 available, not a fake 100%.
* The delay report emits `MAINTENANCE (machine down)` instead of blaming the crew.
* The delay report's span-accounting invariant still closes.

### Existing tests to extend or rebase (all deliberate, all listed up front)

| File | Change | Why |
|---|---|---|
| `tests/test_role_parity.py:234-245` | **add** `POST /machine-downtime` to the write-endpoint list | the "role gating belongs on the control" invariant should cover the new endpoint |
| `tests/test_absences_engine.py:49,98,110,111,113` | `absence_reserved` → `unavailable_reserved` | `ContestSetup` field rename (see §6) |
| `tests/test_absences_engine.py:66-68` | 7-tuple → 8 | `parse_payload` contract change |
| `tests/test_optimize_service.py:45` | 7-name unpack → 8 | same |
| `tests/test_freeze_contest.py:26` | `parsed[-1]` → `parsed[-2]` | same |

### Beyond unit tests

1. **Mutation testing — every part, measured not assumed.** Revert each piece
   individually (the `iter_windows` narrowing; the `_with_unavailability` machine split;
   the freeze release rule; each reporting change) and confirm **at least one test
   fails**. Anything that fails nothing gets **reported as belt-and-braces**, not
   presented as load-bearing. This is the check that was missing when the freeze
   feature shipped, and this fixture family is known to pass vacuously by default
   (2026-08-09).

2. **Measured on the real books.** Re-runnable harness over **Test5, Test8, Test9** at
   several WIP levels, with and without breaks on file, asserting:
   * `routing_order_violations == []`
   * `qualification_violations == []`
   * `batch_quantity_violations == []`
   * **new:** no scheduled segment falls on a machine on one of its down days
   * with no breaks on file: the plan is **unchanged**, order for order.

   Reported honestly: marking a CNC down removes real capacity, so late-days will
   **rise**. That is the truth arriving in the plan, not a regression — the same shape
   as the 2026-07-19 and 2026-08-09 findings, where the old numbers were impossible
   rather than better.

3. **Browser pass** (Playwright / the browse tool), driven as a real user against a
   throwaway local instance on a real workbook, **both roles**: add a break, watch the
   Gantt move; remove it, watch it move back; confirm the user role sees the list and
   cannot add; console clean; no 500s.

4. **The no-break guarantee.** With nothing marked down: the full suite green
   (**baseline: 901 passed, 2 skipped**) and the golden trace byte-identical. If the
   feature is unused, the software behaves exactly as it does today.

---

## Risks, stated plainly

* **Capacity really is removed.** Every day a CNC is marked down is a day of machining
  the shop does not have. Orders will finish later, and the delay report will say so.
  The risk is not that this is wrong — it is that the numbers look worse and someone
  reads that as a bug.
* **Dropping the freeze pin is a real behaviour change** for in-progress work, and it
  is the one part of this feature that can surprise the floor: a part-machined job may
  be re-planned onto a different machine. It happens only for machines the owner has
  explicitly marked down, only while the break overlaps that job's window, and only
  when the routing allows an alternative. This was the owner's explicit decision.
* **`SCHEDULER_FINGERPRINT` v6 flags every applied optimization stale on deploy.** The
  banner will ask for a fresh deep search once. That is correct and one-time, and is
  the same cost the 2026-08-11 v5 bump carried.
* **A break entered for a *past* date is accepted** (like an absence) and simply has no
  effect on a plan that starts today. Not worth a validation error.

---

## Files touched

| File | Change |
|---|---|
| `ppc_engine/domain/calendar.py` | + `machine_downtime` field, + `is_machine_available` |
| `ppc_engine/worktime.py` | `iter_windows` consults the machine, not just the shop |
| `engine/new_engine.py` | `_with_absences` → `_with_unavailability`; `_ppc_frozen` releases a pin on a machine that is down in the step's window; fingerprint v6 |
| `engine/freeze.py` | `compute_frozen_set` copies `prev_end` onto each row — **no signature change**, so every positional caller is untouched |
| `engine/book_store.py` | + store key and three accessors |
| `engine/optimize_service.py` | + `downtime_reservations`; `book_signature`, `build_payload`, `parse_payload`, `prepare_contest`, `ContestSetup` |
| `engine/analytics.py` | + `downtime` kwarg, `_down_days` |
| `engine/delay_report.py` | + `downtime` kwarg, `MAINTENANCE (machine down)` state + column |
| `api/main.py` | 3 endpoints, `_report_for_book`, `_current_book_sig`, `_plan`, delay-report/analytics call sites, `_DELAY_*` constants. **`_compute_and_store_frozen` is deliberately unchanged** — the release decision moved to plan time (§5) |
| `web/index.html` | + Machine maintenance card |
| `web/app.js` | + load/add/remove + render |
| `web/style.css` | (reuses `.absence-list` / `.cfg-row`; minimal additions) |
| `tests/` | 4 new files, 4 rebased/extended assertions |
| `CLAUDE.md` | banner entry, incl. correcting the stale "508 passing" to 901 |

**Untouched on purpose:** `engine/rules/`, `engine/flow_scheduler.py`,
`engine/optimizer.py`, `engine/gantt.py`, `engine/efficiency.py`,
`engine/orderbook.py`, `ppc_engine/scheduler/`, `ppc_engine/optimize/`.
