# Profit and Loss — verification record (2026-11-11)

Evidence behind the CLAUDE.md banner entry of the same date (spec
`2026-11-11-profit-and-loss-design.md`, section 13). Every number below came from the
harnesses in `2026-11-11-profit-and-loss-harness/`, so the next person re-runs them
instead of trusting this page.

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
feature branch (`3b66f17`), feature branch with `_snapshot_pnl` monkeypatched to a
no-op, and the base commit `518b11c` run from a temporary `git worktree` (removed
afterwards).

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

**24 of 24 hashes identical.** The punches move the plan (as they should), identically
in all three. Seeding the rates moves nothing: `rate` is not in
`shop_masters.machines_digest`, so the plan fingerprint does not see it.

## Step 1b to 2: `verify_pnl.py`, Test9 and Test8, all checks pass

| Check | Test9 | Test8 |
|---|---|---|
| A1 snapshot == active orders with zero punches and a counted step | 64 == 64 (68 active, 3 punched, 1 with no counted step) | 61 == 61 (67, 3, 3) |
| A2 orders punched BEFORE the first snapshot are absent | 3 of 3 absent | 3 of 3 absent |
| B1 clubbed ops: members' setup + run == op setup + Q x ORIGINAL cycle | **28 ops, 0 mismatches** | **31 ops, 0 mismatches** |
| B2 op setup the plan paid | 90 min on 18 CNC/VMC ops, 0 on 10 inspection ops | 21 / 10 |
| C1 orders punched AFTER the first snapshot keep it byte-for-byte | 3 of 3 (the plan's figure for all 3 had changed since, so the freeze is not vacuous) | 3 of 3 (same) |
| C2 unpunched orders refreshed on the next compute | 61 of 61 | 58 of 58 |
| D0-D3 one order end to end (below) | pass | pass |
| E1 `/profit-loss` 403 for the user role | pass | pass |
| E2 `/machines` carries no `rate` for the user role, does for admin | pass | pass |

B1 is computed from the plan's own entries (occupancy grouped per batch op, setup =
occupancy minus Q x the planning cycle time) against the snapshot's member rows;
members already punched are skipped (they are not snapshotted, by design).

### Step 2: one order end to end, checked by hand

Order `26-27SO131 / 8010005735` (HUB SMALL END, qty 15, 11 routing steps; the same
order on both books). Every routing step punched through `POST /actuals` with
machine, shift, `shift_minutes` and operator (OS steps with none), each step on its
own past date so no two steps share a machine-shift; the last punch marks it complete.

Routing (Excel cycle, minutes/piece): CNC FIRST 156, CNC SECOND 156, VMC FIRST 312,
VMC SECOND 132, CNC THIRD 72, CNC FORTH 72, CHAMFER 60 (manual, not counted), CMM 60,
WASHING / PACKING (manual), DISPATCH.

- **Planned, by hand from the routing alone:** each CNC/VMC step 90 min setup + 15 x
  cycle, CMM 15 x 60 and no setup: 40.5 + 40.5 + 79.5 + 34.5 + 19.5 + 19.5 + 15.0 =
  **249.0 h**. The report: **249.0 h**, Rs 1,64,850 (CNC3 81 h and VMC3 114 h at 650,
  CNC6 39 h at 650, CMM 15 h at 850). Unpadded: with the 1.3 pad it would read 316.5 h
  (measured under the unpad mutation below).
- **Actual, by hand:** CNC FIRST SIDE was punched as TWO entries on the same CNC3
  1st shift, 400 + 400 minutes (> 630 working), qty 7 with 90 min setup and qty 8
  with 30 min tool-problem downtime. The shift is shared by pieces x cycle + setup:
  630 x 1182/2430 + (630 x 1248/2430 - 30) = **600 min = 10.0 h**. Every other
  counted step: 300 min (CNC/VMC) or 120 min (CMM), 2nd shift, 15 min no-power each:
  5 x 285 + 105 = 1,530 min. Total **35.5 h, Rs 23,425**; the report: **35.5 h,
  Rs 23,425**, difference Rs -1,41,425 (-85.79 %). (The actual times are invented by
  the harness, so the large negative difference means nothing about the shop.)
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
- Cosmetic, not fixed: a negative rupee figure renders as `₹-1,41,425` (sign after
  the symbol), and the totals label says "1 orders".

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

The real-book harness was mutated too, so it is not vacuous: zero-punch gate removed
-> A1, A2, C1, C2 fail; unpad reverted -> B1 (18 of 28 clubbed ops) and D2b (316.5 h vs
249.0 h) fail; downtime deduction removed -> D2 fails. Honest gap: D2 alone recomputes
the PLANNED side from the snapshot, so it cannot see the unpad; D2b (from the routing)
and B1 are what catch it.

## Suite

**1500 passed, 5 skipped** (`.venv/bin/python -B -m pytest`, 7 min). An earlier run made while the harnesses and a browser were also running had one failure, `test_auto_optimize.py::test_auto_contest_applies_only_when_strictly_better`; it passes alone (18/18, on this branch and on base `518b11c`) and in the clean re-run, so it reads as load-sensitive flakiness, not this feature, but it is recorded here rather than dropped.
