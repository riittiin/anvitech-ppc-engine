# Daily Entry: outstanding quantity at the step, and the clubbed-order guard

**Date:** 2026-08-28
**Status:** design approved, not yet implemented
**Owner request:** a director-visible accuracy problem, not a scheduling problem.

---

## The problem, as it happened

`26-27SO149` and `26-27SO150` carry the **same item code**. Rule 1 clubs them into
one batch, so the plan runs them as one lot and the floor sees one pile of
identical parts.

SO149 was for **400** pieces, SO150 for **23**. In reality **SO150's 23 finished**.
The operator punched the 23 against **SO149**, leaving:

* `26-27SO149` — 400 ordered, 23 recorded, **377 shown outstanding** (wrong: none were made)
* `26-27SO150` — 23 ordered, 0 recorded, **still open forever** (wrong: it is finished)

**The plan did not care** — the batch owes the same total either way, and the floor
built the right parts. **The books did.** Anvitech's directors read the order book to
know which customer orders are closed, and it told them the opposite of the truth.

### Why the operator got it wrong

Attribution *is* knowable — the owner confirmed this — but only by research. Today the
operator:

1. asks the machine operators how many pieces they finished,
2. writes the count in a book,
3. **leaves the Daily Entry tab, opens the Orders tab**, and works out which SO
   number and item code that count belongs to.

That cross-reference is slow, and it is skipped or rushed when he is trying to
finish for the day. The mistake is not carelessness; it is a lookup the software
makes him do by hand, in a different tab, from memory.

**So the fix is not to teach him to be careful. It is to put the answer he is
walking away to find on the screen he is already looking at.**

---

## What is being built

Three things, all of them **display and a confirm box**. Nothing in this spec
changes a scheduled date.

### 1. The outstanding block (the main feature)

Once SO No + Item Code + Process are picked, a block appears **under the Process
dropdown** showing, for that exact step:

```
26-27SO113 · 9611416360 · CNC FIRST SIDE           due 20-09-2026

   Ordered              400
   Done at this step    166
 ▶ Still to make        234
```

(No "you can enter up to" line here: this is the first routing step, so nothing
upstream limits it and the cap equals the 234 already shown. It appears only when it
differs — see the clubbed example below, where it reads 166.)

Two numbers, deliberately distinct:

* **Still to make** = `ordered − good recorded at this step`. What the order owes here.
* **You can enter up to** = `upstream cap − pieces already recorded at this step`,
  where the upstream cap is the good qty that cleared the *previous* routing step
  (or the ordered qty at the first step). This is the rule
  `orderbook.precedence_cap_error` **already enforces on Save** — showing it turns a
  red error after the click into a number before it.

When the two are equal the second line is **hidden**, so it only ever appears when it
is saying something. Example where they differ: CNC SECOND SIDE still owes 400, but
only 166 have cleared CNC FIRST SIDE, so 166 is the most that can be entered today.

A process is always selected (the dropdown auto-selects the first routing step when an
item is picked), so the block has no empty state.

### 2. The clubbed-order table

When the chosen **item code is on two or more open SO lines**, the block is preceded
by the comparison the operator does by hand today:

```
⚠ This part is on 2 open orders. Check you have the right one.

   SO No        Due        Ordered   Done   Still to make
 ▶ 26-27SO149   20-09-2026     400      0             400     ← you picked this
   26-27SO150   12-09-2026      23      0              23
```

`Done` and `Still to make` are **at the selected process**, matching the block below.
Rows are sorted by delivery date (soonest first). **Each other row is clickable** and
switches the form to that SO — the whole Orders-tab trip, reduced to one click.

When the item is on **one** open SO this table does not render at all. That is the
common case and it stays uncluttered.

### 3. The quantity cross-check on Save

Fires **only** when all of the following hold:

* the picked item is on 2+ open SO lines, and
* the typed `Qty Produced` does **not** equal the picked SO's *still to make* at this
  step, and
* it **exactly equals** another open SO's *still to make* at this step.

```
⚠ Check this before saving

You entered 23 pieces against 26-27SO149,
which still needs 400 at CNC SECOND SIDE.

23 is exactly what 26-27SO150 still needs (due 12-09-2026,
earlier than SO149).

   [ Switch to 26-27SO150 and save ]   [ Save as 26-27SO149 ]   [ Cancel ]
```

* **Switch … and save** re-points the entry and saves in one action. It does *not*
  merely change the dropdown: a half-switched form left on screen is its own new way
  to get this wrong.
* **Save as …** proceeds exactly as typed. The operator is never blocked — he is the
  one who can see the parts.
* If the quantity matches **several** other SOs, the box names them and offers only
  **Cancel**; there is no single right answer to auto-pick, so it does not guess.

---

## How it is built

### The one new pure function

`engine/orderbook.py` gains **one** function, added alongside the existing ones:

```python
def entry_progress(active_orders: dict, actuals, masters=None) -> dict:
    """Per open (SO, item): ordered qty, delivery date, and per routing step
    {done, still_to_make, can_enter_now} — the numbers the Daily Entry form shows
    before a punch. Pure; reporting only; never consulted by the scheduler."""
```

It is built on **`_process_totals`** — the same internal that `precedence_cap_error`
and `active_so_lines` already use, with the same `_norm` process matching and the same
"good = produced − rejected, netted then clamped at ≥ 0" rule.

**This is the load-bearing decision in the spec.** This repo has been bitten
repeatedly by two features deriving the same number two different ways (the delay
report's own completion date, Analytics' own working hours, the frozen row's own
remaining qty). Reusing `_process_totals` means **the panel and the Save button
cannot disagree about what a step owes.** A number shown as enterable is, by
construction, a number the server will accept.

Unlike the existing `process_progress_rows`, `entry_progress` covers orders with **no
punches yet** (the form needs the ordered qty before any work is recorded) and carries
the **upstream cap**. `process_progress_rows` is left untouched and keeps its caller.

### Where it is served

Two keys are added to the dict already returned by **`GET /items`**:

* `progress` — the `entry_progress` output, keyed `"<so_no>\x1f<item_code>"`
* `item_to_sos` — `{item_code: [so_no, ...]}` for the clubbed table, so the browser
  is not inverting a map by hand

`/items` is the right home and no new endpoint is needed:

* it **already feeds this form** and is fetched by `wireActualsForm` on every render,
* it is a **live store read**, not the cached `/run` response, so the numbers refresh
  after every Save for free,
* it is **role-open**, and the floor logs in as `user`.

**Deliberately NOT put on `/run`.** That response is cached and keyed by
`_plan_fingerprint`; live punch data inside it is the 2026-08-08 stale-cache bug over
again (see CLAUDE.md, "the plan cache must be keyed on what is displayed").

Size: on the live book this is ~68 open orders × ~10 routing steps ≈ 700 small rows —
the same order of magnitude as the routing map `/items` already returns.

### The browser side

In `web/app.js`, confined to the Daily Entry form:

* a render function for the block + clubbed table, called whenever SO, Item or Process
  changes,
* the cross-check as its **own small function** — given the typed qty, the picked SO
  and the other SOs' numbers, return *warn* or *don't* — so it can be reasoned about
  and mutated rather than being buried inside the Save handler,
* the three-button box reusing the existing `.modal-overlay` / `.modal` markup already
  used by the delete-confirm dialog.

---

## Scope contract — what may and may not be touched

The owner's explicit instruction (2026-08-28): **nothing outside this feature's
responsibility may be modified.** This section is the checkable form of that.

### May be changed

| File | Permitted change |
|---|---|
| `engine/orderbook.py` | **Add** `entry_progress`. No existing function edited. |
| `api/main.py` | **Only** the body of `items()` — two keys added to its return dict. |
| `web/app.js` | **Only** the Daily Entry form region: `actualsFormHtml`, `fillItemFromSO`, `fillItemMeta`, `wireActualsForm`, the `a-save` handler, plus new functions added next to them. |
| `web/style.css` | **Append** new class rules only. No existing rule edited. |
| `tests/test_daily_entry_guard.py` | New file. |
| `CLAUDE.md` | Append a bullet describing the shipped feature. |

### Must NOT be touched

* `ppc_engine/` — the whole vendored scheduling package.
* `engine/rules/` — every rule module.
* `engine/new_engine.py`, `engine/freeze.py`, `engine/optimizer.py`,
  `engine/flow_scheduler.py`, `engine/pipeline.py`, `engine/analytics.py`,
  `engine/delay_report.py`, `engine/efficiency.py`, `engine/book_store.py`,
  `engine/models.py`, `engine/config.py`.
* `api/main.py` **anywhere but `items()`** — in particular `_plan`,
  `_plan_fingerprint`, `_resolve_config`, `post_actuals`, `rollback_actual`,
  `_try_start_auto`, and every optimize endpoint.
* `engine/orderbook.py`'s **existing** functions — `_process_totals`,
  `precedence_cap_error`, `active_so_lines`, `process_progress_rows`,
  `completed_by_process` are **read and reused, never edited**.
* Any existing test file. Adding this feature must not require rebasing an
  assertion; if it does, that is a signal the change is out of scope — stop and ask.
* `web/style.css`'s **uncommitted** full-width change (18-08), which is unrelated
  work in progress and must be left exactly as found.

### Why the server write path is untouched

The precedence guard in `post_actuals` **already rejects** an over-cap punch. This
feature surfaces that rule earlier; it does not re-implement or relax it. No
server-side validation is added, changed or removed.

### The proof that scope held

Before finishing: `git diff --stat` must list **only** the files in the "may be
changed" table. Any other path in that list is a scope breach and gets reverted.

---

## What this cannot do

* **It does not fix the 23 pieces already recorded against SO149.** Owner's decision
  (2026-08-28): prevention first. Undo is limited to the latest punched day, so the
  historic entry needs a direct database correction, written up separately.
* **It does not stop a determined wrong entry.** Every guard is overridable, because
  the operator is the one who can see the parts and the software is not.
* **It only compares open orders.** Once an SO is marked complete it leaves the
  picker, so it cannot appear in the clubbed table.
* **It is silent when quantities do not line up neatly.** Punching 20 of SO150's 23
  matches nothing exactly and raises no warning — by design, since a partial punch is
  perfectly normal and warning on it would train the floor to click through.

---

## Testing

Written **RED first** — each test must fail before the code exists.

**Pure (`entry_progress`)**
1. An order with no punches: `done = 0`, `still_to_make = ordered`, first step's
   `can_enter_now = ordered`.
2. Rejects net across entries: 100 produced then 20 rejected ⇒ `done = 80`,
   still_to_make = ordered − 80.
3. The upstream cap: 166 cleared step 1 ⇒ step 2's `can_enter_now = 166` while its
   `still_to_make` is the full ordered qty.
4. `can_enter_now` shrinks by what is already recorded at that step.
5. An item with no routing, and a completed order, are absent — no crash.
6. **Agreement invariant:** for every open order and step, punching exactly
   `can_enter_now` is accepted by `precedence_cap_error`, and one more piece is
   rejected. This is the test that pins the "one definition" decision.

**API**
7. `GET /items` carries `progress` and `item_to_sos`, and `item_to_sos` lists both
   SOs for a shared item code.

**The regression fixture — the owner's actual case**
8. Two open SO lines on one item code: SO149 × 400, SO150 × 23. The cross-check
   **fires** for 23 typed against SO149 and names SO150; it stays **silent** for 23
   typed against SO150, and silent for 20 against either.

**Mutation check.** Each part of the cross-check is reverted individually and at least
one test must fail. Scheduling and capture fixtures in this repo have a documented
habit of passing vacuously (CLAUDE.md, 2026-08-09) — a test that survives the mutation
is proving nothing and gets rewritten.

**Whole-suite.** `pytest` must stay green with **no existing assertion rebased**. That
is also the scope check: this feature reads existing numbers and shows them earlier,
so nothing already asserted about them may move.

---

## What this deliberately does not change

The scheduler, the plan, the plan cache, the optimizer, the freeze set, the delay
report and the Gantt are all untouched. **Not one scheduled date moves.** If a plan
fingerprint or a completion date changes after this work, that is a bug in this
feature, not an expected cost of it — and it is checked, not assumed: the same book
must produce an identical plan before and after.
