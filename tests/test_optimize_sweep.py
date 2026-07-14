"""The Optimize settings sweep (2026-07-15 spec): one click also tunes the
overlap %, spending the SAME total budget (probe every candidate, deepen the
winner). Never worse than the current setting; ties keep the current setting;
promise-guard hook can veto candidates; Apply persists the winning overlap into
the saved plan config with a matching inputs fingerprint."""
import io
from dataclasses import replace
from datetime import date

import pytest

from engine import optimizer
from engine.config import Config, OVERLAP_PERCENT
from engine.loaders import load_all
from engine.optimizer import score
from tests.sample_workbook import build_sample_bytes


def _book(overlap=80):
    so_lines, masters = load_all(io.BytesIO(build_sample_bytes()))
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=overlap,
                 plan_start_date=date(2025, 3, 1))
    cfg.validate()
    return so_lines, cfg, masters


# --------------------------------------------------------------------------- #
# Pure sweep mechanics
# --------------------------------------------------------------------------- #
def test_sweep_never_worse_than_the_current_setting_at_deep_budget():
    """The never-worse contract: probes are noisy (measured on the real book — a
    shallow probe misranked overlap 70 above 80), so the sweep's answer must be at
    least as good as simply running the CURRENT setting at its guaranteed share
    (~half the budget), and a different overlap may win only by beating that
    strictly."""
    so, cfg, masters = _book(overlap=80)
    budget = 80
    sw = optimizer.sweep_optimize(so, cfg, masters, budget_evals=budget, seed=42)

    assert sw.evals <= budget
    cur_deep = optimizer.optimize(so, cfg, masters, budget_evals=budget // 2, seed=42)
    assert score(sw.result.best) <= score(cur_deep.best)
    if sw.overlap_percent != 80:
        assert score(sw.result.best) < score(cur_deep.best)   # strictly better only
    assert sw.result.ranks                                    # persistable artifact


def test_sweep_is_deterministic():
    so, cfg, masters = _book()
    a = optimizer.sweep_optimize(so, cfg, masters, budget_evals=60, seed=42)
    b = optimizer.sweep_optimize(so, cfg, masters, budget_evals=60, seed=42)
    assert (a.overlap_percent, a.result.best, a.result.ranks) == \
           (b.overlap_percent, b.result.best, b.result.ranks)


def test_sweep_keeps_current_setting_when_guard_rejects_all_others():
    """The candidate_setup veto (the API's promise guard) filters candidates; with
    everything but the current setting rejected, the sweep must return the current
    overlap and still produce a valid sequence result."""
    so, cfg, masters = _book(overlap=80)
    vetoed = []

    def guard(candidate_cfg):
        ok = candidate_cfg.overlap_percent == 80
        if not ok:
            vetoed.append(candidate_cfg.overlap_percent)
        return None, ok

    sw = optimizer.sweep_optimize(so, cfg, masters, budget_evals=60, seed=42,
                                  candidate_setup=guard)
    assert sw.overlap_percent == 80
    assert set(vetoed) == set(v for v in optimizer.OVERLAP_CANDIDATES if v != 80)
    assert [t for t in sw.table if not t["eligible"]]
    assert sw.result.ranks


def test_sweep_progress_is_monotonic_across_candidates():
    so, cfg, masters = _book()
    seen = []
    optimizer.sweep_optimize(so, cfg, masters, budget_evals=60, seed=42,
                             on_progress=lambda done, best: seen.append(done))
    assert seen == sorted(seen)
    assert seen[-1] <= 60


# --------------------------------------------------------------------------- #
# API wiring: result carries best_overlap; Apply persists overlap + fingerprint
# --------------------------------------------------------------------------- #
pytest.importorskip("fastapi")
from engine import book_store
from engine.models import Order
from tests.sample_workbook import ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
        Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
    ])


def test_optimize_result_carries_best_overlap_and_apply_persists_it():
    m = _api()
    _seed_book()
    st = m._start_optimize(budget_evals=60, label="quick", background=False)
    assert st["state"] == "done"
    assert "best_overlap" in st

    meta = m._optimize_apply()
    assert meta["best_overlap"] == st["best_overlap"]
    # The winning overlap is now THE saved plan setting (visible in Settings).
    saved = m._load_plan_config()
    assert saved.overlap_percent == st["best_overlap"]
    # Right after Apply the fingerprint matches -> no staleness warning.
    assert m._plan(saved)["optimize_meta"]["inputs_changed"] is False


def test_apply_with_current_setting_winning_changes_nothing():
    """If the current overlap wins, Apply must not churn the saved config."""
    m = _api()
    _seed_book()
    before = m._load_plan_config()
    st = m._start_optimize(budget_evals=60, label="quick", background=False)
    m._optimize_apply()
    after = m._load_plan_config()
    if st["best_overlap"] == before.overlap_percent:
        assert after.to_dict() == before.to_dict()
    else:
        assert after.overlap_percent == st["best_overlap"]
