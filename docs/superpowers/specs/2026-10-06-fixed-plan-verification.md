# Fixed plan — verification record (2026-10-06)

Evidence behind the CLAUDE.md banner entry of the same date (spec
`2026-10-06-fixed-plan-design.md`, section 5). Every number below was produced by the
harnesses in `2026-10-06-fixed-plan-harness/`, so the next person re-runs them instead
of trusting this page.

## Inputs and method

- **Data: a read-only copy of the LIVE store, taken 2026-10-06** (owner's request: live
  data, not Test5/8/9). 67 active orders, 1,130 punches, 346 rows in the old
  `last_applied_schedule`, 6 frozen ops, 0 machine breaks, 5 absences on file, saved
  config overlap 86, flexible machines on, operator logic on. No credential is in the
  harness; nothing was written back; MONGODB_URI / Upstash / cloud-optimize variables
  are removed before `api.main` is imported. Every phase copies the store (or a snapshot
  from an earlier phase) into a scratch directory and points `STORE_DIR` there.
- **Through the real API**: FastAPI `TestClient` against `api.main`, logged in as admin
  and as user (throwaway `ADMIN_/USER_USERNAME/PASSWORD` set by the harness; `api/auth.py`
  honours them). `/run`, `/gantt`, `/delay-report.xlsx`, `/actuals`, `/optimize/done`,
  `/optimize/status`, `/optimize/apply`, `/machine-downtime`, `/new-orders/*`. Entry-level
  checks read the schedule from `_PLAN_CACHE["artifacts"]`, the same object the `/run`
  response was built from.
- **Simulated days**: `api.main._ist_now` is monkeypatched to a controlled clock
  (`_ist_today` derives from it). Day k = the k-th working day from 06-10-2026. Clock at
  D 23:00; every op the CURRENT plan runs in [D 08:00, D+1 08:00) is punched through
  `POST /actuals` (whole remaining qty when the op ends inside the day, else
  floor(qty x share of its working minutes)), split over the batch's SO lines, clipped to
  the precedence cap, operator = the plan's person. On day 3 and day 7 one CNC/VMC op
  that ends that day gets 50 % (shortfall) and another that continues gets its full
  remaining qty (early finish). Then `POST /optimize/done` (alternating user / admin),
  then `/run` and every check.
- **Outsourced steps, two variants.** An OS step is a flat 24x7 block whose length does
  not depend on qty, and the engine re-lays it IN FULL from the next plan start until
  its parts are punched. Variant A punches it when the block ends (parts back); variant
  B (`--os-on-send`) when it starts (parts sent). This one choice dominates the late-day
  growth (below), so both are reported.
- Optimize: `_start_optimize(15, "deep", background=False)` (local, budget 15; the
  endpoint itself always asks for 1,000), then `GET /optimize/status` and
  `POST /optimize/apply` through HTTP.

Commands (repo root, python3.12):

```
DEFAULT_SCHEDULER=new python3.12 -B -u docs/superpowers/specs/2026-10-06-fixed-plan-harness/verify_fixed_plan.py <STORE_COPY> out.json            # variant A
DEFAULT_SCHEDULER=new python3.12 -B -u docs/superpowers/specs/2026-10-06-fixed-plan-harness/verify_fixed_plan.py <STORE_COPY> out.json --os-on-send   # variant B
python3.12 -B -u docs/superpowers/specs/2026-10-06-fixed-plan-harness/mutation_sweep.py            # [--full] [name filter ...]
```

Run each harness from a `git archive HEAD` export when a mutation sweep is running in
the worktree: the sweep edits source files, and a harness that imports a mutated file
measures the mutation, not the code.

## Task 9: the owner's amendment (spec section 8), re-run 2026-10-07

Same live copy, same harness (fresh copy per run), code at `cfc7b48` (after the Task 9
review fix rounds 1 and 2). **Variant A 119 of 119, variant B 119 of 119.**

| Late-days | A before | A after | B before | B after |
|---|---|---|---|---|
| (a) free plan, applied ranks, before the feature | 298 | 298 | 298 | 298 |
| (b) day 0, published then repaired | 308 | **298** | 308 | **298** |
| (c) day 10, repaired every day | 918 | **603** | 508 | **493** |
| (c') day 10, free re-plan on the same punches | 689 | 604 | 504 | 498 |
| (d) day 10, Optimize (budget 15), after Apply | 678 | 599 | 495 | 494 |

- **The rule as built** (`flow_scheduler._late`, `_pick`). Jobs are placed in the
  published (placement) order. A job is LATE when its order reaches it more than a
  minute after its published start. A late job gives way on its own machine only: if a
  job queued there can start before it is ready, the machine takes the next job in
  published order that is READY when the machine can next start one; an
  earlier-published job of that machine ready by the time the picked job would really
  start goes before it; if the picked job cannot start before the late one could, the
  late one goes. Because a job is only known to be ready once its feeding steps are
  placed, when a job ahead on that machine still waits for them, the next job in
  published order on another machine is placed first, unless that would put it in
  front of an unplaced earlier step of its own machine's queue; then there is no
  give-way and the late job keeps its place (published order, as if it were on time).
- **What is checked, and holds on both variants every day:** 0 ops changed machine;
  0 on-time ready jobs finding their machine held by a later-published job, except
  that machine's give-way to a late job (`held_audit`, new in fix round 2; it flags the
  re-reviewer's probe on the earlier code and passes on the final code); 0 pairs where
  an earlier job was ready and a later one took its slot. Other out-of-order pairs, A /
  B over the run: earlier job not ready 695 / 287, frozen 59 / 51, placed first by the
  published plan 2 / 6, earlier job ready but the later one only used time it could not
  use (`ready_hole`) 5 / 0. The audit reads a step by when it started (a manual step can
  run past the end of a shift) and matches a step laid around other jobs part by part.
- The +229 gap of variant A (c vs c') is gone; the repaired plan is within a few
  late-days of a free re-plan on both variants. The go-live repair reproduces the
  published plan exactly (0 of 67 orders moved; before, 19 moved up to 2 days).
- **How the rule got here.** Running this page and two reviews found, each pinned by a
  test that failed first: sub-second "late" (published times are stored to the second;
  now a one-minute tolerance); a pick judged before the piece-flow guard; a give-way
  reordering another machine (twice: the first version took any job in the shop, a
  later "feed it first" step took any feeding step); a not-ready job beating a ready
  one; a ready job jumped because its feeding steps were not placed yet (traced on the
  live day-5/6 states: CNC7, DTC2, CMM, and CNC7 behind a late VMC2 job in variant B);
  ties in time; and the re-reviewer's theoretical case (a picked job that cannot start
  before the late job could), which exists and is fixed.
- **D12**: variant A's OS blocks no longer slide a day per day once the step before
  them is punched complete; most of the A (c) drop.
- **Plan time (live copy, median of 5):** repair 184 ms before Task 9, 794 ms after
  (the repair now evaluates every order's next step instead of each machine's queue
  head); the free plan is about 1.1 s either way.
- Base `035fe86` vs HEAD with nothing published (section 9's harness): **12 of 12
  identical**, on books where no step before an OS step is complete (see section 9:
  D12 does change free and Optimize plans once one is).
- Mutation sweep (`mutation_sweep.py`, full, on the final code, run from a copy of the
  tree): **71 mutations, 64 load-bearing**. Not load-bearing: the two `_pin_queues`
  skips (a finished step, a frozen step; there to keep a head-of-queue from
  deadlocking, which no longer exists); `T5 scheduler guard` (bites only in the full
  suite, `test_optimize_lanes_feedback`, as before); `T6 date list from the FREE
  candidate` (mutates `_candidate_dates`, which has no product caller left); and three
  Task 9 belt-and-braces lines: judging the give-way set on guarded starts (A6, the
  later checks now catch the same case), excluding the late machine's own jobs from the
  "elsewhere" step (R3b), and the late-job exception inside `_jumps_its_machine` (R3i).
  `T5 new-order append through _publish` deadlocks (the publish lock is not
  re-entrant); it was stopped and counts as caught.

The sections below are the Task 8 / fix-wave record and still describe the strict turn.

## Results

**After the final review fix wave (re-run on the same live copy): variant A 102 of
102, variant B 102 of 102.** (Before the fix wave: 91 of 91 and 90 of 91; the one
failure is described in section 6 and is now fixed.) Full suite on the fix-wave code:
1366 passed, 4 skipped, 1 deselected (the known xlsxwriter env failure,
`test_monthly_report_json_and_excel`). Late-day numbers in sections 1 to 5 are
unchanged by the fix wave (identical on both runs).

### 1. Go-live (the published plan is seeded on the first `/run`)

366 published rows (all carry `placed` and `staff`). The plan the floor saw before this
feature (free plan, applied ranks): **298 late-days**; the same plan published and
repaired: **308**. 19 of 67 orders moved, the largest by 2 days. Exactly the Task 6b
measurement, reproduced through the API.

### 2. Stability over 10 simulated days (spec 5.3) — every day, both variants

- **0 ops changed machine** against the day-0 published plan; 0 unpublished ops.
- **0 turn inversions among queued jobs.** From day 2 on, 4 inversions per plan (both
  variants) involve two HALF-FINISHED jobs on one machine: e.g. DTC2, order X step 4 (published
  turn 0) now runs after order Y step 4 (turn 3) because order X's own earlier frozen steps
  must finish first (the 2026-08-09 routing-wins rule for frozen ops). Both are 1-piece
  remainders left by fractional quantities in the live punches. This is the "floor's
  punch makes it started" case of D6, not the software reordering a queue.
- **Calendar (5.1): 0** segments in a meal break, on the weekly off or a holiday, on a
  down machine, outside 08:00-05:00, or at night on a single-shift machine.
- **People (5.2): 0** double bookings, 0 people booked while absent, 0
  `OPERATOR_NOT_QUALIFIED`.
- **Invariants (5.6): 0** `ROUTING_ORDER_VIOLATION`, 0 `BATCH_QTY_SHORT`.
- **One set of dates (5.6): 0** disagreements between the Orders tab (`expected_end`),
  the Gantt, the shift-wise download and the delay report (read from the user role's
  `/delay-report.xlsx`), 67 of 67 orders every day.
- `Done` never started a search (state stays idle, reason `repaired`), and its note
  names who pressed it and says only times changed.

### 3. Idempotence (spec 5.4)

Done twice with no punch: identical plan (hash of machine, operator, start, end, qty,
segments of every entry) on day 0 and on day 5, both variants; also identical to the
first `/run` of the day. Exception: the first Done right after an Optimize is applied,
section 6.

### 4. Dates move only downstream of what changed (spec 5.5)

Per day: orders whose date moved, minus the downstream closure (later on the same
machine, later steps of the same batch, transitively) of (a) every op whose punch
differed from the plan (shortfall, early finish, or the precedence cap clipped it),
(b) OS blocks still at the vendor (they are re-laid one day later every day), (c) ops
whose day share was fractional (whole pieces only).

| Variant | moved orders per day | unexplained by (a) alone | unexplained by (a)+(b)+(c) |
|---|---|---|---|
| A (OS punched on return) | 38-67 | 0-2 | **0 on all 10 days** |
| B (OS punched when sent) | 11-40 | **0 on all 10 days** | 0 |

Set aside before counting: orders with nothing left to make at any step that are not
finished at their gate (so not archived). They are rows of zero-length milestones at
the plan start, and their date follows the plan clock every day (A: 2 to 10 per day,
B: 2 to 35). Not a placement effect, and the same in the old app.

### 5. Late-days: the cost of a stable plan (spec 5.8 / section 7)

| | A: OS on return | B: OS when sent |
|---|---|---|
| (a) today, before this feature (free plan, applied ranks) | 298 | 298 |
| (b) day 0 after go-live (published, repaired) | 308 | 308 |
| (c) day 10, repaired every day, never optimized | **918** | **508** |
| (c') day 10, the old free re-plan on the SAME punches | 689 | 504 |
| (d) day 10, fresh Optimize (budget 15): search best / after Apply | 677 / **678** | 494 / **495** |

Read it as (c) against (c'): the price of never re-deciding machines and turns between
Optimize clicks is **+229 late-days after 10 days in variant A, +4 in variant B**.
Variant A is expensive because every unreturned OS block slides one day per day and,
under the strict turn, every job queued behind its successor waits for it, where the
free re-plan let other work through. Pressing Optimize recovers it fully (678 < 689,
495 < 504). The absolute growth from 308 is inflated in both columns by the
nothing-left orders above (each adds about one late-day per day) and by two tiny live
orders whose fractional punched quantities leave a 1-piece remainder at every step.

### 6. Optimize, Apply, machine down (spec 5.8)

- **Date list told the truth**: after Apply, every listed order's date equals its
  "after" value and no unlisted order moved (A: 47 listed; B: 20; machine-down runs:
  48 and 26). Banner gone after Apply; 0 ops left on the down machine inside the break.
- **Machine down**: VMC1 (A) / CNC7 (B) marked down two working days: 0 machine changes,
  0 turn inversions, published rows untouched, banner names the machine and the waiting
  orders, identical for admin and user.
- **Fixed in the final fix wave, "a repair right after Apply moves nothing"**: before
  the fix the first Done after Apply changed the plan with no new punch (variant B: 0
  dates, 0 machines, 0 turns, but 18 frozen rows rebuilt and one in-progress job
  changed person; `repro_apply_then_done.py`). Cause: `_optimize_apply` published the
  new plan but kept the frozen set built from the OLD one. Apply now rebuilds the frozen
  set from the plan it publishes. Re-run: identical plan hash after Apply + Done on day
  10 AND after the machine-down Optimize + Apply + Done, both variants.
- **The panel's numbers are the plan after Apply (fix wave I2)**: "After" late-days =
  late-days of the plan in force after Apply, A 678 = 678, B 495 = 495; machine-down
  runs A 743 = 743, B 525 = 525. (They were scored on the candidate's FREE plan before.)
- **Banner count (fix wave I7)**: now counts only orders whose PUBLISHED run on the
  machine overlaps the break. VMC1 down 18-19 Oct (A): "2 orders are waiting on it"
  (was "9"); CNC7 (B): 2.

### 7. Add New Orders (spec 5.7)

Day-0 published plan; two new lines (the two items with the most CNC/VMC work), quote,
add, re-plan: quote verified, **quoted dates = plan dates (27-11 and 23-11), 0 existing
orders moved date, 0 moved machine**, no pin warning. Done right after: 0 orders moved,
quoted dates unchanged. Two punched days after the accept: quoted dates still 27-11 and
23-11 (stage 2 replaying the pins in rank order holds). All per-day checks pass after
the add.

### 8. Ask for an earlier date (spec D10), end to end (fix wave C1)

On the day-10 state: two new lines (the two items with the most CNC/VMC work), quoted,
then a preponed search with targets 7 days before the quoted dates (budget 15),
accepted, re-planned, then Done.

| | A | B |
|---|---|---|
| accepted orders' steps in the published plan / held there by the plan | 19 / 19 | 19 / 19 |
| existing orders listed as finishing later / wrong on the floor after accept / later but unlisted | 3 / 0 / 0 | 7 / 0 / 0 |
| existing-book late-days on the screen: before -> after; floor after accept | 918 -> 681; 681 | 508 -> 495; 495 |
| machine changes / turn inversions after accept (vs the new published plan) | 0 / 0 | 0 / 0 (1 between half-finished jobs) |
| calendar, people, invariants, one set of dates (69 orders) | clean | clean |
| Done right after accept | identical plan | identical plan |

Before the fix wave the published plan written by an accepted earlier date did not
contain the accepted orders at all (the snapshot read the book before they were added),
and the "what moved" screen compared against the candidate's free plan.

### 9. Base vs head, nothing published (fix wave minor f)

`run_forward` on Test5/8/9 (`~/Desktop/Anvitech Rebuilt/Test*.xlsx`, read only), plan
start 22-09-2026, overlap 88, consolidation window 1, operator logic on, flexible
machines off and on, WIP 0 and 30 lines part-punched (frozen sets of 17 / 19 / 10 ops),
no published plan: hash of (machine, operator, start, end, qty) of every entry at
`035fe86` (branch base) and at the fix-wave HEAD. **12 of 12 identical.** So without a
published plan the branch plans exactly as before (only `SCHEDULER_FINGERPRINT` differs),
**on books where no step before an outsourced step is complete**: the WIP 30 punches
never complete a step, so no sent date exists. Since Task 9, D12 (vendor time from the
sent date) DOES change free and Optimize plans whenever a step before an OS step is
punched complete; that is intended, and it is not covered by this identity.

## Mutation sweep (spec 5.9)

`mutation_sweep.py`: 35 mutations, each alone on the final code, fixed-plan test set
(196 tests; `--full` for the whole suite), file restored, 1.1 s apart.

**34 of 35 are load-bearing in the suite.** The four spec 5.9 parts all bite: machine pin
(`test_pinned_op_stays_on_its_machine_even_when_another_is_free_sooner`), strict turn
(`test_deadlock_releases_one_turn_and_schedules_everything`), no release on a down
machine (`test_a_repair_does_not_release_a_frozen_pin_but_a_free_plan_does`), published
plan written only on apply (`test_a_break_entered_before_a_new_order_still_raises_the_banner`).
Task 1 (5), Task 2 (4), Task 4 (2), Task 5 (11; the scheduler guard only through the
full suite, `test_optimize_lanes_feedback`), Task 6b (8) all bite.

**Fix wave:** 16 more mutations, one per fix part, each alone, fixed-plan test set:
**16 of 16 caught** (C1 accept without the accepted orders; C1 "what moved" from the
free candidate; I2 panel best from the free candidate; I8 date list from the free
candidate; I3 Apply keeps the old frozen set; I3 candidate repaired with the stored
frozen set; I4 publish failure swallowed; I4 ranks written before the publish; I5 both
silent paths; I6 clear without republish; I7 any later job counted; both lock sites;
date-list error dropped; UI says it only when improved). The item below is closed: the
fix-wave fixture (`test_the_panel_after_numbers_are_the_plan_the_floor_gets`) has a
half-finished job moved off a down machine, so its free plan and its repair differ by
a day on three orders, asserted so it cannot go vacuous.

**Before the fix wave, the one that failed no test: Task 6's "date list from the FREE candidate"**
(`_candidate_dates` returning the candidate's own dates instead of its repaired dates).
Task 6's floor-equality test caught it when written; Task 6b then made a repair of an
unpunched plan reproduce that plan on the test fixture, so the fixture can no longer
tell the two apart. **On live data it is load-bearing**: with the mutation, the live
harness's "listed dates are exactly what the floor gets" check fails, 13 of 16 listed
dates wrong and 6 unlisted orders moved. A test on a fixture where repair and free
candidate differ would close it.

## Not run

- The browser pass (spec 5.10): left to the controller (below).
- Test5/8/9: not re-run here (Task 6b measured them: repaired vs free +1 to +50
  late-days, 0 machine changes, 0 moves on a repair of a repair).
- Cloud / GitHub Actions / Oracle Optimize, multi-admin concurrency (the publish lock
  is checked by a test that the lock is held, not by two concurrent admins).
- A real floor's out-of-turn punch (running the next job when the head of the queue is
  blocked): the simulator always follows the plan, so it never does what D6 lets the
  floor do.

## Browser pass, for the controller

Both roles on a store copy: the Done note text; the Optimize button's confirm dialog
text; the progress panel and the date list (null = "Could not work out ...", [] = "No
delivery date changes.", list); Apply / Discard admin only; the fixed-plan notice
(machine down) visible to both roles at the top of the data-gaps card; the user role's
read-only Optimize panel; no console errors.
