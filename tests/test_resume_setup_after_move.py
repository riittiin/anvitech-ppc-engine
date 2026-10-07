"""A half-finished machining job that an Optimize moved to another machine pays its
setup again when it resumes there (Task 9 Part C, the 2026-08-31 rule: a released
pin pays its 90-minute setup, because the new machine has to be set up for it).

Before this, Apply rebuilt the frozen set from the NEW published plan, so the moved
job was frozen on its new machine and resumed there as if it had been running on it
all along: no setup, while the free plan the search scored had paid one.

The signal is the previous published plan's machine (the frozen set on file when the
new plan is published). The machine named on a punch would be the other signal, but
it is optional on Daily Entry and on the live store only 56 of 1,130 punches carry
one. The setup stays owed until the step is punched again (then it has been set up
and run on the new machine)."""
from datetime import date, datetime, timedelta

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import Machine, MachineKind, Operator, Role, Shift
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.scheduler import decode
from ppc_engine.scheduler.schedule import FrozenOp

from engine import book_store, freeze
from engine.models import Actual
from tests.new_sample_workbook import ITEM_B, SO2
from tests.test_fixed_plan_api import api  # noqa: F401  (fixture)
from tests.test_fixed_plan_final_fixes import _half_finished_job_on_a_down_machine, _plan_sig

START = datetime(2025, 3, 3, 8)
CFG = PlanConfig(plan_start=START, overlap=0.0)


def _shop(kind):
    m = Machine(id="M1", type_text="CNC" if kind == OperationKind.MACHINING else "Manual",
                kind=MachineKind.MACHINING if kind == OperationKind.MACHINING else MachineKind.MANUAL,
                available_hrs_per_day=21.0)
    op = Operator("Alpha", Role.OPERATOR if kind == OperationKind.MACHINING else Role.HELPER,
                  frozenset({"M1"}), Shift.FIRST)
    r = Routing("P", "P", (Operation(1, "STEP", kind, ("M1",), 1.0),
                           Operation(2, "DISPATCH", OperationKind.DISPATCH)))
    return Masters(machines={"M1": m}, operators=(op,), routings={"P": r},
                   calendar=ShopCalendar())


def _minutes(kind, setup):
    o = Order(so_no="S", item_code="P", item_name="P", qty=100, due_date=date(2025, 3, 20),
              process_remaining={1: 60, 2: 100})
    fo = FrozenOp(o.key, 1, "M1", "Alpha", 60, START, setup=setup)
    s = decode([o], [o.key], _shop(kind), CFG, frozen=[fo])
    return sum((g.end - g.start).total_seconds() / 60 for g in s.segments if g.op_seq == 1)


def test_a_moved_machining_job_pays_its_setup_on_resume():
    assert _minutes(OperationKind.MACHINING, False) == 60
    assert _minutes(OperationKind.MACHINING, True) == 60 + CFG.setup_min


def test_a_moved_manual_job_has_no_setup_to_pay():
    assert _minutes(OperationKind.MANUAL, True) == 60


# --------------------------------------------------------------------------- #
# Which frozen rows owe it: the freeze layer.
# --------------------------------------------------------------------------- #
class _P:
    def __init__(self, name):
        self.name, self.seq = name, 1


class _R:
    processes = [_P("CNC FIRST SIDE")]


class _M:
    routings = {"I": _R()}


class _L:
    from engine.loaders import normalize_process_name as _n
    so_no, item_code, process_qty = "S1", "I", {_n("CNC FIRST SIDE"): 40}


def _row(machine, **kw):
    return {"batch_id": "B001", "item_code": "I", "process_seq": 1,
            "process_name": "CNC FIRST SIDE", "machine": machine, "operator": "",
            "start": "2025-03-03T08:00:00", "end": "2025-03-03T12:00:00",
            "so_refs": ["S1"], **kw}


def _frozen(rows, good):
    from engine.loaders import normalize_process_name as n
    return freeze.compute_frozen_set(rows, [_L()], {("S1", "I", n("CNC FIRST SIDE")): good},
                                     _M())


def test_a_step_moved_since_it_was_last_worked_owes_a_setup_until_it_is_punched_again():
    old = [{"so_no": "S1", "item_code": "I", "process": "CNC FIRST SIDE", "op_seq": 1,
            "machine": "CNC1", "operator": "", "remaining_qty": 40,
            "prev_start": "2025-03-03T08:00:00", "prev_end": "2025-03-03T12:00:00"}]
    from engine.loaders import normalize_process_name as n
    good = {("S1", "I", n("CNC FIRST SIDE")): 60}
    rows = freeze.mark_moved([_row("CNC2")], old, good)
    assert [f.get("setup_from") for f in _frozen(rows, 60)] == ["CNC1"]
    assert [f.get("setup_from") for f in _frozen(rows, 70)] == [None]    # punched again
    # Published again before it was worked: still owed (carried from the frozen set).
    again = freeze.mark_moved([_row("CNC2")], _frozen(rows, 60), good)
    assert [f.get("setup_from") for f in _frozen(again, 60)] == ["CNC1"]
    # Moved back to where it was set up: nothing owed.
    back = freeze.mark_moved([_row("CNC1")], _frozen(rows, 60), good)
    assert [f.get("setup_from") for f in _frozen(back, 60)] == [None]
    # Not moved at all: nothing owed.
    same = freeze.mark_moved([_row("CNC1")], old, good)
    assert [f.get("setup_from") for f in _frozen(same, 60)] == [None]


# --------------------------------------------------------------------------- #
# Through Apply: the job the search moved resumes with its setup.
# --------------------------------------------------------------------------- #
def _resume_minutes(m, machine):
    m._PLAN_CACHE["key"] = None
    m._plan(m._load_plan_config())
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    return sum((en - s).total_seconds() / 60
               for e in sched if e.process_name == "CNC SECOND SIDE" and SO2 in e.so_refs
               and e.machine == machine
               for s, en, _ in (e.op_segments or [(e.start, e.end, "")]))


def test_after_apply_the_moved_job_resumes_with_its_setup(api):
    m, client = api
    down = _half_finished_job_on_a_down_machine(m, client)
    m._start_optimize(budget_evals=15, label="quick", background=False)
    m._optimize_apply()
    (fr,) = [f for f in book_store.load_frozen_ops() if f["process"] == "CNC SECOND SIDE"]
    assert fr["machine"] != down and fr.get("setup_from") == down
    cfg = m._resolve_config(m._load_plan_config())
    with_setup = _resume_minutes(m, fr["machine"])
    # Punched again on the new machine: it is set up now, so no more setup.
    book_store.append_actual(Actual(so_no=SO2, item_code=ITEM_B, entry_date=m._ist_today(),
                                    qty_produced=1, process="CNC SECOND SIDE",
                                    operator="Alpha", shift="1st shift"))
    client.post("/optimize/done")
    (fr2,) = [f for f in book_store.load_frozen_ops() if f["process"] == "CNC SECOND SIDE"]
    assert fr2.get("setup_from") is None
    without = _resume_minutes(m, fr2["machine"])
    one_piece = (with_setup - cfg.setup_time_min - without)
    assert 0 < one_piece < 10, (with_setup, without)


def test_the_next_step_waits_for_the_setup_before_overlapping():
    """With overlap the next step may start once part of this one is cut, but no piece
    is cut before the new machine is set up, so it cannot start inside the setup."""
    m1 = Machine(id="M1", type_text="CNC", kind=MachineKind.MACHINING, available_hrs_per_day=21.0)
    m2 = Machine(id="M2", type_text="Manual", kind=MachineKind.MANUAL, available_hrs_per_day=9.5)
    ops = (Operator("Alpha", Role.OPERATOR, frozenset({"M1"}), Shift.FIRST),
           Operator("Bravo", Role.HELPER, frozenset({"M2"}), Shift.FIRST))
    r = Routing("P", "P", (Operation(1, "CNC", OperationKind.MACHINING, ("M1",), 1.0),
                           Operation(2, "DEBURR", OperationKind.MANUAL, ("M2",), 1.0),
                           Operation(3, "DISPATCH", OperationKind.DISPATCH)))
    shop = Masters(machines={"M1": m1, "M2": m2}, operators=ops, routings={"P": r},
                   calendar=ShopCalendar())
    cfg = PlanConfig(plan_start=START, overlap=0.5)
    o = Order(so_no="S", item_code="P", item_name="P", qty=100, due_date=date(2025, 3, 20),
              process_remaining={1: 60, 2: 100, 3: 100})
    s = decode([o], [o.key], shop, cfg, frozen=[FrozenOp(o.key, 1, "M1", "Alpha", 60, START,
                                                          setup=True)])
    first = min(g.start for g in s.segments if g.op_seq == 1)
    nxt = min(g.start for g in s.segments if g.op_seq == 2)
    assert nxt >= first + timedelta(minutes=cfg.setup_min), (first, nxt)
