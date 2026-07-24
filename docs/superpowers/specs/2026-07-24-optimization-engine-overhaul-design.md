# Optimization Engine Overhaul — Design & Autonomous Loop Plan

**Date:** 2026-07-24
**Status:** In progress — owner delegated full authority ("loop engineering", human-out-of-the-loop).
**Owner mandate:** Build the best optimization engine considering ALL parameters. Try many
formulations/combinations, fabricate test books, self-judge results, converge on the best.
Take hours if needed. Owner is NOT in the iteration loop — Claude judges goodness.

## Why (history)
- v1 objective minimized total delay only → pushed a promised order Aug-8→Aug-23 (a single
  order abandoned). Fixed with a convex per-order guard + worst-order ceiling (shipped
  2026-07-24, commit range …136a327).
- v2 (current shipped) still surprised the owner: an "optimized" plan showed 55/57 late vs
  39/57 standard. Root finding: the panel/objective use raw late-COUNT and total late-days,
  which hide the lateness DISTRIBUTION. Measured (Test5): the optimized plan actually pulled
  10 orders OUT of the 20+ day "very bad" bucket (23→13) and cut total late-days 866→745 —
  a genuine improvement by the owner's value model, invisible in the panel.
- Owner's value model (explicit): an order 2–4 days late is FINE; 10+ days is NOT fine.
  So the right measure is a convex per-order cost by how-late, not a raw count.

## The Judge (fixed definition of "good" — owner's values)
Plan cost `J` (lower is better), minimized and used to rank every candidate plan:

```
J = Σ_orders  order_cost(Lᵢ)                     # Lᵢ = days late (0 if on-time)
  + w_make · makespan_days
  − w_util  · utilization_score                  # reward high, balanced machine+operator use
  (+ ∞ if the worst order exceeds the incumbent's worst — the no-regression ceiling)

order_cost(L) = 0                         if L ≤ TOL           # TOL ≈ 4 days ("fine")
              = A·(L−TOL) + B·(L−TOL)²    if L > TOL           # convex ramp
              + STEP10  if L ≥ 10                              # "bad" step
              + STEP20  if L ≥ 20                              # "very bad" step
```

Owner-anchored constants of the JUDGE (fixed; reflect the owner's bands): TOL=4, bad=10,
very-bad=20, with STEP10/STEP20 large enough that a 10+ or 20+ order dominates many small
slips. `w_util` rewards utilization (per-machine + per-operator, penalizing imbalance).
The ENGINE's own objective weights are what get SEARCHED to best satisfy J across books —
J itself is not tuned (avoids circularity: J = the owner's values; the engine is tuned to J).

`utilization_score`: mean machine utilization + mean operator utilization − a spread/imbalance
penalty (so "everyone reasonably busy" beats "a few maxed, most idle"). Uses the fixed
analytics capacity (physical windows, post the 2026-07-24 manual-station fix).

## Levers searched (the engine, not the judge)
- **Objective shape/weights** the search minimizes (proxy for J): per-order convex tardiness
  (tolerance, linear+quadratic coefficients, cap), late-band step penalties, makespan weight,
  worst-order ceiling weight, utilization term.
- **Job sequence** (the sequence search — already exists; retune under the new objective).
- **Overlap %** (auto-tuned — keep).
- **Consolidation window** (auto-tuned).
- **Operator-pick policy** (scarce/balanced/flexible — auto-decide the best).
- **Staffing rule (NEW):** one operator per machine per shift; exception when a job's duration
  on that machine is short (< short_job_threshold). Tune the threshold.

## Hard constraints
- One operator per machine per shift (short-job exception).
- Worst order never increases on a re-optimize (ceiling + apply backstop — keep).
- Absences, machine qualifications, calendar (Thu/holiday off), setup on CNC/VMC only.

## Product changes (owner-requested)
- **Remove the "Order Priority" setting** from Settings/Schedule — the engine decides the
  sequence; the UI no longer exposes it.
- Optimize panel: show the lateness DISTRIBUTION (severity bands) for standard vs optimized,
  plus the judge score, so a genuinely-better plan reads as better (no more scary raw count).
- Utilization is a first-class goal (maximize, balanced) — surfaced and optimized.

## Method (the loop)
1. Judge + fabricator (diverse books: due-date tightness, order count, CNC-heavy vs
   manual-heavy, feasible vs impossible) + baseline the current engine.
2. Sweep objective formulations × coefficients × sequencing × staffing/operator-pick;
   score every plan with J; keep the winner; log the frontier.
3. Implement the staffing rule + utilization; measure.
4. Converge; stress-test on edge cases + the real book (Test5).
5. Implement into the engine (TDD), remove Order Priority, update panel, full suite.
6. Report with numbers.

## Progress log
- (this doc created) — parameters + judge defined; harness next.
