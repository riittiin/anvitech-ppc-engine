"""API smoke tests — endpoints return well-formed traces and handle errors."""
import pytest

pytest.importorskip("fastapi")
import base64  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.main import app, APP_USERNAME, APP_PASSWORD  # noqa: E402


def _auth_header(user, pwd):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()}


# Every request is gated by Basic Auth; the client sends the default dev creds.
client = TestClient(app)
client.headers.update(_auth_header(APP_USERNAME, APP_PASSWORD))


def test_requires_login():
    anon = TestClient(app)
    r = anon.post("/run", json={"config": {}})
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers
    # Wrong password is rejected too.
    bad = TestClient(app)
    bad.headers.update(_auth_header(APP_USERNAME, "wrong"))
    assert bad.get("/items").status_code == 401


def test_upload_then_run_uses_uploaded_dataset():
    from engine.loaders import DEFAULT_XLSX
    with open(DEFAULT_XLSX, "rb") as fh:
        up = client.post(
            "/upload",
            files={"file": ("Test2.xlsx", fh,
                            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        )
    assert up.status_code == 200
    body = up.json()
    assert body["dataset_id"]
    assert body["summary"]["so_lines"] == 8 and body["summary"]["items"] == 85

    # A run tagged with the dataset_id plans against the uploaded workbook.
    r = client.post("/run", json={"config": {}, "dataset_id": body["dataset_id"]})
    assert r.status_code == 200
    assert len(r.json()["trace"]["rule1"]["output"]["rows"]) == 7


def test_upload_rejects_bad_file():
    bad = client.post("/upload", files={"file": ("x.xlsx", b"not a real excel", "application/octet-stream")})
    assert bad.status_code == 400


def test_run_returns_trace_with_all_rules():
    r = client.post("/run", json={"config": {"consolidation_window_days": 10}})
    assert r.status_code == 200
    trace = r.json()["trace"]
    for rule in ["rule1", "rule2", "rule3", "rule4", "rule5", "rule6", "rule7", "rule8", "rule9"]:
        assert rule in trace
    assert len(trace["rule1"]["output"]["rows"]) == 7   # 7 consolidated batches


def test_run_then_fetch_trace():
    rid = client.post("/run", json={"config": {}}).json()["run_id"]
    r = client.get(f"/trace/{rid}")
    assert r.status_code == 200
    assert r.json()["run_id"] == rid


def test_bad_config_returns_400():
    r = client.post("/run", json={"config": {"overlap_percent": 500}})
    assert r.status_code == 400


def test_report_lists_pending_master_data():
    r = client.get("/report")
    assert r.status_code == 200
    text = str(r.json())
    assert "PENDING_MASTER_DATA" in text


def test_actuals_and_rerun(tmp_path, monkeypatch):
    # Redirect the actuals store to a temp file so the test is isolated.
    import engine.rules.rule8_capture_actuals as r8
    monkeypatch.setattr(r8, "DEFAULT_STORE", tmp_path / "actuals.json")

    saved = client.post("/actuals", json={
        "so_no": "24-25SO214", "item_code": "61240807-01",
        "entry_date": "2025-03-07", "qty_produced": 4,
    })
    assert saved.status_code == 200

    rr = client.post("/rerun", json={"config": {}})
    assert rr.status_code == 200
    assert "rule9" in rr.json()["trace"]
