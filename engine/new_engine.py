"""Adapter — run the NEW scheduling engine behind the old build's scheduler seam.

The old build turns prioritized batches into a schedule through ONE dispatch point
(engine/pipeline.py `scheduler_for` -> `run(...)`). This module provides a `run` with the
SAME contract that internally drives the new operator-stable engine (vendored as the
`ppc_engine` package) and maps its output back to the old `ScheduleEntry` list — so the
entire old UI (Gantt, Analytics, Schedule, Orders, Capture-Actuals) keeps working
unchanged while the *scheduling brain* is the new one.

Why this is safe: the new loaders produce byte-identical machine ids / item codes /
operator names to the old loaders on the same workbook (verified: 26/26 machines, 98/98
routings, 19/19 operators). So the new engine's output references exactly the ids the old
view layer looks up. We therefore load the new masters natively from the stored workbook
(no masters translation) and only bridge three things:
    demand : old Batch   -> new Order
    config : old Config  -> new PlanConfig
    output : new Segment -> old ScheduleEntry (one entry per operation; the new engine's
             per-shift Segment split becomes the entry's `op_segments`).
"""

from __future__ import annotations

import hashlib
import io
import re
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from engine import book_store
from engine.loaders import normalize_process_name
from engine.models import ScheduleEntry

from ppc_engine.config import PlanConfig
from ppc_engine.domain.order import Order
from ppc_engine.domain.routing import OperationKind
from ppc_engine.loaders import load_all
from ppc_engine.scheduler import decode

# The lane strings the old Gantt/Analytics expect for non-machine steps.
_OS_LANE = "OS / Outsourced"
_OFF_LANE = "Off-machine"

# New masters parsed from the stored workbook, cached by content hash so the workbook is
# parsed once per upload (the optimizer replays `run` many times).
_MASTERS_CACHE: dict = {}


def _norm(name) -> str:
    """Canonical process-name key. MUST be the EXACT same normaliser the order book
    uses to key a batch's ``process_qty`` (``engine.loaders.normalize_process_name``:
    collapse whitespace + uppercase, but KEEP word breaks — 'cnc  first side' ->
    'CNC FIRST SIDE'). A different rule (e.g. stripping spaces) silently drops every
    multi-word step from ``process_remaining``, so re-plans after production would
    ignore finished progress on those steps and re-schedule them at full qty."""
    return normalize_process_name(name)


# Workbook bytes injected out-of-band (the cloud optimize worker has no store — it carries
# the workbook in its payload). When set, it wins over book_store; the web app leaves it None.
_OVERRIDE_BYTES = None


def set_masters_bytes(raw: bytes | None) -> None:
    """Feed the new engine the masters workbook directly (used by the cloud worker, which
    runs from a payload rather than the store). Pass None to clear and fall back to the store."""
    global _OVERRIDE_BYTES
    _OVERRIDE_BYTES = raw


def _new_masters():
    """Load the new-engine Masters from the injected bytes or the stored workbook, cached."""
    raw = _OVERRIDE_BYTES if _OVERRIDE_BYTES is not None else book_store.load_masters_bytes()
    if not raw:
        raise RuntimeError("new_engine: no masters workbook available (store empty and none injected)")
    h = hashlib.sha256(raw).hexdigest()
    cached = _MASTERS_CACHE.get(h)
    if cached is None:
        _MASTERS_CACHE.clear()
        cached = load_all(io.BytesIO(raw)).masters
        _MASTERS_CACHE[h] = cached
    return cached


def _friday_on_or_before(d: date) -> date:
    return d - timedelta(days=(d.weekday() - 4) % 7)


def _with_absences(masters, reserved):
    """Return a per-plan copy of the new-engine Masters with the app's operator absences
    folded into the calendar's per-operator leave, so an absent operator is never assigned.
    ``reserved`` is the old ``{operator -> [(start_dt, end_dt), ...]}`` from
    optimize_service.absence_reservations. Cached masters are never mutated."""
    from dataclasses import replace as _replace
    if not reserved:
        return masters
    extra: dict[str, set] = {}
    for op, intervals in reserved.items():
        days: set = set()
        for start, end in intervals:
            d = start.date()
            while d < end.date():
                days.add(d)
                d += timedelta(days=1)
        if days:
            extra[op] = days
    if not extra:
        return masters
    cal = masters.calendar
    merged = dict(getattr(cal, "leaves", {}) or {})
    for op, days in extra.items():
        merged[op] = frozenset(merged.get(op, frozenset()) | days)
    return _replace(masters, calendar=_replace(cal, leaves=merged))


def _plan_config(config) -> PlanConfig:
    """Translate the old Config into a new PlanConfig (shift hours, setup, overlap,
    plan start). Consolidation is 0 — the old Rule 1 already consolidated the batches."""
    start = getattr(config, "plan_start_date", None) or date.today()
    h = int(getattr(config, "first_shift_start_hour", 8))
    # The new engine's overlap is always a fraction (0.0 = sequential). Read it straight
    # from overlap_percent, IGNORING the old 'overlap_mode' switch (the new engine has no
    # such mode) — otherwise a tuned or saved overlap silently has no effect. Clamp 0..0.95.
    overlap = min(0.95, max(0.0, float(getattr(config, "overlap_percent", 0)) / 100.0))
    return PlanConfig(
        plan_start=datetime(start.year, start.month, start.day, h, 0),
        week_anchor=_friday_on_or_before(start),
        first_start=time(int(getattr(config, "first_shift_start_hour", 8)), 0),
        first_end=time(int(getattr(config, "first_shift_end_hour", 19)), 0),
        second_start=time(int(getattr(config, "first_shift_end_hour", 19)), 0),
        second_end=time(int(getattr(config, "second_shift_end_hour", 5)), 0),
        setup_min=float(getattr(config, "setup_time_min", 90)),
        overlap=overlap,
        consolidation_window=0.0,
    )


def _orders_from_batches(batches, masters):
    """Old Batch[] -> new Order[], plus an order-key -> batch index for mapping back.

    Each batch becomes one Order keyed by (batch_id, item_code) — unique per batch. A
    batch's per-process remaining (`process_qty`, keyed by normalised process name) is
    mapped to the new engine's `process_remaining` (keyed by operation seq) via the
    routing, so a partially-produced order re-plans each step at its own remaining."""
    orders, batch_by_key = [], {}
    for b in batches:
        # An item with no routing is not schedulable — skip it (it still shows in the order
        # book / validation report). The classic engine tolerated this; the new engine must
        # too, or one unrouted order would crash the whole plan (KeyError on the routing).
        if b.item_code not in masters.routings:
            continue
        key = (b.batch_id, b.item_code)
        process_remaining = None
        if getattr(b, "process_qty", None):
            routing = masters.routings.get(b.item_code)
            if routing:
                process_remaining = {
                    op.seq: int(round(b.process_qty[_norm(op.name)]))
                    for op in routing.operations
                    if _norm(op.name) in b.process_qty
                }
        orders.append(Order(
            so_no=b.batch_id, item_code=b.item_code, item_name=b.item_name,
            qty=int(round(b.qty)), due_date=b.so_delivery_date,
            process_remaining=process_remaining,
        ))
        batch_by_key[key] = b
    return orders, batch_by_key


def _machine_for(kind, machine_id) -> str:
    """The old Gantt/Analytics machine field: canonical id for in-house steps, the OS
    lane for outsourced, the off-machine lane for a resource-less in-house step."""
    if kind == OperationKind.OUTSOURCED:
        return _OS_LANE
    if machine_id:
        return machine_id
    return _OFF_LANE


def _entries_from_schedule(sched, batch_by_key):
    """New Segment[] -> old ScheduleEntry[]. One entry per operation; the new engine's
    per-shift segments become the entry's `op_segments` (the per-shift operator
    hand-offs the old Analytics reads). The DISPATCH milestone is dropped — the old view
    derives completion from the last real operation's end."""
    groups = defaultdict(list)
    for s in sched.segments:
        groups[(s.order_key, s.op_seq)].append(s)

    entries = []
    for (order_key, op_seq), segs in groups.items():
        segs = sorted(segs, key=lambda s: s.start)
        first = segs[0]
        if first.kind == OperationKind.DISPATCH:
            continue
        batch = batch_by_key.get(order_key)
        start = min(s.start for s in segs)
        end = max(s.end for s in segs)
        occupancy_min = sum((s.end - s.start).total_seconds() for s in segs) / 60.0
        operator = next((s.operator for s in segs if s.operator), "")
        # OS steps are single milestone entries (no per-shift operator segments).
        op_segments = ([] if first.kind == OperationKind.OUTSOURCED
                       else [(s.start, s.end, s.operator or "") for s in segs])
        entries.append(ScheduleEntry(
            batch_id=order_key[0],
            item_code=order_key[1],
            process_seq=op_seq,
            process_name=first.op_name,
            machine=_machine_for(first.kind, first.machine_id),
            qty=float(first.qty),
            occupancy_min=occupancy_min,
            start=start,
            end=end,
            notes="",
            so_refs=list(batch.source_so_refs) if batch else [],
            operator=operator,
            op_segments=op_segments,
        ))
    entries.sort(key=lambda e: (e.start, e.batch_id, e.process_seq))
    return entries


# A fingerprint the old API mixes into its plan-staleness signature (api/main.py:315),
# so switching engines correctly invalidates any cached plan.
SCHEDULER_FINGERPRINT = "new-engine-v1"


def run(batches, config=None, notes=None, masters=None, machine_lost_min=None,
        reserved=None, **kw):
    """Scheduler seam contract: prioritized `batches` -> list[ScheduleEntry], via the new
    operator-stable engine.

    The batches arrive already in priority order (Rules 1-3, or a saved optimization's
    rank map), so that order IS the sequence handed to the new decoder — the new engine
    schedules them in exactly the priority the rest of the pipeline decided.
    """
    if not batches:
        return []
    new_masters = _with_absences(_new_masters(), reserved)
    orders, batch_by_key = _orders_from_batches(batches, new_masters)
    if not orders:
        return []
    # Sequence from the ROUTED orders only (unrouted batches were skipped above), preserving
    # the incoming priority order.
    sequence = [o.key for o in orders]
    sched = decode(orders, sequence, new_masters, _plan_config(config))
    return _entries_from_schedule(sched, batch_by_key)


def optimize_sequence(so_lines, config, masters, *, reserved=None, budget_evals=150,
                      seed=42, on_progress=None, should_cancel=None):
    """Sequence-only search for the NEW engine at the config's overlap. The cloud contest
    sweeps overlaps EXTERNALLY (one candidate per overlap) and calls this per candidate, so
    across candidates it becomes the full overlap × sequence contest — just distributed and
    at scale on GitHub Actions. Returns the old OptimizeResult so the contest/apply machinery
    is unchanged."""
    from engine.optimizer import OptimizeResult, plan_metrics, ranks_for
    from engine.rules import rule1_consolidate

    from ppc_engine.optimize import optimize as new_optimize

    nm = _with_absences(_new_masters(), reserved)
    cfg = _plan_config(config)
    plan_start = getattr(config, "plan_start_date", None) or date.today()
    batches = rule1_consolidate.run(so_lines, config)
    if not batches:
        return OptimizeResult()
    orders, batch_by_key = _orders_from_batches(batches, nm)
    # Report EVERY plan (on_eval), not just improvements, so the live counter climbs steadily.
    prog = (lambda evals, _sc: on_progress(evals, None)) if on_progress else None
    res = new_optimize(orders, nm, cfg, budget=int(budget_evals), seed=int(seed), on_eval=prog)
    best_batches = [batch_by_key[k] for k in res.best_sequence if k in batch_by_key]
    ranks = ranks_for(best_batches)
    winner_metrics = plan_metrics(run(best_batches, config, masters), so_lines, plan_start)
    return OptimizeResult(ranks=ranks, best=winner_metrics, evals=res.evaluations,
                          improved=True, cancelled=False)


def tune(so_lines, config, masters, *, budget_per_eval=150, seed=42, on_step=None, reserved=None):
    """The CONTINUOUS overlap optimizer + sequence search (the 'atom optimizer').

    Golden-section search over the overlap value: it treats "best plan score achievable at
    overlap x" as a function of x and homes in on its true minimum — a CONTINUOUS value like
    0.78 / 0.82 / 0.913, not a fixed grid point. Each probe runs the full sequence search
    (comparing many plans), so it optimizes the overlap AND the job order together.

    ``on_step(cumulative_plans, best_score_so_far)`` is fired after every plan for a live
    tracker. Returns ``(ranks, best_overlap_percent, winner_metrics_old_space, plans)``.
    """
    from dataclasses import replace

    from engine.optimizer import plan_metrics, ranks_for
    from engine.rules import rule1_consolidate

    from ppc_engine.optimize import tune_overlap
    from ppc_engine.scheduler import decode

    new_masters = _with_absences(_new_masters(), reserved)
    base = _plan_config(config)
    plan_start = getattr(config, "plan_start_date", None) or date.today()

    batches = rule1_consolidate.run(so_lines, config)
    if not batches:
        return {}, int(round(base.overlap * 100)), {}, 0
    orders, batch_by_key = _orders_from_batches(batches, new_masters)

    tr = tune_overlap(orders, new_masters, base, lo=0.5, hi=0.95, seeds=(int(seed),),
                      budget_per_eval=int(budget_per_eval), tol=0.01, coarse=5, on_step=on_step)

    best_batches = [batch_by_key[k] for k in tr.best_sequence if k in batch_by_key]
    ranks = ranks_for(best_batches)
    # Report metrics at the APPLIED (integer-%) overlap, not the continuous best, so the
    # before/after panel matches the plan the app actually re-plans (no overstated gain).
    overlap_pct = int(round(tr.best_overlap * 100))
    won_cfg = replace(base, overlap=overlap_pct / 100.0)
    winner_metrics = plan_metrics(
        _entries_from_schedule(decode(orders, tr.best_sequence, new_masters, won_cfg), batch_by_key),
        so_lines, plan_start)
    return ranks, overlap_pct, winner_metrics, tr.evaluations


def sweep_optimize(so_lines, config, masters, *, budget_evals=150, seed=42,
                   on_progress=None, should_cancel=None, base_reserved=None, **kw):
    """Local (in-process) fallback for 'Start deep search': the same continuous golden-section
    tune as the cloud, at a smaller budget so it finishes on the free instance. Returns the old
    ``SweepResult`` shape so the optimize/apply/replay machinery is unchanged. ``on_progress``
    is fed EVERY plan (with the best-so-far metrics) so the app's counter tracks real work."""
    from engine.optimizer import OptimizeResult, SweepResult

    state = {"best": {}}

    def _step(plans, best_score):
        if on_progress:
            on_progress(plans, state["best"])

    # Split the (capped) budget across ~12 golden-section probes.
    per = max(15, int(budget_evals) // 10)
    ranks, overlap_pct, metrics, plans = tune(so_lines, config, masters,
                                              budget_per_eval=per, seed=seed, on_step=_step,
                                              reserved=base_reserved)
    state["best"] = metrics or {}
    if not ranks:
        return SweepResult(overlap_percent=overlap_pct, knob="overlap",
                           result=OptimizeResult(evals=0, best=None), table=[], evals=0, cancelled=False)
    result = OptimizeResult(ranks=ranks, best=metrics, evals=plans, improved=True, cancelled=False)
    return SweepResult(overlap_percent=overlap_pct, knob="overlap",
                       result=result, table=[], evals=plans, cancelled=False)
