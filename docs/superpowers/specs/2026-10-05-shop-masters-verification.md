# Machines and holidays in the app, upload removed: verification (Task 9)

**Date:** 2026-10-05 · **Branch:** `shop-masters` (code HEAD `93861c5`, from `origin/main`
@ `1199583`, which is stage 1 live) · **Status:** implemented, unpushed.
Spec: `docs/superpowers/specs/2026-10-05-shop-masters-no-excel-design.md` (section 9 is
the contract this document answers).

Everything below ran on a local machine, `python3.12`, against throwaway temp
`STORE_DIR` stores. The live site and the live database were not touched. The real
workbooks were read only. The baseline is a temporary worktree of `1199583`
(removed afterwards). The feature branch has no `/upload`, so in BOTH checkouts the
harness puts the workbook in the store directly (`book_store.save_masters_bytes`).
Harness sources are at the end, verbatim.

## Summary

| Spec 9 item | Result |
|---|---|
| 1. Byte-identical switch, real books | **16 of 16 schedule hashes identical** to `1199583` (Test5, Test8, Test9, Test9-ORIGINAL x flexible F/T x clean / WIP with a derived frozen set), and identical to the stage-1 table too. **With the workbook made unreadable** after the seed (every opener raises, every cache cleared): **16 of 16 identical, 0 opens.** Two-line `/new-orders/quote` identical on all 4 books, also with the workbook unreadable. Masters differ ONLY in the two fields the spec drops: `Hr Rate` (22 of 26 machines carried 250 / 500) and the two April-2025 `Leave` rows. |
| 2. Live-store copy | **Not run** (no credential this session); owner to run before deploy. |
| 3. Every reader follows an edit | CNC1 2 shifts -> 1 shift, holiday 09-10-2026, machine CNC9 added: **plan, Analytics, delay report, shift-wise, Daily Entry machine list, quote, payload (classic and worker ppc) all moved**; workbook bytes unchanged. **Production analysis did NOT move**, by design: its working minutes (630 / 570) come from the shift config, not from the Machines table or holidays (see F2). |
| 4. Role has no effect | Every new-engine operator forced to `Role.HELPER`: **32 of 32 hashes unchanged** (all 16 runs, in both checkouts), including Test9 WIP+frozen (`95cbb28f49bb624f`). |
| Extra: machine TYPE | Retyping CNC1 to Manual Packing, or MD1 to CNC lathe: **plan hash unchanged** (Test9). Setup and +30% follow the step's machine id prefix, not the type, so the Machines card's explainer is wrong (F4). |
| 5. Mutation testing | **11 mutations (the brief's 10, #10 split classic / ppc): 9 load-bearing on the shop + item-master test files; #1 and #3 fail NO test, in those files or in the whole suite.** #1 matters on a store with tables but no workbook (classic masters empty, 428 -> 422 plan entries); #3 changes the worker's classic machine hours (19.5 instead of 9.5) but not its new-engine score. See section 5. |
| 6. Browser run | **Not run in Task 9.** |
| 7. Full suite | **1 failed, 1272 passed, 4 skipped (80.7 s).** The failure is the pre-existing `test_production_analysis.py::test_monthly_report_json_and_excel` (`xlsxwriter` not installed locally; present at base). |
| Cache-hit `/run` cost | Test9, median of 30, three rounds: base **42.9 / 43.0 / 43.1 ms**, feature **43.3 / 43.2 / 43.3 ms** (+0.2 to +0.4 ms). Cold first `/run` **+50 to +80 ms** (the one-time seed of two more tables). |

## 1. Byte-identical switch

**Method.** `verify_switch2.py` (below) is the stage-1 harness extended. Each (book,
flexible, wip) runs in a fresh subprocess with its own temp store, in the feature
worktree and in a pristine worktree of `1199583`. Production-like config:
`DEFAULT_SCHEDULER=new`, `apply_operator_logic=True`, overlap 80, plan start fixed at
06-10-2026. The book is the workbook's own SO sheet. WIP leg: plan once, store that
schedule as the last-applied plan, punch half of step 1 on 30 orders (entry date
05-10-2026), derive and store the frozen set with `api.main._compute_and_store_frozen()`
exactly as "Done entering" does, then plan again. Hash = sha256 of sorted (batch, seq,
machine, operator, start, end, qty) over every entry.

**Workbook made unreadable (feature only).** After the measured plan, every cache is
cleared (`_PLAN_CACHE`, `_MASTERS_CACHE` incl. the sha, `new_engine._MASTERS_CACHE`) and
`openpyxl.load_workbook`, `ppc_engine.loaders.workbook.open_workbook` and
`ppc_engine.loaders.loader.open_workbook` are replaced by functions that count the call
and RAISE. The plan is computed again from scratch (classic masters, new-engine masters,
freeze, fingerprint, report). Column NOWB.

**Role check (both checkouts).** `new_engine._apply_app_operators` is wrapped so every
operator it returns has `role=Role.HELPER`; caches cleared; plan again. Column ROLE.
Before the flip, roles in use were HELPER / INSPECTOR / OPERATOR on every run in both
checkouts.

| book | flex | wip | entries | frozen | base `1199583` | feature | NOWB (opens) | ROLE base / feature |
|---|---|---|---|---|---|---|---|---|
| Test5 | F | clean | 349 | 0 | 8039b77bc91caf2c | same | same (0) | same / same |
| Test5 | F | WIP | 348 | 17 | e67cc2c9662c0f8f | same | same (0) | same / same |
| Test5 | T | clean | 357 | 0 | 4226723695322de1 | same | same (0) | same / same |
| Test5 | T | WIP | 348 | 17 | 5a55f151ee835e61 | same | same (0) | same / same |
| Test8 | F | clean | 412 | 0 | bebc363c9a620afe | same | same (0) | same / same |
| Test8 | F | WIP | 412 | 19 | cef90f2f50b9d1c6 | same | same (0) | same / same |
| Test8 | T | clean | 412 | 0 | fe467c1b6b13aba2 | same | same (0) | same / same |
| Test8 | T | WIP | 411 | 19 | ffbe22df97a1077f | same | same (0) | same / same |
| Test9 | F | clean | 428 | 0 | 3cd8b2884343b360 | same | same (0) | same / same |
| Test9 | F | WIP | 426 | 10 | 95cbb28f49bb624f | same | same (0) | same / same |
| Test9 | T | clean | 428 | 0 | 62e6e9a1ed9ead4c | same | same (0) | same / same |
| Test9 | T | WIP | 424 | 10 | c91e287222af70e5 | same | same (0) | same / same |
| Test9-ORIGINAL | F | clean | 481 | 0 | 3cb85e6c720bfde2 | same | same (0) | same / same |
| Test9-ORIGINAL | F | WIP | 474 | 13 | a981602f3c9ac5ea | same | same (0) | same / same |
| Test9-ORIGINAL | T | clean | 474 | 0 | a793c8fbbb803228 | same | same (0) | same / same |
| Test9-ORIGINAL | T | WIP | 476 | 13 | 9cffc0a4889f9268 | same | same (0) | same / same |

Every hash is also the one the stage-1 verification recorded for `c123fa1`, so the
whole chain `c123fa1 -> 1199583 -> shop-masters` moves no work on these books.
Seeded tables: 26 machines on every book; holidays 3 (Test5, Test8) or 4 (Test9,
Test9-ORIGINAL; the fourth is 15-08-2026).

**Masters, field by field** (`dump_mc.py`, deterministic dump with sets sorted, Test5
and Test9). The raw repr hash of machines + calendar DIFFERS between base and feature
(e.g. classic Test9 `f3a310b9b4a638e6` vs `338147f1d7d496ae`). The only differences:

| object | base `1199583` | feature | plan effect |
|---|---|---|---|
| classic `Machine.hr_rate` | 250.0 / 500.0 on 22 of 26 machines | None | none: no reader (spec section 2 drops it) |
| classic `WorkCalendar.leaves` | Sidharam Singe 02-04-2025, Rohan Chakane 03-04-2025 | () | none at a 2026 plan start; Settings > Operator absences owns leave |
| ppc `ShopCalendar.leaves` | same two rows | {} | same |
| holidays, weekly off (3 = Thursday), every other machine field, ppc machines | | identical | |

So the brief's warning case (a dropped LEAVE row or the weekly-off handling moving a
hash) did not happen: no hash differs. (The ppc machines/calendar hash printed by the
switch harness itself varies run to run within ONE checkout because `repr` of a
frozenset depends on the process's hash seed; `dump_mc.py` sorts them, and on that
dump ppc machines are identical and ppc calendars differ only in `leaves`.)

**Quote, base vs feature** (`quote_compare2.py`, two draft lines on each book; the
feature leg run twice, plainly and with `NOWB=1`: one `/run` to seed, then every opener
raises and every cache is cleared before the quote). Identical on all four books, all
three legs, `moved 0, violations 0, verified True`:
Test5 QT1 01-12-2026 / QT2 18-12-2026; Test8 13-12-2026 / 23-04-2027; Test9
25-11-2026 / 23-11-2026; Test9-ORIGINAL 28-11-2026 / 30-11-2026 (also equal to stage 1).

**Remaining workbook readers** (grep of `load_all(`, `load_masters_bytes()`,
`open_workbook(` in `api/`, `engine/`, `ppc_engine/`): the one-time seeds
(`_seed_once` -> `item_master.seed_doc`, `shop_masters.seed_machines/seed_calendar`),
the operator table's one-time seed (`_with_operator_overlay`, only while that table is
empty), the fallback when a table is missing (`_current_masters`, `_new_masters`,
`parse_payload`), `_masters_sha` (hashes the bytes, never parses them), and
`build_payload`'s `masters_xlsx_b64` (sent only so an in-flight job or an old worker
still parses). Nothing else.

## 2. Live-store copy

**Not run (no credential this session); owner to run before deploy.** That includes
the quote on the store copy, live WIP, frozen ops, applied ranks and absences.

## 3. Role has no effect (spec 9.4)

See the ROLE column above: 16 of 16 unchanged in the feature and 16 of 16 in the
baseline, including the brief's case, Test9 WIP + frozen, `95cbb28f49bb624f` with
every operator a HELPER. Consistent with the spec's section-2 claim: role reaches only
`ppc_engine.worktime._shift_for`, which ignores it while `week_anchor=None`. Note that
in tables mode the new-engine masters carry no workbook operators, so EVERY role is now
inferred from the person's machines (`_role_for`'s fallback) rather than inherited by
name; the role flip shows that cannot move a plan.

## 4. Every reader follows an edit (feature only, Test9)

`verify_readers2.py`: throwaway in-process instance (TestClient, temp store), Test9,
the production-like config saved as the plan config, the tables seeded by the first
`GET /machines`. One punch on CNC1 dated 05-10-2026 so production analysis has a row.
Edits through the admin endpoints: `PUT /machines` CNC1 19.5 -> 9.5 h (200),
`POST /holidays` 2026-10-09 (a Friday inside the plan window, 200), `POST /machines`
CNC9 (CNC lathe, 19.5 h, 200). Stored workbook sha `58641ee7d70315a3` before and after:
**the workbook was not touched.**

| reader | before | after |
|---|---|---|
| `/run` schedule hash | 108a92cf2f2d | 7821f71dadea |
| `/run` CNC1 work minutes between 19:00 and 05:00 | 21,388.1 | 0.0 |
| `/run` op segments working on 09-10 (all machines) | 17 | 0 |
| expected completion (`optimizer.expected_completion`) hash / latest | 4b0048a7214e / 22-12-2026 | c45184aaced3 / 26-12-2026 |
| Analytics CNC1 busy / available / utilization | 757.7 h / 1,327.5 h / 57.1% | 676.7 h / 716.0 h / 94.5% |
| Analytics CNC3 available (holiday only) | 1,327.5 h | 1,362.0 h (see F3) |
| shift-wise rows hash | 31594d215653 | cb2b0f31c710 |
| shift-wise CNC1 rows by Shift label | First 89, Second 84, Day 0 | First 0, Second 0, Day shift (08:00-19:00) 151 |
| shift-wise rows dated 09-10-2026 | 17 | 0 |
| `/delay-report.xlsx` (built in-process; it writes with openpyxl, so it RAN) Summary / Detail sheet hash | 4b207e686492 / 5b7c058f4eee | 34660d9277dc / 04bb3bd8f941 |
| delay report Detail rows naming CNC1 / mentioning 09-10 | 526 / 145 | 387 / 48 |
| Daily Entry machine list (`/items` `machines`): count, CNC9 listed | 26, no | 27, yes |
| `/new-orders/quote` (QTEST1, QTEST2) | 24-11-2026, 23-11-2026 | 26-12-2026, 25-12-2026 |
| `parse_payload(build_payload(...))` through JSON, workbook bytes REMOVED from the payload: arity / CNC1 hours / two-shift / 09-10 a holiday / machines | 8 / 19.5 / True / False / 26 | 8 / 9.5 / False / True / 27 |
| same payload, worker path (`set_item_master` + `set_shop_masters`, no bytes) ppc CNC1 hours / `runs_second_shift` / 09-10 a holiday | 19.5 / True / False | 9.5 / False / True |
| production analysis (JSON) tables hash | 1a193a78066b | **1a193a78066b (unchanged)** |
| Daily Entry / production analysis shift working minutes | 1st 630, 2nd 570 | **unchanged** |
| Daily Entry CNC1 standard setting time | 90 | 90 (unchanged; CNC1 is still CNC) |

Extra (the production analysis input that DOES follow the Machines table): the
standard setting time the Daily Entry form fills follows a machine's TYPE. `MX9` added
as a Vertical Machining center: 90 min; retyped Manual Packing: **0** (both 200). But
`CNC9` retyped Manual Packing stays at **90**, because `rule6._is_setup_machine` answers
yes for any id starting CNC/VMC whatever its type. The PLAN ignores type altogether
(see F4).

`/production-analysis.xlsx` was not exercised (needs `xlsxwriter`, not installed
locally); the JSON endpoint above reads the same `_production_tables`.

## 5. Mutation testing

`mutate2.py`: each mutation applied alone (exactly-one-match string replace), `python3.12
-B -m pytest` on `tests/test_shop_*.py` + `tests/test_item_master*.py` (10 files, 126
tests unmutated, all pass), restored with `git checkout --`, `git status` clean
asserted before the next (clean at the end). A 1.1 s sleep after each write guards
against stale bytecode.

| # | mutation | failing tests (shop + item-master files) |
|---|---|---|
| 1 | `_current_masters` never takes the tables-only branch (`if False:`) | **0. Whole suite: 0** (only the pre-existing xlsxwriter failure) |
| 2 | `_new_masters` ignores the machines doc (`mdoc = None`) | 1: test_new_engine_masters_follow_the_tables_without_a_workbook |
| 3 | `parse_payload` ignores `machines` | **0. Whole suite: 0** |
| 4 | `run_candidate` skips `set_shop_masters` | 1: test_run_candidate_feeds_the_shop_tables |
| 5 | `_plan_fingerprint` drops the two digests | 1: test_signatures_unchanged_by_seeding_and_moved_by_edits |
| 6 | `_inputs_signature` never appends them | 1: test_signatures_unchanged_by_seeding_and_moved_by_edits |
| 7 | `machine_usage` returns `[]` | 3: test_delete_machine_refused_while_used_then_allowed, test_get_machines_for_both_roles, test_machine_usage_names_every_user |
| 8 | `apply_machine_save` skips the version check | 2: test_edit_machine_with_version_check, test_apply_machine_save_versions |
| 9 | the operator seed no longer reads the workbook (`seed_from = []`) | 5: test_done_after_the_edit_freezes_the_running_step_by_name, test_removing_a_step_ahead_keeps_the_running_step_pinned_on_replan, test_masters_never_open_the_workbook_once_seeded, test_masters_sha_is_cached_not_refetched_per_call, test_operator_seed_still_reads_the_workbook_once |
| 10a | classic `_calendar_from_holiday_rows` returns no holidays | 4: test_holidays_add_list_delete, test_classic_tables_only_equals_workbook_except_dropped_fields, test_masters_follow_a_machine_edit_and_a_holiday, test_payload_round_trips_the_shop_tables |
| 10b | ppc `calendar_from_holiday_rows` returns no holidays | 2: test_calendar_from_holiday_rows_is_thursday_without_leaves, test_ppc_tables_only_equals_workbook_masters |

**The two survivors, measured on Test9** (`survivors.py`, feature checkout, mutation
applied by hand, restored, `git status` clean):

- **#1 is load-bearing, untested.** With the tables present, `engine.loaders.load_all`
  itself ignores the workbook when all three row sets are given, so on a store that HAS
  a workbook the mutation only adds one bytes read. On a store with the three tables
  and NO workbook (a fresh install whose admin entered everything by hand, the spec's
  own section-4 case) it returns `Masters()`: **classic masters 0 machines, 0
  routings; plan 428 -> 422 entries** (unmutated: 26 machines, 106 routings, 428
  entries). Analytics, `/items`, Daily Entry, the freeze and the operator picker all
  read these classic masters, so they would see no machines and no routings (inferred
  from the code, not measured one by one). `test_masters_never_open_the_workbook_once_seeded`
  does not catch it because the workbook is still on file there. Suggested test: seed,
  then make `load_masters_bytes` return None, assert `_current_masters()` still has
  machines and routings.
- **#3 is belt-and-braces under the new engine, untested.** After CNC1 is edited to
  9.5 h, a payload sent WITH the workbook bytes (as production sends it) parses to
  classic CNC1 **19.5 h** under the mutation (9.5 unmutated). One worker candidate
  (`run_candidate`, overlap 80, budget 3, operator table included) scores **identically
  either way: 68 orders, 5,339 late-days, makespan 83.73 d**, because the worker's
  new-engine masters come from `set_shop_masters`, not from `parse_payload`. It would
  matter on a classic / flow worker, or to any future worker code that reads the
  classic machines. `test_payload_round_trips_the_shop_tables` edits only a holiday;
  adding a machine-hours edit to it would pin #3.

## 6. Full suite

`python3.12 -m pytest -q`: **1 failed, 1272 passed, 4 skipped (80.7 s).** The one
failure is `tests/test_production_analysis.py::test_monthly_report_json_and_excel`
(`ModuleNotFoundError: xlsxwriter`), pre-existing at base and environment-only.

## 7. Cache-hit `POST /run` timing (Test9)

`time_cache_hit.py` (the stage-1 script, unchanged; it already seeds through
`save_masters_bytes`), cold run then 30 cache hits (same `run_id` asserted), three
rounds alternating base/feature:

| round | base `1199583` median (p90) | feature median (p90) | cold base / feature |
|---|---|---|---|
| 1 | 42.9 (43.4) ms | 43.3 (44.5) ms | 1,751 / 1,830 ms |
| 2 | 43.0 (43.2) ms | 43.2 (44.3) ms | 1,746 / 1,800 ms |
| 3 | 43.1 (43.7) ms | 43.3 (44.8) ms | 1,746 / 1,822 ms |

+0.2 to +0.4 ms per cache hit on the local file store (two more small store reads; the
workbook sha is cached in-process since `639d3bd`). Cold first plan +50 to +80 ms: the
one-time seed of the Machines and holiday tables. Not measured on Render / MongoDB.

## Findings

- **F1 (test gap).** Mutations #1 and #3 fail no test (section 5). #1 matters on a
  store with tables and no workbook; #3 does not change a new-engine worker's score.
  Two small tests would pin both; not added here (Task 9 changes no code).
- **F2 (expectation not met, by design).** Production analysis does not move on a
  machine-shift or holiday edit. Its working minutes are the shift's minutes after the
  meal break (`_shift_minutes(config)`, 630 / 570), from the plan config, never from
  the Machines table or the holiday list; a holiday has no punches to analyse. What it
  does take from the Machines table is the standard setting time the Daily Entry form
  fills (by type, see F4).
- **F3 (expected).** Analytics "Available" of an UNEDITED machine moved (CNC3 1,327.5 ->
  1,362.0 h) because availability is measured over the plan window and the edit made
  the plan longer (latest completion 22-12 -> 26-12), net of the holiday.
- **F4 (UI copy is wrong; measured).** A machine's TYPE does not change the plan.
  `retype.py` on Test9: CNC1 retyped CNC lathe -> Manual Packing, and MD1 Manual
  deburring -> CNC lathe (hours kept, both 200): schedule hash **`3cd8b2884343`
  unchanged both times**, CNC1 62,089 and MD1 7,270 machine minutes unchanged, although
  the new-engine `Machine.kind` did flip (machining <-> manual). The 90-minute setup and
  the +30% are charged by OPERATION kind, which `ppc_engine.loaders.normalize.
  classify_operation` takes from the step's machine ID prefix (CNC* / VMC*), never from
  the Machine master type; `Machine.kind` feeds only the role inference, which has no
  plan effect (section 3). So the Machines card's explainer (`web/index.html` ~line 258,
  "The type decides how a machine is planned: CNC and VMC types get the 90 minute setup
  and the extra 30% on cycle times") is not true, and the Task 8 review worry that one
  mis-picked type silently drops setup + 30% does not hold either. Where type DOES act:
  the Daily Entry standard setting time for an id that does not start CNC/VMC (MX9:
  VMC 90 min, Manual Packing 0), the maintenance-downtime picker's machining filter, and
  the retired classic engine. `CNC9` retyped Manual Packing keeps 90 min on the Daily
  Entry form (`rule6._is_setup_machine` checks the id prefix first). Not fixed here
  (Task 9 changes no product code); the card copy needs the owner's wording.
- **F5 (minor).** A payload carrying all three tables but NO `operator_table` now plans
  with no operators (the worker's masters no longer read the workbook's operator
  sheet): an early run of `survivors.py` without the operator table scored 0 orders.
  Production's `build_payload` always sends the operator table, so this is not
  reachable from the app; recorded so a future caller does not trip on it.
- **F6 (intended, recorded).** Machines no longer carry `Hr Rate`; the calendar no
  longer carries the workbook's two April-2025 leave rows. No reader / no plan effect
  (section 1).

## What was NOT run

- The live-store copy (no `MONGODB_URI` this session; owner to run before deploy), and
  therefore the quote on the store copy with live WIP, frozen ops, applied ranks and
  absences.
- The browser run of the two Settings cards as both roles (spec 9.6).
- `/production-analysis.xlsx` (needs `xlsxwriter`, not installed locally).
- Cloud / GitHub Actions / Mac worker end to end: the payload round trip, the worker's
  masters path and one `run_candidate` were exercised in-process only.
- Timing on Render / MongoDB.

## Harness sources

All in the session scratchpad (`t9/`), not committed. Run from the root of the checkout
under test with `python3.12 -B <script> <book.xlsx> ...`. `time_cache_hit.py` is the
stage-1 script, unchanged (its source is in
`2026-10-05-item-process-master-verification.md`); `quote_compare2.py` is the stage-1
`quote_compare.py` plus the `NOWB=1` block.

### `verify_switch2.py`

```python
"""Byte-identical switch harness, stage 2 (Task 9, machines + holidays in the app).

Run from the root of a checkout (cwd = repo root), in the feature worktree AND in a
pristine worktree of 1199583 (stage 1 live). Each (book, flexible, wip) runs in a
FRESH subprocess with its own temp STORE_DIR.

The book goes into the store directly (book_store.save_masters_bytes): the feature
branch has no /upload. One line per run:
  book, flexible, wip, #entries, #frozen, schedule hash,
  classic machines+calendar hash, ppc machines+calendar hash,
  NOWB=<hash after the workbook is made unreadable> (feature only),
  ROLE=<hash with every new-engine operator's role forced to HELPER>,
  seeded=<machines/holidays table sizes> (feature only).

NOWB leg: after the first plan has seeded every table, every caches is cleared and
openpyxl.load_workbook, ppc_engine.loaders.workbook.open_workbook and
ppc_engine.loaders.loader.open_workbook are replaced by functions that RAISE; the plan
is computed again from scratch. A workbook open anywhere would crash the run.
"""
import hashlib, io, os, subprocess, sys, tempfile
from dataclasses import replace
from datetime import date, timedelta

PLAN_DAY = date(2026, 10, 6)
WIP_ORDERS = 30


def _h(obj) -> str:
    return hashlib.sha256(repr(obj).encode()).hexdigest()[:16]


def _sched_hash(sched):
    return hashlib.sha256(repr(sorted(
        (e.batch_id, e.process_seq, e.machine, e.operator, e.start.isoformat(),
         e.end.isoformat(), e.qty) for e in sched)).encode()).hexdigest()[:16]


OPENS = {"n": 0}


def _make_unreadable():
    import openpyxl
    import ppc_engine.loaders.workbook as wbmod
    import ppc_engine.loaders.loader as ldr

    def boom(*a, **k):
        OPENS["n"] += 1
        raise RuntimeError("WORKBOOK OPENED")
    openpyxl.load_workbook = boom
    wbmod.open_workbook = boom
    ldr.open_workbook = boom


def _clear_caches(m, new_engine):
    m._PLAN_CACHE.update(key=None, result=None)
    m._MASTERS_CACHE.update(key=None, masters=None)
    for k in ("sha", "sha_key"):
        m._MASTERS_CACHE.pop(k, None)
    new_engine._MASTERS_CACHE.clear()


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
    sh = _sched_hash(sched)
    cm = m._current_masters()
    classic = _h((list(cm.machines.items()), cm.calendar))
    pm = new_engine._new_masters(flexible)
    ppc = _h((list(pm.machines.items()), pm.calendar))

    feature = hasattr(book_store, "load_machines_table") or os.path.exists("engine/shop_masters.py")
    seeded = "n/a"
    nowb = "n/a"
    if feature:
        from engine import shop_masters  # noqa
        mdoc = m._machines_doc()
        cdoc = m._calendar_doc()
        seeded = "machines=%d holidays=%d" % (len(mdoc["machines"]), len(cdoc["holidays"]))
        _clear_caches(m, new_engine)
        _make_unreadable()
        try:
            m._plan(cfg)
            nowb = _sched_hash(m._PLAN_CACHE["artifacts"]["plan_run"].schedule)
            pm2 = new_engine._new_masters(flexible)
            cm2 = m._current_masters()
            nowb += " classic=%s ppc=%s" % (
                _h((list(cm2.machines.items()), cm2.calendar)),
                _h((list(pm2.machines.items()), pm2.calendar)))
        except Exception as ex:  # noqa
            nowb = "FAILED:%r" % ex
        nowb += " opens=%d" % OPENS["n"]

    # Role check: every new-engine operator becomes HELPER.
    from ppc_engine.domain.resources import Role
    orig = new_engine._apply_app_operators

    def all_helpers(new_masters, old_masters):
        out = orig(new_masters, old_masters)
        return replace(out, operators=tuple(replace(o, role=Role.HELPER)
                                            for o in out.operators))
    roles_before = sorted({o.role.name for o in orig(pm, m._current_masters()).operators})
    new_engine._apply_app_operators = all_helpers
    _clear_caches(m, new_engine)
    m._plan(cfg)
    role = _sched_hash(m._PLAN_CACHE["artifacts"]["plan_run"].schedule)
    new_engine._apply_app_operators = orig
    print(os.path.basename(book), flexible, wip, len(sched), n_frozen, sh,
          "classicMC=" + classic, "ppcMC=" + ppc, "NOWB=" + nowb,
          "ROLE=" + role + ("(same)" if role == sh else "(DIFF)"),
          "roles_before=" + "/".join(roles_before),
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

### `dump_mc.py`

```python
"""Dump classic + ppc machines and calendar (deterministic: sets sorted) for one book,
to compare base vs feature field by field. Output: a pickle-free text repr per line."""
import io, os, sys, tempfile, dataclasses, enum
os.environ["STORE_DIR"] = tempfile.mkdtemp(); os.environ["DEFAULT_SCHEDULER"] = "new"
os.environ["AUTO_OPTIMIZE"] = "0"
sys.path.insert(0, os.getcwd())
from engine import book_store, new_engine
import api.main as m


def norm(v):
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return (type(v).__name__, tuple((f.name, norm(getattr(v, f.name)))
                                        for f in dataclasses.fields(v)))
    if isinstance(v, enum.Enum):
        return str(v)
    if isinstance(v, dict):
        return tuple((norm(k), norm(x)) for k, x in v.items())
    if isinstance(v, (set, frozenset)):
        return tuple(sorted((norm(x) for x in v), key=repr))
    if isinstance(v, (list, tuple)):
        return tuple(norm(x) for x in v)
    if hasattr(v, "__dict__") and not isinstance(v, type):
        return (type(v).__name__, tuple((k, norm(x)) for k, x in sorted(vars(v).items())))
    return v


book_store.save_masters_bytes(open(sys.argv[1], "rb").read())
cm = m._current_masters()
print("CLASSIC_MACHINES", norm(cm.machines))
print("CLASSIC_CALENDAR", norm(cm.calendar))
for flex in (False, True):
    pm = new_engine._new_masters(flex)
    print("PPC_MACHINES", flex, norm(pm.machines))
    print("PPC_CALENDAR", flex, norm(pm.calendar))
```

### `quote_compare2.py`

```python
"""(Task 9: NOWB=1 makes the stored workbook unreadable after seeding.) Two-line /new-orders/quote on a real book; runs identically in base and feature."""
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
if os.environ.get("NOWB") == "1":
    # Seed every table with one plan, then make the workbook unreadable and re-quote.
    assert c.post("/run", json={}).status_code == 200
    import openpyxl, ppc_engine.loaders.workbook as wbmod, ppc_engine.loaders.loader as ldr
    from engine import new_engine
    def boom(*a, **k):
        raise RuntimeError("WORKBOOK OPENED")
    openpyxl.load_workbook = wbmod.open_workbook = ldr.open_workbook = boom
    m._PLAN_CACHE.update(key=None, result=None); m._MASTERS_CACHE.update(key=None, masters=None)
    new_engine._MASTERS_CACHE.clear()
q = c.post("/new-orders/quote").json()
print(os.path.basename(sys.argv[1]), [(l["so_no"], l["item_code"], l["completion"]) for l in q["lines"]],
      "moved", len(q["moved"]), "violations", len(q["violations"]), "verified", q["verified"])
```

### `verify_readers2.py`

```python
"""Every reader follows a Machines / holidays edit (Task 9, step 4). FEATURE only.

Throwaway in-process instance (TestClient, temp STORE_DIR), Test9, production-like
config saved as the plan config, tables seeded by the first GET /machines. Edits,
all through the admin endpoints:
  (1) PUT /machines  CNC1 19.5 h -> 9.5 h (2 shifts -> 1 shift)
  (2) POST /holidays 2026-10-09 (a Friday inside the plan window)
  (3) POST /machines add CNC9 (CNC lathe, 19.5 h): for the Daily Entry machine list
Each reader is measured before and after; the stored workbook sha is printed both
times (it must not change).
"""
import base64, hashlib, io, json, os, sys, tempfile
from datetime import date, datetime, time as dtime

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
MID = "CNC1"
HOL = date(2026, 10, 9)
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
mach = c.get("/machines").json()
assert book_store.load_machines_doc() is not None and book_store.load_shop_calendar() is not None
op_name = m._current_masters().operators[0].name
# One punch on CNC1 in October so production analysis has a CNC1 row.
o0 = orders[0]
r0 = m._current_masters().routings[o0.item_code].processes[0].name
book_store.append_actual(Actual(so_no=o0.so_no, item_code=o0.item_code,
                                entry_date=date(2026, 10, 5), shift="2nd shift",
                                process=r0, qty_produced=1, qty_rejected=0,
                                operator=op_name, machine=MID))
wb_sha = lambda: hashlib.sha256(book_store.load_masters_bytes()).hexdigest()[:16]
H = lambda x: hashlib.sha256(json.dumps(x, default=str, sort_keys=True).encode()).hexdigest()[:12]


def night_minutes(entries):
    """Minutes of the given entries' op segments that fall in 19:00-05:00."""
    tot = 0.0
    for e in entries:
        segs = getattr(e, "op_segments", None) or [(e.start, e.end)]
        for seg in segs:
            s, t = (seg[0], seg[1]) if isinstance(seg, (tuple, list)) else (seg.start, seg.end)
            cur = s
            while cur < t:
                nxt = min(t, datetime.combine(cur.date(), dtime(cur.hour)) .replace(minute=0)
                          + __import__("datetime").timedelta(hours=1))
                if cur.hour >= 19 or cur.hour < 5:
                    tot += (nxt - cur).total_seconds() / 60
                cur = nxt
    return round(tot, 1)


def touches(entries, d):
    n = 0
    for e in entries:
        segs = getattr(e, "op_segments", None) or [(e.start, e.end)]
        for seg in segs:
            s, t = (seg[0], seg[1]) if isinstance(seg, (tuple, list)) else (seg.start, seg.end)
            if s.date() <= d <= t.date() and not (t == datetime.combine(d, dtime(0))):
                # work strictly inside the day
                lo = max(s, datetime.combine(d, dtime(0)))
                hi = min(t, datetime.combine(d, dtime(23, 59, 59)))
                if hi > lo:
                    n += 1
    return n


def measure():
    out = {}
    run = c.post("/run", json={}).json()
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    real = [e for e in sched if e.machine and not str(e.machine).startswith(("OS", "Off"))]
    out["run: schedule hash"] = hashlib.sha256(repr(sorted(
        (e.batch_id, e.process_seq, e.machine, e.operator, e.start.isoformat(),
         e.end.isoformat(), e.qty) for e in sched)).encode()).hexdigest()[:12]
    cnc1 = [e for e in sched if e.machine == MID]
    out[f"run: {MID} night (19:00-05:00) work minutes"] = night_minutes(cnc1)
    out[f"run: segments working on {HOL:%d-%m} (all machines)"] = touches(real, HOL)
    from engine import optimizer
    exp = optimizer.expected_completion(sched)
    out["run: expected completion hash / latest"] = (H(sorted((str(k), str(v)) for k, v in exp.items())),
                                                    str(max(exp.values())))
    an = run["trace"]["analytics"]
    mrows = an["machines"]["rows"] if isinstance(an["machines"], dict) else an["machines"]
    mrows = [r if isinstance(r, dict) else dict(zip(an["machines"]["columns"], r)) for r in mrows]
    row = next(r for r in mrows if str(r["Machine"]).replace(" ", "") == MID)
    out[f"analytics: {MID} row"] = {k: v for k, v in row.items() if "vail" in k or "tiliz" in k or "usy" in k}
    other = next(r for r in mrows if str(r["Machine"]).replace(" ", "") == "CNC3")
    out["analytics: CNC3 available (holiday only)"] = {k: v for k, v in other.items() if "vail" in k}
    sw = (run.get("trace", {}).get("rule6", {}) or {}).get("shiftwise") or {}
    rows = sw.get("rows", [])
    cols = sw.get("columns", [])
    out["shift-wise: rows hash"] = H(rows)
    txt = [json.dumps(r, default=str) for r in rows]
    drows = [r if isinstance(r, dict) else dict(zip(cols, r)) for r in rows]
    out["shift-wise: shift labels seen"] = sorted({str(r.get("Shift")) for r in drows})
    out[f"shift-wise: {MID} rows by Shift label"] = {
        lab: sum(1 for r in drows if str(r.get("Machine", "")).replace(" ", "") == MID and str(r.get("Shift")) == lab)
        for lab in sorted({str(r.get("Shift")) for r in drows})}
    out[f"shift-wise: rows dated {HOL:%d-%m-%Y}"] = sum(1 for r in drows if str(r.get("Date")) == HOL.isoformat()
                                                       or str(r.get("Date")) == HOL.strftime("%d-%m-%Y"))
    dr = c.get("/delay-report.xlsx")
    out["delay report: status"] = dr.status_code
    if dr.status_code == 200:
        wb = load_workbook(io.BytesIO(dr.content), read_only=True)
        sheets = {}
        for name in wb.sheetnames:
            sheets[name] = [list(r) for r in wb[name].iter_rows(values_only=True)]
        out["delay report: per-sheet hash"] = {n: H(v) for n, v in sheets.items()}
        det = next((v for n, v in sheets.items() if "etail" in n), None)
        if det:
            out[f"delay report: detail rows naming {MID}"] = sum(1 for r in det if MID in [str(x) for x in r])
            out[f"delay report: detail rows on {HOL:%d-%m}"] = sum(
                1 for r in det if any(HOL.strftime("%d-%m") in str(x) for x in r))
    its = c.get("/items").json()
    ids = [x["id"] for x in its["machines"]]
    out["daily entry: machine list (count, has CNC9)"] = (len(ids), "CNC9" in ids)
    out[f"daily entry: {MID} std setup"] = next(x["std_setup_min"] for x in its["machines"] if x["id"] == MID)
    out["daily entry / production analysis: shift working minutes"] = its["shift_minutes"]
    pa = c.get("/production-analysis", params={"year": 2026, "month": 10}).json()
    out["production analysis: tables hash"] = H({k: pa[k] for k in ("operators", "shifts", "entries")})
    drafts = {"drafts": [{"so_no": "QTEST1", "item_code": orders[0].item_code, "qty": 50},
                         {"so_no": "QTEST2", "item_code": next(o.item_code for o in orders if o.item_code != orders[0].item_code), "qty": 50}]}
    assert c.put("/new-orders/drafts", json=drafts).status_code == 200
    q = c.post("/new-orders/quote")
    out["quote"] = [(l.get("so_no"), l.get("completion")) for l in q.json()["lines"]] if q.status_code == 200 else q.status_code
    # payload round trip (what the GitHub / Mac / Oracle worker rebuilds)
    p = optimize_service.build_payload(book_store.load_active_orders(), book_store.load_actuals(),
                                       book_store.load_masters_bytes(), cfg, seed=1,
                                       item_master=book_store.load_item_master(),
                                       machines=book_store.load_machines_doc(),
                                       shop_calendar=book_store.load_shop_calendar())
    p = json.loads(json.dumps(p))
    p_nowb = dict(p, masters_xlsx_b64=None)           # tables alone must be enough
    parsed = optimize_service.parse_payload(p_nowb)
    cm = parsed[2].machines[MID]
    out["payload: arity / classic hrs / 2-shift / holiday in calendar / #machines"] = (
        len(parsed), cm.available_hrs_per_day, cm.is_two_shift(), HOL in parsed[2].calendar.holidays,
        len(parsed[2].machines))
    new_engine._MASTERS_CACHE.clear()
    new_engine.set_masters_bytes(None)
    new_engine.set_item_master(p["item_master"])
    new_engine.set_shop_masters(p["machines"], p["shop_calendar"])
    try:
        nm = new_engine._new_masters(False)
        pmc = nm.machines[MID]
        out["payload: worker ppc hrs / runs 2nd shift / holiday"] = (
            pmc.available_hrs_per_day, pmc.runs_second_shift, HOL in nm.calendar.holidays)
    finally:
        new_engine.set_masters_bytes(None)
        new_engine.clear_item_master_override()
        new_engine.clear_shop_masters_override()
        new_engine._MASTERS_CACHE.clear()
    out["workbook sha"] = wb_sha()
    return out


before = measure()
cur = next(x for x in c.get("/machines").json()["machines"] if x["id"] == MID)
r1 = c.put("/machines", json={"id": MID, "type": cur["type"], "hours": 9.5, "version": cur["version"]})
r2 = c.post("/holidays", json={"date": HOL.isoformat(), "name": "Test holiday"})
r3 = c.post("/machines", json={"id": "CNC9", "type": "CNC lathe", "hours": 19.5})
print("PUT /machines", r1.status_code, f"{MID} {cur['hours']} -> 9.5 h", "| POST /holidays", r2.status_code,
      HOL.isoformat(), "| POST /machines CNC9", r3.status_code)
after = measure()
for k in before:
    flag = "MOVED" if before[k] != after[k] else "same"
    print(f"{flag:5}  {k}\n         before: {before[k]}\n         after:  {after[k]}")

# Extra: a TYPE change reaches the Daily Entry standard setting time (a production
# analysis input). A non-CNC id is used: _is_setup_machine answers True for any id
# starting CNC/VMC whatever its type (pre-existing rule, recorded in the report).
rA = c.post("/machines", json={"id": "MX9", "type": "Vertical Machining center", "hours": 19.5})
stdA = next(x["std_setup_min"] for x in c.get("/items").json()["machines"] if x["id"] == "MX9")
cur9 = next(x for x in c.get("/machines").json()["machines"] if x["id"] == "MX9")
rB = c.put("/machines", json={"id": "MX9", "type": "Manual Packing", "hours": 9.5, "version": cur9["version"]})
stdB = next(x["std_setup_min"] for x in c.get("/items").json()["machines"] if x["id"] == "MX9")
print("POST MX9 (VMC)", rA.status_code, "std setup", stdA, "| PUT MX9 -> Manual Packing", rB.status_code, "std setup", stdB)
cur9 = next(x for x in c.get("/machines").json()["machines"] if x["id"] == "CNC9")
r4 = c.put("/machines", json={"id": "CNC9", "type": "Manual Packing", "hours": 9.5, "version": cur9["version"]})
std = next(x["std_setup_min"] for x in c.get("/items").json()["machines"] if x["id"] == "CNC9")
print("PUT CNC9 -> Manual Packing", r4.status_code, "| CNC9 std setup still", std, "(id prefix rule)")
```

### `mutate2.py`

```python
"""Mutation testing for stage 2 (machines + holidays in the app), Task 9 step 5.
Run from the feature worktree root. Each mutation: apply (exactly-one-match string
replace), run the shop + item-master test files with -B, record failures, restore
with `git checkout -- <file>`, assert `git status --porcelain` is clean before the
next. `--full` runs the whole suite instead. Never commits."""
import re, subprocess, sys, time, glob

TESTS = sorted(glob.glob("tests/test_shop_*.py") + glob.glob("tests/test_item_master*.py"))

M = [
    ("1 _current_masters never takes the tables-only branch", "api/main.py",
     "        if None not in rows.values():\n            _, base = load_all(None, **rows)",
     "        if False:\n            _, base = load_all(None, **rows)"),
    ("2 _new_masters ignores the machines doc", "engine/new_engine.py",
     "    mdoc, cdoc = _shop_docs()\n    tables_only",
     "    mdoc, cdoc = _shop_docs()\n    mdoc = None\n    tables_only"),
    ("3 parse_payload ignores machines", "engine/optimize_service.py",
     'idoc, mdoc, cdoc = (payload.get("item_master"), payload.get("machines"),',
     'idoc, mdoc, cdoc = (payload.get("item_master"), None,'),
    ("4 run_candidate skips set_shop_masters", "engine/optimize_service.py",
     '        new_engine.set_shop_masters(payload.get("machines"), payload.get("shop_calendar"))\n',
     '        pass\n'),
    ("5 _plan_fingerprint drops the two digests", "api/main.py",
     '        "machines": shop_masters.machines_digest(book_store.load_machines_doc()),\n'
     '        "shop_calendar": shop_masters.calendar_digest(book_store.load_shop_calendar()),\n',
     ''),
    ("6 _inputs_signature never appends them", "api/main.py",
     '            ("machines", book_store.load_machines_doc(), shop_masters.machines_digest),\n'
     '            ("shop_calendar", book_store.load_shop_calendar(),\n'
     '             shop_masters.calendar_digest)):',
     '            ):'),
    ("7 machine_usage returns []", "engine/shop_masters.py",
     '    """Everything that still names this machine, in plain words (empty = free)."""\n',
     '    """Everything that still names this machine, in plain words (empty = free)."""\n    return []\n'),
    ("8 apply_machine_save skips the version check", "engine/shop_masters.py",
     'if cur is None or cur.get("version") != expected_version:',
     'if cur is None:'),
    ("9 operator seed no longer reads the workbook", "api/main.py",
     "                seed_from = load_all(io.BytesIO(raw))[1].operators\n",
     "                seed_from = []\n"),
    ("10a classic _calendar_from_holiday_rows returns no holidays", "engine/loaders.py",
     "holidays=[d for d, _n in rows], leaves=[])",
     "holidays=[], leaves=[])"),
    ("10b ppc calendar_from_holiday_rows returns no holidays", "ppc_engine/loaders/masters_loader.py",
     "holidays=frozenset(d for d, _name in rows), leaves={})",
     "holidays=frozenset(), leaves={})"),
]


def clean():
    return subprocess.run(["git", "status", "--porcelain"], capture_output=True,
                          text=True).stdout.strip() == ""


assert clean(), "worktree not clean before mutating"
args = set(sys.argv[1:])
tests = ["tests"] if "--full" in args else TESTS
only = args - {"--full"}
print("test files:", " ".join(tests))
for name, path, old, new in M:
    if only and name.split()[0] not in only:
        continue
    src = open(path).read()
    n = src.count(old)
    if n != 1:
        print(f"!! {name}: pattern matched {n} times, SKIPPED", flush=True)
        continue
    open(path, "w").write(src.replace(old, new))
    time.sleep(1.1)
    r = subprocess.run([sys.executable, "-B", "-m", "pytest", *tests, "-q",
                        "-p", "no:cacheprovider"], capture_output=True, text=True)
    subprocess.run(["git", "checkout", "--", path], check=True)
    assert clean(), f"restore failed after {name}"
    failed = sorted(set(re.findall(r"^(?:FAILED|ERROR) (\S+)", r.stdout, re.M)))
    summary = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else r.stderr[-300:]
    print(f"== {name}\n   {summary}")
    for f in failed:
        print(f"   FAILED {f}")
    sys.stdout.flush()
print("worktree clean at end:", clean())
```

### `survivors.py`

```python
"""Do the two mutations that fail no test (1 and 3) matter on a real book? FEATURE
checkout; run once unmutated and once with each mutation applied by hand.

(a) mutation 1 (_current_masters never takes the tables-only branch): tables seeded,
    then the store's workbook is taken away (load_masters_bytes -> None), caches
    cleared, plan again. A fresh install whose admin enters every table by hand is
    exactly this state.
(b) mutation 3 (parse_payload ignores machines): CNC1 edited to 9.5 h, payload built
    WITH the workbook bytes (as production sends it), parse_payload's classic CNC1
    hours, and one worker candidate (run_candidate, budget 3) scored.
"""
import io, json, os, sys, tempfile
from datetime import date
os.environ["STORE_DIR"] = tempfile.mkdtemp(); os.environ["DEFAULT_SCHEDULER"] = "new"
os.environ["AUTO_OPTIMIZE"] = "0"
sys.path.insert(0, os.getcwd())
from engine import book_store, loaders, new_engine, optimize_service, shop_masters
from engine.models import Order
from engine.config import Config
import api.main as m

raw = open(sys.argv[1], "rb").read()
book_store.save_masters_bytes(raw)
so, _ = loaders.load_all(io.BytesIO(raw))
book_store.add_orders([Order(s.so_no, s.item_code, s.item_name, s.qty, s.delivery_date) for s in so])
cfg = Config(scheduler="new", apply_operator_logic=True, overlap_percent=80,
             plan_start_date=date(2026, 10, 6))
book_store.save_plan_config(json.dumps(cfg.to_dict(), default=str))
m._plan(cfg)
n0 = len(m._PLAN_CACHE["artifacts"]["plan_run"].schedule)

# (a)
real = book_store.load_masters_bytes
book_store.load_masters_bytes = lambda: None
m._PLAN_CACHE.update(key=None, result=None); m._MASTERS_CACHE.update(key=None, masters=None)
m._MASTERS_CACHE.pop("sha", None); new_engine._MASTERS_CACHE.clear()
try:
    m._plan(cfg)
    n1 = len(m._PLAN_CACHE["artifacts"]["plan_run"].schedule)
    cm = m._current_masters()
    print(f"(a) workbook gone: entries {n0} -> {n1}; classic masters machines {len(cm.machines)}, "
          f"routings {len(cm.routings)}")
except Exception as ex:
    print("(a) workbook gone: plan FAILED", repr(ex)[:300])
book_store.load_masters_bytes = real
m._PLAN_CACHE.update(key=None, result=None); m._MASTERS_CACHE.update(key=None, masters=None)
m._MASTERS_CACHE.pop("sha", None)

# (b)
mdoc = m._machines_doc()
mdoc["machines"]["CNC1"]["hours"] = 9.5
book_store.save_machines_doc(mdoc)
p = optimize_service.build_payload(book_store.load_active_orders(), book_store.load_actuals(),
                                   book_store.load_masters_bytes(), cfg, seed=1,
                                   budget_per_candidate=3,
                                   operator_table=book_store.load_operator_table(),
                                   item_master=book_store.load_item_master(),
                                   machines=book_store.load_machines_doc(),
                                   shop_calendar=book_store.load_shop_calendar())
p = json.loads(json.dumps(p))
parsed = optimize_service.parse_payload(p)
print("(b) parse_payload classic CNC1 hours:", parsed[2].machines["CNC1"].available_hrs_per_day)
res = optimize_service.run_candidate(p, 80, False)
new_engine.clear_shop_masters_override(); new_engine.clear_item_master_override()
new_engine.set_masters_bytes(None)
b = res["best"]
print("(b) worker candidate best: orders", b["orders"], "late days", b["total_late_days"], "makespan", b["makespan_days"], "evals", res["evals"])
```

### `retype.py`

```python
"""Does a machine's TYPE (as edited on the Machines card) change the plan? FEATURE only.
Test9, production-like config. Plan; retype CNC1 'CNC lathe' -> 'Manual Packing'
(hours kept 19.5); plan; restore; retype MD1 (a manual deburring station) -> 'CNC
lathe' (hours kept); plan. Reports schedule hash and the CNC1 / MD1 machine minutes."""
import hashlib, io, json, os, sys, tempfile
from datetime import date
os.environ["STORE_DIR"] = tempfile.mkdtemp(); os.environ["DEFAULT_SCHEDULER"] = "new"
os.environ["AUTO_OPTIMIZE"] = "0"; os.environ["ADMIN_PASSWORD"] = "1930rail"
sys.path.insert(0, os.getcwd())
from fastapi.testclient import TestClient
from engine import book_store, loaders, new_engine
from engine.models import Order
from engine.config import Config
import api.main as m
raw = open(sys.argv[1], "rb").read(); book_store.save_masters_bytes(raw)
so, _ = loaders.load_all(io.BytesIO(raw))
book_store.add_orders([Order(s.so_no, s.item_code, s.item_name, s.qty, s.delivery_date) for s in so])
book_store.save_plan_config(json.dumps(Config(scheduler="new", apply_operator_logic=True, overlap_percent=80,
                                              plan_start_date=date(2026, 10, 6)).to_dict(), default=str))
c = TestClient(m.app); c.post("/login", data={"username": "anvitech", "password": "1930rail"})


def plan(tag):
    c.post("/run", json={})
    s = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    h = hashlib.sha256(repr(sorted((e.batch_id, e.process_seq, e.machine, e.operator, e.start.isoformat(),
                                    e.end.isoformat(), e.qty) for e in s)).encode()).hexdigest()[:12]
    mins = {mid: round(sum((e.end - e.start).total_seconds() / 60 for e in s if e.machine == mid))
            for mid in ("CNC1", "MD1")}
    ppc = new_engine._new_masters(False).machines
    print(f"{tag:42} hash {h}  minutes {mins}  ppc kind CNC1={ppc['CNC1'].kind.value} MD1={ppc['MD1'].kind.value}")


def retype(mid, typ):
    cur = next(x for x in c.get("/machines").json()["machines"] if x["id"] == mid)
    r = c.put("/machines", json={"id": mid, "type": typ, "hours": cur["hours"], "version": cur["version"]})
    return cur["type"], r.status_code


plan("as seeded")
old, st = retype("CNC1", "Manual Packing"); plan(f"CNC1 {old} -> Manual Packing ({st})")
retype("CNC1", old); plan("CNC1 restored")
old, st = retype("MD1", "CNC lathe"); plan(f"MD1 {old} -> CNC lathe ({st})")
```
