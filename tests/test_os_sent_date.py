"""D12 (owner, 2026-10-06, spec section 8): vendor time counts from the day the parts
were sent. "Sent" is the day the step before the outsourced step was entered as
complete (its full quantity punched good; for a batch of several SO lines the latest
such day, and only when every line has completed it). The outsourced step then
returns at sent + lead time, or at the plan start if that is already past. With no
sent date, an outsourced step takes its full lead time from when it is reached, as
before. Before this, an order at a vendor slipped one day per day until somebody
punched the outsourced step."""
import importlib
import io
from dataclasses import replace
from datetime import date, datetime, time, timedelta

import pytest
from fastapi.testclient import TestClient

from engine import book_store, loaders, orderbook
from engine.config import Config
from engine.loaders import normalize_process_name as _norm
from engine.models import Actual, Order as BookOrder, SOLine
from engine.new_engine import _orders_from_batches
from engine.rules import rule1_consolidate
from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import Machine, MachineKind, Operator, Role, Shift
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.loaders import load_all as new_load
from ppc_engine.scheduler import decode
from tests.new_sample_workbook import _set, build_workbook

MON = date(2025, 3, 3)
START = datetime(2025, 3, 3, 8)
CFG = PlanConfig(plan_start=START, overlap=0.0)
LEAD_MIN = 48 * 60.0


def _shop():
    machines = {m: Machine(id=m, type_text="Manual", kind=MachineKind.MANUAL,
                           available_hrs_per_day=9.5) for m in ("X", "Z")}
    ops = (Operator("Alpha", Role.HELPER, frozenset({"X", "Z"}), Shift.FIRST),)
    routing = Routing("P", "P", (
        Operation(1, "CUT", OperationKind.MANUAL, ("X",), 1.0),
        Operation(2, "PLATING OS", OperationKind.OUTSOURCED, (), LEAD_MIN),
        Operation(3, "FINISH", OperationKind.MANUAL, ("Z",), 1.0),
        Operation(4, "DISPATCH", OperationKind.DISPATCH)))
    return Masters(machines=machines, operators=ops, routings={"P": routing},
                   calendar=ShopCalendar())


def _order(os_sent=None):
    # CUT is done (nothing left), PLATING and FINISH still owe all 60 pieces.
    return Order(so_no="S1", item_code="P", item_name="P", qty=60, due_date=date(2025, 3, 20),
                 process_remaining={1: 0, 2: 60, 3: 60, 4: 60}, os_sent=os_sent)


def _seg(sched, seq):
    return sorted((s for s in sched.segments if s.op_seq == seq), key=lambda s: s.start)


def test_parts_sent_long_ago_are_back_at_plan_start():
    o = _order({2: START - timedelta(days=3)})
    s = decode([o], [o.key], _shop(), CFG)
    assert _seg(s, 2)[-1].end == START
    assert _seg(s, 3)[0].start == START


def test_parts_sent_a_day_ago_return_after_the_rest_of_the_lead_time():
    """The outsourced lane runs round the clock (24x7, wall-clock hours), so 48 h of
    lead time with 24 h already passed returns 24 h after the plan start."""
    o = _order({2: START - timedelta(days=1)})
    s = decode([o], [o.key], _shop(), CFG)
    assert _seg(s, 2)[-1].end == START + timedelta(hours=24)
    assert _seg(s, 3)[0].start == START + timedelta(hours=24)


def test_no_sent_date_is_byte_identical():
    shop = _shop()
    plain = Order(so_no="S1", item_code="P", item_name="P", qty=60, due_date=date(2025, 3, 20),
                  process_remaining={1: 0, 2: 60, 3: 60, 4: 60})
    a = decode([plain], [plain.key], shop, CFG)
    b = decode([_order(None)], [plain.key], shop, CFG)
    c = decode([_order({})], [plain.key], shop, CFG)
    assert a == b == c
    assert _seg(a, 2)[-1].end == START + timedelta(hours=48)


# --------------------------------------------------------------------------- #
# The book: which punch completed the step before, and what a batch gets.
# --------------------------------------------------------------------------- #
ITEM_C, SO_C, SO_D = "NEW-C-01", "NSO-031", "NSO-032"


def _plated_workbook_bytes():
    """The sample workbook plus item C: CNC FIRST SIDE -> PLATING OS (48 h) ->
    INSPECTION -> DISPATCH."""
    wb = build_workbook()
    _set(wb["Item's process Master"], 5, {
        0: 3, 1: "ACME", 2: "PLATED C", 3: ITEM_C, 6: "Dia 10 SS", 10: 10,
        12: "CNC FIRST SIDE", 13: 3, 14: 3, 15: "CNC1", 16: "CNC1",
        17: "PLATING OS", 18: LEAD_MIN, 19: LEAD_MIN, 20: "OS", 21: "OS",
        22: "INSPECTION", 23: 2, 24: 2, 25: "MI1",
        27: "DISPATCH", 28: 0, 29: 0})
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _punch(so, qty, day, process="CNC FIRST SIDE", rejected=0):
    return Actual(so_no=so, item_code=ITEM_C, entry_date=day, qty_produced=qty,
                  qty_rejected=rejected, process=process, operator="Alpha", shift="1st shift")


def _lines(actuals, *sos):
    wb = _plated_workbook_bytes()
    masters = loaders.load_all(io.BytesIO(wb))[1]
    book = {(so, ITEM_C): BookOrder(so, ITEM_C, ITEM_C, 10, date(2025, 3, 20)) for so in sos}
    return orderbook.active_so_lines(book, actuals, masters), wb


def test_the_sent_date_is_the_punch_that_completed_the_step_not_the_first():
    acts = [_punch(SO_C, 4, date(2025, 2, 24)), _punch(SO_C, 3, date(2025, 2, 25)),
            _punch(SO_C, 3, date(2025, 2, 27)), _punch(SO_C, 2, date(2025, 2, 28),
                                                       process="PLATING OS")]
    (line,), _ = _lines(acts, SO_C)
    assert line.process_done_on == {_norm("CNC FIRST SIDE"): date(2025, 2, 27)}


def test_a_later_reject_undoes_the_completion_and_a_remake_dates_it_again():
    acts = [_punch(SO_C, 10, date(2025, 2, 24)), _punch(SO_C, 0, date(2025, 2, 25), rejected=2)]
    (line,), _ = _lines(acts, SO_C)
    assert not line.process_done_on
    (line,), _ = _lines(acts + [_punch(SO_C, 2, date(2025, 2, 26))], SO_C)
    assert line.process_done_on == {_norm("CNC FIRST SIDE"): date(2025, 2, 26)}


def _batch_order(lines, wb):
    nm = new_load(io.BytesIO(wb)).masters
    cfg = Config(scheduler="new", plan_start_date=MON, consolidation_window_days=60)
    (batch,) = rule1_consolidate.run(lines, cfg)
    (order,), _ = _orders_from_batches([batch], nm, first_start=time(8, 0))
    os_seq = next(op.seq for op in nm.routings[ITEM_C].operations
                  if op.kind == OperationKind.OUTSOURCED)
    return batch, order, os_seq


def test_a_batch_is_sent_when_its_last_line_is_sent():
    acts = [_punch(SO_C, 10, date(2025, 2, 24)), _punch(SO_D, 10, date(2025, 2, 26))]
    lines, wb = _lines(acts, SO_C, SO_D)
    batch, order, os_seq = _batch_order(lines, wb)
    assert sorted(batch.source_so_refs) == [SO_C, SO_D]
    assert order.os_sent == {os_seq: datetime(2025, 2, 26, 8)}


def test_a_batch_with_a_line_not_yet_complete_has_no_sent_date():
    acts = [_punch(SO_C, 10, date(2025, 2, 24)), _punch(SO_D, 6, date(2025, 2, 26))]
    lines, wb = _lines(acts, SO_C, SO_D)
    batch, order, _ = _batch_order(lines, wb)
    assert sorted(batch.source_so_refs) == [SO_C, SO_D]
    assert not order.os_sent


# --------------------------------------------------------------------------- #
# Through the API: the return date no longer slips a day per day.
# --------------------------------------------------------------------------- #
@pytest.fixture
def api(monkeypatch):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    import api.main as m
    importlib.reload(m)
    book_store.save_masters_bytes(_plated_workbook_bytes())
    book_store.add_orders([BookOrder(SO_C, ITEM_C, ITEM_C, 10, date(2026, 11, 30))])
    client = TestClient(m.app)
    client.post("/login", data={"username": "anvitech", "password": "1930rail"})
    client.get("/operators")
    return m, client


def _os_return(m):
    m._PLAN_CACHE["key"] = None
    m._plan(m._load_plan_config())
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    (end,) = {e.end for e in sched if e.item_code == ITEM_C and e.process_name == "PLATING OS"}
    return end


def test_an_order_at_the_vendor_keeps_its_return_date_day_after_day(api, monkeypatch):
    m, client = api
    day_n = date(2026, 10, 5)                                    # a Monday
    monkeypatch.setattr(m, "_ist_now", lambda: datetime.combine(day_n, time(7, 0)))
    client.post("/run", json={"persist": False})                 # go-live seeds the plan
    book_store.append_actual(_punch(SO_C, 10, day_n))            # CNC done: parts sent
    returns = []
    for d in (1, 2):
        monkeypatch.setattr(m, "_ist_now",
                            lambda d=d: datetime.combine(day_n + timedelta(days=d), time(7, 0)))
        returns.append(_os_return(m))
    assert returns[0] == returns[1] == datetime.combine(day_n, time(8)) + timedelta(hours=48)
