"""PlanConfig — the tunable knobs for one planning run.

Kept small and explicit. Objective weights live here so the *one* objective function
(objective/objective.py) reads them from a single place.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time


@dataclass(frozen=True)
class PlanConfig:
    """Configuration for a single plan.

    Time knobs:
        plan_start:  The datetime the plan clock starts from (nothing is scheduled
                     before this). A concrete datetime keeps the engine pure — the
                     API is responsible for resolving "today" into this value, so the
                     engine never has to know the real clock (a lesson from the old
                     build's magic ``None`` = today convention).
        first_start / first_end:   First-shift clock times (default 08:00 → 19:00).
        second_start / second_end: Second-shift clock times (default 19:00 → 05:00
                     next day). ``second_end`` < ``second_start`` means it crosses
                     midnight.
        week_anchor: A Friday used as the reference for operator shift rotation.
                     Operators flip shift each Friday after this date. Defaults to the
                     Friday on/before ``plan_start`` (resolved by the caller).

    Scheduling knobs:
        setup_min:   Setup minutes charged once per *machining* operation (RULES.md).

    Objective weights (used only by objective/objective.py):
        fairness_weight:  λ — weight on the worst single-order lateness (the
                          no-starvation guard, RULES.md Rule 3).
        makespan_weight:  w — weight on makespan (a strict secondary goal).
    """

    plan_start: datetime

    # shift clock times
    first_start: time = time(8, 0)
    first_end: time = time(19, 0)
    second_start: time = time(19, 0)
    second_end: time = time(5, 0)

    # rotation reference (a Friday); caller resolves the default
    week_anchor: date | None = None

    # scheduling
    setup_min: float = 90.0

    # Order consolidation: merge same-item orders whose due dates fall within this many
    # days into ONE production batch (combined qty, one set of setups instead of two).
    # Saves setup time and can pull both deliveries earlier — but a bigger batch can
    # delay the earlier-due order, so the best window is book-specific and auto-tuned
    # (like overlap). 0 = no consolidation (each order produced separately). Default.
    # See engine/consolidation.py and OPTIMIZATION.md.
    consolidation_window: float = 0.0

    # How to pick which free operator mans a machine for a shift (RULES.md: "ideal —
    # one operator per machine per shift", plus the owner's idea of choosing by
    # flexibility/load rather than grabbing any free one):
    #   "scarce"   : the LEAST-flexible free operator (keeps flexible people free for
    #                machines only they can run — the measured old-build win). Default.
    #   "balanced" : the LEAST-loaded free operator (spread work evenly), tie → scarce.
    #   "flexible" : the MOST-flexible free operator (for contrast / A-B testing).
    operator_pick: str = "scarce"

    # Operation overlap (pipelining) — the floor-practical alternative to chunking.
    # An in-house op may START when its predecessor is this fraction through CUTTING
    # (setup excluded), instead of waiting for full completion. Each op still runs as
    # ONE continuous batch on ONE machine with ONE operator and ONE setup — nothing is
    # fragmented — so operator-machine stability is preserved. The successor is paced
    # so it never finishes before the predecessor (can't process pieces not yet made).
    # 0.0 = sequential (no overlap); 0.5 = start at 50% done; 0.9 = start at 90% done.
    # OS/dispatch never overlap. See scheduler/flow_scheduler.py and OPTIMIZATION.md.
    overlap: float = 0.0

    # objective weights
    # λ = 30: the fairness-sweep (OPTIMIZATION.md §7a) showed λ≈30 protects the worst
    # order (max tardiness ≤ EDD) while still beating EDD on every axis — the
    # fairness-respecting default. Lower λ cuts total tardiness more but lets the
    # worst order regress.
    fairness_weight: float = 30.0
    makespan_weight: float = 0.1

    # Reputation guard (2026-07-24 spec). A CONVEX, capped per-order tardiness
    # penalty. Unlike fairness_weight (which only shields the SINGLE worst order),
    # this penalizes EVERY order's lateness on an accelerating curve, so no savable
    # order is sacrificed for the aggregate. Must equal engine/optimizer.py SEVERITY_* .
    # MEASURED on the real book (Test5, 71 orders, 2026-07-24): OFF->ON pushed ZERO
    # on-time orders late (the Aug-8 failure mode = 0 cases), RESCUED 12 late orders
    # to on-time, and cut total late-days 1596->1451; the only orders made worse were
    # already-late ones (the cap deliberately concentrates unavoidable slip there).
    # w2 and w4 gave identical plans, so mu=2 suffices. Re-measure before moving.
    #   severity_tolerance_days (T): first T late days cost nothing extra.
    #   severity_weight (mu):        strength of the squared overage.
    #   severity_cap_days:           overage capped at this many days before
    #                                squaring, so an impossible order can't dominate.
    severity_tolerance_days: float = 2.0
    severity_weight: float = 2.0
    severity_cap_days: float = 30.0

    # Worst-order ceiling barrier (2026-07-24 amendment). ceiling_days is the current
    # plan's worst lateness (days); the objective heavily penalizes any order pushed
    # PAST it, so re-optimization never worsens the worst order. None = no barrier
    # (byte-identical). ceiling_weight MEASURED on the real book — re-measure before
    # moving; must equal engine/optimizer.py CEILING_WEIGHT.
    ceiling_days: float | None = None
    ceiling_weight: float = 100.0
