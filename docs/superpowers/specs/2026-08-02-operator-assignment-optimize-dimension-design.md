# Operator assignment as an Optimize dimension — design

**Date:** 2026-08-02
**Status:** approved (owner), ready for implementation plan
**Scope:** the Optimize contest only (plus the thin persist/replay path the tuned
overlap and machine-set already use), and the one-line wiring fix that makes the
operator-pick policy actually reach the live engine. No change to the 3 rules, the
scheduler's placement/staffing logic, capture, freeze, the committed-promise cap,
operator rotation, or absences.

## Goal

Let the Optimize contest **choose how operators are matched to machines** for each
plan, sweeping the operator-pick policy as a fourth search dimension alongside the
three it already sweeps (job sequence, overlap %, machine-set). The contest keeps
the single best `(sequence, overlap, machine-set, operator-pick)` combination by the
current score, and the winning policy persists and flows into the everyday plan
exactly like the tuned overlap and machine-set do today.

The primary objective is unchanged and owner-confirmed (2026-08-02): **fewer late
deliveries and shorter makespan.** Evening out operator workload is a welcome side
effect if it happens but is **not** a goal — the score is not touched, and no
operator-fairness term is added.

The contest sweeps **two** of the three available policies — `scarce` and `balanced`
(owner decision 2026-08-02). The third policy, `flexible`, is the inverse of `scarce`
(it burns versatile operators on jobs a specialist could cover) and is the least
likely to ever win, so it is dropped to keep cost down. It stays fully implemented in
the engine and is a one-line addition to the candidate list if the owner ever wants it
back as a data point.

## Background: what the optimizer compares today (verified in code)

The new (production) engine's contest sweeps **three** dimensions:

1. **Job sequence** — a multi-start hill-climb over batch orderings
   (`engine/optimizer.optimize` / `engine/new_engine.optimize_sequence`).
2. **Overlap %** — a grid of candidate values
   (`optimize_service.CLOUD_NEW_OVERLAP_CANDIDATES`), run as a contest.
3. **Machine-set** — Allotted-only vs Allotted+Suggested union
   (`config.flexible_machines`, 2026-07-29), the outer loop in `contest_jobs`.

It does **not** vary how operators are assigned to machines.

### How operators are assigned today (verified in code)

Operator assignment happens **greedily and deterministically at decode time**, per
(machine, shift, work-interval), inside the vendored `ppc_engine` package — not in
`engine/new_engine.py`, and not as any kind of search.

- `ppc_engine/scheduler/flow_scheduler._lay_on_machine` staffs each working window.
  It prefers the machine's existing shift operator (stability) if still free for the
  interval; otherwise it calls the policy-driven picker
  `ppc_engine/scheduler/staffing.StaffingBoard.candidate_operator`.
- `candidate_operator` filters to the **free, qualified, on-shift, available**
  operators, then chooses among them by the policy in `PlanConfig.operator_pick`
  (`ppc_engine/config.py`):
  - **`scarce`** (default) — the least-flexible free operator first
    (pool pre-sorted by `(flexibility, name)`, `flexibility = len(qualified_machines)`),
    so more-flexible people stay available for the machines only they can run.
  - **`balanced`** — the free operator with the least cumulative load so far
    (ties → scarce → name).
  - **`flexible`** — the most-flexible free operator first (the inverse of scarce).
- The pick can only ever choose among operators who are **already free and
  qualified**, so *every* policy produces a feasible schedule. The staffing board's
  `commit`/`free_during` invariants prevent double-booking regardless of policy — a
  different policy can change *who* runs a machine and therefore the makespan/late-days,
  but it can never make a plan invalid.

### The gap this feature closes

Two facts, both verified:

1. **The contest never sweeps `operator_pick`.** It is not a search dimension.
2. **`engine/new_engine._plan_config` never sets `operator_pick`**, so the ppc
   `PlanConfig` falls back to its default `"scarce"`. Production has therefore *always*
   run `scarce` and has **never measured** `balanced` or `flexible` on the real book.

`scarce` encodes exactly the owner's instinct — "don't burn a flexible operator on a
machine a specialist could cover" — but it was chosen statically and never tested
against the alternatives on this book. This feature makes the contest measure it.

### Why this is high-leverage but bounded (prior research)

Past research (memory: `optimizer-research-2026-07-19`, `scheduler-v2-2026-07-19`)
established that on the real book the **crew is the binding constraint** (certified LP
capacity floor ~37.2 calendar days, operators binding, not machines) and that the
scarce-first operator pick was itself the single biggest lever found
(78.5 → 73.7 d on one sequence). So operator assignment is the right place to look —
but because `scarce` already captured much of that lever, the marginal win from
*searching* the three policies is uncertain. That is precisely why Approach 1 is
**measure-first**: it tells us, per book, whether a different policy beats scarce. If
the spread is real, it justifies building the smarter demand-aware policy
(**Approach 2 — a "bottleneck-first" policy that steers flexible operators toward the
machine with the most pending demand**), which is explicitly deferred to a follow-up
and out of scope here.

## Design

### 1. One new config field

`engine/config.py`: add `operator_pick: str = "scarce"`.

- Default `"scarce"` = today's behavior, byte-identical.
- Validation: must be one of `{"scarce", "balanced", "flexible"}` (raise on anything
  else, consistent with the other validated knobs).
- Round-trips in `to_dict`/`from_dict` (default `"scarce"` when missing/blank).
- Folded into `_inputs_signature` (plan-shaping, like `overlap_percent` /
  `flexible_machines` / `committed_promise_slack_days`) so a change to the applied
  policy flags an optimization `inputs_changed` in the staleness banner.

This field is **owned by the optimizer**, not the user — it is set only by applying an
Optimize result (never hand-edited), exactly like the tuned `overlap_percent` and
`flexible_machines`.

### 2. The wiring fix that makes the knob live

`engine/new_engine._plan_config` sets `operator_pick=config.operator_pick` on the ppc
`PlanConfig` it builds (today it omits it → always `"scarce"`). The scheduler's
`candidate_operator` already reads this field, so this one line is the whole enabling
change on the engine side.

Unlike `flexible_machines` (which is baked at masters **load** time and needs a
per-flexibility `_new_masters` variant), `operator_pick` is a pure **decode-time**
choice: the same masters are reused and only the `PlanConfig` copy differs. No masters
reload, no new cache key. The policy is threaded through every entry point that builds
a `PlanConfig`: `run`, `optimize`/`optimize_sequence`, `tune`, `sweep_optimize`.

### 3. The contest gains an operator-pick loop

Operator-pick is a **fourth, orthogonal dimension** — it multiplies the existing
`sequence × overlap × machine-set` contest rather than replacing any knob.

- `optimize_service`: add `OPERATOR_PICK_CANDIDATES = ("scarce", "balanced")` (owner
  decision 2026-08-02: sweep these two; `flexible` dropped for cost). A cloud variant
  name may mirror the overlap constants if a different cloud list is ever wanted; for
  now both use the same two.
- **`contest_jobs`** (the single source of truth for the candidate list) adds
  operator-pick to the product, gated to `scheduler=="new"` exactly like machine-set:
  ```
  picks = OPERATOR_PICK_CANDIDATES if scheduler == "new" else ("scarce",)  # ("scarce", "balanced")
  machine_sets = (False, True) if scheduler == "new" else (False,)
  return [(ov, flex, pick)
          for pick in picks
          for flex in machine_sets
          for ov in contenders]
  ```
  Ordering keeps the **current** setting first so an early Stop leaves it fully
  searched (`scarce`, current machine-set, current overlap lead their axes).
- **`run_candidate`** applies the policy via the same config-copy mechanism already
  used for overlap and flexibility:
  `replace(setup.search_config, flexible_machines=bool(flex), operator_pick=pick,
  **{knob: int(overlap)})`.
- **`pick_winner` / `merge_shard_rows`** carry the winning `operator_pick` alongside
  the winning overlap and machine-set. Ties break to the **current** policy
  (`scarce`) — no churn — consistent with how overlap/machine-set ties already resolve.
- **Local fallback** (`new_engine.sweep_optimize`): the existing loop that runs `tune`
  once per machine-set gains an outer loop over the three policies (config copy with
  `operator_pick` set), keeping the best by the existing score. `SweepResult` gains an
  `operator_pick` field.
- **20-way GitHub matrix** (`run_contest_slice`): the candidate list simply grows 3×;
  `pairs[shard::total]` sharding and the shard-result merge (`merge_shard_rows`) are
  unchanged in shape. No order data leaves the app — the policy string is not sensitive.
- **Cloud payload parity:** `operator_pick` round-trips in the config
  (`build_payload`/`parse_payload` via `to_dict`/`from_dict`) and each candidate tuple
  carries its policy, so a cloud run and a local run of the same book choose the same
  winner (the byte-identical-contest principle).

**Scoring is unchanged.** No operator term is added — the winner is still chosen by
`total_late_days + weighting…` exactly as today. Operator-pick only changes which
schedule each candidate produces.

### 4. Apply persists the winner; the plan reproduces it

`api/main._optimize_apply` (and the auto path via `_finalize_optimize`): when a plan
is applied, persist the winning `operator_pick` into the saved plan config
(`book_store.save_plan_config`), beside the overlap and machine-set it already
persists.

`api/main._plan` reads `config.operator_pick` and (via `run()` → `_plan_config`)
schedules with that policy, so the schedule, Gantt, and analytics everyone sees
reproduce the optimized winner. `_incumbent_metrics` / `_metrics_for_ranks` go through
the same config, so the auto-apply gate (worst-order + committed-promise
no-regression) compares best vs incumbent on a consistent policy basis.

### 5. Progress budget reflects the bigger contest

The three `sweep_total_evals` sites in `api/main.py` (the initial local-mode estimate
in `_start_optimize` plus the two cloud-dispatch-failure/timeout fallbacks) already
×2 for the machine-set dimension on the new engine. They now also ×2 for the two
operator policies (net ×4 vs the pre-machine-set baseline), so the progress bar stays
honest. Classic/flow are unaffected. (The multiplier is `len(OPERATOR_PICK_CANDIDATES)`,
not a hardcoded 2, so adding `flexible` back needs no further edit here.)

### 6. Settings shows it read-only

`web/index.html` + `web/app.js`: a read-only line in the Scheduling settings group,
beside the read-only overlap and machine-set lines — e.g. *"Operator strategy: scarce
(chosen by Optimize)"* — sourced from the plan response's config echo. The user can
see which policy the current plan uses but cannot hand-edit it (the optimizer owns it).

## What stays untouched

- The 3 rules, the scheduler's placement/staffing logic, capture, freeze, the
  committed-promise cap, operator rotation, and absences — no behavior change. (The
  `candidate_operator` picker already supports all three policies; we only choose which
  one runs.)
- The score / objective — no operator term; delivery dates remain the only goal.
- The classic/flow engines and the golden test — the dimension is new-engine only.
  `contest_jobs` gates `picks` on `scheduler=="new"`; classic/flow keep their own
  operator logic and see only `"scarce"`.
- With `operator_pick="scarce"` (the default until the optimizer applies a different
  winner), every plan is **byte-identical** to today.
- Frozen in-progress ops stay pinned to their last-applied **machine and operator** in
  every pass — they are pre-placed before the scheduling loop and are not subject to
  the pick policy — so the floor's running work is never re-crewed by this feature.

## Edge cases (each gets a regression test)

1. **Frozen op is untouched by the policy.** A now-in-progress op is pinned to its
   last-applied operator; verify that pin holds identically under all three policies
   (the picker is never consulted for frozen ops).
2. **A shift with only one qualified free operator.** All three policies must pick the
   same (only) person — the policy only matters when there is a genuine choice; confirm
   no divergence and no double-booking when the pool collapses to one.
3. **Cloud == local parity.** Both sweep all three policies identically and choose the
   same winner on the same book.
4. **Absent / removed operator.** A policy must never select an absent or
   no-longer-qualified operator (the free/qualified/available filter runs before the
   policy); confirm under each policy.

## Testing

- **Byte-identical guard:** `operator_pick="scarce"` → new-engine plan identical to
  today; golden (classic) green. A book where scarce stays the winner applies an
  identical result.
- **Wiring proven behaviorally (per the standing lesson "test behaviour before
  explaining it"):** on a crafted book with a flexible and a specialist operator
  contending for a machine, `operator_pick="balanced"` produces a *different operator
  assignment* than `"scarce"` — proving `_plan_config` actually carries the policy into
  the live engine, verified in code not by assertion. (`"flexible"` is still exercised
  by a unit test at the engine level even though the contest doesn't sweep it, so the
  dropped policy stays regression-covered.)
- **Config round-trip + `_inputs_signature`:** `operator_pick` survives
  to_dict/from_dict, defaults to `"scarce"` on missing/blank, rejects an invalid value,
  and changes the inputs signature (staleness flag) when it differs.
- **Contest engages the dimension:** `contest_jobs` includes `scarce` + `balanced` for
  the new engine and only `"scarce"` for classic/flow; `pick_winner`/`merge_shard_rows`
  carry the winning policy; ties resolve to `scarce`.
- **Local fallback** sweeps both swept policies and keeps the best.
- **Apply persists the policy; `_plan` reproduces the winner** (dates match the applied
  result's expected_end).
- **Auto-apply gate consistency** under a non-scarce winner (incumbent and best scored
  on their own policies; committed/worst gates behave).
- **The four edge cases above.**
- **Test8 re-measurement:** scarce-only contest vs the operator-pick-aware contest —
  report makespan / late-days / worst / bands and **which policy won**, so the owner
  sees the real before/after and whether Approach 2 is worth pursuing.

## Cost

Sweeping two policies roughly **doubles** the contest on top of the machine-set
doubling (net ~4× the pre-machine-set baseline). The 20-way GitHub matrix and the
`OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE` knob absorb most of this: added candidates are
sharded across the 20 runners, and per-candidate depth can be traded against
wall-clock by lowering the budget. The daily "Done entering — update plan" contest
blocks and waits (owner previously accepted ~15–20 min), so after shipping we
**measure actual wall-clock on Test8 and the owner tunes the budget** as done for the
matrix and machine-set rollouts. If the wait ever becomes intolerable, the cheapest
lever is to shrink `OPERATOR_PICK_CANDIDATES` further (e.g. drop `balanced` too during
the daily run) — a one-line change.

## Deferred (explicitly out of scope)

- **Approach 2 — bottleneck-first policy:** a new demand-aware policy that steers
  flexible operators toward the machine with the most pending qualified demand (the
  owner's worker-A-to-VMC scenario). It would slot in as a fourth
  `OPERATOR_PICK_CANDIDATES` value using the same contest scaffolding and the same
  feasibility guarantee. Build it only if Approach 1's Test8 measurement shows a
  meaningful spread between the existing policies.
  **Why this is the real lever (independent analysis, 2026-08-02):** all three
  built-in policies decide who mans a machine from a *fixed property of the person*
  (flexibility count, or accumulated load) and never look at the *demand* on the
  machines those people could otherwise serve. `scarce` uses "fewer qualified
  machines = more precious" as a proxy for demand, but the proxy misfires when a
  *less*-flexible operator is qualified on a hot bottleneck: e.g. Anil{CNC1,CNC2,VMC1
  all light} vs Bimal{CNC1, GRIND-with-a-backlog}. Filling CNC1, `scarce` picks Bimal
  (2 < 3) and strands GRIND (Bimal was its only free operator); the correct call is
  Anil on CNC1, Bimal held for GRIND. A demand-aware policy makes that call; `scarce`
  cannot. `balanced`, by contrast, is expected to be mostly a *fairness* lever
  (it changes who is tired, rarely when orders ship) — so if Approach 1 shows little
  delivery movement, that is evidence to invest in the demand-aware policy, not to
  conclude operator assignment is a dead end. Two heavier variants noted for later:
  a one-step look-ahead ("does this assignment strand another machine's only
  operator this shift?"), and a per-shift min-cost matching of operators→machines
  (the strongest, but it replaces the greedy pick loop). A non-scheduling by-product
  worth surfacing: the same analysis can quantify where **cross-training** pays off
  (e.g. "qualify Chandu on VMC1 → −N late-days"), a business decision for the owner.
- **Approach 3 — full per-op operator-assignment search:** rejected as near the
  certified capacity floor already, for large added cost.
- Any operator-fairness / load-balancing term in the objective (owner: not a goal).
