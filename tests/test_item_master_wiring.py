"""The Item Process Master reaches every planner: store, api masters, new engine,
cloud payload."""
import io
import json
from datetime import date

import pytest

from engine import book_store, item_master as im
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def test_store_round_trip():
    assert book_store.load_item_master() is None
    doc = im.seed_doc(build_sample_bytes(), "2026-10-05T10:00:00")
    book_store.save_item_master(doc)
    assert book_store.load_item_master() == doc
    assert list(book_store.load_item_master()["items"]) == [ITEM_A, ITEM_B]


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
                           Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21))])


def test_first_masters_read_seeds_the_table_once():
    m = _api(); _seed_book()
    assert book_store.load_item_master() is None
    masters = m._current_masters()
    doc = book_store.load_item_master()
    assert list(doc["items"]) == list(masters.routings) == [ITEM_A, ITEM_B]
    doc["items"][ITEM_A]["description"] = "EDITED"
    book_store.save_item_master(doc)
    m._current_masters()
    assert book_store.load_item_master()["items"][ITEM_A]["description"] == "EDITED"


def test_masters_follow_a_table_edit():
    m = _api(); _seed_book()
    m._current_masters()
    doc = book_store.load_item_master()
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 30
    book_store.save_item_master(doc)
    proc = m._current_masters().routings[ITEM_A].processes[0]
    assert proc.cycle_time == 30


def test_inputs_signature_unchanged_by_seeding_but_moved_by_an_edit(monkeypatch):
    m = _api(); _seed_book()
    cfg = m._load_plan_config()
    m._current_masters()                         # seeds
    seeded = m._inputs_signature(cfg)
    # Without the table part the formula is the pre-feature one; a freshly seeded
    # table must not change the signature (no applied optimization goes stale on deploy).
    real_load = book_store.load_item_master
    monkeypatch.setattr(m.book_store, "load_item_master", lambda: None)
    m._MASTERS_CACHE["masters"] = None
    assert m._inputs_signature(cfg) == seeded
    monkeypatch.setattr(m.book_store, "load_item_master", real_load)
    doc = book_store.load_item_master()
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 30
    book_store.save_item_master(doc)
    assert m._inputs_signature(cfg) != seeded


def test_plan_fingerprint_moves_with_the_table():
    m = _api(); _seed_book()
    cfg = m._load_plan_config()
    f0 = m._plan_fingerprint(cfg)
    doc = book_store.load_item_master()
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 30
    book_store.save_item_master(doc)
    assert m._plan_fingerprint(cfg) != f0


def _admin(m):
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return c


def _workbook_with_cycle(cycle):
    from tests.sample_workbook import build_workbook
    wb = build_workbook()
    wb["Item's process Master"].cell(row=3, column=14).value = cycle   # ITEM_A step 1 cycle
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def test_upload_after_seed_does_not_touch_routings():
    m = _api(); _seed_book()
    m._current_masters()
    r = _admin(m).post("/upload", files={"file": ("x.xlsx", _workbook_with_cycle(99))})
    assert r.status_code == 200, r.text
    assert "Item Process Master" in r.json()["routings_note"]
    assert m._current_masters().routings[ITEM_A].processes[0].cycle_time == 3


def test_upload_without_routing_sheet_is_accepted_once_seeded():
    m = _api(); _seed_book()
    m._current_masters()
    from tests.sample_workbook import build_workbook
    wb = build_workbook(); del wb["Item's process Master"]
    buf = io.BytesIO(); wb.save(buf)
    r = _admin(m).post("/upload", files={"file": ("x.xlsx", buf.getvalue())})
    assert r.status_code == 200, r.text
    assert list(m._current_masters().routings) == [ITEM_A, ITEM_B]


def test_upload_as_first_request_seeds_from_the_workbook_on_file():
    m = _api(); _seed_book()          # no _current_masters(): table not seeded yet
    assert book_store.load_item_master() is None
    r = _admin(m).post("/upload", files={"file": ("x.xlsx", _workbook_with_cycle(99))})
    assert r.status_code == 200, r.text
    assert r.json()["routings_note"]
    assert m._current_masters().routings[ITEM_A].processes[0].cycle_time == 3


def test_new_engine_masters_follow_the_table():
    from engine import new_engine
    new_engine._MASTERS_CACHE.clear()
    m = _api(); _seed_book(); m._current_masters()
    before = new_engine._new_masters(False).routings[ITEM_A].operations[0].cycle_min
    doc = book_store.load_item_master()
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 30
    book_store.save_item_master(doc)
    after = new_engine._new_masters(False).routings[ITEM_A].operations[0].cycle_min
    assert before == 3.0 and after == 30.0      # BANDSAW is manual: no +30%


def test_new_engine_override_wins_and_none_means_workbook():
    from engine import new_engine
    new_engine._MASTERS_CACHE.clear()
    _seed_book()
    doc = im.seed_doc(build_sample_bytes(), "t")
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 44
    try:
        new_engine.set_item_master(doc)
        assert new_engine._new_masters(False).routings[ITEM_A].operations[0].cycle_min == 44.0
        new_engine.set_item_master(None)
        assert new_engine._new_masters(False).routings[ITEM_A].operations[0].cycle_min == 3.0
    finally:
        new_engine.clear_item_master_override()
