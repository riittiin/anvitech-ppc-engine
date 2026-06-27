# Anvitech PPC Engine — Rules in Execution Sequence

> Source of truth for the Production Planning & Control engine.
> Rules are reordered from the `PPC logics` sheet of the source workbook into the
> sequence in which the engine actually executes them. Each rule maps back to
> its original number for traceability.

## How to read this file

Each rule is tagged by its role in the data flow:

- 🔻 **Pipeline stage** — a true `output → input` handoff. Its output is the
  next pipeline stage's input.
- ⚙️ **Parameter / calc** — not a transform. A rule/setting that is *consumed
  during allocation* (Rule 6). It sits beside the pipeline, not in it.
- 🔁 **Loop** — flows back to an earlier stage instead of forward.

### Data flow at a glance

```
   Raw SO lines
       │
   🔻 Rule 1  Consolidate
       │  batches {item, qty, SO delivery date}
   🔻 Rule 2  Sort by delivery date
       │  ordered batches
   🔻 Rule 3  Smart priority (least slack)          ←─ also reads routing (Item's process Master)
       │  prioritized batch list
       ▼
   🔻 Rule 6  Allocate to machines  ◄── ⚙️ Rule 4 (setup time)
       │                            ◄── ⚙️ Rule 5 (overlap mode)
       │                            ◄── masters (machine / operator / calendar)
       │  the schedule
   🔻 Rule 7  Capture daily actuals
       │  actuals
   🔁 Rule 8  Re-run MRP  ──────────────────────────► back to Rule 1
```

**True forward pipeline:** `1 → 2 → 3 → 6 → 7 → 8 (loop)`
**Consumed inside Rule 6:** Rules 4, 5.

---

## PHASE 1 — Build the demand list *(what to plan)*

### 🔻 Rule 1 — Consolidate sales orders  *(Pipeline stage)*
Group SO lines of the **same item code** whose **delivery dates fall within a
10-day window** (user-configurable) into a single production batch.

- **Source:** original Rule 1
- **Input:** `Sales Order (SO) list` (raw SO lines)
- **Output:** list of batches `{item code, total qty, SO delivery date, source SO refs}`
  where the batch's **SO delivery date** is the earliest SO delivery date among the
  merged lines (the binding customer commitment). The SO Delivery Date column is the
  single, authoritative delivery date used for logic and display — no other date.
- **Parameter:** consolidation window = 10 days (configurable)

---

## PHASE 2 — Prioritize the batches *(order in which jobs claim machines)*

### 🔻 Rule 2 — Sort by delivery date (primary)  *(Pipeline stage)*
Order all batches by **earliest SO delivery date** first.

- **Source:** original Rule 2
- **Input:** batches from Rule 1
- **Output:** same batches, ordered by delivery date

### 🔻 Rule 3 — Smart priority: workload-aware urgency (secondary)  *(Pipeline stage)*
Strict earliest-due-date alone is naive: a 1-piece order due 19 Jun would outrank
a 100-piece order due 20 Jun, even though the big order is far more at risk of
being late. Rule 3 fixes this with an operations-management dispatching metric
that folds **due date and workload** into one urgency number:

- **Slack** = (working time until SO delivery date) − (work needed), where *work
  needed* = the batch's total machine occupancy (Rule 4: cycle×qty + setup,
  summed over the routing). **Least slack = most at-risk = highest priority.**
  (Alternative metrics: *critical ratio* = time ÷ work; *process time* = legacy.)

On **equal** SO delivery dates the available time is the same, so least-slack
reduces to "more work first" — preserving the documented same-date oracle
(`61241949-01` before `61247047-01`). On **different** dates it weighs the extra
time against the extra work, so a heavy order due slightly later can correctly
jump an earlier trivial one. A `priority_window_days` knob bounds how far apart
two dates may be for the metric to reorder them (default: no limit).

- **Source:** original Rule 3, generalized
- **Input:** ordered batches from Rule 2 **+ routing lookup** (`Item's process Master`,
  for work content) + config (metric, window)
- **Output:** fully prioritized batch list (+ a per-batch slack/critical-ratio
  breakdown shown on the Rule 3 tab)
- **Excel example:** `61240807-01` (highest process time) and `61247047-01`
  (lowest) rank as their Remarks specify; the new metric also handles
  near-date/large-quantity cases the old exact-tie rule missed.

---

## PHASE 3 — Allocation parameters *(consumed inside Rule 6, not pipeline stages)*

### ⚙️ Rule 4 — Setup time per process  *(Parameter / calc)*
For every process, add **90 min setup time** on top of (cycle time × qty) when
computing how long it occupies a machine.

- **Source:** original Rule 5a
- **Consumed by:** Rule 6 (machine occupancy calculation)

### ⚙️ Rule 5 — Operation overlap mode  *(Parameter / calc — global toggle per plan run)*
Provide a two-option selection:
- **(a) Sequential** — a process starts only after the previous process is
  **fully completed**, OR
- **(b) Overlap** — a process starts after **50% (user-defined) of the previous
  process's _cutting time_** is done.

**What the 50% measures (data-confirmed decision).** Machine occupancy =
*cutting time* (cycle × qty) **+ a 90-min setup** (Rule 4). The source rule says
"50% of first operation time," but does not define whether that includes setup.
It is measured against the **cutting time only — the setup is excluded**, because
while the previous operation cuts, the *next* machine's own setup runs in
parallel (so the previous setup need not be counted). This makes the knob mean
"start the next operation when X% of the parts are machined," and it shortens the
plan more than counting setup would.

**No-cutting steps don't overlap.** A finishing/inspection step with **no cycle
time** (deburring, CMM, inspection, washing, packing) produces nothing gradually,
so overlap is meaningless for it — its successor **waits for it to fully
complete** (as in sequential mode). Overlap only compresses real machining steps.

- **Source:** original Rule 6 (sheet rows 19–20, verbatim: "second operation
  shall start after first is fully completed" / "after 50% (or user defined) of
  first operation time is over")
- **Consumed by:** Rule 6 (start-time calculation for each next process)
- **Parameter:** overlap threshold = 50% (configurable). The percentage applies
  to the previous process's cutting time, not its setup.

> **Note — the old "parallel machine" rule was removed.** An earlier draft had a
> rule that, for batches over 400, moved a CNC operation to a separate machine.
> It never delivered true parallel processing (it relocated the operation rather
> than splitting the batch) and is no longer part of the engine. The remaining
> rules are renumbered into a clean 1–8 sequence.

---

## PHASE 4 — Allocate processes to machines *(the actual schedule)*

### 🔻 Rule 6 — Assign each process to the earliest-available preferred machine  *(Pipeline stage)*
Allot each process to the **earliest-available machine from its
preferred/suggested list** — respecting:
- the working calendar (**Thursdays off**, holidays, operator leaves), and
- **shift timings** (1st: 8am–7pm, 2nd: 7pm–5am). Each operator's shift is set by a
  **Shift column** in the Operator & shift Master (First/Second) — there is no
  swap-every-Friday rule.

**Time basis.** A process's machine time = **cycle time × qty** (+ the 90-min setup,
Rule 4). The Process "Total time" column in the master is **never** used.

**Non-delay scheduling (keep machines running).** The engine schedules at the
*operation* level, not batch-by-batch: at every step it considers the next ready
operation of **every** batch and starts the one that can begin earliest on its
machine. A machine is **never left idle while any operation is ready for it** —
the instant a machine finishes one batch's operation, the next operation that
needs it (from whichever batch whose previous process is already done) takes it.
The priority from Rules 1–3 only decides between operations that are *equally*
ready; it never forces a machine to wait for a higher-priority batch whose
operation isn't ready yet. This is the core of the optimization: maximize machine
utilization while honouring delivery-date priority.

**Alternative ("preferred") machines.** A process's *Suggested M/c* cell may list
**alternatives separated by `/`** (e.g. `CNC3/CNC6` = run on either CNC3 or CNC6).
The scheduler picks the **earliest-available** of the allowed machines, so work
**load-balances** across them automatically (once one is taken it's busy, so the
next contending operation grabs the other). On a tie the **first-listed** machine
wins (it's the preferred one), keeping plans deterministic. A listed machine not
yet in the Machine master is registered as a provisional machine and still used.
This applies to **any** alternative cell — CNC, inspection (`MI1/MI2/MI3`), etc.

**Parallel split (`split_parallel`, default on in the UI).** When a step lists
alternatives, the engine can **split the quantity across them to finish the step as
early as possible** instead of running it all on one machine. Each candidate gets the
load it can complete by a common target finish time, counted **from when it becomes
free** — so a machine that's busy now but frees soon still takes a (smaller) share, and
a faster machine (more available hours) takes more, so both halves finish together. It
splits **only when that beats the single best machine**. The next process waits for the
slowest half (split-then-recombine). Same logic for any alternative cell.

**Non-production steps — DISPATCH / OS (passed over).** A process with **no machine
assigned and no cycle time** is treated as a non-production pass-through and **skipped**
— no machine, no operator, no time: **DISPATCH** is the final "consider it done" step,
and an **OS** step (e.g. `BANDSAW OS`) is outsourced, so the next process becomes the
effective first step. (A blank machine *with* a real cycle time is **not** skipped — it
still surfaces as "needs machine", so genuinely missing data fails loud.)

**Operator & shift logic (`apply_operator_logic`, default on in the UI).** Each
machine's working window is driven by its **Available Hrs/Day** and by operator
coverage: a machine with Available Hrs/Day ≥ 12 (CNC/VMC ≈19.5) is a **two-shift**
machine (08:00–19:00 + 19:00–05:00); a smaller value (≈9.5 — saws, milling,
drilling, all manual stations) is a **single-shift** resource running **09:00–18:00
only**. An activity may run on a machine **only during a shift that has a qualified
operator** — an operator whose specialty (Preferred Machines, matched by machine
number *or* type, e.g. `CNC 1` or `Milling M/c`) includes that machine and whose
**Shift** (First/Second) covers it (manual work needs a **first-shift** operator).
So a machine runs the union of its operator-covered shifts: e.g. a CNC with only
second-shift operators runs second shift only. An operation whose machine has **no
covered shift** is **not scheduled** and is listed in a "needs operator" report
(never fatal); operator specialties that match no machine are reported too. All of
this is **Excel-driven** — edit the three master sheets, re-upload, and it reflects.
Provisional machines (referenced by a routing but not yet in the master) bypass the
coverage gate and keep a two-shift window. Each scheduled operation also shows the
**operator** running it — the first qualified operator on the shift the op falls in
(an `Operator` column on the Rule 6 schedule, present only when operator logic is on).

While running, Rule 6 consumes: ⚙️ Rule 4 (setup time) and ⚙️ Rule 5 (overlap mode).

- **Source:** original Rule 4 + `Machine master` + `Operator & shift Master` +
  `Weekly off & holiday master`
- **Input:** prioritized batch list (Rule 3) + Rules 4/5/7 + masters
- **Output:**
  - the schedule `{batch, process, machine, start, end}`, plus
  - a **machine-wise view** derived from it: each machine's ordered queue (with
    an *idle-before* column = working minutes the machine waited) and a
    **utilization** summary (busy / idle-within-span / utilization %) — this is
    how you see that machines run continuously.
  - Drives `Planning status monitoring`, `Machinewise`, `Weekly Production plan`.

**Downtime loop-back (`apply_downtime_to_plan`, default on in the UI).** Recorded
Rule 7 actuals feed lost machine time back into this allocation: for each entry,
`lost = max(actual setup − planned setup, 0) + total downtime` is attributed to the
machine that entry's process runs on (resolved by the same id Rule 6 schedules on),
**accumulated per machine**, and seeded as that machine's *unavailable* time at the
start of the plan. The machine's whole queue then slips by its lost time — so a power
cut / breakdown / over-long setup pushes the plan later instead of being ignored. The
loss is cumulative across re-plans (total lost so far). Entries whose `process` can't
be matched to a routing step are reported as *unattributed* (not fed in), never fatal.
This affects **placement only** — Rule 3 priority is intentionally unchanged in v1.

---

## PHASE 5 — Execute and re-plan *(closed loop)*

### 🔻 Rule 7 — Capture daily actuals  *(Pipeline stage)*
**After every shift / next morning**, enter the previous period's production via
the **Daily Production Entry** form. Fields captured:

- *Identity (manual):* Date, Shift, SO No, Item Code. *Auto/dropdown:* Item Name
  (auto-prompted from the routing) and Process (dropdown of the item's routing).
- *Output (manual):* Qty Produced, Qty Rejected. **Good qty = produced − rejected**
  is what fulfils the order and drives Rule 8's balance (rejected pieces stay to
  be remade).
- *Actual setting time (manual):* the real setup vs the planned `setup_time_min`.
- *Downtime/loss categories (manual, minutes):* No Power, No Operator, Tool
  Problem, Machine Breakdown, No Load, Other Work — summed per item code into a
  total-downtime rollup for analysis. Plus a free-text Remarks.

- **Source:** original Rule 7 + `Sample entry window format`
- **Input:** the schedule (Rule 6) + manual entry via the Daily Production Entry form
- **Output:** actuals data (durable store) + a per-item-code output/downtime
  rollup shown on the Rule 7 tab

### 🔁 Rule 8 — Re-run MRP and regenerate the plan  *(Loop)*
After actuals are entered, **re-run MRP/refresh**: regenerate the plan from
**actual completed qty + balance remaining**, looping back to Phase 1.

- **Source:** original Rule 8
- **Input:** actuals (Rule 7) + balance remaining
- **Output:** triggers a fresh run starting at Rule 1
- **Implementation note:** realized as the unified **"Plan"** over the persistent
  order book — it emits every active order at its remaining qty (ordered − good
  produced) and re-runs Rules 1–6. "Run" and "Rerun MRP" are one action; there is no
  dedicated rule module for it (the engine's `orderbook.active_so_lines` does this).

---

## Configurable parameters (summary)

| Parameter | Default | Rule |
|---|---|---|
| Consolidation window | 10 days | Rule 1 |
| Setup time per process | 90 min | Rule 4 |
| Operation overlap | always on, 50% (configurable) | Rule 5 |
| Apply downtime to plan | on (UI) | Rule 6 ← Rule 7 |
| Apply operator & shift logic | on (UI) | Rule 6 ← masters |
| Two-shift threshold | 12 hrs (Available Hrs/Day) | Rule 6 |
| Manual / single-shift window | 09:00–18:00 | Rule 6 |
| Split alternative machines in parallel | on (UI) | Rule 6 |
