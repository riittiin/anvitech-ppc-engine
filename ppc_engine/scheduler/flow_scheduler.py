"""The decoder — turn an order sequence into a concrete, constraint-legal schedule.

``decode(orders, sequence, masters, config) -> Schedule``

This is the single scheduler (LESSONS.md: one scheduler only) and a pure function of
its inputs (deterministic — same inputs, same schedule). The optimizer will call it
thousands of times over different sequences; this file never knows about the
objective (that lives in engine/objective).

Decode policy (v1, operation-level, non-delay):
  Repeatedly, look at every unfinished order's *next* operation, compute the earliest
  time it could feasibly start, and schedule the one that can start earliest — with
  the order *sequence* breaking ties (so the sequence, the optimizer's lever, decides
  who wins a contended machine). Each operation is laid across real working windows
  (shifts, off-days, leave), staffed by a stable per-shift operator.

See ARCHITECTURE.md "Scheduler v1 scope" for what is intentionally deferred
(piece-flow chunking, operation overlap, coarse idle-operator reassignment) — those
are later, measured layers, not hidden flags.
"""

from __future__ import annotations

import functools
from datetime import datetime, timedelta

from ppc_engine.config import PlanConfig
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import Machine
from ppc_engine.domain.routing import Operation, OperationKind
from ppc_engine.scheduler.duration import operation_duration_min
from ppc_engine.scheduler.schedule import Schedule, Segment
from ppc_engine.scheduler.staffing import StaffingBoard, build_machine_pools
from ppc_engine.worktime import effective_shift, iter_windows

# Tiny tolerance so floating-point minute arithmetic doesn't loop forever.
_EPS_MIN = 1e-9

# In-house op kinds — the only ones that can overlap (OS/dispatch stay sequential).
_INHOUSE = (OperationKind.MACHINING, OperationKind.MANUAL, OperationKind.INSPECTION)


def decode(
    orders: list[Order],
    sequence: list[tuple[str, str]],
    masters: Masters,
    config: PlanConfig,
    dispatch: str = "gt",
    frozen=None,
) -> Schedule:
    """Schedule ``orders`` following the priority ``sequence``.

    Args:
        orders:   The order lines to schedule.
        sequence: Order keys (so_no, item_code) in priority order — the decision the
                  optimizer controls. Every key must be an order with a known routing.
        masters:  The shop (machines, operators, routings, calendar).
        config:   Plan start, shifts, setup, etc.
        dispatch: How to resolve which ready op runs next:
                  - "gt" (default): Giffler-Thompson — find the op with the earliest
                    *completion*, take its machine as the critical resource, then among
                    the ops contending for that machine (that could start before that
                    completion) let the order **sequence** decide. Generates *active*
                    schedules (the class containing the tardiness optimum) and makes the
                    sequence a real lever.
                  - "nondelay": legacy — schedule whichever op can *start* earliest,
                    sequence only breaks exact ties. Kept for A/B measurement.

    Returns:
        A Schedule with all segments and each order's completion datetime.
    """
    # Occupancy (Add New Orders quote, 2026-09-08 spec) and a frozen set (in-progress
    # work) must never be asked for together: occupancy models a SECOND planning
    # stage, and a second stage never has anything in progress (a brand-new order
    # can't be half-finished). Neither `_lay_frozen` nor `_preplace_frozen` consult
    # `ShopCalendar.free_runs`, so a frozen op could be laid straight onto occupied
    # time if the two ever coexisted — asserted here rather than assumed.
    if frozen and (masters.calendar.machine_busy or masters.calendar.operator_busy):
        raise ValueError(
            "occupancy and frozen operations cannot be combined: a second-stage "
            "plan never has in-progress work"
        )

    # Consolidation (transparent): if a window is set, merge same-item nearby-due orders
    # into batches, schedule the batches, then map each batch's completion back onto its
    # original orders — so the caller still sees per-original-order completions.
    if getattr(config, "consolidation_window", 0) and config.consolidation_window > 0:
        return _decode_consolidated(orders, sequence, masters, config, dispatch, frozen)

    order_by_key = {o.key: o for o in orders}
    priority = {key: i for i, key in enumerate(sequence)}

    # Per-order progress: which operation is next, and when it can start (= end of the
    # order's previous operation; starts at plan_start).
    ops_of: dict[tuple[str, str], tuple[Operation, ...]] = {}
    idx_of: dict[tuple[str, str], int] = {}
    ready_of: dict[tuple[str, str], datetime] = {}
    # prev_end tracks the true completion of the order's last-scheduled op, used to
    # PACE the next op (an op can never finish before its predecessor). With overlap,
    # ready_of (when the next op may start) is earlier than prev_end.
    prev_end_of: dict[tuple[str, str], datetime] = {}
    for key in sequence:
        order = order_by_key[key]
        ops_of[key] = masters.routings[order.item_code].operations
        idx_of[key] = 0
        ready_of[key] = config.plan_start
        prev_end_of[key] = config.plan_start

    # What each machine is already committed to, as (start, end) spans of whole
    # operations, kept sorted. Until 2026-09-22 this was ONE "free from" datetime per
    # machine: once a job was committed late for its own routing reasons, every idle
    # hour in front of it was unreachable for work that was ready and staffable the
    # whole time (the owner's "machine free, operator free, nothing scheduled").
    # A ready operation now takes the EARLIEST stretch of the machine it fits in
    # whole (never split around another job, so a setup is never paid twice).
    machine_spans: dict[str, list[tuple[datetime, datetime]]] = {
        mid: [] for mid in masters.machines}
    staffing = StaffingBoard(build_machine_pools(masters),
                             booked=masters.calendar.operator_busy,
                             assigned=masters.calendar.machine_shift_operator)
    segments: list[Segment] = []
    completion: dict[tuple[str, str], datetime] = {}

    if frozen:
        segments.extend(_preplace_frozen(
            frozen, order_by_key, ops_of, idx_of, ready_of, prev_end_of,
            machine_spans, staffing, completion, masters, config))

    # Orders that still have operations left to schedule.
    remaining = [key for key in sequence if idx_of[key] < len(ops_of[key])]

    guard = 0
    guard_max = sum(len(ops_of[k]) for k in sequence) + 1
    while remaining:
        guard += 1
        if guard > guard_max:  # every loop schedules exactly one op — this can't be hit
            raise RuntimeError("scheduler made no progress (internal error)")

        # Evaluate the next op of every remaining order (board read read-only — each
        # placement carries the staffing assignments it would make, committed below
        # only for the chosen op).
        placements = {
            key: _place_operation(
                ops_of[key][idx_of[key]], order_by_key[key], ready_of[key],
                machine_spans, staffing, masters, config,
            )
            for key in remaining
        }

        if dispatch == "nondelay":
            # Legacy: schedule whichever op can START earliest; sequence breaks ties.
            key = min(remaining, key=lambda k: (placements[k]["start"], priority[k], k))
        else:
            # Giffler-Thompson: the critical op is the one finishing earliest; its
            # machine m* is the contested resource. Among ops that want m* and could
            # start before that completion, the order sequence picks the winner. Ops
            # with no machine (OS/dispatch) never contend — schedule them directly.
            crit = min(remaining, key=lambda k: (placements[k]["end"], priority[k], k))
            m_star = placements[crit]["machine_id"]
            if m_star is None:
                key = crit
            else:
                c_star = placements[crit]["end"]
                conflict = [
                    k for k in remaining
                    if placements[k]["machine_id"] == m_star and placements[k]["start"] < c_star
                ]
                key = min(conflict, key=lambda k: (priority[k], k)) if conflict else crit

        placement = placements[key]
        # Piece-flow guard (2026-07-25 spec): a starved fast op must not finish its WORK
        # before its predecessor delivered the last piece — else the machine-wise schedule
        # processes pieces before they exist ("deburring skipped for the last jobs"). Re-lay
        # it later (batch-at-end) so its work ends >= the predecessor's completion. Block
        # model kept: same machine, same operator rule, same occupancy — just placed later.
        if placement["machine_id"] is not None and placement["end"] < prev_end_of[key]:
            # Push the op's START forward by the shortfall (based on its ACTUAL start,
            # which already sits at the machine's free time — bumping `ready` alone
            # wouldn't move an op pinned behind a busy machine). Re-lay until its work
            # ends >= the predecessor's completion; a few passes absorb shift/day gaps.
            for _ in range(8):
                _r = placement["start"] + (prev_end_of[key] - placement["end"])
                placement = _place_operation(
                    ops_of[key][idx_of[key]], order_by_key[key], _r,
                    machine_spans, staffing, masters, config)
                if placement["end"] >= prev_end_of[key]:
                    break
        # Commit the winning placement onto the real state. The machine frees after its
        # actual cutting (placement["end"]) — pacing affects only the ORDER's downstream.
        for machine_id, day, shift, name, seg_start, seg_end in placement["assignments"]:
            staffing.commit(machine_id, day, shift, name, seg_start, seg_end)
        if placement["machine_id"] is not None:
            _occupy(machine_spans, placement["machine_id"], placement["start"], placement["end"])
        for seg in placement["segments"]:
            if seg.operator is not None:  # track load for the "balanced" operator pick
                staffing.add_load(seg.operator, (seg.end - seg.start).total_seconds() / 60.0)
        segments.extend(placement["segments"])

        just = ops_of[key][idx_of[key]]                       # the op just scheduled
        paced_end = max(placement["end"], prev_end_of[key])   # never finish before predecessor
        prev_end_of[key] = paced_end
        idx_of[key] += 1

        if idx_of[key] >= len(ops_of[key]):
            # Order finished — completion is the paced end of its last op (dispatch).
            completion[key] = paced_end
            remaining.remove(key)
        else:
            nxt = ops_of[key][idx_of[key]]
            ready_of[key] = _ready_after(order_by_key[key], just, nxt,
                                         placement["start"], paced_end, config)

    return Schedule(tuple(segments), completion)


def _decode_consolidated(
    orders: list[Order],
    sequence: list[tuple[str, str]],
    masters: Masters,
    config: PlanConfig,
    dispatch: str,
    frozen=None,
) -> Schedule:
    """Decode with order consolidation: schedule merged batches, then expand each
    batch's completion onto the original orders it covers."""
    from dataclasses import replace

    from ppc_engine.consolidation import consolidate

    batches, expand = consolidate(orders, config.consolidation_window)
    orig_to_batch = {mk: bkey for bkey, members in expand.items() for mk in members}

    # Batch sequence = the batches in the order their members first appear in `sequence`.
    seen: set = set()
    batch_seq: list = []
    for key in sequence:
        bkey = orig_to_batch[key]
        if bkey not in seen:
            seen.add(bkey)
            batch_seq.append(bkey)

    # Schedule the batches with consolidation OFF (avoid infinite recursion).
    sub = decode(batches, batch_seq, masters, replace(config, consolidation_window=0.0), dispatch, frozen)

    # A batch's completion is every covered order's completion.
    completion: dict[tuple[str, str], datetime] = {}
    for bkey, members in expand.items():
        end = sub.completion.get(bkey)
        if end is not None:
            for mk in members:
                completion[mk] = end
    return Schedule(sub.segments, completion)


def _occupy(spans, mid, start, end):
    """Record a whole operation's span on ``mid`` (kept sorted by start)."""
    lst = spans.setdefault(mid, [])
    lst.append((start, end))
    lst.sort()


def _free_runs(mid, after, spans, calendar):
    """The stretches of ``mid``'s time, on/after ``after``, that hold no committed
    operation of THIS plan and no occupancy of an earlier planning stage
    (``calendar.machine_busy``, the Add New Orders quote), in time order; the last
    run is open-ended (``None``). A job is laid inside ONE run and never across two:
    crossing a boundary would mean the machine was torn down for another job in
    between, and the setup is not paid twice."""
    busy = list(spans.get(mid, ())) + list(calendar._merged_busy(mid))
    busy.sort()
    runs: list[tuple[datetime, datetime | None]] = []
    cursor = after
    for start, end in busy:
        if end <= cursor:
            continue
        if start > cursor:
            runs.append((cursor, start))
        cursor = max(cursor, end)
    runs.append((cursor, None))
    return runs


def _place_operation(
    op: Operation,
    order: Order,
    ready: datetime,
    machine_spans: dict[str, list],
    staffing: StaffingBoard,
    masters: Masters,
    config: PlanConfig,
) -> dict:
    """Work out where/when ``op`` would run if scheduled next for ``order``.

    Returns a placement dict: start, end, list[Segment], list of new staffing
    assignments to commit, and the machine_id used (None for OS/dispatch). Reads
    ``machine_free`` and the (already-cloned) ``staffing`` but does not mutate real
    state — the caller commits the chosen placement.
    """
    # Per-operation quantity: on a re-plan, each op runs its OWN remaining (from
    # order.process_remaining); on a fresh plan, the full order qty.
    op_qty = order.qty
    if order.process_remaining is not None:
        op_qty = order.process_remaining.get(op.seq, order.qty)

    if op.kind == OperationKind.DISPATCH:
        # Zero-duration milestone: the order is done at ``ready``.
        seg = Segment(order.key, op.seq, op.name, op.kind, None, None, ready, ready, 0)
        return {"start": ready, "end": ready, "segments": [seg], "assignments": [], "machine_id": None}

    dur = operation_duration_min(op, op_qty, config)

    if op.kind == OperationKind.OUTSOURCED:
        # A fixed off-site lead time (or a zero-time milestone if already done).
        end = ready + timedelta(minutes=dur)
        seg = Segment(order.key, op.seq, op.name, op.kind, None, None, ready, end, int(op_qty))
        return {"start": ready, "end": end, "segments": [seg], "assignments": [], "machine_id": None}

    if dur <= 0:
        # This operation is already finished (re-plan) → a zero-time milestone, no
        # machine/operator, no phantom setup. Successors start right after it.
        seg = Segment(order.key, op.seq, op.name, op.kind, None, None, ready, ready, 0)
        return {"start": ready, "end": ready, "segments": [seg], "assignments": [], "machine_id": None}

    # In-house operation: try each allowed machine, keep the one that finishes soonest
    # (ties → the machine's preference order). "Soonest finish" naturally prefers a
    # free machine over a busy one.
    best = None
    for opt_idx, mid in enumerate(op.machine_options):
        machine = masters.machines.get(mid)
        if machine is None:
            continue  # unknown machine id (provisional handling comes with the loader)
        laid = _lay_around(machine, max(ready, config.plan_start), dur, order, op,
                           int(op_qty), machine_spans, staffing, masters, config)
        if laid is None:
            continue
        cand = (laid["end"], opt_idx)
        if best is None or cand < (best["end"], best["opt_idx"]):
            best = {**laid, "opt_idx": opt_idx, "machine_id": mid}

    if best is None:
        # Fail loud (LESSONS.md / RULES.md) rather than silently drop an operation.
        raise RuntimeError(
            f"cannot schedule op '{op.name}' (seq {op.seq}) of order {order.key}: "
            f"no runnable machine among {op.machine_options}"
        )
    return {
        "start": best["start"],
        "end": best["end"],
        "segments": best["segments"],
        "assignments": best["assignments"],
        "machine_id": best["machine_id"],
    }


# A stretch of a person's free time is only worth starting the machine for if it
# holds at least this much work (or finishes the operation). Below it, the machine
# would be started for a sliver and stopped again — noise, not capacity.
_MIN_STRETCH_MIN = 30.0


def _next_stretch(machine, win, at, win_end, remaining, staffing, masters, config,
                  preferred=None):
    """The earliest stretch of [at, win_end) in which SOME qualified person on this
    shift is free for at least ``min(_MIN_STRETCH_MIN, remaining)`` minutes, as
    ``(start, end, name)``, or None when nobody can run the machine again this window.

    Candidates are the machine's own shift operator (``preferred``, or the board's
    record of who last manned it this shift) and every pool member on this shift
    who is not on leave. The EARLIEST stretch wins; on a tie the machine's own
    operator keeps it (stability), then scarce-first, then name — the board's own
    order. Availability is the board's committed bookings, so a person doing a
    short job elsewhere is simply unavailable for those minutes, and available
    again after: the machine pauses for them instead of losing the whole shift.
    """
    need = min(_MIN_STRETCH_MIN, remaining)
    owner = preferred or staffing.operator_for(machine.id, win.shift_date, win.shift)
    eligible = staffing.eligible(machine.id, win.shift_date, win.shift, masters, config)
    names = []
    if owner is not None:
        names.append((owner, -1))          # rank -1: wins every tie
    names.extend((n, i) for i, n in enumerate(eligible) if n != owner)
    best = None
    for name, rank in names:
        for s, e in staffing.free_stretches(name, at, win_end):
            if (e - s).total_seconds() / 60.0 + _EPS_MIN < need:
                continue
            cand = (s, rank, e, name)
            if best is None or cand < best:
                best = cand
            if s <= at:
                return s, e, name          # free from the start: nothing can beat it
            break                          # first qualifying stretch is the earliest
    if best is None:
        return None
    s, _rank, e, name = best
    return s, e, name


def _lay_windows(machine, earliest, dur_min, order, op, op_qty, staffing, masters,
                 config, deadline=None, preferred=None, preferred_ok=None,
                 partial=False):
    """Lay ``dur_min`` minutes of ``op`` on ``machine`` from ``earliest``, window by
    window, in the free stretches of whoever is qualified and on shift.

    The one placement loop both the main decode and the frozen pre-placement use.
    Within a window the work is laid stretch by stretch: the machine's operator
    runs it while they are free, the machine PAUSES while they are booked
    elsewhere (a short job), and it resumes when they are back — or, if they are
    gone for the rest of the window, whoever qualified is free takes over. Before
    2026-09-22 a window was refused outright unless ONE person was free for the
    whole remaining stretch from its start, and a ten-minute job elsewhere threw
    away eleven hours of machine time; measured on the owner's books, that alone
    was 1,300 to 1,750 idle hours per plan with work waiting.

    ``preferred`` / ``preferred_ok(win)`` name the frozen path's planned operator
    and whether they may still man the machine in a given window. ``deadline`` is
    a stop line: the work must finish on or before it, or None is returned meaning
    "not in this stretch of time" — the stage-2 occupancy rule (Add New Orders
    quote) and, for a MACHINING op, the end of the machine's current free run,
    since a CNC/VMC job is one engagement and is never split around another job
    (the setup would be paid twice). With ``partial=True`` (MANUAL / INSPECTION
    work, which has no setup to lose) whatever fits before the deadline is laid and
    the rest is reported as ``remaining`` for the caller to continue in the
    machine's next free run — the job runs AROUND the other jobs on the station,
    the way a helper does.

    Returns the placement (start, end, segments, assignments) or None if the work
    can't be completed within the lookahead horizon.
    """
    cursor = earliest
    remaining = dur_min
    segments: list[Segment] = []
    assignments: list[tuple] = []
    first_start: datetime | None = None

    for win in iter_windows(machine, earliest, masters.calendar, config):
        if remaining <= _EPS_MIN:
            break
        if deadline is not None and win.start >= deadline:
            break        # out of room in this stretch; the caller tries the next one
        win_end = win.end if deadline is None else min(win.end, deadline)
        at = max(cursor, win.start)
        pref = preferred if (preferred and (preferred_ok is None or preferred_ok(win))) else None
        while at < win_end and remaining > _EPS_MIN:
            pick = _next_stretch(machine, win, at, win_end, remaining, staffing,
                                 masters, config, preferred=pref)
            if pick is None:
                break                      # nobody can run it again this window
            s, e, name = pick
            take = min((e - s).total_seconds() / 60.0, remaining)
            seg_end = s + timedelta(minutes=take)
            assignments.append((machine.id, win.shift_date, win.shift, name, s, seg_end))
            segments.append(Segment(order.key, op.seq, op.name, op.kind, machine.id, name,
                                    s, seg_end, op_qty))
            if first_start is None:
                first_start = s
            remaining -= take
            at = seg_end
            pref = name                    # whoever runs it now keeps it this shift
        cursor = win.end

    if first_start is None or (remaining > _EPS_MIN and not partial):
        return None  # unschedulable within this stretch / the lookahead horizon
    return {"start": first_start, "end": segments[-1].end, "segments": segments,
            "assignments": assignments, "remaining": remaining}


def _lay_around(machine, earliest, dur, order, op, op_qty, machine_spans, staffing,
                masters, config, planned_operator=None):
    """Lay ``op`` on ``machine`` given what the machine is already committed to.

    MACHINING (CNC/VMC): one engagement in the earliest free run that holds it
    WHOLE — a job is never split around another job, or the 90-minute setup would
    be paid twice. Runs shorter (wall-clock) than the work are skipped without a
    window walk. MANUAL / INSPECTION: no setup to lose, so the job runs AROUND the
    jobs already on the station — as much as fits in each free run, continuing in
    the next (it pauses while another job occupies the station, exactly as it
    pauses while its helper is booked elsewhere). ``planned_operator`` is the frozen
    path's pin (see ``_lay_frozen``)."""
    lay = (functools.partial(_lay_frozen, planned_operator=planned_operator)
           if planned_operator is not None else _lay_on_machine)
    runs = _free_runs(machine.id, earliest, machine_spans, masters.calendar)
    # A machine that carries occupancy from an EARLIER planning stage (the Add New
    # Orders quote, 2026-09-08 spec) keeps that stage's gap rule for every kind of
    # work: a new job takes a gap only if it fits WHOLE and never straddles a job
    # the existing plan already runs — the quote's verifier compares whole spans
    # and rejects the straddle, and the owner rejected splitting a new job across
    # gaps. Inside the ordinary plan (no occupancy) manual work runs around jobs.
    whole = (op.kind == OperationKind.MACHINING
             or bool(masters.calendar._merged_busy(machine.id)))
    if whole:
        for run_start, run_end in runs:
            if run_end is not None and (run_end - run_start).total_seconds() / 60.0 < dur:
                continue
            laid = lay(machine, run_start, dur, order, op, op_qty, staffing, masters,
                       config, deadline=run_end)
            if laid is not None:
                return laid
        return None
    segments, assignments, remaining, first = [], [], dur, None
    for run_start, run_end in runs:
        got = lay(machine, run_start, remaining, order, op, op_qty, staffing, masters,
                  config, deadline=run_end, partial=True)
        if got is None:
            continue
        segments.extend(got["segments"])
        assignments.extend(got["assignments"])
        remaining = got["remaining"]
        first = first if first is not None else got["start"]
        if remaining <= _EPS_MIN:
            return {"start": first, "end": segments[-1].end, "segments": segments,
                    "assignments": assignments}
    return None


def _lay_on_machine(
    machine: Machine,
    earliest: datetime,
    dur_min: float,
    order: Order,
    op: Operation,
    op_qty: int,
    staffing: StaffingBoard,
    masters: Masters,
    config: PlanConfig,
    deadline: datetime | None = None,
    partial: bool = False,
) -> dict | None:
    """Lay ``dur_min`` minutes of work for ``op`` onto ``machine`` from ``earliest``
    (see ``_lay_windows``). ``staffing`` is read here, never mutated — new
    assignments accumulate in the returned list and are committed only by the
    caller for the placement actually chosen, so this may be called repeatedly
    against the same board (as ``_lay_around`` does, once per candidate run)."""
    return _lay_windows(machine, earliest, dur_min, order, op, op_qty, staffing,
                        masters, config, deadline=deadline, partial=partial)


def _lay_in_free_run(machine, earliest, dur_min, order, op, op_qty, staffing,
                     masters, config):
    """Lay the op in the first stretch of ``machine``'s time (as an earlier planning
    stage left it, ``ShopCalendar.machine_busy``) that can hold it WHOLE. The
    stage-2 gap rule in isolation; decode itself goes through ``_lay_around`` so
    this plan's own commitments count too."""
    for run_start, run_end in _free_runs(machine.id, earliest, {}, masters.calendar):
        laid = _lay_on_machine(machine, run_start, dur_min, order, op, op_qty,
                               staffing, masters, config, deadline=run_end)
        if laid is not None:
            return laid
    return None


def _lay_frozen(machine, earliest, dur_min, order, op, op_qty, staffing, masters,
                config, deadline=None, partial=False, planned_operator=None):
    """Lay a frozen (in-progress) op onto its PINNED machine from ``earliest``.
    Prefer the planned operator in every window they may still man this machine;
    otherwise staff whoever qualified is free (``_lay_windows``). The machine is
    fixed and no setup is charged (already set up mid-run)."""
    operators_by_name = {o.name: o for o in masters.operators}
    planned = operators_by_name.get(planned_operator) if planned_operator else None

    def _ok(win):
        # The pinned operator must STILL be assigned to this machine in Settings.
        # Without this, an admin who removed a machine from someone while they had
        # work in progress got them frozen straight back onto it on the next re-plan
        # (the live "Sidhu Singe on CNC5" bug, 2026-08-03). The machine pin stays;
        # only the person is re-staffed. They must also be rostered on THIS shift
        # and not on leave that day.
        return (planned is not None
                and machine.id in planned.qualified_machines
                and effective_shift(planned, win.shift_date, config) == win.shift
                and masters.calendar.is_operator_available(planned_operator, win.shift_date))

    return _lay_windows(machine, earliest, dur_min, order, op, op_qty, staffing,
                        masters, config, deadline=deadline, partial=partial,
                        preferred=planned_operator if planned else None,
                        preferred_ok=_ok)


def _ready_after(order, just, nxt, start, paced_end, config, *,
                 qty=None, setup_min=None):
    """When the NEXT operation of an order may start, given the one just placed.

    THE one definition of the routing gate, shared by the main loop and the frozen
    pre-placement below. Overlap (Rule 5) lets the next op begin once this one is
    ``overlap`` through cutting, but only between two in-house ops — OS and dispatch
    stay fully sequential. Never later than this op actually finished.

    It is a shared function on purpose: the two callers used to disagree, and the
    frozen path having no routing gate at all is what let an in-progress step be
    pinned before the step feeding it (live 2026-08-09).

    ``qty`` / ``setup_min`` override what the op actually cost, and the frozen caller
    MUST pass them: a resumed op is already set up, so `_preplace_frozen` charges
    `remaining_qty * cycle` and no setup. Taking the defaults there added 90 min of
    CNC setup nobody spends to every in-progress op's successor — measured the same
    day the gate shipped, while attributing a live rise in late-days."""
    if nxt is None:
        return paced_end
    just_qty = qty if qty is not None else (
        order.process_remaining.get(just.seq, order.qty)
        if order.process_remaining is not None else order.qty)
    if config.overlap > 0 and just.kind in _INHOUSE and nxt.kind in _INHOUSE and just_qty > 0:
        setup = (setup_min if setup_min is not None
                 else (config.setup_min if just.kind == OperationKind.MACHINING else 0.0))
        cutting = just_qty * just.cycle_min
        release = start + timedelta(minutes=setup + (1.0 - config.overlap) * cutting)
        return min(release, paced_end)
    return paced_end


def _lay_pinned(machine, earliest, dur, order, op, qty, planned_operator, machine_spans,
                staffing, masters, config):
    """A frozen step on its pinned machine: the same rule as any other step
    (``_lay_around``), preferring the planned operator."""
    return _lay_around(machine, earliest, dur, order, op, qty, machine_spans, staffing,
                       masters, config, planned_operator=planned_operator or "")


def _preplace_frozen(frozen, order_by_key, ops_of, idx_of, ready_of, prev_end_of,
                     machine_spans, staffing, completion, masters, config):
    """Pin every in-progress op onto its machine+operator BEFORE the main loop.

    Frozen ops resume in previous-plan (``prev_start``) order — but an op is never
    placed until every frozen step AHEAD OF IT IN ITS OWN ROUTING has been placed,
    and its start is gated by the owning order's ``ready_of`` exactly as in the main
    loop, with the same piece-flow guard on its end.

    That gate is the 2026-08-09 fix. Before it, frozen ops were grouped BY MACHINE and
    each laid at ``machine_free[machine]`` with no reference to the order at all, so a
    free machine ran a later step days before a busy machine could run the step that
    feeds it: on the real book, 63 inversions across 21 of 68 orders — CNC SECOND SIDE,
    VMC, DEBURING and INSP all running before CNC FIRST SIDE. Checked, not assumed, by
    `new_engine.routing_order_violations`.

    The machine's free time still advances past each frozen op, so new work queues
    after it. Returns the frozen segments."""
    from collections import defaultdict
    seq_index = {k: {op.seq: i for i, op in enumerate(ops_of[k])} for k in ops_of}

    todo = []
    for fo in frozen:
        if fo.machine_id not in masters.machines:
            continue            # machine gone from masters — not frozen (schedule normally)
        if order_by_key.get(fo.order_key) is None:
            continue
        oi = seq_index.get(fo.order_key, {}).get(fo.op_seq)
        if oi is None:
            continue
        todo.append((fo, oi))
    todo.sort(key=lambda t: (t[0].prev_start, t[0].order_key, t[0].op_seq))

    frozen_pos = defaultdict(set)          # order -> routing positions that are frozen
    for fo, oi in todo:
        frozen_pos[fo.order_key].add(oi)
    placed = defaultdict(set)

    out: list[Segment] = []
    while todo:
        # Previous-plan order, restricted to ops whose own frozen predecessors are down.
        pick = next((t for t in todo
                     if all(j in placed[t[0].order_key]
                            for j in frozen_pos[t[0].order_key] if j < t[1])), None)
        if pick is None:
            # Previous-plan order and routing order disagree. Routing wins: it is
            # physics, the other is only a preference.
            pick = min(todo, key=lambda t: (t[1], t[0].prev_start))
        todo.remove(pick)
        fo, oi = pick
        key = fo.order_key
        placed[key].add(oi)

        order = order_by_key[key]
        op = ops_of[key][oi]
        dur = fo.remaining_qty * op.cycle_min          # no setup on resume
        if dur <= 0:
            continue
        mid = fo.machine_id
        machine = masters.machines[mid]
        qty = int(fo.remaining_qty)
        # The order's OWN predecessor gates the start, not just the machine's queue;
        # the machine's earliest free run that holds the step whole takes it.
        laid = _lay_pinned(machine, max(ready_of[key], config.plan_start), dur, order,
                           op, qty, fo.operator, machine_spans, staffing, masters, config)
        if laid is None:
            continue  # unstaffable — leave to the main loop
        # Piece-flow guard, identical in spirit to the main loop's: a fast op must not
        # finish its work before its predecessor delivered the last piece. Push it
        # later by the shortfall; a few passes absorb shift and day gaps.
        for _ in range(8):
            if laid["end"] >= prev_end_of[key]:
                break
            shifted = _lay_pinned(machine,
                                  laid["start"] + (prev_end_of[key] - laid["end"]),
                                  dur, order, op, qty, fo.operator, machine_spans,
                                  staffing, masters, config)
            if shifted is None:
                break
            laid = shifted
        for a in laid["assignments"]:
            staffing.commit(*a)
        _occupy(machine_spans, mid, laid["start"], laid["end"])
        for seg in laid["segments"]:
            if seg.operator is not None:
                staffing.add_load(seg.operator,
                                  (seg.end - seg.start).total_seconds() / 60.0)
        out.extend(laid["segments"])

        paced_end = max(laid["end"], prev_end_of[key])
        prev_end_of[key] = paced_end
        idx_of[key] = max(idx_of[key], oi + 1)
        nxt = ops_of[key][idx_of[key]] if idx_of[key] < len(ops_of[key]) else None
        ready_of[key] = max(
            ready_of[key],
            # A resumed op is already set up: it was laid as `remaining_qty * cycle`
            # with no setup, so its successor must not be charged one either.
            _ready_after(order, op, nxt, laid["start"], paced_end, config,
                         qty=fo.remaining_qty, setup_min=0.0))
        if nxt is None:
            completion[key] = prev_end_of[key]
    return out
