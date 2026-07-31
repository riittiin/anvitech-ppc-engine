"""Sharded-contest helpers: contest_jobs order, merge equivalence, and (Task 2)
run_contest_slice union-equivalence to a whole run_contest."""
import io
from datetime import date

from engine import optimize_service as osvc
from engine import optimizer
from engine.config import Config
from engine.models import Order


def _classic_payload(loaded, config, *, budget=6, candidates=(60, 80)):
    """A cloud payload for the (classic-mode) sample book at a tiny budget
    (fast tests) — built via the real build_payload so the payload shape
    matches production exactly (mirrors api.main._start_optimize's call)."""
    so_lines, masters = loaded
    orders = {}
    for sl in so_lines:
        o = Order(sl.so_no, sl.item_code, sl.item_name, sl.qty, sl.delivery_date)
        orders[o.key] = o
    return osvc.build_payload(orders, [], None, config, seed=1,
                              candidates=candidates, budget_per_candidate=budget)


def _new_engine_payload(*, budget=6, candidates=(60, 80)):
    """A cloud payload on the fully-staffed new-engine sample workbook (mirrors
    tests/test_optimize_service.py::_new_engine_payload) so scheduling actually
    runs end to end under scheduler=='new' (the classic sample book isn't
    fully staffed for the new engine — test_new_engine.py always uses this
    one). ``overlap_percent`` is pinned to a candidate value so the contest
    stays at exactly len(candidates) contenders (fast)."""
    from engine.loaders import load_all
    from tests.new_sample_workbook import build_new_sample_bytes
    raw = build_new_sample_bytes()
    so_lines, masters = load_all(io.BytesIO(raw))
    orders = {}
    for sl in so_lines:
        o = Order(sl.so_no, sl.item_code, sl.item_name, sl.qty, sl.delivery_date)
        orders[o.key] = o
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
                apply_operator_logic=True, overlap_percent=candidates[0])
    cfg.validate()
    return osvc.build_payload(orders, [], raw, cfg, seed=1,
                              candidates=list(candidates), budget_per_candidate=budget)


def test_contest_jobs_order_matches_run_contest():
    payload = _new_engine_payload()
    jobs = osvc.contest_jobs(payload)
    # new engine → two machine-sets × the contenders, flex-outer/overlap-inner.
    cfg = Config.from_dict(payload["config"])
    knob, _ = optimizer.knob_for(cfg)
    contenders = optimizer.sweep_contenders(getattr(cfg, knob), payload["candidates"])
    expected = [(ov, flex) for flex in (False, True) for ov in contenders]
    assert jobs == expected


def test_contest_jobs_classic_single_machineset(loaded, config):
    payload = _classic_payload(loaded, config)  # config fixture is scheduler="classic"
    jobs = osvc.contest_jobs(payload)
    assert all(flex is False for _ov, flex in jobs)


def test_merge_shard_rows_equivalent_to_run_contest():
    payload = _new_engine_payload()
    full = osvc.run_contest(payload, processes=1)
    # Reproduce the rows the contest computed, then merge them ourselves:
    rows = [osvc.run_candidate(payload, ov, flex) for ov, flex in osvc.contest_jobs(payload)]
    merged = osvc.merge_shard_rows(payload, rows,
                                   sum(r["evals"] for r in rows),
                                   any(r["cancelled"] for r in rows))
    assert merged["winner_overlap"] == full["winner_overlap"]
    assert merged["winner_flexible"] == full["winner_flexible"]
    assert merged["ranks"] == full["ranks"]
