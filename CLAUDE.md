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
- **Data format:** the user's `Test3.xlsx` (gitignored real data) — the 3 master
  sheets use a clean header-driven layout the loader reads dynamically.

## Stack

- **Backend:** Python + FastAPI. The engine is plain Python; FastAPI is a thin layer.
- **Frontend:** lightweight HTML/JS with **per-rule tabs**.
- **Data source:** the user **uploads** their masters/SO Excel (the **Test3
  format**) via `POST /upload`, read **read-only** via openpyxl. Upload **merges the
  orders into a persistent order book** (keyed by unique SO number) and stores the
  workbook's masters. `load_all(source)` requires a path or BytesIO — there is **no
  bundled default** (pre-upload the app shows empty masters). Tests + the golden
  trace use a **code-generated sample** in the Test3 format (`tests/sample_workbook.py`);
  the real-data file `Test3.xlsx` is gitignored and used only by uploading it.
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
Upload Excel ─▶ MERGE into the Order Book (by SO#)   ┐
Rule 7 actual ─▶ recorded vs SO# (+ optional complete)┘
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
  the current Test3/sample data there are 0 such cases; this is a future safety net.
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
  window (10d), setup time (90min), overlap mode (50%).
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

- `engine/config.py` — tunable params + validation.
- `engine/models.py` — dataclasses; each exposes `as_row()` for the trace tables.
- `engine/loaders.py` — read the uploaded workbook (Test3 format) → typed objects +
  non-blocking report. The 3 master sheets are read **header-driven** (`_locate_table`);
  resource-name normalization (`CNC 4` ≡ `CNC4`) and provisional-machine handling live here.
- `engine/worktime.py` — `WorkClock`: shifts + Thursday/holiday skip for Rule 6.
- `engine/pipeline.py` — `run_rule` (snapshots in/out/config/notes), `run_forward`
  (1→2→3→6), `RuleError`, `to_table`.
- `engine/orderbook.py` — **order-book logic (pure)**: `merge_upload` (add new /
  flag repeat / flag completed / intra-upload dedup, all by SO#), `derive_status`
  (Pending/Running/Complete), `active_so_lines` (remaining qty for planning),
  `order_rows` (dashboard).
- `engine/book_store.py` — durable persistence of the book: active orders + the
  completed archive (hashes by SO#), actuals (append-only list), masters workbook.
  `delete_orders` / `delete_all` for permanent deletes.
- `engine/storage.py` — the store interface (kv/hash/list) + backends:
  `MongoStore` / `UpstashStore` / `LocalStore`; `get_store()` picks by env.
- `engine/gantt.py` — `build_gantt`: Rule 6 schedule → worker-facing Gantt view-model
  (per-order rows, hour axis, time-positioned bars by machine, Pending/Running label).
- `engine/rules/ruleN_*.py` — Rules 1–7, one pure `run(...)` each; 4/5 also expose
  the calc helpers Rule 6 imports. (Rule 7 = `rule7_capture_actuals`. There is no
  `rule8` module — Rule 8 is the unified "Plan" over the order book; see `api._plan`.)
- `api/auth.py` — accounts (2 roles), `authenticate`, signed-cookie
  `make_token`/`verify_token`, session secret, login rate limiter. Stdlib only.
- `api/main.py` — FastAPI: `/login` `/logout` `/me`, `/upload` (merge, admin),
  `/run`=`/rerun` (plan the book; admin+`persist` saves the config), `/orders`
  (+ `/orders/delete`, `/orders/clear` — admin), `/actuals`, `/items`, `/gantt`,
  `/report`, `/trace/{id}`. `gatekeeper` (session + CSRF) + `security_headers`
  middleware; `require_admin`; helper-tab augmentation.
- `web/` — `login.html` (self-contained login page), `📋 Orders` tab (order book +
  delete), the per-rule tabs, and a `📊 Gantt` tab; `app.js` renders the trace and
  hides admin-only controls for the user role (no per-rule UI code).

## Resolved design decision (data-confirmed)

**Rule 3 "total process time" = sum of per-process _cycle_ times**, not the
sparse "Total time" column. Only the cycle-time sum reproduces the SO-Remarks
oracle (`61240807-01` highest, `61247047-01` lowest, `61241949-01` > `61247047-01`).

## Workflow notes

- This project was scoped via the brainstorming → spec → plan flow. When making
  substantive changes, update `RULES.md` / the spec first, then the code.
- Git repo on GitHub (`riittiin/anvitech-ppc-engine`); pushing to `main`
  auto-deploys to Render. Commit/push to `main` only when the user asks.
