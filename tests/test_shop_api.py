from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _client(m, admin=True):
    c = TestClient(m.app)
    if admin:
        c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    else:
        c.post("/login", data={"username": "anvitech_user", "password": "anvitech12345678"})
    return c


def _setup():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])
    m = _api()
    return m, _client(m), _client(m, admin=False)


def test_get_machines_for_both_roles():
    m, admin, user = _setup()
    for c in (admin, user):
        r = c.get("/machines")
        assert r.status_code == 200
        data = r.json()
        assert data["machines"] and data["types"]
        cnc1 = next(x for x in data["machines"] if x["id"] == "CNC1")
        assert cnc1["kind"] == "machining"
        assert any("SAMP-A-01" in u for u in cnc1["used_by"])


def test_user_role_cannot_write():
    m, admin, user = _setup()
    assert user.post("/machines", json={"id": "CNC8", "type": "CNC lathe", "hours": 19.5}).status_code == 403
    assert user.put("/machines", json={"id": "CNC1", "type": "x", "hours": 9.5, "version": 1}).status_code == 403
    assert user.post("/machines/delete", json={"id": "CNC1"}).status_code == 403
    assert user.post("/holidays", json={"date": "2026-11-08", "name": "Diwali"}).status_code == 403
    assert user.post("/holidays/delete", json={"date": "2026-11-08"}).status_code == 403


def test_add_machine_appears_in_pickers_and_plan():
    m, admin, _ = _setup()
    r = admin.post("/machines", json={"id": "CNC 8", "type": "CNC lathe", "hours": 19.5})
    assert r.status_code == 200, r.text
    assert r.json()["machine"]["id"] == "CNC8"
    assert "CNC8" in m._current_masters().machines
    ops = admin.get("/operators").json()["machines"]
    assert any(x["id"] == "CNC8" for x in ops)
    assert admin.post("/machines", json={"id": "CNC8", "type": "CNC lathe", "hours": 19.5}).status_code == 409


def test_edit_machine_with_version_check():
    m, admin, _ = _setup()
    cnc1 = next(x for x in admin.get("/machines").json()["machines"] if x["id"] == "CNC1")
    body = {"id": "CNC1", "type": cnc1["type"], "hours": 9.5, "version": cnc1["version"]}
    assert admin.put("/machines", json=body).status_code == 200
    assert m._current_masters().machines["CNC1"].available_hrs_per_day == 9.5
    assert admin.put("/machines", json=body).status_code == 409
    assert admin.put("/machines", json={**body, "id": "NOPE9"}).status_code == 404


def test_delete_machine_refused_while_used_then_allowed():
    m, admin, _ = _setup()
    r = admin.post("/machines/delete", json={"id": "CNC1"})
    assert r.status_code == 400 and "SAMP-A-01" in r.json()["detail"]
    assert admin.post("/machines", json={"id": "MX9", "type": "Manual Packing", "hours": 9.5}).status_code == 200
    assert admin.post("/machines/delete", json={"id": "MX9"}).status_code == 200
    assert admin.post("/machines/delete", json={"id": "MX9"}).status_code == 404


def test_holidays_add_list_delete():
    m, admin, user = _setup()
    r = admin.post("/holidays", json={"date": "2026-11-08", "name": "Diwali"})
    assert r.status_code == 200, r.text
    data = user.get("/holidays").json()
    assert data["weekly_off"] == "Thursday"
    assert {"date": "2026-11-08", "name": "Diwali"} in data["holidays"]
    assert date(2026, 11, 8) in m._current_masters().calendar.holidays
    assert admin.post("/holidays", json={"date": "2026-11-08", "name": "Again"}).status_code == 400
    assert admin.post("/holidays/delete", json={"date": "2026-11-08"}).status_code == 200
    assert admin.post("/holidays/delete", json={"date": "2026-11-08"}).status_code == 404


def test_invalid_machine_and_holiday_inputs():
    m, admin, _ = _setup()
    assert admin.post("/machines", json={"id": "CNC8", "type": "CNC lathe", "hours": 30}).status_code == 400
    assert admin.post("/machines", json={"id": "", "type": "CNC lathe", "hours": 9.5}).status_code == 400
    assert admin.post("/holidays", json={"date": "nope", "name": "x"}).status_code == 400
