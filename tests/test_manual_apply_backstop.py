"""Manual "Apply this plan" committed-promise backstop (owner decision,
committed-date-stability branch): `_optimize_apply` — the endpoint behind the
admin "Apply this plan" button — must enforce the SAME committed-promise
guarantee the daily auto path already enforces in `_auto_apply_result`. A
result that would push a committed order's promise slip past the incumbent's
must be rejected with 409, and must NOT persist ranks.

Mirrors the fixture pattern in tests/test_promise_backstop.py."""
from datetime import date

import pytest

pytest.importorskip("fastapi")
from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


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


def test_manual_apply_rejects_plan_that_regresses_committed_promise(monkeypatch):
    """The manual apply backstop: a completed contest result whose committed
    slip is HIGHER than the incumbent's must be rejected (409), and the
    previously-applied ranks must remain untouched."""
    m = _api()
    _seed_book()
    saved_ranks = {"k": 1}
    book_store.save_plan_priority(saved_ranks, {"saved_at": "t"})
    monkeypatch.setattr(m, "_incumbent_metrics",
                         lambda: {"total_late_days": 500, "makespan_days": 50.0,
                                  "max_late_days": 46, "max_committed_slip": 2})
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "done"
        m._OPTIMIZE["result"] = {"best": {"total_late_days": 100,
                                          "makespan_days": 50.0,
                                          "max_late_days": 46,
                                          "max_committed_slip": 4},
                                 "ranks": {"k": 2},
                                 "budget": 15, "seed": 42, "baseline": {},
                                 "best_overlap": None, "current_overlap": None}

    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        m._optimize_apply()
    assert e.value.status_code == 409
    assert "committed" in e.value.detail.lower()

    # Not applied: the previously-saved ranks are untouched, and the job is
    # still sitting there "done" (not silently cleared).
    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == saved_ranks
    assert m._OPTIMIZE["state"] == "done"


def test_manual_apply_applies_when_committed_promise_holds(monkeypatch):
    """Positive case: when best's max_committed_slip <= the incumbent's, the
    manual apply proceeds and persists the new ranks (unchanged behaviour)."""
    m = _api()
    _seed_book()
    saved_ranks = {"k": 1}
    book_store.save_plan_priority(saved_ranks, {"saved_at": "t"})
    monkeypatch.setattr(m, "_incumbent_metrics",
                         lambda: {"total_late_days": 500, "makespan_days": 50.0,
                                  "max_late_days": 46, "max_committed_slip": 4})
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "done"
        m._OPTIMIZE["result"] = {"best": {"total_late_days": 100,
                                          "makespan_days": 50.0,
                                          "max_late_days": 46,
                                          "max_committed_slip": 3},
                                 "ranks": {"k": 2},
                                 "budget": 15, "seed": 42, "baseline": {},
                                 "best_overlap": None, "current_overlap": None}

    meta = m._optimize_apply()
    assert meta["best"]["max_committed_slip"] == 3

    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == {"k": 2}


def test_manual_apply_all_open_book_still_applies(monkeypatch):
    """Nothing committed: both sides' max_committed_slip default to 0, so
    0 > 0 is False and the plan applies freely (owner's
    Apply-before-Commit flow is unaffected)."""
    m = _api()
    _seed_book()
    monkeypatch.setattr(m, "_incumbent_metrics",
                         lambda: {"total_late_days": 500, "makespan_days": 50.0,
                                  "max_late_days": 46})  # no max_committed_slip key
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "done"
        m._OPTIMIZE["result"] = {"best": {"total_late_days": 100,
                                          "makespan_days": 50.0,
                                          "max_late_days": 46},
                                 "ranks": {"k": 9},
                                 "budget": 15, "seed": 42, "baseline": {},
                                 "best_overlap": None, "current_overlap": None}

    meta = m._optimize_apply()
    assert meta is not None

    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == {"k": 9}
