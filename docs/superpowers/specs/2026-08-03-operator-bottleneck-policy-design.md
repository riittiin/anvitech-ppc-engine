# Demand-aware "bottleneck" operator policy (Approach 2, Level 2) — design

**Date:** 2026-08-03
**Status:** approved (owner), ready for implementation plan
**Builds on:** the shelved branch `operator-assignment-optimize-dimension` (spec
`2026-08-02-operator-assignment-optimize-dimension-design.md`). That branch's plumbing —
`Config.operator_pick`, the `_plan_config` wiring, the contest axis, persist/replay, the
read-only Settings line — is the FOUNDATION here and is reused unchanged. This spec adds
one new **policy value** (`"bottleneck"`) and the scheduler logic behind it.

## Why this exists (measured motivation)

The coarse operator dimension (`scarce` vs `balanced`) was built and measured on Test8 at
full depth (2026-08-03, budget 700, 36,400 plans): **zero benefit** — `scarce` won outright
(makespan 56.64 d / late-days 1553), `balanced` never beat it. That confirmed the spec's own
prediction: the three built-in policies are **myopic** — they choose from a fixed property of
the *person* (flexibility count, or accumulated load) and never look at the *demand* on the
machines those people could otherwise serve. `scarce` approximates "keep precious people
free" with a crude proxy (fewer qualified machines = more precious), which misfires when a
*less*-flexible operator is the only hand for a *busy* machine (the GRIND example in the prior
spec).

**Level 2** replaces that proxy with the real signal: how loaded each machine is, plus a
one-step check for whether taking a person now would strand another machine. It is the
owner's original "machines demand from the operator" idea, and the principled version of the
per-decision "mix" (scarce here, balanced there) that a single global policy cannot express.

**Honest expectation:** prior research put the crew near its certified capacity floor (~37.2
d, operators binding), and `scarce` won cleanly, so this may still measure near-zero. That is
an acceptable, cheap, well-isolated test of the last real idea in operator assignment. If it
is flat, the lever is genuinely tapped and only Level 3 (per-shift matching) would remain.

## Where operators are picked today (verified in code)

`ppc_engine/scheduler/staffing.py::StaffingBoard.candidate_operator` chooses among the
**free, qualified, on-shift, available** operators for one (machine, shift, window):

```python
free = [op for op in self._pools.get(machine.id, ())
        if effective_shift(op, day, config) == shift
        and masters.calendar.is_operator_available(op.name, day)
        and self.free_during(op.name, start, end)]
if not free: return None
pick = getattr(config, "operator_pick", "scarce")
if pick == "flexible": return free[-1].name
if pick == "balanced": return min(free, key=lambda o: (self._load.get(o.name,0.0), o.flexibility, o.name)).name
return free[0].name  # scarce: pool pre-sorted (flexibility, name)
```

The board already exposes everything Level 2 needs: `self._pools[m']` (qualified operators
per machine) and `self.free_during(name, start, end)` (who is free in a window). The one
thing it lacks is a per-machine **demand** signal — this spec adds it. The board is built once
in `decode` (`ppc_engine/scheduler/flow_scheduler.py:98`,
`staffing = StaffingBoard(build_machine_pools(masters))`), where `orders` + `masters.routings`
are in scope, so demand can be computed there and passed in.

## Design

### 1. A per-machine demand weight, computed once

In `decode`, before building the board, compute `demand: dict[str, float]` — for every
machine, the total remaining processing minutes of the in-house ops that could run on it:

- For each order in `orders`, for each of its routing operations that is an in-house
  machining/manual/inspection op (skip OS/DISPATCH milestones), take the op's processing
  minutes at the order's **remaining** quantity (the same duration the scheduler itself uses,
  via the existing duration helper — not a re-derivation).
- Add that op's minutes to `demand[m]` for each machine `m` in the op's `machine_options`,
  divided by the number of options (`minutes / len(machine_options)`) — the expected share if
  the work spreads evenly across its allowed machines. (Dividing avoids a machine that merely
  *appears* as an alternative for many ops looking busier than it can actually be.)

`demand` is static for the plan (computed once), cheap, and deterministic. It is passed into
`StaffingBoard(pools, demand=demand)`; the default is an empty dict.

### 2. The `bottleneck` pick

Add a fourth branch to `candidate_operator` (guarded by `pick == "bottleneck"`). Among the
`free` candidates for machine `M` in window `[start, end)`, assign the **least-precious-
elsewhere** person by this cost, keeping the precious ones free:

```
cost(O) = Σ over m' in qualified_machines(O), m' != M, demand[m'] > 0:
              demand[m'] / (1 + others_free(m', O, start, end))
```

where `others_free(m', O, start, end)` = the number of operators in `self._pools[m']`, other
than `O`, who are `free_during(start, end)` (and on-shift/available — reuse the same eligible
filter). Interpretation:

- If **no** other qualified operator is free for `m'` in this window (`others_free == 0`),
  the term is the full `demand[m']` — assigning `O` here **strands** `m'` (the one-step
  look-ahead), so `O` is heavily penalized as "precious right now."
- If several others could cover `m'`, the term shrinks toward zero — `O` is not precious for
  `m'`, so using `O` here is cheap.

Pick `argmin cost(O)`; ties break by the existing scarce order `(flexibility, name)` so the
result is fully deterministic and, when demand is flat/empty, **identical to `scarce`**.

This single formula unifies both Level-1 demand-weighting (`demand[m']`) and the Level-2
strand look-ahead (`others_free`), which is why we go straight to Level 2 (owner decision).

### 3. Make it a contest candidate

`engine/optimizer.OPERATOR_PICK_CANDIDATES` becomes `("scarce", "bottleneck")` — **drop
`balanced`** (it lost the Test8 contest). Everything else in the reused plumbing is unchanged:
`operator_pick_contenders` orders current-first; the contest sweeps sequence × overlap ×
machine-set × operator-pick; `pick_winner`/`merge_shard_rows` carry the winner; apply persists
it; `_plan` replays it. `flexible` and `balanced` remain valid engine values (still
unit-tested) but are not swept.

### 4. Settings label

Extend the read-only "Operator strategy" echo (`web/app.js`) with a friendly label for the
new value, e.g. `bottleneck → "Send help where it's needed most"`. Display-only, optimizer-
owned, same as the others.

## What stays untouched / safe

- **Byte-identical default:** `operator_pick` still defaults to `"scarce"`; `bottleneck` only
  runs when the contest applies it. With an empty `demand` map (or flat demand), the
  `bottleneck` branch resolves to the exact `scarce` order — so even a bottleneck plan degrades
  gracefully to today's behavior when there is no demand signal.
- **Feasibility guaranteed:** the pick is still only ever among `free` (already-free,
  qualified, on-shift) operators, so no policy — including `bottleneck` — can produce an
  infeasible or double-booked schedule. `demand` and `others_free` only reorder *which* free
  person is chosen.
- **Classic/flow engines** ignore `operator_pick` entirely (single-pass, their own logic).
- **Frozen in-progress ops** are pre-placed before the loop and never consult the picker —
  the floor's running work is never re-crewed.
- **Scoring/objective unchanged** — no operator term added; late-days + makespan remain the
  only goal (owner: fewer late deliveries is the point; fairness is not a goal).
- The `demand` computation only *reads* orders/routings; it never mutates a plan.

## Edge cases (each gets a regression test)

1. **Flat/empty demand → `bottleneck` == `scarce`** (graceful degradation; byte-identical).
2. **Single free candidate** → all policies pick the same person (no choice to make).
3. **The GRIND case** (crafted): operator A qualified on quiet machines + operator B qualified
   on a high-demand machine both free for a routine machine → `bottleneck` assigns A and keeps
   B free; `scarce` (which sees B as *less* flexible) wrongly takes B. Assert they differ and
   that `bottleneck` makes the demand-correct choice.
4. **Strand look-ahead:** when B is the *only* free qualified operator for a demanded machine
   `m'`, `bottleneck` must not assign B elsewhere if an alternative exists for the machine at
   hand — assert the strand term dominates.
5. **Absent / removed operator** never selected (the free/eligible filter runs first).

## Testing

- **Byte-identical guard:** `operator_pick="scarce"` unchanged; the golden trace + full suite
  stay green; a bottleneck plan with empty demand equals the scarce plan.
- **Demand computation:** unit-test `demand[m]` on a crafted book (known cycle×qty, known
  options) equals the hand-computed expected-share values; OS/DISPATCH excluded.
- **`bottleneck` pick logic:** the five edge cases above, exercised through the real
  `candidate_operator` (not a re-implementation) with a crafted `StaffingBoard` + `demand`.
- **Behavioral end-to-end:** through `new_engine._plan_config` + `decode`, `operator_pick=
  "bottleneck"` produces a different, demand-correct operator assignment than `scarce` on a
  crafted contended+demand workbook (verify wiring in code, per the standing lesson).
- **Contest wiring:** `OPERATOR_PICK_CANDIDATES == ("scarce","bottleneck")`; the contest can
  select `bottleneck` when it wins; persist/replay round-trips the value.
- **Test8 re-measurement:** the same controlled before/after harness used for the coarse
  version — before = `scarce` (live), after = best of `scarce`+`bottleneck`, full depth
  (budget 700) — report makespan / late-days / worst and which policy won. This is the
  go/no-go for shipping Level 2 and the trigger for deciding on Level 3.

## Cost

The operator axis stays **2 policies** (`scarce`, `bottleneck` — `balanced` dropped), so the
contest size is unchanged from the shelved branch (no new compute vs the coarse version). The
`demand` precompute is O(orders × ops), negligible. `others_free` adds a small per-pick scan
over the candidate's other machines' pools — bounded and cheap.

## Deferred (out of scope)

- **Level 3 — per-shift min-cost matching:** re-solve the whole operator→machine assignment
  each shift (optimal pairing) instead of the greedy per-op pick. The strongest version, but
  it replaces the decode loop's staffing step — a much larger, riskier build. Build only if
  Level 2's Test8 measurement is flat.
- **Cross-training decision-support:** quantify where qualifying an operator on another
  machine would cut late-days. A reporting by-product, not a scheduler change.
- Any operator-fairness term in the objective (owner: not a goal).
