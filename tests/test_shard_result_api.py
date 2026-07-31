"""POST /optimize/shard-result: worker-secret auth, accumulation, and
finalize-when-all-arrived == a single whole-contest run_contest winner."""
from datetime import date

import io
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine.loaders import load_all
from engine.config import Config
from engine.models import Order
from engine import optimize_service as osvc
from tests.new_sample_workbook import build_new_sample_bytes


def _payload(*, budget=6, candidates=(60, 80)):
    """A cloud payload on the fully-staffed new-engine sample workbook (mirrors
    tests/test_optimize_shard.py::_new_engine_payload) so a real contest run
    has routings/masters/operators to schedule against under scheduler=='new'."""
    raw = build_new_sample_bytes()
    so_lines, _masters = load_all(io.BytesIO(raw))
    orders = {}
    for sl in so_lines:
        o = Order(sl.so_no, sl.item_code, sl.item_name, sl.qty, sl.delivery_date)
        orders[o.key] = o
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
                apply_operator_logic=True, overlap_percent=candidates[0])
    cfg.validate()
    return osvc.build_payload(orders, [], raw, cfg, seed=1,
                              candidates=list(candidates), budget_per_candidate=budget)


def _seed_running(m, payload, job_id="job-1"):
    """Put _OPTIMIZE into a running cloud job the collector will accept."""
    cfg = Config.from_dict(payload["config"])
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(state="running", job_id=job_id, cloud_payload=payload,
                           base_config=cfg, baseline=None, label="deep",
                           cancel=False, cloud_failed=False, claimed=False,
                           shards={}, shard_total=None, evals=0, best=None,
                           started_mono=0.0)


def test_shard_result_requires_worker_secret(monkeypatch):
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    c = TestClient(m.app)
    r = c.post("/optimize/shard-result", json={"job_id": "x", "shard_index": 0,
                                               "shard_total": 2, "rows": []})
    assert r.status_code in (401, 403)  # no secret header → rejected


def test_all_shards_finalize_matches_run_contest(monkeypatch):
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    full = osvc.run_contest(payload, processes=1)     # the reference winner
    _seed_running(m, payload)
    c = TestClient(m.app)
    hdr = {"X-Worker-Secret": "s3cr3t"}
    SHARD_TOTAL = 3
    for idx in range(SHARD_TOTAL):
        out = osvc.run_contest_slice(payload, idx, SHARD_TOTAL, processes=1)
        r = c.post("/optimize/shard-result", headers=hdr, json={
            "job_id": "job-1", "shard_index": idx, "shard_total": SHARD_TOTAL,
            "rows": out["rows"], "evals": out["evals"], "cancelled": out["cancelled"]})
        assert r.status_code == 200
    # After the last shard the job finalized to the same winner run_contest found.
    assert m._OPTIMIZE["state"] == "done"
    res = m._OPTIMIZE["result"]
    assert res is not None
    # _finalize_optimize stores the winning knob value under "best_overlap"
    # and the winning machine-set under "flexible_machines" (confirmed by
    # reading _finalize_optimize's `result={...}` assignment) — not
    # "overlap"/"flexible" as an earlier sketch of this test assumed.
    assert res["best_overlap"] == full["winner_overlap"]
    assert res.get("flexible_machines") == full["winner_flexible"]


def test_stale_shard_is_noop(monkeypatch):
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3cr3t")
    import importlib, api.main as m
    importlib.reload(m)
    payload = _payload()
    _seed_running(m, payload, job_id="job-1")
    c = TestClient(m.app)
    r = c.post("/optimize/shard-result", headers={"X-Worker-Secret": "s3cr3t"},
               json={"job_id": "OTHER", "shard_index": 0, "shard_total": 2, "rows": []})
    assert r.status_code == 200        # ignored, never crashes
    assert m._OPTIMIZE["state"] == "running"
