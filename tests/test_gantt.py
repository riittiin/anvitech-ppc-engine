"""Gantt view-model built from the Rule 6 schedule."""
from engine.config import Config
from engine.models import PlanRun
from engine.pipeline import run_forward
from engine.gantt import build_gantt


def test_empty_schedule():
    g = build_gantt([], [], None)
    assert g["rows"] == [] and g["num_days"] == 0


def test_gantt_from_real_run(loaded):
    so_lines, masters = loaded
    pr = PlanRun(so_lines=so_lines)
    run_forward(pr, Config(), masters)
    g = build_gantt(pr.schedule, pr.batches_prioritized, masters)

    # One row per consolidated batch, each carrying the SO identity columns.
    assert len(g["rows"]) == len(pr.batches_prioritized)
    r0 = g["rows"][0]
    for key in ("item_name", "item_code", "so_no", "so_qty", "so_delivery_date", "bars"):
        assert key in r0
    assert r0["bars"], "first row should have process bars"

    # Day axis covers the full schedule span.
    assert g["num_days"] >= 1
    assert len(g["days"]) == g["num_days"]
    assert sum(m["days"] for m in g["months"]) == g["num_days"]

    # Every bar references a coloured machine and a non-negative position.
    for row in g["rows"]:
        for bar in row["bars"]:
            assert bar["machine"] in g["machine_colors"]
            assert bar["offset_days"] >= 0
            assert bar["duration_days"] >= 0


def test_bar_offsets_are_time_accurate(loaded):
    so_lines, masters = loaded
    pr = PlanRun(so_lines=so_lines)
    run_forward(pr, Config(plan_start_date=__import__("datetime").date(2025, 3, 1)), masters)
    g = build_gantt(pr.schedule, pr.batches_prioritized, masters)
    # Axis anchors at midnight of day 1; the first process starts at 08:00, so the
    # earliest bar sits 8/24 of a day in (within the first day column, not at 0).
    first = min(b["offset_days"] for r in g["rows"] for b in r["bars"])
    assert 0.0 <= first < 1.0
    assert abs(first - 8 / 24) < 1e-3   # 08:00 first-shift start
