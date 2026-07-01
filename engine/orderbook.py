"""Order-book logic — pure functions over orders + actuals (no storage/IO here).

This is the stateful layer's brain: it decides how an upload merges into the book,
derives each order's status, and produces the active SO-lines (with remaining qty)
that feed the unchanged Rules 1-6. Keeping it pure makes every rule here testable
in isolation; persistence lives in ``book_store``.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from .models import Order, SOLine, fmt_date
from .loaders import normalize_process_name

PENDING = "Pending"
RUNNING = "Running"
COMPLETE = "Complete"

DISPATCH = "DISPATCH"   # the "consider it done / shipped" finished-goods gate


_norm = normalize_process_name   # shared canonical process-name key (see loaders)


def finished_gate(routing) -> str:
    """The process whose good qty actually fulfils the order (finished goods).

    A piece is finished only when it clears the **DISPATCH** step ('consider it
    done / shipped'). A routing without a DISPATCH step (e.g. one ending in
    PACKING) uses its **last** step instead. Returns the gate's process name as
    written in the routing, or '' if the routing has no processes.

    Earlier (intermediate) steps are work-in-progress: recorded for output and
    downtime, but they do NOT reduce the order — that was the WIP-counted-as-
    finished bug this guards against."""
    if routing is None or not routing.processes:
        return ""
    for p in routing.processes:
        if _norm(p.name) == DISPATCH:
            return p.name
    return routing.processes[-1].name


def produced_good_by_so(actuals) -> dict:
    """Sum net good qty (produced − rejected, summed then clamped ≥ 0) per SO across
    ALL processes — the total output, including work-in-progress. NOT order
    fulfilment; use ``finished_good_by_so`` for remaining-qty / completion logic."""
    good = defaultdict(float)
    for a in actuals:
        good[a.so_no] += (a.qty_produced - a.qty_rejected)
    return {k: max(v, 0.0) for k, v in good.items()}


def finished_good_by_so(actuals, masters) -> dict:
    """Good qty that actually fulfils each order = good produced at the item's
    **finished-goods gate** (DISPATCH, or the last step if there is no DISPATCH).
    Intermediate-process production is ignored here (it is WIP, not finished).

    Matching is normalized (case/space-insensitive). An item with no routing has
    no gate to check, so its good qty is counted as-is (best effort — such items
    are not schedulable anyway)."""
    routings = masters.routings if masters else {}
    good = defaultdict(float)
    for a in actuals:
        routing = routings.get(a.item_code)
        if routing is None:
            good[a.so_no] += (a.qty_produced - a.qty_rejected)   # no recipe to gate on
            continue
        if _norm(a.process) == _norm(finished_gate(routing)):
            good[a.so_no] += (a.qty_produced - a.qty_rejected)
    return {k: max(v, 0.0) for k, v in good.items()}   # net rejections, clamp ≥ 0


def completed_by_process(actuals) -> dict:
    """Good qty completed per (SO number, normalized process) across all entries —
    the per-step progress the floor punches in. Drives 'continue from reality'
    re-planning: each process is re-scheduled at ordered − its completed qty.

    Rejections are netted across ALL entries then clamped at ≥ 0 (NOT per entry) —
    so 100 produced then 20 rejected later nets 80 done, and the 20 rejects stay to
    be redone. (Per-entry clamping would have over-counted and shipped shortages.)"""
    done = defaultdict(float)
    for a in actuals:
        done[(a.so_no, _norm(a.process))] += (a.qty_produced - a.qty_rejected)
    return {k: max(v, 0.0) for k, v in done.items()}


def latest_actual_date(actuals):
    """The most recent date any production was punched, or None if no actuals."""
    return max((a.entry_date for a in actuals), default=None)


def actuals_on_latest_date(actuals) -> list:
    """Only the entries on the latest punched date. The Capture-Actuals 'Saved
    entries' list shows just these (and only these are rollback-able), so the list
    stays one day long instead of growing without bound; earlier days are locked but
    remain in the record + the per-item rollup."""
    d = latest_actual_date(actuals)
    return [a for a in actuals if a.entry_date == d] if d is not None else []


def effective_plan_start_date(actuals, config_start_date, calendar):
    """The date the plan should start from.

    Normally the configured start (``config_start_date``). Once production has been
    punched, it advances to the **next working day after the latest actual's date** —
    that day's work is done and over, so the re-plan continues from the next day's
    first shift instead of restarting from the original date (fixing the bug where
    completed days were 'forgotten' and the remaining work was squeezed too early).
    Never moves earlier than the configured start (an old actual can't drag it back).
    Non-working days (weekly off / holidays) are skipped."""
    latest = latest_actual_date(actuals)
    if latest is None:
        return config_start_date
    nxt = latest + timedelta(days=1)
    while not calendar.is_working_day(nxt):
        nxt += timedelta(days=1)
    return max(config_start_date, nxt)


def so_nos_with_actuals(actuals) -> set:
    """SO numbers that have at least one recorded actual — i.e. work has started.
    Drives the Running status independently of how much is *finished*."""
    return {a.so_no for a in actuals}


def derive_status(order: Order, so_with_actuals: set) -> str:
    """Status is derived, never stored (except the explicit ``completed`` flag).
    An order is Running once it has ANY actual (work started) — even if every
    piece is still mid-routing — and only Complete when the user ticks it."""
    if order.completed:
        return COMPLETE
    return RUNNING if order.so_no in so_with_actuals else PENDING


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


def active_so_lines(active_orders: dict, actuals, masters=None) -> list:
    """SO-lines to plan. Each non-completed order is emitted with:

    * ``qty`` = ordered − **finished** good (good at the DISPATCH/last-step gate) —
      the order's headline remaining; WIP does not reduce it; and
    * ``process_qty`` = {process -> ordered − done at THAT step} so Rule 6 re-plans
      each process at its own remaining ("continue from reality"). Only set when the
      order has recorded progress — otherwise None, so a fresh plan is byte-identical
      to today.

    Orders with nothing left to finish (remaining <= 0) are skipped."""
    good = finished_good_by_so(actuals, masters)
    done = completed_by_process(actuals)
    routings = masters.routings if masters else {}
    started = so_nos_with_actuals(actuals)
    lines = []
    for o in active_orders.values():
        if o.completed:
            continue
        remaining = max(o.ordered_qty - good.get(o.so_no, 0.0), 0.0)
        if remaining <= 0:
            continue
        pq = None
        routing = routings.get(o.item_code)
        if o.so_no in started and routing is not None:
            pq = {_norm(p.name): max(o.ordered_qty - done.get((o.so_no, _norm(p.name)), 0.0), 0.0)
                  for p in routing.processes}
        lines.append(SOLine(
            so_no=o.so_no, item_code=o.item_code, item_name=o.item_name,
            qty=remaining, delivery_date=o.delivery_date, process_qty=pq,
        ))
    return lines


def process_progress_rows(active_orders: dict, actuals, masters=None) -> list:
    """Per-(order, process) progress so the floor can see reality: how many pieces
    have cleared each step and how many remain (ordered − done). One row per process
    for each active order that has started (has at least one actual). This is the
    correct per-step WIP view — not a sum of good across steps, which double-counts."""
    done = completed_by_process(actuals)
    routings = masters.routings if masters else {}
    started = so_nos_with_actuals(actuals)
    rows = []
    for o in active_orders.values():
        if o.completed or o.so_no not in started:
            continue
        routing = routings.get(o.item_code)
        if routing is None:
            continue
        for p in routing.processes:
            c = done.get((o.so_no, _norm(p.name)), 0.0)
            rows.append({
                "SO No": o.so_no,
                "Item Code": o.item_code,
                "Seq": p.seq,
                "Process": p.name,
                "Completed": c,
                "Remaining": max(o.ordered_qty - c, 0.0),
            })
    return rows


def order_rows(active_orders: dict, completed_orders: dict, actuals, masters=None) -> list:
    """Rows for the Orders dashboard (active first by delivery date, completed last).

    'Finished (good)' / 'Remaining' count only good at the finished-goods gate
    (DISPATCH / last step). Per-process WIP is NOT shown here — it can't be derived
    by summing good across processes (the same pieces flow through every step, so
    that double-counts); proper per-process progress is tracked separately."""
    finished = finished_good_by_so(actuals, masters)
    started = so_nos_with_actuals(actuals)

    def row(o: Order, status: str):
        done = finished.get(o.so_no, 0.0)
        remaining = max(o.ordered_qty - done, 0.0)
        return {
            "SO No": o.so_no,
            "Item Code": o.item_code,
            "Item Name": o.item_name,
            "Ordered": o.ordered_qty,
            "Finished (good)": done,
            "Remaining": remaining,
            "SO Delivery Date": fmt_date(o.delivery_date),
            "Status": status,
            "Note": "ready to complete" if (status == RUNNING and remaining <= 0) else "",
        }

    # Sort by the real delivery date (not the DD-MM-YYYY display string, which
    # wouldn't sort chronologically). Active first, then completed.
    items = ([(o, derive_status(o, started)) for o in active_orders.values()]
             + [(o, COMPLETE) for o in completed_orders.values()])
    items.sort(key=lambda t: (t[1] == COMPLETE, t[0].delivery_date, t[0].so_no))
    return [row(o, status) for o, status in items]
