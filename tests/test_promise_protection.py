from dataclasses import replace
from datetime import datetime

from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.objective.objective import score


def _metrics(latenesses, makespan=40.0):
    """Build a PlanMetrics from a list of signed per-order lateness (days)."""
    lb = {("SO", str(i)): float(v) for i, v in enumerate(latenesses)}
    tard = [max(0.0, v) for v in latenesses]
    return PlanMetrics(
        total_tardiness_days=sum(tard),
        max_tardiness_days=max(tard) if tard else 0.0,
        late_order_count=sum(1 for t in tard if t > 0),
        makespan_days=makespan,
        lateness_by_order=lb,
    )


def test_convex_term_protects_the_second_worst_order():
    # X is structurally impossible (~20 late and sets the max); B is savable.
    #   sacrifice: X=20, B pushed to 15   (what the old objective picked)
    #   protect:   X=22, B rescued to 2   (spread a little onto the doomed order)
    cfg = PlanConfig(plan_start=datetime(2025, 3, 1))
    sacrifice = _metrics([20.0, 15.0])
    protect = _metrics([22.0, 2.0])

    # OLD objective (severity off) WRONGLY prefers sacrificing B — the live bug:
    old = replace(cfg, severity_weight=0.0)
    assert score(protect, old) > score(sacrifice, old)

    # NEW objective (default convex term) prefers protecting B:
    assert score(protect, cfg) < score(sacrifice, cfg)
