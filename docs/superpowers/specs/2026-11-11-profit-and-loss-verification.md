# Profit and Loss — verification record (2026-11-11)

Evidence behind the CLAUDE.md banner entry of the same date (spec
`2026-11-11-profit-and-loss-design.md`, section 13). Every number below came from the
harnesses in `2026-11-11-profit-and-loss-harness/`, so the next person re-runs them
instead of trusting this page.

## Final review correction, said first (2026-11-11)

The first version of this page, and of `verify_pnl.py`, punched hand-typed minutes
(60, 120, 300, 400) into "minutes in shift". **The real Daily Entry form cannot
produce those**: the box is READ-ONLY, always 630 (1st shift) or 570 (2nd shift), by
the owner's 2026-10-07 rule, and OS / DISPATCH steps are saved with a blank machine.
I had also told the owner the operator types the time on the job; that was wrong.
Re-run with REAL-FORM punches, the first version's blank-machine rule hid **every
finished order** (Test9 62 of 62, Test8 61 of 61), because every order ends in a
DISPATCH punch with no machine. The numbers below are from the re-run after the fix
wave; the SO131 figures of the first version (35.5 actual h, Rs 23,425) were built
from minutes the form cannot produce and are superseded. The owner chose, on
2026-11-11, that actual hours are a share of the machine-shift (spec section 7).

## What was NOT done, said first

- **The live-store copy was NOT made.** The owner's `MONGODB_URI` was not available in
  this session. Every check below ran on a throwaway LOCAL store built from the owner's
  workbooks (Test9.xlsx and Test8.xlsx). The live book has real punches, clubbed
  in-production batches, absences and an applied optimization that these stores do
  not. **Before pushing, repeat Step 1 on a read-only copy of the live store**
  (method: memory `reading-live-anvitech-data`; copy every key to a local store,
  nothing written back), with `plan_hash.py` and `verify_pnl.py` pointed at the copy.
- The snapshot only ever gets written for orders with NO punches. On the live book
  most orders already have punches, so on the first deploy the report starts EMPTY
  and fills only as new (unpunched) orders finish. That is the design (spec section 6),
  but the owner should hear it before he opens the tab.
- The cloud / GitHub / Oracle paths were not touched by this feature and were not run.

## Inputs and method

- Stores: `build_store.py` writes the workbook bytes, every SO line of its SO sheet
  as an active order (the Excel upload is masters-only since 2026-10-03, so orders
  are put in the store directly, as the 2026-09-22 harness did) and a
  production-like config: new engine, operator logic on, overlap 88, consolidation
  window 1 day. Test9: 68 orders, 26 machines. Test8: 67 orders, 26 machines.
- Every harness copies the store into a scratch directory, removes `MONGODB_URI` /
  Upstash / cloud-optimize variables, sets throwaway login env vars, drives the REAL
  API (FastAPI `TestClient` on `api.main`) and fixes the IST clock at 11-11-2026 07:00.
  The schedule is read from `_PLAN_CACHE["artifacts"]`, the object `/run` was built
  from, and the plan cache is cleared before every `/run` so each one recomputes.

```
S=<scratch>
.venv/bin/python -B docs/superpowers/specs/2026-11-11-profit-and-loss-harness/build_store.py "<Test9.xlsx>" $S/store-t9
DEFAULT_SCHEDULER=new .venv/bin/python -B docs/superpowers/specs/2026-11-11-profit-and-loss-harness/plan_hash.py $S/store-t9 [--noop]
DEFAULT_SCHEDULER=new .venv/bin/python -B docs/superpowers/specs/2026-11-11-profit-and-loss-harness/verify_pnl.py $S/store-t9
.venv/bin/python -B docs/superpowers/specs/2026-11-11-profit-and-loss-harness/mutation_sweep.py
```

## Step 1a: the plan is identical with and without the feature

`plan_hash.py` hashes every schedule entry (batch, step, machine, operator, every
per-shift operator segment, start, end, qty, SO refs) at four stages: first `/run`;
after `GET /machines` (which seeds the default rates into the machines doc on the
feature branch); after 3 punches through `POST /actuals`; after 3 more. Three variants:
feature branch, feature branch with `_snapshot_pnl` monkeypatched to a no-op, and the
base commit `518b11c` run from a temporary `git worktree` (removed afterwards). First
run on `3b66f17`; RE-RUN at the final review fix wave (`9b12c4a` code) with the
punches changed to the form's fixed 630 minutes: same hashes, below.

| Book | Stage | HEAD | HEAD, snapshot no-op | base 518b11c | entries |
|---|---|---|---|---|---|
| Test9 | first /run | a479dfb5eeb10fb1 | same | same | 426 |
| Test9 | after GET /machines | a479dfb5eeb10fb1 | same | same | 426 |
| Test9 | after 3 punches | ed24d5b10b5468f0 | same | same | 426 |
| Test9 | after 6 punches | 91dddb0343e868e3 | same | same | 426 |
| Test8 | first /run | 1ba536c3db49deb6 | same | same | 415 |
| Test8 | after GET /machines | 1ba536c3db49deb6 | same | same | 415 |
| Test8 | after 3 punches | 740cf9e2f7500ea7 | same | same | 414 |
| Test8 | after 6 punches | 0ccb7c2f0338285b | same | same | 414 |

**24 of 24 hashes identical, on both runs** (the minutes in a punch are not a plan
input, so the real-form re-run gives the very same hashes). The punches move the plan (as they should), identically
in all three. Seeding the rates moves nothing: `rate` is not in
`shop_masters.machines_digest`, so the plan fingerprint does not see it.

## Step 1b to 2: `verify_pnl.py`, Test9 and Test8, all checks pass

| Check | Test9 | Test8 |
|---|---|---|
| A1 snapshot == active, zero-punch orders in the plan | 65 == 65 (68 active, 3 punched) | 64 == 64 (67, 3) |
| A1b the orders with no counted step have an EMPTY snapshot (not "older") | 1 (SO84 / 2109801, outsourced only) | 3 (SO129 x 2, SO84) |
| A2 orders punched BEFORE the first snapshot are absent | 3 of 3 absent | 3 of 3 absent |
| B1 clubbed ops: members' setup + run == op setup + Q x ORIGINAL cycle | **28 ops, 0 mismatches** | **31 ops, 0 mismatches** |
| B2 op setup the plan paid | 90 min on 18 CNC/VMC ops, 0 on 10 inspection ops | 21 / 10 |
| C1 orders punched AFTER the first snapshot keep it byte-for-byte | 3 of 3 (the plan's figure for all 3 had changed since, so the freeze is not vacuous) | 3 of 3 (same) |
| C2 unpunched orders: re-stamped only when their plan moved, else untouched | 0 moved, 62 kept | 0 moved, 61 kept |
| D0-D3 one order end to end (below) | pass | pass |
| D4 that order has a blank-machine DISPATCH punch, and the OLD rule hides it | pass | pass |
| F1 every other unstarted order finished with real-form punches: row or "no counted work" | 62 finished: 61 rows, 1 not shown (no CNC, VMC or inspection work), 0 older | 61 finished: 58 rows, 3 not shown (same reason), 0 older |
| F2 the same punches under the OLD blank-machine rule | 0 rows, 62 not shown | 0 rows, 61 not shown |
| E1 `/profit-loss` 403 for the user role | pass | pass |
| E2 `/machines` carries no `rate` for the user role, does for admin | pass | pass |

C2 changed meaning at the fix wave: the snapshot now keeps an unchanged entry (and its
stamp) instead of re-stamping it on every plan. On these books no unpunched order's
figure moved between the two `/run`s, so the "re-stamp when moved" half is exercised
by the unit / API tests (`test_snapshot_freezes_at_first_punch`), not here.

F1 detail: every one of the finished orders has at least one blank-machine punch
(DISPATCH, OS). Of the rows, 56 (Test9) / 54 (Test8) carry a PARTIAL cost, because
MI1/MI2/MI3 have no rate yet; only 5 / 4 rows are complete and in the totals.

B1 is computed from the plan's own entries (occupancy grouped per batch op, setup =
occupancy minus Q x the planning cycle time) against the snapshot's member rows;
members already punched are skipped (they are not snapshotted, by design).

### Step 2: one order end to end, checked by hand

Order `26-27SO131 / 8010005735` (HUB SMALL END, qty 15, 11 routing steps; the same
order on both books). Every routing step punched through `POST /actuals` as the real
form saves it: 630 (1st shift) / 570 (2nd shift) minutes, the form's default machine
(blank on DISPATCH), operator (OS steps with none), each step on its own past date so
no two steps share a machine-shift; the last punch marks it complete.

Routing (Excel cycle, minutes/piece): CNC FIRST 156, CNC SECOND 156, VMC FIRST 312,
VMC SECOND 132, CNC THIRD 72, CNC FORTH 72, CHAMFER 60 (manual, not counted), CMM 60,
WASHING / PACKING (manual), DISPATCH.

- **Planned, by hand from the routing alone:** each CNC/VMC step 90 min setup + 15 x
  cycle, CMM 15 x 60 and no setup: 40.5 + 40.5 + 79.5 + 34.5 + 19.5 + 19.5 + 15.0 =
  **249.0 h**. The report: **249.0 h**, Rs 1,64,850 (CNC3 81 h and VMC3 114 h at 650,
  CNC6 39 h at 650, CMM 15 h at 850). Unpadded: with the 1.3 pad it would read 316.5 h
  (measured under the unpad mutation below).
- **Actual, by hand (real-form re-run):** CNC FIRST SIDE was punched as TWO
  entries on the same CNC3 1st shift, 630 + 630 minutes (> 630 working), qty 7 with
  90 min setup and qty 8 with 30 min tool-problem downtime. The shift is shared by
  pieces x cycle + setup and the shares sum to 630: 630 - 30 = **600 min = 10.0 h**.
  Every other counted step: one 2nd-shift punch, 570 minutes, 15 min no-power: 6 x
  555 = 3,330 min. Total **65.5 h, Rs 44,425**; the report: **65.5 h, Rs 44,425**,
  difference Rs -1,20,425 (-73.05 %). Manual steps (630 min each) and DISPATCH (blank
  machine, 630 min) are not counted and do not hide the order. (Harness-invented
  production; the negative difference means nothing about the shop. The first
  version's 35.5 h / Rs 23,425 used typed minutes the form cannot produce.)
- D1: after completion the order leaves the plan; its snapshot is unchanged on the
  next `/run`. D3: totals show 1 order, 1 counted, 0 incomplete.
- Seen on the way, correct behaviour, worth knowing: in the first version of the
  harness all punches were dated the same day, and a phase-A punch of a DIFFERENT
  order on CNC3 1st shift (60 min) joined that shift's group, so CNC FIRST SIDE read
  8.97 h instead of 10.0 h. The overbooked-shift share works across orders, as
  designed: a report row can move when another order is punched on the same
  machine-shift.

### Browser (throwaway uvicorn on port 8791, the Test9 end-state store)

- Admin: the `Profit and Loss` tab lists the one order with the numbers above;
  clicking the row opens the per-step table (planned machine, planned hrs / cost,
  actual machine, actual hrs / cost); totals line "Totals (1 orders, 0 incomplete)".
  No console errors.
- User: no `Profit and Loss` tab in the nav, and `#profitloss` lands on Orders.
- Cosmetic, FIXED at the fix wave: a negative rupee figure now reads `-₹1,41,425`,
  and the totals label "1 order". Checked by evaluating the page's own helper
  functions in node, not in a browser (no browser re-run at the fix wave).

## Step 3: mutation tests, 10 of 10 load-bearing

`mutation_sweep.py`: each part reverted on its own, the feature's tests run
(`test_profit_loss.py`, `test_profit_loss_api.py`, `test_shop_masters.py`,
`test_role_parity.py`) with `python -B` and a 1.1 s sleep after every write, file
restored byte-for-byte (`git diff` clean afterwards).

| Mutation | Fails |
|---|---|
| zero-punch gate in `merge_snapshot` (freeze at first punch) | `test_merge_never_touches_a_started_order` |
| unpad: `run = pieces x Co` -> occupancy share (padded) | `test_single_line_cnc_step_unpads_run_and_keeps_setup` |
| downtime deduction | `test_minutes_are_shift_minutes_minus_downtime_with_setup_kept` |
| overbooked-shift scaling | `test_two_default_punches_on_one_machine_share_the_shift` |
| counted-kind filter, planned side | `test_manual_step_is_not_counted` |
| counted-kind filter, actual side | `test_manual_machine_punch_is_not_counted` |
| `require_admin` on `GET /profit-loss` | `test_profit_loss_is_admin_only` |
| `model_fields_set` keep-rate rule | `test_save_without_rate_keeps_the_stored_rate` |
| user-role rate strip on `/machines` | `test_user_role_never_sees_rates` |
| `_snapshot_pnl` call in `_plan` | `test_plan_writes_a_snapshot_for_unstarted_orders` |

Fix wave mutations (each reverted on its own, `python -B`, file restored):

| Mutation | Fails |
|---|---|
| blank-machine punch flagged on ANY step (counted-step check removed) | 4 engine tests + `test_manual_only_order_is_...` + `test_real_form_punches_show_the_order` |
| no empty snapshot for an order with no counted step | `test_planned_snapshot_records_...`, `test_manual_only_order_is_...` |
| `merge_snapshot` re-stamps unchanged steps | `test_merge_keeps_the_stamp_...`, `test_an_unchanged_snapshot_is_not_rewritten_...` |
| clubbed share by ordered qty instead of the per-step `pq` | `test_clubbed_share_follows_each_lines_step_qty` |
| overbooked weight prefers the punch's cycle over the master's | `test_overbooked_weight_uses_master_cycle_and_actual_setup` |
| overbooked weight without the actual setup | same test |
| difference shown between partial costs | `test_partial_cost_sums_...`, `test_end_to_end_row`, `test_real_form_punches_show_the_order` |
| totals 0 instead of None when no row is complete | `test_totals_are_none_when_no_order_is_complete` |

The real-book harness was mutated too, so it is not vacuous: zero-punch gate removed
-> A1, A2, C1, C2 fail; unpad reverted -> B1 (18 of 28 clubbed ops) and D2b (316.5 h vs
249.0 h) fail; downtime deduction removed -> D2 fails. Honest gap: D2 alone recomputes
the PLANNED side from the snapshot, so it cannot see the unpad; D2b (from the routing)
and B1 are what catch it.

## Suite

**1500 passed, 5 skipped** at the first version (7 min); **1521 passed, 5 skipped** after the final review fix wave (`.venv/bin/python -B -m pytest`, 6 min). An earlier run made while the harnesses and a browser were also running had one failure, `test_auto_optimize.py::test_auto_contest_applies_only_when_strictly_better`; it passes alone (18/18, on this branch and on base `518b11c`) and in the clean re-run, so it reads as load-sensitive flakiness, not this feature, but it is recorded here rather than dropped.
