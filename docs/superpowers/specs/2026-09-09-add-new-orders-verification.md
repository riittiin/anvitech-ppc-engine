# Task 15 — Verification on the owner's real books

**Verdict: FAIL.** The feature's core promise holds — *no pre-existing order moved,
on any run, on any book*. Three other things do not hold, and two of them are
visible to a director on screen. Details, numbers and re-runnable harnesses below.

Worktree `/Users/ritinwadekar/Desktop/Anvitech Rebuilt/.worktrees/add-new-orders`,
branch `feat/add-new-orders-quote`, commit `a6e9f83`. Pre-feature baseline worktree
`../baseline` at `8dc1e75`. All runs `python3.12`, `PYTHONPATH=$PWD`.

---

## Summary table

| # | Check | Result |
|---|---|---|
| 1 | Byte-identical plan with no new orders | **PASS** — 18 of 18 hashes identical |
| 2 | The freeze (pure, 36 runs on real books) | **PASS** — 0 date/machine/operator moves, 0 violations |
| 3 | Hunt the suspected existing-order date shift | **FOUND — REAL DEFECT** (batch-id collision) |
| 4 | The gap rule | **PASS** — 0 overlaps, 0 spans; 17.0% of new ops land in a gap |
| 5 | First come, first served | **FAIL** — accepted new orders move; quoted date ≠ screen |
| 6 | Mutation testing | 7 of 8 load-bearing; **1 fails no test** |
| 7 | Live through the app | **MIXED** — role gating and queue clearing pass; surfaces disagree |
| 8 | Cross-surface | **FAIL while the queue is non-empty**; clean once it clears |
| — | Full suite | **1109 passed, 2 skipped** (88s) |

---

## 1. Byte-identical — PASS (18 of 18)

Two independent sweeps, each run on `../baseline` (8dc1e75) and on HEAD (a6e9f83),
hashing `(batch_id, item_code, process_seq, machine, operator, start, end, qty)`
over every schedule entry, with `plan_start_date=date(2026,9,1)` so the comparison
is about the code and never about the clock.

**(a) Book size 10 / 30 / full** (`plan_hash.py`, the pre-existing harness):

| Book | lines | entries | sha (baseline) | sha (HEAD) |
|---|---|---|---|---|
| Test5 | 10 | 66 | 4654de14b8637705 | 4654de14b8637705 |
| Test5 | 30 | 164 | cce966056c3ff256 | cce966056c3ff256 |
| Test5 | 57 | 306 | ebcd1db4a7ff28be | ebcd1db4a7ff28be |
| Test8 | 10 | 66 | 0380d5bf398e0052 | 0380d5bf398e0052 |
| Test8 | 30 | 172 | 7b3fbc2144497cd9 | 7b3fbc2144497cd9 |
| Test8 | 67 | 358 | d209d270e65f04e0 | d209d270e65f04e0 |
| Test9 | 10 | 60 | 8cb271c72ede8bbf | 8cb271c72ede8bbf |
| Test9 | 30 | 154 | 26499bfb3760e26e | 26499bfb3760e26e |
| Test9 | 68 | 398 | 9cfed34036e3f971 | 9cfed34036e3f971 |

**(b) Real work in progress** — the book-size sweep is not a WIP test, so a second
sweep punches actuals, derives a frozen set through
`orderbook.active_so_lines` → `freeze.schedule_projection` →
`freeze.compute_frozen_set`, and plans with `frozen=` (`hash_wip.py`):

| Book | orders | part-finished | frozen ops | entries | sha (baseline) | sha (HEAD) |
|---|---|---|---|---|---|---|
| Test5 | 57 | 0 | 0 | 306 | ebcd1db4a7ff28be | ebcd1db4a7ff28be |
| Test5 | 57 | 30 | 17 | 306 | c2cd6b7c68b6604c | c2cd6b7c68b6604c |
| Test5 | 57 | 57 | 27 | 306 | 7fba886b568a7c85 | 7fba886b568a7c85 |
| Test8 | 67 | 0 | 0 | 358 | d209d270e65f04e0 | d209d270e65f04e0 |
| Test8 | 67 | 30 | 19 | 358 | 540e9719563dc94b | 540e9719563dc94b |
| Test8 | 67 | 67 | 36 | 358 | e5f2741072474326 | e5f2741072474326 |
| Test9 | 68 | 0 | 0 | 398 | 9cfed34036e3f971 | 9cfed34036e3f971 |
| Test9 | 68 | 30 | 10 | 398 | d8aebf8f0ab8862f | d8aebf8f0ab8862f |
| Test9 | 68 | 68 | 37 | 398 | 5f732cc9da87a73e | 5f732cc9da87a73e |

`run_forward(occupancy=...)` does not exist on the baseline, so the shared harness
passes `occupancy` only when it is not `None`. Golden trace unchanged; the full
suite is green on HEAD (**1109 passed, 2 skipped**), so nothing was regenerated and
`SCHEDULER_FINGERPRINT` was correctly left alone.

---

## 2. The freeze on real data — PASS (36 runs)

`check2_freeze.py`: 3 books × 3 WIP levels (0 / 30 / all orders part-finished) ×
{1, 3, 10, 20} new orders. New lines use **real item codes from that book's own
Item's Process Master**, quantities 10–500, SO numbers that do not exist in the
book. Each run does the real thing twice: `engine.quote.quote(...)`, and then the
two-stage plan `api._plan` actually runs (`run_forward(occupancy=...)` with the
arrival queue's `priority_rank`).

**Every run: 0 existing orders moved.** Not a date, not a machine, not an operator,
not a start, not an end, not a quantity.

| Book | existing orders checked per run | existing schedule entries checked per run |
|---|---|---|
| Test5 | **57** | **306** |
| Test8 | **67** | **358** |
| Test9 | **68** | **398** |

The entry key is `(sorted so_refs, item_code, process_seq, machine, start)` —
deliberately **not** `batch_id`, which is not unique across the two stages (§3).
An earlier version of this harness keyed on `batch_id` and reported 18–21 "moved"
entries on Test9 at 20 new orders; those were key collisions in the harness, not
moved work. The run asserts a non-zero order count so a vacuous pass is impossible.

**Violations on the merged plan, all 36 runs:**
`routing_order_violations` **0**, `qualification_violations` **0**,
`batch_quantity_violations` **0** (per stage — see the note below).
`QuoteResult.verified` was `True` and `QuoteResult.violations` empty on all 36.

> **Sub-finding (reporting, not planning).** `api._report_for_book` is called with
> the **merged** schedule but only **stage-1** batches. Because stage-2 batch ids
> collide with stage-1 ids (§3), stage-2 entries land in stage-1 buckets inside
> `batch_quantity_violations`. Measured: `bov_live` produced **1 false row on Test8
> at 10 new orders and 4–5 at 20 new orders**, 0 on Test5 and Test9. Queued orders
> are also never checked by that report at all (no batch of theirs is passed in).

---

## 3. Hunting the suspected date shift — FOUND, and it is real

Task 14 reported an "apparent existing-order date shift" it could not reproduce and
attributed to its own session. **The symptom is real, it reproduces on every run, on
every book, and it fires with as little as one new order.** It is not in the plan —
it is in what the screens publish.

### Root cause

`rule1_consolidate._finalize` names batches `B001, B002, …` from a **counter that
restarts at 1 on every call**. `api._plan` calls the rule chain twice — stage 1 over
the existing book, stage 2 over the queued lines — so **every stage-2 batch id
collides with a stage-1 batch id**. Measured on Test9 with 20 new orders:

```
stage1 batch ids: 54   stage2: 20   COLLIDING: 20
ids carrying more than one (item, SO): 20
  B001 [('00518060-01', 26-27SO900), ('00531569-01', 26-27SO117)]
  B002 [('2109801',    26-27SO84 ), ('2109801',     26-27SO916)]
```

`api._plan` then calls `build_gantt(plan_run.schedule /* MERGED */,
plan_run.batches_prioritized /* STAGE 1 ONLY */, …)`, and `engine/gantt.py:85`
**groups schedule entries into rows by `batch_id`**, publishing
`completion = max(e.end for e in entries)`. The same pattern appears in
`rule6_allocate.build_machine_view` (~line 1196) and
`build_shiftwise_timeline` (~line 1041), both of which compute
"Expected completion" as the max end per `batch_id`.

So a new order's bars are appended to a **pre-existing order's Gantt row**, and that
existing order's published completion becomes the new order's end date.

### Measured, pure (`check3_gantt.py`, calling exactly what `_plan` calls)

| Book | new orders | existing Gantt rows whose date SHIFTED | extra bars glued onto existing rows | machine-view dates shifted | new orders with no row of their own |
|---|---|---|---|---|---|
| Test5 | 1 | 1 | 10 | 1 | 1 of 1 |
| Test5 | 3 | 3 | 29 | 3 | 3 of 3 |
| Test5 | 10 | 8 | 77 | 8 | 10 of 10 |
| Test5 | 20 | 10 | 97 | 10 | 20 of 20 |
| Test8 | 1 | 1 | 10 | 1 | 1 of 1 |
| Test8 | 20 | 15 | 155 | 15 | 20 of 20 |
| Test9 | 1 | 1 | 10 | 1 | 1 of 1 |
| Test9 | 20 | 15 | 144 | 15 | 20 of 20 |

Worked examples, one new order only:
* Test9, `B001` = `26-27SO117`: Gantt completion **02-10-2026 → 17-11-2026 (+46 days)**
* Test5, `B001` = `26-27SO117`: **03-10-2026 → 27-10-2026 (+24 days)**
* Test8, 20 new: `B005` = `26-27SO75`: **19-10-2026 → 05-05-2027 (+198 days)**

### Measured, live through the HTTP API (`check_live.py`, `xsurface.py`)

Test9, 5 orders added through `/new-orders/quote` + `/new-orders/add`, dates
normalised to ISO before comparison (the Orders tab publishes `YYYY-MM-DD`, the
Gantt `DD-MM-YYYY`; a raw string compare invents disagreements that are not there):

```
Orders tab vs Gantt:             6 of 73 DISAGREE, 5 absent (all 5 newly added)
Orders tab vs Machine timeline:  6 of 73 DISAGREE
Orders tab vs Shift-wise export: 6 of 73 DISAGREE
  26-27SO84 /2109801     orders=2026-09-13  gantt=2027-02-27   (167 days)
  26-27SO118/61239614-01 orders=2026-11-03  shiftwise=2028-04-05 (518 days)
```

Test5, 10 orders added, clean server and store:

```
Orders tab vs Gantt:              8 of 67 DISAGREE (worst 56 days), 10 absent (all 10 queued)
Orders tab vs Machine timeline:  11 of 67 DISAGREE (worst 56 days)
Orders tab vs Shift-wise export: 11 of 67 DISAGREE (worst 56 days)   [1085 rows]
```

**Confirmation that the queue is the trigger.** On the same live instance, after
`POST /optimize/done` cleared the queue and the whole book was planned in one stage:

```
AFTER the queue cleared -- Orders vs Gantt:      0 of 73 disagree, 0 absent
AFTER the queue cleared -- Orders vs Shift-wise: 0 of 73 disagree
```

### Why the pure freeze check still passes

`optimizer.expected_completion` keys on `so_refs`/`item_code`, never on `batch_id`,
so the Orders tab and the delay report are right. Only the three surfaces that group
by `batch_id` are wrong. This is precisely the "one plan, one set of dates" class
the 2026-08-07 fix was built to kill, and the shift-wise export is a floor document
people plan their day around.

**Two distinct harms, both director-visible:**
1. A pre-existing order's Gantt / machine-wise / shift-wise date moves by up to 518
   days when a new order is accepted — the one thing the feature promises never
   happens.
2. **A newly added order gets no Gantt row of its own at all** (10 of 10 on Test5,
   20 of 20 on Test9). The director accepts a quote and cannot find the order on
   the Gantt.

---

## 4. The gap rule — PASS, with the number the owner asked for

Same 36 runs as §2. For every stage-2 machine interval, checked against the merged
stage-1 busy blocks on that machine:

* **overlaps an existing block: 0** (all 36 runs)
* **spans an existing block: 0** (all 36 runs) — no job is split, the 90-minute
  setup is never paid twice

**Does the feature actually do anything?** Over the 36 runs, new-order operations
placed on a machine:

| | count | share |
|---|---|---|
| **in a gap between existing jobs** | **353** | **17.0%** |
| after the machine's last existing job | 1,723 | 83.0% |

So roughly **one new operation in six lands in genuinely idle time inside the
existing plan** rather than at the end of the queue. It is not zero, and it is not
the majority. Gap use rises with batch size (Test9: 0 of 10 at one new order, 25 of
131 at twenty) — the first new order usually goes to the back; later ones find
holes.

---

## 5. First come, first served — FAIL

`fcfs.py`: 10 orders added **one at a time** through the real API, re-planning
between each, on all three books. Clean code, one server per book, fresh store.

| Book | book size | adds | **pre-existing orders that moved** | already-accepted NEW orders that moved | worst shift |
|---|---|---|---|---|---|
| Test9 | 68 | 10 | **0** | 2 | 15 days |
| Test8 | 67 | 10 | **0** | 14 | **34 days** |
| Test5 | 57 | 10 | **0** | 2 | 2 days |

**The core promise holds: a pre-existing order never moved, 30 adds out of 30.**
Spec acceptance criterion 4 ("adding orders one at a time never moves anything
already in the book") does **not** hold for already-accepted new orders:

```
Test8 #5 26-27SO704: ALREADY-ACCEPTED 26-27SO703 2027-04-25 -> 2027-05-29 (+34d)
Test9 #6 26-27SO705: ALREADY-ACCEPTED 26-27SO702 2027-01-26 -> 2027-02-10 (+15d)
```

### And a worse one: the quoted date is not what the next screen shows

Acceptance criterion 5 ("Immediately after accepting, the Orders tab, Gantt and
shift-wise export show exactly the quoted dates") fails on **9 of the same 30 adds**:

```
Test9 #3  quoted 2027-02-01  shown 2027-01-26   (-6d)
Test9 #6  quoted 2027-01-31  shown 2027-01-03   (-28d)
Test9 #7  quoted 2026-10-27  shown 2026-10-25   (-2d)
Test8 #2  quoted 2026-11-24  shown 2026-11-22   (-2d)
Test8 #5  quoted 2027-05-14  shown 2027-06-16   (+33d)   <-- LATER than promised
Test8 #6  quoted 2026-11-18  shown 2026-11-15   (-3d)
Test8 #9  quoted 2027-07-05  shown 2027-06-02   (-33d)
Test8 #10 quoted 2026-06-25* shown 2027-06-22   (-3d)
Test5 #10 quoted 2026-12-12  shown 2026-11-18   (-24d)
```
(*Test8 #10 quoted 2027-06-25.) Eight of the nine are earlier, which is harmless to
the customer. **Test8 #5 is 33 days LATER on the very next screen than the date the
director was just given** — and that date has already been written into the book as
the SO delivery date.

### Mechanism (same one for both symptoms)

At **quote** time, `/new-orders/quote` treats the already-queued orders as fixed
occupancy: `quote(plan_run.schedule /* stage 1 + queued */, …)` plans only the new
line around them. At **plan** time, `_plan` re-plans **all** queued lines together in
one stage-2 pass ordered by `_queue_priority_rank`. Those are two different
computations of the same thing. `priority_rank` steers Rule 3's ordering; it does
not pin a placement, and the scheduler is a greedy Giffler-Thompson dispatcher, so
adding a line changes the contention set among the queued lines and an earlier
arrival can be displaced. The queue remembers arrival ORDER; it does not remember
the arrival PLAN, which is what the promise actually needs.

---

## 6. Mutation testing — 7 of 8 load-bearing, 1 covered by nothing

Each part reverted **individually** on a clean tree, full suite run, tree restored.

| # | Part reverted | Result |
|---|---|---|
| 1 | the stop line in `_lay_on_machine` (drop the `deadline` clamp) | **4 fail** |
| 2 | the free-run walk in `_place_operation` (call `_lay_on_machine` directly) | **4 fail** |
| 3 | the operator seeding in `decode` (`booked=None`) | **7 fail** |
| 4 | the assignment seeding in `decode` (`assigned=None`) | **0 fail** |
| 5 | skipping `OFF_LANES` in `occupancy_from_entries` | **3 fail** |
| 6 | the queue split in `_plan` (plan everything in one stage) | **2 fail** |
| 7 | the queue in `_plan_fingerprint` | **1 fail** |
| 8 | the stamp check in `/new-orders/add` | **1 fail** |

**Stated plainly: mutation 4 fails no test.** Seeding
`StaffingBoard(assigned=masters.calendar.machine_shift_operator)` is genuine
belt-and-braces. Reverting it leaves `1109 passed, 2 skipped` — identical to the
unmutated suite. It is the mechanism the spec relies on to make stage 2 *prefer the
person already manning that machine that shift* (§4), and nothing checks that it
does. It is not covered; it is kept, and that is the honest description.

Failing tests per mutation (heads):
```
m1  test_quote_occupancy: a_deadline_that_cannot_hold_the_work_returns_none,
    a_deadline_strictly_inside_a_later_window_clamps_correctly,
    occupying_one_machine_leaves_untouched_orders_unchanged,
    a_four_hour_op_skips_a_one_hour_gap_and_lands_whole_in_the_next_run
m2  test_arrival_queue: two_queued_lines_each_reproduce_their_own_quoted_date
    test_quote_engine:  the_rank_search_finds_a_better_arrangement_than_the_first_one_tried
    test_quote_occupancy: occupying_one_machine_leaves_untouched_orders_unchanged,
                          place_operation_routes_through_free_runs_not_a_direct_lay
m3  7 tests across test_arrival_queue / test_quote_engine / test_quote_occupancy
m4  (none)
m5  test_arrival_queue: two_queued_lines_each_reproduce_their_own_quoted_date
    test_quote_engine:  occupancy_lists_every_machine_block_and_skips_outsourcing,
                        a_quote_moves_no_existing_order
m6  test_arrival_queue: a_contended_book_proves_stage_two_protects_existing_orders,
                        clearing_the_queue_alone_busts_the_plan_cache
m7  test_arrival_queue: clearing_the_queue_alone_busts_the_plan_cache
m8  test_new_orders_api: a_stale_quote_is_refused_and_says_to_requote
```

---

## 7. Live, through the running app

Throwaway uvicorn instance, `DEFAULT_SCHEDULER=new`, local file store, real
workbook uploaded through `POST /upload`, driven as admin over HTTP. The native
`confirm()` dialog in the browser blocks automation, so **the whole flow was driven
by direct HTTP request instead of a browser**; that is what was done and no browser
screenshots were taken.

| Step | Result |
|---|---|
| Upload Test9 (68 orders), plan | 200, 68 orders with an expected date |
| Item dropdown | **106 routable items** offered |
| Quote → accept, ×5 and ×10 | all 200, `verified: true` on every quote |
| Orders tab shows the quoted date | **PASS in the 5-add run** (5 of 5); fails 9 of 30 in the 10-add runs (§5) |
| Gantt shows the quoted date | **FAIL** — the new orders have no Gantt row at all (§3) |
| Shift-wise export | 1085–2227 rows produced; **FAIL** on dates (§3) |
| Delay report `.xlsx` | 200, 112–125 KB, downloads fine |
| Analytics tab | renders; makespan reflects the queued orders |
| `POST /optimize/done` | `{started: true}`; **queue 5 → 0 immediately.** PASS |
| After the queue cleared | Orders vs Gantt **0 of 73**, Orders vs shift-wise **0 of 73** |
| Newly added orders after Done | ranked normally, dates unchanged (0 of 5 moved) |

**The user role reaches none of it (server-side, all 403):**
```
user GET  /new-orders/drafts         -> 403
user PUT  /new-orders/drafts         -> 403
user POST /new-orders/quote          -> 403
user POST /new-orders/add            -> 403
user POST /new-orders/prepone        -> 403
user POST /new-orders/prepone/accept -> 403
```

**Note on `AUTO_OPTIMIZE=0`.** An earlier run of this check had that test-isolation
variable set, and `POST /optimize/done` returned `{started: false, reason:
"skipped"}` and left the queue at 5. That is correct designed behaviour (`_plan`'s
comment: clearing on a skip would drop arrival positions with no re-optimization
having earned it), not a defect — it is recorded here so the next person does not
re-discover it as one.

---

## 8. Cross-surface — FAIL while the queue is non-empty, clean once it clears

Covered by §3. Zero routing-order, qualification and batch-quantity violations on
every surface, live and pure. The only validation-report rows produced on the live
book are 6 pre-existing `DUPLICATE_PROCESS` data quirks in Test5's Item's Process
Master (`'DISPATCH' appears at steps [9, 10]` etc.), unrelated to this feature.

---

## A false alarm I raised and then killed — recorded so it is not re-chased

Mid-task I measured `/new-orders/quote` returning **`verified: false`** with CNC3
and "OS / Outsourced" double-bookings, and a long-running server producing a
different stage-2 plan (10 of 432 rows) from a fresh process on the same store.

**All of it was my own contamination.** I had launched the mutation-testing batch in
the background, and it rewrites `ppc_engine/scheduler/flow_scheduler.py` and
`engine/new_engine.py` in place. The servers and probes I started while it ran had
imported mutated code — mutation m5 removes exactly the `OFF_LANES` skip, which is
why outsourcing lanes started reading as double-booked machines.

Re-run serially on a clean tree, **8 consecutive quote-and-accept cycles all return
`verified: true`**, and a restarted server reproduces the fresh process's plan hash
exactly (`1c1e55ff958a319b`). Lesson, and it is the repo's own rule: never run a
mutation batch concurrently with anything else that reads the working tree.

**One real sub-finding survives from that chase:** when a quote does fail its own
check, `POST /new-orders/quote` returns `verified: false` but **not** `violations`
(response keys are `existing_count, lines, moved, stamp, verified`). The director is
told the quote could not be confirmed and is given no reason, and neither is the
next engineer.

---

## What I could not measure

* **The preponed path (`/new-orders/prepone`, `/new-orders/prepone/accept`)** was
  only checked for role gating (403 for the user role). Its full behaviour needs a
  finished contest per trial (15–30 min each on this hardware) and was not measured
  on the real books.
* **The cloud / GitHub Actions / Oracle contest path** was not executed; the queue's
  interaction with `optimize_service.build_payload`/`parse_payload` is unverified
  end to end, exactly as in the 2026-08-09 audit.
* **A real browser.** Everything live was driven over HTTP. The tab's own JavaScript
  (`renderPreponeResult`, the item filter, the duplicate warning) is unexercised
  here; Task 14 covered it in a browser.
* **Concurrency.** Two admins quoting at once shares `anvitech:new_order_drafts`;
  the spec documents this rather than defending it, and I did not test it.
* **Whether the batch-id collision has other consumers.** I traced `gantt.py`,
  `build_machine_view`, `build_shiftwise_timeline` and `batch_quantity_violations`.
  `freeze.schedule_projection` also emits `batch_id`; `compute_frozen_set` indexes
  by `(item_code, process_seq)` + `so_refs`, so it looks safe, but I did not prove
  it with a WIP-plus-queue run.

---

## Harnesses (all in the session scratchpad, none committed)

Scratchpad: `/private/tmp/claude-501/-Users-ritinwadekar-Desktop-Anvitech-Rebuilt/55659fb2-182e-4b1b-b986-a17d39f8c2db/scratchpad`. Run everything with `python3.12` and `PYTHONPATH=$PWD`.

```bash
# 1. byte-identical, book size
for wb in Test5 Test8 Test9; do for n in 10 30 0; do
  PYTHONPATH=$PWD python3.12 $SP/plan_hash.py $PWD/$wb.xlsx $n; done; done   # in BOTH worktrees
# 1b. byte-identical, real WIP
PYTHONPATH=$PWD python3.12 $SP/hash_wip.py "$PWD"                            # in BOTH worktrees
# 2 + 4. the freeze and the gap rule
PYTHONPATH=$PWD python3.12 $SP/check2_freeze.py "$PWD"
# 3. the Gantt / machine-view date shift
PYTHONPATH=$PWD python3.12 $SP/check3_gantt.py "$PWD"
PYTHONPATH=$PWD python3.12 $SP/probe_batchid.py "$PWD"
# 5, 7, 8. live (start the server first, see below)
python3.12 $SP/check_live.py  "$PWD/Test9.xlsx" 5
python3.12 $SP/fcfs.py        "$PWD/Test9.xlsx" 10
python3.12 $SP/xsurface.py
# 6. mutation testing -- RUN NOTHING ELSE AGAINST THE TREE WHILE THIS RUNS
$SP/mutrun.sh m1 m2 m3 m4 m5 m6 m7 m8

# the throwaway instance (passwords have no defaults; they MUST come from env)
STORE_DIR=$SP/livestore DEFAULT_SCHEDULER=new ADMIN_PASSWORD=adm-pass-1 \
  USER_PASSWORD=usr-pass-1 PYTHONPATH=$PWD \
  python3.12 -m uvicorn api.main:app --host 127.0.0.1 --port 8793
```

### `wipbook.py`

```python
"""Shared harness: build a REAL in-progress book from one of the owner's workbooks,
and plan it exactly the way api._plan does (stage 1, frozen set, applied ranks off).

WIP is built by PUNCHING actuals, not by faking process_qty -- so it goes through
orderbook.active_so_lines / freeze.compute_frozen_set, the same two functions the
live app uses. `wip` = how many of the book's orders are part-finished.
"""
import hashlib, io, os, tempfile
from datetime import date

os.environ.setdefault("STORE_DIR", tempfile.mkdtemp())

from engine import loaders, new_engine, orderbook, book_store, freeze
from engine.config import Config
from engine.models import PlanRun, Order, Actual
from engine.pipeline import run_forward

PLAN_START = date(2026, 9, 1)


def cfg():
    return Config(scheduler="new", plan_start_date=PLAN_START,
                  apply_operator_logic=True, overlap_percent=88)


def load(wb):
    raw = open(wb, "rb").read()
    book_store.save_masters_bytes(raw)
    new_engine.set_masters_bytes(raw)
    so_lines, masters = loaders.load_all(io.BytesIO(raw))
    return so_lines, masters


def build_book(so_lines, masters, size=0, wip=0):
    """-> (active_orders dict, actuals list). Deterministic."""
    lines = list(so_lines)[: size or len(so_lines)]
    orders, actuals = {}, []
    for i, l in enumerate(lines):
        o = Order(so_no=l.so_no, item_code=l.item_code, item_name=l.item_name,
                  ordered_qty=float(l.qty), delivery_date=l.delivery_date)
        orders[o.key] = o
        if i >= wip:
            continue
        routing = masters.routings.get(l.item_code)
        if routing is None or not routing.processes:
            continue
        # Punch the FIRST step to 40% good. Precedence-legal by construction
        # (nothing downstream is punched), and it leaves the step part-done, which
        # is exactly what freeze.compute_frozen_set looks for.
        good = max(1.0, round(float(l.qty) * 0.4))
        if good >= float(l.qty):
            good = max(1.0, float(l.qty) - 1.0)
        actuals.append(Actual(so_no=l.so_no, item_code=l.item_code,
                              entry_date=date(2026, 8, 28), qty_produced=good,
                              process=routing.processes[0].name, operator="",
                              shift="1st shift", id=f"wip-{i}"))
    return orders, actuals


def plan(orders, actuals, masters, c=None, occupancy=None, extra_lines=None,
         frozen_rows=None):
    """Stage 1 exactly as api._plan runs it: active lines, then a frozen set
    derived from a first pass (the app derives it from the LAST APPLIED plan;
    with no applied plan on file the first pass is the honest stand-in)."""
    c = c or cfg()
    lines = orderbook.active_so_lines(orders, actuals, masters)
    if extra_lines:
        lines = lines + list(extra_lines)
    if frozen_rows is None:
        frozen_rows = derive_frozen(lines, actuals, masters, c)
    pr = PlanRun(so_lines=lines)
    kw = {}
    if occupancy is not None:
        kw["occupancy"] = occupancy   # absent on the pre-feature baseline
    run_forward(pr, c, masters, frozen=frozen_rows or None, **kw)
    return pr, lines, frozen_rows


def derive_frozen(lines, actuals, masters, c=None):
    c = c or cfg()
    if not actuals:
        return []
    pr0 = PlanRun(so_lines=list(lines))
    run_forward(pr0, c, masters)
    applied = freeze.schedule_projection(pr0.schedule)
    gbs = {}
    for a in actuals:
        k = (a.so_no, a.item_code, loaders.normalize_process_name(a.process))
        gbs[k] = gbs.get(k, 0.0) + a.good_qty()
    return freeze.compute_frozen_set(applied, lines, gbs, masters)


def entry_hash(schedule):
    h = hashlib.sha256()
    for e in schedule:
        h.update(f"{e.batch_id}|{e.item_code}|{e.process_seq}|{e.machine}|"
                 f"{e.operator}|{e.start.isoformat()}|{e.end.isoformat()}|{e.qty}\n"
                 .encode())
    return h.hexdigest()[:16]
```

### `plan_hash.py`

```python
"""Hash a real book's plan, so two commits can be compared exactly.

Acceptance criterion 1 of the Add New Orders quote spec: with no new orders in
play, the plan must be BYTE-IDENTICAL to before the feature. Not 'equivalent' --
identical, by hash, on the owner's own workbooks.
"""
import hashlib
import io
import os
import sys
import tempfile

os.environ["STORE_DIR"] = tempfile.mkdtemp()

from datetime import date

from engine import loaders, new_engine, orderbook, book_store
from engine.config import Config
from engine.models import PlanRun
from engine.pipeline import run_forward

WB = sys.argv[1]
WIP = int(sys.argv[2]) if len(sys.argv) > 2 else 0

raw = open(WB, "rb").read()
book_store.save_masters_bytes(raw)
new_engine.set_masters_bytes(raw)
so_lines, masters = loaders.load_all(io.BytesIO(raw))

# A fixed plan start keeps the comparison about the CODE, never about the clock.
cfg = Config(scheduler="new", plan_start_date=date(2026, 9, 1),
             apply_operator_logic=True, overlap_percent=88)

lines = list(so_lines)[: WIP or len(so_lines)]
pr = PlanRun(so_lines=lines)
run_forward(pr, cfg, masters)

h = hashlib.sha256()
for e in pr.schedule:
    h.update(f"{e.batch_id}|{e.item_code}|{e.process_seq}|{e.machine}|"
             f"{e.operator}|{e.start.isoformat()}|{e.end.isoformat()}|{e.qty}\n"
             .encode())
print(f"{os.path.basename(WB)} lines={len(lines)} entries={len(pr.schedule)} sha={h.hexdigest()[:16]}")
```

### `hash_wip.py`

```python
"""Byte-identical guard, with REAL work in progress (frozen ops)."""
import sys
sys.path.insert(0, "/private/tmp/claude-501/-Users-ritinwadekar-Desktop-Anvitech-Rebuilt/55659fb2-182e-4b1b-b986-a17d39f8c2db/scratchpad")
import wipbook as W

for wb in ("Test5", "Test8", "Test9"):
    so_lines, masters = W.load(f"{sys.argv[1]}/{wb}.xlsx")
    for wip in (0, 30, 10**6):
        orders, actuals = W.build_book(so_lines, masters, wip=wip)
        pr, lines, fz = W.plan(orders, actuals, masters)
        print(f"{wb} orders={len(orders)} wip={min(wip,len(orders))} frozen={len(fz)} "
              f"entries={len(pr.schedule)} sha={W.entry_hash(pr.schedule)}")
```

### `check2_freeze.py`

```python
"""Check 2 + Check 3 + Check 4: the freeze, the gap rule, and gap-vs-after placement.

For each real book, at 3 work-in-progress levels, quote/queue 1, 3, 10, 20 new
orders and assert for EVERY existing order: same expected completion, same
machine, same operator, same start/end/qty. The new lines go through the SAME
two-stage path api._plan runs (stage 1, then run_forward(occupancy=...) with the
arrival queue's rank), so this measures the shipped path, not a lookalike.
"""
import sys, collections
sys.path.insert(0, "/private/tmp/claude-501/-Users-ritinwadekar-Desktop-Anvitech-Rebuilt/55659fb2-182e-4b1b-b986-a17d39f8c2db/scratchpad")
import wipbook as W
from engine import new_engine, optimizer, quote as Q
from engine.models import PlanRun, SOLine
from engine.pipeline import run_forward
from engine.rules import rule1_consolidate

ROOT = sys.argv[1]
QTYS = (120, 45, 300, 80, 500, 15, 210, 60, 350, 25,
        100, 400, 30, 175, 250, 55, 90, 320, 140, 10)


def new_lines(masters, existing_keys, n):
    """n new order lines using REAL item codes from this book's own Item's Process
    Master, cycling deterministically, quantities 10-500. SO numbers are new."""
    codes = sorted(masters.routings)
    out = []
    for i in range(n):
        code = codes[(i * 7) % len(codes)]
        so = f"26-27SO9{i:02d}"
        assert (so, code) not in existing_keys
        r = masters.routings[code]
        out.append(Q.QuoteLine(so_no=so, item_code=code,
                               item_name=getattr(r, "item_name", "") or code,
                               qty=float(QTYS[i % len(QTYS)])))
    return out


def free_runs(intervals):
    """Merged busy intervals -> (start,end) busy blocks, sorted."""
    iv = sorted(intervals)
    merged = []
    for s, e in iv:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def main():
    grand = collections.Counter()
    for wb in ("Test5", "Test8", "Test9"):
        so_lines, masters = W.load(f"{ROOT}/{wb}.xlsx")
        for wip in (0, 30, 10**6):
            orders, actuals = W.build_book(so_lines, masters, wip=wip)
            cfg = W.cfg()
            pr1, lines1, fz = W.plan(orders, actuals, masters, cfg)
            base_entries = list(pr1.schedule)
            base_exp = optimizer.expected_completion(base_entries)
            def sig_key(e):
                # NOT batch_id: stage 2's rule1_consolidate restarts its B00n
                # counter, so stage-1 and stage-2 ids collide (see check 3).
                return (tuple(sorted(e.so_refs or ())), e.item_code,
                        e.process_seq, e.machine, e.start)
            base_sig = {sig_key(e): (e.machine, e.operator, e.start, e.end, e.qty)
                        for e in base_entries}
            existing_keys = set(base_exp)
            occ1 = new_engine.occupancy_from_entries(base_entries, cfg)
            # The same ppc-side Masters api._report_for_book builds for the check.
            nm = new_engine._apply_app_operators(
                new_engine._new_masters(bool(cfg.flexible_machines)), masters)

            for n in (1, 3, 10, 20):
                nl = new_lines(masters, existing_keys, n)
                # --- the QUOTE (engine/quote.py) ---
                qr = Q.quote(base_entries, base_exp, nl, cfg, masters)
                # --- the TWO-STAGE PLAN (api._plan's own path) ---
                rank = {f"{so}\x1f{ic}": i + 1 for i, (so, ic) in enumerate(qr.order)}
                s2 = PlanRun(so_lines=[Q._as_so_line(l, cfg.plan_start_date) for l in nl])
                run_forward(s2, cfg, masters, occupancy=occ1, priority_rank=rank)
                merged = base_entries + list(s2.schedule)

                # === CHECK 2a: no existing order moved ===
                after_exp = optimizer.expected_completion(merged)
                moved = [k for k in base_exp
                         if after_exp.get(k) != base_exp[k]]
                after_sig = {}
                for e in merged:
                    k = sig_key(e)
                    if k in base_sig:
                        after_sig[k] = (e.machine, e.operator, e.start, e.end, e.qty)
                sig_moved = [k for k, v in base_sig.items() if after_sig.get(k) != v]

                # === CHECK 2b: the three violation reports on the MERGED plan ===
                rov = new_engine.routing_order_violations(merged, masters)
                qov = new_engine.qualification_violations(merged, nm)
                b1s = rule1_consolidate.run(lines1, cfg, masters=masters)
                b2s = rule1_consolidate.run(
                    [Q._as_so_line(l, cfg.plan_start_date) for l in nl],
                    cfg, masters=masters)
                bov = (new_engine.batch_quantity_violations(base_entries, b1s)
                       + new_engine.batch_quantity_violations(list(s2.schedule), b2s))
                # What the SHIPPED api._report_for_book computes: merged schedule,
                # stage-1 batches only. Reported separately, never conflated.
                bov_live = new_engine.batch_quantity_violations(merged, b1s)

                # === CHECK 3: the gap rule ===
                occ2 = new_engine.occupancy_from_entries(list(s2.schedule), cfg)
                overlap = span = 0
                in_gap = after_last = 0
                for mid, ivs in occ2["machine"].items():
                    busy = free_runs(occ1["machine"].get(mid, ()))
                    last_end = max((e for _, e in busy), default=None)
                    for ns, ne in ivs:
                        hits = [(bs, be) for bs, be in busy if ns < be and bs < ne]
                        if hits:
                            overlap += 1
                        # spans: an existing block lies wholly inside the new one
                        if any(ns <= bs and be <= ne for bs, be in busy):
                            span += 1
                        if last_end is not None and ns < last_end:
                            in_gap += 1
                        else:
                            after_last += 1
                grand["gap"] += in_gap
                grand["after"] += after_last
                grand["bov_live"] += len(bov_live)

                dated = sum(1 for r in qr.lines if r["completion"])
                ok = (not moved and not sig_moved and not rov and not qov
                      and not bov and not overlap and not span
                      and not qr.violations and not qr.moved)
                grand["runs"] += 1
                grand["fail"] += 0 if ok else 1
                print(f"{wb} wip={min(wip,len(orders)):>2} frozen={len(fz):>2} new={n:>2} | "
                      f"existing_checked={len(base_exp)} entries_checked={len(base_sig)} "
                      f"date_moved={len(moved)} sig_moved={len(sig_moved)} "
                      f"rov={len(rov)} qov={len(qov)} bov={len(bov)} bov_live={len(bov_live)} "
                      f"overlap={overlap} span={span} "
                      f"gap={in_gap} after={after_last} "
                      f"quoted={dated}/{n} verified={qr.verified} "
                      f"{'OK' if ok else '*** FAIL ***'}")

                # A run that checked zero orders proves nothing.
                assert len(base_exp) > 0 and len(base_sig) > 0
    print(f"\nTOTAL runs={grand['runs']} failures={grand['fail']} "
          f"new ops in a gap={grand['gap']} after the machine's last job={grand['after']}")


main()
```

### `check3_gantt.py`

```python
"""CHECK 3 — hunting the suspected existing-order date shift.

Drives the SAME three calls api._plan makes:
    build_gantt(MERGED schedule, STAGE-1 batches, ...)
    build_shiftwise_timeline / build_machine_view (same pair)
and compares each existing order's published completion date before vs after.
"""
import sys, collections
sys.path.insert(0, "/private/tmp/claude-501/-Users-ritinwadekar-Desktop-Anvitech-Rebuilt/55659fb2-182e-4b1b-b986-a17d39f8c2db/scratchpad")
import wipbook as W
from engine import new_engine, optimizer, quote as Q
from engine.gantt import build_gantt
from engine.models import PlanRun
from engine.pipeline import run_forward
from engine.rules.rule6_allocate import build_machine_view

ROOT = sys.argv[1]
for wb in ("Test5", "Test8", "Test9"):
  for n in (1, 3, 10, 20):
    so_lines, masters = W.load(f"{ROOT}/{wb}.xlsx")
    orders, actuals = W.build_book(so_lines, masters)
    cfg = W.cfg()
    pr1, lines1, fz = W.plan(orders, actuals, masters, cfg)
    b1 = pr1.batches_prioritized
    g_before = {r["batch_id"]: r for r in
                build_gantt(pr1.schedule, b1, masters)["rows"]}

    occ1 = new_engine.occupancy_from_entries(pr1.schedule, cfg)
    codes = sorted(masters.routings)
    nl = [Q.QuoteLine(so_no=f"26-27SO9{i:02d}", item_code=codes[(i*7) % len(codes)],
                      item_name=codes[(i*7) % len(codes)], qty=100.0) for i in range(n)]
    qr = Q.quote(pr1.schedule, optimizer.expected_completion(pr1.schedule), nl,
                 cfg, masters)
    quoted = {(r["so_no"], r["item_code"]): r["completion"] for r in qr.lines}
    rank = {f"{so}\x1f{ic}": i + 1 for i, (so, ic) in enumerate(qr.order)}
    s2 = PlanRun(so_lines=[Q._as_so_line(l, cfg.plan_start_date) for l in nl])
    run_forward(s2, cfg, masters, occupancy=occ1, priority_rank=rank)
    merged = list(pr1.schedule) + list(s2.schedule)

    # --- exactly api._plan's call: MERGED schedule, STAGE-1 batches ---
    g_after = {r["batch_id"]: r for r in build_gantt(merged, b1, masters)["rows"]}
    shifted = [(bid, g_before[bid]["so_no"], g_before[bid]["completion"],
                g_after[bid]["completion"])
               for bid in g_before
               if g_after.get(bid) and g_after[bid]["completion"] != g_before[bid]["completion"]]
    extra_bars = sum(len(g_after[b]["bars"]) - len(g_before[b]["bars"])
                     for b in g_before if b in g_after)

    mv_b = build_machine_view(pr1.schedule, masters, cfg, b1)[0]
    mv_a = build_machine_view(merged, masters, cfg, b1)[0]
    def mvdates(v):
        d = {}
        for r in v:
            d.setdefault((r["Batch"], r["SO No"]), set()).add(r["Expected completion"])
        return d
    mb, ma = mvdates(mv_b), mvdates(mv_a)
    mv_shift = [k for k in mb if k in ma and ma[k] != mb[k]]

    # Does the Gantt still publish the date the director was QUOTED?
    quote_wrong = 0
    for r in g_after.values():
        for so in (r["so_no"] or "").split(", "):
            k = (so.strip(), r["item_code"])
            if k in quoted and quoted[k] and r["completion"] != quoted[k].strftime("%d-%m-%Y"):
                quote_wrong += 1
    # How many new orders got NO Gantt row of their own at all?
    shown_keys = set()
    for r in g_after.values():
        for so in (r["so_no"] or "").split(", "):
            shown_keys.add((so.strip(), r["item_code"]))
    missing = [k for k in quoted if k not in shown_keys]

    print(f"{wb} new={n:>2}: gantt rows before={len(g_before)} after={len(g_after)} "
          f"(new rows={len(g_after)-len(g_before)}) | existing gantt dates SHIFTED={len(shifted)} "
          f"| extra bars glued onto existing rows={extra_bars} "
          f"| machine-view dates shifted={len(mv_shift)} "
          f"| new orders with no row of their own={len(missing)}")
    for s in shifted[:3]:
        print(f"      {s[0]} (SO {s[1]}): {s[2]} -> {s[3]}")
```

### `probe_batchid.py`

```python
"""Is a stage-2 batch_id ever the same string as a stage-1 batch_id?"""
import sys, collections
sys.path.insert(0, "/private/tmp/claude-501/-Users-ritinwadekar-Desktop-Anvitech-Rebuilt/55659fb2-182e-4b1b-b986-a17d39f8c2db/scratchpad")
import wipbook as W
from engine import new_engine, optimizer, quote as Q
from engine.models import PlanRun
from engine.pipeline import run_forward

ROOT = sys.argv[1]
so_lines, masters = W.load(f"{ROOT}/Test9.xlsx")
orders, actuals = W.build_book(so_lines, masters)
cfg = W.cfg()
pr1, lines1, fz = W.plan(orders, actuals, masters, cfg)
occ1 = new_engine.occupancy_from_entries(pr1.schedule, cfg)
codes = sorted(masters.routings)
nl = [Q.QuoteLine(so_no=f"26-27SO9{i:02d}", item_code=codes[(i*7) % len(codes)],
                  item_name=codes[(i*7) % len(codes)], qty=100.0) for i in range(20)]
s2 = PlanRun(so_lines=[Q._as_so_line(l, cfg.plan_start_date) for l in nl])
run_forward(s2, cfg, masters, occupancy=occ1)

b1 = {e.batch_id for e in pr1.schedule}
b2 = {e.batch_id for e in s2.schedule}
print("stage1 batch ids:", len(b1), "stage2:", len(b2), "COLLIDING:", len(b1 & b2))
print("collisions:", sorted(b1 & b2)[:10])
# Does a collision put two DIFFERENT items under one id?
byid = collections.defaultdict(set)
for e in list(pr1.schedule) + list(s2.schedule):
    byid[e.batch_id].add((e.item_code, tuple(sorted(e.so_refs or ()))[:1]))
mixed = {k: v for k, v in byid.items() if len(v) > 1}
print("ids carrying more than one (item, so):", len(mixed))
for k in sorted(mixed)[:5]:
    print("  ", k, sorted(mixed[k]))
```

### `live.py`

```python
"""Live driver for the throwaway instance: cookie session + JSON helpers.
requests is not vendored here, so this uses urllib only."""
import json, urllib.request, http.cookiejar, mimetypes, uuid, os

BASE = os.environ.get("LIVE_BASE", "http://127.0.0.1:8791")


class Client:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def _req(self, method, path, data=None, ctype=None):
        r = urllib.request.Request(BASE + path, data=data, method=method)
        r.add_header("Origin", BASE)
        if ctype:
            r.add_header("Content-Type", ctype)
        try:
            with self.op.open(r) as f:
                body = f.read()
                return f.status, body
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def login(self, u, p):
        body = urllib.parse.urlencode({"username": u, "password": p}).encode()
        return self._req("POST", "/login", body,
                         "application/x-www-form-urlencoded")[0]

    def get(self, path):
        st, b = self._req("GET", path)
        try:
            return st, json.loads(b)
        except Exception:
            return st, b

    def post(self, path, obj=None):
        st, b = self._req("POST", path,
                          json.dumps(obj or {}).encode(), "application/json")
        try:
            return st, json.loads(b)
        except Exception:
            return st, b

    def put(self, path, obj):
        st, b = self._req("PUT", path, json.dumps(obj).encode(),
                          "application/json")
        try:
            return st, json.loads(b)
        except Exception:
            return st, b

    def upload(self, path, filepath):
        bnd = uuid.uuid4().hex
        fn = os.path.basename(filepath)
        body = (f"--{bnd}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"{fn}\"\r\nContent-Type: application/octet-stream\r\n\r\n"
                ).encode() + open(filepath, "rb").read() + f"\r\n--{bnd}--\r\n".encode()
        st, b = self._req("POST", path, body, f"multipart/form-data; boundary={bnd}")
        try:
            return st, json.loads(b)
        except Exception:
            return st, b
```

### `check_live.py`

```python
"""CHECKS 5, 7 and 8 through the real HTTP API on a real workbook.

Dates are NORMALISED to ISO before comparison — the Orders tab publishes
YYYY-MM-DD and the Gantt DD-MM-YYYY, so a raw string compare invents
disagreements that are not there.
"""
import sys, collections, datetime as dt
sys.path.insert(0, "/private/tmp/claude-501/-Users-ritinwadekar-Desktop-Anvitech-Rebuilt/55659fb2-182e-4b1b-b986-a17d39f8c2db/scratchpad")
from live import Client

WB = sys.argv[1]
N_ADD = int(sys.argv[2]) if len(sys.argv) > 2 else 5
adm = Client(); assert adm.login("anvitech", "adm-pass-1") == 200
usr = Client(); assert usr.login("anvitech_user", "usr-pass-1") == 200


def iso(s):
    if not s:
        return None
    s = str(s)
    for f in ("%Y-%m-%d", "%d-%m-%Y", "%Y-%m-%dT%H:%M:%S"):
        try:
            return dt.datetime.strptime(s[:19] if "T" in s else s, f).date().isoformat()
        except ValueError:
            pass
    return s


def run():
    st, r = adm.post("/run", {})
    assert st == 200, r
    return r


def dates(resp):
    return {tuple(k.split("\x1f")): iso(v) for k, v in resp["expected_end"].items()}


def col(table, name):
    return table["columns"].index(name)


def gantt_dates(resp):
    out = collections.defaultdict(set)
    for row in resp["gantt"]["rows"]:
        for so in (row["so_no"] or "").split(","):
            if so.strip():
                out[(so.strip(), row["item_code"])].add(iso(row["completion"]))
    return out


def table_dates(tbl, so_col, item_col, date_col):
    out = collections.defaultdict(set)
    for r in tbl["rows"]:
        for so in str(r[col(tbl, so_col)]).split(","):
            if so.strip():
                out[(so.strip(), str(r[col(tbl, item_col)]))].add(iso(r[col(tbl, date_col)]))
    return out


print("=== upload ===", adm.upload("/upload", WB)[0])
base = run()
base_dates = dates(base)
print(f"book: {len(base_dates)} orders with an expected date")

# ---------------- CHECK 5: first come, first served ----------------
st, d = adm.get("/new-orders/drafts")
items = [i["item_code"] for i in d["items"]]
prev, moves, added = dict(base_dates), 0, {}
for i in range(N_ADD):
    so, ic, qty = f"26-27SO80{i}", items[(i * 11) % len(items)], 100 + i * 40
    assert adm.put("/new-orders/drafts",
                   {"drafts": [{"so_no": so, "item_code": ic, "qty": qty}]})[0] == 200
    st, q = adm.post("/new-orders/quote"); assert st == 200 and q["verified"], q
    quoted = iso(q["lines"][0]["completion"])
    st, a = adm.post("/new-orders/add", {"stamp": q["stamp"]}); assert st == 200, a
    after = dates(run())
    moved = {k: (prev[k], after.get(k)) for k in prev if after.get(k) != prev[k]}
    moves += len(moved)
    added[(so, ic)] = quoted
    print(f"  add #{i+1} {so}/{ic}: quoted={quoted} orders-tab={after.get((so, ic))} "
          f"match={'YES' if after.get((so, ic)) == quoted else 'NO'} | "
          f"orders already in the book that MOVED: {len(moved)} of {len(prev)}")
    for k, v in moved.items():
        d0, d1 = dt.date.fromisoformat(v[0]), dt.date.fromisoformat(v[1])
        tag = "PREVIOUSLY ADDED" if k in added else "PRE-EXISTING"
        print(f"      MOVED [{tag}] {k}: {v[0]} -> {v[1]} ({(d1-d0).days:+d} days)")
    prev = after
print(f"CHECK 5 RESULT: {moves} move(s) across {N_ADD} sequential adds "
      f"(spec requires 0)")

# ---------------- CHECK 7/8: the surfaces ----------------
resp = run()
od = dates(resp)
print(f"\nqueue_note: {resp.get('queue_note')!r}")
surfaces = {"Gantt": gantt_dates(resp)}
r6 = resp["trace"]["rule6"]
for tname, tbl in [("Machine timeline", r6["tables"][0]["table"])]:
    surfaces[tname] = table_dates(tbl, "SO No", "Item Code", "Expected completion")
if r6.get("shiftwise"):
    surfaces["Shift-wise export"] = table_dates(
        r6["shiftwise"], "SO No", "Item Code", "Expected completion")
print(f"shift-wise export rows: {len(r6.get('shiftwise', {}).get('rows', []))}")

for name, s in surfaces.items():
    dis = [(k, od[k], sorted(s[k])) for k in od if k in s and {od[k]} != s[k]]
    miss = [k for k in od if k not in s]
    print(f"CHECK 8  Orders vs {name}: {len(dis)} of {len(od)} DISAGREE, "
          f"{len(miss)} absent from that surface")
    for k, a, b in dis[:4]:
        print(f"      {k}: orders={a} {name.lower()}={b}")
    for k in miss[:6]:
        print(f"      ABSENT from {name}: {k} (orders tab: {od[k]}"
              f"{', a NEWLY ADDED order' if k in added else ''})")

rep = resp["report"]
kinds = collections.Counter(r[rep["columns"].index("Kind")] if "Kind" in rep["columns"]
                            else r[0] for r in rep["rows"])
print("validation report rows by kind:", dict(kinds))

# analytics present?
an = resp["trace"].get("analytics")
print("analytics headline:", {k: v for k, v in (an or {}).get("headline", {}).items()
                              if "makespan" in k or "bottleneck" in k})

# delay report downloads?
st, b = adm.get("/delay-report.xlsx")
print("delay report:", st, f"{len(b)} bytes" if isinstance(b, bytes) else b)

# ---------------- CHECK 7: the user role ----------------
print("\n--- user role ---")
for m, p, body in [("GET", "/new-orders/drafts", None),
                   ("PUT", "/new-orders/drafts", {"drafts": []}),
                   ("POST", "/new-orders/quote", {}),
                   ("POST", "/new-orders/add", {"stamp": "x"}),
                   ("POST", "/new-orders/prepone", {}),
                   ("POST", "/new-orders/prepone/accept", {})]:
    st = (usr.get(p) if m == "GET" else usr.put(p, body) if m == "PUT"
          else usr.post(p, body))[0]
    print(f"  user {m} {p} -> {st} {'OK (blocked)' if st in (401, 403) else '*** REACHABLE ***'}")

# ---------------- CHECK 7: Done entering clears the queue ----------------
st, dq = adm.get("/new-orders/drafts")
print(f"\nqueue before Done: {len(dq['queued'])}")
st, done = adm.post("/optimize/done", {})
print("POST /optimize/done ->", st, done)
st, dq2 = adm.get("/new-orders/drafts")
after_done = run()
print(f"queue after Done: {len(dq2['queued'])} | queue_note now: {after_done.get('queue_note')!r}")
ad = dates(after_done)
still = {k: (od[k], ad.get(k)) for k in added if ad.get(k) != od.get(k)}
print(f"newly added orders whose date changed when the queue cleared: {len(still)} of {len(added)}")
for k, v in still.items():
    print(f"      {k}: {v[0]} -> {v[1]}  (expected: the SO DELIVERY DATE is the promise, "
          f"the expected completion may move — spec §9 risk 3)")
```

### `fcfs.py`

```python
"""CHECK 5, deeper: how often does an ALREADY-ACCEPTED order move when the next
one is added? 10 sequential adds, on each real book."""
import sys, datetime as dt, os
os.environ["LIVE_BASE"] = os.environ.get("PORTBASE", "http://127.0.0.1:8793")
sys.path.insert(0, "/private/tmp/claude-501/-Users-ritinwadekar-Desktop-Anvitech-Rebuilt/55659fb2-182e-4b1b-b986-a17d39f8c2db/scratchpad")
from live import Client

WB = sys.argv[1]; N = int(sys.argv[2])
c = Client(); assert c.login("anvitech", "adm-pass-1") == 200
# a clean book each time
print("upload:", c.upload("/upload", WB)[0])


def iso(s):
    return dt.date.fromisoformat(str(s)[:10]).isoformat() if s else None


def dates():
    st, r = c.post("/run", {})
    return {tuple(k.split("\x1f")): iso(v) for k, v in r["expected_end"].items()}


st, d = c.get("/new-orders/drafts")
items = [i["item_code"] for i in d["items"]]
prev = dates()
base_n = len(prev)
added, ex_moves, new_moves, worst = {}, 0, 0, 0
for i in range(N):
    so, ic, qty = f"26-27SO7{i:02d}", items[(i * 13) % len(items)], 50 + i * 30
    assert c.put("/new-orders/drafts", {"drafts": [{"so_no": so, "item_code": ic, "qty": qty}]})[0] == 200
    st, q = c.post("/new-orders/quote")
    if st != 200 or not q["verified"]:
        print("   *** QUOTE NOT VERIFIED ***", q.get("moved"), st); break
    quoted = iso(q["lines"][0]["completion"])
    assert c.post("/new-orders/add", {"stamp": q["stamp"]})[0] == 200
    after = dates()
    added[(so, ic)] = quoted
    msgs = []
    for k in prev:
        if after.get(k) != prev[k]:
            days = (dt.date.fromisoformat(after[k]) - dt.date.fromisoformat(prev[k])).days
            worst = max(worst, abs(days))
            if k in added:
                new_moves += 1
                msgs.append(f"ALREADY-ACCEPTED {k[0]} {prev[k]}->{after[k]} ({days:+d}d)")
            else:
                ex_moves += 1
                msgs.append(f"PRE-EXISTING {k[0]} {prev[k]}->{after[k]} ({days:+d}d)")
    print(f"  #{i+1:>2} {so}/{ic} qty={qty} quoted={quoted} shown={after.get((so,ic))} "
          f"| moves={len(msgs)}  {'; '.join(msgs[:2])}")
    prev = after
print(f"{os.path.basename(WB)}: {N} adds over a {base_n}-order book -> "
      f"pre-existing orders moved {ex_moves} time(s), already-accepted new orders "
      f"moved {new_moves} time(s), worst shift {worst} days")
```

### `xsurface.py`

```python
"""CHECK 8, clean: do the seven surfaces agree while the queue is non-empty?"""
import sys, os, collections, datetime as dt
os.environ["LIVE_BASE"] = os.environ.get("PORTBASE", "http://127.0.0.1:8793")
sys.path.insert(0, "/private/tmp/claude-501/-Users-ritinwadekar-Desktop-Anvitech-Rebuilt/55659fb2-182e-4b1b-b986-a17d39f8c2db/scratchpad")
from live import Client
c = Client(); assert c.login("anvitech", "adm-pass-1") == 200


def iso(s):
    s = str(s)
    for f in ("%Y-%m-%d", "%d-%m-%Y"):
        try:
            return dt.datetime.strptime(s, f).date().isoformat()
        except ValueError:
            pass
    return s


st, r0 = c.post("/run", {})
cfg = dict(r0["config"]); cfg["apply_operator_logic"] = True
st, r = c.post("/run", {"config": cfg, "persist": True})
q = c.get("/new-orders/drafts")[1]["queued"]
print(f"queued: {len(q)}   queue_note: {r.get('queue_note')!r}")
od = {tuple(k.split("\x1f")): iso(v) for k, v in r["expected_end"].items()}
r6 = r["trace"]["rule6"]

def tdates(t, so="SO No", ic="Item Code", dc="Expected completion"):
    ci = {n: i for i, n in enumerate(t["columns"])}
    out = collections.defaultdict(set)
    for row in t["rows"]:
        for s in str(row[ci[so]]).split(","):
            if s.strip():
                out[(s.strip(), str(row[ci[ic]]))].add(iso(row[ci[dc]]))
    return out

g = collections.defaultdict(set)
extra_bars = 0
for row in r["gantt"]["rows"]:
    for s in (row["so_no"] or "").split(","):
        if s.strip():
            g[(s.strip(), row["item_code"])].add(iso(row["completion"]))
surfaces = {"Gantt": g,
            "Machine timeline (Schedule tab)": tdates(r6["tables"][0]["table"]),
            "Shift-wise export": tdates(r6["shiftwise"])}
for name, s in surfaces.items():
    dis = [(k, od[k], sorted(s[k])) for k in od if k in s and {od[k]} != s[k]]
    miss = [k for k in od if k not in s]
    qm = [k for k in miss if list(k) in [list(x) for x in q]]
    worst = 0
    for k, a, b in dis:
        for x in b:
            try:
                worst = max(worst, abs((dt.date.fromisoformat(x) - dt.date.fromisoformat(a)).days))
            except Exception:
                pass
    print(f"Orders tab vs {name}: {len(dis)} of {len(od)} DISAGREE (worst {worst} days), "
          f"{len(miss)} absent ({len(qm)} of them queued new orders)")
    for k, a, b in sorted(dis)[:5]:
        print(f"    {k[0]}/{k[1]}: orders={a}  {name}={b[0]}")
print("shift-wise rows:", len(r6["shiftwise"]["rows"]))
rep = r["report"]
print("validation report rows:", len(rep["rows"]), rep["columns"])
for row in rep["rows"][:5]:
    print("   ", row)
st, b = c.get("/delay-report.xlsx")
print("delay report:", st, len(b) if isinstance(b, bytes) else b)
an = r["trace"].get("analytics")
print("analytics makespan_days:", (an or {}).get("headline", {}).get("makespan_days"))
```

### `repro_verified.py`

```python
"""Reproduce `verified: False` from /new-orders/quote: fresh server, fresh store,
upload in-process, then add orders one at a time. Prints the first failing quote."""
import sys, os
os.environ["LIVE_BASE"] = "http://127.0.0.1:8793"
sys.path.insert(0, "/private/tmp/claude-501/-Users-ritinwadekar-Desktop-Anvitech-Rebuilt/55659fb2-182e-4b1b-b986-a17d39f8c2db/scratchpad")
from live import Client
WB, N = sys.argv[1], int(sys.argv[2])
c = Client(); assert c.login("anvitech", "adm-pass-1") == 200
print("upload:", c.upload("/upload", WB)[0])
items = [i["item_code"] for i in c.get("/new-orders/drafts")[1]["items"]]
for i in range(N):
    so, ic, qty = f"26-27SO7{i:02d}", items[(i * 13) % len(items)], 50 + i * 30
    c.put("/new-orders/drafts", {"drafts": [{"so_no": so, "item_code": ic, "qty": qty}]})
    st, q = c.post("/new-orders/quote")
    print(f"  quote #{i+1} {so}/{ic}: verified={q['verified']} completion={q['lines'][0]['completion']}")
    if not q["verified"]:
        print("  -> the endpoint refuses to show a date; `violations` is NOT in the response body:",
              sorted(q.keys()))
        break
    st, a = c.post("/new-orders/add", {"stamp": q["stamp"]})
    print(f"     add -> {st}")
```

### `mutrun.sh`

```python
#!/bin/bash
R="/Users/ritinwadekar/Desktop/Anvitech Rebuilt/.worktrees/add-new-orders"
SP="/private/tmp/claude-501/-Users-ritinwadekar-Desktop-Anvitech-Rebuilt/55659fb2-182e-4b1b-b986-a17d39f8c2db/scratchpad"
cd "$R" || exit 1
for m in "$@"; do
  git checkout -- ppc_engine engine api web tests 2>/dev/null
  if ! python3.12 "$SP/mut/$m.py"; then echo "### $m: ANCHOR MISSING"; continue; fi
  PYTHONPATH="$R" python3.12 -m pytest -q > "$SP/mut_$m.log" 2>&1
  echo "### $m -> $(grep -E '^[0-9]+ (passed|failed)|passed,|failed,' "$SP/mut_$m.log" | tail -1)"
  grep -E "^FAILED" "$SP/mut_$m.log" | head -10 | sed 's/^/     /'
  git checkout -- ppc_engine engine api web tests 2>/dev/null
done
```

### the eight mutations (`$SP/mut/m1.py` … `m8.py`)

```python
# ---------- m1 ----------
# 1. the stop line in _lay_on_machine: drop the deadline clamp entirely.
p = "ppc_engine/scheduler/flow_scheduler.py"
s = open(p).read()
a = """        if deadline is not None and win.start >= deadline:
            return None  # out of room in this stretch; the caller tries the next one

        seg_start = max(cursor, win.start)
        win_end = win.end if deadline is None else min(win.end, deadline)"""
b = """        seg_start = max(cursor, win.start)
        win_end = win.end"""
assert a in s, "m1 anchor missing"
open(p, "w").write(s.replace(a, b))

# ---------- m2 ----------
# 2. the free-run walk in _place_operation: call _lay_on_machine directly.
p = "ppc_engine/scheduler/flow_scheduler.py"
s = open(p).read()
a = """        laid = _lay_in_free_run(machine, earliest, dur, order, op, int(op_qty),
                                staffing, masters, config)"""
b = """        laid = _lay_on_machine(machine, earliest, dur, order, op, int(op_qty),
                               staffing, masters, config)"""
assert a in s, "m2 anchor missing"
open(p, "w").write(s.replace(a, b))

# ---------- m3 ----------
# 3. the operator seeding in decode: booked=None.
p = "ppc_engine/scheduler/flow_scheduler.py"
s = open(p).read()
a = "                             booked=masters.calendar.operator_busy,"
b = "                             booked=None,"
assert a in s, "m3 anchor missing"
open(p, "w").write(s.replace(a, b))

# ---------- m4 ----------
# 4. the assignment seeding in decode: assigned=None.
p = "ppc_engine/scheduler/flow_scheduler.py"
s = open(p).read()
a = "                             assigned=masters.calendar.machine_shift_operator)"
b = "                             assigned=None)"
assert a in s, "m4 anchor missing"
open(p, "w").write(s.replace(a, b))

# ---------- m5 ----------
# 5. skipping OFF_LANES in occupancy_from_entries.
p = "engine/new_engine.py"
s = open(p).read()
a = "        if e.machine in OFF_LANES or e.end <= e.start:"
b = "        if e.end <= e.start:"
assert a in s, "m5 anchor missing"
open(p, "w").write(s.replace(a, b))

# ---------- m6 ----------
# 6. the queue split in _plan: plan everything in one stage.
p = "api/main.py"
s = open(p).read()
a = """    stage1 = [l for l in so_lines if l.key not in queued_keys]
    note = (f"{len(queued_lines)} new order(s) are planned behind the existing "
            f"book until the next plan update.")
    return stage1, queued_lines, note"""
b = """    note = (f"{len(queued_lines)} new order(s) are planned behind the existing "
            f"book until the next plan update.")
    return so_lines, [], note"""
assert a in s, "m6 anchor missing"
open(p, "w").write(s.replace(a, b))

# ---------- m7 ----------
# 7. the queue in _plan_fingerprint.
p = "api/main.py"
s = open(p).read()
a = '        "queue": book_store.load_new_order_queue(),'
assert a in s, "m7 anchor missing"
open(p, "w").write(s.replace(a, "", 1))

# ---------- m8 ----------
# 8. the stamp check in /new-orders/add.
p = "api/main.py"
s = open(p).read()
a = """    if req.stamp != _plan_fingerprint(config):
        raise HTTPException(
            status_code=409,
            detail=("The plan has changed since this quote. "
                    "Press Finish and Optimize again to get a fresh date."))"""
assert a in s, "m8 anchor missing"
open(p, "w").write(s.replace(a, "    pass"))

```
