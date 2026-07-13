"""Integration test for Task 7: the two-pass Plan (`api._plan`).

Committed/urgent orders are planned first (pass 1), as if open orders don't
exist; open orders backfill the remaining machine/operator time (pass 2, with
the committed pass's busy intervals reserved). This proves a later, larger
open order can never push a committed order's schedule later.
"""
from datetime import date


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _committed_end(result, so_no="SOc"):
    """Read the committed order's rows from the rule6 trace output and return
    the max End datetime string among them."""
    out = result["trace"]["rule6"]["output"]
    cols = out["columns"]
    so_idx = cols.index("SO No")
    end_idx = cols.index("End")
    ends = [row[end_idx] for row in out["rows"] if row[so_idx] == so_no]
    assert ends, f"no rule6 rows found for SO {so_no}"
    return max(ends)


def test_open_order_never_moves_a_committed_order(monkeypatch, tmp_path):
    m = _api()
    from engine import book_store
    from engine.models import Order
    from tests.sample_workbook import build_sample_bytes, ITEM_A
    from engine.config import Config

    # Upload the sample masters so routings exist.
    book_store.save_masters_bytes(build_sample_bytes())

    # One committed order; plan it and note its expected completion.
    book_store.add_orders([Order("SOc", ITEM_A, ITEM_A, 10, date(2025, 3, 20),
                                 commitment="committed", promised_date=date(2025, 3, 20))])
    r1 = m._plan(Config(plan_start_date=date(2025, 3, 5)))
    before = _committed_end(r1)

    # Add a big open order of the same item and re-plan.
    book_store.add_orders([Order("SOo", ITEM_A, ITEM_A, 5000, date(2025, 3, 25))])
    r2 = m._plan(Config(plan_start_date=date(2025, 3, 5)))
    after = _committed_end(r2)

    assert after == before, "the committed order's schedule must not move when an open order is added"
