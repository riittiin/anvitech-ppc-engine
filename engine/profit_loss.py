"""Planned machine minutes per order, read from a plan.

Pure and reporting-only: no store, no HTTP, never touches a plan. Spec:
docs/superpowers/specs/2026-11-11-profit-and-loss-design.md (sections 6 and 8).

Only CNC/VMC (machining) and inspection steps are counted. The plan pads CNC/VMC
cycle times (``planning_time``); the figures here undo that padding so they compare
with the Excel (original) cycle time. Setup is paid once per operation and shared
between the lines of a clubbed batch by pieces.
"""

from __future__ import annotations

from engine.loaders import normalize_process_name
from engine.models import Actual
from engine.production_analysis import cycle_time_for
from engine.planning_time import CNC_VMC_PLANNING_FACTOR, planning_cycle_time
from ppc_engine.loaders.normalize import machine_kind_from_type

COUNTED_KINDS = {"machining", "inspection"}


def machine_kinds(machines_doc: dict) -> dict:
    """Machine id -> kind value ("machining" | "inspection" | "manual")."""
    out = {}
    for mid, row in ((machines_doc or {}).get("machines") or {}).items():
        out[mid] = machine_kind_from_type((row or {}).get("type")).value
    return out


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def planned_steps(schedule, batches, masters, config, kinds) -> dict:
    """(so_no, item_code) -> [{process, step, machine, setup_min, run_min}], counted
    steps only."""
    by_batch = {b.batch_id: b for b in batches or []}
    ops: dict = {}
    for e in schedule or []:
        if kinds.get(e.machine) not in COUNTED_KINDS:
            continue
        key = (e.batch_id, e.process_seq, bool(getattr(e, "resumed", False)))
        op = ops.get(key)
        if op is None:
            ops[key] = {"first": e, "occ": float(e.occupancy_min)}
        else:
            op["occ"] += float(e.occupancy_min)

    out: dict = {}
    for op in ops.values():
        e = op["first"]
        occ, q = op["occ"], float(e.qty)
        kind = kinds[e.machine]
        step = normalize_process_name(e.process_name)
        proc = None
        routing = getattr(masters, "routings", {}).get(e.item_code)
        for p in (routing.processes if routing else []):
            if normalize_process_name(p.name) == step:
                proc = p
                break
        co = proc.cycle_time if proc is not None and _num(proc.cycle_time) else None
        cp = planning_cycle_time(proc, config) if proc is not None else None
        setup_op = max(0.0, occ - q * cp) if _num(cp) else 0.0

        batch = by_batch.get(e.batch_id)
        lpq = getattr(batch, "line_process_qty", None) if batch else None
        lines = list(e.piece_refs or e.so_refs)
        pieces = {}
        for so in lines:
            if lpq and len(lpq) > 1 and so in lpq:
                qty, pq = lpq[so]
                pieces[so] = float(pq.get(step, qty) if pq else qty)
            else:
                pieces[so] = q
        total = sum(pieces.values())
        for so in lines:
            share = pieces[so] / total if total > 0 else 1.0 / len(lines)
            if co is not None:
                run = pieces[so] * co
            elif kind == "machining":
                run = (occ - setup_op) * share / CNC_VMC_PLANNING_FACTOR
            else:
                run = (occ - setup_op) * share
            out.setdefault((so, e.item_code), []).append({
                "process": proc.name if proc is not None else e.process_name,
                "step": step, "machine": e.machine,
                "setup_min": setup_op * share, "run_min": run,
            })
    return out


def merge_snapshot(stored: dict, fresh: dict, started: set, now_iso: str) -> dict:
    """Refresh the planned-minutes snapshot of every order NOT yet started; a started
    order's snapshot is frozen (never written, never created). Orders that left the
    plan (completed) keep what they had."""
    out = dict(stored or {})
    for (so, item), steps in (fresh or {}).items():
        if (so, item) in started:
            continue
        out[f"{so}\x1f{item}"] = {"steps": steps, "at": now_iso}
    return out


def actual_minutes(actuals, masters, kinds, working) -> dict:
    """(so_no, item_code) -> [{step, process, machine, minutes}] from the floor punches.

    A punch's minutes are its shift minutes (capped at the shift's working minutes)
    less its downtime; setup stays in. Punches sharing one (machine, date, shift) that
    together claim more than the shift can hold share it by pieces*cycle + setup.
    A punch with no machine is flagged (``machine`` "", ``minutes`` None).
    """
    items = [a for a in actuals or [] if (getattr(a, "shift_minutes", 0.0) or 0.0) > 0]
    groups: dict = {}
    for i, a in enumerate(items):
        if a.machine:
            groups.setdefault((a.machine, a.entry_date, (a.shift or "").strip()), []).append(i)

    hours = {}
    for (mid, day, shift), idx in groups.items():
        cap = working.get(shift) if working else None
        hs = {i: min(items[i].shift_minutes, cap) if cap is not None else items[i].shift_minutes
              for i in idx}
        if cap is not None and sum(hs.values()) > cap:
            ws = {}
            for i in idx:
                a = items[i]
                co = cycle_time_for(masters, a.item_code, a.process)
                if co is None:
                    co = getattr(a, "cycle_time_min", None)
                if not _num(co):
                    co = 0.0
                ws[i] = (a.qty_produced or 0.0) * co + (a.actual_setup_min or 0.0)
            tot = sum(ws.values())
            hs = {i: cap * (ws[i] / tot if tot > 0 else 1.0 / len(idx)) for i in idx}
        hours.update(hs)

    out: dict = {}
    for i, a in enumerate(items):
        step = normalize_process_name(a.process)
        if not a.machine:
            out.setdefault(a.key, []).append(
                {"step": step, "process": a.process, "machine": "", "minutes": None})
            continue
        if kinds.get(a.machine) not in COUNTED_KINDS:
            continue
        down = sum(getattr(a, f, 0.0) or 0.0 for f, _ in Actual.DOWNTIME_FIELDS)
        m = max(0.0, hours[i] - down)
        if m > 0:
            out.setdefault(a.key, []).append(
                {"step": step, "process": a.process, "machine": a.machine, "minutes": m})
    return out
