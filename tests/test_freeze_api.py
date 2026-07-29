"""API-level test for Task 17: persisting the applied plan's schedule at apply
time (`book_store.save_last_applied_schedule`, written from `_optimize_apply`).

The freeze feature is NEW-ENGINE-ONLY, so these tests force
``DEFAULT_SCHEDULER=new`` and seed the NEW-engine sample workbook (the classic
sample is deliberately under-staffed for the new engine's stricter operator
requirements — see tests/new_sample_workbook.py).

Fixtures here are local to this module (not tests/conftest.py) so they don't
change behaviour for the rest of the suite, which validates the classic engine
by default.
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
    """Force the new engine (authoritative env var) and reload api.main so its
    module-level state (in-memory optimize job, masters cache, etc.) starts
    fresh under that env. Uses monkeypatch (not a raw os.environ write) so the
    env var is restored after the test — leaking DEFAULT_SCHEDULER=new into
    later tests in the same session silently switches them to the new engine
    (that leak is exactly what caused the first version of this fixture to
    fail unrelated tests in tests/test_optimize_api.py /
    tests/test_report_and_staleness.py when the whole suite ran)."""
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
def admin_client_with_book(monkeypatch):
    """A logged-in admin TestClient, wired to the new engine, with the new
    sample workbook's masters + two orders (one per item) merged into the
    order book. Optimize budgets are shrunk so a quick/deep search converges
    fast in a test process (the live default is 1000 evals/click)."""
    m = _api_with_new_engine(monkeypatch)
    _seed_new_book()

    # Trigger the one-time operator seed (from the workbook's operator sheet)
    # so the new engine has a qualified crew to schedule against.
    client = TestClient(m.app)
    client.post("/login", data={"username": "anvitech", "password": "1930rail"})
    client.get("/operators")

    # Keep the search small + deterministic + LOCAL (no cloud dispatch — the
    # sample book has only 2 orders so this still converges to a real result).
    m._OPT_BUDGETS = {"quick": 20, "deep": 20}
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    monkeypatch.delenv("OPTIMIZE_WORKER_SECRET", raising=False)

    yield client


def _wait_optimize_done(client, timeout_s=60):
    """Poll GET /optimize/status until the job leaves the 'running' state.
    Fails the test (rather than hanging) if it never finishes in time."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        body = client.get("/optimize/status").json()
        if body["state"] in ("done", "idle"):
            return body
        time.sleep(0.2)
    raise AssertionError(f"optimize did not finish within {timeout_s}s")


def test_apply_persists_last_applied_schedule(admin_client_with_book):
    client = admin_client_with_book
    # Nothing applied yet.
    assert book_store.load_last_applied_schedule() == []

    r = client.post("/optimize", json={"budget": "quick"})
    assert r.status_code == 200, r.text
    status = _wait_optimize_done(client)
    assert status["state"] == "done", status

    r = client.post("/optimize/apply")
    assert r.status_code == 200, r.text

    rows = book_store.load_last_applied_schedule()
    assert rows, "apply did not persist the applied schedule"
    assert {"batch_id", "item_code", "process_seq", "machine", "operator",
            "start", "end", "so_refs"} <= set(rows[0].keys())
    # Sanity: rows reference the two orders we seeded.
    all_refs = {ref for row in rows for ref in row["so_refs"]}
    assert SO1 in all_refs or SO2 in all_refs


def _punch_partial(client):
    """Punch a partial good quantity (less than the ordered qty) on the first
    in-house machining step of SO1/ITEM_A ("CNC FIRST SIDE", 50 ordered) —
    good=20 leaves remaining_qty=30 > 0, so the step is in-progress and
    should be frozen. Operator "Alpha" is qualified for CNC1/CNC2 (see
    tests/new_sample_workbook.py OPERATORS)."""
    r = client.post("/actuals", json={
        "so_no": SO1, "item_code": ITEM_A, "item_name": ITEM_A,
        "entry_date": "2025-03-20", "qty_produced": 20, "qty_rejected": 0,
        "shift": "1st shift", "process": "CNC FIRST SIDE", "operator": "Alpha",
    })
    assert r.status_code == 200, r.text
    return r


def test_done_computes_frozen_set_from_partial_punch(admin_client_with_book):
    client = admin_client_with_book
    # Apply an initial plan so a last-applied schedule exists.
    r = client.post("/optimize", json={"budget": "quick"})
    assert r.status_code == 200, r.text
    _wait_optimize_done(client)
    r = client.post("/optimize/apply")
    assert r.status_code == 200, r.text

    # Punch a PARTIAL quantity on an in-progress in-house machining step.
    _punch_partial(client)

    # Compute the frozen set (Done path helper) directly.
    import api.main as m
    frozen = m._compute_and_store_frozen()
    assert frozen == book_store.load_frozen_ops()
    assert any(r["remaining_qty"] > 0 and r["machine"] for r in frozen)
