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

from datetime import datetime, timedelta, time as dtime

from ..models import ScheduleEntry
from ..worktime import WorkClock, NoWorkingWindow
from ..loaders import parse_resource_candidates, normalize_process_name
from ..orderbook import is_dispatch
from . import rule4_setup_time as r4
from . import rule5_overlap_mode as r5

# Sentinel operator name for a shift segment NO qualified person is free to man
# (the plan runs more machines in that shift than the crew can staff). Surfaced in
# the shift-wise download and rolled up by Analytics as "unstaffed hours" — never
# double-billed onto a busy person (that made operators exceed 100%).
UNSTAFFED = "⚠ Unstaffed (no qualified operator free)"


def _is_setup_machine(mid, masters=None) -> bool:
    """Setup time (``config.setup_time_min``) models the effort to PROGRAM/SET a CNC or
    VMC before it runs, so it is charged ONLY to CNC/VMC machining. Manual/finishing
    stations (washing, deburring, packing, inspection, drilling/chamfer, bandsaw,
    manual lathe) need no such setup and get 0.

    A machine qualifies if its (normalized) id starts with CNC/VMC — which is how every
    real CNC/VMC machine is named — or, as a fallback for oddly-named ids, if its
    Machine-master type is a CNC lathe / Vertical Machining center."""
    m = str(mid or "").upper()
    if m.startswith("CNC") or m.startswith("VMC"):
        return True
    if masters is not None:
        mach = getattr(masters, "machines", {}).get(mid)
        t = (getattr(mach, "machine_type", "") or "").upper() if mach else ""
        if "CNC" in t or "VMC" in t or "VERTICAL MACHINING" in t:
            return True
    return False


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


def _push_clear(clk, start, qty, cyc, setup, res_lists):
    """Return ``(start, end)`` pushed forward until the op runs continuously clear of
    every reserved interval in ``res_lists`` (each a list of ``(s, e)`` tuples for the
    machine and the operator). Non-preemptive: the whole ``[start, end]`` must clear each
    block. ``end`` is always recomputed via the machine's working clock, so the op keeps
    its full working-minute occupancy no matter how far it is pushed."""
    end = clk.advance(start, cyc * qty + setup)
    for _ in range(256):
        conflict_end = None
        for lst in res_lists:
            for rs, re_ in lst:
                if start < re_ and end > rs and (conflict_end is None or re_ > conflict_end):
                    conflict_end = re_
        if conflict_end is None:
            return start, end
        start = clk.advance(conflict_end, 0)          # first working minute after the block
        end = clk.advance(start, cyc * qty + setup)
    return start, end


def _allocate_op(proc, qty, cyc, setup, ready, machine_free, plan_start, clock_for, config,
                 operator_free=None, op_lookup=None, reserved_intervals=None):
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
    reserved_intervals = reserved_intervals or {}
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
    if reserved_intervals:
        bf, bend = _push_clear(bclk, bf, qty, cyc, setup,
                               [reserved_intervals.get(bm, []), reserved_intervals.get(bop, [])])
    else:
        bend = end_of(bclk, bf, qty)
    single = [(bm, qty, bf, bend, bop)]
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
    # capacity covers the quantity. Note: split sizing uses pre-push free times, so a
    # reservation may make the split slightly sub-optimal; but _push_clear on each
    # split entry (lines 271-273) preserves the no-overlap guarantee. working_minutes
    # is monotonic in T, so capacity is.
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
    entries = []
    for i in range(len(cands)):
        if shares[i] <= 0:
            continue
        m_i, clk_i, st_i, op_i = cands[i][0], cands[i][1], cands[i][2], cands[i][3]
        if reserved_intervals:
            st_i, en_i = _push_clear(clk_i, st_i, shares[i], cyc, setup,
                                     [reserved_intervals.get(m_i, []), reserved_intervals.get(op_i, [])])
        else:
            en_i = end_of(clk_i, st_i, shares[i])
        entries.append((m_i, shares[i], st_i, en_i, op_i))
    # Split only if it genuinely beats one machine and uses 2+ machines.
    if len(entries) < 2 or max(e[3] for e in entries) >= single_end:
        return single, False
    return entries, False


def run(batches, config=None, notes=None, masters=None, machine_lost_min=None,
        reserved=None, **kw):
    # Imported lazily to avoid a circular import (pipeline imports this module).
    from ..pipeline import RuleError

    notes = notes if notes is not None else []
    reserved = reserved or {}
    if masters is None:
        raise RuleError("rule6", "-", "masters are required to allocate")

    clock_for, cov_report = _clock_factory(masters, config)
    # Operators as one-at-a-time resources: op_lookup(m, t) = qualified operators for
    # machine m at time t (empty when logic off → no operator constraint, no label).
    # qualified_operators only actually depends on the machine and WHICH SHIFT t is in
    # (first/second) — two possibilities — so memoize on (machine, shift). This turns a
    # per-op scan over every operator (millions of calls in an optimizer run) into ~52
    # distinct computations; results are read-only downstream, so sharing is safe.
    if getattr(config, "apply_operator_logic", False):
        from ..operator_coverage import qualified_operators, _shift_of
        _qual_memo: dict = {}
        def op_lookup(m, t):
            key = (m, _shift_of(t, config))
            names = _qual_memo.get(key)
            if names is None:
                names = qualified_operators(m, t, masters, config)
                _qual_memo[key] = names
            return names
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

        # Collect every ready op with its earliest-feasible start; the dispatch rule
        # below chooses which one to schedule this iteration.
        options = []  # (feasible, prio, slack, state, proc, resource, occ, note)
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
            # Dynamic slack = working time to the SO delivery date − work still needed
            # for this batch (this op + every later op). Only used by the expedite window.
            due = datetime.combine(s["batch"].so_delivery_date, dtime(23, 59))
            rem_work = sum(r4.occupancy_minutes(pp.cycle_time, _qty_for(s["batch"], pp), config)
                           for pp in s["routing"].processes[s["next"]:])
            slack = (due - feasible).total_seconds() / 60.0 - rem_work
            options.append((feasible, s["prio"], slack, s, proc, resource, occ, note))

        if not options:
            break

        # Dispatch rule. Baseline (expedite_window_min == 0): the earliest-startable op
        # wins, priority breaks exact ties — byte-identical to the legacy plan. With a
        # window: among ops that could start within `expedite_window_min` of the
        # EARLIEST feasible start, pick the least-slack (most at-risk) one, so an urgent
        # order wins a near-race for a shared machine/operator. No resource ever idles —
        # only ops that are already ready-and-feasible-now are ever chosen.
        win = getattr(config, "expedite_window_min", 0) or 0
        if win > 0:
            earliest = min(o[0] for o in options)
            horizon = earliest + timedelta(minutes=win)
            near = [o for o in options if o[0] <= horizon]
            pick = min(near, key=lambda o: (o[2], o[0], o[3]["batch"].batch_id, o[4].seq))
        else:
            pick = min(options, key=lambda o: (o[0], o[1], o[3]["batch"].batch_id, o[4].seq))
        _feasible, _prio, _slack, s, proc, resource, occ, note = pick
        batch = s["batch"]
        cyc = proc.cycle_time or 0.0
        # Setup (machine programming time) applies to CNC/VMC only; manual/finishing
        # steps occupy their station for run time alone (no 90-min setup).
        setup = config.setup_time_min if _is_setup_machine(resource, masters) else 0
        cands_list = _resolve_candidates(proc, config)
        proc_qty = _qty_for(batch, proc)   # this step's remaining (= batch qty if no progress)

        entries, _blk = _allocate_op(proc, proc_qty, cyc, setup, s["ready"],
                                     machine_free, plan_start, clock_for, config,
                                     operator_free, op_lookup, reserved)
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
    _timeline, summary = build_machine_view(schedule, masters, config, batches)
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

    if getattr(config, "balance_operator_load", False):
        moved = _rebalance_operators(schedule, masters, config, reserved=reserved)
        if moved:
            notes.append(
                f"Operator load balancing ON: reassigned {moved} operation(s) to the "
                f"least-loaded qualified operator (same shift, already free) — spreads "
                f"work evenly across interchangeable people; start/end times unchanged."
            )

    return schedule


def _rebalance_operators(schedule, masters, config, reserved=None) -> int:
    """Fairness POST-PROCESS. Timing is already fixed; only reassign *who* runs each
    operator-run op so load spreads evenly across interchangeable people. Walks ops in
    time order and hands each to the qualified, same-shift operator who is **free at
    that op's start** and has the **least accumulated work** so far. Because it never
    touches start/end, makespan and lateness are provably unchanged. Returns the number
    of ops whose operator changed. No-op when operator logic is off (no operators)."""
    from ..operator_coverage import qualified_operators
    reserved = reserved or {}

    def _reserved_clash(o, s, en):
        """True if person ``o`` is booked by another pass (the two-pass Plan's
        committed reservations) anywhere inside [s, en) — such a person must never
        be handed extra work here (2026-07-15 audit: pass-2 rebalance put a person
        on two machines at once across the two passes)."""
        return any(b0 < en and s < b1 for b0, b1 in reserved.get(o, []))

    ops = sorted((x for x in schedule if x.operator), key=lambda x: (x.start, x.end, x.batch_id))
    original = {id(e): e.operator for e in ops}
    busy_until: dict = {}
    load: dict = {}
    for e in ops:
        eligible = qualified_operators(e.machine, e.start, masters, config)
        free = [o for o in eligible if busy_until.get(o, e.start) <= e.start
                and not _reserved_clash(o, e.start, e.end)]
        if free:
            # Least work so far; break ties by longest-idle then name (deterministic).
            pick = min(free, key=lambda o: (load.get(o, 0.0),
                                            busy_until.get(o, e.start), o))
            if pick != e.operator:
                e.operator = pick
        # If nobody is free, keep the original operator for now; the repair pass
        # below guarantees the final assignment is conflict-free.
        op = e.operator
        busy_until[op] = e.end
        load[op] = load.get(op, 0.0) + (e.end - e.start).total_seconds() / 60.0

    # SAFETY NET — never double-book. "Keep the original when nobody is free" is
    # only safe if the walk didn't hand that original other work first (live bug:
    # the walk moved a CNC op to Ankush, then a VMC op whose only qualified person
    # was Ankush kept him too — one man on two machines). The original assignment
    # is conflict-free by construction (Rule 6 allocates people one-at-a-time), so
    # reverting reassigned ops back to their original owner strictly approaches a
    # feasible state: repair until no person overlaps. Timing is never touched.
    def _first_conflict():
        by_op: dict = {}
        for e in ops:
            by_op.setdefault(e.operator, []).append(e)
        for es in by_op.values():
            es.sort(key=lambda x: (x.start, x.end, x.batch_id))
            for a, b in zip(es, es[1:]):
                if b.start < a.end:
                    return a, b
        # A reassignment that lands on a person another pass has reserved is a
        # conflict too (the original assignment respected the reservations).
        for e in ops:
            if e.operator != original[id(e)] and _reserved_clash(e.operator, e.start, e.end):
                return e, e
        return None

    while True:
        pair = _first_conflict()
        if pair is None:
            break
        reverted = False
        for e in (pair[1], pair[0]):          # prefer reverting the later op
            if e.operator != original[id(e)]:
                e.operator = original[id(e)]
                reverted = True
                break
        if not reverted:
            break   # both already original → input schedule itself was infeasible; bail
    return sum(1 for e in ops if e.operator != original[id(e)])


def build_shiftwise_timeline(schedule, masters, config, batches=None):
    """Split every operator-run operation into its per-day, per-shift segments and name
    the **actual operator** working each shift. A two-shift machine runs 08:00–19:00
    (1st) + 19:00–05:00 (2nd); a single-shift/manual station runs 09:00–18:00 only.
    Operators are assigned fairly (qualified for the machine + on that shift; the
    free-now, least-loaded one), so the same machine hands off to different people at
    shift change instead of crediting one person for a multi-day block. Returns a list
    of detail rows (Machine, SO, Batch, Item, …, Date, Shift, Start, End, Minutes,
    Operator). Timing is never changed — this is a reporting view of the fixed plan."""
    from ..operator_coverage import qualified_operators
    cal = masters.calendar
    thr = config.two_shift_threshold_hours

    def _working(d):
        return d.weekday() != cal.weekly_off_weekday and d not in cal.holidays

    def _two_shift(mid):
        m = masters.machines.get(mid)
        hrs = getattr(m, "available_hrs_per_day", None) if m else None
        return (hrs >= thr) if hrs is not None else (mid.startswith(("CNC", "VMC")))

    def _windows(mid, d):
        """Working shift windows (label, start, end) for machine `mid` on date `d`."""
        if not _working(d):
            return []
        base = datetime(d.year, d.month, d.day)
        if _two_shift(mid):
            return [("First shift", base.replace(hour=config.first_shift_start_hour),
                     base.replace(hour=config.first_shift_end_hour)),
                    ("Second shift", base.replace(hour=config.first_shift_end_hour),
                     base + timedelta(days=1, hours=config.second_shift_end_hour))]
        return [(f"Day shift ({config.manual_start_hour:02d}:00–{config.manual_end_hour:02d}:00)",
                 base.replace(hour=config.manual_start_hour),
                 base.replace(hour=config.manual_end_hour))]

    batch_by_id = {b.batch_id: b for b in (batches or [])}
    end_by_batch: dict = {}
    for e in schedule:
        if e.end > end_by_batch.get(e.batch_id, e.end - timedelta(days=1)):
            end_by_batch[e.batch_id] = e.end

    # Collect every shift-segment of every operator-run op, then assign operators in time
    # order (fair: free-now least-loaded qualified person for that machine + shift).
    segs = []
    for e in schedule:
        if not e.operator:
            continue
        d = e.start.date() - timedelta(days=1)   # start a day early to catch overnight 2nd shift
        while d <= e.end.date():
            for label, ws, we in _windows(e.machine, d):
                s = max(ws, e.start)
                en = min(we, e.end)
                if en > s:
                    segs.append((s, en, label, e))
            d += timedelta(days=1)
    segs.sort(key=lambda x: (x[0], x[3].machine))

    # Two-phase assignment (2026-07-15 audit): the printed shift sheet must show the
    # SAME person the schedule/Gantt names, wherever that person covers the shift —
    # the fair walk must never lend a person out from under their own scheduled op.
    #
    # Phase A — follow the plan: every segment whose named operator is qualified for
    # its shift gets that person (the schedule's one-at-a-time invariant means these
    # can't collide; a defensive overlap check guards imperfect inputs).
    # Phase B — fill the rest (segments in shifts the named person doesn't work) in
    # time order with a qualified person who is genuinely free across BOTH phases;
    # nobody free → UNSTAFFED (never double-billed; operators stay ≤ 100%).
    def _overlaps(ivs, s, en):
        return any(b0 < en and s < b1 for b0, b1 in ivs)

    assigned: dict = {}
    own_busy: dict = {}
    load: dict = {}
    for idx, (s, en, label, e) in enumerate(segs):
        eligible = qualified_operators(e.machine, s, masters, config)
        if e.operator in eligible and not _overlaps(own_busy.get(e.operator, []), s, en):
            assigned[idx] = e.operator
            own_busy.setdefault(e.operator, []).append((s, en))
            load[e.operator] = load.get(e.operator, 0.0) + (en - s).total_seconds() / 60.0

    busy_until: dict = {}
    for idx, (s, en, label, e) in enumerate(segs):
        if idx in assigned:
            continue
        eligible = qualified_operators(e.machine, s, masters, config)
        free = [o for o in eligible
                if busy_until.get(o, s) <= s and not _overlaps(own_busy.get(o, []), s, en)]
        if free:
            op = min(free, key=lambda o: (load.get(o, 0.0), busy_until.get(o, s), o))
            busy_until[op] = en
            load[op] = load.get(op, 0.0) + (en - s).total_seconds() / 60.0
        elif eligible:
            # Every qualified person for this shift is already on another machine.
            # NEVER bill a busy person twice (that pushed operators past 100% —
            # physically impossible); surface the gap honestly instead: the plan
            # asks for more concurrent work in this shift than the crew can staff.
            op = UNSTAFFED
        else:
            op = e.operator
        assigned[idx] = op

    rows = []
    for idx, (s, en, label, e) in enumerate(segs):
        op = assigned.get(idx)
        b = batch_by_id.get(e.batch_id)
        routing = masters.routings.get(e.item_code)
        rows.append({
            "Machine": e.machine,
            "SO No": ", ".join(e.so_refs),
            "Batch": e.batch_id,
            "Item Code": e.item_code,
            "Item Description": routing.description if routing else "",
            "SO Del date": b.so_delivery_date if b else "",
            "Expected completion": end_by_batch.get(e.batch_id, e.end).date(),
            "Process": f"{e.process_seq}. {e.process_name}",
            "Qty": int(e.qty),
            "Date": s.date(),
            "Shift": label,
            "Start": s.strftime("%H:%M"),
            "End": en.strftime("%d-%m %H:%M"),
            "Minutes": round((en - s).total_seconds() / 60),
            "Operator": op,
        })
    return rows


def build_machine_view(schedule, masters, config, batches=None):
    """Derive the machine-centric tables from a schedule:

    * ``timeline`` — every operation, grouped per machine and ordered by start,
      with an "Idle before" column = working minutes the machine waited since its
      previous operation (≈0 means it ran continuously). Each row also carries the
      order's SO delivery date + expected completion and the pieces produced in
      that op (``batches`` supplies the SO delivery date per batch).
    * ``summary`` — per machine: op count, busy minutes, idle-within-span, and
      utilization %.
    """
    clock_for, _ = _clock_factory(masters, config)

    # Per-order lookups: SO delivery date (from the batch) and expected completion
    # (latest end across ALL the order's ops — incl. OS/dispatch, matching the Gantt).
    bmap = {b.batch_id: b for b in (batches or [])}
    completion: dict = {}
    for e in schedule:
        c = completion.get(e.batch_id)
        if c is None or e.end > c:
            completion[e.batch_id] = e.end

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

    def desc(item_code):
        r = masters.routings.get(item_code)
        return r.description if r else ""

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
                "Item Description": desc(e.item_code),
                "SO Del date": (bmap[e.batch_id].so_delivery_date if e.batch_id in bmap else ""),
                "Expected completion": (completion[e.batch_id].date()
                                        if e.batch_id in completion else ""),
                "Process": e.process_name,
                "Qty": e.qty,
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
