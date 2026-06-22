# Implementation Plan — Anvitech PPC Engine

Build plan for the PPC engine. Designed so you can **test and debug rule-by-rule**:
each rule is a pure function with its input/output visible in the frontend tabs.

- **Rules:** [`RULES.md`](RULES.md)
- **Design:** [`docs/superpowers/specs/2026-06-19-anvitech-ppc-engine-design.md`](docs/superpowers/specs/2026-06-19-anvitech-ppc-engine-design.md)
- **Guidance:** [`CLAUDE.md`](CLAUDE.md)

Build order is bottom-up: scaffolding → loaders → models → pipeline/trace →
rules in sequence → API → frontend → feedback loop. The pipeline/trace is built
**before** the rules so every rule is observable from day one. Tests are written
per phase, and each phase ends in a testable state.

---

## Phase 0 — Scaffolding

- [x] Create folder structure (`engine/`, `engine/rules/`, `api/`, `web/`,
      `data/`, `tests/`).
- [x] `requirements.txt`: `fastapi`, `uvicorn`, `openpyxl`, `pandas`, `pytest`.
- [x] `engine/config.py`: dataclass for tunable params + validation
      (window=10, setup=90, overlap_mode + overlap%=50, parallel_trigger=400).
- [x] Smoke test: `pytest` runs (0 tests) and `uvicorn` starts.

## Phase 1 — Data foundation

- [x] `engine/models.py`: dataclasses — `SOLine`, `Batch`, `Process`,
      `Machine`, `Operator`, `CalendarDay`, `ScheduleEntry`, `Actual`, `PlanRun`.
- [x] `engine/loaders.py`: read each `Test2.xlsx` sheet → typed objects:
  - [x] Machine master, Operator & shift master, Weekly off & holiday master
  - [x] Item's process master (routings: up to 12 processes each)
  - [x] Sales Order (SO) list
- [x] **Loader validation** (collect all, don't crash on first):
  - [x] `PENDING_MASTER_DATA` (non-blocking): machines referenced by routings but
        not yet in Machine master (e.g. CNC7/VMC3/CNC6). Master is incomplete and
        **will be updated later** — register them as **provisional machines**,
        report them, and continue. Adding them to the Excel master later must need
        **no code change**.
  - [x] `NO_ROUTING` (SO item code missing from process master): **report "no
        routing found" and move ahead** — skip only that order, keep scheduling the
        rest. Non-blocking, fail-localized; the run does not stop.
  - [x] Normalize time units; log coercions
- [x] `to_table(obj)` helper: any model/list → `{columns, rows}` JSON for the trace.
- [x] Tests: loader returns expected counts; validation flags the known quirks.

## Phase 2 — Pipeline & trace (the debug backbone)

> Build this before the rules so every rule is observable from day one.

- [x] `engine/pipeline.py`:
  - [x] `run_rule(trace, name, fn, input_data, **kw)` — snapshots input, output,
        config, notes into `trace[name]`.
  - [x] `run_forward(plan_run, config, masters)` — runs Rules 1→2→3→6, returns trace.
  - [x] Catches `RuleError`, records it in the rule's trace entry, stops the chain.
- [x] Define `RuleError(rule, record_id, message)`.
- [x] Test: a dummy failing rule produces a trace with the error and "not reached"
      downstream.

## Phase 3 — Forward planning rules (1–7) — *you will debug these*

Each: pure `run(...)`, logs decision `notes`, has `tests/test_ruleN.py`.

- [x] **Rule 1 — Consolidate** (`rule1_consolidate.py`): group same item code,
      delivery dates within window. Test: SO 121 + 121A consolidate (4 days);
      121A 10-Apr stays separate (outside 10 days).
- [x] **Rule 2 — Sort by delivery date** (`rule2_sort_by_date.py`). Test: ordering.
- [x] **Rule 3 — Tiebreak by total process time** (`rule3_tiebreak_process_time.py`):
      reads routing master. Test: `61240807-01` beats `61249291-01` (same date).
- [x] **Rule 4 — Setup time** (`rule4_setup_time.py`): occupancy = cycle×qty + 90.
      Calc helper consumed by Rule 6. Test: occupancy math.
- [x] **Rule 5 — Overlap mode** (`rule5_overlap_mode.py`): start offset for next
      process (sequential vs 50%). Calc helper. Test: both modes.
- [x] **Rule 7 — Parallel machine** (`rule7_parallel_machine.py`): batch>400 →
      separate preferred machine for next CNC setup. Calc helper. Test: trigger.
- [x] **Rule 6 — Allocate** (`rule6_allocate.py`): walk each batch's processes,
      assign earliest-available preferred machine; respect calendar (Thu off,
      holidays, leaves) + shifts; consume Rules 4/5/7. Test: small SO subset →
      expected machine + start/end.
- [x] Wire Rules 1–7 into `pipeline.run_forward`.
- [x] **Golden snapshot test** (`test_pipeline_golden.py`) on a fixed SO subset.

## Phase 4 — API

- [x] `api/main.py` (FastAPI):
  - [x] `POST /run?config=...` → run Rules 1–7, return trace.
  - [x] `GET /trace/{run_id}` → past run's trace.
  - [x] Serve `web/` static frontend at `/`.
- [x] Test: `/run` returns a trace with all rule entries.

## Phase 5 — Frontend (per-rule tabs) — *where you test & debug*

- [x] `web/`: page with a tab per rule (1–9).
- [x] Each tab renders that rule's **input table** + **output table** side by side,
      plus config used and decision notes.
- [x] Error state: failed rule's tab shows red error + offending row; downstream
      tabs show "not reached."
- [x] "Run plan" button → calls `/run`, populates tabs.
- [x] Manual check against the Excel worked examples.

> **Checkpoint:** debug Rules 1–7 here. Confirm logic is correct before relying on
> the loop. Report any rule changes; they propagate to Rules 8–9 automatically.

## Phase 6 — Feedback loop (8–9) — *attached to 1–7, built now*

- [x] **Rule 8 — Capture actuals** (`rule8_capture_actuals.py`): save daily entry
      (qty produced/rejected, setup time, downtime reasons) → `data/actuals.json`.
      Test: write/read round-trip.
- [x] **Rule 9 — Rerun MRP** (`rule9_rerun_mrp.py`): read actuals → compute balance
      per SO → **call `rule1..rule7` again** with updated qty. Must import, not copy.
- [x] `POST /actuals` and `POST /rerun` endpoints.
- [x] Tabs 8 (actuals entry form + saved actuals) and 9 (re-planned trace).
- [x] **Reuse test:** rerun with actuals = 0 reproduces the original 1–7 schedule.

## Phase 7 — Polish

- [x] README with run instructions; fill `CLAUDE.md` commands section.
- [x] Surface loader validation report in the UI.
- [ ] Optional: save/diff traces across runs for regression debugging.
- [ ] Optional: `git init` if version control is wanted.

---

## Definition of done (per rule)

A rule is done when: (1) it's a pure `run(...)`; (2) it logs decision notes;
(3) `test_ruleN.py` passes using an Excel-derived example; (4) its input/output
shows correctly in its frontend tab; (5) for Rule 9, the reuse test passes.
