"""load_all — the loaders' public entry point.

Reads a workbook into a Masters + Orders + DataReport, then works out which orders are
actually schedulable (routing present, complete, and every in-house step has a staffed
machine). Unschedulable orders are recorded with a reason and excluded — the schedule
is built only from what can genuinely run (fail-localized, RULES.md).
"""

from __future__ import annotations

from dataclasses import dataclass

from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import ROLE_FOR_KIND
from ppc_engine.domain.routing import OperationKind
from ppc_engine.loaders.masters_loader import (
    calendar_from_holiday_rows,
    load_calendar,
    load_machines,
    load_operators,
    load_routings,
    machines_from_rows,
    register_provisional_machines,
    routings_from_rows,
)
from ppc_engine.loaders.report import DataReport
from ppc_engine.loaders.sales_orders import load_orders
from ppc_engine.loaders.workbook import open_workbook


@dataclass
class LoadResult:
    """Everything a load produces."""

    masters: Masters
    orders: list[Order]
    report: DataReport

    def schedulable_orders(self) -> list[Order]:
        """Orders that can actually be scheduled (not blocked in the report)."""
        blocked = self.report.blocked_orders
        return [o for o in self.orders if o.key not in blocked]


def _staffed_machines(masters: Masters) -> set[str]:
    """Machine ids somebody in Settings is assigned to.

    Role is NOT a gate here either (2026-08-07, same fix as
    ``scheduler.staffing.build_machine_pools``) — and the blast radius of getting it
    wrong is larger: an "unstaffed" machine's orders are BLOCKED as unschedulable, so a
    machine covered only by a role-mismatched person silently took its whole order book
    out of the plan."""
    staffed: set[str] = set()
    for mid in masters.machines:
        if any(mid in op.qualified_machines for op in masters.operators):
            staffed.add(mid)
    return staffed


def _block_unschedulable(masters: Masters, orders: list[Order], report: DataReport) -> None:
    """Record, per order, whether it can be scheduled and why not."""
    from ppc_engine.loaders.report import GapKind

    staffed = _staffed_machines(masters)
    routing_gap_items = {g.ref for g in report.gaps if g.kind == GapKind.ROUTING_GAP}

    for order in orders:
        routing = masters.routings.get(order.item_code)
        if routing is None:
            report.add(GapKind.NO_ROUTING, order.item_code, f"order {order.so_no} has no routing for its item")
            report.block_order(order.key, "no routing for item")
            continue
        if order.item_code in routing_gap_items:
            report.block_order(order.key, "routing has a step with no machine")
            continue
        # Every in-house step must have at least one staffed machine option.
        for op in routing.operations:
            if op.kind in (OperationKind.MACHINING, OperationKind.MANUAL, OperationKind.INSPECTION):
                if not any(m in staffed for m in op.machine_options):
                    report.block_order(
                        order.key,
                        f"step '{op.name}' has no staffed machine ({list(op.machine_options)})",
                    )
                    break


def load_all(path, flexible_machines: bool = False, routing_rows=None,
             machine_rows=None, holiday_rows=None) -> LoadResult:
    """Load a workbook at ``path`` into a LoadResult.

    ``flexible_machines`` (see load_routings): False = machining ops locked to their
    Allotted machine; True = ops may use any machine in their Suggested set (the
    machine-flexibility lever, OPTIMIZATION.md).

    ``routing_rows`` (``[(code, description, steps)]``): when given, routings come
    from these rows (the app's Item Process Master) and the workbook's routing
    sheet is not read. None = read the sheet, as before.

    ``machine_rows`` / ``holiday_rows`` (the app's Machines and holiday tables):
    when routing, machine AND holiday rows are all given, the masters come from them
    alone and no workbook is opened (``path`` may be None; operators come from the
    app overlay, orders from the book). Given partly, the rest is read from the
    workbook, which then must be supplied.
    """
    tables_only = None not in (routing_rows, machine_rows, holiday_rows)
    report = DataReport()
    if tables_only:
        wb = None
    elif path is None:
        raise ValueError("load_all: a workbook is required unless routing, machine "
                         "and holiday rows are all given")
    else:
        wb = open_workbook(path)

    machines = machines_from_rows(machine_rows) if machine_rows is not None else load_machines(wb)
    operators = () if wb is None else load_operators(wb)
    calendar = (calendar_from_holiday_rows(holiday_rows) if holiday_rows is not None
                else load_calendar(wb))
    if routing_rows is None:
        routings = load_routings(wb, report, flexible_machines=flexible_machines)
    else:
        routings = routings_from_rows(routing_rows, report, flexible_machines=flexible_machines)
    register_provisional_machines(machines, routings, report)

    masters = Masters(machines=machines, operators=operators, routings=routings, calendar=calendar)
    orders = [] if wb is None else load_orders(wb)
    _block_unschedulable(masters, orders, report)

    return LoadResult(masters=masters, orders=orders, report=report)
