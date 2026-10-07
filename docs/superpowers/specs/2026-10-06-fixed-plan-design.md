# Fixed plan: only the admin's Optimize moves a job (2026-10-06)

Owner request, after a director's complaint and the floor's. Designed in
conversation with the owner on 2026-10-06; every decision below is his.

## 1. The problem

Every "Done entering — update plan" click re-plans the whole book. Two things move
work on the floor every day:

1. **A daily re-plan rebuilds the schedule from scratch.** Only half-finished steps
   are pinned (the 2026-07-29 freeze). Every step not yet started is laid again by a
   greedy dispatcher from today's 08:00, and a greedy dispatcher is chaotic: on the
   real book a 6-hour change in the plan start re-sequenced 54 of 68 orders and moved
   dates by up to 24 days (2026-08-07, measured). So item B, published on CNC6, can
   be on CNC3 the next morning with no optimizer involved at all.
2. **The daily auto-optimize** (`POST /optimize/done` → `_try_start_auto`) searches
   sequence × overlap × machine-set and auto-applies whatever scores better.

Result: a promised date jumps from 1 January to 10 January overnight, and a job
moves between machines, for reasons nobody on the floor can see. The director's rule:
**the plan must be constant. Daily entry updates progress, not the plan.**

Optimizing weekly instead of daily would remove cause 2 only. Cause 1 needs the
daily re-plan itself to stop re-deciding machines and order.

## 2. Decisions (owner, 2026-10-06)

| # | Question | Decision |
|---|---|---|
| D1 | Item A finishes early or late — what happens to B behind it on the same machine? | B moves up or down with it. Never held idle on purpose. |
| D2 | Who re-plans, and when? | **Only the admin**, whenever he decides. No automatic day. |
| D3 | The button | "Start deep search" in Settings is renamed **Optimize** and warns before it starts. |
| D4 | After the search finishes | Show **which orders' delivery dates move earlier or later** (NOT which jobs change machine — too much). Admin presses **Apply** or **Discard**. Never auto-applied. |
| D5 | A machine is marked down | Jobs on it **wait** (their dates slide), and the admin gets an **"Optimize recommended"** banner. Optimize decides wait-or-move. |
| D6 | A machine's next job in its queue isn't ready, but a later one is | **Strict turn** (amended, see §8): the machine waits for its next job. Running another one first is the floor's decision, not the software's. If the floor does run B first, the punch makes B started and the plan follows. |
| D7 | Operator | Same person where possible; if busy, absent or off shift at the slid time, another qualified person on that shift. **People are swapped, never machines.** |
| D8 | A job's machine is no longer allowed (routing edited, machine removed) | The software places it on an allowed machine and shows the "Optimize recommended" banner. |
| D9 | New orders accepted in Add New Orders | Added to the fixed plan at the slot their quote gave them. |
| D10 | "Ask for an earlier date" in Add New Orders | Re-plans the whole book and is accepted by the admin — treated as an Optimize: it publishes a new plan. |
| D11 | The freeze | **Kept.** Half-finished jobs stay on their machine and operator, quantity from punches. Change: a half-finished job on a down machine now **waits** (D5) instead of being released by the engine on its own. |

**The one rule:** only the admin's Optimize (and accepting an earlier date in Add New
Orders) moves a job to another machine or changes its turn. Exceptions D8 (the
machine is no longer allowed) and the floor's own out-of-turn punch (D6) follow
reality, not the software's choice.

## 3. What the floor and the admin see

- **"Done entering — update plan" (both roles):** repairs the plan in seconds. Every
  job keeps its machine and its turn; start and finish times are recalculated from
  the punches. The optimizer never runs from this button. Note afterwards, e.g.
  *"Plan updated from today's entries at 18:42. No job changed machine."*
- **Optimize (admin, Settings):** confirm dialog — *"This may move jobs to different
  machines and change their order and delivery dates. The floor will see a new plan.
  Continue?"* — then the existing progress panel; at the end, the list of orders whose
  delivery date moves (earlier / later, by how many days) and Apply / Discard. The
  user role keeps today's read-only view of the panel (2026-08-09 role-parity rule).
- **Banner, admin and user:** *"CNC3 is marked down 07-10 to 08-10 and 5 orders are
  waiting on it. Press Optimize to decide whether to wait or move them."* Shown while
  any published job sits on a machine that is down, or on a machine no longer allowed
  (D8). Gone after the next applied Optimize or when the downtime ends. Information,
  so both roles see it; only the button is admin.
- Delivery dates on the Orders tab, Gantt, shift-wise, delay report: unchanged
  surfaces, all reading the repaired plan (one plan, one set of dates).

## 4. Design

### 4.1 The published plan

Written **only** when a plan is published: an applied Optimize (`_optimize_apply`),
an accepted earlier-date (`/new-orders/prepone/accept`), and once at go-live.
Extends the existing `anvitech:last_applied_schedule` projection
(`freeze.schedule_projection`), which already stores machine / operator / start / end
per real machine op. New per row: **its turn** — the position of that op in its
machine's queue (published start order, ties by previous-plan order).

**Key rows by what survives a re-plan, never by batch id.** `rule1_consolidate`'s
`B001…` ids restart on every call (2026-09-08 collision). A row is keyed by
(item code, routing op_seq, sorted SO refs); a batch whose membership changes later
(a line completes, a new line clubs in — owner's 2026-09-27 rule) is matched to the
row sharing any SO ref for that item and step.

New orders accepted by `/new-orders/add` (D9) are appended to the published plan with
the machine, operator and turn their quote placed them at — the quote already plans
them around the published plan's occupancy, so this is consistent by construction.

### 4.2 The repair (what "Done entering" runs, and what every `/run` runs) (amended, see §8)

The normal engine (`flow_scheduler.decode`) with every published op **pinned**:
machine from the published row, turn order per machine strict (D6), operator
preferred but re-staffed by the existing `StaffingBoard` rules (D7). This is the
2026-07-29 freeze generalized from half-finished ops to every op; frozen ops already
go through `_lay_pinned`, so placement reuses one path.

Times therefore come from the one window source, `ppc_engine.worktime.iter_windows`:
weekly off and holidays, machine downtime, night shift only where the machine runs
it, meal breaks cut out. Staffing keeps qualification, shift, absences and
no-double-booking. Every op still goes through `_ready_after` and the piece-flow guard
(the 2026-08-09 rule for any placement path). Quantities still come from the batch
(`Order.process_remaining`, 2026-08-11 rule).

Strict turn: an op may not start on its machine before the op with the previous turn
on that machine has been placed. A machine whose next op is not ready waits (D6).
Half-finished ops keep today's rule: they run first on their machine.

**What is never pinned:** OS / off-machine steps (no machine), DISPATCH. Steps of an
order that is in the book but not in the published plan (should not exist after
D9/go-live; a safety net) are laid by the normal dispatcher in the machine's free
time. (Amended while planning: holding them until all pinned work had passed would
need a second planning stage for what should never happen.)

**Deadlock guard.** A published plan is feasible when written, so strict turns cannot
form a cycle (routing order plus per-machine order were consistent at publish time).
Insertions (D8, out-of-turn punches) could in principle break that. If the repair
cannot place any further op, it releases the turn of the blocked op with the
earliest published start, records it in the validation report
(`TURN_RELEASED`, naming SO#, item, step, machine) and continues. Fail loud, never
stall, never silently drop work.

**Machine down (D5, D11).** The pin is NOT released. The op waits through
`iter_windows`, its dates slide, and the banner shows. `new_engine._ppc_frozen`'s
plan-time maintenance release (2026-08-31) moves to the Optimize path only: an
Optimize run still releases pins on down machines and lets the objective decide wait
versus move, exactly as today.

### 4.3 Optimize

Unchanged search (sequence × overlap × machine-set, frozen set honoured). Changes:
started only by the admin (confirm dialog); never auto-applied; the result panel adds
the per-order date change list (current repaired plan vs candidate, via
`optimizer.expected_completion`, the one definition); Apply writes the published plan.

### 4.4 Removed / changed

- `POST /optimize/done` no longer calls `_try_start_auto`; it runs the repair and
  writes the note. `_try_start_auto` / `_auto_apply_result` stay in code, unreferenced
  by the button, so a future owner decision can bring scheduled auto-optimize back
  without rebuilding it (same pattern as `COMMITMENT_FEATURE_ENABLED`).
- The Add New Orders queue ended at "Done entering"; it now ends only at a published
  plan (an applied Optimize or an accepted earlier date).
- `SCHEDULER_FINGERPRINT` bumped: real work moves (plans now follow the published
  machines).

### 4.5 Go-live

On first plan after deploy with no published plan on file: the plan the floor sees
at that moment (today's planner + applied ranks) is written as the published plan.
From then on nothing moves without an admin.

## 5. Verification (all must pass before pushing)

On a **read-only copy of the live store** (owner gives `MONGODB_URI`; nothing written
back) plus Test5/8/9 at several WIP levels, production-like config:

1. **Calendar:** no repaired op in a meal break, on the weekly off, a holiday, a down
   machine, or a shift its machine doesn't run.
2. **People:** 0 `OPERATOR_NOT_QUALIFIED`, 0 double bookings, nobody absent booked.
3. **Stability:** simulate 10 days of real punches with repairs in between — 0 ops
   change machine, 0 turn changes except logged out-of-turn punches and D8.
4. **Idempotence:** a repair with no new punches returns the identical plan.
5. **Dates move only with punches,** and only for orders downstream of the change.
6. **Existing invariants:** 0 routing-order, qualification, batch-quantity violations;
   one set of dates across Orders / Gantt / delay report / shift-wise.
7. **Add New Orders:** a quote accepted, then a repair → quoted date unchanged.
8. **Machine down:** mark one down → its jobs wait, banner shows, no job moves;
   Optimize → wait-or-move decided, date list shown, Apply publishes.
9. **Mutation testing:** revert each load-bearing part (machine pin, strict turn,
   no-release on down machine, published-plan write on apply only) individually; each
   must fail at least one test.
10. Browser pass, both roles: Done note, Optimize warning, date list, banner.

## 6. Deliberately not built (amended, see §8)

- No list of which jobs changed machine (D4).
- No automatic optimize on any day (D2).
- No software reordering of a machine's queue when a later job is ready (D6; amended, see §8).
- No automatic move back when a down machine returns (moot under D5).

## 7. Risks, stated plainly

- **Strict turn leaves machines idle** when the next job's parts are late. This is
  the owner's choice (D6); the delay report will show it as waiting, and the floor
  may run another job first.
- **Late-days will be worse between Optimize clicks** than a daily re-optimized plan.
  That is the price of a stable plan; the admin recovers it by pressing Optimize.
- The repair path is new placement code; the 2026-09-22 lesson applies — run it
  through `/new-orders/quote` on a store copy, not only through the plan.


## 8. Amendment after the live-copy verification (owner, 2026-10-06)

The 10-day simulation on the live copy showed that an outsourced step not yet
punched as received is re-laid a full vendor lead time from "now" on every plan
(pre-existing), and under strict turn every job queued behind its order on a machine
waited with it: +229 late-days over 10 days when OS steps are punched on return,
+4 when punched on sending. The owner chose two changes:

**D6 amended — next ready job in published order.** A machine works through its
published queue in order. If the job whose turn it is cannot start yet (its previous
step is not done, or it is at a vendor), the next READY job in the queue goes first,
on the same machine. When several are ready, the published order wins. No job ever
changes machine this way; the order only changes when the job ahead genuinely is not
ready. (Replaces "strict turn"; the floor-runs-out-of-turn rule still holds.)

**D12 — vendor time counts from when the parts were sent.** "Sent" = the day the
step before the outsourced step was entered as complete (its full quantity punched
as done; for a batch of several SO lines, the latest such day across its lines, and
only when every line has completed it). The outsourced step then ends at sent date +
lead time, or at the plan start if that is already past (the plan cannot see the
vendor, so it never invents a further delay). With no sent date known, today's
behaviour is unchanged. Until the outsourced step is punched as received, its
order's next steps wait for that expected return, as now.

**Owner decision 2026-10-07 — version A0 adopted.** Three implementations of D6-amended
were measured on the live copy: A0 (this branch), A1 (spec-literal, branch
`fixed-plan-a1`: 116/119 checks on variant A, one CNC7 pair where an earlier-published
job ready for a day ran after a later one) and B (a simple chronological rule, branch
`fixed-plan-simple`: broke repair fidelity, 22 of 67 orders moved at go-live, quoted
dates drifted). The owner chose A0: 119/119 on both variants, late-days level with the
old daily re-plan. **Accepted exception:** when the job ahead on a machine is late AND
the work that would make it ready cannot be placed without jumping another machine's
queue, A0 does not give way: the machine waits for the late job (strict turn, in that
case only). Measured cost on the live copy: none.
