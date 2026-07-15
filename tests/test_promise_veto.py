"""The promise ceiling: end.date() <= promised_date for every committed/urgent
order, or the candidate plan is discarded (score = inf)."""
import io
from datetime import date, timedelta

from engine import optimizer
from engine.config import Config, OVERLAP_PERCENT
from engine.loaders import load_all
from engine.models import PlanRun
from engine.pipeline import run_forward
from tests.sample_workbook import build_sample_bytes


def _lines(cfg_overlap=80):
    so, masters = load_all(io.BytesIO(build_sample_bytes()))
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=cfg_overlap,
                 plan_start_date=date(2025, 3, 1))
    cfg.validate()
    return so, masters, cfg


def _end_date(schedule, key):
    d = None
    for e in schedule:
        for r in (e.so_refs or []):
            if (r, e.item_code) == key and (d is None or e.end.date() > d):
                d = e.end.date()
    return d


def test_promise_ceiling_ok_day_level():
    so, masters, cfg = _lines()
    pr = PlanRun(so_lines=list(so)); run_forward(pr, cfg, masters)
    k = (so[0].so_no, so[0].item_code)
    end = _end_date(pr.schedule, k)
    so[0].commitment, so[0].promised_date = "committed", end
    assert optimizer.promise_ceiling_ok(pr.schedule, so)          # on the day = fine
    so[0].promised_date = end - timedelta(days=1)
    assert not optimizer.promise_ceiling_ok(pr.schedule, so)      # one day late = veto
    so[0].commitment, so[0].promised_date = "open", None
    assert optimizer.promise_ceiling_ok(pr.schedule, so)          # open = never vetoed


def test_optimize_feasible_gate_yields_none_when_all_vetoed():
    so, masters, cfg = _lines()
    r = optimizer.optimize(so, cfg, masters, budget_evals=8, seed=42,
                           feasible=lambda schedule: False)
    assert r.best is None and not r.ranks


def test_optimize_feasible_gate_passthrough_when_always_true():
    so, masters, cfg = _lines()
    a = optimizer.optimize(so, cfg, masters, budget_evals=8, seed=42)
    b = optimizer.optimize(so, cfg, masters, budget_evals=8, seed=42,
                           feasible=lambda schedule: True)
    assert a.best == b.best and a.ranks == b.ranks
