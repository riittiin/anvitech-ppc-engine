"""API smoke tests — login gate + order-book flow (upload → plan → actuals)."""
import base64

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app, APP_USERNAME, APP_PASSWORD  # noqa: E402
from engine.loaders import DEFAULT_XLSX  # noqa: E402


def _auth(user, pwd):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()}


client = TestClient(app)
client.headers.update(_auth(APP_USERNAME, APP_PASSWORD))

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _upload_test_workbook():
    with open(DEFAULT_XLSX, "rb") as fh:
        return client.post("/upload", files={"file": ("Test2.xlsx", fh, XLSX_MIME)})


def test_requires_login():
    anon = TestClient(app)
    assert anon.post("/run", json={"config": {}}).status_code == 401
    bad = TestClient(app)
    bad.headers.update(_auth(APP_USERNAME, "wrong"))
    assert bad.get("/orders").status_code == 401


def test_empty_book_plans_cleanly():
    r = client.post("/run", json={"config": {}})
    assert r.status_code == 200
    trace = r.json()["trace"]
    for rule in ["rule1", "rule2", "rule3", "rule6", "rule7", "rule8"]:
        assert rule in trace
    assert r.json()["orders"]["rows"] == []          # nothing uploaded yet


def test_upload_merges_then_plans():
    up = _upload_test_workbook()
    assert up.status_code == 200
    body = up.json()
    # 7 distinct SO numbers; the reused 24-25SO121A line is flagged, not added.
    assert body["added"] == 7
    assert any("duplicate" in f["reason"] for f in body["flagged"])

    r = client.post("/run", json={"config": {}})
    assert r.status_code == 200
    assert len(r.json()["orders"]["rows"]) == 7
    assert len(r.json()["trace"]["rule1"]["output"]["rows"]) >= 1


def test_reupload_same_file_adds_nothing():
    _upload_test_workbook()
    again = _upload_test_workbook().json()
    assert again["added"] == 0
    assert len(again["flagged"]) >= 7                # every SO# now flagged


def test_actual_marks_order_complete():
    _upload_test_workbook()
    r = client.post("/actuals", json={
        "so_no": "24-25SO214", "item_code": "61240807-01",
        "entry_date": "2025-03-07", "qty_produced": 10, "mark_complete": True,
    })
    assert r.status_code == 200 and r.json()["completed_order"] is True

    rows = client.get("/orders").json()["orders"]["rows"]
    cols = client.get("/orders").json()["orders"]["columns"]
    si, sti = cols.index("SO No"), cols.index("Status")
    so214 = next(row for row in rows if row[si] == "24-25SO214")
    assert so214[sti] == "Complete"


def test_delete_selected_and_clear_all():
    _upload_test_workbook()
    assert len(client.get("/orders").json()["orders"]["rows"]) == 7

    # Delete one order permanently.
    d = client.post("/orders/delete", json={"so_nos": ["24-25SO214"]})
    assert d.status_code == 200 and d.json()["deleted"] == 1
    rows = client.get("/orders").json()["orders"]["rows"]
    assert len(rows) == 6 and not any("24-25SO214" in str(r) for r in rows)

    # Clear everything.
    assert client.post("/orders/clear", json={}).status_code == 200
    assert client.get("/orders").json()["orders"]["rows"] == []


def test_bad_upload_returns_400():
    bad = client.post("/upload", files={"file": ("x.xlsx", b"not excel", "application/octet-stream")})
    assert bad.status_code == 400


def test_report_lists_pending_master_data():
    _upload_test_workbook()
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


def test_downtime_loops_back_into_the_schedule():
    _upload_test_workbook()
    off = client.post("/run", json={"config": {"apply_downtime_to_plan": False}}).json()

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

    on = client.post("/run", json={"config": {"apply_downtime_to_plan": True}}).json()
    # That machine's first op is pushed strictly later by the recorded downtime.
    assert _earliest_start_for(on, machine) > base_start
    # Visibility table is present when the feature is on.
    titles = [t["title"] for t in on["trace"]["rule6"].get("tables", [])]
    assert any("Downtime fed back" in t for t in titles)

    # Gate is clean: flag OFF again returns to the original timing (downtime ignored).
    off2 = client.post("/run", json={"config": {"apply_downtime_to_plan": False}}).json()
    assert _earliest_start_for(off2, machine) == base_start
