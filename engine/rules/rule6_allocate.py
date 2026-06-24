"""Rule 6 — Allocate each process to the earliest-available preferred machine.

This is a **non-delay scheduler**: machines never sit idle while there is work
they could be doing. At every step it looks at the next ready operation of every
batch and starts the one that can begin earliest on its machine. Ties are broken
by the priority Rules 1–3 produced (earlier delivery, then more process time).

Why non-delay matters: the whole point of a PPC engine is keeping machines
running. The moment a machine finishes a batch's operation, the next operation
that needs it (from any batch whose predecessor is already done) takes it — the
machine does not wait for a higher-priority batch whose operation isn't ready
yet. Priority only decides between operations that are *equally* ready.

Each operation respects:
  * the working calendar (Thursdays off, holidays) and shifts — via WorkClock,
  * machine availability (one operation at a time per machine),
  * Rule 4 (setup time) for occupancy,
  * Rule 5 (overlap mode) for when the next process of the same batch may start.

Pure function: ``run(prioritized_batches, config, notes, masters) -> list[ScheduleEntry]``.
Raises RuleError on a contract violation (e.g. a batch with no routing).
"""
from __future__ import annotations

from datetime import datetime

from ..models import ScheduleEntry
from ..worktime import WorkClock
from ..loaders import normalize_resource_id
from . import rule4_setup_time as r4
from . import rule5_overlap_mode as r5


def _resolve_resource(proc):
    """Canonical resource id for a process: suggested, else allotted, else a
    generic station named after the process itself."""
    raw = proc.suggested_machine or proc.allotted_machine or proc.name
    return normalize_resource_id(raw)


def run(batches, config=None, notes=None, masters=None, machine_lost_min=None, **kw):
    # Imported lazily to avoid a circular import (pipeline imports this module).
    from ..pipeline import RuleError

    notes = notes if notes is not None else []
    if masters is None:
        raise RuleError("rule6", "-", "masters are required to allocate")

    clock = WorkClock(masters.calendar, config)
    plan_start = datetime(
        config.plan_start_date.year,
        config.plan_start_date.month,
        config.plan_start_date.day,
        config.first_shift_start_hour,
    )

    # One state record per batch. ``ready`` is the earliest its NEXT process may
    # start (precedence constraint from the previous process); ``next`` indexes
    # the next unscheduled process.
    states = []
    for prio, batch in enumerate(batches):
        routing = masters.routings.get(batch.item_code)
        if routing is None:
            raise RuleError(
                "rule6", batch.item_code,
                "batch has no routing — should have been skipped at load (NO_ROUTING)",
            )
        if batch.qty is None or batch.qty < 0:
            raise RuleError("rule6", batch.batch_id, f"invalid batch qty {batch.qty}")
        states.append({
            "batch": batch,
            "prio": prio,                 # lower = higher priority (Rules 1–3 order)
            "routing": routing,
            "next": 0,
            "ready": plan_start,
        })

    total_ops = sum(len(s["routing"].processes) for s in states)
    machine_free: dict[str, datetime] = {}
    schedule: list[ScheduleEntry] = []

    # Downtime loop-back: a machine that lost time in the actuals is treated as
    # unavailable for that many WORKING minutes from the plan start, so its whole
    # queue slips later (non-delay scheduling then handles the rest).
    if machine_lost_min:
        seeded = []
        for mid, mins in machine_lost_min.items():
            if mins and mins > 0:
                machine_free[mid] = clock.advance(plan_start, mins)
                seeded.append(f"{mid} +{round(mins)} min")
        if seeded:
            notes.append(
                "Downtime loop-back: seeded recorded lost time into machine "
                "availability — " + ", ".join(sorted(seeded)) + "."
            )

    scheduled = 0
    guard = 0
    while scheduled < total_ops and guard <= total_ops + 5:
        guard += 1
        best = None  # (sort_key, state, proc, resource, occ, note)
        for s in states:
            if s["next"] >= len(s["routing"].processes):
                continue
            proc = s["routing"].processes[s["next"]]
            resource = _resolve_resource(proc)
            note = ""

            occ = r4.occupancy_minutes(proc.cycle_time, s["batch"].qty, config)
            machine_ready = machine_free.get(resource, plan_start)
            feasible = clock.advance(max(s["ready"], machine_ready), 0)

            # Non-delay: earliest feasible start wins; priority breaks ties.
            key = (feasible, s["prio"], s["batch"].batch_id, proc.seq)
            if best is None or key < best[0]:
                best = (key, s, proc, resource, occ, note)

        if best is None:
            break
        _, s, proc, resource, occ, note = best
        start = best[0][0]
        end = clock.advance(start, occ)
        machine_free[resource] = end
        schedule.append(ScheduleEntry(
            batch_id=s["batch"].batch_id, item_code=s["batch"].item_code,
            process_seq=proc.seq, process_name=proc.name, machine=resource,
            qty=s["batch"].qty, occupancy_min=occ, start=start, end=end, notes=note,
        ))

        # Advance this batch and set when its next process may start (Rule 5).
        # Overlap measures the previous process's CUTTING time only (setup
        # excluded); a no-cutting step does not overlap.
        s["next"] += 1
        if s["next"] < len(s["routing"].processes):
            run_min = (proc.cycle_time or 0.0) * (s["batch"].qty or 0.0)
            elapsed = r5.elapsed_before_next(occ, run_min, config)
            s["ready"] = clock.advance(start, elapsed)
        scheduled += 1

    # Decision notes: prove machines ran continuously.
    _timeline, summary = build_machine_view(schedule, masters, config)
    total_idle = sum(r["Idle within span (min)"] for r in summary)
    zero_idle = sum(1 for r in summary if r["Idle within span (min)"] < 1)
    notes.append(
        f"Non-delay scheduling: {len(summary)} resources used; {zero_idle} ran with "
        f"zero idle inside their active span; total idle within spans = {round(total_idle, 1)} min."
    )

    return schedule


def build_machine_view(schedule, masters, config):
    """Derive the machine-centric tables from a schedule:

    * ``timeline`` — every operation, grouped per machine and ordered by start,
      with an "Idle before" column = working minutes the machine waited since its
      previous operation (≈0 means it ran continuously).
    * ``summary`` — per machine: op count, busy minutes, idle-within-span, and
      utilization %.
    """
    clock = WorkClock(masters.calendar, config)

    by_machine: dict[str, list] = {}
    for e in schedule:
        by_machine.setdefault(e.machine, []).append(e)

    def display(mid):
        m = masters.machines.get(mid)
        return m.display_name if m else mid

    def provisional(mid):
        m = masters.machines.get(mid)
        return bool(m.provisional) if m else False

    # Order machines by when they first start working (then by id).
    order = sorted(by_machine, key=lambda mid: (min(e.start for e in by_machine[mid]), mid))

    timeline, summary = [], []
    for mid in order:
        ops = sorted(by_machine[mid], key=lambda e: e.start)
        prev_end = None
        busy = 0.0
        for e in ops:
            idle = clock.working_minutes_between(prev_end, e.start) if prev_end else 0.0
            timeline.append({
                "Machine": display(mid),
                "Batch": e.batch_id,
                "Item Code": e.item_code,
                "Process": e.process_name,
                "Start": e.start,
                "End": e.end,
                "Busy (min)": round(e.occupancy_min, 1),
                "Idle before (min)": round(idle, 1),
            })
            busy += e.occupancy_min
            prev_end = e.end

        first, last = ops[0].start, ops[-1].end
        span = clock.working_minutes_between(first, last)
        idle_within = max(span - busy, 0.0)
        util = (busy / span * 100.0) if span > 0 else 100.0
        summary.append({
            "Machine": display(mid),
            "Ops": len(ops),
            "First Start": first,
            "Last End": last,
            "Busy (min)": round(busy, 1),
            "Idle within span (min)": round(idle_within, 1),
            "Utilization %": round(util, 1),
            "Provisional": provisional(mid),
        })

    return timeline, summary
