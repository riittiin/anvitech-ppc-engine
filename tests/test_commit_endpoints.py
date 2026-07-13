"""Task 8: commit / urgent (with push-warning preview) / uncommit — the owner
actions layered on top of the Task 7 two-pass plan.

Drives the internal helpers directly (`_commit_orders`, `_preview_urgent_pushes`)
plus the thin endpoint functions; HTTP auth/CSRF wiring is covered elsewhere
(`require_admin` is exercised by the existing admin-endpoint tests).
"""
from datetime import date

import pytest


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_masters():
    from engine import book_store
    from tests.sample_workbook import build_sample_bytes
    book_store.save_masters_bytes(build_sample_bytes())


# --------------------------------------------------------------------------- #
# _commit_orders
# --------------------------------------------------------------------------- #
def test_commit_snapshots_current_expected_as_promise():
    m = _api()
    from engine import book_store
    from engine.models import Order
    from tests.sample_workbook import ITEM_A

    _seed_masters()
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])

    m._commit_orders([("SO1", ITEM_A)])

    o = book_store.load_active_orders()[("SO1", ITEM_A)]
    assert o.commitment == "committed"
    assert o.promised_date is not None
    assert isinstance(o.promised_date, date)
    assert o.committed_at  # snapshot timestamp recorded


def test_commit_orders_skips_unknown_pair_without_raising():
    m = _api()
    _seed_masters()
    # No orders in the book at all — should just no-op silently.
    m._commit_orders([("NOPE", "NOPE")])


# --------------------------------------------------------------------------- #
# _preview_urgent_pushes
# --------------------------------------------------------------------------- #
def test_preview_returns_empty_when_no_other_protected_orders():
    m = _api()
    from engine import book_store
    from engine.models import Order
    from tests.sample_workbook import ITEM_A

    _seed_masters()
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])

    pushed = m._preview_urgent_pushes("SO1", ITEM_A)
    assert pushed == []


def test_preview_returns_empty_for_unknown_order():
    m = _api()
    _seed_masters()
    assert m._preview_urgent_pushes("GHOST", "GHOST") == []


def test_preview_flags_push_on_another_committed_order():
    """Two committed orders share the only real machines for their item (CNC 1/CNC
    2 from the sample workbook), both with large enough qty to contend for
    machine time. SOc1 is committed with an early promise close to what pass-1
    currently gives it, so it plans first today.

    SOc2 is committed too, currently promised for later (its promise was set
    loosely) — but its underlying SO **delivery date is earlier than SOc1's
    promise**. Making SOc2 urgent snaps its schedule-priority date to that early
    delivery date (per `_preview_urgent_pushes`: `promised_date = delivery_date`),
    which reorders Rule 3's protected-pass sort (earliest promise first) so SOc2
    now plans ahead of SOc1 — and with both wanting the same scarce machines,
    that pushes SOc1's completion past what was promised to it."""
    m = _api()
    from engine import book_store
    from engine.models import Order
    from engine.config import Config
    from tests.sample_workbook import ITEM_A

    _seed_masters()

    # SOc1: committed, LARGE qty (big enough to genuinely contend for CNC1/CNC2
    # across multiple days — a small qty finishes in a day or two and never
    # collides with a second order), due far enough out that pass-1 plans it
    # comfortably today.
    book_store.add_orders([
        Order("SOc1", ITEM_A, ITEM_A, 4000, date(2025, 3, 21),
              commitment="committed", promised_date=date(2025, 3, 21)),
    ])
    cfg = Config(plan_start_date=date(2025, 3, 5))
    m._plan(cfg)

    # Snapshot SOc1's real expected completion under pass-1-alone as its promise,
    # using the helper under test (also exercises _commit_orders end-to-end).
    m._commit_orders([("SOc1", ITEM_A)])
    baseline_promise = book_store.load_active_orders()[("SOc1", ITEM_A)].promised_date
    assert baseline_promise is not None

    # SOc2: committed too, with a promise LATER than SOc1's — but its actual SO
    # delivery date is EARLIER than SOc1's promise. Today it plans behind SOc1
    # (promise governs pass-1 order); making it urgent re-anchors it to that
    # earlier delivery date, jumping it ahead of SOc1 for the same machines.
    book_store.add_orders([
        Order("SOc2", ITEM_A, ITEM_A, 4000, date(2025, 3, 10),
              commitment="committed", promised_date=date(2025, 4, 28)),
    ])

    pushed = m._preview_urgent_pushes("SOc2", ITEM_A)

    assert any(p["so"] == "SOc1" and p["item"] == ITEM_A for p in pushed), (
        f"expected SOc1's promise to be pushed by making SOc2 urgent; got {pushed}")
    entry = next(p for p in pushed if p["so"] == "SOc1")
    assert entry["promised"] == baseline_promise.isoformat()
    assert entry["new"] > entry["promised"]


# --------------------------------------------------------------------------- #
# uncommit
# --------------------------------------------------------------------------- #
def test_uncommit_resets_to_open():
    m = _api()
    from engine import book_store
    from engine.models import Order
    from tests.sample_workbook import ITEM_A

    _seed_masters()
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20),
              commitment="committed", promised_date=date(2025, 3, 20), committed_at="x"),
    ])

    ok = book_store.clear_commitment("SO1", ITEM_A)
    assert ok is True

    o = book_store.load_active_orders()[("SO1", ITEM_A)]
    assert o.commitment == "open"
    assert o.promised_date is None
    assert o.committed_at is None


# --------------------------------------------------------------------------- #
# endpoint functions (thin — driven directly, not via HTTP/TestClient)
# --------------------------------------------------------------------------- #
class _FakeRequest:
    """Stand-in for FastAPI Request with the admin role already set, matching
    how require_admin reads request.state.role."""
    class _State:
        role = "admin"
    state = _State()


def test_commit_endpoint_function_commits_and_counts():
    m = _api()
    from engine import book_store
    from engine.models import Order
    from tests.sample_workbook import ITEM_A

    _seed_masters()
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])

    req = m.CommitRequest(orders=[["SO1", ITEM_A]])
    result = m.commit_orders_ep(req, _FakeRequest())
    assert result == {"committed": 1}
    assert book_store.load_active_orders()[("SO1", ITEM_A)].commitment == "committed"


def test_uncommit_endpoint_function_clears():
    m = _api()
    from engine import book_store
    from engine.models import Order
    from tests.sample_workbook import ITEM_A

    _seed_masters()
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20),
              commitment="committed", promised_date=date(2025, 3, 20), committed_at="x"),
    ])

    req = m.CommitRequest(orders=[["SO1", ITEM_A]])
    result = m.uncommit_orders_ep(req, _FakeRequest())
    assert result == {"uncommitted": 1}
    assert book_store.load_active_orders()[("SO1", ITEM_A)].commitment == "open"


def test_urgent_endpoint_returns_warning_without_confirm_then_applies_with_confirm():
    m = _api()
    from engine import book_store
    from engine.models import Order
    from engine.config import Config
    from tests.sample_workbook import ITEM_A

    _seed_masters()
    book_store.add_orders([
        Order("SOc1", ITEM_A, ITEM_A, 4000, date(2025, 3, 21),
              commitment="committed", promised_date=date(2025, 3, 21)),
    ])
    m._plan(Config(plan_start_date=date(2025, 3, 5)))
    m._commit_orders([("SOc1", ITEM_A)])

    book_store.add_orders([
        Order("SOc2", ITEM_A, ITEM_A, 4000, date(2025, 3, 10),
              commitment="committed", promised_date=date(2025, 4, 28)),
    ])

    req = m.UrgentRequest(so="SOc2", item=ITEM_A, confirm=False)
    result = m.urgent_order_ep(req, _FakeRequest())
    assert "warning" in result
    assert result["warning"]
    # Not applied yet.
    assert book_store.load_active_orders()[("SOc2", ITEM_A)].commitment == "committed"

    req2 = m.UrgentRequest(so="SOc2", item=ITEM_A, confirm=True)
    result2 = m.urgent_order_ep(req2, _FakeRequest())
    assert result2 == {"urgent": True}
    o = book_store.load_active_orders()[("SOc2", ITEM_A)]
    assert o.commitment == "urgent"
    assert o.promised_date is not None
