"""engine/optimize_service.py — the shared cloud/local Optimize service.

The contract that matters: a cloud worker rebuilding the book from a payload
and running the contest must produce BYTE-IDENTICAL results to the local
sweep on the same inputs (fixed seed, shared code path, shared winner rule).
"""
import io
import json
from dataclasses import replace
from datetime import date

from engine import optimize_service as svc
from engine import optimizer
from engine.config import Config, OVERLAP_PERCENT
from engine.loaders import load_all
from engine.models import Order, PlanRun
from engine.pipeline import run_forward
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _book(overlap=80):
    raw = build_sample_bytes()
    so_lines, masters = load_all(io.BytesIO(raw))
    orders = {}
    for i, so in enumerate(so_lines):
        o = Order(so.so_no, so.item_code, so.item_name, so.qty, so.delivery_date)
        orders[o.key] = o
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=overlap,
                 plan_start_date=date(2025, 3, 1))
    cfg.validate()
    return orders, [], raw, masters, cfg


def _payload(per_candidate=10, **kw):
    orders, actuals, raw, masters, cfg = _book(**kw)
    return svc.build_payload(orders, actuals, raw, cfg, seed=42,
                             budget_per_candidate=per_candidate), masters, cfg


def test_payload_round_trip_reconstructs_the_same_plan():
    payload, masters, cfg = _payload()
    # Simulate the network hop: the worker sees only JSON.
    payload = json.loads(json.dumps(payload))
    orders2, actuals2, masters2, cfg2 = svc.parse_payload(payload)

    setup = svc.prepare_contest(orders2, actuals2, masters2, cfg2)
    pr = PlanRun(so_lines=list(setup.target))
    run_forward(pr, setup.config, masters2)
    m2 = optimizer.plan_metrics(pr.schedule, setup.target, setup.config.plan_start_date)

    orders, actuals, raw, masters, cfg = _book()
    setup1 = svc.prepare_contest(orders, actuals, masters, cfg)
    pr1 = PlanRun(so_lines=list(setup1.target))
    run_forward(pr1, setup1.config, masters)
    m1 = optimizer.plan_metrics(pr1.schedule, setup1.target,
                                setup1.config.plan_start_date)
    assert m1 == m2


def test_run_contest_matches_the_local_sweep_byte_for_byte():
    """Cloud (run_contest from a payload) == local (sweep_optimize on live
    objects) for the same contenders, per-candidate depth, and seed."""
    payload, masters, cfg = _payload(per_candidate=10)
    payload = json.loads(json.dumps(payload))
    payload["candidates"] = list(optimizer.OVERLAP_CANDIDATES)
    n = len(optimizer.sweep_contenders(cfg.overlap_percent,
                                       optimizer.OVERLAP_CANDIDATES))
    cloud = svc.run_contest(payload, processes=1)

    orders, actuals, raw, masters, cfg = _book()
    setup = svc.prepare_contest(orders, actuals, masters, cfg)
    local = optimizer.sweep_optimize(setup.target, setup.search_config, masters,
                                     budget_evals=10 * n, seed=42)
    assert cloud["winner_overlap"] == local.overlap_percent
    assert cloud["best"] == local.result.best
    assert cloud["ranks"] == local.result.ranks
    assert cloud["evals"] == local.evals


def test_run_contest_parallel_equals_sequential():
    payload, _, _ = _payload(per_candidate=6)
    payload = json.loads(json.dumps(payload))
    a = svc.run_contest(payload, processes=1)
    b = svc.run_contest(payload, processes=2, poll_seconds=0.2)
    assert (a["winner_overlap"], a["best"], a["ranks"]) == \
           (b["winner_overlap"], b["best"], b["ranks"])


def test_pick_winner_tie_keeps_the_current_setting():
    best = {"makespan_days": 1.0, "late_orders": 0, "total_late_days": 0,
            "max_late_days": 0, "orders": 1}
    rows = [{"overlap": 50, "eligible": True, "best": dict(best)},
            {"overlap": 80, "eligible": True, "best": dict(best)}]
    assert svc.pick_winner(80, rows)["overlap"] == 80
    assert svc.pick_winner(50, rows)["overlap"] == 50
    # Ineligible/failed rows never win.
    rows[1]["eligible"] = False
    assert svc.pick_winner(80, rows)["overlap"] == 50


def test_prepare_contest_raises_when_nothing_to_optimize():
    orders, actuals, raw, masters, cfg = _book()
    import pytest
    with pytest.raises(ValueError):
        svc.prepare_contest({}, [], masters, cfg)


def test_run_candidate_progress_and_cancel():
    payload, _, _ = _payload(per_candidate=8)
    seen = []
    row = svc.run_candidate(payload, 80, on_progress=lambda e, b: seen.append(e))
    assert row["eligible"] and row["evals"] <= 8 and row["ranks"]
    assert seen == sorted(seen) and seen[-1] == row["evals"]
    stopped = svc.run_candidate(payload, 80, should_cancel=lambda: True)
    assert stopped["cancelled"] or stopped["evals"] <= 1


def test_book_signature_tracks_material_changes():
    orders, actuals, raw, masters, cfg = _book()
    from engine import orderbook
    lines = orderbook.active_so_lines(orders, actuals, masters)
    s0 = svc.book_signature(lines)
    assert s0 == svc.book_signature(list(reversed(lines)))   # order-insensitive
    lines[0].qty -= 1                                        # production happened
    assert svc.book_signature(lines) != s0
    lines[0].qty += 1
    lines[0].commitment = "committed"                        # lane change
    lines[0].promised_date = date(2025, 4, 1)
    assert svc.book_signature(lines) != s0
    lines[0].commitment, lines[0].promised_date = "open", None
    assert svc.book_signature(lines) == s0                   # restored ⇒ same sig
    assert svc.book_signature(lines, absences=[{"operator": "X",
        "from_date": "2025-03-02", "to_date": "2025-03-03"}]) != s0
