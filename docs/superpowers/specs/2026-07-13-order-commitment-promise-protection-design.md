# Design — Order Commitment & Promise Protection

**Date:** 2026-07-13
**Status:** Approved in brainstorming; pending spec review → implementation plan.

## Problem

Anvitech's order book is **rolling**: a batch of sales orders arrives, is planned, and
the owner (the planner's father) reads the **expected completion dates** off the app
and **promises those dates to clients**. The next week, a fresh batch arrives (sometimes
a single 1,000-piece order), is merged into the same book, and the next **Plan re-runs
the whole book from scratch, re-prioritising every order together by slack**. New,
larger, or earlier-due orders can therefore **jump ahead of already-promised orders and
push their expected dates later** — silently breaking commitments the owner already made
to customers.

The engine does exactly this today (verified in `api._plan` → `pipeline.run_forward`,
which plans *all* active orders with `rule3` slack priority; there is no notion of a
promised or protected order).

## Goal

Let the owner **lock a promised date** on an order so that **newly-arriving (unpromised)
orders can never push it later**. Keep the mechanism **simple** — no due-date ranking
gymnastics — and add a lightweight **"urgent"** path for an order that must hit a
specific delivery date.

## The model — three priority lanes

Every order is in exactly one lane. The scheduler serves them top to bottom:

| Lane | Meaning | Where it schedules |
|---|---|---|
| 🔴 **Urgent** | Must hit a specific delivery date | Slotted into the protected group **by its delivery date** |
| 🔵 **Committed** | Promised to a client — protected | Behind existing promises, in the order committed |
| ⚪ **Open** | New / not yet promised (default) | After everything protected — leftover capacity |

**Protected group = Committed + Urgent.** Open orders always schedule *after* every
protected order and can **never** change a protected order's schedule.

### Ordering rules (kept deliberately simple)

- **Open never touches protected.** New uploads land Open; they only consume capacity
  left over after the protected group, so their on-screen expected date already reflects
  sitting behind all promises.
- **Within the protected group, orders are ordered by their `promised_date`**
  (earliest first). This single key produces both behaviours we want:
  - A **normal Commit** snapshots the order's *current on-screen expected completion* as
    its promised date. Because a later commit is scheduled behind earlier promises, its
    snapshot is naturally later → it sorts to the back. **First promised, first served**,
    with no explicit seniority bookkeeping.
  - An **Urgent** order's promised date is its **required delivery date** (its SO
    delivery date). Being earlier, it sorts *ahead of* protected orders promised later
    than it, and *behind* those promised earlier — "just high enough to make its date."

There is **no ranking of the protected group by raw customer due date**; the only sort
key is the locked `promised_date`. (Open orders keep the existing Rule 3 slack order
among themselves — irrelevant to promises, since they're all last.)

## The two owner actions

On the **Orders** tab, each order row gains actions:

1. **Commit** — locks the order's *current expected completion* as its `promised_date`
   and moves it to the **Committed** lane (behind existing promises). Nothing else
   recalculates; the owner is freezing the number already on screen. Supports
   **multi-select** (commit a whole week's batch at once — each snapshots its own date).

2. **Commit as Urgent** — marks the order **Urgent** with `promised_date = its SO
   delivery date`, slotting it into the protected group by that date. Used when a job
   *must* ship by a hard date.

Both are **admin-only** (the planner/father), server-enforced. An order can be
**Uncommitted** (returns to Open; lock cleared) — for a cancelled or mistaken commit.

## The warning (the one safety net)

A **normal Commit never pushes an existing promise** (it goes to the back), so it needs
no warning. **Urgent can** — by slotting ahead of later-promised orders. So *before* an
Urgent (or any re-prioritisation that moves an order ahead of an existing promise) is
saved, the system runs a **preview plan** with the proposed change and compares every
*other* protected order's new expected date against its `promised_date`. If any would
now finish **after** its promise, the owner sees an explicit confirm:

> ⚠️ *"Making SO-999 urgent (due 25-Jul) pushes SO-478 from 27-Jul → 29-Jul (past its
> promise). Proceed?"*

He confirms (ship the rush job) or cancels (call that client first). **No promise is
ever broken silently.**

## Promised vs. current — honest slippage

Committing protects an order from *other* orders; it does **not** hide the order's **own**
floor reality. The Orders view shows two dates side by side for every protected order:

- **Promised** — locked at commit time (never changes on its own).
- **Current expected** — live; updates from the feedback loop (Rule 7 actuals → remaining
  qty → re-plan) exactly as today.

If a protected order's **Current expected drifts past its Promised** (its *own*
production fell behind — breakdown, slow shift), the row is **flagged (red)** so the
owner sees a real, self-inflicted slip immediately. This is the only way a committed
order's promise can be at risk, and it's always visible.

## Technical design

Reuses the existing order-book + Rules 1–6 architecture, with **two** real additions: a
**two-pass Plan** (`api._plan`) and a **machine/operator reservation** capability in
Rule 6 so pass 2 can fit around pass 1. Everything else (Rules 1–5, the order book,
persistence) is unchanged, and with all orders Open the plan is byte-identical to today.

### Data model (`engine/orderbook.py`, `engine/models.py`)

`Order` gains three persisted fields (keyed by the existing `(SO No, Item Code)` pair):

- `commitment: str` — `"open"` (default) | `"committed"` | `"urgent"`.
- `promised_date: date | None` — the locked promise (None while Open).
- `committed_at: datetime | None` — snapshot time (audit + deterministic tiebreak among
  equal promised dates).

`SOLine` (emitted by `active_so_lines` for planning) carries the same
`commitment`/`promised_date` so the rules can see the lane.

### Scheduling — the two-pass mechanism (the core of the guarantee)

A reordered priority list is **not** sufficient. Rule 6 is a *non-delay* scheduler — it
gives a free machine to whatever operation can start soonest — so an Open op that is
ready at 09:00 could still snatch a machine before a committed op becomes ready at 09:30
and delay it. Priority only breaks ties; it does not *reserve* capacity. To make the
promise ironclad, the Plan runs in two passes:

**Pass 1 — protected only.** Take just the protected orders (committed + urgent), ordered
among themselves by `promised_date` (then `committed_at`), and run the **unchanged Rules
1→6** over them. Because the Open orders are absent, this pass produces the committed
orders' schedule *as if the open orders did not exist* — so their expected dates equal
the promised ones and cannot move when new Open orders arrive.

**Pass 2 — Open into the gaps.** Collect, from pass 1's schedule, the **busy intervals of
every machine and every operator** (the committed reservations). Run Rules 1→6 over the
Open orders with those reservations seeded in, so an Open operation may occupy a machine
only in a **free interval that is not reserved** by pass 1. Filling is **non-preemptive
and boundary-respecting**: an Open op takes a gap (e.g. CNC4 idle 09:00–10:00) *only if
it is ready and fits entirely before the next committed reservation*; otherwise it moves
to a larger free window or after the committed work. It **never** overruns a committed
block. This backfills idle machine time (efficient) while keeping every committed
operation fixed.

The two passes' schedules are merged for the trace / Gantt / machine-wise / analytics
views (one combined plan on screen; the split is internal). `api._plan` orchestrates the
split, the two `run_forward` calls, the reservation hand-off, and the merge.

**Rule 6 reservation support (the one engine change).** Today Rule 6 tracks a single
"next-free time" per machine (`machine_free[m]`) and per operator (`operator_free[o]`).
Pass 2 needs it to honour a set of **pre-existing busy intervals** per machine/operator
and place ops into the free windows between them (respecting the working calendar exactly
as now). Implemented as an optional `reserved={machine|operator: [(start,end), …]}`
argument: when finding an op's earliest feasible start, skip any window that overlaps a
reservation, and reject a placement that would not finish before the next reservation
begins. `reserved=None` (pass 1, and every existing caller) is byte-identical to today.

**Priority within a pass (`rule3_tiebreak_process_time.py`).** Pass 1 orders the protected
batches by `promised_date` (then `committed_at`); pass 2 keeps the existing slack order
for Open batches. Rule 3 gains only this protected-group sort key; Rules 1/2/4/5 unchanged.

**Consolidation guard (`rule1_consolidate.py`):** orders of the same item are only merged
when they share a lane *and* (for protected) a promised date — a batch must not straddle
lanes, and (given the two-pass split) protected and Open orders of the same item are
planned in different passes anyway. Cross-lane orders of the same item stay separate batches.

### Commit / urgent / uncommit (`engine/orderbook.py` + `api/main.py`)

Pure order-book functions (`commit_order`, `mark_urgent`, `uncommit_order`) set the
fields; `book_store` persists them (composite `(SO#, item)` hash field, like completion).
New admin-only endpoints: `POST /orders/commit`, `/orders/urgent`, `/orders/uncommit`
(password not required — non-destructive; role-gated like other admin actions).

Commit's promised date = the order's **current expected completion** taken from the most
recent plan (the value already shown on the Orders/Gantt view).

### Warning preview

`POST /orders/urgent` (and any move-ahead) first runs a **dry preview**: re-run **pass 1**
(the protected group) with the order marked urgent — inserted by its delivery date — and
diff each *other* protected order's new expected against its `promised_date`. Return the
list of orders it would push past promise. The UI shows the confirm modal; a second call
with `confirm=true` applies it. (Open orders are irrelevant to the warning, so the preview
can skip pass 2 — it's fast.)

### UI (`web/`)

- **Orders tab:** per-row lane **badge** (Open / Committed / Urgent), **Promised** and
  **Current expected** columns (slip flagged red), and action buttons **Commit** /
  **Commit as Urgent** / **Uncommit**. Multi-select → bulk Commit.
- **Warning modal** on Commit-as-Urgent when the preview reports pushed promises.
- Read-only role sees the badges/dates but not the action buttons (as with other
  admin-only controls).

## Edge cases

- **Feedback loop:** committed orders re-plan at their remaining qty as today; only
  Current expected moves; Promised is untouched; slippage flagged if it drifts past.
- **Completion:** a completed order leaves the book (archived), lane state irrelevant.
- **Uncommit:** clears `commitment`/`promised_date`; order returns to Open.
- **All-protected book:** if every order is committed, Open simply has nothing to place;
  the protected group plans by promised date as normal.
- **Urgent with an already-past delivery date:** still slots to the front of the protected
  group (earliest promised date wins); slippage flag will show it can't be met — honest.

## Testing (TDD, one behaviour per test)

- Open order never changes a committed order's expected date (the core guarantee) —
  including when the Open order is far larger / earlier-due than the committed one.
- **Two-pass gap-fill:** an Open op backfills an idle committed gap (e.g. CNC4 09:00–10:00)
  when it fits, and is pushed to a later window when it would overrun the committed block —
  never delaying the committed op (the reservation boundary is a hard wall).
- Rule 6 `reserved=None` is byte-identical to today (existing callers unaffected).
- Commit snapshots `promised_date` = current expected; order moves to Committed lane.
- A later normal commit sorts behind an earlier one (first promised, first served).
- Urgent slots **ahead of** later-promised and **behind** earlier-promised protected orders.
- The preview detects and reports a protected order pushed past its promise.
- Slippage flag: a committed order whose Current expected exceeds Promised is flagged.
- Persistence round-trip of `commitment`/`promised_date`/`committed_at`.
- Golden trace unchanged (all defaults Open → behaviour identical to today).

## Non-goals (YAGNI)

- No automatic commitment — commit is always a deliberate owner action.
- No buffer/padding or manual editing of the promised date — auto-snapshot only.
- No multi-level urgency or numeric priorities — three lanes only.
- No change to how Open orders rank among themselves (existing slack order stands).
