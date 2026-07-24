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


from datetime import date
from types import SimpleNamespace as NS

from engine import optimizer


def test_optimizer_score_convex_protects_second_worst():
    # Same scenario as the ppc test, in the old-space metrics dict. Makespan is
    # equal on both plans, so only the severity term can flip the preference.
    sacrifice = {"total_late_days": 35, "makespan_days": 40.0,
                 "slip_severity": (20 - 2) ** 2 + (15 - 2) ** 2}
    protect = {"total_late_days": 24, "makespan_days": 40.0,
               "slip_severity": (22 - 2) ** 2 + 0}
    assert optimizer.score(protect) < optimizer.score(sacrifice)


def test_plan_metrics_slip_severity_is_convex_and_capped():
    # One order 15 days late: overage 13 -> 169. The first 2 days (tolerance) are free.
    entry = NS(end=__import__("datetime").datetime(2025, 3, 16, 10, 0),
               so_refs=["SO1"], item_code="A")
    lines = [NS(so_no="SO1", item_code="A", delivery_date=date(2025, 3, 1))]
    m = optimizer.plan_metrics([entry], lines, date(2025, 3, 1))
    assert m["max_late_days"] == 15
    assert m["slip_severity"] == (15 - 2) ** 2  # 169.0


def test_plan_metrics_severity_zero_within_tolerance():
    entry = NS(end=__import__("datetime").datetime(2025, 3, 3, 10, 0),
               so_refs=["SO1"], item_code="A")   # 2 days late == tolerance 2 -> free
    lines = [NS(so_no="SO1", item_code="A", delivery_date=date(2025, 3, 1))]
    m = optimizer.plan_metrics([entry], lines, date(2025, 3, 1))
    assert m["slip_severity"] == 0.0


import io

from engine import book_store, loaders, new_engine
from engine.config import Config
from tests.new_sample_workbook import build_new_sample_bytes


def test_new_engine_sequence_search_runs_reputation_aware():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)                 # new_engine reads masters from the store
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    config = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
                    apply_operator_logic=True)
    res = new_engine.optimize_sequence(so_lines, config, masters,
                                       budget_evals=60, seed=42)
    # The search produced a ranked plan and reported metrics including the guard.
    assert res.ranks
    assert res.best["total_late_days"] >= 0
    # slip_severity is present on the reported metrics (mirror wired end-to-end):
    assert "slip_severity" in res.best
