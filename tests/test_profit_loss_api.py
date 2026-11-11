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


# ---- GET /profit-loss (task 6) ----
@pytest.fixture
def user(admin):
    m, _ = admin
    u = TestClient(m.app)
    u.post("/login", data={"username": "anvitech_user", "password": "anvitech12345678"})
    return u


def _punch(c, so, item, process, machine, qty, minutes, day="2025-03-10", complete=False):
    r = c.post("/actuals", json={
        "so_no": so, "item_code": item, "entry_date": day, "shift": "1st shift",
        "process": process, "operator": "Alpha", "qty_produced": qty,
        "machine": machine, "shift_minutes": minutes, "mark_complete": complete})
    assert r.status_code == 200, r.text
    return r


def _steps(snapshot_doc):
    return snapshot_doc["steps"]


def test_profit_loss_is_admin_only(user):
    assert user.get("/profit-loss").status_code == 403


def _finish_order_a(c, minutes_by_process):
    """Punch every counted step of order A (50 pcs) on its planned machine."""
    snap = book_store.load_pnl_planned()[_key(SO1, ITEM_A)]
    done = []
    for s in snap["steps"]:
        if s["process"] in done:
            continue
        done.append(s["process"])
    return snap, done


def test_end_to_end_row(admin):
    m, c = admin
    c.post("/run", json={"persist": False})
    snap = book_store.load_pnl_planned()[_key(SO1, ITEM_A)]
    procs = list(dict.fromkeys(s["process"] for s in snap["steps"]))
    assert procs, "order A has no counted steps"
    planned_min = sum(s["setup_min"] + s["run_min"] for s in snap["steps"])
    cnc, vmc = "CNC1", "VMC1"
    mach_of = {s["process"]: s["machine"] for s in snap["steps"]}
    from engine.models import Actual as _A
    # Punch each routing step in order so the precedence guard accepts it.
    routing = m._current_masters().routings[ITEM_A].processes
    actual_total = 0.0
    last = len(routing) - 1
    for i, p in enumerate(routing):
        name = p.name
        mach = mach_of.get(name)
        if mach:
            mins = 60.0
            actual_total += mins
        else:
            mach, mins = "", 0.0
        _punch(c, SO1, ITEM_A, name, mach, 50, mins, complete=(i == last))
    rep = c.get("/profit-loss").json()
    assert len(rep["rows"]) == 1, rep
    row = rep["rows"][0]
    assert row["so_no"] == SO1 and row["completed_on"]
    assert row["planned_hours"] == pytest.approx(planned_min / 60, abs=0.01)
    assert row["actual_hours"] == pytest.approx(actual_total / 60, abs=0.01)
    assert row["not_punched"] == []
    # MI1 has no default rate: priced "rate not set" and kept out of the totals.
    assert row["rates_missing"] == ["MI1"] and row["planned_cost"] is None
    assert rep["totals"]["orders"] == 1 and rep["totals"]["incomplete"] == 1 and rep["totals"]["counted"] == 0
    # Once the rate is set the row prices by hand: hours x rate per machine.
    assert _put(c, _machine(c, "MI1"), rate=300).status_code == 200
    row = c.get("/profit-loss").json()["rows"][0]
    rate = {"CNC1": 450.0, "VMC1": 500.0, "MI1": 300.0}
    by_step = {s["process"]: s for s in snap["steps"]}
    exp_planned = sum((s["setup_min"] + s["run_min"]) / 60 * rate[s["machine"]] for s in snap["steps"])
    exp_actual = sum(rate[mach_of[n]] * 1.0 for n in mach_of)
    assert row["planned_cost"] == pytest.approx(exp_planned, abs=0.05)
    assert row["actual_cost"] == pytest.approx(exp_actual, abs=0.05)
    assert row["diff_cost"] == pytest.approx(exp_actual - exp_planned, abs=0.05)
    assert row["rates_missing"] == []
    assert rep["older_count"] == 0


def test_rollback_of_completion_removes_the_row(admin):
    m, c = admin
    c.post("/run", json={"persist": False})
    snap = book_store.load_pnl_planned()[_key(SO1, ITEM_A)]
    mach_of = {s["process"]: s["machine"] for s in snap["steps"]}
    routing = m._current_masters().routings[ITEM_A].processes
    ids = []
    for i, p in enumerate(routing):
        name = p.name
        r = _punch(c, SO1, ITEM_A, name, mach_of.get(name, ""), 50,
                   60.0 if mach_of.get(name) else 0.0, complete=(i == len(routing) - 1))
    assert len(c.get("/profit-loss").json()["rows"]) == 1
    last_id = r.json()["actuals_ids"][-1]
    rb = c.post("/actuals/rollback", json={"id": last_id})
    assert rb.status_code == 200, rb.text
    assert c.get("/profit-loss").json()["rows"] == []


def test_already_started_order_never_appears(admin):
    m, c = admin
    # Punch BEFORE the first /run with the feature: the order is already started.
    routing = m._current_masters().routings[ITEM_B].processes
    for i, p in enumerate(routing):
        n = p.name
        _punch(c, SO2, ITEM_B, n, "", 100, 0.0, complete=(i == len(routing) - 1))
    c.post("/run", json={"persist": False})
    rep = c.get("/profit-loss").json()
    assert rep["rows"] == [] and rep["older_count"] == 1
