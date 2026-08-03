"""Demand-aware 'bottleneck' operator policy (Approach 2, Level 2)."""
from datetime import date, datetime

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import Machine, MachineKind, Operator, Role, Shift
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.scheduler.staffing import (StaffingBoard, build_machine_pools,
                                           machine_demand)


def _mach(mid):
    return Machine(id=mid, type_text="CNC lathe", kind=MachineKind.MACHINING,
                   available_hrs_per_day=19.5)


def _cfg(**kw):
    # week_anchor=None => no rotation => operators stay on their base_shift.
    return PlanConfig(plan_start=datetime(2025, 3, 5, 8, 0), week_anchor=None,
                      setup_min=90.0, **kw)


def test_machine_demand_is_expected_share_over_options():
    masters = Masters(
        machines={"CNC1": _mach("CNC1"), "CNC2": _mach("CNC2")},
        routings={
            # op1 can run on CNC1 or CNC2 (2 options); machining => 90 + qty*cycle.
            "IT": Routing("IT", "", operations=(
                Operation(1, "CNC", OperationKind.MACHINING,
                          machine_options=("CNC1", "CNC2"), cycle_min=2.0),
                Operation(2, "DISPATCH", OperationKind.DISPATCH),  # no options -> ignored
            )),
        },
    )
    orders = [Order("SO1", "IT", "item", qty=10, due_date=date(2025, 4, 1))]
    d = machine_demand(orders, masters, _cfg())
    # duration = 90 + 10*2 = 110 min, split across 2 options => 55 each.
    assert d == {"CNC1": 55.0, "CNC2": 55.0}


def test_machine_demand_skips_os_and_dispatch_and_missing_routing():
    masters = Masters(
        machines={"CNC1": _mach("CNC1")},
        routings={"IT": Routing("IT", "", operations=(
            Operation(1, "BANDSAW OS", OperationKind.OUTSOURCED, machine_options=(), cycle_min=240.0),
            Operation(2, "DISPATCH", OperationKind.DISPATCH),
        ))},
    )
    orders = [
        Order("SO1", "IT", "item", qty=5, due_date=date(2025, 4, 1)),
        Order("SO2", "GHOST", "no-routing", qty=5, due_date=date(2025, 4, 1)),  # skipped
    ]
    assert machine_demand(orders, masters, _cfg()) == {}


def _board_and_masters(op_specs, machine_ids, demand):
    """op_specs: list of (name, frozenset(quals)). All OPERATOR role, FIRST shift."""
    machines = {mid: _mach(mid) for mid in machine_ids}
    operators = tuple(
        Operator(name=n, role=Role.OPERATOR, qualified_machines=frozenset(q),
                 base_shift=Shift.FIRST)
        for n, q in op_specs)
    masters = Masters(machines=machines, operators=operators, calendar=ShopCalendar())
    board = StaffingBoard(build_machine_pools(masters), demand)
    return board, masters


def _pick(board, masters, machine_id, cfg):
    day = date(2025, 3, 5)
    start = datetime(2025, 3, 5, 8, 0)
    end = datetime(2025, 3, 5, 12, 0)
    return board.candidate_operator(masters.machines[machine_id], day, Shift.FIRST,
                                    start, end, masters, cfg)


# Anil is MORE flexible (3 quiet machines); Bimal is LESS flexible (2) but is the SOLE
# operator for the busy machine CNC2. scarce wrongly burns Bimal on CNC1; bottleneck
# keeps Bimal free for CNC2 and puts Anil on CNC1.
_GRIND_OPS = [("Anil", {"CNC1", "CNC3", "CNC4"}), ("Bimal", {"CNC1", "CNC2"})]
_GRIND_MACHINES = ["CNC1", "CNC2", "CNC3", "CNC4"]


def test_scarce_burns_the_bottleneck_specialist():
    board, masters = _board_and_masters(_GRIND_OPS, _GRIND_MACHINES, {"CNC2": 1000.0})
    assert _pick(board, masters, "CNC1", _cfg(operator_pick="scarce")) == "Bimal"


def test_bottleneck_keeps_the_specialist_free():
    board, masters = _board_and_masters(_GRIND_OPS, _GRIND_MACHINES, {"CNC2": 1000.0})
    assert _pick(board, masters, "CNC1", _cfg(operator_pick="bottleneck")) == "Anil"


def test_bottleneck_with_empty_demand_equals_scarce():
    board, masters = _board_and_masters(_GRIND_OPS, _GRIND_MACHINES, {})
    assert (_pick(board, masters, "CNC1", _cfg(operator_pick="bottleneck"))
            == _pick(board, masters, "CNC1", _cfg(operator_pick="scarce")))


def test_bottleneck_single_candidate_unchanged():
    board, masters = _board_and_masters([("Solo", {"CNC1"})], ["CNC1"], {"CNC1": 500.0})
    assert _pick(board, masters, "CNC1", _cfg(operator_pick="bottleneck")) == "Solo"


def test_bottleneck_strand_discount_when_others_cover():
    # A THIRD operator also covers CNC2, so pulling Bimal onto CNC1 no longer strands
    # CNC2 -> Bimal's elsewhere-cost drops, and (being less flexible) he wins again.
    ops = _GRIND_OPS + [("Chetan", {"CNC2"})]
    board, masters = _board_and_masters(ops, _GRIND_MACHINES, {"CNC2": 1000.0})
    assert _pick(board, masters, "CNC1", _cfg(operator_pick="bottleneck")) == "Bimal"


from dataclasses import replace as _replace

from engine.config import Config
from engine.new_engine import _plan_config
from ppc_engine.scheduler import decode


def _routing(item, seq_machine):
    ops = tuple(Operation(i + 1, f"OP{i+1}", OperationKind.MACHINING,
                          machine_options=(m,), cycle_min=2.0)
                for i, m in enumerate(seq_machine))
    ops = ops + (Operation(len(ops) + 1, "DISPATCH", OperationKind.DISPATCH),)
    return Routing(item, "", operations=ops)


def _cnc(mid):
    return Machine(id=mid, type_text="CNC lathe", kind=MachineKind.MACHINING,
                   available_hrs_per_day=19.5)


def test_bottleneck_changes_the_decoded_plan_end_to_end():
    """Through _plan_config + decode (demand computed inside decode): scarce puts the
    less-flexible specialist Bimal on the routine machine CNC1 (stranding the busy CNC2);
    bottleneck puts Anil there and keeps Bimal for CNC2."""
    masters = Masters(
        machines={m: _cnc(m) for m in ("CNC1", "CNC2", "CNC3", "CNC4")},
        operators=(
            Operator("Anil", Role.OPERATOR, frozenset({"CNC1", "CNC3", "CNC4"}), Shift.FIRST),
            Operator("Bimal", Role.OPERATOR, frozenset({"CNC1", "CNC2"}), Shift.FIRST),
        ),
        routings={
            "ROUTINE": _routing("ROUTINE", ["CNC1"]),
            "BUSY": _routing("BUSY", ["CNC2"]),
        },
    )
    orders = [
        Order("R", "ROUTINE", "routine", qty=10, due_date=date(2025, 4, 1)),
        Order("B", "BUSY", "busy", qty=400, due_date=date(2025, 4, 1)),  # big -> CNC2 hot
    ]
    seq = [("R", "ROUTINE"), ("B", "BUSY")]
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 5), apply_operator_logic=True)

    def cnc1_operator(pick):
        sched = decode(orders, seq, masters, _plan_config(_replace(cfg, operator_pick=pick)))
        segs = [s for s in sched.segments if s.machine_id == "CNC1" and s.operator]
        return segs[0].operator

    assert cnc1_operator("scarce") == "Bimal"
    assert cnc1_operator("bottleneck") == "Anil"
