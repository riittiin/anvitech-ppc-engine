"""Regression test: the two-pass Plan (`api._plan`) must not let a committed
order and an open order collide on batch_id.

Rule 1 numbers batches ``B001, B002, ...`` fresh on every ``run()``. The
two-pass plan calls ``run_forward`` twice (pass 1 = protected/committed+urgent
orders, pass 2 = open orders), so *both* passes start again from ``B001``.
``engine/gantt.py::build_gantt`` keys everything on ``batch_id``
(``bmap = {b.batch_id: b for b in batches}`` and ``by_batch[e.batch_id]``), so
colliding ids from the two passes overwrite/merge a committed batch's Gantt
row with an open one — the committed order silently disappears from the
Gantt.

The fix namespaces every open-pass batch_id with an ``"O-"`` prefix after
``run_forward`` returns for pass 2, so ids stay globally unique across both
passes.
"""
from datetime import date

from engine import book_store
from engine.config import Config
from engine.models import Order
from tests.sample_workbook import ITEM_A, ITEM_B, build_sample_bytes


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_two_orders():
    """One committed order (ITEM_A) + one open order (ITEM_B), different SOs."""
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SOc", ITEM_A, ITEM_A, 10, date(2025, 3, 20),
              commitment="committed", promised_date=date(2025, 3, 20)),
    ])
    book_store.add_orders([
        Order("SOo", ITEM_B, ITEM_B, 20, date(2025, 3, 25)),
    ])


def test_gantt_has_a_row_for_both_the_committed_and_open_order():
    """Structural check: build_gantt groups the schedule by batch_id into one
    row per batch (see build_gantt's `by_batch`/`bmap`). With colliding ids
    the two orders' entries collapse into a single row. Both must appear."""
    m = _api()
    _seed_two_orders()

    result = m._plan(Config(plan_start_date=date(2025, 3, 5)))
    rows = result["gantt"]["rows"]

    assert len(rows) == 2, (
        f"expected 2 Gantt rows (one per order), got {len(rows)}: "
        f"{[r['batch_id'] for r in rows]}"
    )
    item_codes = {r["item_code"] for r in rows}
    assert item_codes == {ITEM_A, ITEM_B}, (
        f"both orders' item codes must appear in the Gantt, got {item_codes}"
    )
    so_nos = {r["so_no"] for r in rows}
    assert so_nos == {"SOc", "SOo"}, (
        f"both orders' SO numbers must appear in the Gantt, got {so_nos}"
    )


def test_merged_schedule_has_no_batch_id_collisions():
    """Structure-independent invariant: whatever downstream views do with
    batch_id, the merged rule6 schedule itself must never have the same
    batch_id used by both the committed and the open pass."""
    m = _api()
    _seed_two_orders()

    result = m._plan(Config(plan_start_date=date(2025, 3, 5)))
    out = result["trace"]["rule6"]["output"]
    cols = out["columns"]
    bidx = cols.index("Batch ID")
    so_idx = cols.index("SO No")

    # Map each batch_id to the set of SO numbers that used it.
    so_by_batch = {}
    for row in out["rows"]:
        so_by_batch.setdefault(row[bidx], set()).add(row[so_idx])

    for bid, so_set in so_by_batch.items():
        assert len(so_set) == 1, (
            f"batch_id {bid!r} is shared by multiple SOs {so_set} — "
            "a collision between the committed and open pass merged their rows"
        )

    # Both orders must still be represented somewhere in the merged schedule.
    all_so = {so for so_set in so_by_batch.values() for so in so_set}
    assert all_so == {"SOc", "SOo"}, (
        f"both orders must appear in the merged rule6 schedule, got {all_so}"
    )
