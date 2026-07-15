"""The promise ceiling: end.date() <= promised_date for every committed/urgent
order, or the candidate plan is discarded (score = inf)."""
import io
from datetime import date, timedelta

from engine import optimize_service as svc
from engine import optimizer
from engine.config import Config, OVERLAP_PERCENT
from engine.loaders import load_all
from engine.models import Order, PlanRun
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


def test_promise_ceiling_fails_closed_when_order_missing_from_schedule():
    """A promised (committed/urgent) order with NO schedule entries is a
    violation, not a pass — Rule 6 can block a batch non-fatally with zero
    entries emitted, and an unschedulable committed order must not sail
    past the veto as "on time"."""
    so, masters, cfg = _lines()
    pr = PlanRun(so_lines=list(so)); run_forward(pr, cfg, masters)
    k = (so[0].so_no, so[0].item_code)
    end = _end_date(pr.schedule, k)
    so[0].commitment, so[0].promised_date = "committed", end
    # Remove every entry carrying this order's key from the schedule.
    filtered = [e for e in pr.schedule
                if not (e.item_code == k[1] and k[0] in (e.so_refs or []))]
    assert _end_date(filtered, k) is None                   # really absent now
    assert not optimizer.promise_ceiling_ok(filtered, so)   # fail CLOSED


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


# --------------------------------------------------------------------------- #
# Task 8: the contest goes one-pool — joint search space, veto instead of a wall
# --------------------------------------------------------------------------- #
def _orders_from_lines(so_lines):
    return {(s.so_no, s.item_code): Order(s.so_no, s.item_code, s.item_name,
                                          s.qty, s.delivery_date,
                                          commitment=getattr(s, "commitment", "open") or "open",
                                          promised_date=getattr(s, "promised_date", None))
            for s in so_lines}


def test_joint_contest_includes_committed_and_respects_promises():
    """With a committed order present, prepare_contest's joint_target must
    include EVERY active line (committed included) and carry a feasible gate.
    Running a real contender (svc.run_candidate) over that joint pool must
    never produce a winner that breaks the promise when replayed."""
    so, masters, cfg = _lines()
    raw = build_sample_bytes()
    pr = PlanRun(so_lines=list(so)); run_forward(pr, cfg, masters)
    k0 = (so[0].so_no, so[0].item_code)
    end0 = _end_date(pr.schedule, k0)
    so[0].commitment, so[0].promised_date = "committed", end0

    orders = _orders_from_lines(so)
    setup = svc.prepare_contest(orders, [], masters, cfg)
    assert setup.feasible is not None
    assert len(setup.joint_target) == len(so)
    assert any(l.commitment == "committed" for l in setup.joint_target)

    payload = svc.build_payload(orders, [], raw, cfg, seed=42, budget_per_candidate=8)
    row = svc.run_candidate(payload, cfg.overlap_percent)
    assert row["eligible"]
    if row["best"] is not None:
        pr2 = PlanRun(so_lines=list(so))
        run_forward(pr2, setup.search_config, masters, priority_rank=row["ranks"])
        assert optimizer.promise_ceiling_ok(pr2.schedule, so)


def test_all_open_book_contest_unchanged():
    """No promised orders present: joint mode must be indistinguishable from
    today's open-only search — same target list, no feasible gate."""
    so, masters, cfg = _lines()
    orders = _orders_from_lines(so)
    setup = svc.prepare_contest(orders, [], masters, cfg)
    assert setup.feasible is None
    assert setup.joint_target == setup.target
