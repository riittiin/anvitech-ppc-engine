"""The app-owned Item Process Master: item routings, edited in the app instead of
the Excel (spec docs/superpowers/specs/2026-10-05-item-process-master-tab-design.md).

Pure: no store, no HTTP. Steps are stored as the RAW text the Excel cells held
(name, cycle, allotted, suggested), so what kind of step it is (machining, manual,
inspection, outsourced, dispatch) keeps being decided in ONE place,
``ppc_engine.loaders.normalize.classify_operation``, on the same inputs. The
``items`` dict keeps insertion order (seed = sheet order, new items appended): the
routing dict's order reaches the scheduler, so it is never sorted.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json

from engine.loaders import normalize_process_name
from ppc_engine.domain.routing import OperationKind
from ppc_engine.loaders.normalize import classify_operation, parse_machine_options

MAX_STEPS = 12


class VersionConflict(Exception):
    """The item changed since the caller loaded it (or already exists on create)."""


def _jsonable(v):
    return v if v is None or isinstance(v, (bool, int, float, str)) else str(v)


def _sheet_rows(raw: bytes):
    """The workbook's routing sheet as rows, or None when it has no such sheet."""
    from ppc_engine.loaders.masters_loader import routing_rows_from_sheet
    from ppc_engine.loaders.workbook import find_sheet, open_workbook
    wb = open_workbook(io.BytesIO(raw))
    try:
        try:
            find_sheet(wb, "Item's process Master", "Items process Master")
        except KeyError:        # find_sheet raises when no sheet matches
            return None
        return routing_rows_from_sheet(wb)
    finally:
        wb.close()


def seed_doc(raw: bytes, now_iso: str):
    """The one-time copy of a workbook's routing sheet, cell text preserved. None
    when the workbook has no routing sheet (nothing to seed from)."""
    rows = _sheet_rows(raw)
    if rows is None:
        return None
    items: dict = {}
    for code, desc, steps in rows:
        items[code] = {
            "description": desc, "version": 1, "updated_at": now_iso, "updated_by": "seed",
            "steps": [{"name": n, "cycle": _jsonable(c), "allotted": _jsonable(a),
                       "suggested": _jsonable(s)} for n, c, _t, s, a in steps],
        }
    doc = {"seeded_at": now_iso,
           "seeded_from_sha": hashlib.sha256(raw).hexdigest(),
           "items": items}
    doc["seed_digest"] = digest(doc)
    return doc


def routing_rows(doc: dict) -> list:
    """The table as the loaders' rows ``[(code, description, steps)]``."""
    return [(code, it.get("description") or "",
             [(s["name"], s.get("cycle"), None, s.get("suggested"), s.get("allotted"))
              for s in it.get("steps", [])])
            for code, it in doc.get("items", {}).items()]


def digest(doc) -> str:
    """Content hash: codes (in order), descriptions, steps. Bookkeeping excluded."""
    if not doc:
        return "none"
    blob = [[code, it.get("description") or "", it.get("steps", [])]
            for code, it in doc.get("items", {}).items()]
    return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()


def step_info(step: dict) -> dict:
    """How the planner will read this step, for display. Uses the planner's own
    classifier on the stored text."""
    cyc = step.get("cycle")
    cyc_f = float(cyc) if isinstance(cyc, (int, float)) else 0.0
    kind = classify_operation(step.get("name"), step.get("suggested"), step.get("allotted"), cyc_f)
    if kind == OperationKind.DISPATCH:
        return {"kind": "dispatch", "machining": False, "machines": []}
    if kind == OperationKind.OUTSOURCED:
        return {"kind": "outsourced", "machining": False, "machines": []}
    machines = list(parse_machine_options(step.get("allotted")) or
                    parse_machine_options(step.get("suggested")))
    return {"kind": "machine", "machining": kind == OperationKind.MACHINING, "machines": machines}


def _tokens(item) -> set:
    out = set()
    for s in (item or {}).get("steps", []):
        out.update(parse_machine_options(s.get("allotted")))
        out.update(parse_machine_options(s.get("suggested")))
    return out


def validate_item(code: str, item: dict, machine_ids: set, old_item) -> list:
    """Every reason this item cannot be saved, in plain words (empty = fine)."""
    errs = []
    if not str(code or "").strip():
        errs.append("The item code cannot be blank.")
    steps = item.get("steps") or []
    if not steps:
        errs.append("An item needs at least one step.")
    if len(steps) > MAX_STEPS:
        errs.append(f"An item can have at most {MAX_STEPS} steps.")
    allowed_unknown = _tokens(old_item)
    seen = {}
    for i, s in enumerate(steps, start=1):
        name = str(s.get("name") or "").strip()
        if not name:
            errs.append(f"Step {i} needs a name.")
            continue
        key = normalize_process_name(name)
        if key in seen:
            errs.append(f"'{name}' is used twice (steps {seen[key]} and {i}). "
                        f"Give each step a different name.")
        seen.setdefault(key, i)
        cyc = s.get("cycle")
        if cyc is not None and (isinstance(cyc, bool) or not isinstance(cyc, (int, float))):
            errs.append(f"Step {i} ({name}): the cycle time must be a number.")
            continue
        if cyc is not None and cyc < 0:
            errs.append(f"Step {i} ({name}): the cycle time must be 0 or more.")
            continue
        info = step_info(s)
        if info["kind"] == "machine":
            if not info["machines"]:
                errs.append(f"Step {i} ({name}) needs a machine, or mark it outsourced.")
            if not cyc or cyc <= 0:
                errs.append(f"Step {i} ({name}): the cycle time must be more than 0.")
        for mid in (set(parse_machine_options(s.get("allotted")))
                    | set(parse_machine_options(s.get("suggested")))):
            if mid != "OS" and mid not in machine_ids and mid not in allowed_unknown:
                errs.append(f"Step {i} ({name}): {mid} is not a machine in your Machine master.")
    return errs


def punch_safety_errors(code, old_steps, new_steps, orders, actuals) -> list:
    """Refuse an edit that would orphan recorded production: on any OPEN order of
    this item, a step with punches may not be renamed or removed, and the steps
    with punches must keep their order relative to each other."""
    from engine.orderbook import _process_totals
    old_names = [normalize_process_name(s.get("name")) for s in old_steps]
    display = {normalize_process_name(s.get("name")): str(s.get("name")).strip() for s in old_steps}
    new_names = [normalize_process_name(s.get("name")) for s in new_steps]
    errs = []
    for o in orders:
        if o.item_code != code or getattr(o, "completed", False):
            continue
        produced, _good = _process_totals(actuals, o.so_no, o.item_code)
        punched = [n for n in old_names if produced.get(n, 0) > 0]
        missing = [n for n in punched if n not in new_names]
        for n in missing:
            errs.append(f"{display[n]} has {produced[n]:g} punched on {o.so_no}. Finish or "
                        f"complete that order before renaming or removing this step.")
        if not missing and [n for n in new_names if n in punched] != punched:
            errs.append(f"The steps with punches on {o.so_no} "
                        f"({', '.join(display[n] for n in punched)}) must stay in the same order.")
    return errs


def usage(code, orders, drafts) -> list:
    """What still uses this item: open orders, then Add New Orders draft lines."""
    out = [o.so_no for o in orders
           if o.item_code == code and not getattr(o, "completed", False)]
    out += [f"{d.get('so_no')} (Add New Orders draft)" for d in drafts or []
            if d.get("item_code") == code]
    return out


def apply_save(doc, code, item, expected_version, now_iso, user, create) -> dict:
    """A new doc with this item created or replaced. Never mutates ``doc``."""
    out = copy.deepcopy(doc) if doc else {"items": {}}
    items = out.setdefault("items", {})
    cur = items.get(code)
    if create:
        if cur is not None:
            raise VersionConflict(f"An item with code {code} already exists.")
        version = 1
    else:
        if cur is None or cur.get("version") != expected_version:
            raise VersionConflict("Someone else saved this item since you opened it. "
                                  "Reload to see their change.")
        version = cur["version"] + 1
    items[code] = {"description": item.get("description") or "",
                   "steps": [{"name": str(s.get("name")).strip(), "cycle": s.get("cycle"),
                              "allotted": s.get("allotted"),
                              "suggested": s.get("suggested")}
                             for s in item.get("steps", [])],
                   "version": version, "updated_at": now_iso, "updated_by": user}
    return out
