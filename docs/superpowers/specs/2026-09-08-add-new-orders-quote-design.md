# Add New Orders: quote a delivery date instead of guessing one

**Date:** 2026-09-08
**Status:** design approved by the owner (sections 1–5, 2026-09-08), not yet implemented
**Owner request:** new orders arrive on a rolling basis. Today a director invents an SO
delivery date, types it into the Excel, and it is uploaded. The date owes nothing to
what the shop is already committed to. Replace the guess with a date the software
computes against the plan that is actually running.

---

## 1. The problem, in the owner's words

> The Anvitech directors would just guess a random date, add that as the SO delivery
> date, add that to the Excel, and I add that Excel into the software application.
> There is a high chance that the SO delivery date is kind of random, and it does not
> look towards the existing order.

The engine already knows exactly how loaded every machine is. Nobody asks it before
promising a customer a date. Everything downstream — lateness, the delay report, the
directors' view of whether the shop is on time — is measured against a number that was
made up.

**This is not a scheduling problem. It is a promise problem:** the number the customer
is told must come from the plan, not from a guess, and it must be knowable *before* the
order exists.

---

## 2. Decisions taken (owner, 2026-09-08)

| Question | Decision | Why it was asked |
|---|---|---|
| What does "do not disturb the existing orders" mean? | **Every existing order's expected completion date reads exactly the same before and after — and its machines and operators too.** | Everything else in the design follows from the strength of this promise. Anything weaker permits an existing order to move, which is the thing the owner is protecting. |
| May a new job be split across several idle gaps on one machine? | **No.** A job takes a gap only if it fits whole; otherwise it waits for the next gap big enough, or runs after the machine's last existing job. | Each re-engagement on a CNC/VMC pays the 90-minute setup again. Owner: *"it will require that same 90 minutes of setting time, which would take more time in general."* Crossing a night or the weekly off is **not** a split — that is what a job does today. |
| Does pressing "Finish and Optimize" write anything to the book? | **No.** It is a quotation. Nothing exists until the director accepts. | A half-considered order must not reach the floor's plan. |
| What may the quick (frozen) search vary? | **The order of the new orders among themselves, and which machine each new job uses** (Allotted, plus Suggested from the Item's Process Master). | Both are invisible to existing orders, so both are free. Machine choice matters: if the usual machine is booked for three weeks and a sister machine is free, the new order should be able to take it. |
| The director rejects the quoted date and moves it earlier — is that date a hard requirement? | **A target.** It becomes the new order's SO delivery date, the whole book is re-optimized with everything equal, and the app reports what it actually achieved plus every existing order that moved. | A hard requirement refuses too often on a loaded shop and returns nothing usable. A target reuses the objective the optimizer already has (miss delivery dates as little as possible) instead of bolting a new rule onto it. |
| Does accepting a **preponed** result adopt that plan? | **Yes** — same Apply that follows a deep search today. | The whole answer depends on the new sequence. Not adopting it would mean the slip figures shown were from a plan that was then thrown away. |
| Does accepting a **frozen** quote adopt anything? | The orders are saved, and **new orders queue behind the orders already in the book** until the next full optimization. | See §5. Without this the app re-sorts the whole book by delivery date on the very next recalculation, and the date the director was quoted sixty seconds ago changes on screen. |
| A new order for an item already in the book — club it (Rule 1) or not? | **Not clubbed while it is queued.** It runs as its own batch. The next morning's optimization clubs it normally. | Clubbing changes the existing batch's quantity, which moves the existing order's date — forbidden by the first decision. This is forced, not chosen. |
| A later Excel re-import carries that SO with a guessed date — who wins? | **The Excel wins, exactly as today.** | Owner's call. No new precedence rule is introduced; `merge_upload` is untouched. |
| When does the queue rule end? | **The floor's "Done entering — update plan", an admin deep search, or a new Excel upload.** | All three are full re-optimizations or full book changes; holding arrival positions past them would contradict their result. |
| Who can use the tab? | **Admin only**, server-enforced. | The floor shares one login. Adding orders and accepting delivery dates is a director's decision, like uploading the Excel. |
| Extra fields on the entry form? | **None.** SO number, item, quantity. | Owner's call — keep the form to what the plan actually consumes. |
| A duplicate (SO number, item)? | **Blocked**, naming where it already is. | That pair is the identity of an order everywhere in this system. A second one would overwrite the first, including its recorded production. |

**Scale:** up to about 15–20 new orders in a week; a single sitting is usually 1–3 but
may be larger. The quick search must be exhaustive for small batches and bounded above
that.

---

## 3. What the director sees

A new tab, **`Add New Orders`**, admin only, beside Orders / Schedule / Gantt / Daily
Entry / Analytics / Settings.

### Step 1 — enter the lines

| SO number | Item | Qty | |
|---|---|---|---|
| *typed* | *dropdown: code — name* | *typed* | ✕ |

The item dropdown is built from the **Item's Process Master** and lists only items that
have a routing — an item with no routing cannot be scheduled at all. Typing filters it
(~85 items). A duplicate (SO number, item) is refused on the spot:

> *26-27SO150 / 2109801 is already in the book — 300 pieces, delivery 12-Sep.*

### Step 2 — "Finish and Optimize"

Seconds. Nothing is written to the book.

### Step 3 — the quote

> **26-27SO201 · 2109801 · 400 pcs → finishes 20-Sep**
> **26-27SO202 · 61240807-01 · 150 pcs → finishes 24-Sep**
>
> ✅ All 68 existing orders keep the completion date they have today.

**Add these orders** · **I need one earlier** · **Discard**.

### Step 4a — accept

The orders enter the book with the quoted date as their **SO delivery date**.

### Step 4b — "I need one earlier"

Each line gets a date box pre-filled with its quoted date. He edits the ones he cares
about, then confirms a warning he cannot miss:

> *This drops the freeze. Existing orders will be re-planned along with the new ones
> and their dates can move. The search takes 10–30 minutes.*

Then the existing deep-search progress bar, with **Stop & keep best**. The result shows
the prize **and** the damage:

> **New orders**
> 26-27SO201 — asked 12-Sep · **achieved 12-Sep** ✅
> 26-27SO202 — asked 18-Sep · **achieved 20-Sep** — 2 days late
>
> **Existing orders that moved: 6 of 68** (worst first, earlier moves listed too)
> 26-27SO113 — 04-Sep → 09-Sep (**5 days later**)
>
> Total late days across the book: **3,217 → 3,344**

**Accept this plan** saves the orders *and* applies that plan. **Cancel** changes
nothing and leaves the frozen quote on screen.

**The date he typed becomes the SO delivery date**, not the date the search achieved.
His promise to the customer is the promise; the expected completion is whatever the
plan delivers, and the order shows as late if it misses — exactly like every other
order in the book.

---

## 4. How the freeze works

The plan is computed in **two stages**.

**Stage 1 — the existing book, exactly as today.** The same calculation that runs now:
same orders, same punches, same settings, same applied ranks, same in-progress freeze.
The new orders are not in it.

**Stage 2 — the new orders, into what is left.** Stage 1's result is turned into a
statement of what the shop is already doing — per machine and per operator, the exact
minutes already occupied — and the new orders are planned against a shop that is
already that busy.

**Existing orders cannot move because they are not in the second calculation.** This is
a guarantee by construction, not an assertion to be tested and hoped for: there is no
code path in stage 2 that can reach a stage-1 placement.

### Stage 2 invents no rules

It goes through the same scheduler. A new order still obeys its routing order, still
pays the CNC/VMC setup, still needs a person who is qualified for that machine **in
Settings** and rostered on that shift, still respects the weekly off, holidays,
operator absences and machine maintenance. The only difference is that machines and
people are already partly occupied.

**Occupancy enters through the gates that already exist:**

* **Machines** — through `ppc_engine.worktime.iter_windows`, the single window source
  the main decode loop and both frozen-op paths already share, and the same gate the
  2026-08-31 maintenance feature used. No new placement path is created, so all three
  paths are covered by construction.
* **Operators** — through `StaffingBoard`'s existing per-operator busy-interval list.
  `free_during` and `candidate_operator` already read it; seeding it changes no logic,
  so qualification, shift and scarce-first picking are untouched.

### The gap rule

A new job takes an idle chunk on a machine **only if the whole job fits inside it**. If
CNC3 is free from the 5th to the 15th and the job needs six days, it goes there. If it
needs twelve, it does not start and get interrupted — it waits for the next chunk big
enough, and in the worst case runs after CNC3's last existing job. A job is **never**
split across two chunks, so it never pays the setup twice. Running through a night or
across the weekly off is not a split.

Mechanically: the placement helpers gain a stop line ("do not run past the next
existing job") and the placement step tries each free chunk in turn. Both are inert
when there is no occupancy, which is every existing path.

### The quick search

Two dimensions, both invisible to existing orders:

* **Sequence** of the new orders among themselves. **Exhaustive for 4 or fewer**
  (at most 24 arrangements — so the answer is provably the best available under the
  freeze); a bounded hill-climb above that, seeded with the order they were typed in.
* **Machine set** for the new orders: Allotted-only, and Allotted + Suggested (the
  existing `flexible_machines` dimension), keeping whichever is better. **Stage 1 is
  always planned at whatever setting the live plan currently uses** — the machine-set
  choice applies to stage 2 only, so it cannot reach an existing order.

Scored on the new orders' own completion dates (earliest first; against the typed
target once §3 step 4b has supplied one). Seconds either way; a progress indicator
appears only if a batch is large enough to need one.

**The date shown is `optimizer.expected_completion`** — the one definition of "when
this order finishes" that the Orders tab, the Gantt and the delay report already share.
The quote may not derive its own.

### Two honest consequences

1. A new order for an item already in the book runs as its **own** batch, not clubbed.
   The floor may see the same part twice until the next morning's optimization.
2. Stage 2 only ever gets the leftovers, so a frozen quote can be considerably later
   than the shop could really manage. That is the price of not moving anything, and it
   is what the "I need one earlier" button exists for.

### A check, not just a promise

Every quote compares each existing order's completion date before and after and reports
*"all 68 existing orders keep their current completion date"*. If even one moved, the
quote **refuses to show a date** and says so — the same style as the existing
routing-order, qualification and batch-quantity checks, which report rather than trust.

---

## 5. First come, first served — why the queue exists

**The application never stores a plan.** It stores orders and production punches, and
recalculates the plan from them every time a screen is opened. That single fact is why
the queue is needed, and it is the only reason.

The owner's rule, in his own words:

> First, SO number 1 with item A gets added. It gets optimized, then it's added, and
> then it's in the live plan. When item number B with SO number 2 comes, item number A
> is already there, so the entire plan should calculate with item number A being there.
> It's like a first-come, first-served basis.

Left to itself the app does **not** do that — it re-sorts the whole book by delivery
date and slack on every recalculation. So order 1's date could move the moment order 2
is added, which is exactly the failure this feature exists to remove.

**The queue is the app remembering arrival order.** It is one stored list of (SO
number, item) pairs: *these orders are planned behind the orders already in the book*.
It is not a saved schedule and nothing is frozen in amber — production is punched all
day and the plan updates all day exactly as it does now. Stage 1 absorbs the day's
punches like always; the queued orders re-fit behind whatever stage 1 becomes.

**The queue ends** at the next full optimization: the floor's "Done entering — update
plan", an admin deep search, or a new Excel upload. It also clears itself when its
orders are deleted or completed. After that the new orders are ordinary orders, ranked
on merit like everything else, keeping the quoted date as their SO delivery date
permanently.

**The preponed path creates no queue** — after a full re-optimization every order is
equal by definition.

---

## 6. Architecture

### New code

* **`engine/quote.py`** — pure. Given the current plan's schedule, the masters, the
  config and the new order lines: build the occupancy picture, run stage 2, run the
  small search, return per-line results plus the "nothing moved" verification. No
  store, no HTTP, no side effects.
* **`web/`** — the Add New Orders tab. Hidden from the user role **server-side**, not
  only in CSS (2026-08-09 rule: role gating belongs on the control, and JS-built markup
  needs its own check).

### Engine changes — four, each inert by default

1. **`ppc_engine/domain/calendar.py`** — `ShopCalendar` gains two optional interval
   maps: machine-busy and operator-busy. Empty by default, so every existing plan is
   byte-identical.
2. **`ppc_engine/worktime.py::iter_windows`** — subtracts the machine's busy intervals
   from the windows it yields. The one gate (see §4).
3. **`ppc_engine/scheduler/staffing.py`** — `StaffingBoard` seeds its existing
   per-operator interval list from the calendar. No logic change.
4. **`ppc_engine/scheduler/flow_scheduler.py`** — `_lay_on_machine` / `_lay_frozen`
   gain an optional stop line; `_place_operation` walks free chunks when a machine has
   busy intervals; `decode` seeds the staffing board. All `None`/empty on every
   existing path.

`engine/new_engine.py` gains the translation from a finished plan's entries to that
occupancy picture, and passes it through `_with_unavailability` the way absences and
maintenance already flow.

### App changes

* **`engine/book_store.py`** — two keys: the typed draft lines
  (`anvitech:new_order_drafts`) and the arrival queue (`anvitech:new_order_queue`).
* **`api/main.py`** — draft CRUD; `POST /new-orders/quote`; `POST /new-orders/add`;
  the preponed path re-using `_start_optimize` and `_optimize_apply` unchanged; `_plan`
  running stage 2 when the queue is non-empty; the queue cleared by
  `POST /optimize/done`, `_optimize_apply` and `POST /upload`.
* **The plan cache** must include the queue in `_plan_fingerprint`, or the 2026-08-08
  stale-screen class returns: a change that is real but invisible to the cache.

### Two-stage integration point (called out deliberately)

`_plan` today makes one `run_forward` call, which also builds the per-rule trace. Stage
2 is a second pass over the new lines only (Rules 1–3 then the scheduler, so new orders
club among *themselves* but never with existing ones). The two schedules are merged
before anything downstream reads them, so the Gantt, Schedule tab, shift-wise export,
delay report, analytics and `expected_completion` all see one list and cannot disagree.
The per-rule trace tabs show stage 1 with stage 2 appended and labelled. This is the
one place where care is needed; it is not a place where a second definition of anything
is allowed to appear.

### What is not touched

The scoring/objective, the optimizer's search, Rules 1–6, the delay report, analytics,
the efficiency report, Daily Entry, the in-progress freeze, machine maintenance,
operators and absences, `merge_upload`. If any of their behaviour changes, that is a
bug, and the byte-identical check below is designed to catch it.

---

## 7. Edge cases

| Case | Behaviour |
|---|---|
| Item has no routing | Not offered in the dropdown; if a routing disappears between quoting and adding, the line reports it and blocks the add. |
| An operation has no machine anybody is qualified for | The line reports *"cannot be scheduled: no operator in Settings is qualified for MI3"* instead of silently vanishing (the existing `_op_has_no_runnable_machine` guard is honest, not silent). |
| Quantity ≤ 0, blank, or not a whole number | Refused at entry. |
| SO number blank or whitespace | Refused at entry. Duplicate matching is case-insensitive and trimmed. |
| The plan moved between quoting and adding (a punch, an applied search) | Add is refused: *"the plan has changed since this quote — press Finish and Optimize again."* Owner's own resolution. |
| A search is already running when he presses "I need one earlier" | Refused: one search at a time, the rule the app already enforces. A **quote** is read-only and is always allowed. |
| Server restarts between quoting and adding | The typed lines survive (stored); the quote does not. He re-quotes in seconds. |
| Two admins working at once | The draft lines are shared server state. Documented, not defended against — the same as every other admin control in this app. |
| A queued order is deleted or completed | It drops out of the queue; if the queue empties, it is removed. |
| The new order cannot be placed inside the scheduler's lookahead horizon | Reported honestly as unschedulable rather than given a fabricated date. |

---

## 8. Acceptance criteria

1. **Nothing moved.** Test5 / Test8 / Test9 planned before and after the change, with
   no new orders in play, produce a **byte-identical** entry list (hash compared, not
   eyeballed). The golden trace is unchanged and the full suite passes.
2. **The freeze holds.** On all three books at no / partial / full work-in-progress,
   quoting 1 to 20 new orders leaves **every** existing order's completion date,
   machine and operator identical, with routing-order, qualification and
   batch-quantity violations all at zero.
3. **No job split, no setup paid twice.** No new-order operation is ever interrupted by
   an existing job.
4. **First come, first served.** Adding orders one at a time never moves anything
   already in the book.
5. **The quote equals the screen.** Immediately after accepting, the Orders tab, Gantt
   and shift-wise export show exactly the quoted dates.
6. **The queue clears** on Done entering, on an applied deep search, and on an upload —
   after which the new orders are ranked on merit.
7. **Mutation-tested.** Each part of the change is reverted individually and must fail
   at least one test. Anything that turns out to be belt-and-braces is stated plainly
   as such, never dressed up as covered — this fixture family passes vacuously by
   default (CLAUDE.md).
8. **Live, in a browser.** A throwaway local instance loaded with the real workbook,
   driven as admin end to end: quote → accept → Orders/Gantt/shift-wise agree → press
   Done → queue clears.
9. **Cross-surface audit** re-run: zero date disagreements across the seven surfaces.

---

## 9. Risks

**The riskiest edits are `iter_windows` and the placement step** — the most
load-bearing code in the application. Mitigation: every new input is optional and empty
on every existing path, and acceptance criterion 1 (byte-identical on three real books)
is the first gate, not the last.

**Second risk: the two-stage integration in `_plan`.** Every downstream surface must
read one merged schedule. The cross-surface audit (criterion 9) is what proves it.

**Third risk, accepted rather than mitigated:** a frozen quote's date is computed
against today's plan. Tomorrow's full optimization may move the expected completion —
by design, and the same as every other order in the book. The **promise** (the SO
delivery date) never moves.

---

## 10. Deliberately not built

* Partial-day or hour-level control of anything — the quote reads occupancy, it does
  not let anyone hand-place a job.
* Editing or re-quoting an order that is already in the book. A quote is for new
  orders; an existing order's date is changed by re-import, as today.
* Splitting one new job across several gaps to finish sooner (rejected: setup cost).
* Clubbing a new order into an existing batch (rejected: it moves the existing order).
* Any change to how the Excel re-import treats delivery dates.
* A new contest dimension, a new objective term, or any change to how plans are scored.
