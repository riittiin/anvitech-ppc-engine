"""Planned machine minutes per order, read from a plan.

Pure and reporting-only: no store, no HTTP, never touches a plan. Spec:
docs/superpowers/specs/2026-11-11-profit-and-loss-design.md (sections 6 and 8).

Only CNC/VMC (machining) and inspection steps are counted. The plan pads CNC/VMC
cycle times (``planning_time``); the figures here undo that padding so they compare
with the Excel (original) cycle time. Setup is paid once per operation and shared
between the lines of a clubbed batch by pieces.
"""

from __future__ import annotations

from datetime import date

from engine.loaders import normalize_process_name, parse_resource_candidates
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


def rates_from_doc(doc: dict) -> dict:
    """Machine id -> rupees per hour, only for machines that have a rate."""
    out = {}
    for mid, row in ((doc or {}).get("machines") or {}).items():
        r = (row or {}).get("rate")
        if isinstance(r, (int, float)) and not isinstance(r, bool):
            out[mid] = float(r)
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


def planned_snapshot(schedule, batches, masters, config, kinds) -> dict:
    """``planned_steps`` plus an EMPTY step list for every order the plan holds that has
    no counted step at all (manual / outsourced only), so the report can say "no CNC,
    VMC or inspection work" for it instead of calling it an older order."""
    out = {}
    for e in schedule or []:
        for so in list(e.so_refs or []) + list(getattr(e, "piece_refs", None) or []):
            out.setdefault((so, e.item_code), [])
    out.update(planned_steps(schedule, batches, masters, config, kinds))
    return out


def merge_snapshot(stored: dict, fresh: dict, started: set, now_iso: str) -> dict:
    """Refresh the planned-minutes snapshot of every order NOT yet started; a started
    order's snapshot is frozen (never written, never created). Orders that left the
    plan (completed) keep what they had."""
    out = dict(stored or {})
    for (so, item), steps in (fresh or {}).items():
        if (so, item) in started:
            continue
        key = f"{so}\x1f{item}"
        prev = out.get(key)
        if prev is not None and prev.get("steps") == steps:
            continue                      # unchanged: keep the stamp, no store write
        out[key] = {"steps": steps, "at": now_iso}
    return out


def _counted_step(masters, item_code, process, kinds) -> bool:
    """Is this routing step CNC/VMC or inspection work? True when any machine its
    routing allows (Allotted or Suggested, split by the same parser the app uses for
    routing machine text) is a counted kind. A step the routing does not list is
    treated as counted: its time cannot be shown to be outside the report, so an order
    with such a blank-machine punch is held back rather than priced on partial data."""
    routing = (getattr(masters, "routings", None) or {}).get(item_code)
    target = normalize_process_name(process)
    for p in (routing.processes if routing else []):
        if normalize_process_name(p.name) == target:
            cands = (parse_resource_candidates(p.allotted_machine)
                     + parse_resource_candidates(p.suggested_machine))
            return any(kinds.get(m) in COUNTED_KINDS for m in cands)
    return True


def actual_minutes(actuals, masters, kinds, working) -> dict:
    """(so_no, item_code) -> [{step, process, machine, minutes}] from the floor punches.

    A punch's minutes are its shift minutes (capped at the shift's working minutes)
    less its downtime; setup stays in. Punches sharing one (machine, date, shift) that
    together claim more than the shift can hold share it by pieces*cycle + setup.
    A punch with no machine is flagged (``machine`` "", ``minutes`` None) only when its
    step is a COUNTED step (``_counted_step``); the real form saves outsourced, dispatch
    and blank-Allotted manual steps with no machine, and those are dropped silently.
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
            if not _counted_step(masters, a.item_code, a.process, kinds):
                continue
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


def _r2(v):
    return None if v is None else round(v, 2)


def _cost(minutes_by_machine, rates):
    """Cost of {machine: minutes}; None when any machine has no rate."""
    total = 0.0
    for m, mins in minutes_by_machine.items():
        if m not in rates or rates[m] is None:
            return None
        total += mins / 60.0 * rates[m]
    return total


def _priced(by_step, rates):
    """(cost of every machine-minute that HAS a rate, or None when nothing on this
    side is priced; True when some machine on this side has no rate)."""
    total, priced, partial = 0.0, False, False
    for mins_by_machine in by_step:
        for m, mins in mins_by_machine.items():
            if rates.get(m) is None:
                partial = True
            else:
                total += mins / 60.0 * rates[m]
                priced = True
    return (total if priced else None), partial


def _fmt_date(d):
    return d.strftime("%d-%m-%Y") if d else ""


def _step_row(process, planned, actual, rates, missing):
    """planned / actual: {machine: minutes} (planned may be empty)."""
    pc, ac = _cost(planned, rates), _cost(actual, rates)
    ph, ah = sum(planned.values()) / 60.0, sum(actual.values()) / 60.0
    for m in list(planned) + list(actual):
        if rates.get(m) is None:
            missing.add(m)
    if not actual:
        status = "not punched"
    elif pc is None or ac is None:
        status = "rate not set"
    else:
        status = "ok"
    return {"process": process, "planned_machine": "/".join(planned),
            "planned_hours": ph, "planned_cost": pc,
            "actual_machines": list(actual), "actual_hours": ah if actual else None,
            "actual_cost": ac if actual else None, "status": status}


def build_report(completed, snapshot, actual, rates) -> dict:
    """Planned vs actual machine cost per completed order that has a snapshot.

    snapshot: stored doc form {"so\x1fitem": {"steps": [...], "at": iso}};
    actual: {(so, item): [...]} from actual_minutes; rates: {machine: Rs/hour}.
    """
    rows, not_shown, older = [], [], 0
    for o in completed or []:
        doc = (snapshot or {}).get(f"{o.so_no}\x1f{o.item_code}")
        if doc is None:
            older += 1
            continue
        steps = (doc or {}).get("steps") or []
        acts = (actual or {}).get(o.key) or []
        if any(a.get("machine") in ("", None) or a.get("minutes") is None for a in acts):
            not_shown.append({"so_no": o.so_no, "item_code": o.item_code,
                              "reason": "a punch has no machine, so its time cannot be priced"})
            continue
        if not steps and not acts:
            not_shown.append({"so_no": o.so_no, "item_code": o.item_code,
                              "reason": "no CNC, VMC or inspection work"})
            continue
        order_names, plan_by, act_by, label = [], {}, {}, {}
        for s in steps:
            n = s["step"]
            if n not in plan_by:
                order_names.append(n)
                label[n] = s.get("process") or n
            m = plan_by.setdefault(n, {})
            m[s["machine"]] = m.get(s["machine"], 0.0) + (s.get("setup_min") or 0) + (s.get("run_min") or 0)
        for a in acts:
            n = a["step"]
            if n not in plan_by and n not in act_by:
                order_names.append(n)
                label[n] = a.get("process") or n
            m = act_by.setdefault(n, {})
            m[a["machine"]] = m.get(a["machine"], 0.0) + a["minutes"]
        missing, srows, not_punched = set(), [], []
        for n in order_names:
            r = _step_row(label[n], plan_by.get(n, {}), act_by.get(n, {}), rates or {}, missing)
            if r["status"] == "not punched":
                not_punched.append(label[n])
            srows.append(r)
        r_rates = rates or {}
        p_cost, p_part = _priced([plan_by.get(n, {}) for n in order_names], r_rates)
        a_cost, a_part = _priced([act_by[n] for n in order_names if n in act_by], r_rates)
        diff = (None if p_cost is None or a_cost is None or p_part or a_part
                else a_cost - p_cost)
        pct = None if diff is None or not p_cost else diff / p_cost * 100
        rows.append({
            "so_no": o.so_no, "item_code": o.item_code, "item_name": o.item_name,
            "qty": o.ordered_qty, "completed_on": _fmt_date(o.completed_on),
            "_sort": o.completed_on,
            "planned_hours": _r2(sum(r["planned_hours"] for r in srows)),
            "planned_cost": _r2(p_cost), "planned_cost_partial": p_part,
            "actual_hours": _r2(sum(r["actual_hours"] or 0.0 for r in srows)),
            "actual_cost": _r2(a_cost), "actual_cost_partial": a_part, "diff_cost": _r2(diff), "diff_pct": _r2(pct),
            "not_punched": not_punched, "rates_missing": sorted(missing),
            "steps": [{**r, "planned_hours": _r2(r["planned_hours"]),
                       "planned_cost": _r2(r["planned_cost"]),
                       "actual_hours": _r2(r["actual_hours"]),
                       "actual_cost": _r2(r["actual_cost"])} for r in srows],
        })
    rows.sort(key=lambda r: r["_sort"] or date.min, reverse=True)
    for r in rows:
        del r["_sort"]
    full = [r for r in rows if not r["not_punched"] and not r["rates_missing"]
            and r["planned_cost"] is not None and r["actual_cost"] is not None]
    pt = sum(r["planned_cost"] for r in full)
    at = sum(r["actual_cost"] for r in full)
    some = bool(full)                     # no complete order: no total, never a 0
    totals = {"orders": len(rows), "counted": len(full),
              "incomplete": sum(1 for r in rows if r["not_punched"] or r["rates_missing"]),
              "planned_hours": _r2(sum(r["planned_hours"] for r in full)) if some else None,
              "actual_hours": _r2(sum(r["actual_hours"] for r in full)) if some else None,
              "planned_cost": _r2(pt) if some else None,
              "actual_cost": _r2(at) if some else None,
              "diff_cost": _r2(at - pt) if some else None,
              "diff_pct": _r2((at - pt) / pt * 100) if some and pt else None}
    return {"rows": rows, "totals": totals, "not_shown": not_shown, "older_count": older}
