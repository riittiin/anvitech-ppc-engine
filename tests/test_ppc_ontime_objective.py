"""On-time objective, ppc_engine side (spec 2026-08-06, REVISED 2026-09-05).

Mirror of tests/test_ontime_objective.py. The two scorers must agree exactly — see
tests/test_scorer_mirror.py.
"""
from datetime import datetime

from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.objective.objective import _ontime_breach, score

CFG = PlanConfig(plan_start=datetime(2026, 8, 6, 8, 0))


def _pm(lateness, makespan=0.0):
    """lateness_by_order values are SIGNED days: negative means early."""
    return PlanMetrics(
        total_tardiness_days=0.0,
        max_tardiness_days=0.0,
        late_order_count=0,
        makespan_days=makespan,
        lateness_by_order={(f"SO{n}", "x"): float(v) for n, v in enumerate(lateness)},
        promise_slip_by_order={},
    )


def test_finishing_early_costs_a_quarter_of_finishing_late():
    assert _ontime_breach(_pm([30]), CFG) == 1200.0          # 30^2 + 10*30
    assert _ontime_breach(_pm([-30]), CFG) == 56.25          # (0.25*30)^2
    assert _ontime_breach(_pm([-30]), CFG) < _ontime_breach(_pm([30]), CFG)
    assert _ontime_breach(_pm([-30]), CFG) > 0


def test_no_late_day_is_free():
    for d in (1, 2, 3, 4):
        assert _ontime_breach(_pm([d]), CFG) > 0.0, f"{d} days late must not be free"
    assert _ontime_breach(_pm([0]), CFG) == 0.0


def test_one_day_late_costs_the_square_plus_the_linear_term():
    assert _ontime_breach(_pm([1]), CFG) == 11.0             # 1 + 10
    assert _ontime_breach(_pm([-1]), CFG) == 0.0625          # (0.25)^2, no linear term


def test_squaring_still_spreads_the_misses():
    """Ten orders 6 days out must still beat one order 30 days out — the requirement
    that caps ontime_late_linear_weight at 18."""
    assert _ontime_breach(_pm([6] * 10), CFG) == 960.0
    assert _ontime_breach(_pm([30]), CFG) == 1200.0
    assert _ontime_breach(_pm([6] * 10), CFG) < _ontime_breach(_pm([30]), CFG)


def test_cap_limits_the_squared_term_but_not_the_linear_one():
    assert _ontime_breach(_pm([100]), CFG) == 3600.0 + 10 * 100
    assert _ontime_breach(_pm([64]), CFG) == 3600.0 + 10 * 64
    assert _ontime_breach(_pm([100]), CFG) > _ontime_breach(_pm([64]), CFG)


def test_score_is_the_ontime_term_plus_a_makespan_tiebreak():
    """With both guards dormant, the score is exactly these two terms."""
    m = _pm([10], makespan=50.0)
    expected = CFG.ontime_weight * (100.0 + 10 * 10) + CFG.makespan_weight * 50.0
    assert abs(score(m, CFG) - expected) < 1e-9


def test_makespan_cannot_outrank_the_ontime_term():
    shorter_but_worse = _pm([8], makespan=10.0)     # 8^2 + 80 = 144
    longer_but_better = _pm([0], makespan=110.0)    # exactly on time -> 0
    assert score(longer_but_better, CFG) < score(shorter_but_worse, CFG)
