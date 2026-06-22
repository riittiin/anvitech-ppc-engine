# CLAUDE.md — Anvitech PPC Engine

Guidance for any Claude session working in this repository. Read this first.

## What this project is

A **Production Planning & Control (PPC) engine** for Anvitech, a precision
machining job shop. It takes customer sales orders and schedules them onto
machines following 9 business rules, then re-plans as actual production comes in.

- **Rules (source of truth):** [`RULES.md`](RULES.md) — the 9 rules in execution
  order, with input/output for each.
- **Design spec:** [`docs/superpowers/specs/2026-06-19-anvitech-ppc-engine-design.md`](docs/superpowers/specs/2026-06-19-anvitech-ppc-engine-design.md)
- **Build plan:** [`implementation.md`](implementation.md)
- **Original data + requirements:** `Test2.xlsx` (12 sheets).

## Stack

- **Backend:** Python + FastAPI. The engine is plain Python; FastAPI is a thin layer.
- **Frontend:** lightweight HTML/JS with **per-rule tabs**.
- **Data source:** `Test2.xlsx`, read **read-only** via openpyxl. **Test-only:** in
  production the user **uploads** the masters/SO Excel through the website; the
  loader runs against the uploaded workbook, with `Test2.xlsx` as the test/demo
  default. Implemented via `POST /upload` (parses to an in-memory `dataset_id`;
  `load_all` accepts a BytesIO); the frontend sends `dataset_id` on every call,
  falling back to `Test2.xlsx` when none. Uploaded datasets are in-memory only
  (durable storage is a deferred task).
- **Writable data:** `data/actuals.json` only.

## Non-negotiable design principles

These exist to make the engine **easy to test and debug rule-by-rule**. Do not
violate them without the user's explicit say-so.

1. **Every rule is a pure function.** `def run(input_data, config, masters) -> output`.
   No global state, no UI calls, no rule calling another rule. Only `pipeline.py`
   knows the order.
2. **Rule 9 reuses Rules 1–7 — never duplicates them.** Rule 9 imports and calls
   `rule1..rule7` with updated quantities. A fix to Rules 1–7 must automatically
   flow to the loop. If you find yourself copying rule logic into Rule 9, stop.
3. **The pipeline snapshots every rule's input and output into a trace.** This is
   what powers the per-rule tabs. Don't add per-rule UI code — visibility comes
   from the trace. See `pipeline.py` `run_rule()`.
4. **`Test2.xlsx` is read-only.** The only thing the app writes is
   `data/actuals.json`. Keep source data clean and runs reproducible.
5. **Fail loud, fail localized — two distinct layers:**
   - **(a) Loader-level data gaps** (`PENDING_MASTER_DATA`, `NO_ROUTING`) are
     **non-blocking**: report them and continue, skipping only the affected
     resource/order (see Known data quirks). The run does **not** stop.
   - **(b) Rule-level contract violations** raise typed `RuleError(rule,
     record_id, message)`; the pipeline records it in the trace and **stops the
     chain** so the frontend shows exactly where it broke.

## Data flow (memorize this)

```
so_lines → R1 consolidate → R2 sort by date → R3 tiebreak (reads routing)
        → R6 allocate (uses R4 setup, R5 overlap, R7 parallel + masters)
        → R8 capture actuals → R9 rerun MRP (calls R1..R7 again)
```

- Forward chain: `1 → 2 → 3 → 6 → 8 → 9 (loop)`
- Consumed inside Rule 6: Rules 4, 5, 7
- Rule 3 also reads the routing master.

## Known data quirks in Test2.xlsx (handle in the loader)

- **Exact sheet names (loader gotcha):** several sheet names have **trailing spaces**
  (`'PPC logics '`, `'Planning status monitoring '`, `'Machinewise '`, `'Weekly
  Production plan '`) and one is **misspelled** (`'Production anayasis Report'`);
  `'Item's process Master'` has an apostrophe. Match exactly or normalize, or the
  loader will silently miss sheets.
- **Counts:** ~18 machines in `Machine master` (sheet has ~24 rows incl. blanks);
  ~85 distinct item codes in `Item's process Master` (across ~500 rows).
- **Machines not yet in the master (expected, not errors):** routings reference
  resources like `CNC7`, `VMC3`, `CNC6` that are not yet in `Machine master` (which
  today lists only CNC 1–5, VMC 1–2). **The master data is incomplete and will be
  completed in the future** — treat any such reference as a *pending placeholder*,
  not a failure. The loader must:
  - register it as a **provisional machine** so allocation can still proceed,
  - record it in a non-blocking `PENDING_MASTER_DATA` report (informational), and
  - **never drop the row or stop the pipeline.**
  Design so that when the user later adds the machine to `Machine master`, it
  "just works" with **no code change** — only the Excel master is updated. Apply
  this same forgiving approach to any other master reference that may be filled in
  later (e.g. operators, routings), not just machines.
- **Sales order with no routing (`NO_ROUTING`):** if an SO item code has no recipe
  in `Item's process Master`, there's nothing to schedule (no processes, times, or
  machines). **Report "no routing found" for that order and move ahead** — skip only
  that one order, record it in the report, and keep scheduling every other order.
  Non-blocking and fail-localized; the run does not stop. (Unlike a missing machine,
  a missing routing can't be made provisional — you can't invent a recipe.) In the
  current `Test2.xlsx` there are 0 such cases; this is a future safety net.
- **Time-unit inconsistency:** cycle/total times are in minutes in the Process
  Master but appear as tiny decimals in `Planning status monitoring`. Normalize
  to one unit in the loader and log coercions.
- **Suggested vs Allotted machine:** "Suggested" = engine recommendation;
  "Allotted" = final locked choice. Engine fills Suggested; planner may override.

## Conventions

- One rule per file under `engine/rules/`, named `ruleN_<purpose>.py`, each
  exposing `run(...)`.
- One test file per rule under `tests/`, named `test_ruleN.py`. Seed tests with
  the worked examples annotated in `Test2.xlsx` (see spec §9).
- Configurable params live in `engine/config.py` with validation: consolidation
  window (10d), setup time (90min), overlap mode (50%), parallel trigger (400).
- Keep files focused; if a rule file grows large it's probably doing too much.

## Commands

- Install deps: `pip install -r requirements.txt`
- Run API: `uvicorn api.main:app --reload` (frontend served at `/`)
- Run tests: `pytest`
- Regenerate golden trace after an intentional logic change:
  `REGEN_GOLDEN=1 pytest -k golden`
- **Login:** whole app is behind HTTP Basic Auth — `APP_USERNAME`/`APP_PASSWORD`
  env vars (defaults `anvitech`/`ppc2025`). Implemented as a middleware in
  `api/main.py`.
- **Vercel deploy:** `vercel.json` + `api/index.py` (entrypoint re-exporting the
  app). Actuals write to `/tmp` on Vercel (ephemeral) via `ACTUALS_PATH`/`VERCEL`
  detection in `rule8`. See README "Deploying to Vercel".

## Map of the code

- `engine/config.py` — tunable params + validation.
- `engine/models.py` — dataclasses; each exposes `as_row()` for the trace tables.
- `engine/loaders.py` — read `Test2.xlsx` → typed objects + non-blocking report.
  Resource-name normalization (`CNC 4` ≡ `CNC4`) and provisional-machine handling
  live here.
- `engine/worktime.py` — `WorkClock`: shifts + Thursday/holiday skip for Rule 6.
- `engine/pipeline.py` — `run_rule` (snapshots in/out/config/notes), `run_forward`
  (1→2→3→6), `RuleError`, `to_table`.
- `engine/gantt.py` — `build_gantt`: turns the Rule 6 schedule into the worker-facing
  Gantt view-model (per-order rows, day axis, time-positioned bars coloured by
  machine). Served at `GET /gantt` and bundled in `/run`/`/rerun`; rendered in the
  web `📊 Gantt` tab.
- `engine/rules/ruleN_*.py` — one pure `run(...)` per rule; 4/5/7 also expose the
  calc helpers Rule 6 imports.
- `api/main.py` — FastAPI endpoints + helper-tab augmentation.
- `web/` — per-rule tabs (`app.js` renders the trace; no per-rule UI code).

## Resolved design decision (data-confirmed)

**Rule 3 "total process time" = sum of per-process _cycle_ times**, not the
sparse "Total time" column. Only the cycle-time sum reproduces the SO-Remarks
oracle (`61240807-01` highest, `61247047-01` lowest, `61241949-01` > `61247047-01`).

## Workflow notes

- This project was scoped via the brainstorming → spec → plan flow. When making
  substantive changes, update `RULES.md` / the spec first, then the code.
- Not currently a git repo. Initialize before committing if the user wants version
  control.
