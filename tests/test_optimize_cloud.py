"""The cloud-optimize path: dispatch to GitHub Actions, worker endpoints
(secret-gated payload/progress/result), and the fallbacks that keep the
Optimize button working when the cloud misbehaves. No network — the dispatch
call is stubbed and the 'worker' is driven in-process via the same
optimize_service functions the real worker script uses."""
import time
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store, optimize_service
from engine.config import Config
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B

SECRET = "test-worker-secret"


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
        Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
    ])


def _cloud_env(monkeypatch, timeout_min="5"):
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "fake-token")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", SECRET)
    monkeypatch.setenv("OPTIMIZE_CLOUD_TIMEOUT_MIN", timeout_min)


def _wait(m, state, timeout=15.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = m._optimize_status()
        if st["state"] == state:
            return st
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for state={state}; last={st}")


def test_worker_endpoints_require_the_secret(monkeypatch):
    _cloud_env(monkeypatch)
    m = _api()
    client = TestClient(m.app)
    assert client.get("/optimize/job/xyz").status_code == 401           # no session
    assert client.get("/optimize/job/xyz",
                      headers={"X-Worker-Secret": "wrong"}).status_code == 401
    r = client.get("/optimize/job/xyz", headers={"X-Worker-Secret": SECRET})
    assert r.status_code == 404                                         # authed, no job


def test_worker_secret_check_does_not_500_on_non_ascii_header(monkeypatch):
    """hmac.compare_digest(str, str) raises TypeError on non-ASCII input;
    the check must fail closed (401/403), never 500."""
    _cloud_env(monkeypatch)
    m = _api()
    client = TestClient(m.app)
    r = client.post("/optimize/progress",
                     json={"job_id": "xyz"},
                     headers={"X-Worker-Secret": "£key".encode("latin-1")})
    assert r.status_code in (401, 403)


def test_cloud_round_trip_via_the_worker_endpoints(monkeypatch):
    """The full loop, in-process: dispatch (stubbed) → worker fetches the
    payload → runs the contest with the SAME service functions the real
    worker uses → posts progress + result → the job finishes with the same
    result shape as a local run."""
    _cloud_env(monkeypatch)
    m = _api()
    _seed_book()
    dispatched = {}
    monkeypatch.setattr(m, "_dispatch_workflow",
                        lambda cloud, job_id: dispatched.setdefault("job_id", job_id) or True)

    st = m._start_optimize(budget_evals=15, label="deep", background=True)
    assert st["state"] == "running" and st["mode"] == "cloud"
    t0 = time.time()
    while "job_id" not in dispatched and time.time() - t0 < 10:
        time.sleep(0.02)
    job_id = dispatched["job_id"]

    client = TestClient(m.app)
    h = {"X-Worker-Secret": SECRET}
    job = client.get(f"/optimize/job/{job_id}", headers=h)
    assert job.status_code == 200 and not job.json()["cancel"]
    payload = job.json()["payload"]
    payload["budget_per_candidate"] = 4          # keep the test fast

    out = optimize_service.run_contest(payload, processes=1)
    pr = client.post("/optimize/progress", headers=h,
                     json={"job_id": job_id, "evals": out["evals"],
                           "best": out["best"]})
    assert pr.status_code == 200 and pr.json() == {"cancel": False}
    rr = client.post("/optimize/result", headers=h,
                     json={"job_id": job_id, "winner_overlap": out["winner_overlap"],
                           "ranks": out["ranks"], "best": out["best"],
                           "rows": out["rows"], "evals": out["evals"]})
    assert rr.status_code == 200 and rr.json()["ok"]

    st = _wait(m, "done")
    # `best` is now the winner's ranks replayed LOCALLY (so the panel number == the plan
    # the user gets on Apply), which may differ a hair from the worker's own measurement.
    assert st["mode"] == "cloud" and st["best"]["makespan_days"] is not None
    assert st["best"] == m._metrics_for_ranks(out["ranks"], out["winner_overlap"])
    assert st["best_overlap"] == out["winner_overlap"]
    assert st["baseline"]                        # computed app-side
    meta = m._optimize_apply()                   # Apply works off a cloud result
    assert meta["best_overlap"] == out["winner_overlap"]


def test_dispatch_failure_falls_back_to_local_compute(monkeypatch):
    _cloud_env(monkeypatch)
    m = _api()
    _seed_book()
    monkeypatch.setattr(m, "_dispatch_workflow", lambda cloud, job_id: False)
    m._start_optimize(budget_evals=15, label="deep", background=True)
    st = _wait(m, "done")
    assert st["mode"] == "local" and st["best"]   # computed here, button still works


def test_cloud_timeout_falls_back_to_local_compute(monkeypatch):
    _cloud_env(monkeypatch, timeout_min="0.005")   # ~0.3 s
    m = _api()
    _seed_book()
    monkeypatch.setattr(m, "_dispatch_workflow", lambda cloud, job_id: True)
    m._start_optimize(budget_evals=15, label="deep", background=True)
    st = _wait(m, "done", timeout=30.0)
    assert st["mode"] == "local" and st["best"]


def test_worker_error_report_falls_back_to_local_compute(monkeypatch):
    _cloud_env(monkeypatch)
    m = _api()
    _seed_book()
    dispatched = {}
    monkeypatch.setattr(m, "_dispatch_workflow",
                        lambda cloud, job_id: dispatched.setdefault("job_id", job_id) or True)
    m._start_optimize(budget_evals=15, label="deep", background=True)
    t0 = time.time()
    while "job_id" not in dispatched and time.time() - t0 < 10:
        time.sleep(0.02)
    client = TestClient(m.app)
    r = client.post("/optimize/result", headers={"X-Worker-Secret": SECRET},
                    json={"job_id": dispatched["job_id"], "error": "runner exploded"})
    assert r.status_code == 200 and r.json().get("fallback") == "local"
    st = _wait(m, "done", timeout=30.0)
    assert st["mode"] == "local" and st["best"]


def test_no_cloud_env_means_pure_local_as_before(monkeypatch):
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    monkeypatch.delenv("OPTIMIZE_WORKER_SECRET", raising=False)
    m = _api()
    _seed_book()
    st = m._start_optimize(budget_evals=15, label="deep", background=False)
    assert st["state"] == "done" and st["mode"] == "local"


def test_cloud_budget_env_override(monkeypatch):
    new_cfg = Config(scheduler="new")
    classic_cfg = Config(scheduler="classic")
    monkeypatch.delenv("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", raising=False)
    assert optimize_service.cloud_budget(new_cfg) == 150       # current defaults hold
    assert optimize_service.cloud_budget(classic_cfg) == 400
    monkeypatch.setenv("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", "300")
    assert optimize_service.cloud_budget(new_cfg) == 300       # override, both modes
    assert optimize_service.cloud_budget(classic_cfg) == 300
    monkeypatch.setenv("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", "garbage")
    assert optimize_service.cloud_budget(new_cfg) == 150       # invalid -> default
    monkeypatch.setenv("OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE", "0")
    assert optimize_service.cloud_budget(new_cfg) == 150       # non-positive -> default
