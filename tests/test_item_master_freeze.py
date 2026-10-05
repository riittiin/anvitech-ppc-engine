"""An in-progress step stays pinned by NAME after its routing is edited.

Routings are now editable (Item Process Master tab), so a step can be removed ahead
of a step that is running on the floor. That shifts the running step's sequence
number. The frozen rows used to be matched by sequence number in two places:

* ``freeze.compute_frozen_set`` looked up the last-applied plan by (item, seq), so the
  next "Done" pinned the running step to whatever step USED to sit at its new seq
  (here: the removed PREWASH's row, MW1 and its helper);
* ``new_engine._ppc_frozen`` trusted the stored row's ``op_seq``, so the very next
  re-plan pinned a DIFFERENT step (INSPECTION) onto the running job's machine.

The fixture follows CLAUDE.md's warning about this family passing vacuously: a SHORT
step (PREWASH, 1 min on MW1, helper Charlie) feeds a LONG one (VMC FIRST SIDE,
10 min on VMC1, operators Alpha/Bravo), each test was run against the unfixed code
and failed, and the edit really does move the running step from seq 2 to seq 1.
"""
from __future__ import annotations

import importlib

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store, freeze, new_engine
from engine.models import Actual, Order
from tests.new_sample_workbook import build_new_sample_bytes, ITEM_A, SO1

RUNNING = "VMC FIRST SIDE"
STEPS = [
    {"name": "PREWASH", "cycle": 1, "allotted": "MW1", "suggested": ""},
    {"name": RUNNING, "cycle": 10, "allotted": "VMC1", "suggested": ""},
    {"name": "INSPECTION", "cycle": 2, "allotted": "MI1", "suggested": ""},
    {"name": "DISPATCH", "cycle": 0, "allotted": "", "suggested": ""},
]


@pytest.fixture
def m(monkeypatch):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    import api.main as mod
    importlib.reload(mod)
    return mod


def _admin(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return c


def _item(c):
    return next(i for i in c.get("/item-master").json()["items"] if i["code"] == ITEM_A)


def _put(c, steps):
    it = _item(c)
    r = c.put("/item-master", json={"code": ITEM_A, "description": it["description"],
                                    "steps": steps, "version": it["version"]})
    assert r.status_code == 200, r.text


def _schedule(m):
    return m._plan_run_for_report(m._load_plan_config())[0].schedule


def _running_frozen_on_floor(m):
    """SO1 (50 pieces of ITEM_A) has 20 through VMC FIRST SIDE, which the applied plan
    ran on VMC1. PREWASH has no punches: the floor punches machining steps only (and
    the punch is written straight to the store, as a legacy punch would be), which is
    exactly what lets an admin remove PREWASH while VMC FIRST SIDE is running."""
    book_store.save_masters_bytes(build_new_sample_bytes())
    book_store.add_orders([Order(SO1, ITEM_A, ITEM_A, 50, m._ist_today())])
    c = _admin(m)
    c.get("/operators")                       # one-time operator seed
    _put(c, STEPS)
    book_store.append_actual(Actual(so_no=SO1, item_code=ITEM_A, process=RUNNING,
                                    entry_date=m._ist_today(), qty_produced=20,
                                    qty_rejected=0, operator="Alpha"))
    book_store.save_last_applied_schedule(freeze.schedule_projection(_schedule(m)))
    frozen = m._compute_and_store_frozen()
    assert [(f["process"], f["op_seq"], f["machine"]) for f in frozen] == \
        [(RUNNING, 2, "VMC1")], "fixture must freeze VMC FIRST SIDE at seq 2 on VMC1"
    return c, frozen[0]


def _assert_pinned_by_name(m, pin):
    entries = [e for e in _schedule(m) if e.item_code == ITEM_A]
    on_pin = [e for e in entries if e.machine == pin["machine"]]
    assert on_pin, "nothing runs on the pinned machine"
    assert {e.process_name for e in on_pin} == {RUNNING}, (
        f"{pin['machine']} runs {sorted({e.process_name for e in on_pin})}; only the "
        f"step in progress there ({RUNNING}) may be pinned to it")
    first = min(on_pin, key=lambda e: e.start)
    assert first.op_segments[0][2] == pin["operator"], \
        "the running step must resume with the operator it was pinned to"
    assert all(e.machine == "MI1" for e in entries if e.process_name == "INSPECTION")
    masters = m._current_masters()
    assert new_engine.routing_order_violations(entries, masters) == []
    nm = new_engine._apply_app_operators(new_engine._new_masters(False), masters)
    assert new_engine.qualification_violations(entries, nm) == []


def test_removing_a_step_ahead_keeps_the_running_step_pinned_on_replan(m):
    """The stored frozen row says op_seq 2. After PREWASH is removed, seq 2 is
    INSPECTION. The next ordinary re-plan must still pin VMC FIRST SIDE."""
    c, pin = _running_frozen_on_floor(m)
    _put(c, STEPS[1:])
    assert [p.name for p in m._current_masters().routings[ITEM_A].processes][:2] == \
        [RUNNING, "INSPECTION"], "the edit must move the running step to seq 1"
    _assert_pinned_by_name(m, pin)


def test_done_after_the_edit_freezes_the_running_step_by_name(m):
    """Done rebuilds the frozen set against the (stale) applied plan. Matching it by
    seq would read PREWASH's row (MW1, Charlie) for VMC FIRST SIDE."""
    c, pin = _running_frozen_on_floor(m)
    _put(c, STEPS[1:])
    frozen = m._compute_and_store_frozen()
    assert [(f["process"], f["op_seq"], f["machine"], f["operator"]) for f in frozen] == \
        [(RUNNING, 1, "VMC1", pin["operator"])]
    _assert_pinned_by_name(m, pin)


def _adapter_fixture():
    import io
    from datetime import date
    from engine import loaders
    from engine.config import Config
    from engine.rules import rule1_consolidate
    from ppc_engine.loaders import load_all as new_load
    conf = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, _masters = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(so_lines, conf)
    orders, batch_by_key = new_engine._orders_from_batches(batches, nm)
    o = next(o for o in orders if o.item_code == ITEM_A)
    so = batch_by_key[o.key].source_so_refs[0]
    ops = nm.routings[ITEM_A].operations          # CNC FIRST SIDE, VMC FIRST SIDE, ...
    row = {"so_no": so, "item_code": ITEM_A, "machine": "VMC1", "operator": "Alpha",
           "remaining_qty": 5, "prev_start": "2025-03-03T08:00:00"}
    return nm, orders, batch_by_key, ops, row, conf.plan_start_date


def test_a_stale_seq_is_re_resolved_by_name():
    nm, orders, bbk, ops, row, d = _adapter_fixture()
    vmc = next(op for op in ops if op.name == RUNNING)
    stale = {**row, "process": RUNNING, "op_seq": vmc.seq + 1}   # INSPECTION's seq
    fos = new_engine._ppc_frozen([stale], orders, bbk, nm, d)
    assert [f.op_seq for f in fos] == [vmc.seq]


def test_a_step_no_longer_in_the_routing_is_not_frozen():
    nm, orders, bbk, ops, row, d = _adapter_fixture()
    gone = {**row, "process": "PREWASH", "op_seq": ops[1].seq}
    assert new_engine._ppc_frozen([gone], orders, bbk, nm, d) == []
