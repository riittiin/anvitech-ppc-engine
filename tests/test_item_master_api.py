from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store, item_master as im
from engine.models import Actual, Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


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


def _get_item(c, code):
    return next(i for i in c.get("/item-master").json()["items"] if i["code"] == code)


def test_get_lists_items_with_kinds_for_both_roles():
    m, admin, user = _setup()
    for c in (admin, user):
        r = c.get("/item-master")
        assert r.status_code == 200
        data = r.json()
        assert [i["code"] for i in data["items"]] == [ITEM_A, ITEM_B]
        a = data["items"][0]
        assert a["open_orders"] == ["SO1"]
        assert a["steps"][1]["kind"] == "machine" and a["steps"][1]["machining"] is True
        assert data["planning_factor"] == pytest.approx(1.3)
        assert any(mm["id"] == "CNC1" for mm in data["machines"])


def test_user_role_cannot_write():
    m, admin, user = _setup()
    a = _get_item(admin, ITEM_A)
    body = {"code": ITEM_A, "description": "X", "steps": a["steps"], "version": a["version"]}
    assert user.put("/item-master", json=body).status_code == 403
    assert user.post("/item-master", json={**body, "code": "NEW1"}).status_code == 403
    assert user.post("/item-master/delete", json={"code": ITEM_B}).status_code == 403


def test_save_changes_the_plan_input():
    m, admin, _ = _setup()
    a = _get_item(admin, ITEM_A)
    steps = [dict(s) for s in a["steps"]]
    steps[0]["cycle"] = 30
    r = admin.put("/item-master", json={"code": ITEM_A, "description": a["description"],
                                        "steps": steps, "version": a["version"]})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["version"] == a["version"] + 1
    assert m._current_masters().routings[ITEM_A].processes[0].cycle_time == 30


def test_resaving_unchanged_item_keeps_digest():
    m, admin, _ = _setup()
    a = _get_item(admin, ITEM_A)          # first GET seeds the table
    d0 = im.digest(book_store.load_item_master())
    r = admin.put("/item-master", json={"code": ITEM_A, "description": a["description"],
                                        "steps": a["steps"], "version": a["version"]})
    assert r.status_code == 200, r.text
    assert im.digest(book_store.load_item_master()) == d0


def test_stale_version_is_refused():
    m, admin, _ = _setup()
    a = _get_item(admin, ITEM_A)
    body = {"code": ITEM_A, "description": "one", "steps": a["steps"], "version": a["version"]}
    assert admin.put("/item-master", json=body).status_code == 200
    r = admin.put("/item-master", json={**body, "description": "two"})
    assert r.status_code == 409
    assert "Someone else saved" in r.json()["detail"]
    assert _get_item(admin, ITEM_A)["description"] == "one"


def test_invalid_item_is_refused_with_every_reason():
    m, admin, _ = _setup()
    r = admin.post("/item-master", json={"code": "NEW1", "description": "",
                                         "steps": [{"name": "", "cycle": 1, "allotted": "MD1", "suggested": ""}]})
    assert r.status_code == 400
    assert "Step 1 needs a name." in r.json()["detail"]


def test_create_then_duplicate_code_conflicts():
    m, admin, _ = _setup()
    body = {"code": "NEW1", "description": "N",
            "steps": [{"name": "CNC", "cycle": 6, "allotted": "CNC1", "suggested": ""}]}
    assert admin.post("/item-master", json=body).status_code == 200
    assert admin.post("/item-master", json=body).status_code == 409
    codes = [i["code"] for i in admin.get("/item-master").json()["items"]]
    assert codes[-1] == "NEW1"


def test_renaming_a_punched_step_is_refused():
    m, admin, _ = _setup()
    book_store.append_actual(Actual(so_no="SO1", item_code=ITEM_A, process="BANDSAW",
                                    entry_date=date(2026, 10, 1),
                                    qty_produced=4, qty_rejected=0, operator="X"))
    a = _get_item(admin, ITEM_A)
    steps = [dict(s) for s in a["steps"]]
    steps[0]["name"] = "BAND SAW"
    r = admin.put("/item-master", json={"code": ITEM_A, "description": a["description"],
                                        "steps": steps, "version": a["version"]})
    assert r.status_code == 400
    assert "BANDSAW has 4 punched on SO1" in r.json()["detail"]


def test_delete_refused_while_in_use_and_allowed_when_not():
    m, admin, _ = _setup()
    r = admin.post("/item-master/delete", json={"code": ITEM_A})
    assert r.status_code == 400 and "SO1" in r.json()["detail"]
    assert admin.post("/item-master/delete", json={"code": ITEM_B}).status_code == 200
    assert admin.post("/item-master/delete", json={"code": ITEM_B}).status_code == 404


def test_dotted_item_codes_work():
    m, admin, _ = _setup()
    body = {"code": "61243661-01..", "description": "dots",
            "steps": [{"name": "CNC", "cycle": 6, "allotted": "CNC1", "suggested": ""}]}
    assert admin.post("/item-master", json=body).status_code == 200
    assert admin.post("/item-master/delete", json={"code": "61243661-01.."}).status_code == 200
