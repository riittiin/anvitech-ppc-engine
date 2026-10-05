"""The app-owned Machines table and holiday list (spec
docs/superpowers/specs/2026-10-05-shop-masters-no-excel-design.md).

Pure: no store, no HTTP. Rows are seeded once from the workbook through the SAME
sheet readers the ppc loader uses, cell values preserved, so both loaders read them
exactly as before. Dicts keep insertion order. Weekly off is Thursday (the engine
enforces it; not stored). Leave rows are never seeded: Operator absences own leave.
"""

from __future__ import annotations

import copy
import hashlib
import io
import json
import math
from datetime import date

from engine.loaders import normalize_resource_id

WEEKLY_OFF = "Thursday"
TWO_SHIFT_HOURS = 19.5
ONE_SHIFT_HOURS = 9.5
HOLIDAY_YEARS = 5


class VersionConflict(Exception):
    """The machine changed since it was loaded (or already exists on create)."""


def _jsonable(v):
    return v if v is None or isinstance(v, (bool, int, float, str)) else str(v)


def _with_sheet(raw, sheet, reader):
    from ppc_engine.loaders.workbook import find_sheet, open_workbook
    wb = open_workbook(io.BytesIO(raw))
    try:
        try:
            find_sheet(wb, sheet)
        except KeyError:
            return None
        return reader(wb)
    finally:
        wb.close()


def _machine_sheet_rows(raw):
    from ppc_engine.loaders.masters_loader import machine_rows_from_sheet
    return _with_sheet(raw, "Machine master", machine_rows_from_sheet)


def _holiday_sheet_rows(raw):
    from ppc_engine.loaders.masters_loader import holiday_rows_from_sheet
    return _with_sheet(raw, "Weekly off & holiday master", holiday_rows_from_sheet)


def seed_machines(raw: bytes, now_iso: str):
    rows = _machine_sheet_rows(raw)
    if rows is None:
        return None
    machines: dict = {}
    for no, type_raw, hours in rows:
        if no is None or str(no).strip() == "":
            continue
        mid = normalize_resource_id(no)
        if not mid:
            continue
        machines[mid] = {"name": str(no).strip(), "type": str(type_raw or "").strip(),
                         "hours": _jsonable(hours), "version": 1}
    doc = {"seeded_at": now_iso, "seeded_from_sha": hashlib.sha256(raw).hexdigest(),
           "machines": machines}
    doc["seed_digest"] = machines_digest(doc)
    return doc


def seed_calendar(raw: bytes, now_iso: str):
    rows = _holiday_sheet_rows(raw)
    if rows is None:
        return None
    doc = {"seeded_at": now_iso, "seeded_from_sha": hashlib.sha256(raw).hexdigest(),
           "holidays": [{"date": d.isoformat(), "name": name} for d, name in rows]}
    doc["seed_digest"] = calendar_digest(doc)
    return doc


def machine_rows(doc) -> list:
    return [(m["name"], m.get("type"), m.get("hours")) for m in doc.get("machines", {}).values()]


def holiday_rows(doc) -> list:
    return [(date.fromisoformat(h["date"]), h.get("name") or "") for h in doc.get("holidays", [])]


def _hash(blob) -> str:
    return hashlib.sha256(json.dumps(blob, sort_keys=True, default=str).encode()).hexdigest()


def machines_digest(doc) -> str:
    if not doc:
        return "none"
    return _hash([[mid, m.get("name"), m.get("type"), m.get("hours")]
                  for mid, m in doc.get("machines", {}).items()])


def calendar_digest(doc) -> str:
    if not doc:
        return "none"
    return _hash([[h["date"], h.get("name")] for h in doc.get("holidays", [])])


def validate_machine(mid: str, item: dict, existing: dict, create: bool) -> list:
    errs = []
    raw = str(mid or "").strip()
    if any(sep in raw for sep in "/,"):
        errs.append("Enter one machine number at a time.")
    canon = normalize_resource_id(raw) if raw else ""
    if not canon:
        errs.append("The machine number cannot be blank.")
    elif canon == "OS":
        errs.append("OS means outsourced and cannot be a machine number.")
    elif create and canon in existing:
        errs.append(f"Machine {canon} already exists.")
    if not str(item.get("type") or "").strip():
        errs.append("Pick a machine type.")
    hours = item.get("hours")
    if (isinstance(hours, bool) or not isinstance(hours, (int, float))
            or not math.isfinite(hours) or not 0 < hours <= 24):
        errs.append("Working hours a day must be more than 0 and at most 24.")
    return errs


def validate_holiday(d: str, name: str, existing: list, today: date) -> list:
    errs = []
    try:
        day = date.fromisoformat(str(d))
    except ValueError:
        return ["Enter a real date."]
    if abs((day - today).days) > HOLIDAY_YEARS * 366:
        errs.append(f"The date must be within {HOLIDAY_YEARS} years of today.")
    if not str(name or "").strip():
        errs.append("Give the holiday a name.")
    if any(h["date"] == day.isoformat() for h in existing):
        errs.append(f"{day.strftime('%d-%m-%Y')} is already a holiday.")
    return errs


def machine_usage(mid, item_doc, operator_table, downtime_rows, frozen_rows) -> list:
    """Everything that still names this machine, in plain words (empty = free)."""
    from ppc_engine.loaders.normalize import parse_machine_options
    out = []
    for code, it in (item_doc or {}).get("items", {}).items():
        for s in it.get("steps", []):
            if mid in (set(parse_machine_options(s.get("allotted")))
                       | set(parse_machine_options(s.get("suggested")))):
                out.append(f"item {code} step {s.get('name')}")
    for r in (operator_table or {}).get("operators", []):
        if mid in parse_machine_options(r.get("machines_raw")):
            out.append(f"operator {r.get('name')}")
    for r in downtime_rows or []:
        if r.get("machine") == mid:
            out.append(f"a maintenance break from {r.get('from_date')} to {r.get('to_date')}")
    for r in frozen_rows or []:
        if r.get("machine") == mid:
            out.append(f"work in progress on {r.get('so_no')} ({r.get('process')})")
    return out


def apply_machine_save(doc, mid, item, expected_version, now_iso, user, create) -> dict:
    out = copy.deepcopy(doc) if doc else {"machines": {}}
    machines = out.setdefault("machines", {})
    cur = machines.get(mid)
    if create:
        if cur is not None:
            raise VersionConflict(f"Machine {mid} already exists.")
        version = 1
    else:
        if cur is None or cur.get("version") != expected_version:
            raise VersionConflict("Someone else changed this machine since you opened it. "
                                  "Reload to see their change.")
        version = cur["version"] + 1
    machines[mid] = {"name": item.get("name") or mid, "type": str(item.get("type")).strip(),
                     "hours": item.get("hours"), "version": version,
                     "updated_at": now_iso, "updated_by": user}
    return out


def add_holiday(doc, d, name, now_iso) -> dict:
    out = copy.deepcopy(doc) if doc else {"holidays": []}
    out.setdefault("holidays", []).append({"date": date.fromisoformat(d).isoformat(),
                                           "name": str(name).strip()})
    out["holidays"].sort(key=lambda h: h["date"])
    return out


def remove_holiday(doc, d) -> dict:
    out = copy.deepcopy(doc) if doc else {"holidays": []}
    keep = [h for h in out.get("holidays", []) if h["date"] != d]
    if len(keep) == len(out.get("holidays", [])):
        raise KeyError(d)
    out["holidays"] = keep
    return out
