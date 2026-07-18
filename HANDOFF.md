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
- **362 tests pass, 1 skipped** (`pytest` on `main`; the skip is Mongo) — **364** on the
  unshipped `scheduled-optimize` branch below. FastAPI backend + vanilla HTML/JS
  frontend, plain Python engine. Python 3 (run as `python3` locally — no `python`
  alias). The self-tuning plan + operator absences + cloud-Optimize work described in
  "Latest session (2026-07-15/16)" below is now **merged and pushed to `main`** (it was
  staged/unshipped when that section was first written). ⚠️ **`main` is behind again:**
  a floor-stability pivot — automatic optimization now runs only **twice a week** via a
  GitHub cron instead of on every change — is built and tested on branch
  `scheduled-optimize`, **NOT pushed to `main`** (see "Latest session (2026-07-18)"
  below for what's staged and why).
- **Login is a two-role app-owned session** (admin / user) — see "Login & roles".
- The engine has **8 business rules** (1–8). (Rule 8 = the Plan over the order book —
  there is no `rule8` module.) The UI now shows only **4 tabs** — **Orders**,
  **Allocate to machines** (Rule 6), **Capture actuals** (Rule 7), **Gantt** — the
  rules 1–5 debug tabs and the Rule 8 tab are hidden (the trace still records them).
- **Order identity is the `(SO No, Item Code)` pair** — an SO number is NOT unique;
  one SO# can carry several item lines, each its own order. (Changed from SO#-only.)
- **Data files are gitignored real data (`Test*.xlsx`, `*_so_list.xlsx`).** ⚠️ **The
  owner swaps these around** — the file named `Test5.xlsx` on disk changes. As of
  2026-07-14: `Test5.xlsx` = **54 orders / 19 operators** (a newer, smaller book);
  `Test6.xlsx` = **65 orders / 21 operators** (this is the book most of this project was
  analysed on — the old "Test5"); `new_so_list.xlsx` = Test5's 54 orders but different
  masters (27 machines). **Always confirm which file/how many orders before quoting a
  number, and match the owner's exact config (plan start, operators) before comparing —
  see the Test5-vs-Test6 lesson in the latest-session block.**
- **Most recent work (2026-07-15 — ✅ ALL SHIPPED & LIVE, deploy-verified):** the owner
  **submitted the code to Anvitech on 2026-07-15** — treat the live site as production
  in real use. Four shipped batches that day (see the "Latest session (2026-07-15)"
  block below): **operator invariants** (nobody >100%, no double-booking, UNSTAFFED
  surfacing), **optimize staleness fingerprint** (`inputs_changed` banner) + book-scoped
  NO_ROUTING report, the **Optimize settings sweep** (one click also auto-tunes the
  Overlap %, never-worse, promise-guarded), and the **full-app consistency audit fixes**
  (shift-wise download names the same person as the Gantt; two-pass rebalance respects
  the committed pass's people).
- Earlier (2026-07-14 — ✅ SHIPPED & LIVE): **Promise recovery** — when a
  disruption makes committed orders slip past their promises, the planner **automatically**
  (no setting/button) re-sequences the committed set in the **background** to protect the
  most promises (`optimizer.optimize(objective="promise_slip")`), replayed on every Plan;
  never worse than date-order; a quiet Orders-tab note. Also live from 2026-07-13/14: the
  **Optimize plan** feature (multi-start sequence search, ~3.5× faster scheduler, Quick/Deep
  + **Stop & keep best**), the **Expedite↔Optimize fix** (Optimize now ignores the Expedite
  tick so it can't be cancelled out), and the **Analytics per-shift operator fix** (util can
  no longer exceed 100%). Full details in the "Latest session" blocks below, the
  `sequence-optimizer-findings` memory, and the two 2026-07-13/14 specs.
  Earlier: **order commitment & promise protection** (three lanes
  Open/Committed/Urgent + **two-pass planning** so new orders can't push
  already-promised dates), plus optimization levers — **Overlap 80%**, **Expedite**
  tick, **Balance operator workload** tick, a **shift-wise schedule download**, and
  the **setup-is-CNC/VMC-only** fix. Defaults keep every plan byte-identical (golden
  unchanged). See the `committed-orders-design` + `expedite-window-and-ontime-findings`
  memories. Earlier shipped work (on `main`): composite (SO#, item) key + two-step capture
  picker, a MongoDB upload fix, OS/outsourcing milestones on the Gantt, a Plan-start-date
  setting, an Expected-completion column, a quantity-only feedback loop, UI cleanup, the
  Analytics tab.

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

### Latest session (2026-07-18) — scheduled optimize: floor-stability pivot — ⚠️ BUILT & TESTED on branch `scheduled-optimize`, **NOT pushed to `main`**

**The owner's decision:** re-sequencing the batch order every time something changes
(a new upload, a commit, an absence, a Settings save) was destroying schedule trust on
the floor — the plan looked different hour to hour even though nothing important had
moved. The owner's rule: facts (punches) update the plan **every day**, but the JOB
ORDER is only re-optimized **twice a week, automatically — Monday and Friday at 11:00
IST (05:30 UTC)**. Feedback is entered ~10:00 on the floor; the re-optimized schedule
is ready before shift 2. With Thursday as the weekly off, Monday and Friday land
exactly 3 working days apart in both directions. Spec:
`docs/superpowers/specs/2026-07-18-scheduled-optimize-design.md` (explicitly
supersedes the event-triggered behavior of `2026-07-15-self-tuning-plan-design.md`
Phase 1 — the auto-apply/incumbent-comparison machinery itself is unchanged, only the
*triggers* changed).

**What changed (2 code tasks, TDD'd via a subagent-driven SDD ledger,
`.superpowers/sdd/progress.md`):**
- **All event triggers removed.** `_bump_book_changed()`, the `_AUTO` pending/chaining
  dict, and `_drain_pending_auto()` are gone from `api/main.py`, along with every call
  site that used to fire one (`/upload`, `/orders/delete`, `/orders/clear`,
  `/orders/commit`, `/orders/uncommit`, `/orders/urgent`, `/absences` POST/DELETE, a
  `persist=True` `/run`). Those endpoints now do their own thing and stop — no contest.
- **New endpoint `POST /optimize/scheduled`** (worker-secret auth, same gatekeeper
  bypass as the other worker endpoints) is the *only* entry point into
  `_try_start_auto()`, which keeps every existing guard (cloud-only, one-at-a-time, the
  book-fingerprint skip when nothing material changed since the last applied plan).
- **New GitHub Actions cron** — `.github/workflows/scheduled-optimize.yml`
  (`cron: "30 5 * * 1,5"` + `workflow_dispatch` for manual testing, wake-tolerant
  10-attempt retry loop against `/optimize/scheduled` since the free Render instance
  may be asleep).
- **`/optimize/done` removed; the Done button rewired.** The Capture Actuals "Done
  entering" button is relabeled **"Done entering — update plan"** and no longer calls
  the (now-gone) endpoint — it just refreshes the plan client-side (`runPlan(false)`)
  and reports the next scheduled optimization day via a new pure/testable
  `nextScheduledOptimize()` helper in `web/app.js` (next Mon/Fri at 11:00 local; today
  counts if it's a scheduled day still before 11:00).
- **Auto-note timestamps switched to IST** (`_ist_now()` = `utcnow() +
  timedelta(hours=5, minutes=30)`; the server itself still runs UTC) — the schedule is
  now a named local clock time the owner reasons about, so the notes should read in
  that clock too. Skip-note copy updated to "will retry on the next scheduled run."

**Status at time of writing:** branch `scheduled-optimize`, 2 commits
(`80c4435` Task 1 — API endpoint in, event triggers out; `ad1d4d4` Task 2 — cron
workflow + Done-button rewire) on top of the spec/plan commits, **NOT pushed to
`main`**. **364 tests pass, 1 skipped** (`python3 -m pytest -q`; up from 362 on `main`
— new/rewritten coverage in `tests/test_auto_optimize.py`); golden trace untouched, no
regen needed (no scheduling logic changed, only *when* it's invoked).

**Local E2E (controller-run, logged-in against a live local server):** a masters
upload and an order commit each fired **zero** contests (previously either would have
started one); `POST /optimize/scheduled` with the correct worker secret **did** start a
contest end-to-end; `POST /optimize/done` came back **405** (the route no longer
exists) — confirms the button-side removal matches the API-side removal.

### Latest session (2026-07-15/16) — cloud Optimize, self-tuning plan, promise-rule pivot, absences — merged & pushed to `main` (was staged on branch `self-tuning-plan` when this section was first written; the event triggers it introduced are superseded by the 2026-07-18 session above)

**Status:** 362 tests pass, 1 skipped; golden untouched throughout (no regen). Built on
branch `self-tuning-plan`, 23 commits, fully TDD'd via a subagent-driven SDD ledger
(`.superpowers/sdd/progress.md`); that branch has since **merged into `main`** (both at
commit `dae6312`) and is presumed live via Render's auto-redeploy-on-push. The
event-triggered auto-optimize behavior it shipped (immediate re-optimize on any
admin mutation) is what the 2026-07-18 session above replaces with a twice-weekly
schedule — read that section first for the current trigger behavior.

**1. Cloud Optimize — the full 2,400-plan contest on free compute (shipped to `main`
earlier the same day, 2026-07-15).** Render's free web instance is 0.1 CPU and can't
run a big search fast; a free **GitHub Actions runner** (2 vCPU) can. Clicking Optimize
now dispatches `optimize.yml` (`workflow_dispatch`) with a job id;
`scripts/cloud_optimize_worker.py` fetches the book snapshot from `GET
/optimize/job/{id}`, runs the fair contest (`engine/optimize_service.run_contest`,
contenders fanned across cores), heartbeats `POST /optimize/progress`, and posts `POST
/optimize/result` — all three authenticated via the `X-Worker-Secret` header
(`OPTIMIZE_WORKER_SECRET`, constant-time compare, bypasses the session gatekeeper). One
button (the old Quick/Deep split now maps to the same budget); local compute is the
automatic fallback on dispatch failure, a worker error, or a timeout
(`OPTIMIZE_CLOUD_TIMEOUT_MIN`, default 20) — the button always works, cloud or not.
Deterministic: a cloud run is byte-identical to a local run of the same contest
(E2E-verified 717/36.79 late-days/makespan on Test6@11-07). **GitHub billing facts** (so
nobody worries about cost): 2,000 free Actions minutes/month on the repo's own account,
a full 2,400-plan contest runs **~6-10 minutes**, and the repo's spending limit is set
to **$0** — it is architecturally impossible to be billed.

**2. The settings-sweep regression saga (same day, three rewrites — read this before
touching `sweep_optimize` again).** The Settings-sweep feature (auto-tune overlap %
alongside the sequence search) went through three contracts in one day:
   - **v1 (shipped, then found broken):** the current overlap got roughly half the
     total budget, challengers split the rest. On the real 65-order book this let a
     *weaker* search dethrone a *stronger* one — Deep returned 753 late-days/"Overlap
     60" where the pre-sweep button had found 713 at Overlap 80. Unequal search depths
     misrank settings; this is why "current setting wins ties" alone isn't a safe rule.
   - **v2 (owner: "the best setting must win"):** every candidate at the SAME full
     depth — 6 overlaps × 400 plans = 2,400 total. Correct, but ~1.5 hr on Render's free
     tier — too slow to use.
   - **v3 (owner: "too slow — ONE option, ≤1,000 plans total"):** measured that a cheap
     100-eval probe-then-deepen picks the wrong winner 2 times in 3, and that overlap
     90/100 lost every contest ever measured on both real books — dropped them from the
     candidate list entirely. Result: 4 contenders × 250 plans (budget split equally,
     current setting probed first, wins ties) ≈ the 2,400-plan contest's quality at 42%
     of the compute — this is `optimizer.sweep_optimize`'s shape today.
   - **Then Cloud Optimize (above) made v2's "too slow" objection moot** — the owner got
     the FULL 2,400-plan fair contest after all, just on GitHub's compute instead of
     Render's. `sweep_optimize`'s 1,000-plan v3 shape is kept as the **local fallback**
     when cloud is unavailable.

**3. Self-tuning plan — the plan re-optimizes itself (this branch, Phase 1 of
`docs/superpowers/specs/2026-07-15-self-tuning-plan-design.md`).** The owner's ask:
"when production changes reality, the plan should re-optimize without anyone
remembering to click." Built as: a book fingerprint
(`optimize_service.book_signature` — order keys, remaining qty + per-process
remaining, lane, promised date, absences); `api._bump_book_changed()` fires
immediately after every **deliberate** admin mutation (upload, order delete/clear,
commit/uncommit/urgent, absence add/remove, Settings save) and starts a background
contest if the book actually changed since the last applied one; a change landing
mid-run is queued (`_AUTO["pending"]`) and triggers a follow-up run when the current
one finishes, so nothing is silently dropped. **Punches never auto-trigger** — the
Capture Actuals tab got a **"Done entering — update & optimize plan"** button (`POST
/optimize/done`, either role) as the only punch-side trigger, so a clerk mid-entry
doesn't burn a contest per row. **Auto contests are cloud-only** — with no
`GITHUB_DISPATCH_TOKEN`/`OPTIMIZE_WORKER_SECRET` configured the self-tuning check is
skipped with a note rather than running a 20-40 minute local search in the background;
the manual Optimize button is unaffected. **Auto-apply is strictly-better-or-nothing**:
the finished contest's best plan is compared against what users currently see (the
applied ranks, or none, replayed on TODAY'S book); it only applies on a strict
improvement, and either way writes a one-line note (`anvitech:auto_note`) surfaced on
`/run` and the Orders tab — *"Plan auto-re-optimized 18:12 — 445 late-days (was 471),
overlap 80 → 70"* or *"Checked 18:12 — current plan still best."* **No user-facing off
switch** (owner decision) — `AUTO_OPTIMIZE=0` exists only for test isolation, never
documented or exposed.

**4. The promise-rule gate FAILED — owner pivoted, discarding the whole feature (this
branch, 2026-07-16).** The plan's Phase 2 was a "promise ceiling" — every order in one
pool, a hard veto rejecting any candidate plan that breaks a committed/urgent promise.
Built (6 commits), then measured against the spec's own shipping gate on both real
books — and it **failed both**: Test5 scored 1364.2 (joint+veto) vs 1051.4 (the old
two-pass) on the combined promise-slip metric; Test6 scored 1843.3 vs 1464.7. The joint
winner's own replay showed `promises_ok=False` on both books. Root cause: **zero-slack
promises collapse the feasible region** — when a promise has no slack left, a hard veto
makes huge swaths of the search space score infinite, and the search can't find its way
out. Escalated to the owner with options (hybrid contender, re-measure, or drop Phase 2
entirely); **the owner dropped it** and redefined the model instead: **lanes
(Open/Committed/Urgent) are pure status labels, not protection.** Commit/Urgent still
snapshot a `promised_date` for **display only** (Orders tab: Promised vs
Current-expected, red flag on drift) — nothing constrains the scheduler or Optimize
anymore. Removal (Phase 2R, 2 commits): the veto (`promise_ceiling_ok`,
`feasible=`), the sweep's per-candidate promise guard, `_plan`'s two-pass branch +
committed-pass reservations, the promise-recovery auto-trigger (`_maybe_start_recovery`,
`anvitech:promise_recovery`, shipped 2026-07-14, now fully removed), the urgent
push-warning preview, and Rule 1/Rule 3's lane-aware special-casing — **every lane
sorts/groups the same way again.** The core regression: a committed+promised book plans
**byte-identical** to the same book all-open
(`tests/test_replay_single_pass.py`, `tests/test_optimize_service.py::
test_lanes_have_zero_scheduling_effect`). **Lesson for next time:** a hard constraint
plus a real-world book with no slack is a bad combination — measure the gate on the
REAL data before building the removal-resistant version, not after.

**5. Operator absences (this branch, Phase 3, shipped alongside the above — unaffected
by the pivot).** The admin can mark a named operator absent for a date range from
Settings; `anvitech:absences` stores `{id, operator, from_date, to_date}`
(day-granularity, ISO in the store, DD-MM-YYYY in the UI). The person becomes
genuinely **unavailable** — a Rule 6 `reserved=` block — in every plan pass and every
Optimize contest candidate (including the cloud payload, which round-trips absences).
`engine/analytics.py` subtracts an absent operator's working days from their available
capacity so utilization stays honest. A masters re-upload that removes an operator with
absence rows on file doesn't break anything — the orphaned rows are ignored by planning
and reported as a non-blocking `ABSENT_OPERATOR_UNKNOWN` row. UI: an always-visible
panel (not nested in the admin-only toolbar, so the user role sees the read-only list;
add/remove controls are CSS- and server-gated admin-only) — browser-verified live both
roles via Chrome MCP.

**Read next:** `CLAUDE.md`'s code-map bullets for `engine/optimizer.py`,
`engine/optimize_service.py`, and `api/main.py` have the exact function/endpoint names;
`RULES.md`'s "Order commitment (lanes)", "Self-tuning plan", "Operator absences" and
"Optimize plan" sections have the business-rule version. The design history lives in
`docs/superpowers/specs/2026-07-15-self-tuning-plan-design.md` (see its SUPERSEDED
Phase-2 block) and `docs/superpowers/specs/2026-07-15-optimize-settings-sweep-design.md`.

### Latest session (2026-07-15) — deploy-day fixes, settings sweep, full-app audit — ✅ ALL SHIPPED & LIVE

The owner **deployed/submitted the code to Anvitech this day**. Four batches, each
built test-first on a branch, merged + pushed on his explicit "push to main", and
**verified on the live site after deploy** (authenticated checks against the real
plan — note: `app.js` is login-gated, so an unauthenticated `curl` reads a 401 page;
that once caused a false "deploy is slow" alarm. Always verify with the session cookie).

1. **Operator invariants (owner-reported: Mahesh 107%/Saif 106.3% + "same Deep run,
   two results").** Root causes and fixes:
   - `_rebalance_operators` could double-book a person under asymmetric machine
     qualification (live: Ankush on CNC1 + VMC3 at once) → repair pass reverts
     reassignments until no person overlaps; timing untouched.
   - `build_shiftwise_timeline` billed a busy person when a shift ran more machines
     than qualified people (`pool = free or eligible`) → such segments become
     `rule6_allocate.UNSTAFFED`; Analytics rolls them up as `headline.unstaffed_hrs`
     with an honest note ("needs more crew on those shifts"); **no operator can ever
     show >100%** (regression: `tests/test_operator_invariants.py`). Schedule-neutral
     (owner rule: reporting fixes must never move the plan — verified byte-identical).
   - The "two different results" mystery = the owner **re-uploaded an edited workbook
     after Apply** (optimizer is deterministic — proven twice-identical). Fix:
     applied optimizations carry `inputs_sig` (sha of masters + plan-shaping config);
     `/run`'s `optimize_meta.inputs_changed` + a banner explain it. Also: NO_ROUTING
     report rows are now **book-scoped** (`_report_for_book`) — the live "5 orders
     without routing" were ghosts from the workbook's own SO sheet.
2. **Optimize settings sweep** (owner ask: "overlap should also be automated").
   `optimizer.sweep_optimize`: same eval budget, current setting keeps ~half (probes
   are noisy — a shallow probe misranked 70>80 on the real book; full-depth said
   80 is better), challengers share a quarter, best probe deepened, **strictly
   better** dethrones. Promise guard vetoes overlaps that worsen committed slip.
   Apply persists the winning overlap into the saved plan config (visible in
   Settings) + `inputs_sig` computed against winning settings. Spec:
   `2026-07-15-optimize-settings-sweep-design.md`.
3. **Full-app consistency audit** (owner ask: "everything integrated everywhere").
   One harness cross-checked every surface on real Test5 with an applied optimization,
   all-open AND two-pass: machine view, analytics, gantt, expected dates, downloads —
   all consistent except two real bugs, both fixed:
   - **Shift-wise download named a different person than the Gantt for 161/341 ops**
     (it re-derived operators with its own fair walk). Now it **follows the plan's
     named operator** wherever that person covers the shift; only impossible segments
     (other shift, overload) are re-assigned or UNSTAFFED. 341/341 match after fix.
   - **Two-pass rebalance was blind to pass-1's people** (one person on two machines
     across passes). `_rebalance_operators(reserved=)` now treats committed-pass
     people as busy (walk + repair). Zero cross-pass double-bookings after fix.
4. **Optimizer-v3 research (ABANDONED mid-way — owner said "forget this").** Key
   preserved facts (memory `optimizer-v3-research`): certified CP-SAT floor — **no
   plan can beat 401 late-days on Test6** (contention-aware; the old 311 was
   alone-orders only); better plans than the 713 champion EXIST beyond the site
   budget (best found: 675 late-days, ranks preserved in the memory dir); at the
   20-min site budget the CURRENT optimizer beat 5 challenger algorithms — do not
   swap it. OR-Tools was installed locally only (NOT in requirements.txt).

### Earlier session (2026-07-14) — analysis, hard limits, and a debugging lesson

Two things, both important context (no code shipped beyond what's listed in the feature
blocks below — this was mostly investigation):

**1. The software is near its optimization ceiling — remaining levers are BUSINESS, not
code.** The owner pushed hard on "optimize more." Measured exhaustively on the 65-order
book (now `Test6.xlsx`): sequencing is **converged** (400 and 2,500 evals both hit
39.74 d / 713 late-days). The absolute floor (each order planned alone, whole shop to
itself) is **311 late-days / 23 orders late even alone** (a runway problem — due dates at/
before the plan start). **12 out-of-the-box directions were tested WITH the optimizer**
(de-consolidation, overlap 90, lower split, late-days-only score, blanket + per-item
machine flexibility incl. a joint sequence+machine-assignment prototype, cheaper setups) —
all land 700–746; the biggest, a joint machine-assignment search, recovered only ~13 days.
**Do NOT build a machine-assignment v2** (measured prize too small). Idle machines/operators
are idle at the WRONG TIMES — utilization is a means, not the goal. The real remaining
levers: realistic due-date quoting (the 311 unavoidable), family-changeover setups if real
(≈2 days makespan; needs owner's floor knowledge), partial deliveries, temporary week-1/2
capacity. Full data in the `sequence-optimizer-findings` memory.

**2. LESSON — always match the owner's EXACT conditions before quoting/comparing numbers.**
The owner reported a "paradox": `Test6` (65 orders) finished in ~40 days but `Test5` (54
orders, fewer) in ~42, and suspected hard-coding. Investigation (verified, after I got it
WRONG twice by using mismatched masters/start): **it is NOT hard-coding** (no baked-in
numbers; the raw scheduler reproduces it; each file computes its own result). The real
cause is entirely the **2 extra helper operators (HP4, HP5) in Test6**. With helpers held
EQUAL (both books 19 ops, same July-14 start, both Deep-400 optimized): **Test5 = 41.4 d,
Test6 = 43.5 d — the smaller book IS faster, as intuition expects.** The owner's live 40-vs-42
was: Test6 ran with 21 ops (→~40), Test5 with 19 ops (→41.4≈42). Also note: **uploading a
workbook MERGES orders (never auto-removes)** — the owner assumed a new upload replaces the
old book; it does not, so a "smaller" upload can leave a larger merged book. And **the live
site had NO optimization applied** at session end (`optimize_meta.active=False`). Takeaway
for next session: reproduce the owner's exact file + config first; don't reach for
explanations before matching conditions.

### Promise recovery (2026-07-14) — ✅ SHIPPED to `main` and LIVE

**Auto committed re-sequencing after a disruption.** When a worker absent / machine down
makes committed orders slip past their promises, strict promised-date order is measurably
sub-optimal. Measured (real Test5, all committed, lost week): re-sequencing recovers
**344 → 242 promise-slip-days, 55 → 44 broken** (~⅓ of the damage); no fast rule captures
it (a search is required). Built as **automatic, no setting/button** (owner: keep the
surface clean for non-technical users; equal weighting, no critical flag): `_plan` Pass 1
detects a slip → kicks a **background** `optimize(objective="promise_slip")` on the
committed set → persists ranks (`anvitech:promise_recovery`) → replays every Plan
(expedite-off) until the committed set/promises change. Safety: seeded with date-order +
keeps best ⇒ **never worse**; no slip → byte-identical (golden untouched). Verified
end-to-end on the real book (disruption → recovery → **17 promises protected**, slip
377 → 315). Quiet `#recovery-note` on Orders (informational only). **323 tests.** 4 commits
on `promise-recovery` (objective / store / _plan wiring / UI+docs). Spec + full findings:
`2026-07-14-promise-recovery-...design.md` and the `sequence-optimizer-findings` memory.
**All merged to `main` and pushed/live.**

### Optimize ↔ Expedite bug (2026-07-13) — ✅ FIXED, SHIPPED & LIVE

**The owner ran Deep on the live site (Expedite tick ON) and got "No improvement found"
— 43.54 d / 1015 late-days unchanged.** Root cause: `expedite_window_min > 0` makes Rule 6
dynamically re-sort ops by slack at schedule time, which **overrides the batch sequence the
optimizer controls** — so every sequence flattens to the same plan (no lever) and an applied
optimization is neutralised the same way. This is why my laptop tests (expedite OFF) showed
big gains but the owner's live plan (expedite ON) didn't move. **Fix:** the optimizer searches
with expedite forced off (`search_config`) and reports the honest baseline = the admin's real
current plan; `_plan` runs ranked orders with expedite forced off (`ranked_config`) so the
ranks take effect. No ranks → config unchanged (golden untouched). Verified on real Test5 +
the exact live config: 978 → 713 late-days, applied plan 713 (was stuck at 978). Regression:
`tests/test_optimize_expedite_interaction.py`. **Lesson: always reproduce with the owner's
actual saved config, not a clean laptop config.**

### Optimize follow-ups (2026-07-13, same second session) — ✅ SHIPPED to `main` and LIVE

After the owner ran Deep on the live free-tier server, three issues surfaced and were
fixed and **shipped/live** (**308 tests pass**, golden unchanged):

1. **Progress froze / Deep too slow.** The progress counter died on a single failed poll
   (Render free tier drops requests) — fixed to reschedule on any failure + resume on
   refresh. Added a **"Stop & keep best"** button (`/optimize/cancel` + `should_cancel`)
   so a slow run can be ended keeping the best-so-far. Deep reduced 1000→400 plans.
2. **Scheduler was ~13× slower on Render than a laptop — because of redundant work, not
   Render.** Profiling showed millions of invariant string/regex parses + datetime rebuilds
   per plan. Memoized them (`loaders.normalize_resource_id`/`parse_resource_candidates`
   lru_cache; Rule 6 `op_lookup` per machine+shift; `WorkClock._windows_for_day` per-day
   cache; skip the midnight look-back for non-crossing clocks). **~3.5× faster** (640→183
   ms/plan laptop), **byte-identical results** (verified: Quick 39.75/792, Deep 39.75/778
   unchanged; golden intact). On Render: Quick ~22→~8 min, Deep ~1 hr→~19 min.
3. **Optimizer was stuck in a mediocre local optimum** (39.75 d / 778 late-days; the owner
   correctly recalled an earlier exploratory run hitting 38.7). Root cause: a single
   hill-climb trajectory from one seed. Fixed with **multi-start** (SPT/ATC + random
   restarts, keep global best) → **39.7 d / 713 late-days / 44 late** — better than both the
   old shipped plan (778) and the exploratory 752. **Trade-off finding (owner-confirmed
   priority = fewest late deliveries):** the shortest-makespan plans (38.6 d) carry *more*
   late-days (861, worse than today), so the score rightly favours delivery gaps over the
   very shortest finish. Frontier + numbers in the `sequence-optimizer-findings` memory.

### Latest session (2026-07-13, second session) — ✅ SHIPPED to `main` and LIVE

Owner's goals, verbatim: shrink SO-delivery-vs-expected gaps and the ~43-day makespan,
**software only**. Deep measurement on Test5 found the binding constraint is the
**greedy single-pass scheduler**, not capacity (VMC2, the busiest machine, holds only
~34 calendar days of work; 723 order-days of queueing, 74% waiting for CNC/VMC; config
levers don't stack; full machine-pool flexibility made lateness WORSE). One plan
evaluates in <1 s → search instead of trusting one pass. Details + all measured
negatives: the `sequence-optimizer-findings` memory.

**1. Optimize plan (sequence search) — the feature.** Admin **Optimize** button next to
Plan: Quick (150 plans tried) / Deep (1,000), live progress, before/after table,
**Apply/Discard**. `engine/optimizer.py` (pure, deterministic: eval-count budget +
fixed seed; seeds rule3/SPT/ATC/shuffles; insertion/swap/block hill-climb + kicks;
score = late-days + 10×makespan). Apply persists a rank per (SO#, item)
(`anvitech:plan_priority`); `run_forward(priority_rank=)` replays it — ranked batches
reorder among their own slots, **unranked new orders keep their Rule-3 slot**, and with
committed/urgent present only the **open pass** is searched (promises can never move).
Banner "Optimized plan active" + "N orders added since — re-optimize" + admin Remove.
**Measured on the real book via the running app:** 42.5 d / 53 late / 1,026 late-days →
**39.75 d / 47 late / 792 late-days** in 90 s locally (Render free tier ≈ 3–6 min for
Quick); replayed plan recounts to exactly the reported metrics. No optimization applied
→ byte-identical plans (golden untouched). Spec:
`docs/superpowers/specs/2026-07-13-optimize-plan-sequence-search-design.md`.

**2. Analytics operator hours are now billed per shift** (`engine/analytics.py` via
`build_shiftwise_timeline`). A multi-day op on a two-shift machine was billing ALL its
hours to the single named (day) operator — Rupesh Pawar showed an impossible **120%**.
Now each shift's hours go to the person actually manning it: Rupesh 120→79%, the real
near-bottleneck is the **second shift (Saif/Mahesh ~88%)**, and **no operator or
machine can exceed 100%** (owner-requested guarantee, regression-tested).

### Earlier session (2026-07-13) — ✅ ALL SHIPPED to `main` and LIVE

Two big things shipped this session, both **live** on https://anvitech-ppc.onrender.com.
**267 tests pass; golden trace byte-identical without regen** (every new lever/feature
is opt-in or a no-op by default). Built test-first; the commitment feature went through
full subagent-driven development (10 tasks, each code-reviewed + a final whole-branch
review). See memories `committed-orders-design` and `expedite-window-and-ontime-findings`.

**1. Order commitment lanes & two-pass promise protection (the flagship).** Solves the
owner's rolling-order-book fear: new weekly uploads used to re-plan the whole book and
push already-promised delivery dates later. Now orders have a **commitment lane**
(`open` default | `committed` | `urgent`) + a locked `promised_date`.
- **Two-pass Plan** (`api._plan`): Pass 1 schedules protected (committed+urgent) orders
  in isolation via the unchanged Rules 1–6, locking their dates as if Open orders don't
  exist; Pass 2 schedules Open orders into the free machine/operator intervals left over
  (Rule 6's new `reserved=` arg), so an Open order can **never** push a committed order.
- Admin **Commits** an order (snapshots its current expected completion as the promise)
  or marks it **Urgent** (promised = its SO delivery date, slotted by that date, with a
  **warning** if it would push another committed order past its promise); **Uncommit**
  reverts. Orders tab shows lane badge, Promised vs Current expected (red slip flag).
- Endpoints `/orders/commit|urgent|uncommit`; fields persisted in `book_store`
  (`set_commitment`/`clear_commitment`). All-open book → plan is byte-identical to today.
- **Gotcha fixed in final review:** the two passes each numbered batches `B001..`, so
  their ids collided when merged and committed orders vanished off the Gantt — pass 2's
  ids are now prefixed `O-` (`api._plan`). Regression test: `tests/test_two_pass_gantt.py`.

**2. Optimization levers + a deep root-cause analysis of late deliveries.** Three opt-in
tick marks/settings shipped, plus a shift-wise download:
- **Overlap 80%** (Settings, was 50) — the one clean free win: makespan 44→43 days AND
  6 fewer late orders on Test5. Overlap is a *model of how the shop works* (pipelining),
  not a quality dial — the live app always runs overlap mode.
- **Expedite urgent orders** tick (`config.expedite_window_min`, default 0=off): Rule 6
  least-slack tie-break within a window. Pulls the worst-stuck orders in (worst
  48.6→38.7d) but can push one on-time order late — an A/B toggle, off by default.
- **Balance operator workload** tick (`config.balance_operator_load`, default off):
  schedule-neutral post-process — reassigns *who* runs each op to the least-loaded
  qualified same-shift person. Peak operator 106%→97%, **makespan/lateness unchanged**.
- **Download shift-wise schedule** button (Allocate tab): splits each multi-shift op
  into per-shift segments with the real operator each shift (`build_shiftwise_timeline`).
- **Findings (see the `expedite-window-and-ontime-findings` memory):** Test5 lateness is
  a **runway** problem (from a Jul-11 start, ~29/57 orders are structurally impossible —
  overdue or routing+OS too long), NOT a master-imbalance problem. Machines run ~40%;
  the true throughput constraint is the **single-shift finishing/inspection crew** during
  the end-drain, then VMC — NOT CNC (owner's father thought CNC; the data disagreed —
  freeing CNC operators changed nothing). Sequencing/WIP/lot-streaming all proved they
  can't beat the ~43-day floor (it's resource+precedence bound). Manual-per-piece×qty is
  now a *small* lever (the setup-CNC/VMC-only fix already removed most inflation).

### Previous (2026-07-11) — ✅ now SHIPPED (was uncommitted; committed + pushed this session)

Deep root-cause analysis + two changes, both **now live**:

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
python3 -m pytest -q                          # 339 pass + 1 skipped (Mongo)
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
stations + CNC/VMC +30% time). **Setup time is CNC/VMC-only** (shipped). **Order
commitment lanes + two-pass promise protection** (shipped/live). **Optimization toggles:**
Overlap 80% default in Settings, **Expedite urgent orders** tick, **Balance operator
workload** tick, **shift-wise schedule download** (all shipped/live).

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
