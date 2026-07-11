# HANDOFF — Anvitech PPC Engine

Orientation for a **new Claude Code session** taking over this project. The goal is
to pick up **exactly where the last session left off — same standards, same way of
working with the user, same grasp of the domain**, not just the same codebase. Read
this whole file (especially **"How to pick up where the last session left off"**),
then [`CLAUDE.md`](CLAUDE.md) (design principles, data flow, code map) and
[`RULES.md`](RULES.md) (the rule logic).

---

## TL;DR

A **Production Planning & Control (PPC) engine** for Anvitech, a precision-machining
job shop. **Built, tested, deployed live, and actively iterated.**

- **Live app:** https://anvitech-ppc.onrender.com (login-gated).
- **Host:** Render (free web service). **Database:** MongoDB Atlas (free M0, 512 MB).
- **Repo:** GitHub `riittiin/anvitech-ppc-engine` (private). Push to `main` →
  Render auto-redeploys (no separate deploy step).
- **232 tests pass** (`pytest`). FastAPI backend + vanilla HTML/JS frontend, plain
  Python engine. Python 3 (run as `python3` locally — there is no `python` alias).
- **Login is a two-role app-owned session** (admin / user) — see "Login & roles".
- The engine has **8 business rules** (1–8). (Rule 8 = the Plan over the order book —
  there is no `rule8` module.) The UI now shows only **4 tabs** — **Orders**,
  **Allocate to machines** (Rule 6), **Capture actuals** (Rule 7), **Gantt** — the
  rules 1–5 debug tabs and the Rule 8 tab are hidden (the trace still records them).
- **Order identity is the `(SO No, Item Code)` pair** — an SO number is NOT unique;
  one SO# can carry several item lines, each its own order. (Changed from SO#-only.)
- **Data file is now `Test5.xlsx`** (supersedes `Test4.xlsx` → `Test3.xlsx`) —
  gitignored real data. Test5 adds parallel manual stations (`MW1/MW2/MW3`, `MD1/MD2`,
  `MPK1/MPK2/MPK3`) and a **+30% cycle/total time on every CNC/VMC step**.
- **Most recent work (2026-07-11 — UNCOMMITTED, NOT on the live site yet):** a
  root-cause analysis of late deliveries + two changes — (1) **setup time is now
  CNC/VMC-only** (a code change; manual steps no longer carry the 90-min setup) and
  (2) the **Test5 +30% CNC/VMC time bump** (data). See "Latest session" below. Earlier
  shipped work (on `main`): composite (SO#, item) key + two-step capture picker, a
  MongoDB upload fix, OS/outsourcing milestones on the Gantt, a Plan-start-date
  setting, an Expected-completion column, a quantity-only feedback loop, UI cleanup.

## How to pick up where the last session left off (behavioral context)

This project was built across long, iterative sessions. **Work the way they did** —
the patterns below are what kept it on track. This matters as much as the architecture.

### Who the user is
- The **owner/operator of Anvitech** (a precision-machining job shop) — a domain
  expert on the shop floor, **not a software engineer**. He talks in business terms:
  sales orders, SO numbers, item codes, delivery dates, shifts, downtime, machines,
  processes.
- He wants a **real, free, deployed** tool his workers + planner actually use. Cost
  must stay **$0** (Render free + MongoDB Atlas free).
- **Decisive and iterative.** He surfaces edge cases and "loopholes" himself and
  wants to **understand the *why*** before changing things. He asks pointed questions
  ("both machines busy → what happens?", "why did Test4 error but not Test3?") and
  expects a precise, honest answer before any code.
- He **does the external-service signups himself** (Render, Atlas) and expects you to
  do **all the code** and give him **only his manual steps** — one at a time, exact
  clicks. Be **patient and precise**.

### How to behave (the working norms that worked)
1. **Verify before you claim anything works.** Run `pytest`; for UI changes, actually
   drive a real browser (start a local `uvicorn` on a spare port; log in through the
   form or by POSTing `/login`, since the app is cookie-session gated; assert DOM
   state). Hit the live URL (`401`/`200` on `/login` = up). For data-shaped bugs,
   reproduce against the **real** file and the **real backend** — the MongoDB upload
   bug (below) passed every LocalStore test and only failed on MongoDB, because a raw
   `.` in an item code broke a dotted update path. **Evidence before assertions.**
2. **Plain language, business framing.** No jargon. Use **tables** and **short numbered
   steps**. Explain shop-floor impact, not implementation, unless he asks.
3. **Be honest — including about your own mistakes and the system's limits.** State
   tradeoffs/caveats up front; correct yourself when wrong. (When a claim was verified
   with the wrong settings, or a fix wasn't tested on the real backend, own it plainly.)
4. **Decide-and-state; ask only when it matters.** Make sensible defaults and say so.
   Use `AskUserQuestion` for the **one** decision that genuinely changes the outcome
   (e.g. "wipe & re-upload vs migrate" for the key change; "two-step SO→Item picker").
5. **Plan big features first.** For substantive work: study the data + code, then
   present the plan, get approval, *then* implement. The order-book and operator-logic
   features were built this way.
6. **Protect the live site.** It's deployed and holds real data. Build every change on
   a **branch**; merge to `main` and push **only when the user explicitly says "push
   to main"** (push = deploy). He approves each push individually. **This is a hard
   rule — never push to `main` without an explicit "push to main".**
7. **Keep the UI minimal and the rules pure.** He values an uncluttered interface (he
   had the rules 1–5 and Rule 8 tabs hidden). Rules are pure functions; the order book
   is the only stateful layer; "Plan" reuses Rules 1–6, never re-implements them.
8. **Keep tests + the golden snapshot + RULES.md/CLAUDE.md in lockstep** with every change.

### Communication shape
- Lead with the result/answer, then the detail. Use ✅ / ⚠️ and tables.
- End with a clear next step or **one** focused question — not a wall of options.
- After committing to a branch, end with a short status table and ask whether to push.

## Architecture (what it is now)

The **8 business rules** (see [`RULES.md`](RULES.md)) turn sales orders into a
machine-by-machine schedule + Gantt, re-planning as actual production comes in. The
engine is a **persistent, shared Order Book** (keyed by the unique **(SO No, Item
Code)** pair) sitting **above the unchanged pure Rules 1–6**:

```
Upload Excel ─▶ MERGE into the Order Book (by (SO#, item code))       ┐
Rule 7 actual ─▶ recorded vs (SO#, item code) (good qty, complete?)   ┘
                              │
   Order Book ──▶ active SO-lines (remaining qty) ──▶ R1 consolidate ─▶ R2 sort
   (orders · actuals · masters)                       ─▶ R3 smart priority (slack)
                                                       ─▶ R6 allocate (R4 setup,
                                                          R5 overlap)
                                                       ─▶ schedule + Gantt
```

- **Upload** an Excel → orders **merge** into the book (new (SO#, item) → Pending; a
  known/completed/intra-file-duplicate pair → flagged, never double-counted). **Same
  SO# with a different item = two separate orders.**
- **Plan** (one button; unifies the old "Run" + "Rerun MRP") → schedules every
  *active* order at its **remaining** qty (ordered − finished good) through Rules 1–6.
  The plan clock **advances past days already punched** (starts from the next working
  day after the latest actual), and its base date is the **Settings → Plan start date**.
- **Rule 7** daily entry (a **two-step SO → Item picker**) records production; the
  first actual flips an order Pending → Running; ticking **"mark complete"** on an
  entry → Complete (archived). The feedback loop is **quantity-only** (recorded
  downtime/setup times are for the record and never move the schedule).
- **Delete** (single / multiple / all) permanently removes from the database, by the
  (SO#, item) pair (admin re-enters their password to confirm).
- **Login:** a two-role app-owned **session cookie** gates the whole app. **Persistence:**
  `engine/storage.py` picks **MongoDB > Upstash > local file** by env var.

### The 8 rules
| # | Rule | File | Tab visible? |
|---|---|---|---|
| 1 | Consolidate SO lines (same item, 10-day window) | `rule1_consolidate.py` | hidden |
| 2 | Sort by SO delivery date | `rule2_sort_by_date.py` | hidden |
| 3 | Smart priority = least **slack** | `rule3_tiebreak_process_time.py` | hidden |
| 4 | Setup time (cycle×qty + 90 min, **CNC/VMC steps only**) — calc, consumed by R6 | `rule4_setup_time.py` | hidden |
| 5 | Overlap mode — calc, consumed by R6 | `rule5_overlap_mode.py` | hidden |
| 6 | Allocate to machines (non-delay scheduler) | `rule6_allocate.py` | **Allocate to machines** |
| 7 | Capture daily actuals | `rule7_capture_actuals.py` | **Capture actuals** |
| 8 | Plan / Re-run MRP loop | **no module** — `api._plan` over the order book | hidden |

Trace keys are `rule1`…`rule8`; the hidden tabs still exist in the trace, they're
just not rendered. Plus an **Orders** tab (the order book) and a **Gantt** tab.

## Login & roles (current auth)

The whole app sits behind an **app-owned session login** with **two roles**. Code:
`api/auth.py` (accounts, signed cookie, rate limiter) + `gatekeeper`/`security_headers`
middleware in `api/main.py` + `web/login.html`. Spec:
`docs/superpowers/specs/2026-06-25-two-role-login-design.md`.

- **Roles:** **Admin** = full control. **User** = read-only view of every tab, PLUS
  download the Rule 6 allocation CSV and submit Capture Actuals (incl. "mark complete").
  Admin-only: config/Settings, Plan button, Upload, Delete/Delete-all.
- **Enforced server-side** (`require_admin` → 403 on `/upload`, `/orders/delete`,
  `/orders/clear`), not just hidden in the UI (`body.role-user` + `/me`).
- **Credentials are baked into `api/auth.py`** (owner's choice): admin
  `anvitech` / `1930rail`, user `anvitech_user` / `anvitech12345678`. Each is
  **overridable by env vars** (`ADMIN_USERNAME`/`ADMIN_PASSWORD`,
  `USER_USERNAME`/`USER_PASSWORD`; legacy `APP_USERNAME`/`APP_PASSWORD` still
  override the admin).
- **Session:** stateless HMAC-SHA256-signed cookie (`anvitech_session`, HttpOnly,
  SameSite=Lax, Secure on HTTPS, 7-day expiry). Secret = `SESSION_SECRET` env, else
  a random secret persisted in the store.
- **Hardening:** username-keyed login rate limit (5/15min → 429), CSRF Origin/Referer
  check, CSP + security headers, interactive docs disabled, 10 MB upload cap.
- **Plan consistency:** the admin's Plan click (`/run` with `persist:true`) saves the
  config to `anvitech:plan_config`; users (and admin auto-load) plan with that config.

## What changed most recently (read these, newest first)

### Latest session (2026-07-11) — ⚠️ UNCOMMITTED, NOT on `main`/live yet

This session did a deep root-cause analysis of "orders finishing past their delivery
date" and made two changes. **All of it is local/uncommitted** (git status: modified
`engine/rules/rule6_allocate.py`, `rule4_setup_time.py`, `tests/test_rule6.py`,
`tests/test_parallel_split.py`, `tests/golden_trace.json`, `CLAUDE.md`, `RULES.md`;
`Test5.xlsx` is gitignored). Decide with the user whether to commit + push (push =
deploy). **The live site still runs the old code and has whatever Test4 data was last
uploaded.**

1. **Setup time is now CNC/VMC-only** (`rule6_allocate._is_setup_machine`). The 90-min
   setup models CNC/VMC **programming** time, so manual/finishing steps (washing,
   deburring, packing, inspection, drilling/chamfer DTC2, bandsaw, manual lathe) now
   get **0 setup**. Detection: machine id `CNC*`/`VMC*`, or the master's CNC-lathe /
   Vertical-Machining-center type. Golden trace regenerated; 232 tests pass.
2. **Test5 data edits** (in the gitignored `Test5.xlsx`, via openpyxl, backed up first):
   - Expanded manual stations in the Item's process Master: `MW1`→`MW1/MW2/MW3`,
     `MD1`→`MD1/MD2`, `MPK1`→`MPK1/MPK2/MPK3` (mirrors how inspection is `MI1/MI2/MI3`).
   - **+30% cycle time AND total time on every CNC/VMC machining step** (566 cells).

**Root-cause findings (important context — see the `late-deliveries-root-cause`
memory):**
- Machines are **not** the constraint (avg utilization ~28%); orders spend ~76% of
  their life **waiting**. The model *appeared* to blame 3 manual operators, but that was
  an **artifact of inflated manual step times** — manual steps are computed **per-piece
  × full qty** (e.g. deburring 1000 pcs = one long block) plus the phantom 90-min setup.
- The owner's father (floor knowledge) says **manual is fast and never the bottleneck;
  CNC/VMC sequencing is**. Confirmed: modeling manual as fast collapses lateness ~95%,
  and the residual bottleneck is then **CNC/VMC (70% of the late orders' wait)**.
- **All CNC/VMC steps are hard-pinned to one machine** in the Allotted column (0% list
  alternatives); the Suggested column *does* list alternatives (`CNC3/CNC6`) but only
  on smaller items → load imbalance (CNC4 1.7×, VMC2 2×).
- **Parallel split isn't firing** because its two data gates never line up: 51/65 orders
  are ≤400 pcs (below `split_min_qty=401`), and the 14 orders that ARE big enough list a
  **single** machine per CNC/VMC step (nothing to split across).

**Still open (discussed, not done):** (a) manual steps modeled per-piece × qty — the
owner's father implies they're **bulk/batch** ops (fast, ~fixed per batch); if so the
engine should stop multiplying manual steps by qty (needs owner confirmation: per-piece
vs per-batch). (b) CNC hard-pinning — put real alternatives in Allotted (or lower
`split_min_qty`) so big runs can split/balance. (c) local `plan_start_date` is stale
(`2025-03-01`) → everything plans a year early; the real lateness only shows when the
start is near the July-2026 due cluster.

### Earlier, shipped to `main` (newest first)

The **(SO#, item) key**, the **MongoDB fix**, and **OS inclusion** are the three most
recent shipped and most important.

- **OS / outsourcing shown as milestones** (`520601a`) — a step with **no machine AND
  no cycle time** (OS/outsourcing like `BANDSAW OS`, `DISPATCH`, and other off-machine
  steps) was previously *silently skipped*. It is now scheduled as a **visible
  zero-duration milestone** on an **"OS / Outsourced"** lane (name has an `OS` token)
  or **"Off-machine"** lane, consuming no machine/operator/time. `rule6._is_offmachine`
  + `_offmachine_lane`. Golden trace unaffected (the sample workbook has no such steps).
- **MongoDB upload fix** (`a66f631`) — the (SO#, item) hash field
  `"<so>\x1f<item>"` was interpolated into a dotted Mongo update path (`$set
  {"h.<field>": v}`); real item codes contain `.` (e.g. `61243661-01..`), which Mongo
  reads as a nested path and rejects ("empty field name"), so uploads died **on the
  live MongoDB store only** (LocalStore was fine — that's why local tests passed).
  `MongoStore` now percent-encodes field names (`_enc_field`/`_dec_field`). Regression
  test: `tests/test_storage_mongo_fields.py`.
- **Order identity = (SO No, Item Code)** (`1dfee6a`) — an SO number is not unique; one
  SO# can carry several item lines, each its own order. Rekeyed the entire order book
  on the pair: `Order`/`Actual`/`SOLine.key`; `merge_upload` dedupes by the pair;
  good-by-order / orders-with-actuals / per-process progress keyed by the pair;
  `book_store` uses a composite hash field; `complete`/`delete`/`rollback` target one
  line; `/orders/delete` takes pairs; `/items` returns `so_to_items` for the two-step
  picker; Capture Actuals is now **pick SO No, then pick that SO's item**.
- **Parallel split only over 400** (`b1b3d50`) — batches ≤ 400 go entirely to the
  single least-queued alternative machine; only batches **over 400** split across
  alternatives (`config.split_min_qty` default 401).
- **Settings → Plan start date** (`e394e59`) — the date scheduling begins from is now a
  Settings field (persisted with the plan config), instead of a hardcoded constant.
- **Gantt: Expected completion column** (`35200a4`) — each order row shows when its
  **last** process finishes; moves as the feedback loop updates.
- **UI cleanup** (`029b88b`, `1e44860`, `14fa6b8`, `967e44f`, `56f98da`) — hid the
  rules 1–5 and Rule 8 tabs, redesigned the header + collapsible Settings bar, removed
  decorative emojis (professional look), dropped rule-number prefixes from tab labels.
- **Pre-prod audit** (`af00ffa`, `76dad8b`) — fixed 6 verified bugs (rejection
  accounting, a non-finite hang, input validation, a `_RUNS` memory leak, a ragged-row
  loader crash, a completed-order clock issue) and made a **blank machine + a real
  cycle time fail loud** (never scheduled on a phantom station).
- **Feedback loop rework** (`d7050be`, `1f13de0`, `bc44c28`, `240aaae`, `b92cb06`,
  `96bfc37`) — count **finished goods at the gate** (DISPATCH/last step) and re-plan
  each process at its **own per-process remaining** ("continue from reality");
  **quantity-only** (recorded times never move the schedule); the **plan clock advances
  past days already worked**; Capture Actuals shows + allows rollback for **only the
  latest punched day** (list stays small); **operators are a real one-at-a-time
  resource** assigned to the earliest-free qualified person (load-balanced), and a punch
  **auto re-plans** on save.
- **Rule 3 column rename** (`47865f6`) — "Total Process Time" → "Cycle time per piece".
- **Dates** (`32054ab`) — strict day-first parser + DD-MM-YYYY echo on the date picker.

Older work (order book, two-role login, operator/shift logic, preferred/alternative
machine + smart split, day-level Gantt, Rule 5 cutting-only overlap, perf + keep-warm,
Test2→Test3 migration) is in the git history and the design specs.

## Run / test / deploy

```bash
pip install -r requirements.txt
python3 -m pytest -q                          # 232 tests
REGEN_GOLDEN=1 python3 -m pytest -k golden    # ONLY after an intentional logic change
python3 -m uvicorn api.main:app --reload      # http://127.0.0.1:8000  (frontend at /)
```
Locally, with no store env vars set, data goes to `data/store/` (gitignored). **Local**
login is the baked admin `anvitech` / `1930rail` (or user `anvitech_user` /
`anvitech12345678`). **Deploy:** push to `main`.

**Browser-verify a UI change locally** (login is a cookie session):
```bash
rm -rf data/store
STORE_DIR=data/store nohup python3 -m uvicorn api.main:app --port 8011 >/tmp/uv.log 2>&1 &
# log in (saves the session cookie), then upload as admin:
curl -s -c /tmp/ck.txt -X POST http://127.0.0.1:8011/login -d "username=anvitech&password=1930rail" >/dev/null
curl -s -b /tmp/ck.txt -F "file=@Test5.xlsx" http://127.0.0.1:8011/upload >/dev/null   # your real data (Test5)
# then drive a browser (log in through /login; the cookie persists), or POST /run and read the JSON.
```
The scratchpad `STORE_DIR=/tmp/...` trick gives a truly fresh store for a clean repro.

## Git, GitHub & deploy workflow

- `origin` = GitHub **`riittiin/anvitech-ppc-engine`** (private). **`gh` CLI is installed
  and authed**; git author identity is set — commit/push/PR with no setup.
- **Deploy = push to `main`.** Render watches `main` and auto-redeploys from
  `render.yaml` (`uvicorn api.main:app`). **No separate deploy step.**
- **Commit message convention:** end every message with
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.
- **Branch for every change; merge + push to `main` ONLY when the user says "push to
  main".** Standard flow: `git checkout -b <feature>` → implement test-first →
  `pytest` → commit → report → on "push to main": `git checkout main && git merge
  --ff-only <feature> && git push origin main` → `curl` the live URL.
- **Verify a deploy:** Render → **Events** tab; or
  `curl -s -o /dev/null -w '%{http_code}\n' https://anvitech-ppc.onrender.com/login`
  → **200** = up.

## Live deployment specifics (no secrets here)

- **Render** service `anvitech-ppc` — free web service; **sleeps after ~15 min idle**
  (first hit ~30–60s to wake; data unaffected). A keep-warm GitHub Action pings it
  during work hours.
- **MongoDB Atlas** free **M0 (512 MB)** — db `anvitech`, collections `hash` (orders),
  `list` (actuals), `kv` (masters). Atlas Network Access includes `0.0.0.0/0`.
- **Env vars on Render:** `MONGODB_URI` (storage), plus **optional** auth overrides.
  Auth works with none set (credentials are baked into `api/auth.py`).
- After deploy, the recommended path for the (SO#, item) key change is to **clear the
  order book and re-upload fresh** (no migration was written — decided with the user).

## Domain rules to honor (confirmed with the user)

- **Order key = the `(SO No, Item Code)` pair.** An SO number is NOT unique — the same
  SO# with a different item is a **separate order**. Repeats of the same pair are
  flagged, never double-counted; an order is **never auto-deleted** for being absent
  from an upload.
- **Status is derived:** Pending (no actuals) → Running (≥1 actual) → Complete
  (**explicit only — via the Rule 7 "mark complete" tick; the engine NEVER
  auto-completes**, not even at remaining ≤ 0).
- **Feedback loop is quantity-only.** Only produced/rejected qty re-plans; recorded
  downtime/setup times are for the record and **never** move the schedule.
- **Finished goods count at the gate** (DISPATCH, or the last step if none); WIP at an
  earlier process does NOT reduce the order. Each process re-plans at its **own**
  per-process remaining ("continue from reality").
- **Plan clock** starts from **Settings → Plan start date** and **advances past days
  already punched** (next working day after the latest actual).
- **Rule 3 priority = least slack** = (working-time-until-due) − (work-needed); equal
  dates ⇒ "more work first". **"total process time" = sum of per-process _cycle_ times.**
- **Rule 5 overlap** = % of the previous op's **cutting time only** (setup excluded).
- **Rule 6 = non-delay scheduler.** For an **alternative-machine** process it picks the
  **earliest-free** allowed machine; batches **over 400** **split** across alternatives
  to finish soonest (load-balanced by free-time + speed), smaller batches go entirely
  to the single least-queued one.
- **Off-machine steps (DISPATCH / OS) are shown as zero-duration milestones**, never
  ignored — "OS / Outsourced" or "Off-machine" lane; no machine/operator/time consumed.
- **Time basis = cycle time × qty** (+ 90-min setup **on CNC/VMC steps only**; manual/
  finishing steps get no setup). The Process "Total time" column is **never** used.
  (NOTE / open item: manual steps are still multiplied by full qty — likely should be
  batch-based; confirm with the owner.)
- **Operator/shift logic** (toggle) — each machine runs only shifts with a qualified
  operator; operators are a real one-at-a-time resource (earliest-free, load-balanced).
- **Masters** are latest-wins on upload, kept if a file omits them.
- **Dates display DD-MM-YYYY** everywhere; storage/config stay ISO internally.

## Done vs deferred

**Done:** order book + lifecycle keyed by **(SO#, item)**; upload-merge + dedup;
completion via Rule 7; unified Plan; day-level Gantt (operator on bars + **Expected
completion** column); two-role login + hardening; Render + MongoDB deploy (**hash-field
encoding fix** for dotted item codes); password-confirmed permanent delete;
quantity-only feedback loop (gate-counted finished goods, per-process re-plan,
self-advancing plan clock, latest-day-only capture list); operators as a real resource;
preferred/alternative machine + **split only over 400**; **OS/off-machine milestones**;
**Settings Plan-start-date**; DD-MM-YYYY dates; UI cleanup (4 tabs, no emojis, redesigned
header). **Data:** now **`Test5.xlsx`** (same header-driven format; parallel manual
stations + CNC/VMC +30% time). **Setup time is CNC/VMC-only** (2026-07-11, uncommitted).

**Deferred (explicitly, per the user):**
- **Manual step time model (per-piece vs per-batch)** — manual/finishing steps are
  computed cycle × full-qty; the owner's floor knowledge implies they're bulk/batch
  ops. Confirm and, if batch, stop multiplying manual steps by qty. **The single
  biggest remaining lever on delivery lateness.**
- **CNC/VMC hard-pinning** — every machining step names one machine in Allotted (no
  alternatives), causing load imbalance and blocking parallel split on big runs. Put
  the real alternatives in Allotted, or lower `split_min_qty`.
- **Remaining process-master items** — historically only the focus items were fully
  filled in; the rest of the Item's process Master is completed as the user edits the
  Excel. A missing machine is a *provisional* placeholder (non-blocking); a missing
  routing skips just that order (`NO_ROUTING`).
- **Outside-service (OS) lead time** — OS steps now **appear** as zero-duration
  milestones, but a real vendor turnaround/lead time is still not modelled.
- Applying **revisions** to existing orders (changed qty/date) — currently flagged only.
- Explicit **cancel** action (orders leave only via complete or delete).
- An **`Actual` "which machine ran"** field (would make alternative-machine attribution
  exact).
- Suggested-vs-**allotted** machine override semantics (allotted = locked) — kept
  suggested-first; not touched.

## Gotchas / operational notes

- **Order key is the (SO#, item) pair.** Anything that keys by SO# alone is a bug —
  `Order.key`/`Actual.key` give the pair. The MongoDB hash field is the composite
  `"<so>\x1f<item>"`, **percent-encoded** in `MongoStore` (a raw `.`/`$` in an item
  code breaks the dotted update path — that was the live upload bug).
- **`Test5.xlsx` is the user's real-data file** — **gitignored** (`Test*.xlsx`), never
  commit it. Same header names as Test4/Test3 (header-driven loader unaffected). There
  are timestamped backups from this session's edits (`Test5_backup_*.xlsx`, also
  gitignored). `61243661-01..` in the SO list looks like a data typo (trailing dots) —
  the app handles it, but worth flagging to the user.
- **There is no bundled data file.** Tests + the golden trace use a **code-generated
  sample** (`tests/sample_workbook.py`, Test4 format). Pre-upload the app shows empty
  masters ("please upload").
- **Golden trace** (`tests/golden_trace.json`) snapshots rule1/2/3/6 **output** for the
  generated sample. Rule-output changes require `REGEN_GOLDEN=1 pytest -k golden`; then
  eyeball the diff. The sample has **no off-machine steps**, so the OS-milestone change
  left the golden untouched — a real-data plan will show them.
- **Verify data-shaped fixes on the real backend.** LocalStore (local/dev) and
  MongoStore (prod) differ; the dotted-field upload bug only reproduced on MongoDB.
- **`get_store()` is cached** — one `MongoClient` per process. Don't reintroduce
  per-call construction (it was the main latency bug). `requirements.txt` has
  `pymongo[srv]`.
- Local shell is **zsh**: quote `grep` globs; a `grep -c` that finds 0 exits non-zero
  and breaks `&&` chains (not a failure). Use `python3`, not `python`.
- Static assets serve `Cache-Control: no-cache`; tell the user to **hard-refresh**
  after a deploy if they see stale UI. Render-deploy lag (~1–3 min) is the usual reason
  "I don't see my change yet."

## Where to look

- [`CLAUDE.md`](CLAUDE.md) — design principles, data flow, **code map**, commands. Read first.
- [`RULES.md`](RULES.md) — the rules (source of truth for logic), 1–8.
- `engine/` — `loaders.py` (Excel→objects, header-driven, provisional machines),
  `models.py` (dataclasses + `.key` + `fmt_date`/`fmt_datetime` + `as_row`),
  `pipeline.py` (`run_forward` 1→2→3→6, trace), `rules/ruleN_*.py`, `orderbook.py`
  (pure book logic, keyed by the (SO#, item) pair), `book_store.py` / `storage.py`
  (persistence; MongoStore field-encoding), `worktime.py`, `gantt.py`, `config.py`.
- `api/main.py` — FastAPI: `/upload`, `/run`=`/rerun`, `/orders` (+delete/clear),
  `/actuals` (+rollback), `/items` (two-step picker data), `/gantt`, `/report`,
  `/trace/{id}`; login + security middleware; `_augment_helpers`; `_plan` = Rule 8.
- `web/` — `login.html`, `index.html`/`app.js`/`style.css` (per-rule tabs render the
  trace; only 4 tabs shown). `.github/workflows/keep-warm.yml`.
- `docs/superpowers/specs/` — design docs (some historical; trust code + RULES.md +
  CLAUDE.md over them where they diverge). The order-book spec is updated for the
  (SO#, item) key.

## Making changes safely

1. For substantive logic changes, update `RULES.md` (and CLAUDE.md if structural) **first**, then code.
2. Keep rules **pure**; the order book (`orderbook.py` / `book_store.py`) is the only stateful layer; reuse Rules 1–6 for planning.
3. **Test-first** (the repo is TDD-structured: one `test_ruleN.py` per rule). Run
   `python3 -m pytest`; regenerate the golden trace only for **intentional** logic changes.
4. For UI changes, **drive a real browser** to verify before claiming done. For
   data/persistence changes, reproduce on the **real backend** (MongoDB), not just LocalStore.
5. **Branch for everything; commit/push to `main` only when the user says "push to main"** — it auto-deploys.
