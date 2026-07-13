# Promise Recovery — automatic committed re-sequencing after a disruption

**Date:** 2026-07-14
**Status:** Approved (2026-07-14) — **background** execution, **equal weighting** (protect
the most promises), **no critical flag** (keep the surface clean for non-technical users;
a truly critical order is still handled by marking it Urgent). Build test-first.
**Related:** [`RULES.md`](../../../RULES.md) (Rule 6, two-pass plan),
[order-commitment design](2026-07-13-order-commitment-promise-protection-design.md),
[optimize-plan design](2026-07-13-optimize-plan-sequence-search-design.md),
memory `sequence-optimizer-findings`

---

## 1. Problem

When the shop is disrupted — a worker absent, a machine down — committed orders fall
behind and their promised dates slip. Today the two-pass planner schedules committed
orders in **strict promised-date order** (earliest promise first). That rule is fine when
the shop is healthy, but under stress it is measurably **sub-optimal**: it serves the
earliest promise even when doing so needlessly breaks several others that a smarter order
would have saved.

**Owner's ask (2026-07-14):** infuse the recovery **directly into the system** — no tick
mark, no mode, no extra button. It should just happen.

## 2. Evidence (measured on the real Test5 book)

All 65 orders committed at their achievable dates, then a disruption (replan from N
working days later); metric = **promise-slip** (days each order finishes past its own
promise) and **promises missed**.

| Disruption | Strict date-order (today) | Re-sequenced | Recovered |
|---|---|---|---|
| Lost 1 week | 344 slip-days, 55 missed | 242, 44 | **102 days, 11 promises** |
| Lost 2 weeks | 637 slip-days, 62 missed | 511, 54 | **126 days, 8 promises** |

Two decisive secondary findings that shape the design:

- **A search is required — no fast rule captures it.** Least-slack, ATC, SPT, EDD+SPT all
  land 338–442 slip-days (≈ today's 344). Only the search reaches 242. So recovery cannot
  be a smarter *sort*; it needs the sequence *search*.
- **Budget curve** (1-week disruption, from 344): 60 plans → 292 (11 s laptop); 120 → 276
  (22 s); 250 → 263 (47 s); 500 → 242 (~90 s). Half the prize is cheap; the tail needs depth.

## 3. Behaviour (owner-visible)

**No setting, no button.** When committed orders exist and a re-plan shows any of them
slipping past its promise (a disruption has occurred), the planner **automatically
re-sequences the committed orders to protect the most promises**, and every Plan from then
on uses that recovered order. The planner never touches committed orders when nothing is
slipping — a healthy book keeps its exact date-order behaviour.

What the owner sees on the Orders tab after punching a disruption and re-planning:
committed orders that would have slipped show **fewer and smaller red flags**, with a quiet
line — *"Committed orders re-sequenced to protect N promises after the delay."* It is
information, not a control.

## 4. How it works (the logic, in one paragraph)

There is a fixed amount of machine/operator time after a disruption; the only lever is the
**order in which committed jobs claim the shared machines**. An order with **slack** (it
would finish comfortably before its promise) is made to wait, yielding its place in a
machine queue to an **at-risk** order (no slack) so the at-risk one finishes on time. The
slack order spends its spare room and still makes its promise; the at-risk order is saved.
When no slack exists (everyone tight), re-sequencing cannot save everyone — it chooses the
arrangement that breaks the **fewest** promises. It never invents capacity the disruption
took away: it redistributes the unavoidable slip intelligently.

## 5. Architecture

Reuses everything already built — the two-pass planner, the sequence optimiser, the
background-job machinery, and the rank-persistence used by the open-order Optimize feature.

```
 Plan (committed orders present)
   └─ Pass 1: committed/urgent
        ├─ schedule in strict promised-date order        (fast, deterministic)
        ├─ any order's expected end > its promise?  ──no──▶ use date-order (done, no search)
        └─ yes (disruption) ──▶ trigger PROMISE-RECOVERY search in the background
                                 (optimizer.optimize on the committed set,
                                  scored on promise-slip; seeded with date-order)
   └─ Pass 2: open orders backfill (unchanged; the open-order Optimize still applies here)
```

- **New scoring in `engine/optimizer.py`:** an optional `objective="promise_slip"` that
  scores a schedule by `Σ max(expected − promised, 0)` (slip-days) with broken-promise
  count as the tiebreak, instead of the delivery-date lateness used for open orders.
- **Auto-trigger, not a button:** the recovery search is kicked off by the planner itself
  when Pass 1 shows a slip, reusing the existing background-job runner (`_OPTIMIZE`-style
  state, one job at a time). It is **not** exposed as a control.
- **Persisted result:** the recovered committed order is saved as a rank map keyed by
  `(SO No, Item Code)` (a second key alongside `anvitech:plan_priority`, e.g.
  `anvitech:promise_recovery`), and Pass 1 replays it on every Plan until the committed
  set or its promises change (feedback punched, new commit/uncommit) — which invalidates
  it and re-triggers the search.
- **Budget:** default ~150 plans (captures the bulk; the tail has diminishing returns).
  Eval-counted + fixed seed → deterministic. On Render (~2.6 s/plan) that is a few
  minutes in the background; the Plan shows the best-so-far and settles when it finishes.

## 6. Safety (non-negotiable, given the live site)

- **Never worse than today.** The search is seeded with the strict date-order sequence and
  keeps the global best, so the recovered plan's promise-slip is **≤** date-order's. It can
  only help or tie — it can never make committed orders worse.
- **Deterministic.** Fixed seed + eval budget → the same recovered order every time for the
  same book, so the plan doesn't wander between clicks.
- **Committed only when slipping.** No slip → no search → byte-identical to today. Healthy
  books are untouched (golden trace unaffected).
- **Open-order Optimize is unchanged** and still applies to Pass 2. The two features
  compose: committed orders are protected first, open orders backfill optimally.

## 7. Objective & a deliberate v1 limit

v1 minimises **total promise-slip** (fewest broken-promise-days). It treats every promise
as equal weight. In reality some customers matter more — but per the owner's "no tick
mark" direction, importance is **not** a global setting. If it proves needed, it becomes a
**per-order flag** (like commit/urgent already are): mark an order "critical" and the
recovery weights its promise heavily. Left out of v1; noted as the natural next step.

## 8. What it does NOT do

- It does not create capacity — a lost week is still lost; it redistributes the slip.
- It does not touch open orders (that is the existing Optimize) or reorder by anything
  other than protecting promises.
- It does not model *future* disruptions (e.g. "this worker is out all next week too") —
  that is an operator-leave edit in the Excel, re-uploaded.

## 9. Testing

- `tests/test_promise_recovery.py` — pure: the promise-slip objective; recovered slip is
  always ≤ date-order slip (the safety guarantee); determinism; a synthetic disruption where
  re-sequencing provably saves a promise date-order breaks.
- API: auto-trigger fires only when a committed order slips; no slip → no job, plan
  unchanged; recovered rank persists and is invalidated on feedback/commit change.
- Golden trace untouched (no committed orders in the sample).
- Real-data guard (Test5-gated): commit-all + disruption recovers > 0 promise-days.

## 10. Decisions (settled with the owner, 2026-07-14)

1. **Background** (not inline) — reuse the proven Optimize job runner so Plan never blocks;
   the recovered committed order lands a few minutes after a disruption is punched.
2. **Equal weighting** — protect the most promises; every promise counts the same. **No
   critical flag** in v1: the owner wants the surface kept clean for non-technical floor
   users, and a genuinely critical order is already handled by marking it **Urgent** (front
   of the queue). Per-order "critical" weighting stays out until real cases demand it.
3. **Depth:** default ~150 plans (most of the prize); revisit only if disruptions are frequent.
