"""Phase 1 trigger: admin actions start a contest immediately; punches never do
(the Done button does); cloud-only; mid-run changes chain a follow-up; the
AUTO_OPTIMIZE=0 env (internal, tests only) disables everything."""
import time
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient
from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


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


def _auto_env(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "manual")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3")


def test_admin_bump_starts_contest_immediately(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    starts = []
    monkeypatch.setattr(m, "_start_optimize",
                        lambda budget_evals, label, background=True, auto=False:
                        starts.append((label, auto)))
    m._bump_book_changed()
    assert starts == [("auto", True)]


def test_done_button_any_role_starts_contest(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech_user",
                           "password": "anvitech12345678"})
    r = c.post("/optimize/done")
    assert r.status_code == 200 and starts


def test_mid_run_change_sets_pending_and_chains(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["state"] = "running"          # simulate a running contest
    m._bump_book_changed()
    assert m._AUTO["pending"] is True
    with m._OPTIMIZE_LOCK:                        # let it finish
        m._OPTIMIZE["state"] = "idle"
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    m._drain_pending_auto()
    assert starts and m._AUTO["pending"] is False


def test_auto_is_cloud_only(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN")
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    m._bump_book_changed()
    assert starts == []
    assert "retry" in (book_store.load_auto_note() or {}).get("text", "")


def test_internal_env_disables_everything(monkeypatch):
    m = _api(); _seed_book()                      # AUTO_OPTIMIZE=0 via fixture
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    m._bump_book_changed()
    assert starts == []


def test_no_fire_when_signature_matches_applied(monkeypatch):
    _auto_env(monkeypatch)
    m = _api(); _seed_book()
    sig = m._current_book_sig()
    book_store.save_plan_priority({}, {"saved_at": "t", "book_sig": sig})
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    m._bump_book_changed()
    assert starts == []


def test_auto_contest_applies_only_when_strictly_better(monkeypatch):
    _auto_env(monkeypatch)
    # "manual" dispatch never calls a real worker; force a fast fallback to
    # local compute so this real contest finishes inside the test's patience.
    monkeypatch.setenv("OPTIMIZE_CLOUD_TIMEOUT_MIN", "0.01")
    m = _api()
    _seed_book()
    m._bump_book_changed()                              # real contest, sample book
    t0 = time.time()
    while m._optimize_status()["state"] == "running" and time.time() - t0 < 60:
        time.sleep(0.05)
    # The auto-apply hook runs just after the contest lands in "done" — and,
    # when it applies, the hook itself moves state on to "idle" again — so
    # give the note/priority writes a brief grace window before asserting.
    note = book_store.load_auto_note()
    t0 = time.time()
    while note is None and time.time() - t0 < 5:
        time.sleep(0.05)
        note = book_store.load_auto_note()
    assert note is not None
    saved = book_store.load_plan_priority()
    if saved:                                          # applied ⇒ strictly better + meta
        assert "auto" in note["text"].lower() or "re-optimized" in note["text"]
        assert saved["meta"]["book_sig"] == m._current_book_sig()
    else:                                               # not applied ⇒ honest note
        assert "still best" in note["text"]


def test_manual_apply_also_records_book_sig(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    m._start_optimize(budget_evals=15, label="deep", background=False)
    m._optimize_apply()
    assert book_store.load_plan_priority()["meta"]["book_sig"] == m._current_book_sig()


# --------------------------------------------------------------------------- #
# Task 5: wiring the bumps + surfacing the auto note + the clerk's Done button
# --------------------------------------------------------------------------- #

def test_mutating_endpoints_bump_and_run_returns_note(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    bumps = []
    monkeypatch.setattr(m, "_bump_book_changed", lambda: bumps.append(1))
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    c.post("/actuals", json={"so_no": "SO1", "item_code": ITEM_A,
                             "entry_date": "2025-03-05", "qty_produced": 1,
                             "process": "", "shift": "A"})
    assert not bumps                                   # punches do NOT bump
    c.post("/orders/commit", json={"orders": [["SO2", ITEM_B]]})
    assert bumps                                       # admin action bumped
    book_store.save_auto_note({"text": "hello", "at": "t"})
    r = c.post("/run", json={})
    assert r.json()["auto_note"]["text"] == "hello"


def test_rollback_does_not_bump(monkeypatch):
    """The /actuals/rollback punch endpoint must not bump either."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    r = c.post("/actuals", json={"so_no": "SO1", "item_code": ITEM_A,
                                "entry_date": "2025-03-05", "qty_produced": 1,
                                "process": "", "shift": "A"})
    entry_id = r.json()["actuals_ids"][0]
    bumps = []
    monkeypatch.setattr(m, "_bump_book_changed", lambda: bumps.append(1))
    c.post("/actuals/rollback", json={"id": entry_id})
    assert not bumps


def test_all_deliberate_admin_mutations_bump(monkeypatch):
    """upload, delete, clear, commit, uncommit, urgent, and the persist=True
    branch of /run each call _bump_book_changed() — every other admin
    mutation the brief lists besides the one already covered above."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    bumps = []
    monkeypatch.setattr(m, "_bump_book_changed", lambda: bumps.append(1))
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})

    bumps.clear()
    c.post("/orders/urgent", json={"so": "SO1", "item": ITEM_A, "confirm": True})
    assert bumps, "orders/urgent did not bump"

    bumps.clear()
    c.post("/orders/uncommit", json={"orders": [["SO1", ITEM_A]]})
    assert bumps, "orders/uncommit did not bump"

    bumps.clear()
    r = c.post("/run", json={"config": {}, "persist": True})
    assert r.status_code == 200
    assert bumps, "/run persist=True did not bump"

    bumps.clear()
    r = c.post("/run", json={"config": {}, "persist": False})
    assert r.status_code == 200
    assert not bumps, "/run persist=False must NOT bump"

    from tests.sample_workbook import build_sample_bytes
    bumps.clear()
    r = c.post("/upload", files={"file": ("sample.xlsx", build_sample_bytes(),
               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
    assert r.status_code == 200
    assert bumps, "/upload did not bump"

    bumps.clear()
    r = c.post("/orders/delete", json={"orders": [["SO1", ITEM_A]], "password": "1930rail"})
    assert r.status_code == 200
    assert bumps, "/orders/delete did not bump"

    bumps.clear()
    r = c.post("/orders/clear", json={"password": "1930rail"})
    assert r.status_code == 200
    assert bumps, "/orders/clear did not bump"


def test_optimize_status_carries_auto_field(monkeypatch):
    """/optimize/status must expose whether the current/last run was an
    auto-triggered contest, so the UI can suppress a stale Apply panel."""
    m = _api()
    _seed_book()
    st = m._optimize_status()
    assert "auto" in st
    assert st["auto"] is False
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["auto"] = True
    assert m._optimize_status()["auto"] is True


def test_auto_apply_keeps_current_when_no_plan(monkeypatch):
    """When the contest finishes with best=None (e.g. cancelled/empty),
    _auto_apply_result writes a 'kept the current plan' note and leaves
    plan_priority unchanged."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    # Save a known plan priority
    saved_ranks = {"k": 1}
    book_store.save_plan_priority(saved_ranks, {"saved_at": "t"})
    # Stub the contest result: best=None
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["result"] = {"best": None}
        m._OPTIMIZE["auto"] = True
    # Call _auto_apply_result
    m._auto_apply_result()
    # Check: note reflects "no plan"
    note = book_store.load_auto_note()
    assert note is not None
    assert "no plan" in note["text"]
    # Check: plan_priority unchanged
    loaded_ranks = book_store.load_plan_priority()
    assert loaded_ranks is not None
    assert loaded_ranks["ranks"] == saved_ranks
