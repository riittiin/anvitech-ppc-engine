"""The objective function — one number, lower is better.

This is the ONLY place the scoring rule lives (RULES.md Rule 3):

    score = total_tardiness            # every late day on every order counts
          + λ · max_tardiness          # fairness: don't starve any single order
          + w · makespan               # a strict secondary tie-breaker

The two lessons baked in:
  - We minimise the *sum* of tardiness (never the *count* of late orders), so no
    order is ever abandoned to polish another.
  - The λ·max_tardiness term is the fairness guard the old build never had — it stops
    a savable order being pushed dramatically late for the others' sake.
λ (fairness_weight) and w (makespan_weight) live in PlanConfig.
"""

from __future__ import annotations

from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics


def _severity(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Convex, capped per-order tardiness — the reputation guard. Each order's
    lateness beyond a tolerance is squared (accelerating) and capped, so a savable
    order is never dumped for the aggregate and one impossible order can't dominate.
    Protects EVERY order, not just the single worst (that was max_tardiness's blind
    spot — see the 2026-07-24 spec)."""
    tol = config.severity_tolerance_days
    cap = config.severity_cap_days
    total = 0.0
    for late in metrics.lateness_by_order.values():
        over = late - tol            # `late` is signed; early/on-time -> <= 0
        if over <= 0.0:
            continue
        if over > cap:
            over = cap
        total += over * over
    return total


def score(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Score a plan from its metrics. Lower is better."""
    return (
        metrics.total_tardiness_days
        + config.severity_weight * _severity(metrics, config)
        + config.fairness_weight * metrics.max_tardiness_days
        + config.makespan_weight * metrics.makespan_days
    )
