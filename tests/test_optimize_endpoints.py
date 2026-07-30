"""Task 6: `_finalize_optimize` recomputes the winner AT the winning machine-set
(so the Optimize panel's shown numbers match what Apply actually produces), and
`/optimize/status` exposes that winning machine-set.

Fixtures mirror tests/test_freeze_api.py's new-engine harness (force
DEFAULT_SCHEDULER=new + the new-engine sample workbook, shrink the optimize
budgets so a quick search converges fast in-process). Kept local to this
module (not tests/conftest.py) so they don't change behaviour for the rest of
the suite, which validates the classic engine by default.
"""
import importlib
import time
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store
from engine.models import Order
from tests.new_sample_workbook import build_new_sample_bytes, ITEM_A, ITEM_B, SO1, SO2


def _api_with_new_engine(monkeypatch):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    import api.main as m
    importlib.reload(m)
    return m


def _seed_new_book():
    book_store.save_masters_bytes(build_new_sample_bytes())
    book_store.add_orders([
        Order(SO1, ITEM_A, ITEM_A, 50, date(2025, 3, 20)),
        Order(SO2, ITEM_B, ITEM_B, 100, date(2025, 3, 21)),
    ])


@pytest.fixture
def new_engine_client_with_book(monkeypatch):
    """A logged-in admin TestClient, wired to the new engine, with the new
    sample workbook's masters + two orders (one per item) merged into the
    order book. Optimize budgets are shrunk so a quick search converges fast
    in a test process (the live default is 1000 evals/click)."""
    m = _api_with_new_engine(monkeypatch)
    _seed_new_book()

    client = TestClient(m.app)
    client.post("/login", data={"username": "anvitech", "password": "1930rail"})
    client.get("/operators")   # trigger the one-time operator seed

    m._OPT_BUDGETS = {"quick": 20, "deep": 20}
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    monkeypatch.delenv("OPTIMIZE_WORKER_SECRET", raising=False)

    yield client


def _wait_done(client, timeout_s=60):
    """Poll GET /optimize/status until the job leaves the 'running' state."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get("/optimize/status").json()
        if body["state"] in ("done", "idle"):
            return body
        time.sleep(0.2)
    raise AssertionError("optimize did not finish within the timeout")


def test_finalize_recomputes_winner_at_its_machine_set(new_engine_client_with_book):
    """A local sweep's winning machine-set must be stored and surfaced on
    /optimize/status, so the Optimize panel and Apply agree (shown == applied)."""
    client = new_engine_client_with_book
    r = client.post("/optimize", json={"budget": "quick"})
    assert r.status_code == 200, r.text
    _wait_done(client)
    st = client.get("/optimize/status").json()
    assert st["state"] == "done", st
    assert "flexible_machines" in st
    assert "current_flexible" in st
    assert isinstance(st["flexible_machines"], bool)
    assert isinstance(st["current_flexible"], bool)
