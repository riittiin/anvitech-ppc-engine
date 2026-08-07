"""Delay justification — pure, post-hoc reconstruction of WHY each order is delayed,
from the FINISHED plan (no scheduler state, no I/O). For every (SO No, Item Code) order,
time from the plan start to the order's completion is partitioned into RUNNING and
WAITING intervals, and every wait is attributed to a concrete cause:

  * machine busy   — the machine the next step needs is occupied by other orders
                     (each blocking order named; '(higher priority)' when it ranks ahead)
  * off-hours      — that machine is outside its working window (night / weekly off / holiday)
  * crew           — machine free within working hours, but no qualified operator was free

RUNNING + all WAIT == the order's whole span, so every hour is accounted for. See
docs/superpowers/specs/2026-07-28-delay-justification-report-design.md.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from .operator_coverage import eligible_window
from .optimizer import expected_completion
from .worktime import WorkClock

_OFF_LANES = {"OS / Outsourced", "Off-machine"}


def _hours(a, b):
    return (b - a).total_seconds() / 3600.0


def _merge(intervals):
    """Merge overlapping/adjacent (start, end) intervals."""
    out = []
    for s, e in sorted(intervals):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _gaps(start, end, busy):
    """Complement of merged `busy` intervals within [start, end]."""
    gaps, cur = [], start
    for s, e in _merge(busy):
        if s > cur:
            gaps.append((cur, min(s, end)))
        cur = max(cur, e)
    if cur < end:
        gaps.append((cur, end))
    return [(a, b) for a, b in gaps if b > a]


def _order_ops(schedule, so, item):
    ops = [e for e in schedule if e.item_code == item and so in (e.so_refs or [])
           and e.machine not in _OFF_LANES and e.end > e.start]
    return sorted(ops, key=lambda e: e.start)


def _rank_by_key(batches_prioritized):
    """Priority position of each (SO, item): lower index = higher priority."""
    rank = {}
    for i, b in enumerate(batches_prioritized or []):
        for so in (b.source_so_refs or []):
            rank[(so, b.item_code)] = i
    return rank


def _next_machine(ops, gap_end):
    """The machine the order's NEXT operation (starting at/after the gap) needs."""
    nxt = min((e for e in ops if e.start >= gap_end), key=lambda e: e.start, default=None)
    return nxt.machine if nxt else (ops[-1].machine if ops else "")


def _machine_busy(a, b, machine, schedule, this_rank, rank):
    """Rows for every OTHER op occupying `machine` during [a, b] (each blocker named),
    plus the machine-free remainder of [a, b]."""
    others = [e for e in schedule if e.machine == machine and not (e.end <= a or e.start >= b)]
    others.sort(key=lambda e: e.start)
    rows, occupied = [], []
    for e in others:
        os, oe = max(e.start, a), min(e.end, b)
        if oe <= os:
            continue
        occupied.append((os, oe))
        bso = (e.so_refs or [""])[0]
        hp = rank.get((bso, e.item_code), 10 ** 9) < this_rank
        why = (f"{machine} busy with {bso} / {e.item_code} — {e.process_name}"
               + (" (higher priority)" if hp else ""))
        rows.append({"State": "WAITING (machine busy)", "Process": "", "Machine": machine,
                     "Operator": "", "From": os, "To": oe, "Hours": round(_hours(os, oe), 2),
                     "Why": why})
    free = _gaps(a, b, occupied)
    return rows, free


def _classify_free(a, b, clock):
    """Split a machine-free interval into crew (inside the machine's working window) and
    off-hours (outside it) sub-intervals."""
    # Start a day early: a two-shift machine's night window (e.g. 19:00→05:00) belongs to
    # the PREVIOUS day but overflows into this one, so it can cover the early morning of `a`.
    work, d = [], a.date() - timedelta(days=1)
    while datetime.combine(d, datetime.min.time()) < b:
        for ws, we in clock._windows_for_day(d):
            s, e = max(ws, a), min(we, b)
            if e > s:
                work.append((s, e))
        d = d + timedelta(days=1)
    work = _merge(work)
    rows = []
    for s, e in work:
        rows.append({"State": "WAITING (crew)", "Process": "", "Machine": "", "Operator": "",
                     "From": s, "To": e, "Hours": round(_hours(s, e), 2),
                     "Why": "Machine free — waiting for a free qualified operator"})
    for s, e in _gaps(a, b, work):
        rows.append({"State": "WAITING (off-hours)", "Process": "", "Machine": "", "Operator": "",
                     "From": s, "To": e, "Hours": round(_hours(s, e), 2),
                     "Why": "Outside working hours (night / weekly off / holiday)"})
    return rows


def _why_summary(days_late, buckets):
    if days_late <= 0:
        return "On time."
    parts = []
    if buckets["machine"] > 0:
        parts.append(f"{buckets['machine']}d machines busy (higher-priority orders)")
    if buckets["off"] > 0:
        parts.append(f"{buckets['off']}d off-hours")
    if buckets["crew"] > 0:
        parts.append(f"{buckets['crew']}d waiting for operators")
    return f"{days_late} days late — " + ", ".join(parts) if parts else f"{days_late} days late"


def build_delay_report(schedule, so_lines, batches_prioritized, config, masters):
    """See module docstring. Returns {'summary': [row], 'detail': [row]}."""
    plan_start = datetime.combine(config.plan_start_date, datetime.min.time())
    rank = _rank_by_key(batches_prioritized)
    clock_cache = {}

    def clock_for(mid):
        if mid not in clock_cache:
            mac = masters.machines.get(mid)
            iv = eligible_window(mac, config) if mac is not None else []
            clock_cache[mid] = WorkClock(masters.calendar, iv)
        return clock_cache[mid]

    # THE shared completion definition (engine/optimizer.expected_completion) — over
    # ALL of an order's entries, OS/dispatch lanes included. The wait analysis below
    # still runs over real machine ops only (there is no machine to wait for on an
    # off-lane), but the DATE this report publishes must be the same date the Gantt
    # and the Orders tab publish, or the same order reads two ways (live 2026-08-07).
    completion_by_key = expected_completion(schedule)

    detail, summary = [], []
    for line in so_lines:
        so, item = line.so_no, line.item_code
        ops = _order_ops(schedule, so, item)
        if not ops:
            continue
        running = _merge([(e.start, e.end) for e in ops])
        completion = max(e.end for e in ops)
        this_rank = rank.get((so, item), 10 ** 9)
        rows = []
        for e in ops:
            rows.append({"State": "RUNNING", "Process": f"{e.process_seq}. {e.process_name}",
                         "Machine": e.machine, "Operator": e.operator_label(),
                         "From": e.start, "To": e.end, "Hours": round(_hours(e.start, e.end), 2),
                         "Why": ""})
        for (a, b) in _gaps(plan_start, completion, running):
            machine = _next_machine(ops, b)
            busy, free = _machine_busy(a, b, machine, schedule, this_rank, rank)
            rows.extend(busy)
            for (fa, fb) in free:
                rows.extend(_classify_free(fa, fb, clock_for(machine)))
        rows.sort(key=lambda r: r["From"])
        for r in rows:
            r["SO No"], r["Item Code"] = so, item

        # 'Working' = MERGED running wall-clock (an order's ops can run concurrently —
        # parallel split / overlap — so summing each RUNNING row would double-count).
        # Waits are the complement of the merged running, so work + waits == span exactly.
        buckets = {"machine": 0.0, "off": 0.0, "crew": 0.0,
                   "work": sum(_hours(s, e) for s, e in running)}
        for r in rows:
            if r["State"] == "WAITING (machine busy)":
                buckets["machine"] += r["Hours"]
            elif r["State"] == "WAITING (off-hours)":
                buckets["off"] += r["Hours"]
            elif r["State"] == "WAITING (crew)":
                buckets["crew"] += r["Hours"]
        days = {k: round(v / 24.0, 1) for k, v in buckets.items()}
        completion_date = completion_by_key.get((so, item), completion.date())
        days_late = (completion_date - line.delivery_date).days
        summary.append({
            "SO No": so, "Item Code": item, "Item Name": line.item_name,
            "Ordered Qty": int(line.qty), "SO Delivery Date": line.delivery_date,
            "Expected Completion": completion_date, "Days Late": days_late,
            "Working (days)": days["work"], "Waiting: machine (days)": days["machine"],
            "Waiting: off-hours (days)": days["off"], "Waiting: crew (days)": days["crew"],
            "Why": _why_summary(days_late, days)})
        detail.extend(rows)

    summary.sort(key=lambda s: -s["Days Late"])
    pos = {(s["SO No"], s["Item Code"]): i for i, s in enumerate(summary)}
    detail.sort(key=lambda r: (pos[(r["SO No"], r["Item Code"])], r["From"]))
    # normalize detail column order
    cols = ["SO No", "Item Code", "State", "Process", "Machine", "Operator",
            "From", "To", "Hours", "Why"]
    detail = [{c: r[c] for c in cols} for r in detail]
    return {"summary": summary, "detail": detail}
