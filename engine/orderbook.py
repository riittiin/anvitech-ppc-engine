"""Order-book logic — pure functions over orders + actuals (no storage/IO here).

This is the stateful layer's brain: it decides how an upload merges into the book,
derives each order's status, and produces the active SO-lines (with remaining qty)
that feed the unchanged Rules 1-6. Keeping it pure makes every rule here testable
in isolation; persistence lives in ``book_store``.
"""
from __future__ import annotations

from collections import defaultdict

from .models import Order, SOLine, fmt_date
from .loaders import normalize_resource_id

PENDING = "Pending"
RUNNING = "Running"
COMPLETE = "Complete"


def produced_good_by_so(actuals) -> dict:
    """Sum good qty (produced − rejected) per SO number from actuals."""
    good = defaultdict(float)
    for a in actuals:
        good[a.so_no] += a.good_qty()
    return good


def derive_status(order: Order, good_by_so: dict) -> str:
    """Status is derived, never stored (except the explicit ``completed`` flag)."""
    if order.completed:
        return COMPLETE
    return RUNNING if good_by_so.get(order.so_no, 0.0) > 0 else PENDING


def merge_upload(so_lines, active_orders: dict, completed_orders: dict, first_seen: str = ""):
    """Merge uploaded SO lines into the book by SO number. Pure — returns
    ``(new_orders, flags)`` and does not mutate the inputs.

    * unseen SO# -> a new Pending order
    * SO# already active -> flagged (changed vs identical), original untouched
    * SO# in the completed archive -> flagged "already completed", not re-added
    """
    new_orders, flags = [], []
    seen_in_upload = set()
    for so in so_lines:
        sn = so.so_no
        if sn in seen_in_upload:
            flags.append({"so_no": sn, "reason": "duplicate SO# within this upload"})
            continue
        if sn in active_orders:
            ex = active_orders[sn]
            changed = (ex.ordered_qty != so.qty
                       or ex.delivery_date != so.delivery_date
                       or ex.item_code != so.item_code)
            flags.append({
                "so_no": sn,
                "reason": "changed — original kept (revisions deferred)" if changed
                          else "duplicate — already in the book",
            })
        elif sn in completed_orders:
            flags.append({"so_no": sn, "reason": "already completed — not re-added"})
        else:
            seen_in_upload.add(sn)
            new_orders.append(Order(
                so_no=sn, item_code=so.item_code, item_name=so.item_name,
                ordered_qty=so.qty, delivery_date=so.delivery_date,
                completed=False, first_seen=first_seen,
            ))
    return new_orders, flags


def active_so_lines(active_orders: dict, actuals) -> list:
    """SO-lines to plan: each non-completed order with qty = ordered − good
    produced, excluding any with nothing left to make (remaining <= 0)."""
    good = produced_good_by_so(actuals)
    lines = []
    for o in active_orders.values():
        if o.completed:
            continue
        remaining = max(o.ordered_qty - good.get(o.so_no, 0.0), 0.0)
        if remaining <= 0:
            continue
        lines.append(SOLine(
            so_no=o.so_no, item_code=o.item_code, item_name=o.item_name,
            qty=remaining, delivery_date=o.delivery_date,
        ))
    return lines


def machine_lost_minutes(actuals, masters, planned_setup_min):
    """Lost machine time recorded in actuals, attributed per machine — for feeding
    back into Rule 6 as availability delays. Pure (no storage/IO).

    For each actual: ``lost = setup overrun (actual − planned, if positive) + total
    downtime``. The machine is resolved from ``(item_code, process)`` via the routing
    using the SAME id Rule 6 schedules on (suggested → allotted → process name,
    normalized), so the keys match the scheduler's ``machine_free`` keys. Returns
    ``(lost_by_machine, unattributed)``; ``unattributed`` lists entries whose
    process/routing could not be matched (free-text typos, missing routing) so the
    loss is surfaced rather than silently dropped or raised on."""
    lost = defaultdict(float)
    unattributed = []
    for a in actuals:
        loss = max((a.actual_setup_min or 0.0) - planned_setup_min, 0.0) + a.total_downtime_min()
        if loss <= 0:
            continue
        routing = masters.routings.get(a.item_code)
        proc = None
        if routing is not None:
            want = (a.process or "").strip()
            proc = next((p for p in routing.processes if p.name.strip() == want), None)
        if proc is None:
            unattributed.append({"so_no": a.so_no, "item_code": a.item_code,
                                 "process": a.process, "lost": loss})
            continue
        mid = normalize_resource_id(proc.suggested_machine or proc.allotted_machine or proc.name)
        lost[mid] += loss
    return dict(lost), unattributed


def order_rows(active_orders: dict, completed_orders: dict, actuals) -> list:
    """Rows for the Orders dashboard (active first by delivery date, completed last)."""
    good = produced_good_by_so(actuals)

    def row(o: Order, status: str):
        prod = good.get(o.so_no, 0.0)
        remaining = max(o.ordered_qty - prod, 0.0)
        return {
            "SO No": o.so_no,
            "Item Code": o.item_code,
            "Item Name": o.item_name,
            "Ordered": o.ordered_qty,
            "Produced (good)": prod,
            "Remaining": remaining,
            "SO Delivery Date": fmt_date(o.delivery_date),
            "Status": status,
            "Note": "ready to complete" if (status == RUNNING and remaining <= 0) else "",
        }

    # Sort by the real delivery date (not the DD-MM-YYYY display string, which
    # wouldn't sort chronologically). Active first, then completed.
    items = ([(o, derive_status(o, good)) for o in active_orders.values()]
             + [(o, COMPLETE) for o in completed_orders.values()])
    items.sort(key=lambda t: (t[1] == COMPLETE, t[0].delivery_date, t[0].so_no))
    return [row(o, status) for o, status in items]
