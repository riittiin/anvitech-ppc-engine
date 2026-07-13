# CLAUDE.md — Anvitech PPC Engine

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
  auto-redeploys. See README "Free public deployment".

## Map of the code

- `engine/config.py` — tunable params + validation. Includes `expedite_window_min`
  (default 0 = off): Rule 6's least-slack tie-break window (Settings tick mark
  "Expedite urgent orders"); 0 is byte-identical to the legacy non-delay plan. And
  `balance_operator_load` (default off): Rule 6's schedule-neutral operator-fairness
  post-process (Settings tick mark "Balance operator workload") — reassigns *who* runs
  each op without moving any time, so makespan/lateness are unchanged.
- `engine/models.py` — dataclasses; each exposes `as_row()` for the trace tables.
  `Order` and `SOLine` now carry `commitment` (open|committed|urgent), `promised_date`,
  and `committed_at` for promise protection.
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
- `engine/pipeline.py` — `run_rule` (snapshots in/out/config/notes), `run_forward`
  (1→2→3→6), `RuleError`, `to_table`.
- `engine/orderbook.py` — **order-book logic (pure)**: `merge_upload` (add new /
  flag repeat / flag completed / intra-upload dedup, all by the **(SO#, item code)**
  pair — same SO# + different item = two orders), `derive_status`
  (Pending/Running/Complete), `active_so_lines` (remaining qty for planning),
  `order_rows` (dashboard). `Order`/`Actual`/`SOLine` each expose `.key = (so_no,
  item_code)`; the good-by-order / orders-with-actuals / per-process maps are all
  keyed by that pair. The DISPATCH gate (`finished_gate`) is matched via `is_dispatch`
  (tolerates the `DISAPTCH` misspelling). `split_committed_open` separates protected
  (Committed + Urgent) from Open orders for two-pass planning; carries
  `commitment`/`promised_date` forward to `SOLine` so rules can see the lane.
- `engine/book_store.py` — durable persistence of the book: active orders + the
  completed archive (hashes keyed by a composite **`"<so_no>\x1f<item_code>"`** field;
  `complete`/`uncomplete`/`delete` target one (SO#, item) line), actuals (append-only
  list), masters workbook.
  `delete_orders` / `delete_all` (permanent deletes); `delete_actual` + `uncomplete_order`
  (per-entry **rollback**: each `Actual` has a uuid `id`, legacy backfilled);
  `set_commitment`/`clear_commitment` persist the `commitment`, `promised_date`,
  `committed_at` fields for promise protection.
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
  early keeping the best-so-far. **`objective="promise_slip"`** switches the scorecard to
  promise recovery — `promise_slip_metrics` scores committed orders vs their `promised_date`
  (not delivery date); used by the auto committed re-sequencing (below). **Speed:** the scheduler is memoized — `loaders`
  `normalize_resource_id`/`parse_resource_candidates` (lru_cache on fixed routing text),
  Rule 6's `op_lookup` (per machine+shift, not per op), and `WorkClock._windows_for_day`
  (per-day window cache) — ~3.5× faster per plan, results byte-identical (golden unchanged). Returns `OptimizeResult`
  with a rank per **"<so>\x1f<item>"** key; `pipeline.apply_priority_rank` replays it
  (ranked batches reorder among their own slots; unranked keep their Rule-3 slot).
  `run_forward(priority_rank=)` is the replay hook — `None` (all existing callers) is
  byte-identical. Persisted via `book_store.save/load/clear_plan_priority`
  (`anvitech:plan_priority`). API: `/optimize` (admin; quick=150/deep=1000 evals, one
  background thread at a time), `/optimize/status`, `/optimize/apply`, `/optimize/clear`;
  `_plan` passes the saved ranks to the open pass only (committed pass untouched) and
  returns `optimize_meta` (active/saved_at/covered/uncovered) for the staleness banner.
- **Promise recovery (auto committed re-sequencing)** — when a disruption makes committed
  orders slip past their promises, `api._plan`'s Pass 1 auto-triggers a **background**
  `optimize(objective="promise_slip")` on the committed set (its own slot `_RECOVERY`,
  separate from the manual `_OPTIMIZE`), persists the result as ranks
  (`book_store.save/load/clear_promise_recovery` → `anvitech:promise_recovery`), and replays
  it on every Plan (expedite-off) until the committed set/promises change (freshness =
  `_recovery_signature`, which includes each promise date). Never worse than date-order
  (search seeded with it); no slip → no search → byte-identical. `_plan` returns
  `recovery_meta` (active/promises_saved/slip_before-after/computing); `web/` shows a quiet
  `#recovery-note` (informational only — no control). Design:
  `docs/superpowers/specs/2026-07-14-promise-recovery-committed-resequencing-design.md`.
- `engine/gantt.py` — `build_gantt`: Rule 6 schedule → worker-facing Gantt view-model
  (per-order rows, time-positioned bars by machine, **operator** on each bar, split
  halves as separate bars, Pending/Running label).
- `engine/analytics.py` — pure `build_analytics(schedule, masters, config, batches)`:
  utilization & bottlenecks from the current plan. **Utilization = busy ÷ each resource's
  OWN available time in the plan window** (`[min(start), max(end)]`), so every machine is
  judged fairly against its own capacity. Machine capacity reuses Rule 6's `_clock_factory`
  clock (same shifts/coverage/calendar as the schedule). Sections: per-machine (+ type
  rollup), per-operator (busy vs shift capacity), per-process (work share, not %), and a
  headline (bottleneck / under-used ≤30% / totals). Surfaced as the **Analytics** tab
  (`trace.analytics`; CSS bars + tables in `web/`, no chart lib).
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
  **Two-pass promise protection:** Rule 6 gains an optional `reserved={machine|operator:
  [(start,end), …]}` argument for Pass 2 of the two-pass plan — when finding an op's
  earliest feasible start, skip windows that overlap a reservation, and reject
  placement that would not finish before the next reservation begins. `reserved=None`
  (Pass 1 and all existing callers) is byte-identical to today.
- `api/auth.py` — accounts (2 roles), `authenticate`, signed-cookie
  `make_token`/`verify_token`, session secret, login rate limiter. Stdlib only.
- `api/main.py` — FastAPI: `/login` `/logout` `/me`, `/upload` (merge, admin),
  `/run`=`/rerun` (plan the book; admin+`persist` saves the config), `/orders`
  (+ `/orders/delete`, `/orders/clear` — admin, **password-confirmed**), `/actuals`
  (+ `/actuals/rollback`), `/items` (`so_nos` for the SO dropdown), `/gantt`,
  `/report`, `/trace/{id}`. `gatekeeper` (session + CSRF) + `security_headers`
  middleware; `require_admin`; `require_password` (re-auth on destructive deletes);
  helper-tab augmentation. **New admin-only endpoints for promise protection:**
  `/orders/commit`, `/orders/urgent`, `/orders/uncommit` (role-gated, non-destructive).
  **Two-pass `_plan`**: orchestrates Pass 1 (protected orders only) via `run_forward`,
  extracts reservations from the schedule, then Pass 2 (`run_forward` on Open orders
  with `reserved=` seeded), and merges both passes' schedules for display.
- `web/` — `login.html` (self-contained login page), `📋 Orders` tab (order book +
  delete, with a **password-confirm modal**), the per-rule tabs (Rule 7 = Capture
  Actuals, with an **SO No dropdown** + per-entry **↺ Rollback** button), and a
  `📊 Gantt` tab; `app.js` renders the trace and hides admin-only controls for the
  user role (no per-rule UI code).

## Resolved design decision (data-confirmed)

**Rule 3 "total process time" = sum of per-process _cycle_ times**, not the
sparse "Total time" column. Only the cycle-time sum reproduces the SO-Remarks
oracle (`61240807-01` highest, `61247047-01` lowest, `61241949-01` > `61247047-01`).

## Workflow notes

- This project was scoped via the brainstorming → spec → plan flow. When making
  substantive changes, update `RULES.md` / the spec first, then the code.
- Git repo on GitHub (`riittiin/anvitech-ppc-engine`); pushing to `main`
  auto-deploys to Render. Commit/push to `main` only when the user asks.
