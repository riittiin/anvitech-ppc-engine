# Item Process Master: verification (Task 10)

**Date:** 2026-10-05 · **Branch:** `item-process-master` (code HEAD `43bfabd` after
the final fix wave, section 8; Task 10 itself ran at `987c1ba`; from `origin/main` @
`c123fa1`) · **Status:** implemented, unpushed.
Spec: `docs/superpowers/specs/2026-10-05-item-process-master-tab-design.md` (section 10 is
the contract this document answers).

Everything below was run on a local machine, `python3.12`, against throwaway temp
`STORE_DIR` stores. The live site and the live database were not touched. The real
workbooks were read only. Harness sources are at the end, verbatim.

## Summary

| Spec 10 item | Result |
|---|---|
| 1. Byte-identical switch, real books | **16 of 16 schedule hashes identical** (Test5, Test8, Test9, Test9-ORIGINAL-times x flexible False/True x clean / WIP with a derived frozen set). ppc routings identical 16/16. Classic routings differ ONLY in the four fields the spec drops (identical 4/4 once those are blanked). Two-line `/new-orders/quote` identical on all 4 books. |
| 1. Live-store copy | **Not run** (no credential this session); owner to run before deploy. |
| 2. Every reader switched | **All moved** on one table edit, workbook bytes unchanged: `/run` schedule, Gantt, shift-wise, delay report, production analysis, `/items`, quote, cloud payload round-trip (classic and worker ppc masters). |
| 3. Upload no longer reaches routings | Upload of a doctored Test9 (one cycle cell x3+7, one item row blanked): **plan hash unchanged**, table digest unchanged. |
| 4. Safety rules | rename / remove punched step: 400 with the message; cycle change: 200; delete in-use item: 400; stale version: 409. Moving a punched step below an unpunched one, or a new step in front of it, was accepted at Task 10 (Findings F2); **refused since the final fix wave** (section 8). |
| 5. Mutation testing | 9 brief mutations: **9 of 9 load-bearing** on the four item-master files. #9 `_keep_blank_as_stored` first failed NO test (whole suite included) although it is load-bearing on the real book; closed by the new `test_resaving_unchanged_item_as_the_browser_sends_it_keeps_digest` (F3). Extras: #10 caught only by the full suite, #11 caught. |
| 6. Browser run | **Not run in Task 10.** |
| 7. Full suite | After the final fix wave: **1 failed, 1233 passed, 4 skipped (78.5 s)**. The one failure is the pre-existing `test_production_analysis.py::test_monthly_report_json_and_excel` (`xlsxwriter` not installed locally; present at base). |
| Cache-hit `/run` cost | Test9: base **38.2 / 39.0 / 38.2 ms**, feature **43.6 / 42.9 / 43.5 ms** (median of 30, three rounds): about **+5 ms (+13%)** per cache hit on the local file store. |

## 1. Byte-identical switch

**Method.** `verify_switch.py` (below) runs each (book, flexible, wip) in a fresh
subprocess with its own temp store, in the feature worktree and in a pristine
worktree of `c123fa1`. Production-like config: `DEFAULT_SCHEDULER=new`,
`apply_operator_logic=True`, overlap 80, plan start fixed at 06-10-2026. The book is
the workbook's own SO sheet (`Order(...)` per SO line). WIP leg: plan once, store that
schedule as the last-applied plan (`freeze.schedule_projection`), punch half of step 1
on 30 orders (`book_store.append_actual`, entry date 05-10-2026), derive and store the
frozen set with `api.main._compute_and_store_frozen()` exactly as "Done entering" does,
then plan again. Hash = sha256 of sorted (batch, seq, machine, operator, start, end, qty)
over every entry. Also hashed: classic routings (`_current_masters().routings`) and ppc
routings (`new_engine._new_masters(flexible).routings`), both in dict order. The
feature run asserts the table was seeded (`seeded=yes(N items)`); the baseline has no
table (`n/a`), which is the point.

**Books:** Test5, Test8, Test9 and `Test9 (5) (1) (1) (1) - ORIGINAL times.xlsx` (the
master the owner is about to upload, original cycle times), all from
`~/Desktop/Anvitech Rebuilt/`, read only.

| book | flexible | wip | entries | frozen | schedule hash base | feature | ppc routings |
|---|---|---|---|---|---|---|---|
| Test5 | F | clean | 349 | 0 | 8039b77bc91caf2c | same | same |
| Test5 | F | WIP | 348 | 17 | e67cc2c9662c0f8f | same | same |
| Test5 | T | clean | 357 | 0 | 4226723695322de1 | same | same |
| Test5 | T | WIP | 348 | 17 | 5a55f151ee835e61 | same | same |
| Test8 | F | clean | 412 | 0 | bebc363c9a620afe | same | same |
| Test8 | F | WIP | 412 | 19 | cef90f2f50b9d1c6 | same | same |
| Test8 | T | clean | 412 | 0 | fe467c1b6b13aba2 | same | same |
| Test8 | T | WIP | 411 | 19 | ffbe22df97a1077f | same | same |
| Test9 | F | clean | 428 | 0 | 3cd8b2884343b360 | same | same |
| Test9 | F | WIP | 426 | 10 | 95cbb28f49bb624f | same | same |
| Test9 | T | clean | 428 | 0 | 62e6e9a1ed9ead4c | same | same |
| Test9 | T | WIP | 424 | 10 | c91e287222af70e5 | same | same |
| Test9-ORIGINAL | F | clean | 481 | 0 | 3cb85e6c720bfde2 | same | same |
| Test9-ORIGINAL | F | WIP | 474 | 13 | a981602f3c9ac5ea | same | same |
| Test9-ORIGINAL | T | clean | 474 | 0 | a793c8fbbb803228 | same | same |
| Test9-ORIGINAL | T | WIP | 476 | 13 | 9cffc0a4889f9268 | same | same |

Seeded table sizes: Test5 98 items, Test8 104, Test9 106, Test9-ORIGINAL 108.

**Classic routings: the raw repr hash DIFFERS on all four books** (e.g. Test9 base
`36eaada688d410c1`, feature `75f3f1dad51d7254`). Investigated (`classic_neutral.py`):
item order identical (106 = 106 on Test9), and the only fields that differ are the four
the spec (section 3) drops on purpose: `Routing.customer`, `Routing.rm_type`,
`Routing.moq`, `Process.total_time`. With those four blanked on both sides the hashes
are equal on every book:

| book | items | steps | neutral hash base = feature | values the base carried (customer / rm_type / moq / total_time) |
|---|---|---|---|---|
| Test5 | 98 | 802 | e3d366173a59780d | 98 / 95 / 96 / 637 |
| Test8 | 104 | 835 | edddfa0eb2ac68d1 | 104 / 101 / 96 / 660 |
| Test9 | 106 | 851 | 75f3f1dad51d7254 | 106 / 102 / 96 / 670 |
| Test9-ORIGINAL | 108 | 869 | bb197cd37577f85f | 108 / 105 / 97 / 686 |

Readers of those four fields, checked in code: none outside the loader and the models'
`as_row()`; no routing or process object is ever passed to `pipeline.to_table`, so no
screen shows them. Intended by the spec, recorded so nobody re-discovers it as a bug.
No book has a blank step in the middle of a routing (step counts equal, ppc hashes
equal).

**Quote, base vs feature** (`quote_compare.py`, two draft lines on each book):
identical on all four books, `moved 0, violations 0, verified True` on both sides:
Test5 QT1 01-12-2026 / QT2 18-12-2026; Test8 13-12-2026 / 23-04-2027; Test9
25-11-2026 / 23-11-2026; Test9-ORIGINAL 28-11-2026 / 30-11-2026.

**Live-store copy: not run (no credential this session); owner to run before
deploy.** That includes the quote on the store copy.

## 2. Every reader moves (feature only, Test9)

`verify_readers.py`: throwaway in-process instance (TestClient, temp store), Test9, the
production-like config saved as the plan config, the table seeded by the first
`GET /item-master`. Target (first order with a CNC step after step 1): **26-27SO85 /
9611443650, step 2 `CNC SECOND SIDE` on CNC1/CNC4, 600 pieces**. One punch on that step
dated 05-10-2026 so production analysis has a row. Then `PUT /item-master` (admin)
changed that step's cycle **3.3 -> 11.6 min** (200). Stored workbook sha
`58641ee7d70315a3` before and after: **the workbook was not touched.**

| reader | before | after |
|---|---|---|
| `/run` schedule hash | 1abb3ad34443 | d2a8a54a118f |
| `/run` target step machine minutes (SO85's batch) | 3,139.7 | 12,452.9 |
| `/run` target step end | 27-10 12:19 | 18-10 10:32 |
| expected completion (`optimizer.expected_completion`, the Orders tab's definition) | 08-11-2026 | 19-10-2026 |
| Rule 3 priority position of the batch | 12 | 2 |
| `/gantt` hash / SO85 row completion | 16a153fd4e1d / 08-11-2026 | d7f4f41924a4 / 19-10-2026 |
| shift-wise download (the `/run` trace's `rule6.shiftwise`, CSV'd client-side) | rows hash f0f72e88ecab, 18 rows for the step | e4eee9149b35, 56 rows |
| `/delay-report.xlsx` (runs locally: openpyxl) | SO85 08-11-2026, 93 days late, 19.8 d waiting for machine | 19-10-2026, 73 days late, 0 d waiting for machine |
| `/production-analysis` (JSON) entry | cycle 3.3, 3.3 std min earned | cycle 11.6, 11.6 |
| `/items` process_info cycle | 3.3 | 11.6 |
| `/new-orders/quote` (QTEST1 on 9611443650, QTEST2 on 9612220701-P) | 24-11-2026, 23-11-2026 | 29-11-2026, 29-11-2026 |
| `parse_payload(build_payload(...))` through JSON: arity / classic cycle | 8 / 3.3 | 8 / 11.6 |
| same payload, worker path (`set_item_master` + `_new_masters`): ppc cycle (padded x1.3) | 4.29 | 15.08 |

Every reader followed the table. Note the target order finished EARLIER after its step
became 3.5x slower: Rule 3 ranks batches by the sum of cycle times, so the batch rose
from 12th to 2nd in the dispatch priority and stopped waiting for machines (19.8 d to
0). That is Rule 3 doing what it says, not a defect of this feature, but it means a
cycle edit can move an order's date in the direction nobody expects.

`/production-analysis.xlsx` was not exercised (needs `xlsxwriter`, not installed
locally); the JSON endpoint above reads the same `_production_tables`.

## 3. Upload no longer reaches routings (spec 10.3)

`verify_upload_safety.py` part (a), Test9: seed, plan (hash `3cd8b2884343b360`), then
`POST /upload` a doctored copy of Test9 whose routing sheet has cell N106 changed
2.8 -> 15.4 and item 9612220701-P's row blanked (row 24). Upload 200, stored workbook
replaced, **table digest unchanged, plan hash `3cd8b2884343b360` unchanged.**

## 4. Safety rules (spec 10.4)

`verify_upload_safety.py` part (b), Test9, order 26-27SO85 with 1 piece punched on step 1
`CNC FIRST SIDE`:

| attempt | result |
|---|---|
| rename the punched step | 400 "CNC FIRST SIDE has 1 punched on 26-27SO85. Finish or complete that order before renaming or removing this step." |
| remove the punched step | 400, same message |
| change the punched step's cycle | 200 |
| move the punched step below the unpunched step 2 | **200** (see F2) |
| delete the item (open orders SO85, SO133) | 400 "9611443650 is still used by 26-27SO85, 26-27SO133. Complete or remove those first." |
| save with a stale version | 409 "Someone else saved this item since you opened it. Reload to see their change." |

After the accepted move the routing reads `CNC SECOND SIDE, CNC FIRST SIDE, ...`, the
plan still runs (200), and `orderbook.precedence_cap_error` refuses one more piece on
`CNC FIRST SIDE`: "Only 0 pieces have cleared the step before it, 'CNC SECOND SIDE'".

## 5. Mutation testing

`mutate.py`: each mutation applied alone, `python3.12 -B -m pytest
tests/test_routing_rows.py tests/test_item_master.py tests/test_item_master_wiring.py
tests/test_item_master_api.py -q`, restored with `git checkout --`, `git status`
clean asserted before the next (clean at the end). A 1.1 s sleep after each write
guards against the stale-bytecode masking seen on 2026-09-22. Unmutated: 55 passed.

| # | mutation | failing tests (four files) |
|---|---|---|
| 1 | `_current_masters` passes `routing_rows=None` | 5: test_masters_follow_a_table_edit, test_save_changes_the_plan_input, test_upload_after_seed_does_not_touch_routings, test_upload_as_first_request_seeds_from_the_workbook_on_file, test_upload_without_routing_sheet_is_accepted_once_seeded |
| 2 | `_new_masters` passes `routing_rows=None` | 2: test_new_engine_masters_follow_the_table, test_new_engine_override_wins_and_none_means_workbook |
| 3 | `parse_payload` ignores `item_master` | 1: test_payload_round_trips_the_table |
| 4 | `run_candidate` does not call `set_item_master` | 1: test_run_candidate_feeds_the_table_to_the_new_engine |
| 5 | `_plan_fingerprint` drops `item_master` | 1: test_plan_fingerprint_moves_with_the_table |
| 6 | `_inputs_signature` never appends the table | 1: test_inputs_signature_unchanged_by_seeding_but_moved_by_an_edit |
| 7 | `punch_safety_errors` returns `[]` | 4: test_renaming_a_punched_step_is_refused (api), test_removing_a_punched_step_is_refused, test_renaming_a_punched_step_is_refused_with_reason, test_reordering_punched_steps_is_refused |
| 8 | `apply_save` skips the version comparison | 2: test_apply_save_versions_and_conflicts, test_stale_version_is_refused |
| 9 | `_keep_blank_as_stored` returns `new_steps` unchanged | first run: **0, whole suite also 0**. After the F3 fix: 1: test_resaving_unchanged_item_as_the_browser_sends_it_keeps_digest |
| 10 (extra) | upload parses the new file with `routing_rows=None` | 0 in the four files; **full suite: 1** (test_report_and_staleness.py::test_upload_ignores_a_routing_dropped_from_the_new_file) |
| 11 (extra) | upload does not seed before replacing the workbook | 1: test_upload_as_first_request_seeds_from_the_workbook_on_file |

**Mutation 9 is NOT belt-and-braces.** `verify_blank_resave.py` on Test9: of 851 seeded
steps, 111 have `allotted` None and 174 `suggested` None, touching **106 of 106 items**.
The browser sends `s.allotted || ""` (`web/app.js` imSave), so re-saving an UNEDITED
item: with the helper, digest and inputs signature unchanged; with it bypassed, **both
change** (200, digest moved, inputs signature moved), i.e. one click of Save with no
edit would flag an applied optimization stale. `test_resaving_unchanged_item_keeps_digest`
does not catch it because it PUTs the GET response verbatim (nulls), not the browser's
shape. Closed: `test_resaving_unchanged_item_as_the_browser_sends_it_keeps_digest` (tests/test_item_master_api.py) PUTs ITEM_A the way the browser does (`null` machine fields as `""`) and asserts the digest is unchanged; it passes, fails with the mutation applied (digest `8cc2b5d0...` vs `ee9e4f02...`), and passes again after restore. See F3.

## 6. Full suite

`python3.12 -m pytest -q` at Task 10: **1 failed, 1208 passed, 4 skipped** (77.6 s).
With the test that closed Finding F3: **1 failed, 1209 passed, 4 skipped** (77.9 s).
After the final fix wave (section 8): **1 failed, 1233 passed, 4 skipped (78.5 s)**. The one failure
is `tests/test_production_analysis.py::test_monthly_report_json_and_excel`
(`ModuleNotFoundError: xlsxwriter`), pre-existing at base and environment-only.

## 7. Cache-hit `POST /run` timing (Test9)

`time_cache_hit.py`, same script in both worktrees, cold run then 30 cache hits
(same `run_id` asserted), three rounds alternating base/feature:

| round | base median (p90) | feature median (p90) | cold base / feature |
|---|---|---|---|
| 1 | 38.2 (38.9) ms | 43.6 (45.8) ms | 1,781 / 1,738 ms |
| 2 | 39.0 (43.2) ms | 42.9 (43.4) ms | 1,850 / 1,739 ms |
| 3 | 38.2 (38.8) ms | 43.5 (47.2) ms | 1,768 / 1,740 ms |

About +5 ms (+13%) per cache hit on the local file store, from the extra
`load_item_master` reads (masters key + fingerprint + inputs signature). On MongoDB each
uncached read is a network round trip; the per-request store cache dedupes repeats
within one request, but the absolute cost on Render was not measured.

## Findings

- **F1 (intended, recorded).** Classic routings no longer carry Customer, RM type, MOQ
  or Total Time (blank/None). No reader; plans byte-identical. Spec section 3.
- **F2 (CLOSED by the final fix wave, section 8: both are now refused).** Should Save also refuse moving a
  punched step below an unpunched one, or adding a new step in front of it? Today both
  are accepted, and the next punch on that step is then refused. Detail: the punch-safety rule checks only the RELATIVE order of
  punched steps (spec section 8 says exactly that), so moving a punched step below an
  unpunched one, or inserting a new step in front of a punched one ("new steps are
  always allowed"), is accepted. The result is the state the spec's own reason forbids:
  a downstream step with more pieces than its new upstream; the next punch on that step
  is refused by `precedence_cap_error`. The implementation matches the spec; the spec's
  rule is narrower than its rationale. Owner's call whether to widen it.
- **F3 (test gap, CLOSED).** `_keep_blank_as_stored` is load-bearing on every real item
  but no test failed without it. Closed by `test_resaving_unchanged_item_as_the_browser_sends_it_keeps_digest`, which PUTs the browser's shape
  (`""` for null machine fields); mutation-proven.
- **F4.** Mutation 10 (the upload's own parse ignoring the table) is caught only outside
  the four item-master test files, by `test_report_and_staleness.py`.
- **F5.** A cycle-time edit can move an order's date the "wrong" way through Rule 3's
  priority (section 2). Behaviour of Rule 3, not of this feature.

## 8. Final fix wave (after the whole-branch review)

Commits `39f240d` (F1) and `43bfabd` (F2 to F5). Every code fix was written test
first (RED for the stated reason), then fixed (GREEN), then mutated: each mutation
applied alone, run with `python3.12 -B`, a 1.1 s sleep after every write, file
restored and compared byte for byte before the next.

**F1. A frozen step is followed by its name, not its sequence number.** Routings are
editable, so removing a step ahead of a running one shifts the running step's seq.
`freeze.compute_frozen_set` looked the applied plan up by (item, seq) and
`new_engine._ppc_frozen` trusted the stored `op_seq`. Now `compute_frozen_set` indexes
the applied rows by (item, normalised process name), rows at the step's own seq first
so an unedited routing reads exactly the row it always did, and `_ppc_frozen` keeps the
row's seq only while the routing still has the row's step at that seq, otherwise
re-resolves it by name, and drops the row when the name is gone.
`ppc_engine/scheduler/flow_scheduler.py` is untouched.

`tests/test_item_master_freeze.py`, new-engine sample workbook, through the API. ITEM_A
is edited to PREWASH (1 min, MW1, helper Charlie) -> VMC FIRST SIDE (10 min, VMC1,
Alpha / Bravo) -> INSPECTION (MI1) -> DISPATCH, so a SHORT step feeds a LONG one on
machines with different people. 20 of 50 are punched on VMC FIRST SIDE (PREWASH
unpunched, as the floor punches machining steps only); the plan is stored as the
applied plan and Done derives the frozen row (VMC FIRST SIDE, seq 2, VMC1). Then
`PUT /item-master` removes PREWASH, so VMC FIRST SIDE becomes seq 1.

| test | RED on the old code |
|---|---|
| re-plan after the edit keeps VMC FIRST SIDE pinned | "VMC1 runs ['INSPECTION', 'VMC FIRST SIDE']" (INSPECTION pinned onto the running job's machine) |
| Done after the edit freezes it by name | frozen row came back as (VMC FIRST SIDE, seq 1, **MW1, Charlie**): the removed PREWASH's machine and helper |
| a stale seq is re-resolved by name (`_ppc_frozen` unit) | pinned INSPECTION's seq |
| a step no longer in the routing is not frozen (`_ppc_frozen` unit) | pinned at the stale seq |

Each re-plan test also asserts the first entry on VMC1 resumes with the pinned
operator, INSPECTION stays on MI1, and `routing_order_violations` and
`qualification_violations` are empty. Mutation: `freeze.py` reverted alone fails the
Done test; `new_engine.py` reverted alone fails the other three. Not covered by a test,
said plainly: the "same seq first" ordering inside `compute_frozen_set` only matters
when two steps of one routing share a normalised name, which `validate_item` refuses
and no real book has; it is there so an unedited routing can never read differently.

**Byte-identical re-run.** `verify_switch.py` (unchanged) in a fresh worktree of
`c123fa1` and on the fixed branch: **16 of 16 schedule hashes identical**, frozen counts
identical (17 / 19 / 10 / 13 on the WIP legs), ppc routings 16 of 16 identical, classic
routings differing only as section 1 records (same hashes as section 1). The baseline
worktree was removed afterwards.

**F2. No step new, or moved, in front of a punched step.** `punch_safety_errors` now
also refuses, on every open order with punches, any unpunched step that sits before a
punched step P in the new list but was not before P in the old list (a name the old
list does not have counts as new). Message: "WASH cannot go before BANDSAW: BANDSAW has
4 punched on SO1. Add new steps after it, or finish that order first." Tests: insert
before refused, move an unpunched step from after to before refused, insert after
allowed, remove an unpunched step ahead allowed, completed order allowed (unit); insert
before and move before refused through the endpoint. Mutation (rule skipped): 4 tests
fail. Behaviour change, deliberate: renaming an UNPUNCHED step that sits in front of a
punched step is now refused too, since the rule cannot tell a rename from a new step;
`test_safe_edits_are_allowed` was rebased to rename a step after the last punched one.

**F3. Cycle times bounded.** `validate_item` refuses non-finite numbers, a Machine step
above `MAX_MACHINE_CYCLE_MIN` = 1440 min per piece, an Outsourced block above
`MAX_OUTSOURCED_BLOCK_MIN` = 86400 min (60 days). RED evidence through the endpoint
with a raw JSON body: `60000` was saved (200); `NaN` and `Infinity` were accepted and
saved, and the response then failed to serialise ("Out of range float values are not
JSON compliant"); `-Infinity` gave the wrong message. Mutations: isfinite removed 6
fail, machine cap removed 2, outsourced cap removed 2. Real books: largest machine
cycle 1092 min (Test5/8/9), 910 (Test9-ORIGINAL), largest outsourced block 10080 on all
four, so **no seeded item is refused** on an unedited re-save (checked on all four).

**F4. Only real machines can be newly added.** `_save_item` passes the ids of
non-provisional Machine-master rows; a provisional token already on the item stays
allowed. Test: CNC9 (provisional, from ITEM_B's routing) cannot be added to ITEM_A, and
ITEM_B still re-saves. Mutation (filter removed): 1 test fails. No real book registers a
provisional machine today.

**F5. Test gaps closed.** Delete refused while an Add New Orders draft line uses the
item; `PUT` on an unknown code is 404 and creates nothing;
`test_upload_ignores_a_routing_dropped_from_the_new_file` now also asserts ITEM_B keeps
its routing (with `_current_masters` mutated to ignore the table, that test fails).

## What was NOT run

- The live-store copy (no `MONGODB_URI` this session; owner to run before deploy), and
  therefore the quote on the store copy.
- The browser run of the tab as both roles (spec 10.6).
- `/production-analysis.xlsx` (needs `xlsxwriter`, not installed locally).
- Cloud / GitHub Actions / Mac worker end to end: the payload round trip and the
  worker's masters path were exercised in-process only.
- Timing on Render / MongoDB.

## Harness sources

All in the session scratchpad, not committed. Run from the root of the checkout under
test with `python3.12 -B <script> <book.xlsx> ...`.

### `verify_switch.py`

```python
"""Byte-identical switch harness (Task 10, Item Process Master).

Run from the root of a checkout (cwd = repo root). Plans each real book on the
CURRENT checkout and prints one line per run: book, flexible, wip, #entries,
#frozen ops, schedule hash, classic-routings hash, ppc-routings hash, seeded?.

Each run is a FRESH subprocess with its own temp STORE_DIR, so no module-level
cache (book_store backend, masters caches) can leak between runs.

Run it in the feature worktree AND in a pristine worktree of c123fa1 (origin/main)
and diff the outputs; every line must match except the trailing `seeded=` field
(the baseline has no table, that is the point).
"""
import hashlib, io, os, subprocess, sys, tempfile
from datetime import date, timedelta

PLAN_DAY = date(2026, 10, 6)
WIP_ORDERS = 30


def _h(obj) -> str:
    return hashlib.sha256(repr(obj).encode()).hexdigest()[:16]


def one(book, flexible, wip):
    os.environ["STORE_DIR"] = tempfile.mkdtemp()
    os.environ["DEFAULT_SCHEDULER"] = "new"
    os.environ["AUTO_OPTIMIZE"] = "0"
    for k in ("MONGODB_URI", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
        os.environ.pop(k, None)
    sys.path.insert(0, os.getcwd())
    from engine import book_store, loaders, new_engine, freeze
    from engine.models import Order, Actual
    import api.main as m

    raw = open(book, "rb").read()
    book_store.save_masters_bytes(raw)
    so_lines, _ = loaders.load_all(io.BytesIO(raw))
    orders = [Order(s.so_no, s.item_code, s.item_name, s.qty, s.delivery_date)
              for s in so_lines]
    book_store.add_orders(orders)

    cfg = m._load_plan_config()
    cfg.plan_start_date = PLAN_DAY
    cfg.apply_operator_logic = True
    cfg.overlap_percent = 80
    cfg.flexible_machines = flexible

    n_frozen = 0
    if wip:
        # Real WIP + a DERIVED frozen set: plan once, record it as the applied plan
        # (what the floor follows), punch half of step 1 on 30 orders, then derive
        # and store the frozen set exactly as "Done entering" does.
        m._plan(cfg)
        sched0 = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
        book_store.save_last_applied_schedule(freeze.schedule_projection(sched0))
        masters = m._current_masters()
        op_name = masters.operators[0].name
        done = 0
        for o in orders:
            if done >= WIP_ORDERS:
                break
            r = masters.routings.get(o.item_code)
            if r is None or not r.processes:
                continue
            first = r.processes[0].name
            book_store.append_actual(Actual(
                so_no=o.so_no, item_code=o.item_code,
                entry_date=PLAN_DAY - timedelta(days=1), shift="1st shift",
                process=first, qty_produced=max(1, int(o.ordered_qty) // 2),
                qty_rejected=0, operator=op_name))
            done += 1
        n_frozen = len(m._compute_and_store_frozen())

    m._plan(cfg)
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    sh = hashlib.sha256(repr(sorted(
        (e.batch_id, e.process_seq, e.machine, e.operator, e.start.isoformat(),
         e.end.isoformat(), e.qty) for e in sched)).encode()).hexdigest()[:16]
    classic = m._current_masters().routings
    ppc = new_engine._new_masters(flexible).routings
    seeded = "n/a"
    if hasattr(book_store, "load_item_master"):
        doc = book_store.load_item_master()
        seeded = "yes(%d items)" % len(doc["items"]) if doc else "no"
    print(os.path.basename(book), flexible, wip, len(sched), n_frozen, sh,
          "classic=" + _h(list(classic.items())), "ppc=" + _h(list(ppc.items())),
          "seeded=" + seeded, sep="\t", flush=True)


if __name__ == "__main__":
    if sys.argv[1] == "--one":
        one(sys.argv[2], sys.argv[3] == "True", sys.argv[4] == "True")
    else:
        for b in sys.argv[1:]:
            for flexible in (False, True):
                for wip in (False, True):
                    r = subprocess.run([sys.executable, "-B", __file__, "--one", b,
                                        str(flexible), str(wip)],
                                       capture_output=True, text=True)
                    sys.stdout.write(r.stdout)
                    if r.returncode:
                        sys.stdout.write("ERROR %s %s %s\n%s\n" % (
                            b, flexible, wip, r.stderr[-3000:]))
                    sys.stdout.flush()
```

### `classic_neutral.py`

```python
"""Hash the CLASSIC routings with the four spec-dropped fields (Process.total_time,
Routing.customer/rm_type/moq) blanked, so base and feature can be compared on every
field the table keeps. Also counts how many values of each dropped field were set."""
import os, sys, tempfile, hashlib
from dataclasses import replace
os.environ["STORE_DIR"]=tempfile.mkdtemp(); os.environ["DEFAULT_SCHEDULER"]="new"
sys.path.insert(0, os.getcwd())
from engine import book_store
import api.main as m
book_store.save_masters_bytes(open(sys.argv[1],"rb").read())
r = m._current_masters().routings
neutral = [(k, replace(v, customer="", rm_type="", moq=None,
                       processes=[replace(p, total_time=None) for p in v.processes]))
           for k, v in r.items()]
nz = dict(customer=sum(bool(v.customer) for v in r.values()),
          rm_type=sum(bool(v.rm_type) for v in r.values()),
          moq=sum(v.moq is not None for v in r.values()),
          total_time=sum(p.total_time is not None for v in r.values() for p in v.processes),
          steps=sum(len(v.processes) for v in r.values()))
print(os.path.basename(sys.argv[1]), len(r), hashlib.sha256(repr(neutral).encode()).hexdigest()[:16], nz, sep="\t")
```

### `quote_compare.py`

```python
"""Two-line /new-orders/quote on a real book; runs identically in base and feature."""
import io, json, os, sys, tempfile
from datetime import date
os.environ["STORE_DIR"] = tempfile.mkdtemp(); os.environ["DEFAULT_SCHEDULER"] = "new"
os.environ["AUTO_OPTIMIZE"] = "0"; os.environ["ADMIN_PASSWORD"] = "1930rail"
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient
from engine import book_store, loaders
from engine.models import Order
from engine.config import Config
import api.main as m
raw = open(sys.argv[1], "rb").read(); book_store.save_masters_bytes(raw)
so, _ = loaders.load_all(io.BytesIO(raw))
orders = [Order(s.so_no, s.item_code, s.item_name, s.qty, s.delivery_date) for s in so]
book_store.add_orders(orders)
book_store.save_plan_config(json.dumps(Config(scheduler="new", apply_operator_logic=True, overlap_percent=80,
                                              plan_start_date=date(2026, 10, 6)).to_dict(), default=str))
c = TestClient(m.app); c.post("/login", data={"username": "anvitech", "password": "1930rail"})
a, b = orders[0].item_code, next(o.item_code for o in orders if o.item_code != orders[0].item_code)
r = c.put("/new-orders/drafts", json={"drafts": [{"so_no": "QT1", "item_code": a, "qty": 120},
                                                  {"so_no": "QT2", "item_code": b, "qty": 80}]})
assert r.status_code == 200, r.text
q = c.post("/new-orders/quote").json()
print(os.path.basename(sys.argv[1]), [(l["so_no"], l["item_code"], l["completion"]) for l in q["lines"]],
      "moved", len(q["moved"]), "violations", len(q["violations"]), "verified", q["verified"])
```

### `verify_readers.py`

```python
"""Every reader follows a table edit (Task 10, step 3). FEATURE checkout only.

A throwaway in-process instance (TestClient, temp STORE_DIR) with Test9: change ONE
CNC step's cycle time through PUT /item-master (admin) and show each consumer moves,
while the stored workbook bytes stay byte-identical.
"""
import base64, hashlib, io, json, os, sys, tempfile
from datetime import date

os.environ["STORE_DIR"] = tempfile.mkdtemp()
os.environ["DEFAULT_SCHEDULER"] = "new"
os.environ["AUTO_OPTIMIZE"] = "0"
os.environ["ADMIN_PASSWORD"] = "1930rail"   # throwaway instance only (as tests/conftest.py)
for k in ("MONGODB_URI", "UPSTASH_REDIS_REST_URL", "UPSTASH_REDIS_REST_TOKEN"):
    os.environ.pop(k, None)
sys.path.insert(0, os.getcwd())

from fastapi.testclient import TestClient
from openpyxl import load_workbook
from engine import book_store, loaders, new_engine, optimize_service
from engine.models import Order, Actual
from engine.config import Config
import api.main as m

BOOK = sys.argv[1]
raw = open(BOOK, "rb").read()
book_store.save_masters_bytes(raw)
so_lines, _ = loaders.load_all(io.BytesIO(raw))
orders = [Order(s.so_no, s.item_code, s.item_name, s.qty, s.delivery_date) for s in so_lines]
book_store.add_orders(orders)
cfg = Config(scheduler="new", apply_operator_logic=True, overlap_percent=80,
             plan_start_date=date(2026, 10, 6))
book_store.save_plan_config(json.dumps(cfg.to_dict(), default=str))

c = TestClient(m.app)
c.post("/login", data={"username": "anvitech", "password": "1930rail"})
items = c.get("/item-master").json()["items"]           # first GET seeds the table
assert book_store.load_item_master() is not None

# Target: the first order whose item has a CNC step that is NOT step 1 (step 1 is
# what most WIP touches); the step must be a machining step.
target = None
for o in orders:
    it = next((i for i in items if i["code"] == o.item_code), None)
    if not it:
        continue
    for idx, s in enumerate(it["steps"]):
        if idx > 0 and s.get("machining") and str(s.get("allotted", "")).upper().startswith("CNC"):
            target = (o, it, idx)
            break
    if target:
        break
order, item, idx = target
step = item["steps"][idx]
print(f"TARGET {order.so_no} / {order.item_code} step {idx+1} '{step['name']}' "
      f"on {step['allotted']} cycle {step['cycle']} min, qty {order.ordered_qty}")

# One punch on the target step in October 2026 so production analysis has a row.
op_name = m._current_masters().operators[0].name
book_store.append_actual(Actual(so_no=order.so_no, item_code=order.item_code,
                                entry_date=date(2026, 10, 5), shift="1st shift",
                                process=step["name"], qty_produced=1, qty_rejected=0,
                                operator=op_name))
wb_sha = lambda: hashlib.sha256(book_store.load_masters_bytes()).hexdigest()[:16]


def measure():
    out = {}
    run = c.post("/run", json={}).json()
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    tgt = [e for e in sched if e.item_code == order.item_code and e.process_seq == idx + 1
           and order.so_no in (e.so_refs or [])] \
        if hasattr(sched[0], "item_code") else []
    if not tgt:
        tgt = [e for e in sched if order.so_no in (getattr(e, "so_refs", None) or [order.so_no])
               and getattr(e, "process", "") == step["name"]]
    out["run: target step minutes"] = round(sum((e.end - e.start).total_seconds() / 60 for e in tgt), 1)
    out["run: target step end"] = max(e.end for e in tgt).strftime("%d-%m %H:%M")
    out["run: schedule hash"] = hashlib.sha256(repr(sorted(
        (e.batch_id, e.process_seq, e.machine, e.operator, e.start.isoformat(),
         e.end.isoformat(), e.qty) for e in sched)).encode()).hexdigest()[:12]
    from engine import optimizer
    out["run: expected completion (one definition)"] = str(optimizer.expected_completion(sched).get(order.key))
    ot = run["orders"]
    rows_ot = [r if isinstance(r, dict) else dict(zip(ot["columns"], r)) for r in ot["rows"]]
    hit_ot = [r for r in rows_ot if order.so_no in r.values() and order.item_code in r.values()]
    out["run: Orders tab row"] = {k: v for k, v in hit_ot[0].items() if "xpect" in k or "ate" in k} if hit_ot else None
    # shift-wise download = the /run trace's rule6.shiftwise table, CSV'd client-side
    sw = (run.get("trace", {}).get("rule6", {}) or {}).get("shiftwise") or {}
    rows = sw.get("rows", [])
    out["shift-wise: rows hash"] = hashlib.sha256(json.dumps(rows, default=str).encode()).hexdigest()[:12]
    out["shift-wise: target rows"] = sum(1 for r in rows if order.item_code in json.dumps(r, default=str)
                                          and step["name"] in json.dumps(r, default=str))
    g = c.get("/gantt").json()
    gtxt = json.dumps(g, default=str)
    out["gantt: hash"] = hashlib.sha256(gtxt.encode()).hexdigest()[:12]
    grow = [r for r in g.get("rows", []) if order.so_no in str(r.get("so_no", ""))
            and r.get("item_code") == order.item_code]
    out["gantt: target row completion"] = grow[0]["completion"] if grow else None
    pr = m._PLAN_CACHE["artifacts"]["plan_run"].batches_prioritized
    out["rule 3: target batch position in priority order"] = next(
        (n + 1 for n, b in enumerate(pr) if order.so_no in b.source_so_refs
         and b.item_code == order.item_code), None)
    dr = c.get("/delay-report.xlsx")
    out["delay report: status"] = dr.status_code
    if dr.status_code == 200:
        wb = load_workbook(io.BytesIO(dr.content), read_only=True)
        ws = wb[wb.sheetnames[0]]
        hdr, *body = [list(r) for r in ws.iter_rows(values_only=True)]
        row = next((r for r in body if order.so_no in r and order.item_code in [str(x) for x in r]), None)
        out["delay report: target summary"] = dict(zip(hdr, row)) if row else None
        out["delay report: xlsx sheet hash"] = hashlib.sha256(repr(body).encode()).hexdigest()[:12]
    pa = c.get("/production-analysis", params={"year": 2026, "month": 10}).json()
    ent = pa["entries"]
    cols = ent["columns"]
    rows = [r if isinstance(r, dict) else dict(zip(cols, r)) for r in ent["rows"]]
    hit = [r for r in rows if order.item_code in json.dumps(r, default=str)]
    out["production analysis: target entry"] = {k: v for k, v in hit[0].items()
                                                if "ycle" in k or "arned" in k or "fficien" in k} if hit else None
    its = c.get("/items").json()["items"][order.item_code]["process_info"][step["name"]]
    out["/items: cycle_time"] = its["cycle_time"]
    # quote: two draft lines, one on the target item
    other = next(o for o in orders if o.item_code != order.item_code)
    r = c.put("/new-orders/drafts", json={"drafts": [
        {"so_no": "QTEST1", "item_code": order.item_code, "qty": 50},
        {"so_no": "QTEST2", "item_code": other.item_code, "qty": 50}]})
    assert r.status_code == 200, r.text
    q = c.post("/new-orders/quote")
    out["quote: status"] = q.status_code
    if q.status_code == 200:
        out["quote: lines"] = [(l.get("so_no"), l.get("item_code"), l.get("completion"))
                               for l in q.json()["lines"]]
    # cloud payload round-trip (what the GitHub/Mac worker rebuilds)
    p = optimize_service.build_payload(book_store.load_active_orders(), book_store.load_actuals(),
                                       book_store.load_masters_bytes(), cfg, seed=1,
                                       item_master=book_store.load_item_master())
    p = json.loads(json.dumps(p))                     # through the wire
    parsed = optimize_service.parse_payload(p)
    out["payload: tuple arity"] = len(parsed)
    out["payload: classic cycle"] = parsed[2].routings[order.item_code].processes[idx].cycle_time
    new_engine._MASTERS_CACHE.clear()
    new_engine.set_masters_bytes(base64.b64decode(p["masters_xlsx_b64"]))
    new_engine.set_item_master(p["item_master"])
    try:
        nr = new_engine._new_masters(False).routings[order.item_code]
        out["payload: worker ppc cycle (padded x1.3)"] = nr.operations[idx].cycle_min
    finally:
        new_engine.set_masters_bytes(None)
        new_engine.clear_item_master_override()
        new_engine._MASTERS_CACHE.clear()
    out["workbook sha"] = wb_sha()
    return out


before = measure()
item = next(i for i in c.get("/item-master").json()["items"] if i["code"] == order.item_code)
steps = [dict(s) for s in item["steps"]]
new_cycle = round(float(steps[idx]["cycle"]) * 2 + 5, 2)
steps[idx]["cycle"] = new_cycle
r = c.put("/item-master", json={"code": item["code"], "description": item["description"],
                                "steps": steps, "version": item["version"]})
print("PUT /item-master", r.status_code, f"cycle {item['steps'][idx]['cycle']} -> {new_cycle}")
after = measure()
for k in before:
    flag = "MOVED" if before[k] != after[k] else "same"
    print(f"{flag:5}  {k}\n         before: {before[k]}\n         after:  {after[k]}")
```

### `verify_upload_safety.py`

```python
"""Spec 10.3 (upload no longer reaches routings) + 10.4 (safety rules), FEATURE only.

Throwaway in-process instance, Test9. (a) Seed, plan, then upload a COPY of Test9
whose routing sheet has one cycle cell changed and one whole item row blanked:
the plan hash must not move. (b) Punch a step on an active order, then try to
rename / remove / reorder it (400 expected), change its cycle (200), and delete
an item that an open order uses (refused).
"""
import hashlib, io, json, os, sys, tempfile
from datetime import date

os.environ["STORE_DIR"] = tempfile.mkdtemp()
os.environ["DEFAULT_SCHEDULER"] = "new"
os.environ["AUTO_OPTIMIZE"] = "0"
os.environ["ADMIN_PASSWORD"] = "1930rail"     # throwaway instance only (as tests/conftest.py)
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from engine import book_store, loaders
from engine.models import Order, Actual
from engine.config import Config
import api.main as m

BOOK = sys.argv[1]
raw = open(BOOK, "rb").read()
book_store.save_masters_bytes(raw)
so_lines, _ = loaders.load_all(io.BytesIO(raw))
orders = [Order(s.so_no, s.item_code, s.item_name, s.qty, s.delivery_date) for s in so_lines]
book_store.add_orders(orders)
cfg = Config(scheduler="new", apply_operator_logic=True, overlap_percent=80,
             plan_start_date=date(2026, 10, 6))
book_store.save_plan_config(json.dumps(cfg.to_dict(), default=str))
c = TestClient(m.app)
c.post("/login", data={"username": "anvitech", "password": "1930rail"})


def plan_hash():
    c.post("/run", json={})
    s = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    return hashlib.sha256(repr(sorted((e.batch_id, e.process_seq, e.machine, e.operator,
                                       e.start.isoformat(), e.end.isoformat(), e.qty)
                                      for e in s)).encode()).hexdigest()[:16]


h0 = plan_hash()
seed_digest = __import__("engine.item_master", fromlist=["digest"]).digest(book_store.load_item_master())

# (a) a doctored copy: change the first numeric cycle cell of the first order's item
#     row, and blank the item code of a second item's row entirely.
wb = load_workbook(io.BytesIO(raw))
ws = next(wb[n] for n in wb.sheetnames if "process" in n.lower() and "item" in n.lower())
tgt_code, gone_code = orders[0].item_code, orders[1].item_code
changed = blanked = None
for row in ws.iter_rows():
    vals = [str(c.value).strip() if c.value is not None else "" for c in row]
    if changed is None and tgt_code in vals:
        for cell in row[vals.index(tgt_code) + 1:]:
            if isinstance(cell.value, (int, float)):
                changed = (cell.coordinate, cell.value, cell.value * 3 + 7)
                cell.value = changed[2]
                break
    elif blanked is None and gone_code in vals and gone_code != tgt_code:
        row[vals.index(gone_code)].value = None
        blanked = (gone_code, row[0].row)
buf = io.BytesIO(); wb.save(buf)
r = c.post("/upload", files={"file": ("doctored.xlsx", buf.getvalue(),
           "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
h1 = plan_hash()
d1 = __import__("engine.item_master", fromlist=["digest"]).digest(book_store.load_item_master())
print("10.3 upload doctored copy:", r.status_code, "| cell changed", changed, "| item row blanked", blanked)
print("     plan hash before", h0, "after", h1, "->", "UNCHANGED" if h0 == h1 else "MOVED")
print("     table digest unchanged:", seed_digest == d1,
      "| stored workbook replaced:", hashlib.sha256(book_store.load_masters_bytes()).hexdigest()[:12]
      != hashlib.sha256(raw).hexdigest()[:12])

# (b) safety rules on a punched step of an ACTIVE order
o = orders[0]
it = next(i for i in c.get("/item-master").json()["items"] if i["code"] == o.item_code)
st = it["steps"]
op_name = m._current_masters().operators[0].name
book_store.append_actual(Actual(so_no=o.so_no, item_code=o.item_code, entry_date=date(2026, 10, 5),
                                shift="1st shift", process=st[0]["name"], qty_produced=1,
                                operator=op_name))


def put(steps):
    cur = next(i for i in c.get("/item-master").json()["items"] if i["code"] == o.item_code)
    return c.put("/item-master", json={"code": o.item_code, "description": cur["description"],
                                       "steps": steps, "version": cur["version"]})


ren = [dict(s) for s in st]; ren[0]["name"] = ren[0]["name"] + " X"
rem = [dict(s) for s in st[1:]]
reo = [dict(s) for s in st]; reo[0], reo[1] = reo[1], reo[0]
cyc = [dict(s) for s in st]; cyc[0]["cycle"] = (float(cyc[0]["cycle"] or 1) + 1)
for label, steps in (("rename punched step", ren), ("remove punched step", rem),
                     ("change punched step's cycle", cyc), ("reorder punched step past an unpunched one", reo)):
    r = put(steps)
    print(f"10.4 {label}: {r.status_code} {r.json().get('detail', '') if r.status_code != 200 else ''}")
r = c.post("/item-master/delete", json={"code": o.item_code})
print(f"10.4 delete item an open order uses: {r.status_code} {r.json().get('detail', '')}")
r = c.put("/item-master", json={"code": o.item_code, "description": "x", "steps": st, "version": -1})
print(f"10.4 stale version: {r.status_code} {r.json().get('detail', '')}")

# After the allowed reorder: the punched step now sits AFTER an unpunched one.
cur = next(i for i in c.get("/item-master").json()["items"] if i["code"] == o.item_code)
print("after reorder, steps:", [s["name"] for s in cur["steps"]][:3])
r = c.post("/run", json={})
print("plan after reorder:", r.status_code)
from engine import orderbook
ms = m._current_masters()
err = orderbook.precedence_cap_error(book_store.load_actuals(), o.so_no, o.item_code,
                                     cur["steps"][1]["name"], ms.routings[o.item_code], o.ordered_qty)
print("precedence_cap_error for one more piece on the punched step (now step 2), i.e. is the CURRENT state legal:", err)
```

### `mutate.py`

```python
"""Mutation testing for the Item Process Master safeguards (Task 10, step 4).
Run from the feature worktree root. Each mutation: apply (exactly-one-match
string replace), run the four item-master test files with -B, record failures,
restore with `git checkout -- <file>`, and assert `git status --porcelain` is
clean before the next. Never commits."""
import re, subprocess, sys, time

TESTS = ["tests/test_routing_rows.py", "tests/test_item_master.py",
         "tests/test_item_master_wiring.py", "tests/test_item_master_api.py"]

M = [
    ("1 _current_masters passes routing_rows=None", "api/main.py",
     "            _, base = load_all(io.BytesIO(raw),\n"
     "                               routing_rows=item_master.routing_rows(doc) if doc else None)",
     "            _, base = load_all(io.BytesIO(raw),\n"
     "                               routing_rows=None)"),
    ("2 _new_masters passes routing_rows=None", "engine/new_engine.py",
     "routing_rows=item_master.routing_rows(doc) if doc else None).masters",
     "routing_rows=None).masters"),
    ("3 parse_payload ignores item_master", "engine/optimize_service.py",
     '    doc = payload.get("item_master")\n    if raw:',
     '    doc = None\n    if raw:'),
    ("4 run_candidate does not call set_item_master", "engine/optimize_service.py",
     '        new_engine.set_item_master(payload.get("item_master"))\n',
     '        pass\n'),
    ("5 _plan_fingerprint drops item_master", "api/main.py",
     '        "item_master": item_master.digest(book_store.load_item_master()),\n', ''),
    ("6 _inputs_signature never appends the table", "api/main.py",
     '        parts.append(["item_master", item_master.digest(doc)])',
     '        pass'),
    ("7 punch_safety_errors returns []", "engine/item_master.py",
     "    with punches must keep their order relative to each other.\"\"\"\n",
     "    with punches must keep their order relative to each other.\"\"\"\n    return []\n"),
    ("8 apply_save skips the version comparison", "engine/item_master.py",
     'if cur is None or cur.get("version") != expected_version:',
     'if cur is None:'),
    ("9 _keep_blank_as_stored returns new_steps unchanged", "api/main.py",
     "    so re-saving an unedited seeded item changes nothing (not even the digest).\"\"\"\n",
     "    so re-saving an unedited seeded item changes nothing (not even the digest).\"\"\"\n"
     "    return new_steps\n"),
    # extras beyond the brief's nine
    ("10 (extra) upload parses the new file with routing_rows=None", "api/main.py",
     "            io.BytesIO(contents),\n            routing_rows=item_master.routing_rows(doc) if doc else None)",
     "            io.BytesIO(contents),\n            routing_rows=None)"),
    ("11 (extra) upload does not seed before replacing the workbook", "api/main.py",
     "    # Seed from the workbook CURRENTLY on file, before the save below replaces it.\n"
     "    doc = _item_master_doc()\n",
     "    doc = book_store.load_item_master()\n"),
]


def clean():
    return subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                          text=True).stdout.strip() == ""


assert clean(), "worktree not clean before mutating"
only = set(sys.argv[1:])
tests = TESTS
if only and only >= {"--full"}:
    tests = ["tests"]
    only -= {"--full"}
for name, path, old, new in M:
    if only and name.split()[0] not in only:
        continue
    src = open(path).read()
    n = src.count(old)
    if n != 1:
        print(f"!! {name}: pattern matched {n} times, SKIPPED"); continue
    open(path, "w").write(src.replace(old, new))
    time.sleep(1.1)
    r = subprocess.run([sys.executable, "-B", "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider",
                        "-x" if False else "-q"], capture_output=True, text=True)
    subprocess.run(["git", "checkout", "--", path], check=True)
    assert clean(), f"restore failed after {name}"
    failed = sorted(set(re.findall(r"^FAILED (\S+)", r.stdout, re.M)))
    summary = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:]
    print(f"== {name}\n   {summary}")
    for f in failed:
        print(f"   FAILED {f}")
    sys.stdout.flush()
print("worktree clean at end:", clean())
```

### `verify_blank_resave.py`

```python
"""Mutation 9 survives the suite: does it matter on the real book? Re-save an
unedited Test9 item the way the BROWSER sends it (null machine fields as "",
app.js imSave) with the real helper and with it bypassed."""
import io, json, os, sys, tempfile
os.environ["STORE_DIR"] = tempfile.mkdtemp(); os.environ["DEFAULT_SCHEDULER"] = "new"
os.environ["AUTO_OPTIMIZE"] = "0"; os.environ["ADMIN_PASSWORD"] = "1930rail"
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient
from engine import book_store, item_master as im
import api.main as m
book_store.save_masters_bytes(open(sys.argv[1], "rb").read())
c = TestClient(m.app); c.post("/login", data={"username": "anvitech", "password": "1930rail"})
items = c.get("/item-master").json()["items"]
doc = book_store.load_item_master()
steps = [s for it in doc["items"].values() for s in it["steps"]]
nulls = [it for it in doc["items"].values()
         if any(s.get("allotted") is None or s.get("suggested") is None for s in it["steps"])]
print(f"seeded steps {len(steps)}; with allotted None {sum(s.get('allotted') is None for s in steps)}, "
      f"suggested None {sum(s.get('suggested') is None for s in steps)}; items affected {len(nulls)}/{len(doc['items'])}")
code = next(k for k, it in doc["items"].items() if it in nulls)
def browser_save(code):
    a = next(i for i in c.get("/item-master").json()["items"] if i["code"] == code)
    body = {"code": code, "description": a["description"], "version": a["version"],
            "steps": [{"name": s["name"], "cycle": s["cycle"], "allotted": s["allotted"] or "",
                       "suggested": s["suggested"] or ""} for s in a["steps"]]}
    d0 = im.digest(book_store.load_item_master()); sig0 = m._inputs_signature(m._load_plan_config())
    r = c.put("/item-master", json=body)
    return r.status_code, im.digest(book_store.load_item_master()) == d0, m._inputs_signature(m._load_plan_config()) == sig0
print("real helper   : status, digest unchanged, inputs signature unchanged =", browser_save(code))
m._keep_blank_as_stored = lambda new, old: new
print("helper bypassed: status, digest unchanged, inputs signature unchanged =", browser_save(code))
```

### `time_cache_hit.py`

```python
"""Cache-hit POST /run timing on a book; runs identically in base and feature."""
import io, json, os, statistics, sys, tempfile, time
from datetime import date
os.environ["STORE_DIR"] = tempfile.mkdtemp()
os.environ["DEFAULT_SCHEDULER"] = "new"
os.environ["AUTO_OPTIMIZE"] = "0"
os.environ["ADMIN_PASSWORD"] = "1930rail"     # throwaway instance only
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient
from engine import book_store, loaders
from engine.models import Order
from engine.config import Config
import api.main as m

raw = open(sys.argv[1], "rb").read()
book_store.save_masters_bytes(raw)
so, _ = loaders.load_all(io.BytesIO(raw))
book_store.add_orders([Order(s.so_no, s.item_code, s.item_name, s.qty, s.delivery_date) for s in so])
book_store.save_plan_config(json.dumps(Config(scheduler="new", apply_operator_logic=True,
                                              overlap_percent=80,
                                              plan_start_date=date(2026, 10, 6)).to_dict(), default=str))
c = TestClient(m.app)
c.post("/login", data={"username": "anvitech", "password": "1930rail"})
t = time.perf_counter(); r = c.post("/run", json={}); cold = (time.perf_counter() - t) * 1000
rid = r.json().get("run_id")
hits = []
for _ in range(30):
    t = time.perf_counter(); r = c.post("/run", json={}); hits.append((time.perf_counter() - t) * 1000)
    assert r.json().get("run_id") == rid, "not a cache hit"
print(f"cold {cold:.0f} ms | cache hit median {statistics.median(hits):.1f} ms, "
      f"p90 {sorted(hits)[26]:.1f} ms, min {min(hits):.1f} ms (n=30)")
```
