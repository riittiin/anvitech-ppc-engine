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
Add **90 min setup time** on top of (cycle time × qty) when computing how long a
process occupies a machine — but **only for CNC/VMC machining** (the setup models the
time to program/set the machine). Manual/finishing steps (washing, deburring, packing,
inspection, drilling/chamfer, bandsaw, manual lathe) need no such setup and occupy
their station for run time alone. A machine is CNC/VMC by id (`CNC*`/`VMC*`) or by its
Machine-master type (CNC lathe / Vertical Machining center).

- **Source:** original Rule 5a
- **Consumed by:** Rule 6 (machine occupancy calculation); see `rule6_allocate._is_setup_machine`

### ⚙️ Rule 5 — Operation overlap mode  *(Parameter / calc — global toggle per plan run)*
Provide a two-option selection:
- **(a) Sequential** — a process starts only after the previous process is
  **fully completed**, OR
- **(b) Overlap** — a process starts after **50% (user-defined) of the previous
  process's _cutting time_** is done.

**What the 50% measures (data-confirmed decision).** Machine occupancy =
*cutting time* (cycle × qty) **+ a 90-min setup** (Rule 4, CNC/VMC steps only). The source rule says
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

**A step can START early but cannot FINISH before its predecessor delivers the
last piece (pacing).** Overlap lets a step begin after 50% of the previous step's
cutting — but a step processes pieces only as fast as its predecessor hands them
over. A **fast** step after a **slow** one (e.g. INSPECTION on 3 stations after
WASHING on one) is *starved*: it starts early (pipelined) yet its completion is
**held to its predecessor's end**, finishing just after — never before. Without
this, a fast step would appear to finish before the slow step delivered its last
pieces, and those pieces would be dispatched having skipped it. So each step's END
is ≥ every earlier step's END (ends are monotonic through the routing); its span
grows (idle waiting for pieces) while its work — cutting × qty + setup — is
unchanged. The overlap *start* and the schedule's compression are preserved; only
the impossible early *finish* is corrected.

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

**Time basis.** A process's machine time = **cycle time × qty** (+ the 90-min setup
for CNC/VMC steps only, Rule 4). The Process "Total time" column in the master is **never** used.

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

**Expedite window (`expedite_window_min`, default 0 = off).** Pure non-delay
(above) breaks ties only when two operations can start at the *exact* same instant;
when the earliest-startable op is a few minutes ahead, it wins even if a **more
urgent** order's op is right behind it — so an overdue order can keep losing every
near-race for a shared machine/operator and finish weeks late. The expedite window
softens this **without ever idling a resource**: among the operations that could
start within `expedite_window_min` minutes of the *earliest* startable one, the
scheduler picks the one with the **least slack** (dynamic slack = working time to
its SO delivery date − work it still needs), rather than the one that is merely a
few minutes earlier. With the window at **0** the behaviour is exactly the legacy
non-delay tie-break (byte-identical plans). A small window (≈45 min) pulls the
worst-stuck urgent orders forward by days while never pushing a comfortably on-time
order late — it only redistributes machine/operator time *among orders competing at
nearly the same moment*. It is a tie-break refinement, not a reordering: no machine
ever waits for a not-yet-ready op.

**Alternative ("preferred") machines.** A process's *Suggested M/c* cell may list
**alternatives separated by `/`** (e.g. `CNC3/CNC6` = run on either CNC3 or CNC6).
The scheduler picks the **earliest-available** of the allowed machines, so work
**load-balances** across them automatically (once one is taken it's busy, so the
next contending operation grabs the other). On a tie the **first-listed** machine
wins (it's the preferred one), keeping plans deterministic. A listed machine not
yet in the Machine master is registered as a provisional machine and still used.
This applies to **any** alternative cell — CNC, inspection (`MI1/MI2/MI3`), etc.

**Suggested vs Allotted, and what the parallelization toggle spans.** The *Suggested
M/c* cell lists every machine the item is **capable** of using; the *Allotted M/c* cell
is the machine(s) actually **allotted** for the step. Which set the scheduler may use
depends on the parallelization toggle (`split_parallel`):

- **Toggle OFF** → the step uses its **Allotted** machine(s) only (the planned choice).
  If the Allotted cell is blank, it falls back to the Suggested machine(s) so the step
  still schedules.
- **Toggle ON** → the step may use the **union of Allotted + Suggested** (Allotted
  first) — every capable machine — so work load-balances and (for batches over 400)
  splits across all of them.

So with the toggle off, an item with Allotted `CNC4` and Suggested `CNC3/CNC6` runs only
on `CNC4`; with it on, it may run on `CNC4`, `CNC3` and `CNC6`. Ties prefer the Allotted
machine (it is listed first). This is independent of OS/DISPATCH handling, which is
decided before machine selection.

**Parallel split (`split_parallel`, default on in the UI) — LARGE batches only.**
When a step lists alternatives, the engine **splits the quantity across the machines the
toggle exposes (Allotted only when off, Allotted+Suggested when on) to finish
the step as early as possible** — but **only for batches over 400 pieces**
(`split_min_qty = 401`). Each candidate gets the load it can complete by a common target
finish time, counted **from when it becomes free** — so a machine that's busy now but
frees soon still takes a (smaller) share, and a faster machine takes more, so both halves
finish together. It splits **only when that beats the single best machine**. The next
process waits for the slowest half (split-then-recombine). Same logic for any alternative cell.

A batch of **400 or fewer** is **not split** — splitting a small job would waste a second
90-min setup for little gain. Instead the whole batch runs on the **single least-queued
(earliest-free) alternative**, so small jobs still load-balance across the alternatives
(e.g. two 10-piece inspection lots land on MI1 and MI2 by whoever's free), just without
the extra setup.

**Off-machine steps — DISPATCH vs OS / outsourcing.** A process that runs off any
in-house machine is handled in one of two ways:

- **DISPATCH** (the final "consider it done / shipped" gate) and any other step with
  **no machine and no cycle time** → a **zero-duration milestone** on the
  "Off-machine" lane (or "OS / Outsourced" if its name has an `OS` word). It consumes
  no machine, operator or time. The dispatch gate is matched tolerantly — `DISPATCH`,
  `Dispatch`, and the real-data misspelling `DISAPTCH` all count.
  **DISPATCH waits for the WHOLE order.** Because the Rule 5 overlap can let a *later*
  fast step finish before an *earlier* long step (e.g. INSPECTION on 3 stations finishing
  before WASHING on one), the dispatch milestone is placed at the **latest end across all
  of the batch's processes**, not at its immediate predecessor's overlap point — so an
  order is "dispatched" only once **every piece has cleared every process**. (The overlap
  rule itself is unchanged for every real step; only the dispatch gate is held back.)
- **OS / outsourcing with a turnaround time** — a step marked `OS` in its Allotted (or
  Suggested) machine cell, carrying a **cycle-time value in minutes** (e.g. `7200`).
  This is scheduled as a **reserved continuous block**: it holds that many minutes of
  vendor turnaround **flat per batch** (NOT × qty, no 90-min setup), runs **continuous
  24×7** (it ignores Anvitech's Thursday-off and shift hours — the vendor works on its
  own clock), takes **no in-house machine or operator**, and has **unlimited parallel
  capacity** (any number of orders can be at OS at once). An OS step is **fully
  sequential on both sides**: its in-house **predecessor runs to 100% completion before
  the block starts** (no overlap *into* an OS step — you can't ship parts that aren't
  machined yet, so all of process 1 finishes whether it's 20 or 1000 pcs, regardless of
  the overlap toggle), and the next process **waits for the full block to finish** (no
  overlap *out of* it). Shown as an `OS` bar on the "OS / Outsourced"
  Gantt lane; kept out of the machine-utilization table (it is not a machine). If the
  cycle-time cell is **blank**, the step is a zero-duration milestone until a number is
  entered — then it reserves that block automatically, no code change. An OS step that
  arrives early is closed via Capture Actuals (its per-process remaining hits 0 and the
  next Plan skips it). A blank machine *with* a real cycle time is still NOT off-machine
  — it surfaces as "needs machine" so genuinely missing data fails loud.

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
coverage gate and keep a two-shift window.

**Operators are one-at-a-time resources (load-balanced).** Each scheduled operation is
assigned a specific person — the **earliest-free** qualified operator for that machine
and shift — and that person is then busy until the op ends. So work **spreads across the
whole crew** instead of always landing on the first-listed name: with four interchangeable
helpers (HP1–HP4) the load divides four ways, and an operation **waits for a free person**
when all qualified operators are busy, capping concurrency at the real headcount (e.g. at
most 4 helper jobs at once, not 5). On a tie (everyone free) the first-listed operator
wins, keeping plans deterministic. Parallel-split siblings get **distinct** people (one
person can't run two machines at once). Inspectors (MI1/MI2/MI3, one per station) are
naturally balanced; CNC/VMC operators and helpers now share load. The assigned person
shows in the `Operator` column on the Rule 6 schedule (present only when operator logic is
on). With operator logic **off**, no person is assigned and concurrency is machine-limited
(unchanged).

**Continue from reality — per-process remaining (the feedback loop).** Every ordered
piece must pass through every process, so a piece has *cleared* a step once it is
punched as good there (Rule 7). When a re-plan runs, each process is scheduled for
its **own** remaining = **ordered − pieces already done at that step** (not the whole
order through every step). Earlier steps have more done, so they schedule fewer
pieces; the finished-goods gate (last/DISPATCH step) has the most. A step already
**fully** produced (remaining 0) is **skipped** — finished work is never re-run, and
its pieces are ready for the next step. So after a day's punching, the plan picks up
**where the floor actually is** instead of restarting the routing. (With no recorded
progress, every step runs the full order qty — today's behaviour, byte-identical.)
This is the order book's `process_qty` carried through Rule 1's consolidation into
Rule 6; the finished-goods gate is just this rule applied to the **last** process.

While running, Rule 6 consumes: ⚙️ Rule 4 (setup time) and ⚙️ Rule 5 (overlap mode).

**Balance operator workload (`balance_operator_load`, default off).** A
**schedule-neutral** fairness pass applied *after* the plan is built. The first-listed
tie-break can pile work on the early-named operators (e.g. one at 106% while an equally
qualified peer sits idle). When on, each already-scheduled operation is reassigned to
the **qualified, same-shift operator who is free at that moment and has the least work
so far** — spreading load evenly across interchangeable people. It **never changes any
start/end time**, so **makespan and lateness are provably unchanged** (only *who* runs
each job changes). It **never double-books a person**: after the fairness walk, a
repair pass reverts reassignments until no operator holds two overlapping ops (the
walk's "keep the original when nobody is free" could otherwise collide with the walk's
own earlier reassignments — found live 2026-07-15, one person on CNC1 + VMC3 at once;
regression: `tests/test_operator_invariants.py`). Measured on Test5: peak operator
106%→97%, spread (stdev) 23→16, with an identical schedule. See
`rule6_allocate._rebalance_operators`.

**Operator load can never exceed 100% (owner guarantee), and uncovered work is
surfaced as UNSTAFFED.** The shift-wise view assigns each per-shift segment to a
qualified person who is actually **free** at that moment; when a shift runs more
machines than it has qualified people (e.g. three VMCs overnight with two night-shift
VMC operators), the extra segment is marked **`⚠ Unstaffed`** instead of being billed
to an already-busy person (that double-billing pushed operators to 107% live).
Analytics rolls those segments into a headline **"unstaffed hours"** number with an
explanatory note — the plan itself is untouched (reporting only); the note tells the
owner where extra shift crew is genuinely needed.

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
  - an **Analytics view** (`engine/analytics.py`): utilization & bottlenecks from the
    plan — each machine/operator/process measured against **its own** available time in
    the plan window (busy ÷ available), so a single-shift manual station and a two-shift
    CNC are judged fairly. Flags bottlenecks (≥85%) and under-used resources (≤30%).

**Recorded times are NOT fed into the plan.** The downtime categories (No Power, No
Operator, Tool Problem, Breakdown, …) and the actual setup time are captured and
stored **for the record only** (shown in the Rule 7 rollup). They never affect the
schedule — the feedback loop is driven **purely by quantity produced/rejected per
process** (the director's spec). A power cut or over-long setup is logged for
analysis but does not move the plan.

### Order commitment (lanes) & two-pass promise protection *(feature)*

Every order occupies exactly one **commitment lane**:
- **Open** (default) — new or not yet promised; schedules after all protected work
  into remaining machine/operator capacity.
- **Committed** — promised to a client; locked at its **current expected completion
  date**. Scheduled in the protected group, ordered by `promised_date` (first promised,
  first served).
- **Urgent** — must hit a specific delivery date (its SO delivery date). Scheduled in
  the protected group, ordered by that date (just high enough to meet it).

**Two-pass planning enforces the promise.** Pass 1 schedules the **protected orders
(Committed + Urgent)** in isolation via the unchanged Rules 1–6, producing their
schedule as if Open orders do not exist. Pass 2 runs Rules 1–6 on **Open orders only**,
with machine/operator intervals reserved from Pass 1's schedule, so Open ops backfill
idle windows but **never overrun a committed block**. New Open orders thus cannot
push a promised order past its date — the protected schedule is frozen. An **Urgent
order's promised date** is its SO delivery date; a **normal Commit snapshots** the
order's current expected completion at commit time. The admin may **Uncommit** an
order (returns it to Open) or view a **warning preview** before marking an order
**Urgent**, showing any committed orders it would push past their promise.

**Rule 6 reservation support (engine-side change).** Rule 6 gains an optional
`reserved={machine|operator: [(start,end), …]}` argument: when placing an operation,
skip windows that overlap a reservation and reject placement that would not finish
before the next reserved interval. `reserved=None` (Pass 1 and all existing callers) is
byte-identical to today.

**Consolidation guard:** orders of the same item are merged only when they share the
same lane and (for protected lanes) the same promised date — a batch must not
straddle lanes. Cross-lane orders stay separate.

**Default:** all orders are Open; with no committed orders the plan is byte-identical
to today (no change in behaviour).

### Promise recovery — automatic committed re-sequencing after a disruption *(feature)*

When a disruption (a worker absent, a machine down) makes committed orders slip past their
promises, scheduling them in strict promised-date order is measurably sub-optimal: it
serves the earliest promise even when that needlessly breaks several others. Measured on a
real disrupted book, **re-sequencing the committed set recovers ~a third of the promise
damage** (e.g. a lost week: 344 → 242 promise-slip-days, 55 → 44 broken; a lost fortnight:
637 → 511). This is done **automatically — no setting, no button**:

- When Pass 1 (committed/urgent) shows any order finishing past its promise, the planner
  kicks off a **background** search (`optimizer.optimize(..., objective="promise_slip")`)
  that minimises total promise-slip (broken-promise count as tiebreak) on the committed set.
- The result — a committed order **rank** — is persisted (`anvitech:promise_recovery`) and
  **replayed on every Plan** (planned expedite-off so the order takes effect), until the
  committed set or its promises change (a new commit/uncommit or changed promise re-triggers;
  a mere feedback quantity change does not — the ORDER still replays).
- **Safety:** the search is seeded with the promised-date order and keeps the best, so the
  recovered plan is **never worse** than today's date-order. Deterministic. No committed
  slip → no search → byte-identical to today (golden untouched).
- **How it works (the logic):** it re-orders which committed job claims each shared machine
  next. An order with slack is made to wait, yielding its machine slot to an at-risk order;
  the slack order still makes its promise, the at-risk one is saved. It **redistributes** the
  unavoidable slip to protect the most promises — it never invents lost capacity back.
- **Equal weighting (owner decision):** every promise counts the same; **no per-order
  "critical" flag** (kept off the surface for non-technical floor users — a truly critical
  order is handled by marking it Urgent). A quiet Orders-tab note reports how many promises
  were protected; there is no control to operate.

### Optimize plan (sequence search) *(feature)*

Rule 6 is a greedy, single-pass scheduler: it builds exactly ONE plan, and the batch
sequence it consumes (Rule 3's order) is worth days of makespan by itself — measured on
the real book, better sequencing alone cut the plan from 42.5 to 39.7 days and total
late-days from 1,026 to 792. Because one full plan evaluates in under a second, the
**Optimize** button (admin) searches instead of trusting one pass: it tries many batch
sequences on the *current* order book, replays the **unchanged Rule 6** for each, scores
every plan (`total_late_days + 10 × makespan_days` — **delivery gaps dominant**, the
owner-chosen priority: favour fewest/smallest late deliveries over the very shortest
finish, because on the real book the shortest-finish plans push *more* orders late), and
keeps the best.

- **Multi-start search.** A single hill-climb from one seed gets trapped in whatever local
  optimum it first descends into (measured: it stalled at 39.75 d / 778 late-days on
  Test5, and adding plans didn't help). So the search runs **many independent restarts** —
  the strong dispatch heuristics (SPT, ATC) then an unlimited stream of fresh random
  permutations — hill-climbs each with insertion/swap/block moves until it stalls
  (`_RESTART_AFTER`), and keeps the **global best** across all restarts. This reliably
  reaches better basins (Test5: → 39.7 d / **713 late-days** / 44 late). Fully
  deterministic (every restart's RNG is seeded off the base seed); never skips a run.
- **Quick** ≈ 150 plans tried; **Deep** ≈ 400. Budgets are **evaluation counts** with
  a fixed random seed, so the same book + settings + budget always yields the same
  result on any machine. The scheduler is memoized (invariant machine-name parsing and
  per-day work-windows are cached), so each plan evaluates ~3.5× faster with identical
  results.
- **Settings sweep (2026-07-15).** The same click also auto-tunes the **overlap %**
  (`optimizer.sweep_optimize`): candidates 50–100 step 10 plus the current value, all
  inside the SAME eval budget — half probes every candidate identically, half deepens
  the winner. The current setting is probed first and only a **strictly better** score
  dethrones it (never worse; no setting churn on ties). With committed/urgent orders,
  a candidate overlap must first prove — on the committed pass alone — that promise
  slip and broken-promise count are **no worse than under the current setting**
  (the promise guard; vetoed candidates are skipped). Apply persists the winning
  overlap **into the saved plan config** (openly visible in Settings) alongside the
  ranks, and the staleness fingerprint is computed against the winning settings.
  Result panel reports "Best setting found: Overlap X%". Spec:
  `docs/superpowers/specs/2026-07-15-optimize-settings-sweep-design.md`.
- The admin sees a before/after table and chooses **Apply** or Discard. Apply persists
  a **rank per (SO No, Item Code)**; every subsequent Plan replays it
  (`pipeline.apply_priority_rank`): ranked batches reorder among the slots they already
  occupy, **unranked (new) orders keep their natural Rule-3 slot** — a fresh urgent
  order is never pushed to the back. A banner flags how many orders were added since
  the last optimization ("re-optimize for the best plan"); **Remove optimization**
  reverts to the pure Rule-3 order.
- **Promises stay sacred:** with committed/urgent orders present, only the OPEN pass is
  searched, against the protected pass's reservations — an optimized plan can never
  move a promised date. All-open books search the whole book.
- **Replay guarantee:** feeding the saved ranks back through the pipeline reproduces
  exactly the metrics the search reported (tested) — **for the same inputs**. The
  applied result carries a **fingerprint of the masters workbook + the plan-shaping
  settings** it was computed on (`inputs_sig`); when the owner later re-uploads an
  edited workbook or changes Settings, the replayed numbers legitimately differ, and
  the banner says so ("Settings or masters have changed since this optimization —
  run Optimize again") instead of looking non-deterministic (live 2026-07-15 finding:
  "the same Deep run gave two results on two days" — the masters had changed between
  them). Schedule-neutral knobs (balance workload; expedite, which is forced off under
  ranks) are excluded from the fingerprint.
- **Default: off.** With no applied optimization every plan is byte-identical to today
  (golden trace unchanged).

---

## PHASE 5 — Execute and re-plan *(closed loop)*

### 🔻 Rule 7 — Capture daily actuals  *(Pipeline stage)*
**After every shift / next morning**, enter the previous period's production via
the **Daily Production Entry** form. Fields captured:

- *Identity (manual):* Date, Shift, SO No, Item Code. *Auto/dropdown:* Item Name
  (auto-prompted from the routing) and Process (dropdown of the item's routing).
- *Output (manual):* Qty Produced, Qty Rejected, **at the named Process**. **Good qty
  = produced − rejected** for that step. Good at the **finished-goods gate** (the
  DISPATCH step, or the last step if the routing has no DISPATCH) is what **fulfils
  the order** and reduces its remaining qty. Good at an **earlier** step is
  **work-in-progress**: it is recorded (and lets the next Plan skip that finished
  work — see Rule 6 "continue from reality"), but it does **not** reduce the order's
  remaining qty — those pieces still owe every later step. (This fixed a bug where
  any step's production was wrongly counted as finished goods.)
- *Actual setting time (manual):* the real setup vs the planned `setup_time_min`.
- *Downtime/loss categories (manual, minutes):* No Power, No Operator, Tool
  Problem, Machine Breakdown, No Load, Other Work — summed per item code into a
  total-downtime rollup for analysis. Plus a free-text Remarks.

- **Source:** original Rule 7 + `Sample entry window format`
- **Input:** the schedule (Rule 6) + manual entry via the Daily Production Entry form
- **Output:** actuals data (durable store) + a per-item-code output/downtime
  rollup shown on the Rule 7 tab
- **One day at a time:** the "Saved entries" list shows only the **latest punched
  date's** entries, and **only those can be rolled back** (server-enforced); once a
  newer date is punched the previous day locks and drops off the list. This keeps the
  list from growing without bound across 20–50 SOs × processes × days. Every entry
  stays permanently in the record and in the per-item rollup (which sums **all**
  entries) — only the editable *list* is scoped to the current day.

### 🔁 Rule 8 — Re-run MRP and regenerate the plan  *(Loop)*
After actuals are entered, **re-run MRP/refresh**: regenerate the plan from
**actual completed qty + balance remaining**, looping back to Phase 1.

- **Source:** original Rule 8
- **Input:** actuals (Rule 7) + balance remaining
- **Output:** triggers a fresh run starting at Rule 1
- **Implementation note:** realized as the unified **"Plan"** over the persistent
  order book — it emits every active order at its remaining qty (ordered − **finished**
  good at the gate) **plus its per-process remaining** (`process_qty`), and re-runs
  Rules 1–6. So the re-plan is **dynamic**: it reflects exactly the quantity the floor
  punched in per process, continues from there, and refreshes the
  schedule, machine allotment, Gantt and Orders tab everywhere. "Run" and "Rerun MRP"
  are one action; there is no dedicated rule module (`orderbook.active_so_lines` does this).
- **Plan clock advances past completed days** (`orderbook.effective_plan_start_date`):
  once a day's production is punched, the re-plan starts from the **next working day's
  first shift** after the latest actual's date — that day is done and over. So punching
  1 March's output makes the remaining work start **2 March 08:00**, not restart from
  1 March. It never moves earlier than the configured `plan_start_date`, and skips
  weekly-off/holiday days. With no actuals recorded, the plan starts from the configured
  date exactly as before (golden trace unchanged).

---

## Configurable parameters (summary)

| Parameter | Default | Rule |
|---|---|---|
| Consolidation window | 10 days | Rule 1 |
| Setup time per process (CNC/VMC only) | 90 min | Rule 4 |
| Operation overlap | always on, 50% (configurable) | Rule 5 |
| Apply operator & shift logic | on (UI) | Rule 6 ← masters |
| Two-shift threshold | 12 hrs (Available Hrs/Day) | Rule 6 |
| Manual / single-shift window | 09:00–18:00 | Rule 6 |
| Split alternative machines in parallel | on (UI) | Rule 6 |
| Expedite window (least-slack tie-break) | 0 min = off (engine) | Rule 6 |
| Balance operator workload (schedule-neutral) | off (engine) | Rule 6 |
