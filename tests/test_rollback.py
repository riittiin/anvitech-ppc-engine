"""Capture-actuals rollback — delete one mis-punched entry, return that order to
normal, including un-archiving an order that the entry had marked complete."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from api import auth  # noqa: E402
from tests.sample_workbook import build_sample_bytes, SO1, ITEM_A  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SAMPLE = build_sample_bytes()
_ACCTS = auth._accounts()
_ADMIN = next(u for u, a in _ACCTS.items() if a["role"] == auth.ADMIN)
_ADMIN_PWD = _ACCTS[_ADMIN]["password"]


@pytest.fixture
def client():
    c = TestClient(app)
    c.post("/login", data={"username": _ADMIN, "password": _ADMIN_PWD})
    c.post("/upload", files={"file": ("sample.xlsx", _SAMPLE, XLSX_MIME)})
    return c


def _post(client, **kw):
    body = {"so_no": SO1, "item_code": ITEM_A, "entry_date": "2025-03-10"}
    body.update(kw)
    r = client.post("/actuals", json=body)
    assert r.status_code == 200
    return r.json()["actuals_ids"][-1]   # the just-appended entry's id


def _status(client, so):
    o = client.get("/orders").json()["orders"]
    ci = {c: i for i, c in enumerate(o["columns"])}
    row = next((r for r in o["rows"] if r[ci["SO No"]] == so), None)
    return None if row is None else row[ci["Status"]]


def _remaining(client, so):
    o = client.get("/orders").json()["orders"]
    ci = {c: i for i, c in enumerate(o["columns"])}
    row = next(r for r in o["rows"] if r[ci["SO No"]] == so)
    return row[ci["Remaining"]]


def test_rollback_normal_entry_restores_order(client):
    eid = _post(client, qty_produced=3)               # SO-001 ordered 5
    assert _status(client, SO1) == "Running"
    assert _remaining(client, SO1) == 2
    r = client.post("/actuals/rollback", json={"id": eid})
    assert r.status_code == 200 and r.json()["removed"] is True
    assert _status(client, SO1) == "Pending"          # no actuals left
    assert _remaining(client, SO1) == 5


def test_rollback_mark_complete_unarchives_order(client):
    eid = _post(client, qty_produced=5, mark_complete=True)
    assert _status(client, SO1) == "Complete"
    r = client.post("/actuals/rollback", json={"id": eid}).json()
    assert r["uncompleted_order"] is True
    assert _status(client, SO1) == "Pending"          # back to active, recomputed


def test_rollback_one_of_many_keeps_the_rest(client):
    e1 = _post(client, qty_produced=2)
    _post(client, qty_produced=1)
    client.post("/actuals/rollback", json={"id": e1})
    assert _remaining(client, SO1) == 4               # only the qty-2 entry removed
    assert _status(client, SO1) == "Running"


def test_rollback_keeps_complete_if_another_entry_still_marks_it(client):
    e1 = _post(client, qty_produced=5, mark_complete=True)   # completes + archives
    _post(client, qty_produced=0, mark_complete=True)        # 2nd complete flag remains
    r = client.post("/actuals/rollback", json={"id": e1}).json()
    assert r["uncompleted_order"] is False
    assert _status(client, SO1) == "Complete"               # stays complete


def test_rollback_unknown_id_is_404(client):
    assert client.post("/actuals/rollback", json={"id": "nope"}).status_code == 404


def test_rolled_back_downtime_drops_out_of_plan(client):
    # An entry's downtime feeds the plan; rolling it back removes that effect.
    eid = _post(client, qty_produced=1, machine_breakdown_min=600)
    on = client.post("/run", json={"config": {"apply_downtime_to_plan": True}}).json()
    assert any(a != "" for row in on["trace"]["rule7"]["output"]["rows"] for a in row)
    client.post("/actuals/rollback", json={"id": eid})
    after = client.post("/run", json={"config": {"apply_downtime_to_plan": True}}).json()
    assert after["trace"]["rule7"]["output"]["rows"] == []   # no actuals left
