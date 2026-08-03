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
