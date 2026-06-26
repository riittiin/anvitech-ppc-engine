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
from ..worktime import WorkClock, NoWorkingWindow
from ..loaders import normalize_resource_id, parse_resource_candidates
from . import rule4_setup_time as r4
from . import rule5_overlap_mode as r5


def _clock_factory(masters, config):
    """A memoized ``clock_for(machine_id)``. With operator logic ON, each machine
    gets its own working window (per Available Hrs/Day + operator coverage; an
    uncovered machine gets an EMPTY clock → ``advance`` raises ``NoWorkingWindow``).
    With it OFF, every machine shares the legacy two-shift window (current behaviour).
    A resource not in the master (a generic station from a process name) defaults to
    the legacy two-shift window. Returns ``(clock_for, cov_report)``."""
    if getattr(config, "apply_operator_logic", False):
        from ..operator_coverage import machine_windows
        windows, cov_report = machine_windows(masters, config)
    else:
        windows, cov_report = None, None
    legacy = WorkClock.from_config(masters.calendar, config)
    cache = {}

    def clock_for(mid):
        if windows is None:
            return legacy
        if mid not in cache:
            iv = windows.get(mid)
            cache[mid] = legacy if iv is None else WorkClock(masters.calendar, iv)
        return cache[mid]

    return clock_for, cov_report


def _resolve_candidates(proc):
    """Ordered candidate machine ids for a process (first = preferred).

    A "Suggested M/c" cell may list ALTERNATIVES ('CNC3/CNC6') — the process may run
    on any of them, and the scheduler picks the earliest-free. Falls back to a generic
    station named after the process when no machine is given."""
    raw = proc.suggested_machine or proc.allotted_machine or proc.name
    return parse_resource_candidates(raw) or [normalize_resource_id(proc.name)]


def _free_at(proc, start, ready, machine_free, plan_start, clock_for):
    """This op's candidate machines that can begin right at ``start`` (i.e. are free
    that moment). In candidate order; uncovered machines (no window) are excluded.
    Used to decide a parallel split."""
    out = []
    for cand in _resolve_candidates(proc):
        try:
            feasible = clock_for(cand).advance(max(ready, machine_free.get(cand, plan_start)), 0)
        except NoWorkingWindow:
            continue
        if feasible == start:
            out.append(cand)
    return out


def _split_qty(qty, n):
    """Split ``qty`` into ``n`` whole-piece shares, remainder to the first shares.
    e.g. (50, 2) -> [25, 25]; (51, 2) -> [26, 25]; (5, 2) -> [3, 2]."""
    base, rem = divmod(int(qty), n)
    return [base + (1 if i < rem else 0) for i in range(n)]


def run(batches, config=None, notes=None, masters=None, machine_lost_min=None, **kw):
    # Imported lazily to avoid a circular import (pipeline imports this module).
    from ..pipeline import RuleError

    notes = notes if notes is not None else []
    if masters is None:
        raise RuleError("rule6", "-", "masters are required to allocate")

    clock_for, cov_report = _clock_factory(masters, config)
    op_for = None
    if getattr(config, "apply_operator_logic", False):
        from ..operator_coverage import operator_for as op_for
    plan_start = datetime(
        config.plan_start_date.year,
        config.plan_start_date.month,
        config.plan_start_date.day,
        config.first_shift_start_hour,
    )

    # One state record per batch. ``ready`` is the earliest its NEXT process may
    # start (precedence constraint from the previous process); ``next`` indexes
    # the next unscheduled process. ``blocked`` = an op had no covered machine.
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
            "blocked": False,
        })

    total_ops = sum(len(s["routing"].processes) for s in states)
    machine_free: dict[str, datetime] = {}
    schedule: list[ScheduleEntry] = []
    blocked_ops: list[dict] = []

    # Downtime loop-back: a machine that lost time in the actuals is treated as
    # unavailable for that many WORKING minutes from the plan start, so its whole
    # queue slips later (non-delay scheduling then handles the rest). Uses the
    # machine's own working window.
    if machine_lost_min:
        seeded = []
        for mid, mins in machine_lost_min.items():
            if mins and mins > 0:
                try:
                    machine_free[mid] = clock_for(mid).advance(plan_start, mins)
                    seeded.append(f"{mid} +{round(mins)} min")
                except NoWorkingWindow:
                    pass  # an uncovered machine has no window to delay
        if seeded:
            notes.append(
                "Downtime loop-back: seeded recorded lost time into machine "
                "availability — " + ", ".join(sorted(seeded)) + "."
            )

    guard, guard_cap = 0, total_ops + len(states) + 5
    while guard <= guard_cap:
        guard += 1
        best = None  # (sort_key, state, proc, resource, occ, note)
        for s in states:
            if s["blocked"] or s["next"] >= len(s["routing"].processes):
                continue
            proc = s["routing"].processes[s["next"]]
            candidates = _resolve_candidates(proc)
            occ = r4.occupancy_minutes(proc.cycle_time, s["batch"].qty, config)

            # Among the allowed machines, pick the one that can start earliest. A
            # candidate with no working window (no operator coverage) is skipped.
            # Strict '<' keeps the first-listed (preferred) machine on a tie.
            resource, feasible = None, None
            for cand in candidates:
                try:
                    cand_feasible = clock_for(cand).advance(
                        max(s["ready"], machine_free.get(cand, plan_start)), 0)
                except NoWorkingWindow:
                    continue   # candidate uncovered — not usable
                if feasible is None or cand_feasible < feasible:
                    resource, feasible = cand, cand_feasible

            if resource is None:
                # No candidate has a working window → block this op (and its batch's
                # downstream). Surfaced as "needs operator", never fatal.
                s["blocked"] = True
                blocked_ops.append({
                    "SO No": ", ".join(s["batch"].source_so_refs),
                    "Batch": s["batch"].batch_id, "Item Code": s["batch"].item_code,
                    "Process": f"{proc.seq}. {proc.name}",
                    "Machine(s)": "/".join(candidates),
                })
                continue

            note = f"chose {resource} of {'/'.join(candidates)}" if len(candidates) > 1 else ""
            key = (feasible, s["prio"], s["batch"].batch_id, proc.seq)
            if best is None or key < best[0]:
                best = (key, s, proc, resource, occ, note)

        if best is None:
            break
        _, s, proc, resource, occ, note = best
        start = best[0][0]
        batch = s["batch"]
        cyc = proc.cycle_time or 0.0
        setup = config.setup_time_min

        # Parallel split: if enabled and 2+ of this op's machines are FREE the moment
        # it can start, split the quantity across them to run in parallel.
        free = _free_at(proc, start, s["ready"], machine_free, plan_start, clock_for) \
            if getattr(config, "split_parallel", False) else []
        do_split = (len(free) >= 2 and (batch.qty or 0) >= getattr(config, "split_min_qty", 2))

        if do_split:
            subs = _split_qty(int(batch.qty), len(free))
            cand_str = "/".join(_resolve_candidates(proc))
            slow_end, slow_clock, slow_run, slow_occ = start, clock_for(free[0]), 0.0, 0.0
            for cand, sq in zip(free, subs):
                if sq <= 0:
                    continue
                occ_i = cyc * sq + setup
                cclock = clock_for(cand)
                end_i = cclock.advance(start, occ_i)
                machine_free[cand] = end_i
                schedule.append(ScheduleEntry(
                    batch_id=batch.batch_id, item_code=batch.item_code,
                    process_seq=proc.seq, process_name=proc.name, machine=cand,
                    qty=sq, occupancy_min=occ_i, start=start, end=end_i,
                    notes=f"parallel split: {sq} of {int(batch.qty)} on {cand} ({cand_str})",
                    so_refs=list(batch.source_so_refs),
                    operator=(op_for(cand, start, masters, config) if op_for else ""),
                ))
                if end_i > slow_end:   # the slowest half decides when the batch recombines
                    slow_end, slow_clock, slow_run, slow_occ = end_i, cclock, cyc * sq, occ_i
            s["next"] += 1
            if s["next"] < len(s["routing"].processes):
                elapsed = r5.elapsed_before_next(slow_occ, slow_run, config)
                s["ready"] = slow_clock.advance(start, elapsed)
        else:
            mclock = clock_for(resource)
            end = mclock.advance(start, occ)
            machine_free[resource] = end
            operator = op_for(resource, start, masters, config) if op_for else ""
            schedule.append(ScheduleEntry(
                batch_id=batch.batch_id, item_code=batch.item_code,
                process_seq=proc.seq, process_name=proc.name, machine=resource,
                qty=batch.qty, occupancy_min=occ, start=start, end=end, notes=note,
                so_refs=list(batch.source_so_refs), operator=operator,
            ))
            # Advance this batch and set when its next process may start (Rule 5).
            # Overlap measures the previous process's CUTTING time only (setup
            # excluded); the wait is walked on the PRODUCER machine's clock.
            s["next"] += 1
            if s["next"] < len(s["routing"].processes):
                run_min = cyc * (batch.qty or 0.0)
                elapsed = r5.elapsed_before_next(occ, run_min, config)
                s["ready"] = mclock.advance(start, elapsed)

    # Decision notes: prove machines ran continuously.
    _timeline, summary = build_machine_view(schedule, masters, config)
    total_idle = sum(r["Idle within span (min)"] for r in summary)
    zero_idle = sum(1 for r in summary if r["Idle within span (min)"] < 1)
    notes.append(
        f"Non-delay scheduling: {len(summary)} resources used; {zero_idle} ran with "
        f"zero idle inside their active span; total idle within spans = {round(total_idle, 1)} min."
    )
    if getattr(config, "apply_operator_logic", False):
        notes.append(
            f"Operator/shift logic ON: machines use per-availability windows "
            f"(≥{config.two_shift_threshold_hours:g} hrs → both shifts 08:00–05:00; "
            f"else single-shift {config.manual_start_hour:02d}:00–{config.manual_end_hour:02d}:00), "
            f"and only run shifts that have a qualified operator."
        )
        if blocked_ops:
            notes.append(
                f"{len(blocked_ops)} operation(s) NOT scheduled — no qualified operator "
                f"on a valid shift for the machine (see the 'needs operator' table)."
            )
        if cov_report and cov_report.get("unmatched_specialties"):
            notes.append(
                f"{len(cov_report['unmatched_specialties'])} operator specialty entr(ies) "
                f"match no machine — check naming (see the table)."
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
    clock_for, _ = _clock_factory(masters, config)

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
        clock = clock_for(mid)   # this machine's own working window
        ops = sorted(by_machine[mid], key=lambda e: e.start)
        prev_end = None
        busy = 0.0
        for e in ops:
            idle = clock.working_minutes_between(prev_end, e.start) if prev_end else 0.0
            trow = {
                "Machine": display(mid),
                "SO No": ", ".join(e.so_refs),
                "Batch": e.batch_id,
                "Item Code": e.item_code,
                "Process": e.process_name,
                "Start": e.start,
                "End": e.end,
                "Busy (min)": round(e.occupancy_min, 1),
                "Idle before (min)": round(idle, 1),
            }
            if e.operator:
                trow["Operator"] = e.operator
            timeline.append(trow)
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
