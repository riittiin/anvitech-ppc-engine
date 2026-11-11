"""Profit and Loss: the planned-minutes snapshot written by /run (Task 2)."""
import importlib
from datetime import date, datetime

import pytest
from fastapi.testclient import TestClient

from engine import book_store, profit_loss
from engine.models import Actual, Order
from tests.new_sample_workbook import ITEM_A, ITEM_B, SO1, SO2, build_new_sample_bytes


@pytest.fixture
def admin(monkeypatch):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    import api.main as m
    importlib.reload(m)
    book_store.save_masters_bytes(build_new_sample_bytes())
    book_store.add_orders([Order(SO1, ITEM_A, ITEM_A, 50, date(2025, 3, 20)),
                           Order(SO2, ITEM_B, ITEM_B, 100, date(2025, 3, 21))])
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    c.get("/operators")
    return m, c


def _key(so, item):
    return f"{so}\x1f{item}"


def test_plan_writes_a_snapshot_for_unstarted_orders(admin):
    m, c = admin
    assert c.post("/run", json={"persist": False}).status_code == 200
    snap = book_store.load_pnl_planned()
    assert snap, "no snapshot written"
    for k, v in snap.items():
        assert v["at"] and v["steps"]
        assert all(s["run_min"] > 0 or s["setup_min"] > 0 for s in v["steps"])
    assert _key(SO1, ITEM_A) in snap or _key(SO2, ITEM_B) in snap


def test_snapshot_freezes_at_first_punch(admin, monkeypatch):
    m, c = admin
    c.post("/run", json={"persist": False})
    snap0 = book_store.load_pnl_planned()
    ka, kb = _key(SO1, ITEM_A), _key(SO2, ITEM_B)
    assert ka in snap0 and kb in snap0
    # A punch on order A (changes the plan fingerprint -> a real recompute).
    book_store.append_actual(Actual(so_no=SO1, item_code=ITEM_A, entry_date=date(2025, 3, 10),
                                    qty_produced=1.0, process="CNC FIRST SIDE", operator="x"))
    # Make the fresh plan differ so the freeze is observable.
    real = profit_loss.planned_steps
    marker = [{"process": "Z", "step": "z", "machine": "M", "setup_min": 7, "run_min": 7}]
    monkeypatch.setattr(profit_loss, "planned_steps",
                        lambda *a, **k: {key: marker for key in real(*a, **k)})
    monkeypatch.setattr(m, "_ist_now", lambda: datetime(2030, 1, 1, 0, 0, 0))
    assert c.post("/run", json={"persist": False}).status_code == 200
    snap1 = book_store.load_pnl_planned()
    assert snap1[ka] == snap0[ka]
    assert snap1[kb]["steps"] == marker and snap1[kb]["at"] == "2030-01-01T00:00:00"


def test_plan_response_is_identical_with_the_snapshot_disabled(admin, monkeypatch):
    m, c = admin
    a = c.post("/run", json={"persist": False}).json()
    m._PLAN_CACHE.clear()
    monkeypatch.setattr(m, "_snapshot_pnl", lambda *a, **k: None)
    b = c.post("/run", json={"persist": False}).json()
    a.pop("run_id", None); b.pop("run_id", None)
    assert a == b


def test_a_failing_snapshot_never_breaks_the_plan(admin, monkeypatch):
    m, c = admin
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(profit_loss, "planned_steps", boom)
    r = c.post("/run", json={"persist": False})
    assert r.status_code == 200 and r.json()["gantt"]


# ---- machine rates (task 5) ----
def _put(c, m_view, **over):
    body = {"id": m_view["id"], "type": m_view["type"], "hours": m_view["hours"],
            "version": m_view["version"]}
    body.update(over)
    return c.put("/machines", json=body)


def _machine(c, mid="CNC1"):
    return next(x for x in c.get("/machines").json()["machines"] if x["id"] == mid)


def test_save_without_rate_keeps_the_stored_rate(admin):
    m, c = admin
    v = _machine(c)
    assert v["rate"] == 450
    r = _put(c, v, hours=9.5)
    assert r.status_code == 200, r.text
    assert _machine(c)["rate"] == 450


def test_rate_zero_or_negative_is_rejected(admin):
    m, c = admin
    v = _machine(c)
    for bad in (0, -5):
        assert _put(c, v, rate=bad).status_code == 400
    assert _machine(c)["rate"] == 450


def test_rate_blank_clears_it(admin):
    m, c = admin
    v = _machine(c)
    assert _put(c, v, rate=None).status_code == 200
    assert "rate" not in _machine(c) or _machine(c)["rate"] is None


def test_default_rates_seed_once_and_never_overwrite(admin):
    m, c = admin
    assert _put(c, _machine(c), rate=500).status_code == 200
    assert _machine(c)["rate"] == 500
    assert _machine(c)["rate"] == 500                     # reload: still 500
    assert _machine(c, "VMC1")["rate"] == 500


def test_user_role_never_sees_rates(admin):
    m, c = admin
    u = TestClient(m.app)
    u.post("/login", data={"username": "anvitech_user", "password": "anvitech12345678"})
    r = u.get("/machines")
    assert r.status_code == 200 and r.json()["machines"]
    assert "rate" not in r.text


def test_rate_has_no_plan_effect(admin):
    m, c = admin
    c.get("/machines")
    cfg = m._load_plan_config()
    fp = m._plan_fingerprint(cfg)
    dg = m.shop_masters.machines_digest(book_store.load_machines_doc())
    assert _put(c, _machine(c), rate=777).status_code == 200
    assert m.shop_masters.machines_digest(book_store.load_machines_doc()) == dg
    assert m._plan_fingerprint(cfg) == fp


def test_rates_from_doc():
    doc = {"machines": {"A": {"rate": 5}, "B": {}, "C": {"rate": None}}}
    assert profit_loss.rates_from_doc(doc) == {"A": 5.0}
