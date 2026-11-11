# Profit and Loss tab — design (2026-11-11)

Owner request (director's feature). Base: `origin/main` `518b11c`, NOT the
`optimizer-search-fixes` working tree (that branch is behind main: it has no
Machines tab, no fixed plan, no Item Process Master tab).

## 1. Intent

For every finished order (SO number + item code) the director wants to see what the
order cost Anvitech in machine time, two ways:

- **Planned:** what the plan said the order would take.
- **Actual:** what the floor really spent, from Daily Entry.

Cost = hours on a machine × that machine's hourly rate. Later, when the owner supplies
selling prices, the tab will compare each cost against what the client was charged
(price − cost = profit or loss). The owner's example: plan 100 h = ₹10,000, actual
120 h = ₹12,000, charged ₹9,000 → planned loss ₹1,000, actual loss ₹3,000.

The point is to show the director which orders lose money and whether the loss comes
from the quote (the plan already costs more than the price) or from the floor (actual
costs more than plan).

## 2. Goals

- A new tab **Profit and Loss**, **admin only** (viewing included), server-enforced.
- One row per **completed** order that is eligible (section 5).
- Per row: planned hours, planned cost, actual hours, actual cost, difference
  (actual − planned) in ₹ and %. Expandable per-step / per-machine breakdown.
- Machine hourly rates editable in the app (Machines table), never in code.

## 3. Non-goals (this stage)

- **Selling price and the profit/loss figure.** The owner will provide prices later.
  This stage leaves the table ready for them (sections 9, 10) but builds no price entry.
- **Rates for manual inspection (MI1, MI2, MI3) and any other inspection station.**
  Owner will give them later. Until then those hours show, with cost "rate not set".
- Non-machining steps (deburring, washing, packing, marking, etc.), outsourced (OS)
  steps and dispatch: **never counted**, owner's rule.
- Orders still running: **not shown** (owner: only after the order is complete).
- Orders that had punches before this feature went live: **not shown** (owner: do not
  consider older punches; only new orders or orders not yet started).
- Material, tooling, labour, overheads, vendor cost. This is machine-time cost only.
- No effect on the plan, the optimizer, or any other report.

## 4. Anti-goals

- A cost that looks real but is built from partial data (e.g. an order whose early
  steps were punched before go-live). Eligibility (section 5) exists to prevent this.
- Two different definitions of "hours on a step" between this tab and the production
  analysis report. Actual minutes must be derived from the same punch fields that report
  uses (section 7).
- The tab moving because a re-optimization happened after the order was finished.
  A finished order's planned figure is fixed (section 6).

## 5. Which orders appear (eligibility)

An order appears when BOTH hold:

1. It is **complete** (archived: someone ticked "mark complete" on a Daily Entry).
2. A **planned snapshot** exists for it (section 6). A snapshot is only ever taken
   while the order has **zero punches**, so an order already started when the feature
   goes live can never get one and is excluded **by construction**, with no cut-off
   date to maintain. New orders (Add New Orders) and orders not yet started at go-live
   both qualify.

Builder note: also exclude (and list in a small "not shown" note with the reason) any
eligible order where a counted step has a punch with no machine recorded, since its
actual cost cannot be priced. Owner wording for the note is the builder's call, plain
English.

**Which blank-machine punch counts (final review, 2026-11-11).** The real Daily Entry
form saves outsourced, DISPATCH and manual steps whose routing names no machine with
`machine = ""` and the fixed 630/570 minutes. Those must never hide an order: on the
Test9 / Test8 harness every finished order has one, and the first version hid all of
them. A blank-machine punch is the "cannot be priced" flag ONLY when its routing
step is a counted step: one of the machines the routing names for it (Allotted, then
Suggested, split by `loaders.parse_resource_candidates`, the same list the form's
machine picker offers via `_routing_machines`) is CNC/VMC or inspection. A step the
routing does not list at all is treated as counted (held back, not priced on partial
data). Every other blank-machine punch is dropped silently.

**An order with no counted step at all** (manual / outsourced only) gets an EMPTY
snapshot (`steps: []`) while unstarted, so it is listed under "not shown: no CNC, VMC
or inspection work", never miscounted as an older order.

## 6. Planned hours: what "the plan" means for a finished order

A completed order is no longer in the current plan, so its planned figure must be
**recorded while it is still planned**. Rule (my call, see section 11):

- On every plan computation (`_plan` compute path, not cache hits), for each active
  order with **zero punches**, record its planned machine minutes per counted step:
  `{(so_no, item_code): {"steps": [{op_seq, process, machine, minutes}], "at": iso}}`
  in a new store key `anvitech:pnl_planned`. Overwrite while the order is unstarted.
- **Stop overwriting at the first punch.** The last snapshot before work began is the
  planned figure, permanently. (Under the fixed plan, 2026-10-07, this is the published
  plan the floor started from.)
- Minutes = the machine's working time for that entry as the engine laid it (the same
  per-entry busy minutes Analytics counts, setup included; nights, breaks, weekly off
  excluded). Use the engine's own entry/segment data; do not re-derive durations.
- **Clubbed batches (Rule 1):** a batch's entry serves several SO lines. Split each
  entry's minutes across its member orders **in proportion to each line's pieces on
  that entry** (`piece_refs` / `Batch.line_process_qty`, which handle a line clubbed
  into a batch already in production), not by ordered quantity. Setup split the same.
- Store **setup minutes and run minutes separately** per step, so the planning factor
  question (section 12) can be answered at display time without re-snapshotting.
- Store **minutes and machine**, never rupees. Cost is computed at display time from
  the current rate, so a rate edit moves planned and actual together and the
  comparison stays fair.
- Writing the snapshot is a side effect of planning; it must never affect the plan
  response or `_plan_fingerprint`.

## 7. Actual hours

From Daily Entry punches (`Actual`) for that (SO#, item).

**What "minutes in shift" really is (corrected at final review, 2026-11-11).** On the
Daily Entry form the box is READ-ONLY and always holds the shift's working minutes,
630 (1st shift) or 570 (2nd shift), by the owner's 2026-10-07 rule. The operator never
types the time spent on a job. So a punch's actual time is a share of the machine's
shift, not a time on the job: **the owner chose this on 2026-11-11 (option a,
machine-shift sharing)**. Each machine's shift is shared between the jobs punched on
it that shift by the work each one reports (section 7b); a job punched alone on a
machine that shift is charged the whole shift, less its downtime.

- Count only punches whose step is a **counted step** (section 8).
- Minutes per punch = `shift_minutes` (the fixed shift minutes, see above) **minus the six
  downtime fields** (no power, no operator, machine breakdown, tool problem, other
  work, no load). **Setup time stays in** (owner: take setup, not downtime).
  `shift_minutes` already includes setup (production analysis treats H as the whole
  time on the job and deducts setup and downtime from it), so setup is not added again.
- Machine = the punch's own `machine` field (where it really ran), not the plan's.
- Cap each punch's minutes at the shift's working minutes (630/570), exactly as
  `production_analysis.actual_metrics` does, so there is one definition. Floor at 0 if
  downtime exceeds the minutes typed.
- A punch with blank (0) minutes on the job is NOT a real zero: that step is treated
  as "not punched" (see section 7a).
- Rejected pieces are not deducted: the time spent making them is real cost.
- Mis-punches against the wrong SO of a clubbed pair will distort that pair's actuals;
  accepted, not corrected here (the Daily Entry panel already guards against it).

## 7a. A counted step with no usable punch (owner, 2026-11-11: option b)

The floor punches machining steps but may not punch inspection, and "mark complete"
does not require every step to be punched. A counted step with no punch, or whose
punches all have a blank (0) "minutes in shift", is **"not punched"**: the order is
still shown, its actual hours/cost are the sum of what was punched, and the row is
flagged **"actual incomplete: N step(s) not punched"** (the breakdown names them). It
is never silently counted as 0 and the totals row says how many orders are incomplete.

## 7b. Several punches on one machine in one shift (owner, 2026-11-11)

The "minutes in shift" box is fixed at the full shift (630/570) and the operator
cannot change it. Two punches on the same machine in the same shift (e.g. clubbed SO149 +
SO150, or two jobs) would then count 1,260 minutes in a 630-minute shift.

Rule: group all punches (every order, not only eligible ones) by **(machine, date,
shift)**. If the group's total "minutes in shift" is more than that shift's working
minutes, scale each punch's minutes down to its share of the shift, weighted by the
work it reports: `qty_produced × standard cycle time (original, unpadded) + actual
setup minutes` (split evenly if every weight is 0). A group that fits inside the shift
is used exactly as typed. Downtime is then deducted from each punch's (scaled)
minutes, floor 0. The operator's workflow does not change. Punches with no machine
cannot be grouped and are handled by the section 5 rule.

## 8. Counted steps and rates

- A step counts when its machine is **machining (CNC/VMC)** or **inspection**, by
  `ppc_engine.loaders.normalize.machine_kind_from_type` on the Machines table type
  (the one classification the app already uses). Outsourced/off-machine never count.
- Rates live on the **Machines table** (`anvitech:machines`) as a new optional field,
  rupees per hour, editable in the Settings Machines card. The seed carries no rate
  (`shop_masters.py` seeds name/type/hours only), so rates start blank and the go-live
  list below is loaded. `apply_machine_save` rewrites the whole entry, so every save
  must carry the rate through or it is wiped.
- **Rates are admin-only (my call):** `GET /machines` and the Machines card are open to
  the user role, so the rate field must be stripped from the user-role response and
  its column/input hidden for that role (role gate on the control, 2026-08-09 rule).
- Rates to load at go-live (owner, 2026-11-11, ₹/hour):
  CNC1, CNC4, CNC5, CNC7 = 450 · CNC3, CNC6 = 650 · VMC1 = 500 · VMC2, VMC3 = 650 ·
  CMM = 850. MI1/MI2/MI3 and other inspection: blank until the owner gives them.
- A counted step on a machine with no rate: hours shown, cost "rate not set", and the
  order's total cost marked incomplete (never silently treated as ₹0). **Ruling at
  final review (2026-11-11):** the row's planned / actual cost is the sum over the
  machine time that HAS a rate, flagged `planned_cost_partial` / `actual_cost_partial`,
  shown as the number plus "incomplete (rate not set: MI1)"; the difference is shown
  only when both sides are complete; totals still cover complete rows only, and read
  "-" (None) when there is none.
- The rate field must have **no plan effect**. Verified: `machines_digest` hashes
  name, type and hours only, and it is what feeds `_plan_fingerprint`,
  `_inputs_signature` and the masters cache key; keep it that way and pin it by a test.

## 9. The screen

Admin-only nav tab **Profit and Loss**. A table, newest completion first:

| SO No | Item | Qty | Completed on | Planned hrs | Planned cost ₹ | Actual hrs | Actual cost ₹ | Difference ₹ | Difference % |

- Difference = actual − planned; red when actual costs more.
- Click a row: per-step breakdown (process, planned machine, planned hrs, actual
  machine(s), actual hrs, rate, costs).
- Totals row at the bottom.
- Columns reserved for later, NOT shown this stage: Selling price, Profit/loss
  (planned), Profit/loss (actual).
- Plain operator English throughout, no em dashes (UI convention since 2026-08-05).
- An xlsx download is not asked for; leave it out.

## 10. API and code shape

- Pure module `engine/profit_loss.py` (no store, no HTTP): planned-snapshot extraction
  from a schedule + batches, actual minutes from punches, per-order rows with costs.
- `GET /profit-loss` (admin; 403 for the user role).
- Rate edits go through the existing Machines save endpoint with the new field.
- `tests/test_role_parity.py`'s whole-nav invariant carries a named exception set
  (currently Add New Orders); add Profit and Loss to that exact set.
- "Completed on" = `Order.completed_on` (set by `book_store.complete_order`).

## 11. Decisions made without the owner (marked as mine)

- Planned figure = the last plan before the first punch, frozen (section 6). The owner
  said "the current plan", which no longer contains a finished order; this is the
  closest stable meaning.
- Clubbed batch time split by quantity.
- Rupees computed at display time from current rates, not stored.
- Steps on a machine with no rate shown as "rate not set", not ₹0.
- No xlsx download, no price entry this stage.

## 12. Owner answers (2026-11-11)

- **Planned CNC/VMC time does NOT include the 30% planning pad.** The plan uses cycle
  × 1.30 on CNC/VMC (`engine/planning_time.py`, `CNC_VMC_PLANNING_FACTOR`). The snapshot
  stores RUN minutes at the ORIGINAL standard (pieces × master cycle time) and the
  plan's setup separately; where no cycle time exists it divides by the constant,
  never a hardcoded 1.3. Inspection is never padded.
- Unpunched steps: option b (section 7a).
- Several punches per machine-shift: proportional split (section 7b).

## 13. What the builder must verify

- On a copy of the live store (read-only copy, never plan against live): snapshot
  taken only for unstarted orders; frozen at first punch; already-started orders never
  appear; plan byte-identical with and without the feature.
- Clubbed batch: planned minutes of members sum to the batch's minutes.
- Mutation-test each load-bearing part (zero-punch gate, freeze at first punch,
  downtime deduction, counted-step filter, role gate).
