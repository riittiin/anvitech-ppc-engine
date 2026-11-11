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
