"""The interaction the owner asked about: Optimize with committed + urgent + open
orders all present, AND still honouring the daily-actuals feedback loop.

Two guarantees proved end-to-end (through the real _plan path):
  1. LANES: an applied optimization only reorders the OPEN pass. Committed and urgent
     orders are planned first (Pass 1, by promise) and their scheduled dates are
     identical whether or not an optimization is applied — a found plan can never move
     a promise.
  2. FEEDBACK: after Optimize is applied, punching a Rule-7 actual on an open order
     still reduces that order's remaining qty on the next Plan (the saved artifact is
     an ORDERING, not a frozen schedule — it replays against current reality), and the
     committed/urgent dates are STILL protected across that feedback re-plan.
"""
from datetime import date

import pytest

pytest.importorskip("fastapi")

from engine import book_store
from engine.models import Order, Actual
from engine.pipeline import KEY_SEP
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_three_lanes():
    """One committed, one urgent, two open orders (all real, routable items)."""
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("C1", ITEM_A, "SAMPLE RING A", 20, date(2025, 3, 20)),   # -> committed
        Order("U1", ITEM_B, "SAMPLE PIN B", 30, date(2025, 3, 18)),    # -> urgent
        Order("O1", ITEM_A, "SAMPLE RING A", 40, date(2025, 3, 25)),   # open
        Order("O2", ITEM_B, "SAMPLE PIN B", 50, date(2025, 3, 26)),    # open
    ])
    # Lock the two protected lanes.
    book_store.set_commitment("C1", ITEM_A, "committed", date(2025, 3, 22), "t0")
    book_store.set_commitment("U1", ITEM_B, "urgent", date(2025, 3, 18), "t0")


def _expected_by_order(result):
    """{(so,item)->iso date} of each order's expected completion from a _plan result."""
    return dict(
        (tuple(k.split(KEY_SEP)), v) for k, v in result["expected_end"].items()
    )


def test_optimize_reorders_only_open_and_never_moves_a_promise():
    m = _api()
    _seed_three_lanes()

    before = _expected_by_order(m._plan(m._load_plan_config()))

    # Apply an optimization whose rank map deliberately names EVERY order (incl. the
    # protected ones) — the protected pass must ignore ranks entirely.
    m._start_optimize(budget_evals=40, label="quick", background=False)
    m._optimize_apply()
    assert book_store.load_plan_priority() is not None

    after = _expected_by_order(m._plan(m._load_plan_config()))

    # Committed + urgent expected dates are byte-identical before/after optimize.
    for k in (("C1", ITEM_A), ("U1", ITEM_B)):
        assert after[k] == before[k], f"promise for {k} moved: {before[k]} -> {after[k]}"

    # The optimization IS active (open pass replays ranks).
    meta = m._plan(m._load_plan_config())["optimize_meta"]
    assert meta["active"] is True


def _remaining(m, so, item):
    out = m._plan(m._load_plan_config())["trace"]["rule8"]["output"]
    cols = out["columns"]
    si, ii, ri = cols.index("SO No"), cols.index("Item Code"), cols.index("Remaining Qty")
    for r in out["rows"]:
        if r[si] == so and r[ii] == item:
            return r[ri]
    return None


def test_feedback_reduces_open_remaining_even_with_optimize_applied():
    # The saved artifact is an ORDERING (ranks), not a frozen schedule — so the daily
    # feedback loop still drives quantities. After Optimize is applied, punching an
    # actual on an open order still reduces that order's remaining on the next Plan.
    m = _api()
    _seed_three_lanes()
    m._start_optimize(budget_evals=40, label="quick", background=False)
    m._optimize_apply()

    assert _remaining(m, "O1", ITEM_A) == 40
    book_store.append_actual(Actual(
        so_no="O1", item_code=ITEM_A, entry_date=date(2025, 3, 6), shift="First shift",
        item_name="SAMPLE RING A", process="INSP", qty_produced=15, qty_rejected=0))
    assert _remaining(m, "O1", ITEM_A) == 25       # 40 − 15 good at the gate


def test_optimize_never_disturbs_committed_or_urgent_across_a_feedback_replan():
    # The promise-protection invariant, isolated from the plan-clock advance (which
    # legitimately moves the WHOLE book forward when any day is punched). At the SAME
    # book state — after an open order's actual — the committed/urgent expected dates
    # are identical whether or not an optimization is applied, and both still meet
    # their promised date. So Optimize can never push a promise, feedback or not.
    m = _api()
    _seed_three_lanes()
    book_store.append_actual(Actual(
        so_no="O1", item_code=ITEM_A, entry_date=date(2025, 3, 6), shift="First shift",
        item_name="SAMPLE RING A", process="INSP", qty_produced=15, qty_rejected=0))

    no_opt = _expected_by_order(m._plan(m._load_plan_config()))
    m._start_optimize(budget_evals=40, label="quick", background=False)
    m._optimize_apply()
    with_opt = _expected_by_order(m._plan(m._load_plan_config()))

    promised = {("C1", ITEM_A): date(2025, 3, 22), ("U1", ITEM_B): date(2025, 3, 18)}
    for k in promised:
        assert with_opt[k] == no_opt[k], \
            f"optimize disturbed promised order {k}: {no_opt[k]} -> {with_opt[k]}"
        assert date.fromisoformat(with_opt[k]) <= promised[k], \
            f"promise for {k} missed: expected {with_opt[k]} > promised {promised[k]}"
