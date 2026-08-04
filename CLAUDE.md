# CLAUDE.md — Anvitech PPC Engine

> ## ⚠️ CURRENT STATE — READ THIS FIRST (updated 2026-07-29)
>
> This app now runs a **NEW operator-stable scheduling engine**, swapped in behind the old
> scheduler seam. **Everything below this banner describes the PRE-SWAP classic/flow engine and
> is historical** — when it conflicts with the code, trust the code + this banner.
>
> - **New engine = `ppc_engine/`** (a vendored package; its imports were rewritten `engine.` →
>   `ppc_engine.`). Adapter = **`engine/new_engine.py`**: it runs ppc_engine behind
>   `engine/pipeline.py:scheduler_for` and maps its output back to the old `ScheduleEntry` list, so
>   the entire UI is unchanged. It exists to enforce **RULES.md Rule 1 — one operator per machine
>   per shift, NO hour-by-hour hopping** (the old classic/flow engines broke this). `ppc_engine/`,
>   `new_engine.py`, and the `"new"` branches are **intentional, not errors or stray copies.**
> - **Production runs `scheduler="new"`** via the env var **`DEFAULT_SCHEDULER=new`** (see
>   `api/main.py:_load_plan_config`). The code default in `engine/config.py` is `"classic"` **on
>   purpose**: the ~500 existing tests validate the KEPT classic engine; the new engine has its own
>   tests in **`tests/test_new_engine.py`** (+ `tests/new_sample_workbook.py`). **Classic/flow are
>   retired but kept so those tests stay green — do not delete them.**
> - **"Start deep search"** auto-tunes **overlap % + job sequence** for the new engine: a PARALLEL
>   fine-grid overlap contest on GitHub Actions (`optimize_service.run_contest` +
>   `CLOUD_NEW_OVERLAP_CANDIDATES`), with a local golden-section fallback (`new_engine.tune` /
>   `sweep_optimize`). `optimizer.optimize` / `sweep_optimize` **delegate to `new_engine`** for
>   `scheduler=="new"`. Progress is per-plan (`ppc_engine` `on_eval`).
> - **Recent audit fixes (keep — regression tests exist):** unrouted orders are skipped not crashed;
>   operator absences are honoured (`new_engine._with_absences`); the optimizer's before/after is
>   reported at the applied overlap.
> - **Piece-flow guard (2026-07-25, `docs/superpowers/specs/2026-07-25-piece-flow-no-premature-work-design.md`):**
>   `ppc_engine/scheduler/flow_scheduler.py::decode` RE-LAYS a starved fast op later (batch-at-end)
>   so its WORK never finishes before its predecessor delivered the last piece — the machine-wise
>   schedule no longer processes pieces before they exist ("deburring skipped for the last jobs").
>   Block model + speed kept (owner chose it over 5×-slower per-piece flow). It makes the schedule
>   physically honest: on Test8 optimized makespan ~52.5→55.56 d, late-days ~1214→1323 (the old
>   numbers were infeasible, not better). `new_engine._entries_from_schedule` still span-paces the
>   entry `end` as belt-and-suspenders. Regression: `tests/test_new_engine.py::
>   test_op_work_never_finishes_before_its_predecessor`.
> - **Freeze in-progress work — daily restricted optimize (2026-07-29,
>   `docs/superpowers/specs/2026-07-29-freeze-in-progress-restricted-optimize-design.md`) —
>   "Done entering — update plan" now re-optimizes EVERY DAY, not just Thursday.** The
>   Thursday-only gate (`_is_optimize_day`/`_OPTIMIZE_WEEKDAY`, including the temp-Sunday
>   testing override) is **removed** from `POST /optimize/done` — it always calls
>   `_try_start_auto()` (still a no-op when a contest is already running or nothing material
>   changed). What makes daily re-sequencing safe: any operation the punches show
>   **partially done** (`0 < good qty < required`) is **frozen** — pinned to its **last-applied
>   plan's** machine + operator, remaining qty from the punches, no setup on resume, multiple
>   frozen ops on one machine resume in previous-plan order before any new work, shift
>   handoff/absent-operator substitution unchanged, OS/DISPATCH never frozen — so a
>   physically-running part is never yanked onto a different machine/person. New pure module
>   **`engine/freeze.py`** (`schedule_projection`, `compute_frozen_set`) + two store keys
>   (`anvitech:last_applied_schedule`, `anvitech:frozen_ops`) + `ppc_engine.scheduler.decode(...,
>   frozen=None)` / `FrozenOp` (pre-places frozen ops before the Giffler-Thompson loop;
>   `frozen` empty/None is byte-identical to before). Threaded through
>   `new_engine.run`/`optimize`/`tune`/`sweep_optimize`, `engine.optimizer`, `run_forward`, and
>   the contest + cloud payload (`optimize_service`) — every candidate plan pins the same
>   frozen set. The admin's manual **"Start deep search"** (`POST /optimize`) now **also
>   respects the freeze** (owner decision, 2026-07-29 — deep-search is pressed in week 2
>   when week-1 work is already running): `_start_optimize` recomputes the frozen set
>   itself, inside the lock, right before every run — manual and auto alike. It is
>   naturally unrestricted only when nothing is in progress yet (a fresh first import).
> - **Committed-promise cap — soft +3-day ceiling (2026-07-29,
>   `docs/superpowers/specs/2026-07-29-committed-date-stability-design.md`) — a
>   committed order's completion may pull EARLIER by any amount on re-optimize but
>   must not slip LATER than `promised_date + committed_promise_slack_days`
>   (Config knob, default 3 days).** Reuses the proven 2026-07-24 worst-order-ceiling
>   pattern, not the 2026-07-13/14 hard two-pass/veto that collapsed the feasible
>   region (see the Phase-2R note below). **(1) Soft, in-search:** a convex penalty
>   `COMMITTED_PROMISE_WEIGHT (= 5000.0, Test8-measured) × committed_promise_breach`
>   is added to the score in **both** `engine/optimizer.score` and the
>   `ppc_engine/objective/objective.py` mirror (`_committed_promise_breach`,
>   weighted by `PlanConfig.committed_promise_weight`) — the search keeps committed
>   orders inside the cap and delays **Open** orders instead, no reservation, no
>   separate pass. **(2) Hard, at apply:** `api/main._auto_apply_result` gates
>   auto-apply on `promise_ok = best.max_committed_slip <= inc.max_committed_slip`
>   (no-regression), alongside the existing `worst_ok`. **Not wired to the manual
>   `POST /optimize/apply` path** — the design intended the same gate there, but the
>   admin's Apply button today applies unconditionally (verified in code; flag if
>   this matters). New fields: `engine/optimizer.plan_metrics`'s
>   `committed_promise_breach`/`max_committed_slip`; ppc `Order.promise_date`,
>   `PlanMetrics.promise_slip_by_order`, `PlanConfig.committed_promise_slack_days`/
>   `committed_promise_weight`; `rule1_consolidate` populates each consolidated
>   batch's tightest committed promise. **This is a deliberate PARTIAL reversal of
>   the 2026-07-16 "lanes are status labels, no scheduling effect" pivot below:
>   committed now has a real (soft) scheduling effect; open remains a pure label.**
>   **Urgent lane removed entirely** (owner decision, same day) — `/orders/urgent`
>   deleted, Orders tab shows Committed + Open only; a stored
>   `commitment == "urgent"` row migrates to `"committed"` on load
>   (`Order.from_json`). Measured on Test8 (~half the book committed): weight 5000
>   roughly halved committed-past-+3 orders (8→4) and cut the worst slip 16d→9d, at
>   ~2% more total late-days.
> - **Deploy:** repo `riittiin/anvitech-ppc-engine` (branch `main`) → Render service `anvitech-ppc`
>   (https://anvitech-ppc.onrender.com). Env: `DEFAULT_SCHEDULER=new`, `GITHUB_DISPATCH_TOKEN`,
>   `OPTIMIZE_WORKER_SECRET`, `MONGODB_URI`, `APP_USERNAME`/`APP_PASSWORD`. **Render auto-deploy is
>   OFF** — deploy via the dashboard: **Manual Deploy → Deploy latest commit**. Tests: `pytest`
>   (508 passing).

Guidance for any Claude session working in this repository. Read this first.
**Taking over a fresh?** Start with [`HANDOFF.md`](HANDOFF.md) — current deployed
state, live URL, what's done vs deferred, and operational gotchas.

## What this project is

A **Production Planning & Control (PPC) engine** for Anvitech, a precision
machining job shop. It takes customer sales orders and schedules them onto
machines following 9 business rules, then re-plans as actual production comes in.

- **Rules (source of truth):** [`RULES.md`](RULES.md) — the 9 rules in execution
  order, with input/output for each.
- **Design spec (original 9 rules):** [`docs/superpowers/specs/2026-06-19-anvitech-ppc-engine-design.md`](docs/superpowers/specs/2026-06-19-anvitech-ppc-engine-design.md)
- **Order-book design (current architecture):** [`docs/superpowers/specs/2026-06-22-order-book-design.md`](docs/superpowers/specs/2026-06-22-order-book-design.md)
- **Data format:** the user's `Test5.xlsx` (gitignored real data — the **current
  file**; supersedes `Test4.xlsx`, which superseded `Test3.xlsx`) — the 3 master
  sheets use a clean header-driven layout the loader reads dynamically. Extra/reordered
  columns are fine: the loader finds columns by name, so each new file (more columns,
  same header names) loads unchanged. `Test5` adds parallel manual stations
  (`MW1/MW2/MW3`, `MD1/MD2`, `MPK1/MPK2/MPK3`) and carries a **+30% cycle/total time
  on every CNC/VMC step** (owner request, 2026-07-11).

## Stack

- **Backend:** Python + FastAPI. The engine is plain Python; FastAPI is a thin layer.
- **Frontend:** lightweight HTML/JS with **per-rule tabs**.
- **Data source:** the user **uploads** their masters/SO Excel (the **Test4
  format**) via `POST /upload`, read **read-only** via openpyxl. Upload **merges the
  orders into a persistent order book** (keyed by the unique **(SO number, item
  code)** pair — an SO number alone is NOT unique; one SO# can carry several item
  lines) and stores the workbook's masters. `load_all(source)` requires a path or
  BytesIO — there is **no bundled default** (pre-upload the app shows empty masters).
  Tests + the golden trace use a **code-generated sample** in the same format
  (`tests/sample_workbook.py`); the real-data file `Test5.xlsx` is gitignored and
  used only by uploading it.
- **Persistent state (the order book):** orders, their actuals, and the latest
  masters live in a durable key/value store. `engine/storage.py` selects the backend:
  **MongoDB Atlas (`MONGODB_URI`) > Upstash Redis > local file (`data/store/`)**.
  This store is the only thing the app writes; uploaded workbooks are read-only.
- **Operators are app-owned, not Excel** (`anvitech:operators`, 2026-07-18). The
  workbook's "Operator & shift Master" sheet **seeds the table exactly once** — only
  the first time the store table is empty and a workbook is on file — then is
  ignored forever: a later re-upload can never touch operators (the week-2
  stale-sheet-overwrite problem is impossible by construction). Every Friday,
  unpinned two-shift operators swap shift automatically (effective from that
  Friday's first shift); a per-operator pin exempts individuals; admins
  add/edit/remove/pin operators directly in Settings. See
  `engine/operator_master.py` and the standalone feature bullet below.

## Non-negotiable design principles

These exist to make the engine **easy to test and debug rule-by-rule**. Do not
violate them without the user's explicit say-so.

1. **Every rule is a pure function.** `def run(input_data, config, masters) -> output`.
   No global state, no UI calls, no rule calling another rule. Only `pipeline.py`
   knows the order.
2. **Planning reuses Rules 1–6 — never duplicates them.** The order book emits the
   active SO-lines (each at its *remaining* qty = ordered − good produced) and feeds
   them straight into the unchanged Rules 1–6 (`api._plan` → `pipeline.run_forward`).
   "Plan" and the old "Rerun MRP" are now one action. Never copy rule logic into the
   order-book layer (`engine/orderbook.py`).
3. **The pipeline snapshots every rule's input and output into a trace.** This is
   what powers the per-rule tabs. Don't add per-rule UI code — visibility comes
   from the trace. See `pipeline.py` `run_rule()`.
4. **Uploaded workbooks are read-only.** The only thing the app writes is the durable
   store (order book + actuals, via `engine/storage.py`). Keep source data clean.
5. **Fail loud, fail localized — two distinct layers:**
   - **(a) Loader-level data gaps** (`PENDING_MASTER_DATA`, `NO_ROUTING`) are
     **non-blocking**: report them and continue, skipping only the affected
     resource/order (see Known data quirks). The run does **not** stop.
   - **(b) Rule-level contract violations** raise typed `RuleError(rule,
     record_id, message)`; the pipeline records it in the trace and **stops the
     chain** so the frontend shows exactly where it broke.

## Data flow (memorize this)

```
Upload Excel ─▶ MERGE into the Order Book (by (SO#, item code))   ┐
Rule 7 actual ─▶ recorded vs (SO#, item code) (+ optional complete)┘
                              │
   Order Book ──▶ active SO-lines (remaining qty) ──▶ R1 consolidate ─▶ R2 sort
   (orders · actuals · masters)                       ─▶ R3 smart priority (slack)
                                                       ─▶ R6 allocate (R4 setup,
                                                          R5 overlap)
                                                       ─▶ schedule + Gantt
```

- Forward chain (the pure rules): `1 → 2 → 3 → 6`. Rules **4, 5** are consumed
  inside Rule 6; Rule 3 also reads the routing master.
- **"Plan"** = take every active (non-completed) order at its remaining qty and run
  the forward chain. It **unifies the old "Run" and "Rerun MRP"**. The trace's
  `rule8` tab is a *view* of the planned book, not a separate module.
- Order lifecycle (status is **derived**): **Pending** → *(first actual)* →
  **Running** → *(user ticks "mark complete" on a Rule 7 entry)* → **Complete**
  (archived, excluded from planning).

## Known data quirks in the uploaded workbook (handle in the loader)

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
  the current Test5/sample data there are 0 such cases; this is a future safety net.
  **The banner is book-scoped** (`api.main._report_for_book`): NO_ROUTING rows are
  derived from the *current order book* vs the current masters — never from the stored
  workbook's own SO sheet, which can list orders that were never merged or were later
  deleted (live 2026-07-15 bug: 5 ghost "orders without routing" while all real orders
  planned fine).
- **Time-unit inconsistency:** cycle/total times are in minutes in the Process
  Master but appear as tiny decimals in `Planning status monitoring`. Normalize
  to one unit in the loader and log coercions.
- **Suggested vs Allotted machine:** "Suggested" = engine recommendation;
  "Allotted" = final locked choice. Engine fills Suggested; planner may override.

## Conventions

- One rule per file under `engine/rules/`, named `ruleN_<purpose>.py`, each
  exposing `run(...)`.
- One test file per rule under `tests/`, named `test_ruleN.py`. Seed tests with
  the generated sample workbook (`tests/sample_workbook.py`) or self-contained data.
- Configurable params live in `engine/config.py` with validation: consolidation
  window (10d), setup time (90min, **CNC/VMC steps only** — manual/finishing steps get
  no setup; see `rule6_allocate._is_setup_machine`), overlap mode (50%).
- Keep files focused; if a rule file grows large it's probably doing too much.

## Commands

- Install deps: `pip install -r requirements.txt`
- Run API: `uvicorn api.main:app --reload` (frontend served at `/`)
- Run tests: `pytest`
- Regenerate golden trace after an intentional logic change:
  `REGEN_GOLDEN=1 pytest -k golden`
- **Login:** whole app is behind an app-owned **session login** with **two roles**
  (`api/auth.py` + the `gatekeeper` middleware in `api/main.py`). A login page
  (`web/login.html`) posts to `/login`, which sets a signed HMAC-SHA256 session
  cookie; `/logout` clears it; `/me` reports the role. **Admin** = full control;
  **User** = read-only view of every tab + download the Rule 6 allocation CSV +
  submit Capture Actuals (incl. mark-complete). Admin-only endpoints (`/upload`,
  `/orders/delete`, `/orders/clear`) enforce the role **server-side** (403), not
  just in the UI. Credentials are **baked into `api/auth.py`** (admin `anvitech` /
  `1930rail`, user `anvitech_user` / `anvitech12345678`), each overridable by env
  vars (`ADMIN_USERNAME`/`ADMIN_PASSWORD`, `USER_USERNAME`/`USER_PASSWORD`; legacy
  `APP_USERNAME`/`APP_PASSWORD` still override the admin). Hardening: username-keyed
  login rate limit, CSRF Origin check on unsafe methods, CSP + security headers,
  interactive docs disabled, upload size cap. The plan config the admin last saved
  is persisted (`anvitech:plan_config`) so users see the planner's schedule.
- **Deploy (Render + MongoDB Atlas):** `render.yaml` runs `uvicorn api.main:app`.
  On Render set env vars: `APP_USERNAME`, `APP_PASSWORD`, and the store
  (`MONGODB_URI`, or the Upstash pair). Persistence is **opt-in** via those vars;
  with none set the app uses a local file store (`data/store/`). Pushing to `main`
  auto-redeploys. See README "Free public deployment". **Cloud Optimize** needs two
  more Render env vars — `GITHUB_DISPATCH_TOKEN` (fine-grained PAT, Actions
  read+write on the repo) and `OPTIMIZE_WORKER_SECRET` (must equal the GitHub repo
  secret of the same name; repo secrets `APP_URL` + `OPTIMIZE_WORKER_SECRET` are
  already set) — without them Optimize computes locally. **Oracle always-on
  worker (2026-08-01, optional):** an owner-run free-tier VM polls
  `GET /optimize/pending` and, if it claims a job within the `ORACLE_CLAIM_TIMEOUT_MIN`
  window (default 3 min), computes it locally instead of GitHub — a third,
  faster tier ahead of the existing GitHub→local ladder (Oracle → GitHub →
  local; unclaimed after the window still dispatches to GitHub as before).
  Deep-search knob when the box is up: `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE=300`
  on Render (unset/invalid falls back to the mode default). Setup + owner
  runbook: `docs/ORACLE_WORKER.md`.

## Map of the code

- `engine/config.py` — tunable params + validation. **`plan_start_date` is nullable —
  `None` = "auto: start from today (IST)", and `None` is now the LIVE DEFAULT** (live-mode
  switch for the 2026-07-19 go-live). The plan clock follows the real current date without
  anyone editing a setting; a fixed `date(...)` is a testing override (the golden test pins
  `date(2025, 3, 1)` test-side). `to_dict` keeps `None` as JSON null; `from_dict` maps
  `None`/`""`/missing → `None`. **The pure engine must NEVER see `None`** — the API boundary
  resolves it to today via `api.main._resolve_config` / `_ist_today()` at every planning
  entry (`_plan`, `_start_optimize`, `_incumbent_metrics`), and the SAVED config keeps
  `None` so a moving "today" isn't mistaken for a settings change (`_inputs_signature`
  hashes the unresolved config). The full `date.today()`/`datetime.now()` sweep in
  `api/main.py` also went through `_ist_today()`/`_ist_now()` (rotation overlay, first-seen,
  next-rotation, audit stamps) so every app-derived date is IST-current. Also includes
  `expedite_window_min`
  (default 0 = off): Rule 6's least-slack tie-break window; 0 is byte-identical to the
  legacy non-delay plan. **Its Settings tick mark was REMOVED 2026-07-19** (measured
  consistently harmful under per-shift staffing; forced off when ranks are applied) —
  the config field stays, UI always sends 0. **The overlap % input left Settings the
  same day** (owner rule: users never touch knobs the optimizer owns — the sweep
  contest tunes overlap and Apply persists the winner; the UI shows it read-only and
  `readConfig()` echoes the STORED `currentConfig.overlap_percent` on save, refusing
  to save before the first /run response populates it — never a form/fallback value).
  And `balance_operator_load` (default off): Rule 6's schedule-neutral operator-fairness
  post-process (Settings tick mark "Balance operator workload") — reassigns *who* runs
  each op without moving any time, so makespan/lateness are unchanged. A repair pass
  guarantees it never double-books a person (2026-07-15 live fix; see
  `tests/test_operator_invariants.py`). **Per-shift staffing (2026-07-19,
  `operator-shift-handoff`): Rule 6 books a qualified operator for EVERY shift segment
  of an op** — handoff at the 19:00/05:00 boundary to a free least-loaded qualified
  operator on the new shift, or the machine PAUSES until one frees (see the rule6
  bullet + RULES.md). `UNSTAFFED`/`headline.unstaffed_hrs` still exist for the legacy
  display path but a schedule produced with operator logic ON now yields 0 unstaffed
  hours by construction (invariant-tested in `tests/test_shift_handoff.py`; guard
  exhaustion in `_lay_segments` raises RuleError — fail loud, never under-schedule).
  Honest-plan impact on the real book (2026-07-19): makespan 47→79d, late-days
  1413→2533 unoptimized; deep optimize recovers to 79d/1607 (owner-approved cutover);
  crew floor ~44d (night pools: VMC1-3 have 2 night-qualified operators, CNC3/6 have 2).
  **Scarce-first operator pick (2026-07-19 evening, `crew-smart-scheduling`):** among
  FREE qualified operators, `_lay_segments` books the least-flexible person first
  (`op_rank` = machines-qualified count from the operator master; ties earliest-free
  then sheet order) — flexible people stay available for the machines only they can
  run. Measured on the live 71-order book: makespan 78.5 → 73.7 d on the identical
  sequence; all shift-handoff invariants hold (`tests/test_scarce_operator_pick.py`).
  Same research: the crew's SHAPE (not hours) costs ~35 d vs a machines-only world
  (43.8 d); certified LP capacity floor 37.2 calendar days (operators binding, not
  machines). Dead ends measured — do not retry: dispatch-window pick policies
  (slack/remwork/due/prio × 60-1440 min, all worse) and CP-SAT-as-sequence-oracle
  (idealized 44-45 d, greedy replay loses it all). `OVERLAP_CANDIDATES` is now
  (70, 80, 85, 88) and `CLOUD_OVERLAP_CANDIDATES` (60, 70, 80, 85, 88, 95) — 85/88
  dominate under this scheduler; best plan settings: overlap 88 + consolidation
  window 1 day (UI-settable) + split/metric unchanged. Also
  `committed_promise_slack_days` (default 3, validated ≥0) — the committed-promise
  cap's slack in days (2026-07-29, see the banner bullet); folded into
  `_inputs_signature` (it's plan-shaping, like the other knobs above).
- `engine/flow_scheduler.py` — **the flow scheduler (2026-07-19,
  `docs/superpowers/specs/2026-07-19-flow-scheduler-design.md`)**: the productized
  from-scratch rebuild (owner mandate: only the three basics are rules). Same
  contract as `rule6_allocate.run`; `pipeline.scheduler_for(config)` dispatches by
  `config.scheduler` ("classic" engine default = golden untouched; "flow" = the
  LIVE mode after cutover). Chunked piece-flow (`config.flow_chunks`, contest-
  tuned), no resource-holding, setup per re-engagement, scarce-first crewing,
  process_qty feedback (punched pieces = initial WIP downstream), reserved=
  absences, `FLOW_FINGERPRINT` in `_inputs_signature`. The optimizer evaluates
  through the same dispatcher; in flow mode the sweep contest tunes
  `flow_chunks` over `FLOW_CHUNK_CANDIDATES` (3,4,6; cloud 2,3,4,6 — flow evals
  are ~5× slower than classic, budget accordingly) and Apply persists the
  winning chunk count (wire field names keep "overlap"; `knob` says which).
  Real-book numbers: classic optimized 70.8d/1460 → flow ~44d/~1170 searched;
  crew capacity floor 37.2d. Tests: `tests/test_flow_scheduler.py` (crafted
  piece-flow/no-holding/feedback/absence/setup cases + independent validators).
- `engine/models.py` — dataclasses; each exposes `as_row()` for the trace tables.
  `Order` and `SOLine` carry `commitment` (**open|committed** — the `urgent` lane was
  removed 2026-07-29; `Order.from_json` migrates any stored `"urgent"` row to
  `"committed"` on load, keeping its `promised_date`), `promised_date`, and
  `committed_at`. `promised_date` is a display-only snapshot either way (Orders tab
  shows Promised vs Current-expected with a red drift flag when it slips). **Open is
  still informational only** (owner pivot 2026-07-16 — see the self-tuning-plan
  spec's SUPERSEDED block): a pure status label, no scheduler/Optimize effect.
  **Committed is no longer purely informational** (2026-07-29 partial reversal, see
  the banner's committed-promise-cap bullet): it now carries a soft ceiling
  (`promised_date + committed_promise_slack_days`) enforced in the optimizer's score
  + an apply-time no-regression backstop — see that bullet for the full mechanism.
  Historical note: an earlier design (2026-07-13/14) had these fields drive a
  two-pass scheduler + a hard promise veto; that was measured ~30% worse on both
  real books and discarded (the 2026-07-29 mechanism is soft, not that veto).
  `Actual` gains an
  `operator` field (2026-07-18, JSON round-trip) — **required at capture**:
  `POST /actuals` 400s on a blank operator or one not in the current operator
  master; legacy rows predating this feature default to `""` and are grouped
  as an "Unattributed" row by the efficiency report below.
- `engine/loaders.py` — read the uploaded workbook (Test4 format) → typed objects +
  non-blocking report. The 3 master sheets are read **header-driven** (`_locate_table`);
  resource-name normalization (`CNC 4` ≡ `CNC4`) and provisional-machine handling live here.
  The `OS` sentinel is never registered as a (provisional) machine.
- `engine/worktime.py` — `WorkClock`: a list of day-relative working **intervals**
  (per-machine windows) + Thursday/holiday skip; `from_config` = legacy two-shift
  window; empty intervals raise `NoWorkingWindow`.
- `engine/operator_coverage.py` — pure `machine_windows(masters, config)`: each
  machine's working window from Available Hrs/Day + operator shift coverage (two-shift
  vs 09:00–18:00 manual); blocked + unmatched-specialty report. Consumed by Rule 6
  when `apply_operator_logic` is on.
- `engine/operator_master.py` — pure module owning the app's operator/shift table
  (rotation effective **Friday shift 1**, applied **as-of the plan's start date**;
  display uses **as-of today**; persistence is a lazy catch-up write on any store
  touch). `seed_rows_from_masters` (one-time, workbook → app-owned rows, all
  unpinned), `rotate_table(table, today)` (flips unpinned two-shift rows once per
  ODD count of elapsed Fridays since `week_anchor` — an EVEN count, e.g. two missed
  Fridays, nets to no change; blank-shift/day-window rows and pinned rows never
  flip; idempotent same-day), `operators_as_of(table, as_of)` (the one PURE view
  every wiring site shares — rotate + convert to `Operator` objects, nothing
  persisted), `to_operators` (parses `machines_raw` via the same
  `loaders.parse_resource_candidates` the Excel loader uses, so a seeded row is
  indistinguishable from a workbook-loaded one), `last_friday`/`next_rotation`.
- `engine/pipeline.py` — `run_rule` (snapshots in/out/config/notes), `run_forward`
  (1→2→3→6), `RuleError`, `to_table`.
- `engine/orderbook.py` — **order-book logic (pure)**: `merge_upload` (add new /
  **update an active line's delivery date** / flag repeat / flag completed /
  intra-upload dedup, all by the **(SO#, item code)** pair) returns
  `(new_orders, updated_orders, flags)`. **Delivery date is the ONLY field a
  re-import may change** (2026-08-04,
  `docs/superpowers/specs/2026-08-04-so-delivery-date-reimport-design.md`) —
  directors revise SO Delivery Date in the Excel and re-import; quantity is
  entangled with recorded production so it stays report-only. A blank/unreadable
  uploaded date never wipes an existing one, and a completed order is never
  updated. `optimize_service.book_signature` now includes `delivery_date` (so the
  daily auto-optimize stops calling a date-only edit "nothing changed"), and an
  applied optimization stores a `dates` map in its meta which `_plan` compares —
  over the INTERSECTION of keys, so a completed or newly added order never
  false-alarms — to raise `optimize_meta.dates_changed`/`dates_changed_count` and
  the "run Start deep search" banner. The applied plan is deliberately KEPT, not
  cleared, so one date edit can never discard a searched plan. `derive_status`
  (Pending/Running/Complete), `active_so_lines` (remaining qty for planning),
  `order_rows` (dashboard). `Order`/`Actual`/`SOLine` each expose `.key = (so_no,
  item_code)`; the good-by-order / orders-with-actuals / per-process maps are all
  keyed by that pair. The DISPATCH gate (`finished_gate`) is matched via `is_dispatch`
  (tolerates the `DISAPTCH` misspelling). `split_committed_open` (still present, still
  tested) separates Committed from Open orders (the `Urgent` lane it used to also pull
  out was removed 2026-07-29) — **kept as a standalone helper but unused by planning**:
  `api._plan` and every contest are single-pass over the whole book, so lanes carry
  `commitment`/`promised_date` onto `SOLine` for display **and** (2026-07-29, committed
  only) as the input to the promise-cap penalty in `optimizer.plan_metrics` — see the
  banner's committed-promise-cap bullet; grouping/reservation is still never done here.
  **Feedback precedence guard (2026-07-25,
  `docs/superpowers/specs/2026-07-25-feedback-precedence-guardrail-design.md`):**
  `precedence_cap_error` / `rollback_cap_error` (pure) enforce piece-flow — a process's
  cumulative recorded qty (`produced`) can't exceed the good qty that cleared the
  process *before* it in the routing (first step capped at ordered qty); rollback can't
  retro-create the illegal downstream>upstream state. Wired at **`POST /actuals`**
  (before `r7.run`) and **`POST /actuals/rollback`** (before `delete_actual`), both →
  400 with a message naming the blocking step. Reuses `_norm` + `completed_by_process`
  accounting (same as planning), so **capture and planning can never disagree** and the
  planner never re-schedules already-done upstream work. Consequence: every step —
  incl. OS/inspection — must be punched, or downstream caps at 0.
- `engine/book_store.py` — durable persistence of the book: active orders + the
  completed archive (hashes keyed by a composite **`"<so_no>\x1f<item_code>"`** field;
  `complete`/`uncomplete`/`delete` target one (SO#, item) line), actuals (append-only
  list), masters workbook.
  `delete_orders` / `delete_all` (permanent deletes); `delete_actual` + `uncomplete_order`
  (per-entry **rollback**: each `Actual` has a uuid `id`, legacy backfilled);
  `set_commitment`/`clear_commitment` persist the `commitment`, `promised_date`,
  `committed_at` fields — informational only (see `engine/models.py` above).
  `save_auto_note`/`load_auto_note` (`anvitech:auto_note`) hold the self-tuning
  trigger's one-line status ("Plan auto-re-optimized …" / "Checked … still best").
  `load_absences`/`save_absence`/`delete_absence` (`anvitech:absences`) — a plain
  list of `{id, operator, from_date, to_date}`; `save_absence` assigns the uuid,
  `delete_absence` returns `False` on an unknown id (→ 404 at the API).
  **Freeze keys (2026-07-29):** `save_last_applied_schedule`/`load_last_applied_schedule`
  (`anvitech:last_applied_schedule`, `LAST_APPLIED_SCHEDULE_KEY`) — a compact per-op
  projection (machine/operator/start/end) of "the plan the floor is following," written
  **only when an optimize result is applied** (never on an ordinary display re-plan, or
  it would drift with new actuals); `save_frozen_ops`/`load_frozen_ops`/`clear_frozen_ops`
  (`anvitech:frozen_ops`, `FROZEN_OPS_KEY`) — the current frozen (in-progress) set,
  recomputed on every "Done entering — update plan". See the freeze banner bullet + the
  `engine/freeze.py` / `api/main.py` bullets below.
- `engine/freeze.py` (2026-07-29,
  `docs/superpowers/specs/2026-07-29-freeze-in-progress-restricted-optimize-design.md`) —
  **pure freeze logic, reporting/derivation only — never mutates a plan.**
  `schedule_projection(schedule)`: one row per real (machine) op in an applied schedule
  (batch/item/process_seq/machine/operator/start/end/so_refs); OS/off-lane entries are
  skipped (nothing in-house to pin). `compute_frozen_set(applied_rows, so_lines,
  good_by_step, masters)`: for each active SO-line + routing step, freezes it iff
  `good > 0` **and** `remaining > 0` (partially punched) — looks up machine/operator from
  the `applied_rows` row covering that SO for that step's `op_seq`; a step **not** found
  in the applied rows, or whose applied machine is OS/off-lane, is **not** frozen (falls
  through to normal scheduling). Called from `api._compute_and_store_frozen()` on every
  "Done entering — update plan," never from inside the pure engine.
- `engine/storage.py` — the store interface (kv/hash/list) + backends:
  `MongoStore` / `UpstashStore` / `LocalStore`; `get_store()` picks by env.
  `MongoStore` **percent-encodes hash field names** (`_enc_field`/`_dec_field`)
  before they go into the `h.<field>` update path — a raw `.` or `$` in a field name
  (e.g. an item code like `61243661-01..`) would otherwise be read as a nested path
  and break the write. Any hash field string is safe.
- `engine/optimizer.py` — **the Optimize feature's pure sequence search**: `optimize(
  so_lines, config, masters, reserved=, budget_evals=, seed=, on_progress=, should_cancel=)`
  runs the unchanged Rules 1→2→3 once, then **multi-start** search: independent restarts
  (SPT, ATC, then fresh random permutations), each hill-climbed (insertion/swap/block) until
  it stalls (`_RESTART_AFTER`), keeping the **global best** — a single trajectory got stuck
  in a worse local optimum (39.75/778 on Test5; multi-start → 39.7/713). Scores each plan
  `total_late_days + 10×makespan_days` (delivery gaps dominant — owner priority: fewest late
  deliveries, since shortest-makespan plans push more orders late). Deterministic (eval-count
  budget + fixed seed). `should_cancel()` is polled between evals so a run can be stopped
  early keeping the best-so-far. (Historical: an `objective="promise_slip"` mode scored
  committed orders against their `promised_date` for the auto committed re-sequencing
  feature; removed with the rest of the promise-rule machinery — see the Phase-2R note
  below.) **Speed:** the scheduler is memoized — `loaders`
  `normalize_resource_id`/`parse_resource_candidates` (lru_cache on fixed routing text),
  Rule 6's `op_lookup` (per machine+shift, not per op), and `WorkClock._windows_for_day`
  (per-day window cache) — ~3.5× faster per plan, results byte-identical (golden unchanged). Returns `OptimizeResult`
  with a rank per **"<so>\x1f<item>"** key; `pipeline.apply_priority_rank` replays it
  (ranked batches reorder among their own slots; unranked keep their Rule-3 slot).
  `run_forward(priority_rank=)` is the replay hook — `None` (all existing callers) is
  byte-identical. `run_forward`/`optimize`/`sweep_optimize` also accept `frozen=`
  (2026-07-29, list of `FrozenOp`-shaping dicts) — threaded straight through to
  `new_engine`/`decode` for every candidate in a contest so the frozen (in-progress)
  set is honored identically whether the plan is a single pass or a search; `frozen=
  None`/empty is byte-identical to before. See the freeze banner bullet +
  `engine/freeze.py`. Persisted via `book_store.save/load/clear_plan_priority`
  (`anvitech:plan_priority`). API: `/optimize` (admin; quick=150/deep=400 evals, one
  background thread at a time), `/optimize/status`, `/optimize/apply`, `/optimize/clear`;
  `_plan` replays the saved ranks over its single pass (every active line — see the
  self-tuning/Phase-2 notes below for why there is no separate open/committed pass
  anymore) and returns `optimize_meta` (active/saved_at/covered/uncovered/**inputs_changed**) for the
  staleness banner — `inputs_changed` compares the applied run's `inputs_sig` (sha of the
  masters workbook + plan-shaping config; schedule-neutral knobs excluded) against the
  current inputs, so a masters re-upload or Settings change after Apply is flagged
  instead of looking non-deterministic (2026-07-15 live fix). **Settings sweep
  (2026-07-15; contract rewritten same day — live regression, then two owner
  decisions: "the best setting wins" + "ONE option, ≤1,000 plans total"):**
  `optimizer.sweep_optimize` auto-tunes the overlap % as a FAIR CONTEST inside one
  TOTAL budget — the budget splits EQUALLY across the contenders (the current overlap
  + `OVERLAP_CANDIDATES = (50,60,70,80)`; 90/100 dropped after losing every measured
  contest on both real books; an off-list current setting still joins its own
  contest) and the best plan wins outright. Live: single "Deep search" button, 1,000
  plans total → 250/contender ≈ the 2,400-plan full contest at 42% compute (measured;
  a cheap rank-then-deepen shape was rejected — a 100-eval ranking picks the wrong
  winner 2/3). The current setting's only privileges: runs first (early Stop keeps it
  fully searched) and wins exact ties (no churn). Apply persists the winning overlap
  into the saved plan config and `inputs_sig` is computed against the winning settings.
  (Historical: a per-candidate "promise guard" that vetoed overlaps worsening the
  committed pass existed 2026-07-14→16 and was removed with the rest of the promise-rule
  machinery — Phase 2R below; every candidate now searches the same one-pool book.)
  **Cloud compute (2026-07-15, owner decision):** with `GITHUB_DISPATCH_TOKEN` +
  `OPTIMIZE_WORKER_SECRET` set on Render, Start dispatches the FULL 2,400-plan
  contest (`optimize_service.CLOUD_OVERLAP_CANDIDATES` × 400) to a free GitHub
  Actions runner (~8-10 min; `.github/workflows/optimize.yml` →
  `scripts/cloud_optimize_worker.py` → `engine/optimize_service.py`, the ONE shared
  code path — payload round-trips the book via the models' own to/from_json, so a
  cloud run is byte-identical to a local run of the same contest, E2E-verified
  717/36.79 on Test6@11-07). Worker endpoints `GET /optimize/job/{id}` /
  `POST /optimize/progress` / `POST /optimize/result` authenticate via the
  `X-Worker-Secret` header (gatekeeper bypass, constant-time). Fallbacks: dispatch
  failure / worker error / `OPTIMIZE_CLOUD_TIMEOUT_MIN` (20) exceeded → compute
  locally (1,000-total split), so the button always works; env unset → pure local.
  `GITHUB_DISPATCH_TOKEN=manual` skips the GitHub call (run the worker by hand).
- **Machine-set as a third Optimize dimension (2026-07-29, new engine only,
  `docs/superpowers/specs/2026-07-29-machine-set-optimize-dimension-design.md`).**
  The contest now searches **sequence × overlap % × machine-set** — for every
  plan it also tries **Allotted-only** vs **Allotted + Suggested** (deduped
  union, Allotted first) as the option set for in-house machining/manual/
  inspection ops, keeping the single global best by the unchanged score.
  `PlanConfig.flexible_machines` (default `False` = today's Allotted-only,
  byte-identical; folded into `_inputs_signature` like `overlap_percent`) is
  **optimizer-owned** — never hand-edited in Settings (read-only, like
  overlap), set only when Optimize applies a winning result, and replayed by
  every subsequent Plan the same way the tuned overlap is (`_new_masters`
  loads masters at the applied flexibility). Local fallback
  (`new_engine.sweep_optimize`) runs the golden-section `tune` once per
  machine-set and keeps the better; the cloud contest wraps its overlap×
  sequence loop in the same outer `(False, True)` loop. Cost: roughly **2×**
  the sequence×overlap search (cloud ~15→~30 min, local ~200→~400 plans) —
  the progress-bar budget display doubles for `scheduler=="new"` only
  (`api/main.py`'s three `sweep_total_evals` sites: the initial local-mode
  estimate in `_start_optimize` plus its two cloud-dispatch-failure/timeout
  fallbacks). Classic/flow
  ignore the flag entirely (single-pass, their own `rule6_allocate.
  _resolve_candidates`/`split_parallel` machine selection).
- **Feedback-triggered optimize, THURSDAY-gated (2026-07-22,
  `docs/superpowers/specs/2026-07-22-feedback-triggered-optimize-design.md`,
  supersedes the twice-weekly cron below — itself **SUPERSEDED 2026-07-29** by the
  freeze-in-progress daily cadence, banner above — kept for the `_try_start_auto()`
  guard mechanics the new cadence still reuses unchanged) — historical: the job
  order re-optimized once a week, on Thursday; the plan reflected new facts every
  day.** The **"Done entering — update plan"** button (both roles) hit
  **`POST /optimize/done`**, which **first checked `_is_optimize_day()`** (today, IST,
  is Thursday — `_ist_today().weekday() == _OPTIMIZE_WEEKDAY`, `= 3`). **Non-Thursday:**
  it returned `{started:False, reason:"not_optimize_day"}` and the client just
  `runPlan(false)`d (facts refresh, NO contest). **Thursday** (the weekly off day — the
  owner punches Wednesday's feedback then, so the new schedule is ready for Friday): it
  called `_try_start_auto()`, which started an auto-applying contest unless a run was
  already going or nothing changed since the last one it RAN (applied **or**
  last-searched book+inputs fingerprint — `anvitech:last_searched`, written by
  `_finalize_optimize` from the contest-start snapshot; writes a "plan unchanged" note).
  It was **NOT cloud-only** (local fallback). The frontend blocked on live progress
  (`/optimize/status`, **no Stop button** — owner's block-and-wait decision; admins
  keep the Settings-panel Stop) then `runPlan(false)`d to the auto-applied winner.
  **Removed:** the Mon/Fri GitHub cron
  (`.github/workflows/scheduled-optimize.yml`), `POST /optimize/scheduled`, and
  `nextScheduledOptimize()`. **Now (2026-07-29):** `_is_optimize_day`/`_OPTIMIZE_WEEKDAY`
  are ALSO removed — `POST /optimize/done` calls `_try_start_auto()` unconditionally,
  every day (the freeze makes this safe; see the banner). The admin manual
  **"Start deep search"** (`POST /optimize`) was never weekday-gated, and (as of the
  same day, see the banner) also now respects the freeze — naturally unrestricted only
  when nothing is in progress. Auto-apply is still strictly-better-or-nothing
  (`_auto_apply_result`); `AUTO_OPTIMIZE=0` still disables it (test isolation only).
- **Scheduled optimize (2026-07-18 — design spec since pruned as superseded,
  superseded the event-triggered self-tuning-plan Phase 1; itself
  **SUPERSEDED 2026-07-22** by the feedback-triggered bullet above — kept for the guard
  mechanics the new trigger still reuses) — historical: the job order re-optimized
  itself, but only **twice a week**, never on every change.** The owner's rule at the
  time: re-sequencing the floor daily destroys schedule trust; facts (punches) updated
  the plan every day, but the JOB ORDER was re-optimized only **Monday and Friday at
  11:00 IST (05:30 UTC)** — after the ~10:00 feedback entry, ready before shift 2, and
  (with Thursday the weekly off) equally spread from both directions. No event trigger
  fired a contest on its own back then: uploads, `/orders/delete`/`/orders/clear`,
  commit/uncommit/urgent, `/absences` POST/DELETE, and a `persist=True` `/run` (Settings
  save) never started one — new orders arrived Open and waited for the next scheduled
  run or the owner's manual Optimize button. The entry point into the decision logic was
  `POST /optimize/scheduled` (worker-secret auth, same gatekeeper bypass list as the
  other worker endpoints), hit by a GitHub Actions cron
  (`.github/workflows/scheduled-optimize.yml`, `cron: "30 5 * * 1,5"` +
  `workflow_dispatch` for manual testing; wake-tolerant retry loop since the free Render
  instance may be asleep) calling `_try_start_auto()`, which was gated **cloud-only**
  (`_cloud_config()` unset → skip with a note, never a 20-40 min local burn on the cron
  path; the manual Optimize button always kept its local fallback), **one-at-a-time**
  (a contest already running → no-op, no queueing), and by the **book-fingerprint skip**
  (`optimize_service.book_signature(so_lines, absences=)` compared against the applied
  ranks' saved `book_sig` — nothing material changed since the last applied plan ⇒ skip
  silently, zero cost). **All of this is now removed (2026-07-22):** the cron workflow
  file, `POST /optimize/scheduled`, the fixed Mon/Fri clock, and the cloud-only gate —
  see the feedback-triggered bullet above for the replacement (same `_try_start_auto()`
  guard function, now invoked by `POST /optimize/done` and no longer cloud-restricted).
  `_bump_book_changed()`, the `_AUTO` pending/chaining dict, and `_drain_pending_auto()`
  stayed removed from the 2026-07-18 cutover — there was never anything left to debounce.
  **Auto-apply is still strictly-better-or-nothing**: `_finalize_optimize` calls
  `_auto_apply_result()` for auto runs, which computes the incumbent (`_incumbent_metrics`
  — the applied ranks, or none, replayed on TODAY'S book via `_all_lines_schedule`, scored
  over ALL active lines so both sides are on the same domain) and applies
  (`_optimize_apply()`) only if the contest's `best` strictly beats it; either way it
  writes a one-line note via `book_store.save_auto_note` (`anvitech:auto_note`) — e.g.
  *"Plan auto-re-optimized 14:32: 445 late-days (was 471), overlap 80 → 70"* if it
  applied, *"Checked 14:32: current plan still best (471 late-days)"* if the contest
  ran but didn't beat the incumbent, or *"No new feedback since the last optimization
  — plan unchanged."* if `_try_start_auto()` skipped before running a contest at all
  — surfaced on `/run`'s `auto_note` field and the Orders tab. Note timestamps are
  stamped in **IST** (`_ist_now()` = `utcnow() + timedelta(hours=5, minutes=30)`; the
  server itself runs
  UTC) since the clock is a named local time the owner reasons about. `AUTO_OPTIMIZE=0`
  is an **internal test-isolation env var only** (`_auto_enabled()`) — never documented
  or exposed in the UI; there is no user-facing off switch by owner decision.
- **Operator absences (Phase 3, same spec).** `anvitech:absences` (see `book_store.py`
  above) — day-granularity `{operator, from_date, to_date}` rows, ISO in the store,
  DD-MM-YYYY in the UI. `optimize_service.absence_reservations(absences)` turns them into
  Rule 6 `reserved={operator: [(start,end),…]}` blocks (00:00 of `from_date` through 00:00
  of the day after `to_date`) — **physical unavailability, not a promise reservation** —
  merged (`merge_reservations`) into every plan pass and every contest candidate
  (`ContestSetup.absence_reserved`; the cloud payload round-trips `absences` via
  `build_payload`/`parse_payload`). `engine/analytics.py` subtracts absent working days from
  an operator's available capacity so utilization stays honest. An absence whose operator
  is no longer in the current masters (re-upload) is **orphaned**: ignored by planning,
  listed in `GET /absences`'s `orphans` and as a non-blocking `ABSENT_OPERATOR_UNKNOWN` row
  in the validation report (`api._absence_orphans`/`_report_for_book`) — never fatal.
  API: `GET /absences` (any role) returns `{absences, orphans, operators}`; `POST
  /absences` / `DELETE /absences/{id}` (admin) validate dates/operator, no password
  re-auth (non-destructive, reversible), no trigger call (see the feedback-triggered
  optimize bullet above — absences don't call `_try_start_auto` directly; only the
  Done button does). UI: an
  always-visible Settings-area panel (not nested in the admin-only toolbar, so the user
  role sees the read-only list) with add/remove controls CSS- and server-gated admin-only.
- **Operator & shift master rotation (2026-07-18,
  `docs/superpowers/specs/2026-07-18-operator-master-rotation-design.md`) — operators
  moved OUT of Excel, INTO the app.** Store: `anvitech:operators` (`{week_anchor,
  operators: [{id, name, machines_raw, shift, pinned}]}`). **Seeding is one-time**:
  `api._with_operator_overlay` seeds from the workbook's operator sheet only when
  the store table is empty AND a workbook is on file
  (`operator_master.seed_rows_from_masters`); every subsequent upload is ignored for
  operators — the sheet becomes a fossil after that. **The ONE wiring point**:
  `api._current_masters()` calls `_with_operator_overlay(base)` on every call, which
  replaces `base.operators` with the store's rows (via `operator_master.to_operators`)
  — every consumer (Rule 6, `operator_coverage`, analytics, gantt, shift-wise)
  inherits automatically, no per-consumer code. **Rotation**: every Friday, unpinned
  two-shift ("First shift"/"Second shift") operators swap; a per-operator "stays on
  current shift" pin exempts them; blank-shift (day-window/manual) operators never
  rotate. `operator_master.rotate_table` is lazy and idempotent — it counts every
  elapsed Friday since `week_anchor` and nets an ODD count to one flip (an EVEN
  count, e.g. two missed Fridays, nets to no change — true catch-up, not a
  double-flip). **Effectiveness rule (owner, 2026-07-18): the swap takes effect at
  Friday SHIFT 1**, so a plan whose schedule BEGINS on/after a rotation Friday sees
  the rotated shifts even if computed on Thursday (the off day) — `api._plan`
  re-overlays `operator_master.operators_as_of` onto the already-fresh masters using
  `eff_start` (the plan's effective start date, not `date.today()`);
  `_with_operator_overlay`/`GET /operators`/the Settings panel use **as-of TODAY**
  for display. Persistence of `week_anchor` is a **lazy catch-up write** — any
  request that observes today ≥ an unapplied Friday persists the rotation
  (idempotent same-day, never a double-write). Cloud payload carries the
  ALREADY-EFFECTIVE operator rows (computed as-of the plan start when the payload
  is built), so the worker applies them directly
  (`optimize_service.prepare_contest(operator_table=)`) with no anchor logic
  worker-side; local == cloud byte-identical. **`_inputs_signature` now folds in the
  operator table's sorted row CONTENT** (name/machines/shift/pin — ids and
  `week_anchor` excluded; the masters sha alone no longer covers operators) so a
  rotation or an edit correctly flags an applied optimization `inputs_changed`;
  excluding `week_anchor` specifically avoids firing on a no-op double-Friday
  catch-up (churn regression-tested). The `_try_start_auto()` skip check is an OR:
  run when EITHER `book_sig` OR the current `_inputs_signature` differs from the
  applied meta's (a rotation alone doesn't change `book_sig`). **API**
  (`api/main.py`): `GET /operators` (any role, `{operators, next_rotation}`,
  triggers seed/rotation as a side effect), `POST /operators` / `PATCH
  /operators/{id}` / `DELETE /operators/{id}` (admin; each calls
  `_current_masters()` FIRST so a direct mutation on a fresh deploy can never
  suppress the one-time workbook seed — a review-caught data-loss hole, closed). No
  trigger call (operator mutations don't call `_try_start_auto` directly — only the
  Done button does; see the feedback-triggered optimize bullet above). **UI**:
  Settings "Operators & shifts" panel (`#operators-panel` in
  `web/index.html`) — table (name, machines, shift, "Stays" pin, remove), add-row
  form, "Next rotation: Friday DD-MM-YYYY"; admin edits, user role read-only (the
  pin shows as plain text, not a checkbox).
- **Phase 2 — promise ceiling — DISCARDED (owner pivot, 2026-07-16).** A one-pool contest
  with a hard promise veto was built and measured on both real books: ~30% worse than
  the (now-removed) two-pass approach, because zero-slack promises collapse the feasible
  region. The owner redefined the model instead: **lanes are status labels, not
  protection** (see `engine/models.py` above). Removed entirely: `optimizer.
  promise_ceiling_ok`/`promise_score`/`promise_slip_metrics`, the `objective="promise_slip"`
  branch, `sweep_optimize`'s `feasible=`/`candidate_setup=` guard, `_plan`'s two-pass
  branch + its pass-1/pass-2 schedule merge (the api-level alias that fed
  `optimize_service.reservations_from_schedule` into it is gone; the function itself is
  kept, now unreferenced), the promise-recovery
  auto-trigger/replay (`_maybe_start_recovery`, `_RECOVERY` slot, `anvitech:promise_recovery`),
  the urgent push-warning preview (`_preview_urgent_pushes`), and Rule 1/Rule 3's
  lane-aware special-casing (`rule1_consolidate`/`rule3_tiebreak_process_time` are uniform
  again — every lane sorts/groups the same way). A regression pins the pivot: a committed
  +promised book plans **byte-identical** to the same book all-open
  (`tests/test_replay_single_pass.py`, `tests/test_optimize_service.py::
  test_lanes_have_zero_scheduling_effect` — both now apply only to a book with
  **nothing committed**; see the next note). Design history: the promise-recovery /
  committed-resequencing design (2026-07-14, spec pruned as superseded).
  **Partially reversed 2026-07-29** (see the banner's committed-promise-cap bullet):
  committed orders regained a scheduling effect, but a deliberately **soft** one — a
  convex penalty in the search plus a no-regression backstop at apply, never the hard
  veto/reserved-capacity/two-pass machinery this bullet describes as discarded. That
  is precisely why it doesn't repeat the ~30% regression measured here. Open orders
  are untouched by the reversal — still a pure label.
- `engine/gantt.py` — `build_gantt`: Rule 6 schedule → worker-facing Gantt view-model
  (per-order rows, time-positioned bars by machine, **operator** on each bar, split
  halves as separate bars, Pending/Running label).
- `engine/analytics.py` — pure `build_analytics(schedule, masters, config, batches,
  absences=None)`: utilization & bottlenecks from the current plan. **Utilization =
  busy ÷ each resource's OWN available time in the plan window** (`[min(start),
  max(end)]`), so every machine is judged fairly against its own capacity. Machine
  capacity reuses Rule 6's `_clock_factory` clock (same shifts/coverage/calendar as
  the schedule). `absences` (2026-07-16) subtracts each operator's in-window working
  absence days from their available capacity (`_absent_working_days`) so utilization
  stays honest under a marked-absent operator. Sections: per-machine (+ type
  rollup), per-operator (busy vs shift capacity), per-process (work share, not %), and a
  headline (bottleneck / under-used ≤30% / totals). Surfaced as the **Analytics** tab
  (`trace.analytics`; CSS bars + tables in `web/`, no chart lib).
- `engine/efficiency.py` (2026-07-18,
  `docs/superpowers/specs/2026-07-18-operator-efficiency-report-design.md`) —
  **pure monthly operator efficiency report**, reporting-only (never touches
  the plan/schedule). `monthly_report(actuals, absences, masters, config,
  year, month) -> list[dict]`, one row per operator, sorted by efficiency %
  descending (Unattributed always last); `REPORT_COLUMNS` is the column
  contract (also the CSV header for a month with no rows). Formula:
  **Efficiency % = Earned ÷ Attended × 100** — Earned = Σ standard
  `cycle_time(item, process)` × good qty punched; Attended = Σ per distinct
  (operator, date, shift) worked window − that group's recorded downtime −
  setup minutes, counted **once** per (operator, date, shift) even when
  several jobs were punched that window. Fairness rules baked in: only GOOD
  qty earns (rejects earn nothing, surface as Reject %); downtime and setup
  are **neutral** (they shrink attended time but never earn nor penalize); a
  punch whose item/process has no cycle-time standard is **excluded from
  BOTH sides** — no earned minutes AND no attended contribution (neither its
  shift window nor its downtime/setup) — and counted in "No-standard
  punches" (nobody is judged against a standard that doesn't exist); absence
  days are their own column from the absence table, never folded into pace;
  legacy punches with no operator name land in an "Unattributed" row. Shift
  text is normalized case-insensitively ("1st shift"/"first"/"1" → first,
  "2nd shift"/"second"/"2" → second — the Capture form's Shift field is now a
  `<select>` constrained to "1st shift"/"2nd shift" so free text can't drift);
  any other/unrecognized shift text falls to the manual (day) window, a
  documented fallback for legacy free-text punches.
  **Review-caught fairness bug (fixed same task, RED-first regression
  pins):** the first cut excluded no-standard punches from Earned only,
  still charging their shift window (minus their downtime/setup) to
  Attended — a shift with several no-standard jobs deflated Attended and
  showed a misleadingly LOW efficiency (45.5% vs the fair 90.9% on the same
  punches once excluded both-sides). Shift windows come from `config`
  (First/Second/manual hours), never hardcoded. **Data is never deleted**
  (owner decision — a year of punches ≈ 5-10 MB of the 512 MB Atlas tier;
  the report is computed on demand for any month, nothing pruned). API: `GET
  /efficiency`/`GET /efficiency.csv` (admin) — see the `api/main.py` bullet.
- `engine/rules/ruleN_*.py` — Rules 1–7, one pure `run(...)` each; 4/5 also expose
  the calc helpers Rule 6 imports. (Rule 7 = `rule7_capture_actuals`. There is no
  `rule8` module — Rule 8 is the unified "Plan" over the order book; see `api._plan`.)
  Rule 6 (`rule6_allocate.py`) also has: `_is_setup_machine(mid, masters)` — the
  90-min setup (`config.setup_time_min`) is charged to **CNC/VMC machining only** (id
  `CNC*`/`VMC*`, or the master's CNC-lathe / Vertical-Machining-center type); manual/
  finishing steps get **0 setup** (2026-07-11 change). **Expedite window**
  (`config.expedite_window_min`, default 0 = off): the op-selection step collects all
  ready ops into `options`, then — when the window is > 0 — picks the **least-slack** op
  among those startable within the window of the earliest feasible start (else the
  legacy earliest-feasible, priority tie-break). It never idles a resource (only
  ready-now ops are chosen). Trade-off measured on Test5: pulls the worst-stuck orders
  in (worst 48.6→38.7 days) but can push a currently-on-time order late — a tick mark so
  the planner can A/B it, **not on by default**. `_allocate_op` (smart **parallel
  split** of alternative-machine steps — split the qty to finish soonest, only when
  faster; flag `split_parallel`). `_resolve_candidates(proc, config)` is **parallelization-aware**:
  split OFF → the Allotted machine(s) only (Suggested fallback if blank); split ON →
  the union of Allotted + Suggested (Allotted first). OS/off-machine detection is
  independent of the toggle. Also `_is_offmachine` (**DISPATCH/OS** steps — no machine + no
  cycle time → scheduled as a **visible zero-duration milestone** on an "OS /
  Outsourced" or "Off-machine" lane, so outsourcing is shown, never ignored;
  `_offmachine_lane` picks the lane). A **DISPATCH** milestone (matched via
  `orderbook.is_dispatch`) is placed at the **latest end across all the batch's
  processes** — it waits for the whole order (overlap can let a later step finish before
  an earlier long one), so dispatch never precedes a still-running process. **Overlap
  pacing:** a step may START early (overlap) but its entries' END is extended to ≥ the
  latest end of the batch's earlier processes — a fast step starved by a slow predecessor
  finishes just after it, never before (occupancy unchanged, span grows); full-completion
  successors (OS / sequential / no-cutting) then wait for that *paced* end (`slow[0]`).
  `_is_os` (outsourced step — Allotted/Suggested =
  `OS`, or an `OS` word in the name when no real machine) reserves the **cycle-time as
  a flat, continuous 24×7, unlimited-parallel, operator-less block** on the "OS /
  Outsourced" lane; it is **fully sequential both sides** — its in-house predecessor
  runs to 100% before the block starts (no Rule 5 overlap *into* an OS step) and the
  successor waits for the whole block. A blank OS cycle stays a zero-duration
  milestone. OS/off-machine lanes are excluded from the machine-utilization view.
  **Reservations:** Rule 6 has an optional `reserved={machine|operator:
  [(start,end), ...]}` argument — when finding an op's
  earliest feasible start, skip windows that overlap a reservation, and reject
  placement that would not finish before the next reservation begins. `reserved=None`
  is byte-identical to today; the one live caller today is **operator absences**
  (`api._plan`/every contest pass `reserved=optimize_service.absence_reservations(...)`
  — physical unavailability, not a promise). The mechanism was originally built for
  the two-pass committed/open split (2026-07-13); that caller was removed in the
  Phase-2R pivot (see the optimizer bullet above), leaving the generic `reserved=`
  kwarg serving only absences now.
- `api/auth.py` — accounts (2 roles), `authenticate`, signed-cookie
  `make_token`/`verify_token`, session secret, login rate limiter. Stdlib only.
- `api/main.py` — FastAPI: `/login` `/logout` `/me`, `/upload` (merge, admin),
  `/run`=`/rerun` (plan the book; admin+`persist` saves the config), `/orders`
  (+ `/orders/delete`, `/orders/clear` — admin, **password-confirmed**), `/actuals`
  (+ `/actuals/rollback`), `/items` (`so_nos` for the SO dropdown), `/gantt`,
  `/report`, `/trace/{id}`. `gatekeeper` (session + CSRF) + `security_headers`
  middleware; `require_admin`; `require_password` (re-auth on destructive deletes);
  helper-tab augmentation. **`POST /actuals` (2026-07-18):** requires a
  non-empty `operator` that exists in the current operator master — 400
  `operator is required` (blank) / `unknown operator '<name>'` (not on file);
  otherwise unchanged, either role. **Commitment endpoints (admin, role-gated, non-destructive,
  no password re-auth):** `/orders/commit`, `/orders/uncommit` — set status +
  snapshot `promised_date` (= current expected completion from a fresh plan).
  **`/orders/urgent` is removed (2026-07-29)** — the Urgent lane is gone; a stored
  `commitment == "urgent"` row migrates to `"committed"` on load (`Order.from_json`).
  `promised_date` is no longer purely informational for Committed — see the banner's
  committed-promise-cap bullet (soft ceiling at `promised_date +
  committed_promise_slack_days`). No trigger call — commit/uncommit don't call
  `_try_start_auto` directly (see the feedback-triggered optimize bullet above; only
  the Done button does). They no longer gate on a push-preview/warning (the
  `_preview_urgent_pushes` confirm-modal was removed with the rest of Phase 2R).
  **`_plan` is a single pass, always** — every active line (all lanes) goes through
  one `run_forward` call; operator absences are the only `reserved=` (see the Rule 6
  bullet above); a saved Optimize/auto-optimize result replays via `priority_rank=`
  (expedite forced off while ranks exist). Returns `optimize_meta` (staleness banner)
  and `auto_note` (`book_store.load_auto_note()`, the auto-trigger's status
  line, IST-stamped). **Feedback trigger** (2026-07-22, cadence updated 2026-07-29;
  see the optimizer bullet above for the full mechanics): `_try_start_auto()`/
  `_auto_apply_result()`, endpoint `POST /optimize/done` (either role — the "Done
  entering — update plan" button) — runs **every day** (the Thursday gate /
  `_is_optimize_day()` is removed); `_try_start_auto()` still no-ops when a contest
  is already running or nothing material changed. Before starting the contest,
  `_compute_and_store_frozen()` derives and persists the in-progress **frozen set**
  (`engine/freeze.compute_frozen_set` over `book_store.load_last_applied_schedule()`
  + the day's punches → `anvitech:frozen_ops`) so the contest pins already-started
  ops to their last-applied machine/operator — this restriction is what makes the
  daily cadence safe. `book_store.save_last_applied_schedule` is written only when
  an optimize result is **applied** (`_optimize_apply()`), never on an ordinary
  display re-plan.
  **Absences:** `GET /absences` (any role, `{absences, orphans, operators}`), `POST
  /absences` / `DELETE /absences/{id}` (admin) — see the `book_store.py`/optimizer
  bullets above; `_absence_orphans` feeds the `ABSENT_OPERATOR_UNKNOWN` rows
  `_report_for_book` appends.
  **Operators:** `GET /operators` (any role, `{operators, machines,
  next_rotation}` — triggers seed/rotation as a side effect via
  `_current_masters()`; `machines` is `_machine_options(masters)`, the uploaded
  workbook's **Machine master** rows as `{id, name, type, provisional}` sorted
  `(provisional, type, id)`, added 2026-08-04 to feed the Settings machine
  picker — read-only, no plan effect, provisional machines included so they can
  still be staffed), `POST
  /operators` / `PATCH /operators/{id}` / `DELETE /operators/{id}` (admin, each
  calling `_current_masters()` first for the same seed-once guarantee) — see the
  operator-master-rotation bullet above.
  **Efficiency report (2026-07-18):** `GET /efficiency?year=&month=` (admin,
  JSON `{year, month, rows}`) and `GET /efficiency.csv?year=&month=` (admin,
  CSV download `operator-efficiency-YYYY-MM.csv`, BOM-prefixed, built
  server-side — unlike the client-scraped Rule-6 CSVs, there's no on-screen
  table to scrape until Preview is clicked) — both share `_efficiency_rows`
  (loads actuals/absences/masters/config, calls
  `engine.efficiency.monthly_report`) and `_validate_year_month` (400 on
  month outside 1-12 or year outside 2000-2100). Pure reporting — no
  plan/schedule effect.
- `web/` — `login.html` (self-contained login page), `📋 Orders` tab (order book +
  delete, with a **password-confirm modal**, the commit/uncommit lane controls
  (the Urgent button/badge was removed 2026-07-29 — two lanes only), and the
  auto-note line), the per-rule tabs (Rule 7 = Capture Actuals,
  with an **SO No dropdown**, a **required Operator dropdown** (2026-07-18, fed by
  `GET /operators`, same list either role sees on Settings — the form blocks
  submit and focuses the field when it's blank), per-entry **↺ Rollback** button,
  and the **"Done entering — update plan"** button for both roles — hits `POST
  /optimize/done`: **every day (2026-07-29, no more Thursday-only gate)** it starts a
  feedback-triggered, auto-applying RESTRICTED contest (in-progress ops frozen to
  their last-applied machine/operator — see the freeze banner bullet; skipped with a
  "plan unchanged" note if nothing material changed or one's already running), blocks
  on `/optimize/status` progress, then refreshes the plan to pick up the winner), an
  always-visible
  **Operator Absences** panel (list visible to both roles; add/remove controls
  admin-only), a Settings **Operators & shifts** panel (table
  of name/machines/shift/"Stays" pin + add-row; "Next rotation: Friday
  DD-MM-YYYY"; admin edits, user role read-only). **Machines are PICKED, never
  typed (2026-08-04,
  `docs/superpowers/specs/2026-08-04-operator-machine-picker-design.md`):** the
  cell is chips (✕ to remove) + a "＋ Add machine" `<select>` of the unpicked
  machines from `GET /operators`'s `machines`, `<optgroup>`-grouped by machine
  type. It writes canonical ids joined by `/` — the ONE separator both machine
  parsers agree on (`ppc_engine`'s `parse_machine_options` splits `/,` only;
  `engine/loaders.parse_resource_candidates` also splits `&`/` or ` and strips
  non-alphanumerics), so a hand-typed `CNC1.CNC2` can no longer silently
  canonicalize to one unmatched id and disqualify that operator from every
  machine. `parseMachinesRaw` in `app.js` MIRRORS the live-engine parser exactly
  (split `/,`, uppercase, strip whitespace) — deliberately, so any token the
  scheduler can't match renders as a red "unknown" chip instead of the panel
  drawing healthy chips the scheduler doesn't agree with. Chip edits PATCH
  immediately and re-render from a local cache; the follow-up `runPlan(false)`
  is debounced (`replanSoon`) so a burst of clicks costs one re-plan, a Settings **Operator
  efficiency** block (2026-07-18, admin-only — month picker defaulting to last
  month, "Preview" rendering the on-screen report table, "Download efficiency
  report (CSV)"; `previewEfficiency`/`renderEfficiencyTable`/
  `downloadEfficiencyCsv` in `app.js` call `GET /efficiency`/`GET
  /efficiency.csv`), and a `📊 Gantt` tab; `app.js`
  renders the trace and hides admin-only controls for the user role (no per-rule
  UI code).

## Resolved design decision (data-confirmed)

**Rule 3 "total process time" = sum of per-process _cycle_ times**, not the
sparse "Total time" column. Only the cycle-time sum reproduces the SO-Remarks
oracle (`61240807-01` highest, `61247047-01` lowest, `61241949-01` > `61247047-01`).

## Workflow notes

- This project was scoped via the brainstorming → spec → plan flow. When making
  substantive changes, update `RULES.md` / the spec first, then the code.
- Git repo on GitHub (`riittiin/anvitech-ppc-engine`); pushing to `main`
  auto-deploys to Render. Commit/push to `main` only when the user asks.
