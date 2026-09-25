"""Standalone 'Mark this SO+item complete' (owner, 2026-07-28): needs only SO No + item
code — NO operator, no production punch — archives the order, and KEEPS the production
records so reports/efficiency still count them."""
import importlib
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A


def _api():
    import api.main as m
    importlib.reload(m)
    return m


def _seed(m):
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20))])
    m._current_masters()


def _client(m, user="anvitech", pw="1930rail"):
    c = TestClient(m.app)
    c.post("/login", data={"username": user, "password": pw})
    return c


def test_mark_complete_needs_only_so_and_item_no_operator():
    m = _api(); _seed(m); c = _client(m)
    assert ("SO1", ITEM_A) in book_store.load_active_orders()
    r = c.post("/orders/complete", json={"so_no": "SO1", "item_code": ITEM_A})
    assert r.status_code == 200 and r.json().get("completed") is True
    assert ("SO1", ITEM_A) not in book_store.load_active_orders()
    assert ("SO1", ITEM_A) in book_store.load_completed_orders()


def test_mark_complete_available_to_the_user_role():
    m = _api(); _seed(m); c = _client(m, "anvitech_user", "anvitech12345678")
    r = c.post("/orders/complete", json={"so_no": "SO1", "item_code": ITEM_A})
    assert r.status_code == 200


def test_mark_complete_unknown_order_is_404():
    m = _api(); _seed(m); c = _client(m)
    r = c.post("/orders/complete", json={"so_no": "NOPE", "item_code": "X"})
    assert r.status_code == 404


def test_mark_complete_keeps_the_production_records_for_reporting():
    m = _api(); _seed(m); c = _client(m)
    procs = [p.name for p in m._current_masters().routings[ITEM_A].processes]
    c.post("/actuals", json={"so_no": "SO1", "item_code": ITEM_A, "item_name": ITEM_A,
                             "entry_date": "2025-03-10", "qty_produced": 10, "qty_rejected": 0,
                             "shift": "1st shift", "process": procs[0], "operator": "Operator One"})
    n = len(book_store.load_actuals())
    assert n >= 1
    c.post("/orders/complete", json={"so_no": "SO1", "item_code": ITEM_A})
    assert len(book_store.load_actuals()) == n   # marking complete must not delete the records


# --- The day an order was marked complete is recorded (owner, 2026-09-25) --------------
# Before this, the Complete button archived the order and saved no date at all, so
# "how many orders were completed in August" could only be estimated from punches.

DAY = date(2026, 9, 25)


def test_complete_button_records_the_day_it_was_pressed(monkeypatch):
    m = _api(); _seed(m); c = _client(m)
    monkeypatch.setattr(m, "_ist_today", lambda: DAY)
    c.post("/orders/complete", json={"so_no": "SO1", "item_code": ITEM_A})
    assert book_store.load_completed_orders()[("SO1", ITEM_A)].completed_on == DAY


def test_the_orders_table_shows_the_completion_date(monkeypatch):
    m = _api(); _seed(m); c = _client(m)
    monkeypatch.setattr(m, "_ist_today", lambda: DAY)
    c.post("/orders/complete", json={"so_no": "SO1", "item_code": ITEM_A})
    t = c.get("/orders").json()
    t = t.get("orders", t)
    row = dict(zip(t["columns"], t["rows"][0]))
    assert row["Status"] == "Complete" and row["Completed On"] == "25-09-2026"


def test_daily_entry_mark_complete_records_the_day_it_was_marked_not_the_entry_date(monkeypatch):
    m = _api(); _seed(m); c = _client(m)
    monkeypatch.setattr(m, "_ist_today", lambda: DAY)
    procs = [p.name for p in m._current_masters().routings[ITEM_A].processes]
    r = c.post("/actuals", json={"so_no": "SO1", "item_code": ITEM_A, "item_name": ITEM_A,
                                 "entry_date": "2025-03-10", "qty_produced": 10, "qty_rejected": 0,
                                 "shift": "1st shift", "process": procs[0], "operator": "Operator One",
                                 "mark_complete": True})
    assert r.status_code == 200, r.text
    assert book_store.load_completed_orders()[("SO1", ITEM_A)].completed_on == DAY


def test_rolling_back_a_completion_clears_the_date():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20))])
    book_store.complete_order("SO1", ITEM_A, on=DAY)
    book_store.uncomplete_order("SO1", ITEM_A)
    assert book_store.load_active_orders()[("SO1", ITEM_A)].completed_on is None


def test_an_order_completed_before_the_date_was_recorded_loads_with_no_date():
    legacy = Order("SO1", ITEM_A, ITEM_A, 40, date(2025, 3, 20), completed=True).to_json()
    legacy.pop("completed_on")
    assert Order.from_json(legacy).completed_on is None
