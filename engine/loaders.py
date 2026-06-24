"""Read Test2.xlsx (read-only) into typed Python objects + a validation report.

Design principles honoured here (CLAUDE.md):
  * Test2.xlsx is opened read-only; nothing is ever written back.
  * Loader-level data gaps are NON-BLOCKING: collect every problem into
    ``masters.report`` and keep going (PENDING_MASTER_DATA, NO_ROUTING, time
    coercions). The pipeline never stops here.

Sheet-name gotchas (trailing spaces, an apostrophe, a misspelling) are handled
by ``_find_sheet`` which matches on a normalized name.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

import openpyxl

from .models import (
    SOLine,
    Process,
    Routing,
    Machine,
    Operator,
    WorkCalendar,
    Masters,
)

DEFAULT_XLSX = Path(__file__).resolve().parent.parent / "Test2.xlsx"

# Number of process blocks in the routing sheet, and the 5 columns per block.
MAX_PROCESSES = 12
ROUTING_FIRST_PROCESS_COL = 12  # 0-based col of "Process 1"


# --------------------------------------------------------------------------- #
# Normalization helpers
# --------------------------------------------------------------------------- #
def normalize_resource_id(raw) -> str:
    """Canonical machine/resource id: uppercase, alnum only.

    Collapses the master's spaced labels ('CNC 4', 'VMC 1') and the routing's
    compact labels ('CNC4', 'VMC1') onto the same key, so they match without a
    hand-maintained alias table.
    """
    if raw is None:
        return ""
    return re.sub(r"[^A-Z0-9]", "", str(raw).upper())


def parse_resource_candidates(raw) -> list:
    """Canonical machine ids a routing cell allows, in order (first = preferred).

    A "Suggested M/c" cell may list ALTERNATIVES separated by '/', ',', '&' or ' or '
    (e.g. 'CNC3/CNC6' = run on either CNC3 or CNC6). Returns an ordered, deduped list
    of normalized ids; empty/None -> []. The same split is used for operators."""
    if raw is None:
        return []
    out = []
    for token in re.split(r"[/&,]| or ", str(raw)):
        cid = normalize_resource_id(token)
        if cid and cid not in out:
            out.append(cid)
    return out


def parse_date(value):
    """Coerce a cell to a ``date``. Handles datetime cells and 'dd/mm/yyyy'
    strings (the SO sheet mixes both). Returns None if unparseable."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    s = str(value).strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _norm_sheet(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(name).lower())


def _find_sheet(wb, wanted: str):
    """Locate a sheet by normalized name (tolerates trailing spaces, the
    apostrophe in "Item's process Master", and the 'anayasis' misspelling)."""
    target = _norm_sheet(wanted)
    for ws in wb.worksheets:
        if _norm_sheet(ws.title) == target:
            return ws
    # Fall back to a contains-match for the misspelled analysis sheet etc.
    for ws in wb.worksheets:
        if target in _norm_sheet(ws.title) or _norm_sheet(ws.title) in target:
            return ws
    return None


def _num(value, masters: Masters = None, ref: str = ""):
    """Coerce a time/number cell to float; log a coercion if it was a string."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        coerced = float(str(value).strip())
        if masters is not None:
            masters.add_report(
                "TIME_COERCION", ref, f"coerced {value!r} -> {coerced}"
            )
        return coerced
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Individual sheet loaders
# --------------------------------------------------------------------------- #
def _load_machines(wb, masters: Masters):
    ws = _find_sheet(wb, "Machine master")
    if ws is None:
        masters.add_report("MISSING_SHEET", "Machine master", "sheet not found")
        return
    for row in ws.iter_rows(min_row=4, values_only=True):
        # Layout: (None, Machine Type, Machine No., Hr rate)
        machine_type, machine_no, hr_rate = row[1], row[2], row[3]
        if not machine_no:
            continue  # blank separator row
        canonical = normalize_resource_id(machine_no)
        if not canonical:
            continue
        masters.machines[canonical] = Machine(
            machine_no=canonical,
            display_name=str(machine_no).strip(),
            machine_type=str(machine_type).strip() if machine_type else "",
            hr_rate=_num(hr_rate),
            provisional=False,
        )


def _load_operators(wb, masters: Masters):
    ws = _find_sheet(wb, "Operator & shift Master")
    if ws is None:
        return
    for row in ws.iter_rows(min_row=4, values_only=True):
        name, pref = row[1], row[2]
        if not name:
            continue
        label = str(name).strip()
        if label.lower().startswith("shift master"):
            break  # reached the shift section
        parsed = [normalize_resource_id(t) for t in re.split(r"[/&,]| or ", str(pref or ""))]
        parsed = [p for p in parsed if p]
        masters.operators.append(
            Operator(name=label, preferred_machines_raw=str(pref or "").strip(), machines=parsed)
        )


def _load_calendar(wb, masters: Masters):
    ws = _find_sheet(wb, "Weekly off & holiday master")
    cal = WorkCalendar()
    if ws is None:
        masters.calendar = cal
        return
    section = None
    for row in ws.iter_rows(values_only=True):
        label, when = row[1], row[2]
        if isinstance(label, str):
            low = label.strip().lower()
            if low.startswith("weekly off"):
                section = "weekly"
            elif low.startswith("holiday"):
                section = "holiday"
            elif low.startswith("leave"):
                section = "leave"
        d = parse_date(when)
        if d is None:
            continue
        if section == "holiday":
            cal.holidays.append(d)
        elif section == "leave":
            cal.leaves.append((str(label).strip() if label else "", d))
    masters.calendar = cal


def _load_routings(wb, masters: Masters):
    ws = _find_sheet(wb, "Item's process Master")
    if ws is None:
        masters.add_report("MISSING_SHEET", "Item's process Master", "sheet not found")
        return
    for row in ws.iter_rows(min_row=3, values_only=True):
        item_code = row[3]
        if item_code is None or str(item_code).strip() == "":
            continue  # blank / separator row
        code = str(item_code).strip()
        processes = []
        for p in range(MAX_PROCESSES):
            base = ROUTING_FIRST_PROCESS_COL + p * 5
            name = row[base] if base < len(row) else None
            if not name or str(name).strip() == "":
                continue
            processes.append(
                Process(
                    seq=p + 1,
                    name=str(name).strip(),
                    cycle_time=_num(row[base + 1], masters, f"{code} P{p+1} cycle"),
                    total_time=_num(row[base + 2], masters, f"{code} P{p+1} total"),
                    suggested_machine=(str(row[base + 3]).strip() if row[base + 3] else None),
                    allotted_machine=(str(row[base + 4]).strip() if row[base + 4] else None),
                )
            )
        masters.routings[code] = Routing(
            item_code=code,
            description=str(row[2]).strip() if row[2] else "",
            customer=str(row[1]).strip() if row[1] else "",
            rm_type=str(row[6]).strip() if row[6] else "",
            moq=_num(row[10]),
            processes=processes,
        )


def _load_so_lines(wb, masters: Masters):
    ws = _find_sheet(wb, "Sales Order (SO) list")
    if ws is None:
        masters.add_report("MISSING_SHEET", "Sales Order (SO) list", "sheet not found")
        return []
    so_lines = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        item_code = row[19]
        if item_code is None or str(item_code).strip() == "":
            continue
        delivery = parse_date(row[23])
        if delivery is None:
            masters.add_report(
                "BAD_DELIVERY_DATE", str(row[5]), f"unparseable delivery date {row[23]!r}"
            )
            continue
        so_lines.append(
            SOLine(
                so_no=str(row[5]).strip() if row[5] else "",
                item_code=str(item_code).strip(),
                item_name=str(row[20]).strip() if row[20] else "",
                qty=_num(row[21]) or 0.0,
                delivery_date=delivery,
                pending_qty=_num(row[27]),
                customer=str(row[8]).strip() if row[8] else "",
                remarks=str(row[24]).strip() if row[24] else "",
            )
        )
    return so_lines


# --------------------------------------------------------------------------- #
# Validation (non-blocking)
# --------------------------------------------------------------------------- #
def _register_provisional(masters: Masters, raw_label: str):
    """Ensure a resource referenced by a routing exists. If it isn't in the
    Machine master, register it as a PROVISIONAL machine and report it once."""
    canonical = normalize_resource_id(raw_label)
    if not canonical:
        return
    if canonical in masters.machines:
        return
    masters.machines[canonical] = Machine(
        machine_no=canonical,
        display_name=str(raw_label).strip(),
        machine_type="(provisional — fill in Machine master)",
        hr_rate=None,
        provisional=True,
    )
    masters.add_report(
        "PENDING_MASTER_DATA",
        canonical,
        f"resource '{raw_label}' used by a routing but not in Machine master; "
        f"registered as provisional — add it to the Excel master to complete it",
    )


def _validate(masters: Masters, so_lines):
    # PENDING_MASTER_DATA: every resource a routing names must resolve.
    for routing in masters.routings.values():
        for proc in routing.processes:
            for raw in (proc.suggested_machine, proc.allotted_machine):
                # A cell may list alternatives ("CNC3/CNC6") — register EACH so a
                # missing one (e.g. VMC3) becomes its own provisional machine, not a
                # merged bogus id.
                for cid in parse_resource_candidates(raw):
                    _register_provisional(masters, cid)

    # NO_ROUTING: SO item codes with no recipe — report and (caller) skip them.
    seen = set()
    for so in so_lines:
        if so.item_code not in masters.routings and so.item_code not in seen:
            seen.add(so.item_code)
            masters.add_report(
                "NO_ROUTING",
                so.item_code,
                f"SO item '{so.item_code}' has no routing in Item's process Master; "
                f"order skipped (cannot schedule without a recipe)",
            )


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def load_all(xlsx_path=DEFAULT_XLSX):
    """Load masters + SO lines from Test2.xlsx.

    Returns ``(so_lines, masters)``. ``masters.report`` holds all non-blocking
    issues. SO lines whose item has no routing are dropped from the returned
    list (recorded as NO_ROUTING) so downstream rules only see schedulable demand.
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    masters = Masters()
    try:
        _load_machines(wb, masters)
        _load_operators(wb, masters)
        _load_calendar(wb, masters)
        _load_routings(wb, masters)
        so_lines = _load_so_lines(wb, masters)
    finally:
        wb.close()

    _validate(masters, so_lines)

    schedulable = [so for so in so_lines if so.item_code in masters.routings]
    return schedulable, masters
