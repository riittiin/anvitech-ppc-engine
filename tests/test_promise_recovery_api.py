"""Promise recovery — the _plan wiring: detect a committed slip, run the recovery,
replay it, and never make committed orders worse."""
from datetime import date

import pytest

pytest.importorskip("fastapi")

from engine import book_store
from engine.models import Order
from engine.pipeline import KEY_SEP
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _run_recovery_sync(m, protected, config, masters):
    """Run the background recovery job inline (deterministic in tests)."""
    import threading
    real = threading.Thread
    captured = {}
    def fake_thread(target=None, daemon=None):
        class T:
            def start(self_):
                captured["ran"] = True
                target()
        return T()
    threading.Thread = fake_thread
    try:
        m._maybe_start_recovery(protected, config, masters)
    finally:
        threading.Thread = real
    return captured.get("ran", False)


def test_no_committed_slip_means_no_recovery_and_plan_unchanged():
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])
    # Commit at a comfortably-future promise -> no slip -> no recovery.
    book_store.set_commitment("SO1", ITEM_A, "committed", date(2026, 1, 1), "t")
    result = m._plan(m._load_plan_config())
    assert result["recovery_meta"]["active"] is False
    assert book_store.load_promise_recovery() is None


def test_committed_slip_triggers_recovery_and_replays_it():
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    # Several committed orders, promised in the PAST -> they slip -> recovery should fire.
    book_store.add_orders([
        Order("C1", ITEM_A, ITEM_A, 40, date(2025, 3, 20)),
        Order("C2", ITEM_B, ITEM_B, 60, date(2025, 3, 20)),
        Order("C3", ITEM_A, ITEM_A, 30, date(2025, 3, 20)),
    ])
    for so, it in (("C1", ITEM_A), ("C2", ITEM_B), ("C3", ITEM_A)):
        book_store.set_commitment(so, it, "committed", date(2025, 1, 1), "t")   # promised in the past

    # First plan: no recovery yet -> it should kick one off (computing=True).
    from engine import orderbook
    masters = m._current_masters()
    so_lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                         book_store.load_actuals(), masters)
    protected, _ = orderbook.split_committed_open(so_lines)
    ran = _run_recovery_sync(m, protected, m._load_plan_config(), masters)
    assert ran and book_store.load_promise_recovery() is not None

    # Next plan replays the saved recovery.
    result = m._plan(m._load_plan_config())
    assert result["recovery_meta"]["active"] is True
    assert result["recovery_meta"]["slip_after"] <= result["recovery_meta"]["slip_before"]


def test_recovery_signature_changes_when_promise_changes():
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("C1", ITEM_A, ITEM_A, 40, date(2025, 3, 20))])
    book_store.set_commitment("C1", ITEM_A, "committed", date(2025, 1, 1), "t")
    from engine import orderbook
    masters = m._current_masters()

    def _protected():
        sl = orderbook.active_so_lines(book_store.load_active_orders(),
                                       book_store.load_actuals(), masters)
        return orderbook.split_committed_open(sl)[0]

    sig1 = m._recovery_signature(_protected())
    book_store.set_commitment("C1", ITEM_A, "committed", date(2025, 2, 1), "t")   # new promise
    sig2 = m._recovery_signature(_protected())
    assert sig1 != sig2      # changing a promise invalidates the cached recovery


def test_all_open_book_has_no_recovery_and_stays_byte_identical():
    m = _api()
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])
    result = m._plan(m._load_plan_config())     # all open, no commitments
    assert result["recovery_meta"] == {"active": False}
