"""Analytics — utilization & bottlenecks derived from a plan (pure; no state/UI).

Every resource is measured against ITS OWN available capacity in the plan window
[min(start), max(end)], so a manual station (≈9.5 h/day) and a two-shift CNC
(≈19.5 h/day) are judged fairly. Machine capacity reuses Rule 6's per-machine
clock, so it matches exactly how the plan was scheduled.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from .models import fmt_date
from .rules.rule6_allocate import _clock_factory

NON_MACHINE_LANES = {"OS / Outsourced", "Off-machine"}
BOTTLENECK_PCT = 85.0
UNDERUSED_PCT = 30.0


def _util(busy_hrs, avail_hrs):
    """Utilization % (Busy/Available), or None when there is no available capacity."""
    return round(busy_hrs / avail_hrs * 100.0, 1) if avail_hrs > 0 else None


def _status(u):
    if u is None:
        return "no capacity"
    if u >= BOTTLENECK_PCT:
        return "bottleneck"
    if u <= UNDERUSED_PCT:
        return "under-used"
    return "healthy"


def _by_util(rows):
    rows.sort(key=lambda r: (r["Utilization %"] is None, -(r["Utilization %"] or 0.0)))
    return rows


def _avg_util(rows):
    vals = [r["Utilization %"] for r in rows if r["Utilization %"] is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _working_days(calendar, start, end):
    d, last, n = start.date(), end.date(), 0
    while d <= last:
        if calendar.is_working_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def _shift_hours(shift, config):
    """Standard hours for an operator's shift (a stated estimate, shown on the tab)."""
    s = (shift or "").lower()
    if "second" in s:                                   # 19:00 -> 05:00 next day
        return float(24 - config.first_shift_end_hour + config.second_shift_end_hour)
    return float(config.first_shift_end_hour - config.first_shift_start_hour)   # first: 08->19


def build_analytics(schedule, masters, config, batches=None):
    """Utilization analytics for one plan. Returns a JSON-able dict with keys:
    ``window``, ``machines``, ``machine_groups``, ``operators``, ``processes``, ``headline``."""
    if not schedule:
        return {"window": None, "machines": [], "machine_groups": [],
                "operators": [], "processes": [], "headline": {}}

    win_start = min(e.start for e in schedule)
    win_end = max(e.end for e in schedule)
    clock_for, _ = _clock_factory(masters, config)

    def disp(mid):
        m = masters.machines.get(mid)
        return m.display_name if m else mid

    def mtype(mid):
        m = masters.machines.get(mid)
        return m.machine_type if (m and m.machine_type) else "Other"

    # --- Machines (exclude non-machine lanes) ---
    by_machine = defaultdict(list)
    for e in schedule:
        if e.machine in NON_MACHINE_LANES:
            continue
        by_machine[e.machine].append(e)

    machines = []
    for mid, ops in by_machine.items():
        busy = sum(e.occupancy_min for e in ops) / 60.0
        avail = clock_for(mid).working_minutes_between(win_start, win_end) / 60.0
        u = _util(busy, avail)
        machines.append({
            "Machine": disp(mid), "Type": mtype(mid),
            "Busy (hrs)": round(busy, 1), "Available (hrs)": round(avail, 1),
            "Utilization %": u, "Idle (hrs)": round(max(avail - busy, 0.0), 1),
            "Ops": len(ops), "Pieces": round(sum(e.qty for e in ops)),
            "Status": _status(u),
        })
    _by_util(machines)

    # --- Group rollup by machine type ---
    groups = defaultdict(lambda: {"busy": 0.0, "avail": 0.0, "machines": 0})
    for mid, ops in by_machine.items():
        g = groups[mtype(mid)]
        g["busy"] += sum(e.occupancy_min for e in ops) / 60.0
        g["avail"] += clock_for(mid).working_minutes_between(win_start, win_end) / 60.0
        g["machines"] += 1
    machine_groups = _by_util([
        {"Type": t, "Machines": v["machines"],
         "Busy (hrs)": round(v["busy"], 1), "Available (hrs)": round(v["avail"], 1),
         "Utilization %": _util(v["busy"], v["avail"]),
         "Status": _status(_util(v["busy"], v["avail"]))}
        for t, v in groups.items()
    ])

    # --- Operators (only when operator logic assigned them) ---
    operators = []
    if getattr(config, "apply_operator_logic", False):
        by_op = defaultdict(list)
        for e in schedule:
            if e.operator:
                by_op[e.operator].append(e)
        wdays = _working_days(masters.calendar, win_start, win_end)
        shift_of = {o.name: o.shift for o in masters.operators}
        for name, ops in by_op.items():
            busy = sum(e.occupancy_min for e in ops) / 60.0
            avail = wdays * _shift_hours(shift_of.get(name, ""), config)
            u = _util(busy, avail)
            operators.append({
                "Operator": name, "Busy (hrs)": round(busy, 1),
                "Available (hrs)": round(avail, 1), "Utilization %": u,
                "Ops": len(ops), "Pieces": round(sum(e.qty for e in ops)), "Status": _status(u),
            })
        _by_util(operators)

    # --- Processes (in-house machine work only; where capacity is spent) ---
    by_proc = defaultdict(list)
    for e in schedule:
        if e.machine in NON_MACHINE_LANES:
            continue
        by_proc[e.process_name].append(e)
    proc_total = sum(e.occupancy_min for ops in by_proc.values() for e in ops) / 60.0
    processes = sorted(
        [{"Process": name, "Work (hrs)": round(sum(e.occupancy_min for e in ops) / 60.0, 1),
          "Share %": (round(sum(e.occupancy_min for e in ops) / 60.0 / proc_total * 100.0, 1)
                      if proc_total else 0.0),
          "Ops": len(ops), "Pieces": round(sum(e.qty for e in ops)),
          "Machines": ", ".join(sorted({disp(e.machine) for e in ops}))}
         for name, ops in by_proc.items()],
        key=lambda r: -r["Work (hrs)"])

    total_busy = round(sum(m["Busy (hrs)"] for m in machines), 1)
    makespan_days = (win_end.date() - win_start.date()).days + 1
    return {
        "window": {"start": fmt_date(win_start), "end": fmt_date(win_end),
                   "makespan_days": makespan_days},
        "machines": machines,
        "machine_groups": machine_groups,
        "operators": operators,
        "processes": processes,
        "headline": {
            "window_start": fmt_date(win_start), "window_end": fmt_date(win_end),
            "makespan_days": makespan_days,
            "total_busy_hrs": total_busy,
            "avg_machine_util": _avg_util(machines),
            "bottleneck": machines[0] if machines else None,
            "underused": [m for m in machines
                          if m["Utilization %"] is not None and m["Utilization %"] <= UNDERUSED_PCT],
        },
    }
