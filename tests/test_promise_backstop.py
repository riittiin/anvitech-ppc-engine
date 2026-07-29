"""Committed-promise apply backstop (mirrors the worst-order backstop in
tests/test_auto_optimize.py): a contest result that scores strictly better
overall must still be REJECTED if it would push a committed order's promise
slip past the incumbent's — `_auto_apply_result`'s `promise_ok` gate."""
from datetime import date

import pytest

pytest.importorskip("fastapi")
from engine import book_store, optimizer
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


def test_auto_apply_keeps_current_when_committed_promise_would_regress(monkeypatch):
    """The committed-promise backstop (mirrors the worst-order one): a contest
    result with a strictly BETTER score is still REJECTED if it would push a
    committed order's promise slip later than the incumbent's max slip.
    Directly exercises the `elif not promise_ok` branch of
    _auto_apply_result."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    # A known applied plan — must remain untouched if the backstop holds.
    saved_ranks = {"k": 1}
    book_store.save_plan_priority(saved_ranks, {"saved_at": "t"})
    # Incumbent: a committed order sitting at 2 days of slip, high total
    # late-days (headroom for `best` to score strictly better while still
    # regressing the committed order).
    monkeypatch.setattr(m, "_incumbent_metrics",
                         lambda: {"total_late_days": 500, "makespan_days": 50.0,
                                  "max_late_days": 46, "max_committed_slip": 2})
    # Contest best: strictly better score (far fewer total late-days, same
    # worst order) but its committed slip is pushed to 4 days — the backstop
    # must reject it.
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["result"] = {"best": {"total_late_days": 100,
                                          "makespan_days": 50.0,
                                          "max_late_days": 46,
                                          "max_committed_slip": 4},
                                 "ranks": {"k": 2}}
        m._OPTIMIZE["auto"] = True
    # Sanity check on the premise this test isolates: score is strictly
    # better on its own — the backstop is the ONLY reason the plan is kept.
    assert (optimizer.score({"total_late_days": 100, "makespan_days": 50.0}) <
            optimizer.score({"total_late_days": 500, "makespan_days": 50.0}))

    m._auto_apply_result()

    note = book_store.load_auto_note()
    assert note is not None
    assert "protect a committed promise" in note["text"]
    assert "2" in note["text"]               # regression amount: 4 - 2

    # Not applied: the previously-saved ranks are untouched.
    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == saved_ranks


def test_auto_apply_applies_when_score_improves_and_promise_holds(monkeypatch):
    """Positive case: when `best` improves the score, doesn't regress the
    worst order, and doesn't raise the committed max slip, it DOES apply."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
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
        m._OPTIMIZE["auto"] = True

    m._auto_apply_result()

    note = book_store.load_auto_note()
    assert note is not None
    assert "auto-re-optimized" in note["text"]

    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == {"k": 2}
