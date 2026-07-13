"""book_store persistence of the auto promise-recovery (ranks + metadata)."""
from engine import book_store
from engine.pipeline import KEY_SEP

RANKS = {f"SO1{KEY_SEP}X": 1, f"SO2{KEY_SEP}Y..": 2}   # dotted item = MongoDB-hostile, must survive
META = {"saved_at": "2026-07-14T10:00:00", "covered_keys": [f"SO1{KEY_SEP}X", f"SO2{KEY_SEP}Y.."],
        "slip_before": 344, "slip_after": 242, "promises_saved": 11}


def test_round_trip():
    book_store.save_promise_recovery(RANKS, META)
    data = book_store.load_promise_recovery()
    assert data["ranks"] == RANKS and data["meta"] == META


def test_absent_returns_none():
    assert book_store.load_promise_recovery() is None


def test_clear_removes_it():
    book_store.save_promise_recovery(RANKS, META)
    book_store.clear_promise_recovery()
    assert book_store.load_promise_recovery() is None


def test_corrupt_or_empty_treated_as_absent():
    from engine.storage import get_store
    get_store().kv_set(book_store.PROMISE_RECOVERY_KEY, "not json{")
    assert book_store.load_promise_recovery() is None
    get_store().kv_set(book_store.PROMISE_RECOVERY_KEY, '{"ranks": {}}')
    assert book_store.load_promise_recovery() is None


def test_independent_from_plan_priority():
    # The two rank stores must not collide.
    book_store.save_plan_priority({f"A{KEY_SEP}B": 1}, {"saved_at": "t"})
    book_store.save_promise_recovery(RANKS, META)
    assert book_store.load_plan_priority()["ranks"] == {f"A{KEY_SEP}B": 1}
    assert book_store.load_promise_recovery()["ranks"] == RANKS
