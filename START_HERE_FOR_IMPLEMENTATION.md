# START HERE — Implementation Handoff (for a fresh Claude session)

You are a new Claude session with **no prior context**. Your job is to **implement
the Anvitech Production Planning & Control (PPC) engine** described in this repo.
This document is your single entry point. Read it fully before doing anything.

The design is **already done and approved by the user**. You are NOT redesigning —
you are building what the documents specify. If something is genuinely ambiguous or
contradictory, ask the user before improvising.

---

## 0. First actions (do these in order)

1. Read these four documents, in this order. They are the authority — this handoff
   only orients you:
   1. **`CLAUDE.md`** — project guidance, principles, data quirks (repo root).
   2. **`RULES.md`** — the 9 business rules in execution order, with each rule's
      input/output (repo root).
   3. **`docs/superpowers/specs/2026-06-19-anvitech-ppc-engine-design.md`** — the
      full approved design spec.
   4. **`implementation.md`** — the phase-by-phase build plan with checkboxes.
2. Inspect the source data file `Test2.xlsx` so you understand the real columns
   before coding the loader (see §4 for how).
3. Confirm your understanding with the user, then **follow `implementation.md`
   phase by phase**, top to bottom. Check off items as you complete them.

> Do not skip the reading. The whole project is built around a specific structure
> that makes it testable rule-by-rule; deviating quietly will break that goal.

---

## 1. What you are building (30-second version)

Anvitech is a precision-machining job shop. They need software that:
- reads customer **sales orders** and the shop's **masters** (machines, operators,
  shifts, working calendar, item process routings) from `Test2.xlsx`,
- runs **9 sequenced rules** to produce a **machine-by-machine production schedule**
  (which order runs on which machine, when), optimizing for delivery dates and
  capacity,
- then **closes the loop**: daily actual production is entered, and the plan is
  re-generated (MRP re-run) from actual-completed + balance-remaining.

The user's overriding requirement: **it must be easy to test and debug rule by
rule.** Each rule's exact input and output must be visible in the frontend.

## 2. Locked decisions (do not relitigate)

| Decision | Choice |
|---|---|
| Runtime | Python engine + browser frontend (web app) |
| Web framework | FastAPI (thin layer) |
| Frontend | Lightweight HTML/JS, **one tab per rule** |
| Data source | Read `Test2.xlsx` directly, **read-only** |
| Writable data | `data/actuals.json` only (daily actuals) |
| Scope | Build all 9 rules now; user will debug Rules 1–7 first |

## 3. The five principles you must not violate

(Full version in `CLAUDE.md` — repeated here because they are load-bearing.)

1. **Every rule is a pure function:** `run(input_data, config, masters) -> output`.
   No global state, no UI calls, no rule calling another rule. Only `pipeline.py`
   knows the order.
2. **Rule 9 reuses Rules 1–7 — never duplicates them.** Rule 9 imports and calls
   `rule1..rule7` with updated quantities. If you copy rule logic into Rule 9,
   you've done it wrong. This is so the user's debugging of Rules 1–7 flows into
   the loop automatically.
3. **The pipeline snapshots every rule's input and output into a trace object.**
   That trace is what the per-rule tabs render. Visibility is a pipeline feature,
   not per-rule UI code.
4. **`Test2.xlsx` is read-only.** The only thing the app ever writes is
   `data/actuals.json`.
5. **Fail loud, fail localized — two distinct layers.** (a) *Loader-level* data
   gaps (`PENDING_MASTER_DATA`, `NO_ROUTING`) are **non-blocking**: report and
   continue, skipping only the affected resource/order — the run does not stop
   (see §4). (b) *Rule-level* contract violations raise `RuleError(rule, record_id,
   message)`; the pipeline records it in the trace and **stops the chain** so the UI
   shows exactly where it broke.

## 4. The data: Test2.xlsx (what's in it)

12 sheets. Roles:

**Masters / inputs**
- `Weekly off & holiday master` — weekly off = **every Thursday**, plus fixed
  holidays and operator leaves. Scheduler must skip these.
- `Machine master` — ~18 resources (CNC 1–5, VMC 1–2, 2 bandsaws, lathe, milling,
  drilling, and manual stations) with hourly rates. (The sheet has ~24 rows; the
  extras are blank separator rows — count actual resources, not rows.)
- `Operator & shift Master` — operators + machines they can run; **2 shifts**
  (1st 8am–7pm, 2nd 7pm–5am); operators swap shifts every Friday.
- `Item's process Master` — routings for **~85 distinct item codes** (across ~500
  rows; many rows are blank/section separators): RM info + up to **12 sequential
  processes**, each with process name, cycle time, total time, **suggested** machine,
  **allotted** machine.
- `Sales Order (SO) list` — the demand: SO lines with item code, qty, delivery date,
  pending qty. The Remarks column contains worked examples to use as tests.
- `PPC logics` — the original (unordered) rules. Already cleaned up into `RULES.md`.

**Outputs the engine should be able to produce (reference these for shape)**
- `Sample entry window format` — desired UI: a calendar/Gantt of processes per
  machine + a daily production entry form.
- `Planning status monitoring` — plan per SO (total process days, planning delivery
  date, RM stock Y/N, each process time + allotted machine).
- `Machinewise` — the plan exploded per machine (the queue each machine works).
- `Weekly Production plan` — human-readable weekly schedule per day/shift/machine.
- `Production analysis Report` — KPI/feedback loop (actual vs planned, efficiency,
  utilization, rejection %, downtime breakdown).

**How to inspect the sheets** (Python; data is binary xlsx):
```python
import openpyxl
wb = openpyxl.load_workbook("Test2.xlsx", data_only=True, read_only=True)
for ws in wb.worksheets:
    print(ws.title, ws.max_row, ws.max_column)
# then iterate ws.iter_rows(values_only=True) on a sheet to see its columns
```

### Known data quirks — handle in the loader, don't paper over
- **Exact sheet names (loader gotcha):** several sheet names have **trailing spaces**
  — `'PPC logics '`, `'Planning status monitoring '`, `'Machinewise '`, `'Weekly
  Production plan '` — and one is **misspelled**: `'Production anayasis Report'`.
  `'Item's process Master'` contains an apostrophe. Match names exactly (or strip and
  normalize), or the loader will silently miss sheets.
- **Machines not yet in the master (expected, not errors):** routings reference
  resources like `CNC7`, `VMC3`, `CNC6` that are not yet in `Machine master` (today
  only CNC 1–5, VMC 1–2). **This master data is incomplete and will be filled in
  later** — do not treat it as a failure. The loader must register such a reference
  as a **provisional machine**, record it in a non-blocking `PENDING_MASTER_DATA`
  report, and keep going. Build it so that when the user later adds the machine to
  `Machine master`, no code change is needed — only the Excel master changes. Apply
  the same forgiving handling to other master references that may be completed later.
- **Sales order with no routing (`NO_ROUTING`):** if an SO item code has no recipe in
  `Item's process Master`, there is nothing to schedule (no processes/times/machines).
  **Report "no routing found" for that order and move ahead** — skip only that one
  order, record it in the report, and keep scheduling all the others. Non-blocking and
  fail-localized; the run does not stop. (Unlike a missing machine, this can't be made
  provisional — you can't invent a recipe.) In the current `Test2.xlsx` there are 0
  such cases; it's a future safety net.
- **Time-unit inconsistency:** cycle/total times are in minutes in the Process
  Master but appear as tiny decimals in `Planning status monitoring`. Normalize to
  one unit in the loader and log every coercion.
- **Suggested vs Allotted machine:** Suggested = engine recommendation; Allotted =
  final locked choice. Engine fills Suggested; a planner may override into Allotted.

## 5. The 9 rules (summary — full text in RULES.md)

Forward chain (output → input): `1 → 2 → 3 → 6 → 8 → 9 (loops back to 1)`.
Rules 4, 5, 7 are calculation helpers consumed *inside* Rule 6.

1. **Consolidate** same item-code SOs whose delivery dates fall within a 10-day
   window (configurable) into one batch.
2. **Sort** batches by earliest delivery date (primary priority).
3. **Tiebreak** equal dates by higher total process time (reads routing master).
4. **Setup time:** add 90 min per process to machine occupancy (helper).
5. **Overlap mode:** next process starts after previous is fully done, OR after 50%
   (configurable) of it — global toggle per run (helper).
6. **Allocate** each process to the earliest-available preferred machine, respecting
   calendar (Thu off, holidays, leaves) and shifts. Consumes Rules 4/5/7 + masters.
7. **Parallel machine:** if batch > 400, allot a separate preferred machine for the
   next CNC setup (helper).
8. **Capture actuals:** after each shift, enter actual production → `data/actuals.json`.
9. **Re-run MRP:** read actuals → balance = SO qty − completed → call Rules 1–7 again.

Configurable params (defaults): consolidation window 10 days · setup 90 min ·
overlap 50% · parallel trigger 400.

## 6. How to proceed

Follow `implementation.md` exactly — it is the ordered, checkbox build plan:
- Phase 0 Scaffolding → Phase 1 Loaders/models → Phase 2 Pipeline & trace (build
  before the rules so they're observable) → Phase 3 Rules 1–7 → Phase 4 API →
  Phase 5 Per-rule tabs frontend → Phase 6 Rules 8–9 → Phase 7 Polish.
- Each rule is done only when: it's a pure `run(...)`, logs decision notes, has a
  passing `tests/test_ruleN.py` seeded with an Excel-derived example, and shows
  correctly in its frontend tab (Rule 9 also needs the reuse test).

**Recommended working method (if these skills are available in your session):**
- Use `superpowers:writing-plans` to expand `implementation.md` into a detailed,
  step-by-step plan before coding.
- Use `superpowers:test-driven-development` per rule — the Excel worked examples are
  ready-made test cases.
- Use `superpowers:executing-plans` to work through the plan with review checkpoints.
If those skills aren't present, just follow `implementation.md` phase by phase.

## 7. Checkpoint the user expects

After Phases 0–5, the user will **debug Rules 1–7 through the frontend tabs** and may
request logic changes. Build Rules 8–9 (Phase 6) so they sit on top of the 1–7
functions — when the user fixes 1–7, the loop must inherit the fix with no extra
work. Prove this with the reuse test (rerun with actuals = 0 reproduces the original
schedule).

## 8. Things to confirm with the user before/while building

- Machines referenced but not yet in the master (CNC6/CNC7/VMC3) are expected to be
  added later — confirm the engine should keep treating them as provisional
  placeholders until the user updates `Machine master` (no remapping needed).
- The canonical time unit (minutes assumed) and how to normalize the decimal values.
- Whether the frontend tabs should also render the legacy output sheet shapes
  (`Machinewise`, `Weekly Production plan`) or just raw rule input/output tables for
  now (raw tables are sufficient for the first build).

## 9. Do NOT

- Do not redesign the architecture or change the locked decisions without asking.
- Do not duplicate rule logic into Rule 9.
- Do not write to `Test2.xlsx`.
- Do not add a database, auth, or extra frameworks (out of scope for now).
- Do not skip the per-rule tests or the trace mechanism — they are the whole point.

---

Good luck. Start at §0.
