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
- **157 tests pass** (`pytest`). FastAPI backend + vanilla HTML/JS frontend, plain
  Python engine. Python 3 (run as `python3` locally — there is no `python` alias).
- **Login is a two-role app-owned session** (admin / user) — see "Login & roles".
- The engine has **8 business rules** (1–8). A 9th "parallel machine" rule was
  removed earlier; rules are a clean 1–8. (Rule 8 = the Plan over the order book —
  there is no `rule8` module.)
- **Most recent work** is the "Latest session" block below: smart parallel split,
  DISPATCH/OS pass-through, Capture-Actuals rollback, delete password re-auth, SO No
  dropdown, Gantt operator, and a restructured operator/machine master (data).

## How to pick up where the last session left off (behavioral context)

This project was built across long, iterative sessions. **Work the way they did** —
the patterns below are what kept it on track. This matters as much as the architecture.

### Who the user is
- The **owner/operator of Anvitech** (a precision-machining job shop) — a domain
  expert on the shop floor, **not a software engineer**. He talks in business terms:
  sales orders, SO numbers, delivery dates, shifts, downtime, machines, processes.
- He wants a **real, free, deployed** tool his workers + planner actually use. Cost
  must stay **$0** (Render free + MongoDB Atlas free).
- **Decisive and iterative.** He surfaces edge cases and "loopholes" himself and
  wants to **understand the *why*** before changing things. He asks pointed questions
  ("why is setup not in downtime?", "both machines busy → what happens?") and expects
  a precise, honest answer before any code.
- He **does the external-service signups himself** (Render, Atlas) and expects you to
  do **all the code** and give him **only his manual steps** — one at a time, exact
  clicks. Be **patient and precise**; if he can't find something, point at the exact
  button and ask what he sees.

### How to behave (the working norms that worked)
1. **Verify before you claim anything works.** Run `pytest`; for UI changes, actually
   drive a real browser (the gstack `/browse` skill was used heavily — start a local
   `uvicorn` on a spare port, auth via an `Authorization: Basic` header because the
   page is login-gated, and assert DOM state + check the console). Hit the live URL
   (`401` = up). **Evidence before assertions** — never say "it works" without it.
2. **Plain language, business framing.** No jargon. Use **tables** and **short numbered
   steps**. Explain shop-floor impact, not implementation, unless he asks.
3. **Be honest — including about your own mistakes and the system's limits.** State
   tradeoffs/caveats up front; correct yourself when wrong. Never paper over a
   limitation to sound finished (e.g. the downtime-attribution approximation is
   documented openly, not hidden).
4. **Decide-and-state; ask only when it matters.** Make sensible defaults and say so.
   Use `AskUserQuestion` for the **one** decision that genuinely changes the outcome
   (e.g. "downtime delays the whole machine vs only the logged order" was worth
   asking; most things weren't).
5. **Plan big features first.** For substantive work: study the data + code, then use
   **plan mode** (a Plan subagent to validate the design and catch edge cases),
   present the plan, get approval, *then* implement. The preferred-machine feature was
   built exactly this way — and the Plan agent caught a real cross-file coupling bug.
6. **Protect the live site.** It's deployed and holds real data. Build every change on
   a **branch**; merge to `main` and push **only when the user explicitly says "push
   to main"** (push = deploy). He approves each push individually.
7. **Keep the UI minimal and the rules pure.** He values an uncluttered interface.
   Rules are pure functions; the order book is the only stateful layer; "Plan" reuses
   Rules 1–6, never re-implements them.
8. **Keep tests + the golden snapshot + RULES.md/CLAUDE.md in lockstep** with every change.

### Communication shape
- Lead with the result/answer, then the detail. Use ✅ / ⚠️ and tables.
- End with a clear next step or **one** focused question — not a wall of options.
- After committing to a branch, end with a short status table and ask whether to push.

## Architecture (what it is now)

The **8 business rules** (see [`RULES.md`](RULES.md)) turn sales orders into a
machine-by-machine schedule + Gantt, re-planning as actual production comes in. The
engine is a **persistent, shared Order Book** (keyed by unique SO number) sitting
**above the unchanged pure Rules 1–6**:

```
Upload Excel ─▶ MERGE into the Order Book (by SO#)              ┐
Rule 7 actual ─▶ recorded vs SO# (good qty, downtime, complete?)┘
                              │
   Order Book ──▶ active SO-lines (remaining qty) ──▶ R1 consolidate ─▶ R2 sort
                                                       ─▶ R3 smart priority (slack)
                                                       ─▶ R6 allocate (R4 setup,
                                                          R5 overlap; downtime loop-back)
                                                       ─▶ schedule + Gantt
```

- **Upload** an Excel → orders **merge** into the book (new SO# → Pending; a
  known/completed/intra-file-duplicate SO# → flagged, never double-counted).
- **Plan** (one button; unifies the old "Run" + "Rerun MRP") → schedules every
  *active* order at its **remaining** qty (ordered − good produced) through Rules 1–6.
- **Rule 7** daily entry records production; the first actual flips an order
  Pending → Running; ticking **"mark complete"** on an entry → Complete (archived).
- **Delete** (single / multiple / all) permanently removes from the database.
- **Login:** HTTP Basic Auth gates the whole app. **Persistence:** `engine/storage.py`
  picks **MongoDB > Upstash > local file** by env var.

### The 8 rules (renumbered this session — IMPORTANT)
| # | Rule | File |
|---|---|---|
| 1 | Consolidate SO lines (same item, 10-day window) | `rule1_consolidate.py` |
| 2 | Sort by SO delivery date | `rule2_sort_by_date.py` |
| 3 | Smart priority = least **slack** | `rule3_tiebreak_process_time.py` |
| 4 | Setup time (cycle×qty + 90 min) — calc, consumed by R6 | `rule4_setup_time.py` |
| 5 | Overlap mode — calc, consumed by R6 | `rule5_overlap_mode.py` |
| 6 | Allocate to machines (non-delay scheduler) | `rule6_allocate.py` |
| 7 | Capture daily actuals | `rule7_capture_actuals.py` |
| 8 | Plan / Re-run MRP loop | **no module** — it's `api._plan` over the order book |

There is **no `rule8` module** and **no parallel-machine rule** anymore. Trace keys
are `rule1`…`rule8`. The web tabs read **1–8** with no gap.

## Login & roles (current auth — read this)

The whole app sits behind an **app-owned session login** with **two roles**
(replaced the old HTTP Basic-Auth popup). Code: `api/auth.py` (accounts, signed
cookie, rate limiter) + `gatekeeper`/`security_headers` middleware in `api/main.py`
+ `web/login.html`. Spec: `docs/superpowers/specs/2026-06-25-two-role-login-design.md`.

- **Roles:** **Admin** = full control. **User** = read-only view of every tab,
  PLUS download the Rule 6 allocation CSV and submit Capture Actuals (including
  "mark complete"). Admin-only: config bar, Plan button, Upload, Delete/Delete-all.
- **Enforced server-side** (`require_admin` → 403 on `/upload`, `/orders/delete`,
  `/orders/clear`), not just hidden in the UI. The frontend hides admin controls
  for the user role via a `body.role-user` CSS class + `/me`.
- **Credentials are baked into `api/auth.py`** (owner's choice): admin
  `anvitech` / `1930rail`, user `anvitech_user` / `anvitech12345678`. Each is
  **overridable by env vars** (`ADMIN_USERNAME`/`ADMIN_PASSWORD`,
  `USER_USERNAME`/`USER_PASSWORD`; legacy `APP_USERNAME`/`APP_PASSWORD` still
  override the admin) — so you CAN now read the live creds from the code unless the
  user set overrides on Render.
- **Session:** stateless HMAC-SHA256-signed cookie (`anvitech_session`, HttpOnly,
  SameSite=Lax, Secure on HTTPS, 7-day expiry). Secret = `SESSION_SECRET` env, else
  a random secret generated + persisted in the store (`anvitech:session_secret`).
- **Hardening:** username-keyed login rate limit (5/15min → 429), CSRF Origin/
  Referer check on unsafe methods, CSP + `X-Frame-Options`/`nosniff`/HSTS headers,
  interactive docs disabled (`/docs`,`/openapi.json` → 404), 10 MB upload cap.
- **Plan consistency:** the admin's Plan click (`/run` with `persist:true`) saves
  the config to `anvitech:plan_config`; users (and admin auto-load) plan with that
  saved config, so everyone sees the planner's schedule.
- **Tests:** `tests/test_auth_unit.py` (token/creds/rate-limit) + `tests/test_auth_api.py`
  (login flow, role 403s, CSRF, headers, docs-off, upload cap). `tests/test_api.py`
  now logs in via a `client` fixture (cookie), not a Basic-Auth header.

## What changed this session (most recent work — read these commits)

All shipped to `main`. Newest first.

### Latest session — data restructure + scheduling polish (all live)

Code commits `2bce847…ed9d33c`. The first three are **data edits to the user's real
`Test3.xlsx`** (gitignored — NOT in git); the rest are code on `main`.

- **DATA — machine master standardized** (in `Test3.xlsx`): helper machines renamed to
  codes (`MD1` deburring, `MP1` punching, `MPK1` packing, `MW1` washing, `MA1` assembly),
  the person-named ones (Anturam/Sanjay/Murali) gone; `Q INSPECTOR` deleted and folded
  into the inspection pool `MI1/MI2/MI3`; current machines = CNC 1,3,4,5,6,7 · VMC 1,2,3 ·
  BS2 · MD1/MP1/MPK1/MW1/MA1 · MI1/MI2/MI3 · CMM · M LATHE · DTC2.
- **DATA — operator & shift master has 3 roles** (Name · **Role** · Preferred Machines ·
  Shift): **Operators** = the named people (run CNC/VMC/BS2/CMM/M LATHE/DTC2, both shifts
  where two-shift); **Helpers** = `HP1–HP4` (run the ₹80 machines, first shift);
  **Inspectors** = `MI1/MI2/MI3` (each its own station, first shift). The dummies A/B/C/D
  are gone. Loader reads it header-driven (Shift column must stay immediately right of
  Preferred Machines; Role is ignored by the loader).
- **DATA — focus items**: 7 active-order item codes are highlighted **green** in the
  Item's process Master and filled in: `61247047-01, 61241949-01, 61242130-01,
  61241989-01, 61249291-01, 61240807-01, 9611416360`. They plan cleanly (every step has an
  operator). The **rest of the process-master items are intentionally ignored for now.**
- **Smart parallel split** (`split_parallel`, UI toggle ON) — when a step lists alternative
  machines (`CNC7/CNC3`, `MI1/MI2/MI3`, …) the qty is split to **finish the step as early
  as possible**: each machine gets the load it can complete by a common target time, from
  when IT frees up (handles a busy-but-soon-free machine and unequal speeds), and it only
  splits when that beats one machine. `rule6._allocate_op`. **Generic** — any alternative
  cell, not just CNC.
- **DISPATCH / OS pass-through** (`rule6._is_passthrough`) — a step with **no machine AND
  no cycle time** is a non-production pass-through (`DISPATCH` = "consider it done"; an
  outside-service `OS` step like `BANDSAW OS`): no machine, no operator, no time, the batch
  skips it. A blank machine **with** a cycle time still fails loud ("needs machine"), so
  forgotten data isn't silently dropped. (Cycle-time × qty is the only time basis — the
  "Total time" column is never used; alternatives + split apply everywhere.)
- **Capture Actuals — per-entry rollback** — every saved entry has a **↺ Rollback** button
  (first column). It deletes that one mis-punched entry and returns the order to normal; if
  the entry had marked the order **complete**, the order is **un-archived** back to active
  (unless another entry still marks it complete). `Actual.id` (uuid, legacy backfilled),
  `book_store.delete_actual`/`uncomplete_order`, `POST /actuals/rollback`. Both roles.
- **Delete password re-auth** — *Delete selected* and *Delete ALL data* now pop a modal
  requiring the admin to **re-enter their password**, verified **server-side**
  (`require_password` → 403 on mismatch). Prevents an accidental click wiping the book.
- **Capture Actuals — SO No is a dropdown** (from `/items` `so_nos` = active+completed
  orders); picking one auto-fills the (read-only) Item Code, Item Name, Process list.
- **Gantt shows the operator** — each bar carries the operator (hover tooltip:
  process · machine · 👤 operator · time · qty). Split halves are separate bars; DISPATCH/OS
  never appear. Operator visibility is now complete: Rule 6 tab + CSV + Gantt.

### Earlier session — operator logic + Test3 migration + two-role login

000. **Operator & shift logic + full master ingestion** (branch `operator-logic`) —
     ingests Available Hrs/Day + per-operator Shift + shift times. Rule 6 now gives
     each machine its own working window: Available Hrs/Day ≥ 12 → two-shift
     (08:00–05:00), else single-shift **09:00–18:00**; and an activity runs only
     during a shift that has a **qualified operator** (specialty matched by machine
     no OR type; manual needs a first-shift op). Uncovered machines → "needs operator"
     report (not fatal); unmatched specialties (e.g. `CNC2`) reported. Behind
     `config.apply_operator_logic` (engine **OFF** → golden/tests byte-identical; web
     UI **ON**). New: `engine/operator_coverage.py`, `WorkClock` interval refactor
     (`NoWorkingWindow`), Rule 6 per-machine clocks + producer-clock handoff + blocked
     skip, coverage table on the Rule 6 tab, UI checkbox. Verified on real Test3
     (CNC1/3/4 both shifts, CNC5/6 first, CNC7 second, 16 machines need operators).
     Spec: `docs/superpowers/specs/2026-06-26-operator-logic-design.md`. Deferred:
     Model B (operator as one-at-a-time scarce resource) + applying operator **leaves**.

00. **Moved to Test3 format + deleted Test2.xlsx** (branch `drop-test2`) — the loader
    now reads the 3 reorganized master sheets **header-driven** (`_locate_table` in
    `loaders.py`; SO list + process master unchanged). `load_all` requires a source
    (no bundled default); pre-upload masters are empty. `Test2.xlsx` is **deleted** —
    tests + the golden trace use a **code-generated sample** in Test3 format
    (`tests/sample_workbook.py`); golden regenerated. Verified end-to-end by uploading
    the real `Test3.xlsx` (9 orders, 85 items, 27 machines, 71 scheduled ops). Loader
    reads but does **not yet act on** the new columns (Available Hrs/Day, shift times,
    leaves) — that's the next decision. `.gitignore` widened to `Test3*.xlsx`.

0. **Two-role login (admin/user) + security hardening** (branch `two-role-login`) —
   see "Login & roles" above. Replaced the Basic-Auth popup with a real login page
   + signed session cookie; user role is view-only + download + capture-actuals;
   admin-only enforced server-side. 26 new auth tests (92 → 118).

1. **Preferred / alternative machine selection** (`646bb03`) — a routing's "Suggested
   M/c" cell may list alternatives like `CNC3/CNC6`. The engine now schedules the
   **earliest-free** of the allowed machines (ties → first-listed = preferred), so work
   load-balances. New pure helper `loaders.parse_resource_candidates`; `rule6` picks
   among candidates; `orderbook.machine_lost_minutes` resolves the **same** preferred
   candidate (cross-file contract — a Plan agent caught this). Schedule shows a note
   `chose CNC3 of CNC3/CNC6`. **Note:** only appears for orders of items whose recipe
   uses an alternative — the generated sample's SAMP-A item exercises `CNC1/CNC2`, so
   you need an order for an alternative-machine item to see the note.
2. **SO No column** on the Rule 6 schedule + machine-wise view + CSV (`922be82`) —
   `ScheduleEntry.so_refs`.
3. **Day-level Gantt** (`365886d`) — removed the 0–23 hour ruler; one column per day,
   non-working days shaded, date-only tooltips, "Zoom (day width)". `ganttDayWidth` in
   `app.js`.
4. **Three UI features** (`8d2293c`): (a) typing **SO No auto-fills Item Code** (via
   `/items` `so_to_item`); (b) **all dates display DD-MM-YYYY** (datetimes
   DD-MM-YYYY HH:MM) via `models.fmt_date`/`fmt_datetime` + `pipeline._cell` — sorts
   that relied on ISO ordering now sort on the date object; **persistence + config
   round-trips stay ISO** internally; the native `<input type=date>` picker still shows
   the browser locale; (c) **Download schedule (CSV)** + **machine-wise view** buttons
   on the Rule 6 tab.
5. **Plan tab lists every active order** (`227473a`) — including fully-produced-but-not-
   completed ones (Remaining 0, "In this plan = no — fully produced, mark complete"), so
   its Running count matches the Orders tab.
6. **Performance** (`458f5fa`, `36bcf5e`) — `storage.get_store()` now **caches the
   backend per env** so a single `MongoClient` is reused (was rebuilt ~5× per `/run`);
   `api._current_masters` caches the parsed workbook in-process (invalidated on upload).
   A **keep-warm GitHub Action** (`.github/workflows/keep-warm.yml`) pings the live URL
   every 12 min during work hours (UTC 03:00–14:59 ≈ 08:30–20:29 IST) to dodge Render's
   15-min idle sleep (~1800 Actions min/mo, under the 2000 free tier).
7. **Daily-entry form UX** (`eec8576`, `4ee91ad`) — blank safe defaults + today's date;
   **clears after save**; **warns on exact-duplicate** save; **instant save** (no heavy
   re-plan — it updates the Rule 7 tab from the `/actuals` response + a light `/orders`
   refresh; click Plan to refresh the schedule); **mark-complete jumps to the Orders
   tab** and shows the order Complete; "Saving…" button state.
8. **Downtime loop-back** (`4b6fab8`) — recorded downtime + actual-setup overrun feed
   back into Rule 6 as **per-machine availability delays** (the machine's whole queue
   slips). `orderbook.machine_lost_minutes` → `run_forward(machine_lost_min=…)` →
   `rule6` seeds `machine_free`. Config flag **`apply_downtime_to_plan`** (engine
   default **off** for test/golden stability; **UI checkbox default ON**). Rule 6 tab
   shows a "Downtime fed back" table + an "unattributed" table for typo'd processes.
   Cumulative across re-plans. Rule 3 priority intentionally unchanged.
9. **Rule renumber + parallel removal** (`4aec727`, `a58bd90`) — deleted the parallel-
   machine rule entirely; renumbered to 1–8.
10. **Rule 5 overlap fix** (`758ccaa`) — overlap % now applies to **cutting time only**
    (the 90-min setup is excluded; the next machine's setup runs in parallel); **no-cutting
    steps don't overlap** (successor waits full). The Rule 5 tab shows the **real
    per-handoff effect** on the actual plan (`rule5_overlap_mode.build_overlap_view`),
    not a static demo.

## Run / test / deploy

```bash
pip install -r requirements.txt
python3 -m pytest -q                          # 157 tests
REGEN_GOLDEN=1 python3 -m pytest -k golden    # ONLY after an intentional logic change
python3 -m uvicorn api.main:app --reload      # http://127.0.0.1:8000  (frontend at /)
```
Locally, with no store env vars set, data goes to `data/store/` (gitignored). **Local**
login is the baked admin `anvitech` / `1930rail` (or user `anvitech_user` /
`anvitech12345678`). **Deploy:** push to `main`.

**Browser-verify a UI change locally** (login is now a cookie session, not Basic Auth):
```bash
rm -rf data/store
STORE_DIR=data/store nohup python3 -m uvicorn api.main:app --port 8011 >/tmp/uv.log 2>&1 &
# log in (saves the session cookie), then upload as admin:
curl -s -c /tmp/ck.txt -X POST http://127.0.0.1:8011/login -d "username=anvitech&password=1930rail" >/dev/null
curl -s -b /tmp/ck.txt -F "file=@Test3.xlsx" http://127.0.0.1:8011/upload >/dev/null  # your real data (Test3 format)
# drive gstack /browse by signing in through the form (cookie persists in the daemon):
#   $B goto http://127.0.0.1:8011/login
#   $B snapshot -i ; $B fill @e1 anvitech ; $B fill @e2 1930rail ; $B click @e3
#   (use anvitech_user / anvitech12345678 to verify the restricted user view)
```

## Git, GitHub & deploy workflow

- `origin` = GitHub **`riittiin/anvitech-ppc-engine`** (private). **`gh` CLI is installed
  and authed** as `riittiin`; git author identity is set — commit/push/PR with no setup.
- **Deploy = push to `main`.** Render watches `main` and auto-redeploys from
  `render.yaml` (`uvicorn api.main:app`). **No separate deploy step.**
- **Commit message convention:** end every message with
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Branch for every change; merge + push to `main` ONLY when the user says so.** The
  standard flow used all session: `git checkout -b <feature>` → implement test-first →
  `pytest` → commit → report → on "push to main": `git checkout main && git merge
  --ff-only <feature> && git push origin main` → `curl` the live URL (expect 401).
- **Verify a deploy:** Render → service **Events** tab shows the build; or
  `curl -s -o /dev/null -w '%{http_code}\n' https://anvitech-ppc.onrender.com/` →
  **401** = up; **502/503** = crashed (check Render **Logs**).

## Live deployment specifics (no secrets here)

- **Render** service `anvitech-ppc` — free web service; **sleeps after ~15 min idle**
  (first hit ~30–60s to wake; data unaffected). Keep-warm Action mitigates work hours.
- **MongoDB Atlas** free **M0 (512 MB)** — db `anvitech`, collections `hash` (orders),
  `list` (actuals), `kv` (masters). Atlas Network Access includes `0.0.0.0/0`.
- **Env vars on Render:** `MONGODB_URI` (storage), plus **optional** auth overrides
  (`ADMIN_USERNAME`/`ADMIN_PASSWORD`, `USER_USERNAME`/`USER_PASSWORD`, `SESSION_SECRET`).
  Auth works with none set (credentials are baked into `api/auth.py`).
- **Live login** is now the baked credentials (admin `anvitech` / `1930rail`, user
  `anvitech_user` / `anvitech12345678`) **unless** the user set the override env vars
  on Render. If a prior `APP_USERNAME`/`APP_PASSWORD` is still set on Render it
  overrides the admin login — check the dashboard. To verify live behavior, sign in
  through the login page (or guide the user).

## Domain rules to honor (confirmed with the user)

- **SO number is the unique order key.** Repeats flagged, never double-counted; an
  order is **never auto-deleted** for being absent from an upload.
- **Status is derived:** Pending (no actuals) → Running (≥1 actual) → Complete
  (**explicit only — via the Rule 7 "mark complete" tick; the engine NEVER
  auto-completes**, not even at remaining ≤ 0).
- **Rule 3 priority = least slack** = (working-time-until-due) − (work-needed); no
  window by default; equal dates ⇒ "more work first." Metric + window configurable.
  **"total process time" = sum of per-process _cycle_ times** (data-confirmed).
- **Rule 5 overlap** = % of the previous op's **cutting time only** (setup excluded);
  no-cutting steps don't overlap.
- **Rule 6 = non-delay scheduler** — a machine never idles while an op is ready. For an
  **alternative-machine** process it picks the **earliest-free** allowed machine, and with
  **`split_parallel`** on it **splits the qty** across them to finish the step soonest
  (load-balanced by free-time + speed; only when faster). Same logic for ANY alternative
  cell (CNC, inspection, …), not just CNC.
- **DISPATCH / OS = pass-through.** A step with no machine AND no cycle time is skipped
  (no machine/operator/time) — `DISPATCH` is "consider it done"; `OS` = outsourced.
- **Time basis = cycle time × qty** (+ 90-min setup). The Process "Total time" column is
  **never** used.
- **Operator/shift logic** (toggle) — each machine runs only shifts that have a qualified
  operator; manual/₹80 + inspection are single-shift (09:00–18:00), CNC/VMC two-shift.
- **Downtime + setup overrun loop back** into Rule 6 as per-machine delays (toggle).
- **Masters** are latest-wins on upload, kept if a file omits them.
- **Dates display DD-MM-YYYY** everywhere; storage/config stay ISO internally.

## Done vs deferred

**Done:** order book + lifecycle; upload-merge + dedup; completion via Rule 7; unified
Plan; day-level Gantt (now with operator on bars); SO No column + CSV download; two-role
login + hardening; Render + MongoDB deploy; permanent delete (now password-confirmed);
append-safe storage; Rule 5 cutting-only overlap; downtime loop-back; preferred/alternative
machine selection **+ smart parallel split**; **DISPATCH/OS pass-through**; **operator &
shift logic** (per-machine windows + coverage gate, operator on the schedule);
**Capture-Actuals rollback**; **SO No dropdown**; DD-MM-YYYY dates; daily-entry UX; perf
(connection reuse + masters cache) + keep-warm. **Data:** Test3 format, standardized
machine master + 3-role operator master, 7 focus items filled in.

**Deferred (explicitly, per the user):**
- **Remaining process-master items** — only the **7 green focus items** are filled in /
  verified; the other ~80 items in the Item's process Master are intentionally left for
  later. This is the most likely **next task**.
- **Outside-service (OS) lead time** — OS steps are currently skipped as zero in-house time
  (correct for scheduling machines), but a vendor turnaround/lead time isn't modelled.
- Applying **revisions** to existing orders (changed qty/date) — currently flagged only.
- Explicit **cancel** action (orders leave only via complete or delete).
- **Actual `Actual` "which machine ran" field** — today downtime on an alternative-
  machine process is attributed to the **preferred** candidate (a documented
  approximation). Adding a machine field would make it exact.
- **Plan clock advancing to "today"** — the plan still starts from a fixed
  `config.plan_start_date` (2025-03-01), so downtime loop-back models "lost so far",
  not a real calendar. Revisit if he wants the plan to roll forward with the date.
- Suggested-vs-**allotted** machine override semantics (allotted = locked) — kept
  suggested-first; not touched.

## Gotchas / operational notes

- **Live creds are custom** (see above) — the single biggest "why can't I verify live"
  gotcha.
- **There is no bundled data file anymore.** `Test2.xlsx` was deleted; the app reads
  the **Test3 format**. Tests + the golden trace use a **code-generated sample**
  (`tests/sample_workbook.py`, in Test3 format). **`Test3.xlsx` is the user's
  real-data file** — **gitignored** (`Test3*.xlsx`), never commit it. Pre-upload the
  app shows empty masters ("please upload").
- **Golden trace** (`tests/golden_trace.json`) snapshots rule1/2/3/6 **output** for
  the generated sample. Date-format / rule-output changes require
  `REGEN_GOLDEN=1 pytest -k golden`; then eyeball the diff (no merged ids like `CNC1CNC2`).
- **`apply_downtime_to_plan`** defaults **off in the engine** (so golden/tests stay
  stable) but the **UI checkbox defaults ON** — an intentional asymmetry. Tests that
  construct `Config()` directly get the off behavior.
- **`get_store()` is cached** — one `MongoClient` per process. Don't reintroduce
  per-call construction (it was the main latency bug).
- **`requirements.txt` includes `pymongo[srv]`** (for `mongodb+srv://`).
- Local shell is **zsh**: put `-u user:pass` literally on each `curl`; quote `grep`
  globs (`--include='*.py'`) or zsh errors; a `grep -c` that finds 0 exits non-zero and
  breaks `&&` chains (not a failure).
- Static assets serve `Cache-Control: no-cache`; a normal refresh picks up new JS/CSS,
  but tell the user to **hard-refresh** after a deploy if they see stale UI.
- The Render-deploy lag (~1–3 min) is the usual reason "I don't see my change yet."

## Where to look

- [`CLAUDE.md`](CLAUDE.md) — design principles, data flow, **code map**, commands. Read first.
- [`RULES.md`](RULES.md) — the rules (source of truth for logic), now 1–8.
- `engine/` — `loaders.py` (Excel→objects, `parse_resource_candidates`, provisional
  machines), `models.py` (dataclasses + `fmt_date`/`fmt_datetime` + `as_row`),
  `pipeline.py` (`run_forward` 1→2→3→6, trace), `rules/ruleN_*.py`, `orderbook.py`
  (pure book logic + `machine_lost_minutes`), `book_store.py` / `storage.py`
  (persistence), `worktime.py` (shifts/holidays), `gantt.py`, `config.py`.
- `api/main.py` — FastAPI: `/upload`, `/run`=`/rerun`, `/orders` (+delete/clear),
  `/actuals`, `/items`, `/gantt`, `/report`, `/trace/{id}`; login + no-cache middleware;
  per-rule trace augmentation (`_augment_helpers`); `_plan` is the unified Plan (Rule 8).
- `web/` — `index.html`, `app.js` (all UI logic; per-rule tabs render the trace),
  `style.css`. `.github/workflows/keep-warm.yml`.
- `docs/superpowers/specs/` — original design docs (historical; some predate the
  order-book + renumber, so trust code/RULES.md over them).

## Making changes safely

1. For substantive logic changes, update `RULES.md` (and CLAUDE.md if structural) **first**, then code.
2. Keep rules **pure**; the order book (`orderbook.py` / `book_store.py`) is the only stateful layer; reuse Rules 1–6 for planning.
3. **Test-first** (the repo is TDD-structured: one `test_ruleN.py` per rule). Run
   `python3 -m pytest`; regenerate the golden trace only for **intentional** logic changes.
4. For UI changes, **drive a real browser** to verify before claiming done.
5. **Branch for everything; commit/push to `main` only when the user says "push"** — it auto-deploys.
