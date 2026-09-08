"""Add New Orders: storage, endpoints, role gating (2026-09-08 spec)."""
from engine import book_store


def test_drafts_round_trip():
    book_store.save_new_order_drafts([{"so_no": "NEW-1", "item_code": "X",
                                       "item_name": "x", "qty": 10}])
    assert book_store.load_new_order_drafts() == [
        {"so_no": "NEW-1", "item_code": "X", "item_name": "x", "qty": 10}]


def test_drafts_default_to_empty():
    book_store.save_new_order_drafts([])
    assert book_store.load_new_order_drafts() == []


def test_queue_round_trips_and_clears():
    book_store.save_new_order_queue([("NEW-1", "X"), ("NEW-2", "Y")])
    assert book_store.load_new_order_queue() == [["NEW-1", "X"], ["NEW-2", "Y"]]
    book_store.clear_new_order_queue()
    assert book_store.load_new_order_queue() == []
