# Promise Protection via a Convex Fairness Term — Design

**Date:** 2026-07-24
**Status:** Approved (brainstorming) — pending spec review
**Author:** Claude + owner
**Supersedes / relates to:** the discarded 2026-07-16 "promise ceiling" hard veto
(Phase 2R); the current `ppc_engine` `λ·max_tardiness` fairness guard.

## The incident

On a live Thursday auto-optimize, an order **due and promised for 8 August** came out
of the re-optimization finishing **23 August** — a ~15-day slip the optimizer *chose*,
not one the floor forced. The owner discovered it only by hand-comparing before/after
expected dates. A self-inflicted 15-day miss on a promised order damages Anvitech's
reputation and must not happen when it is avoidable.

## Root cause

Production runs `scheduler="new"` (`ppc_engine`). Its sequence search
(`ppc_engine/optimize/memetic.py`) scores every candidate job order with the one
objective in `ppc_engine/objective/objective.py`:

```
score = total_tardiness_days            # Σ over orders of max(0, days late)
      + λ · max_tardiness_days          # λ = 30, the "fairness" guard
      + w · makespan_days               # w = 0.1
```

The `λ·max_tardiness` term was added *specifically* to stop a savable order being
pushed dramatically late. **But it only protects the single worst order.**
`max_tardiness` is the lateness of the one most-late order in the whole plan. When some
*other* order is already later than our Aug-8 order (very common — the book routinely
carries structurally-impossible orders that are 20-40 days late from a mid-cycle start),
pushing Aug-8 from on-time to 15-days-late **does not raise `max_tardiness`** (15 < 20),
so the guard is blind to it. Meanwhile `total_tardiness` is a flat sum: +15 days on one
order scores identically to +1 day on fifteen orders. Nothing in the objective objected
to sacrificing Aug-8 for an aggregate gain.

**In one sentence: the fairness term protects rank-1 worst and leaves rank-2, rank-3, …
fully exposed.**

The same slip-neutral aggregate is mirrored in `engine/optimizer.py`
(`score = total_late_days + 40·makespan_days`), which is what the overlap contest
(`optimize_service.py:246`) and the Thursday auto-apply "strictly better" gate
(`api/main.py:_auto_apply_result`) use to pick and accept plans. So even the acceptance
gate that applied this plan judged it purely on aggregate.

## Goals

1. The optimizer must **not push an order far past its due date when a better sequence
   exists** — protecting *every* order, not just the current worst.
2. When a slip is **genuinely unavoidable** (the floor fell behind and no sequence hits
   the date), the optimizer must still **report the honest new date** — the fix fights
   only *avoidable* damage.
3. Preserve the hard-won aggregate quality (today ≈ 72.7 d makespan / ~1528 late-days on
   the live book) — the fix must not blow up total late-days or makespan.
4. Give the owner **visibility** each Thursday into which orders moved later, so slips
   (avoidable or not) can be managed with customers proactively.

## Non-goals

- **No hard veto / infeasibility gate.** The 2026-07-16 "promise ceiling" made most
  plans illegal (zero-slack promises collapse the feasible region) and measured ~30%
  worse. This design is a *soft* penalty only — the search never becomes infeasible.
- **No manual marking.** Protection applies to every order against its own SO delivery
  date; the owner chose this over Committed/Urgent-only tiers. `commitment` /
  `promised_date` stay informational and untouched.
- **No change to the three basic rules or the scheduler mechanics.** Only the *objective*
  (which sequence is "best") and reporting change.

## Design

### Part 1 — Convex, per-order fairness in the `ppc_engine` objective (primary lever)

Replace the "protect only the worst" term with one that penalizes **each order's**
lateness on an **accelerating (convex) curve** — the "accelerating pain" the owner
chose. Per order, tardiness `t_i = max(0, completion_date − due_date in days)`. Define a
per-order severity:

```
severity_i = min(CAP, ( max(0, t_i − T) )²)
severity   = Σ over orders of severity_i
```

- **`T` (tolerance days):** the first `T` days of lateness cost nothing extra (a small
  slip is normal/recoverable — only `total_tardiness` counts them). Beyond `T`, cost
  accelerates.
- **squared beyond `T`:** a 15-day slip costs vastly more than fifteen 1-day slips, so
  the optimizer strongly prefers spreading small delays over dumping a big one on any
  single order — and this now protects *every* rank, not just the worst.
- **`CAP` (per-order ceiling):** a genuinely-impossible order (can never hit its date
  from any sequence) cannot hijack the search into a lost cause at the expense of the
  savable orders. Past the cap, extra lateness on a doomed order stops dominating.

New objective:

```
score = total_tardiness_days
      + μ · severity                    # NEW: convex, every order, capped
      + w · makespan_days               # unchanged
      + (λ · max_tardiness_days)        # see "λ decision" below
```

**λ decision (settled by measurement):** the convex sum generalizes the max term (max is
its degenerate ∞-norm extreme). Default intent is to **replace** `λ·max_tardiness` with
`μ·severity`. The tuning sweep (below) decides whether retaining a small `λ·max` as an
extra cheap guard on the absolute worst measurably helps; if not, it is removed. Either
way the outcome is a measured dominance point, documented in a code comment like the
existing `MAKESPAN_WEIGHT` note.

**Why this satisfies both goals automatically.** If a big slip is *avoidable*, some other
sequence has a lower `severity`, so the search finds and prefers it. If it is
*unavoidable*, every candidate carries that `severity_i`, the term is a constant the
search cannot escape, and the optimizer simply returns the honest date. The convex
penalty only ever moves the plan away from *avoidable* damage.

**Where it lives.**
- `ppc_engine/objective/metrics.py` — `compute_metrics` already returns
  `lateness_by_order`; add a computed `severity_days` field (needs `T`, `CAP` from
  config). Keep `max_tardiness_days` for reporting regardless of the λ decision.
- `ppc_engine/objective/objective.py` — `score()` adds `μ · metrics.severity_days`.
- `ppc_engine/config.py` — new weights beside `fairness_weight` / `makespan_weight`:
  `severity_weight` (μ), `severity_tolerance_days` (T), `severity_cap` (CAP), each with a
  measured-default comment.

### Part 2 — Mirror the term in `engine/optimizer.py` (acceptance parity)

The overlap contest winner-pick and the Thursday auto-apply gate score plans through
`engine/optimizer.py` `score()` / `plan_metrics()` in the "old space". If only the
`ppc_engine` objective changes, the search would protect Aug-8 but the contest/auto-apply
gate could still wave through an aggregate-better-but-reputation-worse plan. So mirror the
same convex term here:

- `engine/optimizer.py:plan_metrics` — it already computes each order's `gaps`/`late`
  internally; add a `slip_severity` field using the same `T`/`CAP`.
- `engine/optimizer.py:score` — add `μ · metrics["slip_severity"]` alongside the existing
  `total_late_days + 40·makespan_days`.
- The `T`/`μ`/`CAP` constants live beside `MAKESPAN_WEIGHT` (module constants, matching
  the codebase's "measured constant" convention), kept numerically consistent with the
  `ppc_engine` config values.

No new callers, no new machinery, no signature changes to the contest/apply/replay path.
Both surfaces (search + acceptance) now judge plans by the same reputation-aware yardstick.

### Part 3 — Thursday "what moved" transparency note

After each auto-optimize apply, extend the existing one-line auto-note
(`book_store.save_auto_note`, surfaced on `/run`'s `auto_note` and the Orders tab) to
name the orders that now finish **materially later than the plan that was on screen
before this run**.

**Data flow (no new persisted state).** At apply time in
`api/main.py:_auto_apply_result` we already have both plans on today's book:
- the **incumbent** schedule (current applied ranks replayed — `_incumbent_metrics`
  already builds it via `_all_lines_schedule`), and
- the **new** schedule (the winning ranks replayed via `_all_lines_schedule`).

Compute each order's expected completion date from both schedules (the same
`e.end`-per-`so_ref` expected map `plan_metrics` already derives), diff them, and list
orders whose new expected date is later than the incumbent's by more than a small
threshold (e.g. `> N` days — a config/module constant). The note then reads, e.g.:

> "Plan auto-re-optimized 11:03: 1490 late-days (was 1528), overlap 80 → 85.
> ⚠ 2 orders now finish later than before: SO123-01 +6d (→ 14-Aug), SO440-02 +3d."

If nothing moved later, the note keeps its current form (no warning line). This turns a
silent surprise into a heads-up the owner can act on. It is **schedule-neutral** — pure
reporting, computed from schedules that already exist at apply time.

## Verification & tuning plan (first-class, not an afterthought)

The codebase treats objective weights as *measured dominance points, not taste*
(`MAKESPAN_WEIGHT = 40` — "re-measure before moving it"). The new `T` / `μ` / `CAP` get
the same rigor:

1. **Reproduce the failure first.** Load the real book at (or a crafted reproduction of)
   the state that produced Aug 8 → Aug 23, run the *current* optimizer, and confirm the
   code reproduces a plan that avoidably pushes a savable order far past its due date.
   No fix is trusted until the bug is seen failing in a test.
2. **Sweep `T` × `μ` (× `CAP`)** on the real book. For each combination measure: **worst
   single-order slip on savable orders**, **total late-days**, and **makespan**. Choose
   the point that eliminates the avoidable big slips **without** regressing aggregate
   quality (guardrail: total late-days and makespan stay within a small tolerance of
   today's ≈ 1528 / 72.7 d). Settle the λ-retention question here.
3. **Regression test — the Aug-8 pin.** A crafted book where a savable order *can* be
   protected must come out protected; assert the worst-savable-order slip is bounded.
   This makes the blind spot impossible to silently reintroduce.
4. **Consistency test.** `ppc_engine` objective and `engine/optimizer` score agree on
   which of two hand-built plans is better under the convex term.
5. **Transparency test.** Given two schedules where one order moves later, the note lists
   exactly that order with the correct delta and new date; when nothing moves later, no
   warning line appears.
6. **Full suite green** (`pytest`, ~508 tests). **Golden trace unchanged** — the classic
   engine's rule output is untouched; only the optimizer's *choice of sequence* and the
   note text change. Existing `ppc_engine` objective tests updated to the new score shape
   with justified expected values.

## Risks & mitigations

- **Re-collapsing the search (the July failure).** Mitigated by construction: soft
  penalty, never a feasibility gate; `CAP` prevents doomed orders from dominating.
- **Aggregate regression.** Mitigated by the tuning guardrail (step 2) — a combination
  that improves worst-slip but worsens totals beyond tolerance is rejected.
- **Weights drift with data.** Documented as measured constants with re-measure notes; the
  Aug-8 regression test guards the *behavior* even if data shifts.
- **Two score functions diverging over time.** Kept numerically consistent and covered by
  the consistency test (step 4); a comment in each points at the other.

## Rollout

Single change set, behind the existing engine seam. No env var, no schema change, no UI
change beyond the richer auto-note text. Deploy is the standard Render manual deploy after
`pytest` passes.
