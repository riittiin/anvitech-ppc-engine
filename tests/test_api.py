"""API smoke tests — login gate + order-book flow (upload → plan → actuals).

The app is gated by a signed session cookie now (not Basic Auth), so tests log in
as admin via POST /login (the TestClient keeps the cookie). A fresh client is made
per test so the cookie matches each test's isolated store/secret."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app  # noqa: E402
from api import auth  # noqa: E402
from tests.sample_workbook import build_sample_bytes, SO1, ITEM_A  # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SAMPLE = build_sample_bytes()   # generated workbook (Test3 format) — replaces Test2

_ACCTS = auth._accounts()
_ADMIN = next(u for u, a in _ACCTS.items() if a["role"] == auth.ADMIN)
_ADMIN_PWD = _ACCTS[_ADMIN]["password"]


@pytest.fixture
def client():
    """A TestClient logged in as admin (cookie persisted on the client)."""
    c = TestClient(app)
    r = c.post("/login", data={"username": _ADMIN, "password": _ADMIN_PWD})
    assert r.status_code == 200  # 303 followed to the app shell at /
    return c


def _upload_test_workbook(client):
    return client.post("/upload", files={"file": ("sample.xlsx", _SAMPLE, XLSX_MIME)})


def test_requires_login():
    anon = TestClient(app)
    assert anon.post("/run", json={"config": {}}).status_code == 401
    # Wrong password does not establish a session.
    bad = TestClient(app)
    assert bad.post("/login", data={"username": _ADMIN, "password": "wrong"}).status_code == 401
    assert bad.get("/orders").status_code == 401


def test_empty_book_plans_cleanly(client):
    r = client.post("/run", json={"config": {}})
    assert r.status_code == 200
    trace = r.json()["trace"]
    for rule in ["rule1", "rule2", "rule3", "rule6", "rule7", "rule8"]:
        assert rule in trace
    assert r.json()["orders"]["rows"] == []          # nothing uploaded yet


def test_upload_merges_then_plans(client):
    up = _upload_test_workbook(client)
    assert up.status_code == 200
    body = up.json()
    assert body["added"] == 3                        # 3 distinct SO numbers

    r = client.post("/run", json={"config": {}})
    assert r.status_code == 200
    assert len(r.json()["orders"]["rows"]) == 3
    assert len(r.json()["trace"]["rule1"]["output"]["rows"]) >= 1


def test_reupload_same_file_adds_nothing(client):
    _upload_test_workbook(client)
    again = _upload_test_workbook(client).json()
    assert again["added"] == 0
    assert len(again["flagged"]) >= 3                # every SO# now flagged


def test_actual_marks_order_complete(client):
    _upload_test_workbook(client)
    r = client.post("/actuals", json={
        "so_no": SO1, "item_code": ITEM_A,
        "entry_date": "2025-03-10", "qty_produced": 5, "mark_complete": True,
    })
    assert r.status_code == 200 and r.json()["completed_order"] is True

    rows = client.get("/orders").json()["orders"]["rows"]
    cols = client.get("/orders").json()["orders"]["columns"]
    si, sti = cols.index("SO No"), cols.index("Status")
    so = next(row for row in rows if row[si] == SO1)
    assert so[sti] == "Complete"


def test_delete_selected_and_clear_all(client):
    _upload_test_workbook(client)
    assert len(client.get("/orders").json()["orders"]["rows"]) == 3

    # Delete one order permanently (admin password required to confirm).
    d = client.post("/orders/delete", json={"so_nos": [SO1], "password": _ADMIN_PWD})
    assert d.status_code == 200 and d.json()["deleted"] == 1
    rows = client.get("/orders").json()["orders"]["rows"]
    assert len(rows) == 2 and not any(SO1 in str(r) for r in rows)

    # Clear everything.
    assert client.post("/orders/clear", json={"password": _ADMIN_PWD}).status_code == 200
    assert client.get("/orders").json()["orders"]["rows"] == []


def test_delete_rejected_without_correct_password(client):
    _upload_test_workbook(client)
    # Wrong / missing password → 403, nothing deleted.
    assert client.post("/orders/delete", json={"so_nos": [SO1], "password": "wrong"}).status_code == 403
    assert client.post("/orders/delete", json={"so_nos": [SO1]}).status_code == 403
    assert client.post("/orders/clear", json={"password": "wrong"}).status_code == 403
    assert len(client.get("/orders").json()["orders"]["rows"]) == 3   # all still there


def test_bad_upload_returns_400(client):
    bad = client.post("/upload", files={"file": ("x.xlsx", b"not excel", "application/octet-stream")})
    assert bad.status_code == 400


def test_report_lists_pending_master_data(client):
    _upload_test_workbook(client)
    assert "PENDING_MASTER_DATA" in str(client.get("/report").json())


def _parse_dt(s):
    # "DD-MM-YYYY HH:MM" -> a sortable tuple (chronological, cross-month safe).
    from datetime import datetime
    return datetime.strptime(s, "%d-%m-%Y %H:%M")


def _earliest_start_for(plan, machine):
    out = plan["trace"]["rule6"]["output"]
    ci = {c: i for i, c in enumerate(out["columns"])}
    starts = [_parse_dt(row[ci["Start"]]) for row in out["rows"] if row[ci["Machine"]] == machine]
    return min(starts)


def test_downtime_loops_back_into_the_schedule(client):
    _upload_test_workbook(client)
    # Admin planning with an explicit config persists it (persist=True).
    off = client.post("/run", json={"config": {"apply_downtime_to_plan": False},
                                     "persist": True}).json()

    out = off["trace"]["rule6"]["output"]
    ci = {c: i for i, c in enumerate(out["columns"])}
    first = min(out["rows"], key=lambda r: _parse_dt(r[ci["Start"]]))   # earliest op overall
    machine, item, process = first[ci["Machine"]], first[ci["Item Code"]], first[ci["Process"]]
    base_start = _earliest_start_for(off, machine)

    # Log a big machine breakdown against that machine's process.
    so = next((s for s, it in client.get("/items").json()["so_to_item"].items() if it == item), "X")
    client.post("/actuals", json={
        "so_no": so, "item_code": item, "entry_date": "2025-03-07",
        "process": process, "machine_breakdown_min": 600,
    })

    on = client.post("/run", json={"config": {"apply_downtime_to_plan": True},
                                    "persist": True}).json()
    # That machine's first op is pushed strictly later by the recorded downtime.
    assert _earliest_start_for(on, machine) > base_start
    # Visibility table is present when the feature is on.
    titles = [t["title"] for t in on["trace"]["rule6"].get("tables", [])]
    assert any("Downtime fed back" in t for t in titles)

    # Gate is clean: flag OFF again returns to the original timing (downtime ignored).
    off2 = client.post("/run", json={"config": {"apply_downtime_to_plan": False},
                                     "persist": True}).json()
    assert _earliest_start_for(off2, machine) == base_start
