"""Task 9: `_plan` replays JOINT ranks over all active lines, re-validates the
promise ceiling on the produced schedule, and falls back to the two-pass plan
when the applied sequence would break a now-tighter promise (drift). Plus:
the Optimize before/after is scored on the SAME domain (all active lines) on
both sides (`_incumbent_metrics` and the baseline), not open-lines vs all-lines.
"""
from datetime import date, timedelta

import pytest

pytest.importorskip("fastapi")

from engine import book_store
from engine.models import Order
from tests.sample_workbook import ITEM_A, ITEM_B, build_sample_bytes

KEY_A = f"SO1\x1f{ITEM_A}"


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


def _apply_joint_contest(m):
    """Commit SO1 at its own current expected end (comfortable), run a tiny
    inline JOINT contest, and apply it. Returns the committed promise date."""
    first = m._plan(m._load_plan_config())
    exp = first["expected_end"][KEY_A]
    promise = date.fromisoformat(exp)
    book_store.set_commitment("SO1", ITEM_A, "committed", promise, "t")

    m._start_optimize(budget_evals=12, label="deep", background=False)
    m._optimize_apply()
    saved = book_store.load_plan_priority()
    assert saved and saved["meta"].get("joint") is True, "contest must save JOINT ranks"
    return promise


def test_joint_ranks_replay_is_honored_and_keeps_the_promise():
    m = _api()
    _seed_book()
    promise = _apply_joint_contest(m)

    result = m._plan(m._load_plan_config())
    assert "joint_fallback" not in result, "a keepable joint replay must NOT fall back"
    end = date.fromisoformat(result["expected_end"][KEY_A])
    assert end <= promise, f"joint replay broke the promise: end {end} > promise {promise}"
    assert result["gantt"]["rows"], "the plan must still produce a schedule"


def test_promise_drift_falls_back_to_two_pass():
    m = _api()
    _seed_book()
    promise = _apply_joint_contest(m)

    # Tighten SO1's promise well before any feasible finish → the applied joint
    # sequence now breaks it, so the replay must be discarded for the two-pass.
    tighter = promise - timedelta(days=40)
    book_store.set_commitment("SO1", ITEM_A, "committed", tighter, "t")

    result = m._plan(m._load_plan_config())
    assert result.get("joint_fallback") is True, "drift must fall back to two-pass"
    assert result["gantt"]["rows"], "the fallback plan must still produce a schedule"


def test_drift_fallback_is_byte_identical_to_no_optimization():
    """On the drift path the joint ranks must be IGNORED ENTIRELY: the fallback
    two-pass plan is byte-identical to the plan with no saved optimization at
    all (same rule6 table, same expected ends). Joint ranks were computed for a
    single-pass all-lines pool — they must not drive an isolated open pass.

    The book needs TWO open orders so a leaked rank map could actually reorder
    the open pass; the saved ranks deliberately invert both natural orders."""
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        # Impossible promise (before the plan start) → the joint replay can
        # never satisfy the ceiling → the fallback path always runs.
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20),
              commitment="committed", promised_date=date(2025, 1, 1)),
        Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
        Order("SO3", ITEM_A, ITEM_A, 8, date(2025, 3, 25)),
    ])
    # Joint-tagged ranks that invert the open orders' natural (Rule-3) order —
    # if they leak into the open pass, its schedule visibly changes.
    ranks = {f"SO3\x1f{ITEM_A}": 1, f"SO2\x1f{ITEM_B}": 2, f"SO1\x1f{ITEM_A}": 3}
    book_store.save_plan_priority(ranks, {"saved_at": "t", "joint": True})

    fallback = m._plan(m._load_plan_config())
    assert fallback.get("joint_fallback") is True

    book_store.clear_plan_priority()          # the no-optimization reference
    clean = m._plan(m._load_plan_config())
    assert "joint_fallback" not in clean

    assert fallback["trace"]["rule6"]["output"] == clean["trace"]["rule6"]["output"], (
        "the drift fallback's rule6 schedule must match a clean no-optimization two-pass")
    assert fallback["expected_end"] == clean["expected_end"], (
        "the drift fallback's expected ends must match a clean no-optimization two-pass")


def test_legacy_open_only_ranks_are_untouched():
    """A saved plan_priority with ranks but NO joint flag (deployed today) keeps
    the current behavior: it never triggers the joint branch / fallback."""
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
        Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
    ])
    # All-open book, legacy meta (no "joint").
    book_store.save_plan_priority({KEY_A: 1}, {"saved_at": "t"})

    result = m._plan(m._load_plan_config())
    assert "joint_fallback" not in result
    assert result["gantt"]["rows"], "the legacy plan must still run"


def test_incumbent_metrics_cover_all_active_lines():
    """Same-domain fix: on a committed book the incumbent score is measured over
    ALL active orders (committed + open), not the open lane only."""
    m = _api()
    _seed_book()
    book_store.set_commitment("SO1", ITEM_A, "committed", date(2025, 3, 20), "t")

    metrics = m._incumbent_metrics()
    assert metrics["orders"] == 2, (
        f"expected metrics over both active lines, got orders={metrics['orders']}")
