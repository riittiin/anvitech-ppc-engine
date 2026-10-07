"""SO numbers carry the Indian financial-year prefix (owner, 2026-10-07): the floor
types only the last digits, so Add New Orders pre-fills "26-27SO" and a bare number
typed anyway is completed to it, never stored short."""
from datetime import date

from engine import orderbook


def test_prefix_follows_the_indian_financial_year():
    assert orderbook.so_prefix(date(2026, 4, 1)) == "26-27SO"
    assert orderbook.so_prefix(date(2026, 10, 7)) == "26-27SO"
    assert orderbook.so_prefix(date(2027, 3, 31)) == "26-27SO"
    assert orderbook.so_prefix(date(2027, 4, 1)) == "27-28SO"
    assert orderbook.so_prefix(date(2099, 6, 1)) == "99-00SO"


def test_a_bare_number_is_completed_to_the_prefix():
    d = date(2026, 10, 7)
    assert orderbook.normalize_so_no("228", d) == "26-27SO228"
    assert orderbook.normalize_so_no(" 319 ", d) == "26-27SO319"


def test_a_full_or_other_number_is_left_alone():
    d = date(2026, 10, 7)
    assert orderbook.normalize_so_no("26-27SO228", d) == "26-27SO228"
    assert orderbook.normalize_so_no("25-26SO900", d) == "25-26SO900"
    assert orderbook.normalize_so_no("", d) == ""


def test_the_prefix_alone_is_not_an_so_number():
    msg = orderbook.validate_new_order_line("26-27SO", "X", 5, {}, {}, None)
    assert msg == "Type the SO number after 26-27SO."


# --------------------------------------------------------------------------- #
# Through the API: what Add New Orders stores and what the screen pre-fills
# --------------------------------------------------------------------------- #
import importlib

import pytest
from fastapi.testclient import TestClient

from engine import book_store
from tests.new_sample_workbook import ITEM_A, build_new_sample_bytes


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    import api.main as m
    importlib.reload(m)
    book_store.save_masters_bytes(build_new_sample_bytes())
    monkeypatch.setattr(m, "_ist_today", lambda: date(2026, 10, 7))
    client = TestClient(m.app)
    client.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return client


def test_drafts_store_the_full_so_number(api):
    r = api.put("/new-orders/drafts",
                json={"drafts": [{"so_no": "228", "item_code": ITEM_A, "qty": 5}]})
    assert r.status_code == 200, r.text
    assert r.json()["drafts"][0]["so_no"] == "26-27SO228"
    assert book_store.load_new_order_drafts()[0]["so_no"] == "26-27SO228"


def test_the_screen_is_told_the_prefix(api):
    assert api.get("/new-orders/drafts").json()["so_prefix"] == "26-27SO"
