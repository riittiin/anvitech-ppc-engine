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

from datetime import datetime, timedelta

from ..models import ScheduleEntry
from ..worktime import WorkClock, NoWorkingWindow
from ..loaders import parse_resource_candidates, normalize_process_name
from ..orderbook import is_dispatch
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


def _resolve_candidates(proc, config=None):
    """Ordered REAL machine ids for a process (first = preferred), or [] if none.

    The parallelization toggle (``config.split_parallel``) decides the set:
      * OFF → the **Allotted** machine(s) only (the planned choice); if Allotted is
        blank, fall back to the **Suggested** machine(s) so the step still schedules.
      * ON  → the **union** of Allotted + Suggested (Allotted first, deduped) — every
        machine the item is capable of, so work can spread across all of them.
    Alternatives within a cell ('CNC3/CNC6') are parsed either way. A fully blank cell
    returns [] (the step is never invented onto a phantom station; with no cycle time
    it is an off-machine milestone, else 'needs machine')."""
    allotted = parse_resource_candidates(proc.allotted_machine)
    suggested = parse_resource_candidates(proc.suggested_machine)
    if getattr(config, "split_parallel", False):
        return allotted + [c for c in suggested if c not in allotted]
    return allotted or suggested


def _qty_for(batch, proc):
    """Pieces still to run at THIS process. With per-process progress known
    (``batch.process_qty``) it's ordered − done-at-this-step, so finished work isn't
    re-scheduled ("continue from reality"). Without it, the full batch qty (today's
    behaviour)."""
    pq = getattr(batch, "process_qty", None)
    if pq is None:
        return batch.qty
    return pq.get(normalize_process_name(proc.name), batch.qty)


def _is_offmachine(proc):
    """An off-machine step: NO machine assigned AND no cycle time — e.g. an
    outside-service **OS** step (outsourcing) or **DISPATCH** (ship / final step, no
    time allotted). It runs off any in-house machine, so it takes no machine, no
    operator and no time — but it is NOT ignored: it is scheduled as a visible
    zero-duration milestone (see the allocation loop) so outsourcing is always shown.

    Keyed on *both* (no machine AND no time) on purpose: a step you merely forgot to
    fill in (blank machine but a real cycle time) is NOT off-machine — it still
    surfaces as 'needs machine' so missing data fails loud."""
    raw = proc.suggested_machine or proc.allotted_machine or ""
    has_machine = bool(parse_resource_candidates(raw))
    has_time = bool(proc.cycle_time) and proc.cycle_time > 0
    return not has_machine and not has_time


def _is_os(proc):
    """True if this process is an OUTSOURCED (OS) step.

    Marked by the machine cell being the sentinel ``OS`` (Allotted or Suggested =
    OS), or — only when NO real machine is assigned — by an ``OS`` word in the
    process name. Keyed on the machine cell on purpose: a step merely NAMED
    '... OS' but given a real machine (e.g. the sample's 'CNC OS' on CNC1/CNC2) is
    an in-house step, NOT outsourcing."""
    if "OS" in parse_resource_candidates(proc.allotted_machine) \
            or "OS" in parse_resource_candidates(proc.suggested_machine):
        return True
    real = [c for c in (parse_resource_candidates(proc.allotted_machine)
                        + parse_resource_candidates(proc.suggested_machine)) if c != "OS"]
    return not real and "OS" in normalize_process_name(proc.name).split()


def _offmachine_lane(proc):
    """Gantt lane (shown as the 'machine') for an off-machine milestone: an
    outsourced **OS** step vs any other off-machine step (DISPATCH, a manual step
    with no machine assigned, etc.). Two buckets keeps the colour legend readable
    while still surfacing outsourcing distinctly."""
    tokens = normalize_process_name(proc.name).split()
    return "OS / Outsourced" if "OS" in tokens else "Off-machine"


def _allocate_op(proc, qty, cyc, setup, ready, machine_free, plan_start, clock_for, config,
                 operator_free=None, op_lookup=None):
    """Decide how to run one operation. Returns ``(entries, blocked)`` where
    ``entries`` is a list of ``(machine, qty, start, end, operator)``:

    * a SINGLE entry = run the whole quantity on the earliest-free machine
      (today's behaviour), OR
    * MULTIPLE entries = a parallel split. When the step lists alternative machines
      and ``split_parallel`` is on, the quantity is shared across them to **minimise
      when the step finishes** — each machine gets the load it can complete by a
      common target finish time T, starting from when IT becomes free.

    **Operators are one-at-a-time resources.** A candidate machine's free time also
    waits for its **earliest-free qualified operator** (``op_lookup`` + ``operator_free``);
    split siblings get **distinct** operators (a person can't run two machines at once),
    so work spreads across the crew and concurrency is capped by headcount. With
    operator logic off (or a provisional machine with no listed crew) there's no
    operator constraint and the operator is ''.

    ``blocked`` is True only if no candidate machine has a working window (uncovered)."""
    qty = int(qty or 0)
    listed = _resolve_candidates(proc, config)
    operator_free = operator_free if operator_free is not None else {}
    op_lookup = op_lookup or (lambda m, t: [])
    reserved = set()        # operators tentatively taken by earlier (split-sibling) candidates
    cands = []  # (machine, clock, earliest_free, operator)
    for m in listed:
        clk = clock_for(m)
        try:
            mf = clk.advance(max(ready, machine_free.get(m, plan_start)), 0)
        except NoWorkingWindow:
            continue   # uncovered machine — can't run here
        names = op_lookup(m, mf)
        if names:                       # operator logic on for this machine
            avail = [o for o in names if o not in reserved]
            if not avail:
                continue                # every qualified operator already taken by a sibling
            op = min(avail, key=lambda o: operator_free.get(o, plan_start))
            reserved.add(op)
            f = clk.advance(max(mf, operator_free.get(op, plan_start)), 0)
            cands.append((m, clk, f, op))
        else:                           # no operator constraint (logic off / provisional)
            cands.append((m, clk, mf, ""))
    if not cands:
        return [], True
    cands.sort(key=lambda c: (c[2], listed.index(c[0])))  # earliest free, first-listed tiebreak

    def end_of(clk, f, q):
        return clk.advance(f, cyc * q + setup)

    bm, bclk, bf, bop = cands[0]
    single = [(bm, qty, bf, end_of(bclk, bf, qty), bop)]
    if (not getattr(config, "split_parallel", False) or len(cands) < 2
            or qty < getattr(config, "split_min_qty", 2) or cyc <= 0):
        return single, False
    single_end = single[0][3]

    def capacities(T):
        """Whole pieces each machine can finish by time T (from its free time, incl.
        its own setup)."""
        out = []
        for _m, clk, f, _op in cands:
            if T <= f:
                out.append(0); continue
            wm = clk.working_minutes_between(f, T)
            out.append(int((wm - setup) // cyc) if wm > setup else 0)
        return out

    # Binary-search the smallest finish time T (in (bf, single_end]) whose combined
    # capacity covers the quantity. working_minutes is monotonic in T, so capacity is.
    span = (single_end - bf).total_seconds() / 60.0
    lo, hi, bestT = 0.0, span, None
    for _ in range(40):
        mid = (lo + hi) / 2
        if sum(capacities(bf + timedelta(minutes=mid))) >= qty:
            bestT = bf + timedelta(minutes=mid); hi = mid
        else:
            lo = mid
    if bestT is None:
        return single, False

    per = capacities(bestT)
    shares, rem = [0] * len(cands), qty
    for i in range(len(cands)):
        take = min(per[i], rem); shares[i] = take; rem -= take
        if rem <= 0:
            break
    if rem > 0:
        return single, False
    entries = [(cands[i][0], shares[i], cands[i][2], end_of(cands[i][1], cands[i][2], shares[i]),
                cands[i][3])
               for i in range(len(cands)) if shares[i] > 0]
    # Split only if it genuinely beats one machine and uses 2+ machines.
    if len(entries) < 2 or max(e[3] for e in entries) >= single_end:
        return single, False
    return entries, False


def run(batches, config=None, notes=None, masters=None, machine_lost_min=None, **kw):
    # Imported lazily to avoid a circular import (pipeline imports this module).
    from ..pipeline import RuleError

    notes = notes if notes is not None else []
    if masters is None:
        raise RuleError("rule6", "-", "masters are required to allocate")

    clock_for, cov_report = _clock_factory(masters, config)
    # Operators as one-at-a-time resources: op_lookup(m, t) = qualified operators for
    # machine m at time t (empty when logic off → no operator constraint, no label).
    if getattr(config, "apply_operator_logic", False):
        from ..operator_coverage import qualified_operators
        def op_lookup(m, t):
            return qualified_operators(m, t, masters, config)
    else:
        def op_lookup(m, t):
            return []
    operator_free: dict[str, datetime] = {}   # operator name → when they next free up
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

    # Optional: seed a machine as unavailable for N working-minutes from the plan
    # start (a low-level scheduling primitive — the app does NOT use it; recorded
    # downtime never affects the schedule). Its whole queue then slips later.
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
                "Seeded machine-unavailable time — " + ", ".join(sorted(seeded)) + "."
            )

    offmachine: list = []    # (item_code, seq, name) of off-machine milestones (OS/DISPATCH)
    done_steps: list = []    # (item_code, seq, name) of steps already fully produced
    needs_machine: list = []  # steps with a cycle time but NO machine — a data gap
    os_reserved: list = []   # (item_code, seq, name, minutes) of OS turnaround blocks

    guard, guard_cap = 0, total_ops + len(states) + 5
    while guard <= guard_cap:
        guard += 1
        # Advance each batch, handling steps that need no machine scheduling:
        #  * off-machine steps (no machine, no time) — OS / outsourcing, DISPATCH:
        #    emitted as a visible zero-duration milestone (NOT ignored) at the batch's
        #    current ready time, then stepped past (no machine/operator/time consumed);
        #  * steps already fully produced on the floor (per-process remaining 0) —
        #    don't re-run finished work; the pieces are ready for the next step.
        for s in states:
            while not s["blocked"] and s["next"] < len(s["routing"].processes):
                p = s["routing"].processes[s["next"]]
                if _is_os(p):
                    q = _qty_for(s["batch"], p)
                    if q <= 0:                      # already cleared on the floor — skip
                        done_steps.append((s["batch"].item_code, p.seq, p.name))
                        s["next"] += 1
                        continue
                    cyc = p.cycle_time or 0.0
                    start = s["ready"]
                    end = start + timedelta(minutes=cyc) if cyc > 0 else start
                    schedule.append(ScheduleEntry(
                        batch_id=s["batch"].batch_id, item_code=s["batch"].item_code,
                        process_seq=p.seq, process_name=p.name, machine="OS / Outsourced",
                        qty=q, occupancy_min=cyc, start=start, end=end,
                        notes=(f"outsourced (OS) — reserves {cyc:g} min of vendor "
                               f"turnaround (continuous, no in-house machine/operator)"
                               if cyc > 0 else
                               "outsourced (OS) — no turnaround time set yet; shown "
                               "as a milestone"),
                        so_refs=list(s["batch"].source_so_refs), operator="",
                    ))
                    if cyc > 0:
                        os_reserved.append((s["batch"].item_code, p.seq, p.name, cyc))
                        s["ready"] = end            # successor waits for the full block
                    else:
                        offmachine.append((s["batch"].item_code, p.seq, p.name))
                    s["next"] += 1
                elif _is_offmachine(p):
                    if is_dispatch(p.name):
                        # DISPATCH = the finished-goods gate: wait for the WHOLE order.
                        # Overlap can let a later step finish before an earlier long one,
                        # so place it at the LATEST end across all of this batch's prior
                        # processes (not the immediate predecessor) — you can't ship until
                        # every piece has cleared every process.
                        at = max((e.end for e in schedule
                                  if e.batch_id == s["batch"].batch_id), default=s["ready"])
                    else:
                        at = s["ready"]
                    schedule.append(ScheduleEntry(
                        batch_id=s["batch"].batch_id, item_code=s["batch"].item_code,
                        process_seq=p.seq, process_name=p.name, machine=_offmachine_lane(p),
                        qty=_qty_for(s["batch"], p), occupancy_min=0.0,
                        start=at, end=at,   # milestone: zero duration
                        notes="off-machine step (OS / outsourcing / dispatch) — no in-house "
                              "machine or time; shown as a milestone",
                        so_refs=list(s["batch"].source_so_refs), operator="",
                    ))
                    offmachine.append((s["batch"].item_code, p.seq, p.name))
                    s["next"] += 1
                elif _qty_for(s["batch"], p) <= 0:
                    done_steps.append((s["batch"].item_code, p.seq, p.name))
                    s["next"] += 1
                else:
                    break

        best = None  # (sort_key, state, proc, resource, occ, note)
        for s in states:
            if s["blocked"] or s["next"] >= len(s["routing"].processes):
                continue
            proc = s["routing"].processes[s["next"]]
            candidates = _resolve_candidates(proc, config)
            if not candidates:
                # No machine assigned but it wasn't an off-machine milestone → it has a
                # cycle time. Fail loud: block the batch, never invent a phantom station.
                s["blocked"] = True
                needs_machine.append({
                    "SO No": ", ".join(s["batch"].source_so_refs),
                    "Batch": s["batch"].batch_id, "Item Code": s["batch"].item_code,
                    "Process": f"{proc.seq}. {proc.name}",
                })
                continue
            occ = r4.occupancy_minutes(proc.cycle_time, _qty_for(s["batch"], proc), config)

            # Among the allowed machines, pick the one that can start earliest. A
            # candidate with no working window (no operator coverage) is skipped.
            # Strict '<' keeps the first-listed (preferred) machine on a tie.
            resource, feasible = None, None
            for cand in candidates:
                clk = clock_for(cand)
                try:
                    base = clk.advance(max(s["ready"], machine_free.get(cand, plan_start)), 0)
                except NoWorkingWindow:
                    continue   # candidate uncovered — not usable
                # Also wait for the earliest-free qualified operator (one-at-a-time),
                # so ordering reflects real availability, not just machine free time.
                names = op_lookup(cand, base)
                if names:
                    op_free = min(operator_free.get(o, plan_start) for o in names)
                    cand_feasible = clk.advance(max(base, op_free), 0)
                else:
                    cand_feasible = base
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
        batch = s["batch"]
        cyc = proc.cycle_time or 0.0
        setup = config.setup_time_min
        cands_list = _resolve_candidates(proc, config)
        proc_qty = _qty_for(batch, proc)   # this step's remaining (= batch qty if no progress)

        entries, _blk = _allocate_op(proc, proc_qty, cyc, setup, s["ready"],
                                     machine_free, plan_start, clock_for, config,
                                     operator_free, op_lookup)
        # Pace by the predecessor: a step may START early (overlap) but cannot FINISH
        # before every earlier process of this batch has delivered the last piece — a fast
        # step is *starved* by a slow one. Hold its completion to the latest prior end
        # (extend the entries' span; the machine stays engaged with this batch until then).
        # Work (occupancy) is unchanged — only the span grows (idle waiting for pieces).
        prev_end = max((e.end for e in schedule if e.batch_id == batch.batch_id), default=None)
        if prev_end is not None and entries:
            naive_end = max(en for _, _, _, en, _ in entries)
            if prev_end > naive_end:
                entries = [(m, q, st, prev_end, op) for (m, q, st, en, op) in entries]
        split = len(entries) > 1

        # Emit an entry per machine; track the slowest portion (it decides recombine).
        slow = None  # (end, clock, start, run_min, occ)
        for m, q, st, en, op in entries:
            machine_free[m] = en
            if op:
                operator_free[op] = en          # this person is busy until the op ends
            if split:
                e_note = f"parallel split: {q} of {int(proc_qty)} on {m} ({'/'.join(cands_list)})"
            else:
                e_note = f"chose {m} of {'/'.join(cands_list)}" if len(cands_list) > 1 else ""
            schedule.append(ScheduleEntry(
                batch_id=batch.batch_id, item_code=batch.item_code,
                process_seq=proc.seq, process_name=proc.name, machine=m,
                qty=q, occupancy_min=cyc * q + setup, start=st, end=en, notes=e_note,
                so_refs=list(batch.source_so_refs),
                operator=op,
            ))
            if slow is None or en > slow[0]:
                slow = (en, clock_for(m), st, cyc * q, cyc * q + setup)

        # Advance this batch; the next process may start after the slowest portion
        # (Rule 5 overlap measured on that machine's clock — split-then-recombine).
        s["next"] += 1
        if s["next"] < len(s["routing"].processes):
            nxt = s["routing"].processes[s["next"]]
            elapsed = r5.elapsed_before_next(slow[4], slow[3], config)
            if _is_os(nxt) or elapsed >= slow[4]:
                # Full-completion cases — the OS next step (can't ship un-machined parts),
                # sequential mode, or a no-cutting predecessor: the successor waits for the
                # predecessor's *paced* END (slow[0], held to its own predecessor). Overlap
                # otherwise: start after % of the predecessor's cutting time (setup excluded).
                s["ready"] = slow[0]
            else:
                s["ready"] = slow[1].advance(slow[2], elapsed)

    if offmachine:
        names = sorted({nm for _, _, nm in offmachine})
        notes.append(
            f"Included {len(offmachine)} off-machine step(s) as milestones — OS / "
            f"outsourcing / dispatch (no in-house machine or time), shown on the Gantt "
            f"but consuming no machine/operator time (e.g. {', '.join(names[:5])})."
        )
    if done_steps:
        names = sorted({nm for _, _, nm in done_steps})
        notes.append(
            f"Skipped {len(done_steps)} step(s) already fully produced on the floor "
            f"(per-process remaining 0) — work continues from there (e.g. "
            f"{', '.join(names[:5])})."
        )
    if needs_machine:
        names = sorted({r["Process"] for r in needs_machine})
        notes.append(
            f"⚠ {len(needs_machine)} step(s) have a cycle time but NO machine assigned — "
            f"NOT scheduled (their batch is held). Fix the Machine column in the process "
            f"master (e.g. {', '.join(names[:5])})."
        )
    if os_reserved:
        names = sorted({nm for _, _, nm, _ in os_reserved})
        notes.append(
            f"Reserved {len(os_reserved)} outsourced (OS) step(s) as continuous "
            f"turnaround blocks — no in-house machine or operator; the next process "
            f"waits for each to return (e.g. {', '.join(names[:5])})."
        )

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

    NON_MACHINE_LANES = {"OS / Outsourced", "Off-machine"}
    by_machine: dict[str, list] = {}
    for e in schedule:
        if e.machine in NON_MACHINE_LANES:
            continue   # outsourcing / dispatch lanes are not machines
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
