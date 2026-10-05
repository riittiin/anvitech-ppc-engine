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


def test_resaving_unchanged_item_as_the_browser_sends_it_keeps_digest():
    # The browser (app.js imSave) sends a null machine field back as "". Re-saving an
    # unedited item that way must change nothing, or one Save click would flag an
    # applied optimization stale (every real Test9 item has a null machine field).
    m, admin, _ = _setup()
    a = _get_item(admin, ITEM_A)          # first GET seeds the table
    assert any(s["allotted"] is None or s["suggested"] is None for s in a["steps"])
    d0 = im.digest(book_store.load_item_master())
    steps = [{"name": s["name"], "cycle": s["cycle"],
              "allotted": s["allotted"] if s["allotted"] is not None else "",
              "suggested": s["suggested"] if s["suggested"] is not None else ""}
             for s in a["steps"]]
    r = admin.put("/item-master", json={"code": ITEM_A, "description": a["description"],
                                        "steps": steps, "version": a["version"]})
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


def _punch(process, qty):
    book_store.append_actual(Actual(so_no="SO1", item_code=ITEM_A, process=process,
                                    entry_date=date(2026, 10, 1),
                                    qty_produced=qty, qty_rejected=0, operator="X"))


def _put_steps(admin, steps):
    a = _get_item(admin, ITEM_A)
    return admin.put("/item-master", json={"code": ITEM_A, "description": a["description"],
                                           "steps": steps, "version": a["version"]})


def test_a_new_step_in_front_of_a_punched_step_is_refused():
    m, admin, _ = _setup()
    _punch("BANDSAW", 4)
    steps = _get_item(admin, ITEM_A)["steps"]
    wash = {"name": "WASH", "cycle": 1, "allotted": "MW1", "suggested": ""}
    r = _put_steps(admin, [wash] + steps)
    assert r.status_code == 400
    assert ("WASH cannot go before BANDSAW: BANDSAW has 4 punched on SO1. "
            "Add new steps after it, or finish that order first.") in r.json()["detail"]
    assert _put_steps(admin, steps + [wash]).status_code == 200      # after: allowed


def test_moving_an_unpunched_step_in_front_of_a_punched_step_is_refused():
    m, admin, _ = _setup()
    _punch("BANDSAW", 4)
    _punch("CNC OS", 4)
    s = _get_item(admin, ITEM_A)["steps"]
    r = _put_steps(admin, [s[0], s[2], s[1]])
    assert r.status_code == 400
    assert "INSP cannot go before CNC OS: CNC OS has 4 punched on SO1." in r.json()["detail"]
    assert [x["name"] for x in _get_item(admin, ITEM_A)["steps"]] == ["BANDSAW", "CNC OS", "INSP"]


@pytest.mark.parametrize("raw_cycle, words", [
    ("60000", "1440 minutes"),
    ("NaN", "must be a number"),
    ("Infinity", "must be a number"),
    ("-Infinity", "must be a number"),
])
def test_cycle_time_out_of_bounds_is_refused_through_the_endpoint(raw_cycle, words):
    # A raw body: JSON parsers here accept NaN / Infinity, so this is what a hand-made
    # request can actually send.
    m, admin, _ = _setup()
    a = _get_item(admin, ITEM_A)
    body = ('{"code": "%s", "description": "", "version": %d, "steps": ['
            '{"name": "CNC OS", "cycle": %s, "allotted": "CNC1", "suggested": ""}]}'
            % (ITEM_A, a["version"], raw_cycle))
    r = admin.put("/item-master", content=body,
                  headers={"Content-Type": "application/json"})
    assert r.status_code == 400, r.text
    assert words in r.json()["detail"]


def test_an_outsourced_block_longer_than_60_days_is_refused():
    m, admin, _ = _setup()
    s = _get_item(admin, ITEM_A)["steps"]
    os_step = {"name": "HEAT TREAT OS", "cycle": 90 * 1440, "allotted": "OS", "suggested": ""}
    r = _put_steps(admin, s + [os_step])
    assert r.status_code == 400
    assert "86400 minutes" in r.json()["detail"]


def test_a_provisional_machine_cannot_be_newly_added():
    # CNC9 is in no Machine master row; the loader registered it provisionally because
    # ITEM_B's routing names it. It may stay on ITEM_B, never be added elsewhere.
    m, admin, _ = _setup()
    assert m._current_masters().machines["CNC9"].provisional
    s = _get_item(admin, ITEM_A)["steps"]
    r = _put_steps(admin, [s[0], dict(s[1], allotted="CNC9"), s[2]])
    assert r.status_code == 400
    assert "CNC9 is not a machine in your Machine master" in r.json()["detail"]
    b = _get_item(admin, ITEM_B)
    r = admin.put("/item-master", json={"code": ITEM_B, "description": b["description"],
                                        "steps": b["steps"], "version": b["version"]})
    assert r.status_code == 200, r.text


def test_delete_refused_while_a_draft_line_uses_the_item():
    m, admin, _ = _setup()
    book_store.save_new_order_drafts([{"so_no": "SO900", "item_code": ITEM_B, "qty": 5}])
    r = admin.post("/item-master/delete", json={"code": ITEM_B})
    assert r.status_code == 400
    assert "SO900 (Add New Orders draft)" in r.json()["detail"]
    assert ITEM_B in [i["code"] for i in admin.get("/item-master").json()["items"]]


def test_put_on_an_unknown_code_is_404():
    m, admin, _ = _setup()
    r = admin.put("/item-master", json={"code": "NOPE", "description": "", "version": 1,
                                        "steps": [{"name": "CNC", "cycle": 6,
                                                   "allotted": "CNC1", "suggested": ""}]})
    assert r.status_code == 404
    assert "NOPE" not in [i["code"] for i in admin.get("/item-master").json()["items"]]
