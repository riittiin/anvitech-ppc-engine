"""Operator-assignment (operator_pick) as a 4th Optimize dimension (2026-08-02)."""
import json

import pytest

from engine.config import Config


def test_operator_pick_defaults_to_scarce():
    assert Config().operator_pick == "scarce"


def test_operator_pick_round_trips():
    c = Config(operator_pick="balanced")
    assert Config.from_dict(c.to_dict()).operator_pick == "balanced"
    # to_dict must carry it (no special-casing needed — it's a plain str field).
    assert c.to_dict()["operator_pick"] == "balanced"


def test_operator_pick_blank_coerces_to_scarce():
    assert Config.from_dict({"operator_pick": ""}).operator_pick == "scarce"
    assert Config.from_dict({"operator_pick": None}).operator_pick == "scarce"


def test_operator_pick_invalid_is_rejected():
    with pytest.raises(ValueError):
        Config(operator_pick="nope").validate()


def test_operator_pick_candidates_are_scarce_and_balanced():
    from engine.optimizer import OPERATOR_PICK_CANDIDATES
    assert OPERATOR_PICK_CANDIDATES == ("scarce", "balanced")


def test_operator_pick_contenders_put_current_first():
    from engine.optimizer import operator_pick_contenders
    assert operator_pick_contenders("balanced")[0] == "balanced"
    assert operator_pick_contenders("scarce") == ["scarce", "balanced"]
    # An off-list current policy still joins its own contest, first.
    assert operator_pick_contenders("flexible")[0] == "flexible"
    assert set(operator_pick_contenders("flexible")) == {"flexible", "scarce", "balanced"}


def test_sweepresult_defaults_operator_pick_scarce():
    from engine.optimizer import SweepResult
    assert SweepResult().operator_pick == "scarce"


def test_plan_config_carries_operator_pick():
    from engine.new_engine import _plan_config
    assert _plan_config(Config()).operator_pick == "scarce"
    assert _plan_config(Config(operator_pick="balanced")).operator_pick == "balanced"
    assert _plan_config(Config(operator_pick="flexible")).operator_pick == "flexible"


def test_sweep_optimize_sweeps_all_operator_picks(monkeypatch):
    """The local fallback tries every (machine-set × operator-pick) pass and keeps
    the best. tune() is stubbed so the test is fast and deterministic."""
    from engine import new_engine

    calls = []

    def fake_tune(so_lines, config, masters, **kw):
        calls.append((config.flexible_machines, config.operator_pick))
        # Make "balanced" the strict winner so we can assert the returned policy.
        late = 10 if config.operator_pick == "scarce" else 5
        return ({("b", "i"): 0}, 80, {"total_late_days": late, "makespan_days": 0}, 5)

    monkeypatch.setattr(new_engine, "tune", fake_tune)
    res = new_engine.sweep_optimize(["x"], Config(scheduler="new"), object(),
                                    budget_evals=40)
    assert set(calls) == {(False, "scarce"), (True, "scarce"),
                          (False, "balanced"), (True, "balanced")}
    assert res.operator_pick == "balanced"


def _payload(scheduler="new", overlap=50):
    from engine.config import Config
    cfg = Config(scheduler=scheduler, overlap_percent=overlap)
    return {"config": cfg.to_dict(), "candidates": (70, 80)}


def test_contest_jobs_sweeps_operator_pick_for_new_engine():
    from engine import optimize_service
    jobs = optimize_service.contest_jobs(_payload("new"))
    assert all(len(t) == 3 for t in jobs)
    picks = {pick for (_ov, _flex, pick) in jobs}
    assert picks == {"scarce", "balanced"}
    # sequence contenders (current 50 + 70 + 80) × machine-sets(2) × picks(2)
    assert len(jobs) == 3 * 2 * 2


def test_contest_jobs_classic_stays_scarce_single_pass():
    from engine import optimize_service
    jobs = optimize_service.contest_jobs(_payload("classic"))
    assert {pick for (_ov, _flex, pick) in jobs} == {"scarce"}
    assert all(flex is False for (_ov, flex, _pick) in jobs)


def test_pick_winner_tie_prefers_current_pick():
    from engine import optimize_service
    m = {"total_late_days": 5, "makespan_days": 0}
    rows = [
        {"overlap": 80, "flexible": False, "pick": "balanced", "eligible": True, "best": m},
        {"overlap": 80, "flexible": False, "pick": "scarce", "eligible": True, "best": m},
    ]
    win = optimize_service.pick_winner(80, False, "scarce", rows)
    assert win["pick"] == "scarce"


def test_merge_shard_rows_carries_winner_pick():
    from engine import optimize_service
    rows = [{"overlap": 80, "flexible": True, "pick": "balanced", "eligible": True,
             "best": {"total_late_days": 1, "makespan_days": 0}, "evals": 5, "ranks": {}}]
    out = optimize_service.merge_shard_rows(_payload("new"), rows, 5, False)
    assert out["winner_pick"] == "balanced"
    assert "pick" in out["rows"][0]


def test_local_contest_multiplier():
    from engine import optimize_service
    from engine.config import Config
    assert optimize_service.local_contest_multiplier(Config(scheduler="new")) == 4
    assert optimize_service.local_contest_multiplier(Config(scheduler="classic")) == 1


def test_inputs_signature_reflects_operator_pick():
    from api import main as m
    from engine.config import Config
    base = m._inputs_signature(Config())
    assert m._inputs_signature(Config(operator_pick="balanced")) != base


def test_apply_persists_the_winning_operator_pick(monkeypatch):
    from api import main as m
    from engine.config import Config
    saved = {}
    monkeypatch.setattr(m, "_load_plan_config", lambda: Config(scheduler="new"))
    monkeypatch.setattr(m, "_incumbent_metrics",
                        lambda: {"max_late_days": 100, "max_committed_slip": 0,
                                 "total_late_days": 100})
    monkeypatch.setattr(m, "_current_book_sig", lambda: "bs")
    monkeypatch.setattr(m.book_store, "save_plan_priority", lambda *a, **k: None)
    monkeypatch.setattr(m.book_store, "save_plan_config",
                        lambda s: saved.update(cfg=json.loads(s)))
    # The schedule-snapshot block is wrapped in try/except; force it to bail early.
    monkeypatch.setattr(m.book_store, "load_active_orders",
                        lambda: (_ for _ in ()).throw(RuntimeError("skip snapshot")))
    m._OPTIMIZE.update(state="done", started_mono=0.0, result={
        "ranks": {"b\x1fi": 0}, "best": {"total_late_days": 10, "max_committed_slip": 0},
        "baseline": {}, "budget": "deep", "seed": 1, "inputs_sig": "x",
        "best_overlap": 85, "current_overlap": 50, "knob": "overlap_percent",
        "flexible_machines": True, "operator_pick": "balanced"})
    m._optimize_apply()
    assert saved["cfg"]["operator_pick"] == "balanced"
    assert saved["cfg"]["flexible_machines"] is True


def test_run_contest_result_exposes_winner_pick(monkeypatch):
    """The single-worker path posts run_contest(...)['winner_pick']; guarantee the key
    exists so scripts/cloud_optimize_worker.py can forward it."""
    from engine import optimize_service
    monkeypatch.setattr(optimize_service, "contest_jobs", lambda p: [])
    out = optimize_service.run_contest(_payload("new"))
    assert "winner_pick" in out
