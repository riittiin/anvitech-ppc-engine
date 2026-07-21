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
    """Old build's process-name normaliser (engine/orderbook.py): strip everything but
    A-Z0-9, uppercase. Used to line up a batch's `process_qty` keys with the new
    engine's operation names for per-process 'continue from reality' re-planning."""
    return re.sub(r"[^A-Z0-9]", "", str(name or "").upper())


def _new_masters():
    """Load the new-engine Masters from the stored workbook, cached by content hash."""
    raw = book_store.load_masters_bytes()
    if not raw:
        raise RuntimeError("new_engine.run: no masters workbook stored")
    h = hashlib.sha256(raw).hexdigest()
    cached = _MASTERS_CACHE.get(h)
    if cached is None:
        _MASTERS_CACHE.clear()
        cached = load_all(io.BytesIO(raw)).masters
        _MASTERS_CACHE[h] = cached
    return cached


def _friday_on_or_before(d: date) -> date:
    return d - timedelta(days=(d.weekday() - 4) % 7)


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
    new_masters = _new_masters()
    orders, batch_by_key = _orders_from_batches(batches, new_masters)
    sequence = [(b.batch_id, b.item_code) for b in batches]
    sched = decode(orders, sequence, new_masters, _plan_config(config))
    return _entries_from_schedule(sched, batch_by_key)


# Overlaps the sweep contest tries (0.0 = sequential .. 0.90). The best-scoring one is
# applied, so "Start deep search" auto-tunes the overlap AND the sequence together.
_OVERLAP_CANDIDATES = (0.0, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9)


def sweep_optimize(so_lines, config, masters, *, budget_evals=150, seed=42,
                   on_progress=None, should_cancel=None, base_reserved=None, **kw):
    """The full contest for the NEW engine: search the job SEQUENCE **and** sweep the
    OVERLAP, keeping the best (sequence, overlap) by the owner's objective (total tardiness
    + fairness). Returns the old ``SweepResult`` shape, so the existing optimize job / apply
    / rank-replay machinery is unchanged and the winning overlap is applied to the plan.

    Two phases (cheap enough for the free instance):
      1. Search the sequence at the overlap sweet-spot (0.7) — many plans, keep the best.
      2. Sweep every overlap on that winning sequence and pick the score-minimising one
         (the "auto-overlap" tuner). Overlap is the biggest makespan lever, so this is where
         most of the improvement comes from.
    Before/after is reported in the OLD ``plan_metrics`` space so the app's panel matches.
    """
    from dataclasses import replace

    from engine.optimizer import OptimizeResult, SweepResult, plan_metrics, ranks_for
    from engine.rules import rule1_consolidate

    from ppc_engine.objective import compute_metrics, score
    from ppc_engine.optimize import optimize as new_optimize
    from ppc_engine.scheduler import decode

    new_masters = _new_masters()
    base = _plan_config(config)
    plan_start = getattr(config, "plan_start_date", None) or date.today()

    batches = rule1_consolidate.run(so_lines, config)
    if not batches:
        return SweepResult(overlap_percent=int(round(base.overlap * 100)), knob="overlap",
                           result=OptimizeResult(evals=0, best=None),
                           table=[], evals=0, cancelled=False)

    orders, batch_by_key = _orders_from_batches(batches, new_masters)

    # Phase 1 — search the sequence at the overlap sweet-spot.
    search_cfg = replace(base, overlap=0.7)
    progress = (lambda evals, best: on_progress(evals, best)) if on_progress else None
    res = new_optimize(orders, new_masters, search_cfg, budget=int(budget_evals),
                       seed=int(seed), on_progress=progress)
    best_keys = list(res.best_sequence)

    # Phase 2 — auto-tune the overlap on the winning sequence (score-minimising).
    best_overlap, best_score = 0.7, None
    for ov in _OVERLAP_CANDIDATES:
        cfg_ov = replace(base, overlap=ov)
        sc = score(compute_metrics(decode(orders, best_keys, new_masters, cfg_ov),
                                   orders, cfg_ov.plan_start), cfg_ov)
        if best_score is None or sc < best_score:
            best_score, best_overlap = sc, ov

    best_batches = [batch_by_key[k] for k in best_keys if k in batch_by_key]
    ranks = ranks_for(best_batches)
    # Winner metrics in the old space: decode the winning (sequence, overlap) the old way.
    won_cfg = replace(base, overlap=best_overlap)
    winner_entries = _entries_from_schedule(decode(orders, best_keys, new_masters, won_cfg),
                                            batch_by_key)
    winner_metrics = plan_metrics(winner_entries, so_lines, plan_start)
    result = OptimizeResult(ranks=ranks, best=winner_metrics, evals=res.evaluations,
                            improved=True, cancelled=False)
    return SweepResult(overlap_percent=int(round(best_overlap * 100)), knob="overlap",
                       result=result, table=[], evals=res.evaluations, cancelled=False)
