"""The objective — how good a schedule is.

The ONE home of the scoring rule (RULES.md Rule 3): minimize total tardiness with a
fairness guard against starving any single order, then makespan. The scheduler itself
is objective-agnostic; only this package knows what "better" means.
"""

from ppc_engine.objective.metrics import PlanMetrics, compute_metrics, order_lateness_days
from ppc_engine.objective.objective import score

__all__ = ["PlanMetrics", "compute_metrics", "order_lateness_days", "score"]
