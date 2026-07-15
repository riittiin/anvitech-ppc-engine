"""commit / urgent / uncommit — the owner status actions.

Post-pivot (2026-07-16): lanes are pure status labels with no scheduling effect.
Commit snapshots the current expected completion as an INFORMATIONAL promise;
urgent flags the order and snapshots its SO delivery date; neither constrains the
plan (the byte-identical regression lives in test_replay_single_pass). There is
no push-warning preview anymore — urgent just sets and bumps.

Drives the internal helpers directly (`_commit_orders`) plus the thin endpoint
functions; HTTP auth/CSRF wiring is covered elsewhere.
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


def test_urgent_endpoint_sets_status_and_promises_delivery_date():
    """No preview / confirm anymore: marking urgent immediately sets the lane to
    'urgent' and snapshots the SO delivery date as the informational promise."""
    m = _api()
    from engine import book_store
    from engine.models import Order
    from tests.sample_workbook import ITEM_A

    _seed_masters()
    book_store.add_orders([Order("SOc2", ITEM_A, ITEM_A, 4000, date(2025, 3, 10))])

    req = m.UrgentRequest(so="SOc2", item=ITEM_A)
    result = m.urgent_order_ep(req, _FakeRequest())
    assert result == {"urgent": True}

    o = book_store.load_active_orders()[("SOc2", ITEM_A)]
    assert o.commitment == "urgent"
    assert o.promised_date == date(2025, 3, 10)   # snapshotted delivery date
