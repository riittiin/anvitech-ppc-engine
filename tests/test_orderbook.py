"""Order-book pure logic: merge, status derivation, active lines, persistence."""
from datetime import date

from engine.models import SOLine, Order, Actual
from engine import orderbook, book_store


def _so(no, item, qty, d):
    return SOLine(so_no=no, item_code=item, item_name=item, qty=qty, delivery_date=d)


def _order(no, item, qty, d, completed=False):
    return Order(so_no=no, item_code=item, item_name=item, ordered_qty=qty,
                 delivery_date=d, completed=completed)


D = date(2025, 8, 1)


def test_merge_adds_unseen_and_flags_known():
    active = {"SO1": _order("SO1", "A", 10, D)}
    completed = {"SO9": _order("SO9", "Z", 5, D, completed=True)}
    lines = [_so("SO1", "A", 10, D), _so("SO2", "B", 20, D), _so("SO9", "Z", 5, D)]

    new, flags = orderbook.merge_upload(lines, active, completed)
    assert [o.so_no for o in new] == ["SO2"]                 # only the unseen one added
    reasons = {f["so_no"]: f["reason"] for f in flags}
    assert "duplicate" in reasons["SO1"]
    assert "already completed" in reasons["SO9"]


def test_merge_flags_changed_order_without_modifying():
    active = {"SO1": _order("SO1", "A", 10, D)}
    new, flags = orderbook.merge_upload([_so("SO1", "A", 99, D)], active, {})
    assert new == []
    assert "changed" in flags[0]["reason"]
    assert active["SO1"].ordered_qty == 10                   # original untouched


def test_status_derivation():
    o = _order("SO1", "A", 10, D)
    assert orderbook.derive_status(o, {}) == orderbook.PENDING
    assert orderbook.derive_status(o, {"SO1": 4}) == orderbook.RUNNING
    o.completed = True
    assert orderbook.derive_status(o, {"SO1": 4}) == orderbook.COMPLETE


def test_active_so_lines_use_remaining_and_skip_done():
    active = {
        "SO1": _order("SO1", "A", 10, D),      # produced 4 -> remaining 6
        "SO2": _order("SO2", "B", 5, D),       # produced 5 -> remaining 0 -> skipped
        "SO3": _order("SO3", "C", 8, D, completed=True),   # completed -> skipped
    }
    actuals = [
        Actual("SO1", "A", D, qty_produced=4),
        Actual("SO2", "B", D, qty_produced=5),
    ]
    lines = orderbook.active_so_lines(active, actuals)
    by = {l.so_no: l.qty for l in lines}
    assert by == {"SO1": 6}                                  # only SO1, at remaining 6


def test_persistence_round_trip_and_complete():
    book_store.add_orders([_order("SO1", "A", 10, D)])
    book_store.append_actual(Actual("SO1", "A", D, qty_produced=4))

    active = book_store.load_active_orders()
    assert active["SO1"].ordered_qty == 10
    assert len(book_store.load_actuals()) == 1

    assert book_store.complete_order("SO1") is True
    assert "SO1" not in book_store.load_active_orders()
    assert book_store.load_completed_orders()["SO1"].completed is True
