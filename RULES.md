# Anvitech PPC Engine — Rules in Execution Sequence

> Source of truth for the Production Planning & Control engine.
> Rules are reordered from the `PPC logics` sheet of `Test2.xlsx` into the
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
       │                            ◄── ⚙️ Rule 7 (parallel machine, batch>400)
       │                            ◄── masters (machine / operator / calendar)
       │  the schedule
   🔻 Rule 8  Capture daily actuals
       │  actuals
   🔁 Rule 9  Re-run MRP  ──────────────────────────► back to Rule 1
```

**True forward pipeline:** `1 → 2 → 3 → 6 → 8 → 9 (loop)`
**Consumed inside Rule 6:** Rules 4, 5, 7.

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
  process** is done.

- **Source:** original Rule 6 (sheet rows 19–20, verbatim: "second operation
  shall start after first is fully completed" / "after 50% (or user defined) of
  first operation time is over")
- **Consumed by:** Rule 6 (start-time calculation for each next process)
- **Parameter:** overlap threshold = 50% (configurable)

### ⚙️ Rule 7 — Parallel machine for large batches  *(Parameter / calc — nested in Rule 6)*
If **batch size > 400**, allot a **separate (preferred) machine for the next CNC
setup** so the batch runs in parallel rather than queuing on one machine.

- **Source:** original Rule 5b
- **Consumed by:** Rule 6 (machine selection for large batches)
- **Parameter:** parallel trigger = 400 nos (configurable)

---

## PHASE 4 — Allocate processes to machines *(the actual schedule)*

### 🔻 Rule 6 — Assign each process to the earliest-available preferred machine  *(Pipeline stage)*
Allot each process to the **earliest-available machine from its
preferred/suggested list** — respecting:
- the working calendar (**Thursdays off**, holidays, operator leaves), and
- **shift timings** (1st: 8am–7pm, 2nd: 7pm–5am; operators swap shifts every Friday).

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

While running, Rule 6 consumes: ⚙️ Rule 4 (setup time), ⚙️ Rule 5 (overlap mode),
⚙️ Rule 7 (parallel machine).

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

---

## PHASE 5 — Execute and re-plan *(closed loop)*

### 🔻 Rule 8 — Capture daily actuals  *(Pipeline stage)*
**After every shift / next morning**, enter the previous period's production via
the **Daily Production Entry** form. Fields captured:

- *Identity (manual):* Date, Shift, SO No, Item Code. *Auto/dropdown:* Item Name
  (auto-prompted from the routing) and Process (dropdown of the item's routing).
- *Output (manual):* Qty Produced, Qty Rejected. **Good qty = produced − rejected**
  is what fulfils the order and drives Rule 9's balance (rejected pieces stay to
  be remade).
- *Actual setting time (manual):* the real setup vs the planned `setup_time_min`.
- *Downtime/loss categories (manual, minutes):* No Power, No Operator, Tool
  Problem, Machine Breakdown, No Load, Other Work — summed per item code into a
  total-downtime rollup for analysis. Plus a free-text Remarks.

- **Source:** original Rule 7 + `Sample entry window format`
- **Input:** the schedule (Rule 6) + manual entry via the Daily Production Entry form
- **Output:** actuals data (durable store) + a per-item-code output/downtime
  rollup shown on the Rule 8 tab

### 🔁 Rule 9 — Re-run MRP and regenerate the plan  *(Loop)*
After actuals are entered, **re-run MRP/refresh**: regenerate the plan from
**actual completed qty + balance remaining**, looping back to Phase 1.

- **Source:** original Rule 8
- **Input:** actuals (Rule 8) + balance remaining
- **Output:** triggers a fresh run starting at Rule 1
- **Implementation note:** realized as the unified **"Plan"** over the persistent
  order book — it emits every active order at its remaining qty (ordered − good
  produced) and re-runs Rules 1–7. "Run" and "Rerun MRP" are one action; there is no
  separate `rule9` module (the engine's `orderbook.active_so_lines` does this).

---

## Configurable parameters (summary)

| Parameter | Default | Rule |
|---|---|---|
| Consolidation window | 10 days | Rule 1 |
| Setup time per process | 90 min | Rule 4 |
| Operation overlap mode | Sequential / 50% overlap | Rule 5 |
| Parallel machine trigger | batch size > 400 | Rule 7 |
