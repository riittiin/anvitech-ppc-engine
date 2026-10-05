# Machines and Holidays in the App, Upload Removed — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the Machine master and the holiday list out of the uploaded Excel into app-owned tables edited in Settings, build every planner's masters from the app's tables without opening the workbook, and remove the Excel upload.

**Architecture:** Two store keys (`anvitech:machines`, `anvitech:shop_calendar`) seeded once from the stored workbook through the same sheet readers the ppc loader uses. Each loader's per-row rule for machines and holidays is extracted and used for both sources (the stage-1 shape), and both `load_all`s accept `machine_rows=` / `holiday_rows=` beside `routing_rows=`; with all three the workbook is not opened. A pure module `engine/shop_masters.py` owns seeding, rows, digests, validation and "what uses this machine". The masters builders, the optimize payload, caches and signatures carry the two new tables exactly as they carry the Item Process Master.

**Tech Stack:** Python 3.12, FastAPI, openpyxl, plain HTML/JS/CSS, pytest.

**Spec:** `docs/superpowers/specs/2026-10-05-shop-masters-no-excel-design.md`

## Global Constraints

- Run every test with `python3.12 -m pytest` (system `python3` is 3.14; openpyxl breaks there). Known pre-existing failure, environment only: `tests/test_production_analysis.py::test_monthly_report_json_and_excel` (xlsxwriter not installed).
- Work on branch `shop-masters` in worktree `~/Desktop/anvitech-ipm`. **Never push.**
- Store keys, exactly: `anvitech:machines`, `anvitech:shop_calendar`.
- Weekly off is Thursday, weekday `3`, not stored, not editable.
- Holiday "Leave" rows are never seeded; Settings > Operator absences owns leave.
- Hour rate is not stored.
- Stored dicts keep insertion order; never sort them (dict order reaches the scheduler).
- Ids and dates travel in JSON bodies, never in URL paths.
- `SCHEDULER_FINGERPRINT` is not bumped. `parse_payload` keeps its 8-tuple.
- Every write endpoint calls `require_admin(request)`; GETs are open to both roles.
- `anvitech:masters` (the stored workbook) is never deleted or overwritten by this work.
- User-visible copy: plain operator English, no em dashes.
- Machine hours: finite, `0 < hours <= 24`. Holiday dates: within 5 years either side of today.

## Review Focus

1. **A blank "Available Hrs/Day" cell in the seeded sheet**: the table must store `null` and both loaders must read it exactly as before (classic `None` → two-shift; ppc default by kind). Pinned in Task 3 (`test_seed_keeps_blank_hours`).
2. **A machine number written with a space ("CNC 4")**: stored under the canonical id, displayed as written, and the planner sees the same id as before. Pinned in Task 1/2 equivalence tests and Task 3 (`test_seed_canonical_keys`).
3. **A fresh store whose operator table is empty** while machines/holidays/routings tables exist: operators must still be seeded once from the workbook on file, or the plan has nobody. Pinned in Task 4 (`test_operator_seed_still_reads_the_workbook_once`).
4. **An optimize job built before this deploy** (payload without `machines` / `shop_calendar`): plans from the workbook it carries. Pinned in Task 5 (`test_payload_without_shop_tables_falls_back`).
5. **Deleting a machine still referenced somewhere** (routing step, operator, maintenance break, frozen work): refused with each user named. Pinned in Task 3 (`test_machine_usage_names_every_user`) and Task 6 endpoint test.

---

## File Structure

| File | Responsibility |
|---|---|
| `ppc_engine/loaders/masters_loader.py` (modify) | `machine_rows_from_sheet`, `machines_from_rows`, `holiday_rows_from_sheet`, `calendar_from_holiday_rows`; `load_machines` composes the first two |
| `ppc_engine/loaders/loader.py` (modify) | `load_all(path, flexible_machines=False, routing_rows=None, machine_rows=None, holiday_rows=None)`; no workbook when all three given |
| `engine/loaders.py` (modify) | `_machine_from_row`, `_load_machines_from_rows`, `_calendar_from_holiday_rows`; `load_all(xlsx_path, routing_rows=None, machine_rows=None, holiday_rows=None)` |
| `engine/shop_masters.py` (create) | pure: seeds, rows, digests, validation, usage, saves |
| `engine/book_store.py` (modify) | load/save for the two keys |
| `api/main.py` (modify) | seed-once docs, table-mode `_current_masters`, operator seed, fingerprint/signature, payload call site, endpoints, upload removed |
| `engine/new_engine.py` (modify) | table-mode `_new_masters`, override hook |
| `engine/optimize_service.py` (modify) | payload fields, parse/run use them |
| `web/index.html`, `web/app.js`, `web/style.css` (modify) | Machines + Holidays cards; upload card and copy removed |
| `tests/seed.py` (modify) | `upload_and_seed` stores the workbook directly |
| `tests/test_shop_rows.py`, `tests/test_shop_masters.py`, `tests/test_shop_wiring.py`, `tests/test_shop_api.py`, `tests/test_shop_ui.py` (create) | per task |

---

### Task 1: ppc loader reads machines and holidays from rows

**Files:**
- Modify: `ppc_engine/loaders/masters_loader.py` (`load_machines` ~line 42, `load_calendar` ~91)
- Modify: `ppc_engine/loaders/loader.py` (`load_all`)
- Test: `tests/test_shop_rows.py`

**Interfaces:**
- Produces: `machine_rows_from_sheet(wb) -> list[tuple[name_raw, type_raw, hours_raw]]`
- Produces: `machines_from_rows(rows) -> dict[str, Machine]`
- Produces: `holiday_rows_from_sheet(wb) -> list[tuple[date, str]]` (Holiday category only; Leave and Weekly rows skipped)
- Produces: `calendar_from_holiday_rows(rows) -> ShopCalendar` (Thursday, holidays, no leaves)
- Produces: `load_all(path, flexible_machines=False, routing_rows=None, machine_rows=None, holiday_rows=None)`. When `routing_rows`, `machine_rows` and `holiday_rows` are all not None, `path` may be None and no workbook is opened: operators `()`, orders `[]`. Mixing (some None) with `path=None` raises `ValueError`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_shop_rows.py
"""Machines and holidays reach both loaders through ONE rows rule each, whatever the
source (workbook sheet or the app's tables), and with all three tables no workbook
is opened."""
import io
from datetime import date

import pytest

from ppc_engine.loaders import loader as ppc_loader
from ppc_engine.loaders.masters_loader import (
    calendar_from_holiday_rows, holiday_rows_from_sheet, machine_rows_from_sheet,
    routing_rows_from_sheet)
from ppc_engine.loaders.workbook import open_workbook
from tests.new_sample_workbook import build_new_sample_bytes
from tests.sample_workbook import build_sample_bytes


def _rows(raw):
    wb = open_workbook(io.BytesIO(raw))
    try:
        return (routing_rows_from_sheet(wb), machine_rows_from_sheet(wb),
                holiday_rows_from_sheet(wb))
    finally:
        wb.close()


def test_ppc_machine_rows_are_raw_cells():
    _r, machines, _h = _rows(build_sample_bytes())
    assert machines and all(len(m) == 3 for m in machines)


def test_ppc_tables_only_equals_workbook_masters():
    for raw in (build_sample_bytes(), build_new_sample_bytes()):
        routing, machines, holidays = _rows(raw)
        for flexible in (False, True):
            a = ppc_loader.load_all(io.BytesIO(raw), flexible_machines=flexible,
                                    routing_rows=routing)
            b = ppc_loader.load_all(None, flexible_machines=flexible, routing_rows=routing,
                                    machine_rows=machines, holiday_rows=holidays)
            assert a.masters.machines == b.masters.machines
            assert list(a.masters.machines) == list(b.masters.machines)
            assert a.masters.routings == b.masters.routings
            assert a.masters.calendar.holidays == b.masters.calendar.holidays
            assert a.masters.calendar.weekly_off_weekday == b.masters.calendar.weekly_off_weekday
            assert b.masters.calendar.leaves == {}
            assert b.orders == [] and b.masters.operators == ()


def test_ppc_tables_only_never_opens_a_workbook(monkeypatch):
    routing, machines, holidays = _rows(build_sample_bytes())
    def boom(*a, **k):
        raise AssertionError("a workbook was opened")
    monkeypatch.setattr(ppc_loader, "open_workbook", boom)
    ppc_loader.load_all(None, routing_rows=routing, machine_rows=machines, holiday_rows=holidays)


def test_ppc_partial_tables_without_a_workbook_is_an_error():
    routing, machines, _h = _rows(build_sample_bytes())
    with pytest.raises(ValueError):
        ppc_loader.load_all(None, routing_rows=routing, machine_rows=machines)


def test_calendar_from_holiday_rows_is_thursday_without_leaves():
    cal = calendar_from_holiday_rows([(date(2026, 8, 15), "Independence Day")])
    assert cal.weekly_off_weekday == 3
    assert cal.holidays == frozenset({date(2026, 8, 15)})
    assert cal.leaves == {}
```

Before writing code, check `ShopCalendar`'s field defaults (`ppc_engine/domain/calendar.py`) and whether `Masters.operators` is a tuple; adjust only the test's expected empty values if the type differs.

- [ ] **Step 2: Run** `python3.12 -m pytest tests/test_shop_rows.py -q` → FAIL (`ImportError`).

- [ ] **Step 3: Implement** in `masters_loader.py`, replacing `load_machines`' body:

```python
def machine_rows_from_sheet(wb) -> list:
    """The 'Machine master' as raw rows ``[(machine_no, machine_type, available_hrs)]``.
    The ONE reading of the sheet: ``load_machines`` and the app's one-time seed of
    its Machines table both use it."""
    ws = find_sheet(wb, "Machine master")
    rows = rows_of(ws)
    h = locate_header_row(rows, "Machine No", "Machine Type")
    t = Table.from_rows(rows, h)
    c_type = t.col("Machine Type")
    c_no = t.col("Machine No")
    c_hrs = t.col("Available Hrs/Day", "Available Hrs", "Available Hrs / Day")
    return [(t.get(row, c_no), t.get(row, c_type), t.get(row, c_hrs)) for row in t.data_rows]


def machines_from_rows(rows) -> dict[str, Machine]:
    """``[(machine_no, machine_type, available_hrs)]`` -> {canonical id -> Machine}.
    The ONE rule, whatever the source (sheet or the app's Machines table)."""
    machines: dict[str, Machine] = {}
    for no, type_raw, hrs in rows:
        mid = canon_machine(no)
        if not mid:
            continue
        type_text = str(type_raw or "").strip()
        kind = machine_kind_from_type(type_text)
        hrs = float(hrs) if isinstance(hrs, (int, float)) else _DEFAULT_HRS.get(kind, 9.5)
        machines[mid] = Machine(mid, type_text, kind, hrs)
    return machines


def load_machines(wb) -> dict[str, Machine]:
    """Read the 'Machine master' sheet into {canonical id → Machine}."""
    return machines_from_rows(machine_rows_from_sheet(wb))
```

If the original `load_machines` read `t.get(row, c_hrs)` when `c_hrs` is None, keep that exact behaviour (the reader must return the same value the old code used). Add after `load_calendar` (leave `load_calendar` unchanged):

```python
def holiday_rows_from_sheet(wb) -> list:
    """The 'Weekly off & holiday master' HOLIDAY rows as ``[(date, name)]``, in sheet
    order. Weekly-off and Leave rows are not returned: the weekly off is Thursday in
    the engine, and leave belongs to Settings > Operator absences."""
    ws = find_sheet(wb, "Weekly off & holiday master")
    rows = rows_of(ws)
    h = locate_header_row(rows, "Category", "Day / Date", "Day/Date")
    t = Table.from_rows(rows, h)
    c_cat = t.col("Category")
    c_name = t.col("Name")
    c_date = t.col("Day / Date", "Day/Date", "Date")
    out = []
    for row in t.data_rows:
        cat = str(t.get(row, c_cat) or "").strip().lower()
        d = _as_date(t.get(row, c_date))
        if cat.startswith("holiday") and d is not None:
            out.append((d, str(t.get(row, c_name) or "").strip()))
    return out


def calendar_from_holiday_rows(rows) -> ShopCalendar:
    """The shop calendar from the app's holiday list: Thursday off, the listed
    holidays closed, no per-operator leave (absences carry that)."""
    return ShopCalendar(weekly_off_weekday=THURSDAY,
                        holidays=frozenset(d for d, _name in rows), leaves={})
```

In `loader.py`:

```python
def load_all(path, flexible_machines: bool = False, routing_rows=None,
             machine_rows=None, holiday_rows=None) -> LoadResult:
    """(keep docstring; add:)
    ``machine_rows`` / ``holiday_rows`` (the app's Machines and holiday tables):
    when routing, machine AND holiday rows are all given, the masters come from them
    alone and no workbook is opened (``path`` may be None; operators come from the
    app overlay, orders from the book). Given partly, the rest is read from the
    workbook, which then must be supplied."""
    tables_only = None not in (routing_rows, machine_rows, holiday_rows)
    report = DataReport()
    if tables_only:
        wb = None
    elif path is None:
        raise ValueError("load_all: a workbook is required unless routing, machine "
                         "and holiday rows are all given")
    else:
        wb = open_workbook(path)

    machines = machines_from_rows(machine_rows) if machine_rows is not None else load_machines(wb)
    operators = () if wb is None else load_operators(wb)
    calendar = (calendar_from_holiday_rows(holiday_rows) if holiday_rows is not None
                else load_calendar(wb))
    if routing_rows is None:
        routings = load_routings(wb, report, flexible_machines=flexible_machines)
    else:
        routings = routings_from_rows(routing_rows, report, flexible_machines=flexible_machines)
    register_provisional_machines(machines, routings, report)

    masters = Masters(machines=machines, operators=operators, routings=routings, calendar=calendar)
    orders = [] if wb is None else load_orders(wb)
    _block_unschedulable(masters, orders, report)

    return LoadResult(masters=masters, orders=orders, report=report)
```

Import the four new names in `loader.py`.

- [ ] **Step 4: Run** `python3.12 -m pytest tests/test_shop_rows.py tests/test_routing_rows.py tests/test_new_engine.py tests/test_flexible_machines.py -q` → PASS; then the full suite.

- [ ] **Step 5: Commit**

```bash
git add ppc_engine/loaders/masters_loader.py ppc_engine/loaders/loader.py tests/test_shop_rows.py
git commit -m "refactor(loader): ppc machines and holidays read through one rows rule; tables-only load opens no workbook"
```

---

### Task 2: classic loader reads machines and holidays from rows

**Files:**
- Modify: `engine/loaders.py` (`_load_machines` ~228, `load_all` ~533)
- Test: `tests/test_shop_rows.py` (append)

**Interfaces:**
- Produces: `engine.loaders.load_all(xlsx_path, routing_rows=None, machine_rows=None, holiday_rows=None)`; same tables-only rule as Task 1 (`xlsx_path` may be None; operators `[]`, so_lines `[]`; `ValueError` when partial without a workbook).
- Classic machine from a table row: `Machine(machine_no=normalize_resource_id(no), display_name=str(no).strip(), machine_type=str(type).strip() or "", hr_rate=None, provisional=False, available_hrs_per_day=_num(hours))`.
- Classic calendar from holiday rows: `WorkCalendar(weekly_off_weekday=3, holidays=[d, ...] in row order, leaves=[])`.

- [ ] **Step 1: Failing tests** (append)

```python
from engine import loaders as classic


def test_classic_tables_only_equals_workbook_except_dropped_fields():
    for raw in (build_sample_bytes(), build_new_sample_bytes()):
        routing, machines, holidays = _rows(raw)
        _so, a = classic.load_all(io.BytesIO(raw), routing_rows=routing)
        so_b, b = classic.load_all(None, routing_rows=routing, machine_rows=machines,
                                   holiday_rows=holidays)
        assert list(a.machines) == list(b.machines)
        for mid in a.machines:
            ma, mb = a.machines[mid], b.machines[mid]
            assert (ma.machine_no, ma.display_name, ma.machine_type, ma.provisional,
                    ma.available_hrs_per_day) == \
                   (mb.machine_no, mb.display_name, mb.machine_type, mb.provisional,
                    mb.available_hrs_per_day)
            assert mb.hr_rate is None                      # dropped on purpose
        assert sorted(a.calendar.holidays) == sorted(b.calendar.holidays)
        assert b.calendar.weekly_off_weekday == 3 and b.calendar.leaves == []
        assert so_b == [] and b.operators == []


def test_classic_tables_only_never_opens_a_workbook(monkeypatch):
    routing, machines, holidays = _rows(build_sample_bytes())
    def boom(*a, **k):
        raise AssertionError("a workbook was opened")
    monkeypatch.setattr(classic.openpyxl, "load_workbook", boom)
    classic.load_all(None, routing_rows=routing, machine_rows=machines, holiday_rows=holidays)


def test_classic_partial_tables_without_a_workbook_is_an_error():
    routing, machines, _h = _rows(build_sample_bytes())
    with pytest.raises(ValueError):
        classic.load_all(None, routing_rows=routing, machine_rows=machines)
```

Check the sample workbooks' Weekly off sheet: if it states a weekday other than Thursday, the classic workbook path reads it while tables-only is Thursday; in that case assert `b.calendar.weekly_off_weekday == 3` only (as written) and note it in the report.

- [ ] **Step 2: Run** → FAIL (`TypeError ... machine_rows`).

- [ ] **Step 3: Implement** in `engine/loaders.py`:

```python
def _machine_from_row(no, type_raw, hours, rate=None):
    """One classic Machine from a Machine-master row (sheet or the app's table), or
    None for a blank / unusable id. The ONE classic rule."""
    if not no or str(no).strip() == "":
        return None
    canonical = normalize_resource_id(no)
    if not canonical:
        return None
    return Machine(
        machine_no=canonical,
        display_name=str(no).strip(),
        machine_type=str(type_raw).strip() if type_raw else "",
        hr_rate=_num(rate),
        provisional=False,
        available_hrs_per_day=_num(hours),
    )
```

In `_load_machines`, replace the loop body with:

```python
    for row in rows[hdr + 1:]:
        m = _machine_from_row(
            _cell(row, col["no"]), _cell(row, col["type"]),
            _cell(row, avail_col) if avail_col is not None else None,
            _cell(row, col["rate"]))
        if m is not None:
            masters.machines[m.machine_no] = m
```

Add:

```python
def _load_machines_from_rows(rows, masters: Masters):
    """Machines from the app's Machines table rows ``[(no, type, hours)]``."""
    for no, type_raw, hours in rows:
        m = _machine_from_row(no, type_raw, hours)
        if m is not None:
            masters.machines[m.machine_no] = m


def _calendar_from_holiday_rows(rows) -> WorkCalendar:
    """The calendar from the app's holiday list: Thursday off, no leave rows."""
    return WorkCalendar(weekly_off_weekday=3, holidays=[d for d, _n in rows], leaves=[])
```

Rewrite `load_all`:

```python
def load_all(xlsx_path, routing_rows=None, machine_rows=None, holiday_rows=None):
    """(keep docstring; add:)
    ``machine_rows`` / ``holiday_rows``: the app's Machines and holiday tables. With
    routing, machine AND holiday rows all given no workbook is opened (``xlsx_path``
    may be None; there are no SO lines and no workbook operators)."""
    tables_only = None not in (routing_rows, machine_rows, holiday_rows)
    masters = Masters()
    so_lines = []
    if tables_only:
        _load_machines_from_rows(machine_rows, masters)
        masters.calendar = _calendar_from_holiday_rows(holiday_rows)
        _load_routings_from_rows(routing_rows, masters)
    else:
        if xlsx_path is None:
            raise ValueError("load_all: a workbook is required unless routing, machine "
                             "and holiday rows are all given")
        wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
        try:
            if machine_rows is None:
                _load_machines(wb, masters)
            else:
                _load_machines_from_rows(machine_rows, masters)
            _load_operators(wb, masters)
            if holiday_rows is None:
                _load_calendar(wb, masters)
            else:
                masters.calendar = _calendar_from_holiday_rows(holiday_rows)
            if routing_rows is None:
                _load_routings(wb, masters)
            else:
                _load_routings_from_rows(routing_rows, masters)
            so_lines = _load_so_lines(wb, masters)
        finally:
            wb.close()

    _validate(masters, so_lines)

    schedulable = [so for so in so_lines if so.item_code in masters.routings]
    return schedulable, masters
```

- [ ] **Step 4: Run** `python3.12 -m pytest tests/test_shop_rows.py tests/test_routing_rows.py tests/test_loaders.py tests/test_pipeline_golden.py -q` → PASS (golden unchanged); full suite.

- [ ] **Step 5: Commit**

```bash
git add engine/loaders.py tests/test_shop_rows.py
git commit -m "refactor(loader): classic machines and holidays read through one rows rule; tables-only load opens no workbook"
```

---

### Task 3: pure `engine/shop_masters.py` + store keys

**Files:**
- Create: `engine/shop_masters.py`
- Modify: `engine/book_store.py` (constants + 4 functions next to `load_item_master`)
- Test: `tests/test_shop_masters.py`

**Interfaces:**
- Consumes: Task 1 readers; `engine.loaders.normalize_resource_id`; `ppc_engine.loaders.normalize.parse_machine_options`, `machine_kind_from_type`.
- Produces:
  - `book_store.MACHINES_KEY = "anvitech:machines"`, `SHOP_CALENDAR_KEY = "anvitech:shop_calendar"`, `load_machines_doc()`, `save_machines_doc(doc)`, `load_shop_calendar()`, `save_shop_calendar(doc)` (JSON in kv; `None` when absent).
  - `shop_masters.WEEKLY_OFF = "Thursday"`, `TWO_SHIFT_HOURS = 19.5`, `ONE_SHIFT_HOURS = 9.5`, `class VersionConflict(Exception)`.
  - `seed_machines(raw: bytes, now_iso: str) -> dict | None`, `seed_calendar(raw: bytes, now_iso: str) -> dict | None` (None when the sheet is absent).
  - `machine_rows(doc) -> list[tuple]`, `holiday_rows(doc) -> list[tuple[date, str]]`.
  - `machines_digest(doc) -> str`, `calendar_digest(doc) -> str` ("none" for None).
  - `validate_machine(mid: str, item: dict, existing: dict, create: bool) -> list[str]`, `validate_holiday(d: str, name: str, existing: list, today: date) -> list[str]`.
  - `machine_usage(mid, item_doc, operator_table, downtime_rows, frozen_rows) -> list[str]`.
  - `apply_machine_save(doc, mid, item, expected_version, now_iso, user, create) -> dict` (raises `VersionConflict`).
  - `add_holiday(doc, d, name, now_iso) -> dict`, `remove_holiday(doc, d) -> dict` (raises `KeyError` when absent).

- [ ] **Step 1: Failing tests**

```python
# tests/test_shop_masters.py
from datetime import date

import pytest

from engine import book_store, shop_masters as sm
from tests.sample_workbook import build_sample_bytes

NOW = "2026-10-05T10:00:00"
TODAY = date(2026, 10, 5)


def test_seed_machines_canonical_keys_in_sheet_order():
    doc = sm.seed_machines(build_sample_bytes(), NOW)
    assert list(doc["machines"])                    # non-empty, insertion order kept
    for mid, m in doc["machines"].items():
        assert mid == mid.upper() and " " not in mid
        assert set(m) >= {"name", "type", "hours", "version"}
    assert doc["seed_digest"] == sm.machines_digest(doc)


def test_seed_canonical_keys(monkeypatch):
    monkeypatch.setattr(sm, "_machine_sheet_rows", lambda raw: [("CNC 4", "CNC lathe", 19.5)])
    doc = sm.seed_machines(b"x", NOW)
    assert list(doc["machines"]) == ["CNC4"]
    assert doc["machines"]["CNC4"]["name"] == "CNC 4"


def test_seed_keeps_blank_hours(monkeypatch):
    monkeypatch.setattr(sm, "_machine_sheet_rows", lambda raw: [("MD1", "Manual deburring", None)])
    doc = sm.seed_machines(b"x", NOW)
    assert doc["machines"]["MD1"]["hours"] is None
    assert sm.machine_rows(doc) == [("MD1", "Manual deburring", None)]


def test_seed_calendar_holidays_only(monkeypatch):
    monkeypatch.setattr(sm, "_holiday_sheet_rows",
                        lambda raw: [(date(2026, 8, 15), "Independence Day")])
    doc = sm.seed_calendar(b"x", NOW)
    assert doc["holidays"] == [{"date": "2026-08-15", "name": "Independence Day"}]
    assert sm.holiday_rows(doc) == [(date(2026, 8, 15), "Independence Day")]
    assert doc["seed_digest"] == sm.calendar_digest(doc)


def test_seed_returns_none_without_the_sheet():
    import io, openpyxl
    wb = openpyxl.Workbook(); buf = io.BytesIO(); wb.save(buf)
    assert sm.seed_machines(buf.getvalue(), NOW) is None
    assert sm.seed_calendar(buf.getvalue(), NOW) is None


def test_digests_ignore_bookkeeping():
    doc = {"machines": {"CNC1": {"name": "CNC1", "type": "CNC lathe", "hours": 19.5, "version": 1}}}
    d0 = sm.machines_digest(doc)
    doc["machines"]["CNC1"]["version"] = 7
    assert sm.machines_digest(doc) == d0
    doc["machines"]["CNC1"]["hours"] = 9.5
    assert sm.machines_digest(doc) != d0
    assert sm.machines_digest(None) == "none" and sm.calendar_digest(None) == "none"


EXISTING = {"CNC1": {"name": "CNC1", "type": "CNC lathe", "hours": 19.5, "version": 1}}


@pytest.mark.parametrize("mid,item,create,needle", [
    ("", {"type": "CNC lathe", "hours": 19.5}, True, "machine number"),
    ("CNC1", {"type": "CNC lathe", "hours": 19.5}, True, "already"),
    ("CNC8", {"type": "", "hours": 19.5}, True, "type"),
    ("CNC8", {"type": "CNC lathe", "hours": 0}, True, "hours"),
    ("CNC8", {"type": "CNC lathe", "hours": 25}, True, "hours"),
    ("CNC8", {"type": "CNC lathe", "hours": float("nan")}, True, "hours"),
    ("OS", {"type": "Outsourced", "hours": 9.5}, True, "OS"),
    ("CNC8/CNC9", {"type": "CNC lathe", "hours": 19.5}, True, "one machine"),
])
def test_validate_machine_refuses(mid, item, create, needle):
    errs = sm.validate_machine(mid, item, EXISTING, create)
    assert errs and any(needle in e for e in errs), errs


def test_validate_machine_accepts():
    assert sm.validate_machine("CNC 8", {"type": "CNC lathe", "hours": 19.5}, EXISTING, True) == []
    assert sm.validate_machine("CNC1", {"type": "CNC lathe", "hours": 9.5}, EXISTING, False) == []


def test_validate_holiday():
    existing = [{"date": "2026-11-08", "name": "Diwali"}]
    assert sm.validate_holiday("2026-12-25", "Christmas", existing, TODAY) == []
    assert sm.validate_holiday("2026-11-08", "Again", existing, TODAY)            # duplicate
    assert sm.validate_holiday("not-a-date", "X", existing, TODAY)
    assert sm.validate_holiday("2040-01-01", "Far", existing, TODAY)               # > 5 years
    assert sm.validate_holiday("2026-12-25", "  ", existing, TODAY)                # blank name


def test_machine_usage_names_every_user():
    item_doc = {"items": {"ITEM1": {"steps": [{"name": "CNC FIRST", "allotted": "CNC1/CNC2", "suggested": ""}]}}}
    operators = {"operators": [{"name": "Ravi", "machines_raw": "CNC1/VMC1"}]}
    downtime = [{"machine": "CNC1", "from_date": "2026-10-10", "to_date": "2026-10-11"}]
    frozen = [{"so_no": "SO5", "item_code": "ITEM1", "process": "CNC FIRST", "machine": "CNC1"}]
    used = sm.machine_usage("CNC1", item_doc, operators, downtime, frozen)
    assert any("ITEM1" in u for u in used)
    assert any("Ravi" in u for u in used)
    assert any("maintenance" in u for u in used)
    assert any("SO5" in u for u in used)
    assert sm.machine_usage("MD9", item_doc, operators, downtime, frozen) == []


def test_apply_machine_save_versions():
    doc = {"machines": dict(EXISTING)}
    out = sm.apply_machine_save(doc, "CNC1", {"name": "CNC1", "type": "CNC lathe", "hours": 9.5},
                                1, NOW, "anvitech", create=False)
    assert out["machines"]["CNC1"]["hours"] == 9.5 and out["machines"]["CNC1"]["version"] == 2
    assert doc["machines"]["CNC1"]["version"] == 1
    with pytest.raises(sm.VersionConflict):
        sm.apply_machine_save(out, "CNC1", {"name": "CNC1", "type": "x", "hours": 9.5}, 1, NOW, "a", False)
    new = sm.apply_machine_save(out, "CNC8", {"name": "CNC 8", "type": "CNC lathe", "hours": 19.5},
                                None, NOW, "a", create=True)
    assert list(new["machines"])[-1] == "CNC8"


def test_holiday_add_remove_keeps_date_order():
    doc = {"holidays": [{"date": "2026-11-08", "name": "Diwali"}]}
    doc = sm.add_holiday(doc, "2026-08-15", "Independence Day", NOW)
    assert [h["date"] for h in doc["holidays"]] == ["2026-08-15", "2026-11-08"]
    doc = sm.remove_holiday(doc, "2026-11-08")
    assert [h["date"] for h in doc["holidays"]] == ["2026-08-15"]
    with pytest.raises(KeyError):
        sm.remove_holiday(doc, "2026-01-01")


def test_store_round_trip():
    assert book_store.load_machines_doc() is None and book_store.load_shop_calendar() is None
    book_store.save_machines_doc({"machines": {}})
    book_store.save_shop_calendar({"holidays": []})
    assert book_store.load_machines_doc() == {"machines": {}}
    assert book_store.load_shop_calendar() == {"holidays": []}
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement.** `book_store.py`:

```python
MACHINES_KEY = "anvitech:machines"            # kv: json {seed..., machines:{id:{name,type,hours,version}}}
SHOP_CALENDAR_KEY = "anvitech:shop_calendar"  # kv: json {seed..., holidays:[{date,name}]}


def load_machines_doc():
    """The app-owned Machines table, or ``None`` if never seeded."""
    raw = get_store().kv_get(MACHINES_KEY)
    return json.loads(raw) if raw else None


def save_machines_doc(doc: dict) -> None:
    get_store().kv_set(MACHINES_KEY, json.dumps(doc))


def load_shop_calendar():
    """The app-owned holiday list, or ``None`` if never seeded."""
    raw = get_store().kv_get(SHOP_CALENDAR_KEY)
    return json.loads(raw) if raw else None


def save_shop_calendar(doc: dict) -> None:
    get_store().kv_set(SHOP_CALENDAR_KEY, json.dumps(doc))
```

`engine/shop_masters.py`:

```python
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
```

Note: `machine_usage` keys on the stored row field names; check `engine/freeze.py` frozen rows and the downtime rows for their exact field names (`machine`, `so_no`, `process`, `from_date`, `to_date`) and adapt if they differ. Holiday order: sorting by date in `add_holiday` is safe (holidays are a set to both engines).

- [ ] **Step 4: Run** `python3.12 -m pytest tests/test_shop_masters.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/shop_masters.py engine/book_store.py tests/test_shop_masters.py
git commit -m "feat(shop-masters): pure module and store keys for the Machines table and holidays"
```

---

### Task 4: API masters built from the tables, no workbook opened

**Files:**
- Modify: `api/main.py`: next to `_item_master_doc` (~338), `_current_masters` (~352), `_with_operator_overlay` (~380), `_inputs_signature`, `_plan_fingerprint`
- Test: `tests/test_shop_wiring.py`

**Interfaces:**
- Produces: `api.main._machines_doc() -> dict | None`, `api.main._calendar_doc() -> dict | None` (seed once each, like `_item_master_doc`).
- `_current_masters()`: when the item master, machines and calendar docs all exist, `load_all(None, routing_rows=..., machine_rows=..., holiday_rows=...)`; else the stage-1 workbook path, passing whichever rows exist. Cache key `(store env, item digest, machines digest, calendar digest)`.
- Operator seed: when the operator table is empty, it is seeded from the WORKBOOK's operator sheet (classic `load_all(BytesIO(raw))[1].operators`), even in tables mode.

- [ ] **Step 1: Failing tests**

```python
# tests/test_shop_wiring.py
import io
import json
from datetime import date

import pytest

from engine import book_store, shop_masters as sm
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
                           Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21))])


def test_first_masters_read_seeds_both_tables_once():
    m = _api(); _seed_book()
    masters = m._current_masters()
    mdoc, cdoc = book_store.load_machines_doc(), book_store.load_shop_calendar()
    assert set(mdoc["machines"]) <= set(masters.machines)
    assert cdoc is not None
    mdoc["machines"][next(iter(mdoc["machines"]))]["type"] = "EDITED"
    book_store.save_machines_doc(mdoc)
    m._current_masters()
    assert "EDITED" in json.dumps(book_store.load_machines_doc())     # not re-seeded


def test_masters_never_open_the_workbook_once_seeded(monkeypatch):
    m = _api(); _seed_book()
    m._current_masters()                                 # seeds everything
    m._MASTERS_CACHE["masters"] = None
    import openpyxl
    def boom(*a, **k):
        raise AssertionError("workbook opened")
    monkeypatch.setattr(openpyxl, "load_workbook", boom)
    from ppc_engine.loaders import loader as ppc_loader
    monkeypatch.setattr(ppc_loader, "open_workbook", boom)
    masters = m._current_masters()
    assert masters.routings and masters.machines and masters.operators


def test_masters_follow_a_machine_edit_and_a_holiday():
    m = _api(); _seed_book(); m._current_masters()
    mdoc = book_store.load_machines_doc()
    mid = next(iter(mdoc["machines"]))
    mdoc["machines"][mid]["hours"] = 9.5
    book_store.save_machines_doc(mdoc)
    assert m._current_masters().machines[mid].available_hrs_per_day == 9.5
    book_store.save_shop_calendar(sm.add_holiday(book_store.load_shop_calendar(),
                                                 "2026-11-08", "Diwali", "t"))
    assert date(2026, 11, 8) in m._current_masters().calendar.holidays


def test_operator_seed_still_reads_the_workbook_once():
    m = _api(); _seed_book()
    assert book_store.load_operator_table() is None
    m._current_masters()
    table = book_store.load_operator_table()
    assert table and table["operators"]


def test_signatures_unchanged_by_seeding_and_moved_by_edits(monkeypatch):
    m = _api(); _seed_book()
    cfg = m._load_plan_config()
    m._current_masters()
    seeded = m._inputs_signature(cfg)
    f0 = m._plan_fingerprint(cfg)
    real_m, real_c = book_store.load_machines_doc, book_store.load_shop_calendar
    monkeypatch.setattr(m.book_store, "load_machines_doc", lambda: None)
    monkeypatch.setattr(m.book_store, "load_shop_calendar", lambda: None)
    m._MASTERS_CACHE["masters"] = None
    assert m._inputs_signature(cfg) == seeded            # seeded tables == stage-1 signature
    monkeypatch.setattr(m.book_store, "load_machines_doc", real_m)
    monkeypatch.setattr(m.book_store, "load_shop_calendar", real_c)
    book_store.save_shop_calendar(sm.add_holiday(book_store.load_shop_calendar(),
                                                 "2026-11-08", "Diwali", "t"))
    assert m._inputs_signature(cfg) != seeded
    assert m._plan_fingerprint(cfg) != f0
```

- [ ] **Step 2: Run** `python3.12 -m pytest tests/test_shop_wiring.py -q` → FAIL.

- [ ] **Step 3: Implement** in `api/main.py` (add `from engine import shop_masters` to imports):

```python
def _seed_once(load, save, seed):
    """One app-owned master table, seeded ONCE from the workbook on file."""
    doc = load()
    if doc is None:
        raw = book_store.load_masters_bytes()
        if raw is not None:
            doc = seed(raw, _ist_now().isoformat(timespec="seconds"))
            if doc is not None:
                save(doc)
    return doc


def _machines_doc():
    """The app-owned Machines table (seeded once from the workbook's Machine master)."""
    return _seed_once(book_store.load_machines_doc, book_store.save_machines_doc,
                      shop_masters.seed_machines)


def _calendar_doc():
    """The app-owned holiday list (seeded once from the workbook's holiday sheet)."""
    return _seed_once(book_store.load_shop_calendar, book_store.save_shop_calendar,
                      shop_masters.seed_calendar)
```

Rewrite `_item_master_doc` to `return _seed_once(book_store.load_item_master, book_store.save_item_master, item_master.seed_doc)` (same behaviour, one helper).

`_current_masters`:

```python
def _current_masters():
    """The shop's masters, built from the app's tables: Item Process Master
    (routings), Machines and holidays, each seeded once from the workbook on file.
    With all three tables the workbook is not opened at all (2026-10-05, stage 2).
    Cached per (store, the three table digests); the operator table is overlaid on
    every call."""
    idoc, mdoc, cdoc = _item_master_doc(), _machines_doc(), _calendar_doc()
    key = (_store_env_key(), item_master.digest(idoc),
           shop_masters.machines_digest(mdoc), shop_masters.calendar_digest(cdoc))
    if _MASTERS_CACHE["masters"] is not None and _MASTERS_CACHE["key"] == key:
        base = _MASTERS_CACHE["masters"]
    else:
        rows = dict(routing_rows=item_master.routing_rows(idoc) if idoc else None,
                    machine_rows=shop_masters.machine_rows(mdoc) if mdoc else None,
                    holiday_rows=shop_masters.holiday_rows(cdoc) if cdoc else None)
        raw = None
        if None in rows.values():
            raw = book_store.load_masters_bytes()
        if None not in rows.values():
            _, base = load_all(None, **rows)
        elif raw is None:
            base = Masters()
        else:
            _, base = load_all(io.BytesIO(raw), **rows)
        _MASTERS_CACHE.update(key=key, masters=base)
    return _with_operator_overlay(base)
```

`_masters_sha()` must no longer rely on `_MASTERS_CACHE["sha"]` being set by a workbook parse. Replace its body with a cached sha of the stored bytes:

```python
def _masters_sha() -> str:
    """Content hash of the stored masters workbook. Unchanged by this stage: the
    workbook is still on file (as the seed source), it is just no longer parsed."""
    _current_masters()
    raw = book_store.load_masters_bytes()
    return hashlib.sha256(raw).hexdigest() if raw else "none"
```

`_with_operator_overlay`: replace the seed branch so it reads the workbook's operators when the table is empty (the base may have none in tables mode):

```python
    table = book_store.load_operator_table()
    if table is None:
        seed_from = base.operators
        if not seed_from:
            raw = book_store.load_masters_bytes()
            if raw is not None:
                seed_from = load_all(io.BytesIO(raw))[1].operators
        if not seed_from:
            return base                       # nothing to seed from yet
        table = {"week_anchor": operator_master.last_friday(today).isoformat(),
                 "operators": operator_master.seed_rows_from_masters(replace(base, operators=seed_from))}
        book_store.save_operator_table(table)
```

(Read the function first and keep the rest unchanged; confirm `seed_rows_from_masters` reads `masters.operators`.)

`_inputs_signature`: replace the stage-1 item-master block with one loop. Keep the stage-1 tag `"item_master"` exactly, so the stage-1 signature does not move:

```python
    for tag, doc, digest in (
            ("item_master", book_store.load_item_master(), item_master.digest),
            ("machines", book_store.load_machines_doc(), shop_masters.machines_digest),
            ("shop_calendar", book_store.load_shop_calendar(), shop_masters.calendar_digest)):
        # Fold a table in only once it differs from what it was seeded from: right
        # after the seed it equals the workbook (already covered by the masters sha).
        if doc and digest(doc) != doc.get("seed_digest"):
            parts.append([tag, digest(doc)])
```

Read the stage-1 block first and confirm its tag string and list shape (`parts.append(["item_master", ...])`); match them.

`_plan_fingerprint` parts gain:

```python
        "machines": shop_masters.machines_digest(book_store.load_machines_doc()),
        "shop_calendar": shop_masters.calendar_digest(book_store.load_shop_calendar()),
```

- [ ] **Step 4: Run** `python3.12 -m pytest tests/test_shop_wiring.py tests/test_item_master_wiring.py tests/test_operators_api.py tests/test_plan_cache.py tests/test_plan_cache_freshness.py -q` → PASS; full suite. Then the reader sweep: `grep -n "load_all(\|load_masters_bytes()\|open_workbook" api/main.py engine/*.py` and record every remaining site with why it is allowed (seed readers, operator seed, payload fallback) in the commit message.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_shop_wiring.py
git commit -m "feat(shop-masters): app masters built from the three tables, no workbook opened; signatures follow edits"
```

---

### Task 5: new engine and optimize payload

**Files:**
- Modify: `engine/new_engine.py` (`_item_master_doc` area, `_new_masters`)
- Modify: `engine/optimize_service.py` (`build_payload`, `parse_payload`, `run_candidate`)
- Modify: `api/main.py` payload call site (`item_master=book_store.load_item_master(),`)
- Modify: `tests/conftest.py` (teardown)
- Test: `tests/test_shop_wiring.py` (append)

**Interfaces:**
- Produces: `new_engine.set_shop_masters(machines_doc, calendar_doc)`, `new_engine.clear_shop_masters_override()`, `new_engine._shop_docs() -> (machines_doc, calendar_doc)`. Override semantics as stage 1: `_UNSET` = store; `None` = the workbook's own sheet.
- `_new_masters(flexible)`: tables-only when all three docs exist (no workbook needed); cache key `(workbook sha or "none", item digest, machines digest, calendar digest, flexible)`.
- `build_payload(..., machines=None, shop_calendar=None)` → keys `"machines"`, `"shop_calendar"`; `parse_payload` uses them (tables-only when all three present).

- [ ] **Step 1: Failing tests** (append)

```python
def test_new_engine_masters_follow_the_tables_without_a_workbook(monkeypatch):
    from engine import new_engine
    new_engine._MASTERS_CACHE.clear()
    m = _api(); _seed_book(); m._current_masters()
    mdoc = book_store.load_machines_doc()
    mid = next(i for i, x in mdoc["machines"].items() if "CNC" in x["type"].upper())
    from ppc_engine.loaders import loader as ppc_loader
    def boom(*a, **k):
        raise AssertionError("workbook opened")
    monkeypatch.setattr(ppc_loader, "open_workbook", boom)
    mdoc["machines"][mid]["type"] = "Manual deburring"
    book_store.save_machines_doc(mdoc)
    from ppc_engine.domain.resources import MachineKind
    assert new_engine._new_masters(False).machines[mid].kind == MachineKind.MANUAL


def _payload(machines, calendar):
    from engine import optimize_service as svc
    from engine.config import Config
    from engine import item_master as im
    _seed_book()
    return svc.build_payload(book_store.load_active_orders(), [], build_sample_bytes(),
                             Config(plan_start_date=date(2025, 3, 1)), seed=1,
                             item_master=im.seed_doc(build_sample_bytes(), "t"),
                             machines=machines, shop_calendar=calendar)


def test_payload_round_trips_the_shop_tables():
    from engine import optimize_service as svc
    mdoc = sm.seed_machines(build_sample_bytes(), "t")
    cdoc = sm.add_holiday(sm.seed_calendar(build_sample_bytes(), "t"), "2026-11-08", "Diwali", "t")
    payload = json.loads(json.dumps(_payload(mdoc, cdoc)))
    assert payload["machines"] == mdoc and payload["shop_calendar"] == cdoc
    parsed = svc.parse_payload(payload)
    assert len(parsed) == 8
    assert date(2026, 11, 8) in parsed[2].calendar.holidays


def test_payload_without_shop_tables_falls_back():
    from engine import optimize_service as svc
    payload = json.loads(json.dumps(_payload(None, None)))
    payload.pop("machines"); payload.pop("shop_calendar")
    parsed = svc.parse_payload(payload)
    assert parsed[2].machines                          # read from the workbook it carries


def test_run_candidate_feeds_the_shop_tables(monkeypatch):
    from engine import new_engine, optimize_service as svc
    seen = {}
    monkeypatch.setattr(new_engine, "set_shop_masters", lambda md, cd: seen.update(m=md, c=cd))
    monkeypatch.setattr(svc, "prepare_contest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    mdoc = sm.seed_machines(build_sample_bytes(), "t")
    cdoc = sm.seed_calendar(build_sample_bytes(), "t")
    payload = json.loads(json.dumps(_payload(mdoc, cdoc)))
    payload["config"]["scheduler"] = "new"
    try:
        with pytest.raises(RuntimeError, match="stop"):
            svc.run_candidate(payload, 50)
    finally:
        new_engine.set_masters_bytes(None)
        new_engine.clear_item_master_override()
    assert seen == {"m": mdoc, "c": cdoc}


def test_api_payload_call_site_passes_the_shop_tables():
    import inspect
    src = inspect.getsource(_api())
    assert "machines=book_store.load_machines_doc()" in src
    assert "shop_calendar=book_store.load_shop_calendar()" in src
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.** `new_engine.py` (beside the stage-1 override):

```python
_OVERRIDE_SHOP = _UNSET   # (machines_doc, calendar_doc) from a worker payload


def set_shop_masters(machines_doc, calendar_doc) -> None:
    global _OVERRIDE_SHOP
    _OVERRIDE_SHOP = (machines_doc, calendar_doc)


def clear_shop_masters_override() -> None:
    global _OVERRIDE_SHOP
    _OVERRIDE_SHOP = _UNSET


def _shop_docs():
    if _OVERRIDE_SHOP is not _UNSET:
        return _OVERRIDE_SHOP
    return book_store.load_machines_doc(), book_store.load_shop_calendar()
```

`_new_masters`:

```python
def _new_masters(flexible: bool = False):
    """(keep docstring; add:) With the Item Process Master, Machines and holiday
    tables all present no workbook is needed or opened (stage 2)."""
    from engine import shop_masters
    doc = _item_master_doc()
    mdoc, cdoc = _shop_docs()
    tables_only = bool(doc and mdoc and cdoc)
    raw = None
    if not tables_only:
        raw = _OVERRIDE_BYTES if _OVERRIDE_BYTES is not None else book_store.load_masters_bytes()
        if not raw:
            raise RuntimeError("new_engine: no masters workbook available (store empty and none injected)")
    h = hashlib.sha256(raw).hexdigest() if raw else "none"
    d = (item_master.digest(doc), shop_masters.machines_digest(mdoc),
         shop_masters.calendar_digest(cdoc))
    key = (h, d, bool(flexible))
    cached = _MASTERS_CACHE.get(key)
    if cached is None:
        for k in [k for k in _MASTERS_CACHE if k[:2] != (h, d)]:
            del _MASTERS_CACHE[k]
        cached = load_all(
            None if tables_only else io.BytesIO(raw), flexible_machines=bool(flexible),
            routing_rows=item_master.routing_rows(doc) if doc else None,
            machine_rows=shop_masters.machine_rows(mdoc) if mdoc else None,
            holiday_rows=shop_masters.holiday_rows(cdoc) if cdoc else None).masters
        cached = replace(cached, routings=pad_routings(cached.routings))
        _MASTERS_CACHE[key] = cached
    return cached
```

`optimize_service.py`: `build_payload` gains `machines=None, shop_calendar=None` and returns `"machines": machines, "shop_calendar": shop_calendar` (read with `.get` downstream; comment as stage 1). `parse_payload`:

```python
    raw = payload.get("masters_xlsx_b64")
    from engine import item_master as _im, shop_masters as _sm
    idoc, mdoc, cdoc = (payload.get("item_master"), payload.get("machines"),
                        payload.get("shop_calendar"))
    rows = dict(routing_rows=_im.routing_rows(idoc) if idoc else None,
                machine_rows=_sm.machine_rows(mdoc) if mdoc else None,
                holiday_rows=_sm.holiday_rows(cdoc) if cdoc else None)
    if None not in rows.values():
        _, masters = load_all(None, **rows)
    elif raw:
        _, masters = load_all(io.BytesIO(base64.b64decode(raw)), **rows)
    else:
        masters = Masters()
```

`run_candidate`, inside the `"new"` branch after `set_item_master(...)`:

```python
        new_engine.set_shop_masters(payload.get("machines"), payload.get("shop_calendar"))
```

`api/main.py` `build_payload(` call: add, one per line exactly:
```python
                machines=book_store.load_machines_doc(),
                shop_calendar=book_store.load_shop_calendar(),
```
`tests/conftest.py` teardown: also call `new_engine.clear_shop_masters_override()` next to `clear_item_master_override()`.

- [ ] **Step 4: Run** `python3.12 -m pytest tests/test_shop_wiring.py tests/test_item_master_wiring.py tests/test_optimize_cloud.py tests/test_optimize_service.py tests/test_optimize_shard.py tests/test_machine_downtime_api.py tests/test_new_engine.py -q` → PASS; full suite.

- [ ] **Step 5: Commit**

```bash
git add engine/new_engine.py engine/optimize_service.py api/main.py tests/conftest.py tests/test_shop_wiring.py
git commit -m "feat(shop-masters): new engine and optimize payload plan from the shop tables"
```

---

### Task 6: endpoints

**Files:**
- Modify: `api/main.py` (request models beside `ItemStep`; endpoints after the Item Process Master endpoints)
- Test: `tests/test_shop_api.py`

**Interfaces:**
- `GET /machines` (any) → `{"machines": [{"id","name","type","hours","kind": "machining"|"manual"|"inspection","version","used_by": [str]}], "types": [str], "standard_hours": {"two": 19.5, "one": 9.5}}`
- `POST /machines` (admin) `{id, type, hours}` → `{"machine": {...}}`; 400 invalid; 409 exists
- `PUT /machines` (admin) `{id, type, hours, version}` → `{"machine": {...}}`; 404 unknown; 409 stale; 400 invalid
- `POST /machines/delete` (admin) `{id}` → `{"deleted": id}`; 400 in use (detail names every user); 404 unknown
- `GET /holidays` (any) → `{"weekly_off": "Thursday", "holidays": [{"date","name"}]}`
- `POST /holidays` (admin) `{date, name}` → `{"holidays": [...]}`; 400 invalid/duplicate
- `POST /holidays/delete` (admin) `{date}` → `{"holidays": [...]}`; 404 unknown

- [ ] **Step 1: Failing tests**

```python
# tests/test_shop_api.py
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _client(m, admin=True):
    c = TestClient(m.app)
    if admin:
        c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    else:
        c.post("/login", data={"username": "anvitech_user", "password": "anvitech12345678"})
    return c


def _setup():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20))])
    m = _api()
    return m, _client(m), _client(m, admin=False)


def test_get_machines_for_both_roles():
    m, admin, user = _setup()
    for c in (admin, user):
        r = c.get("/machines")
        assert r.status_code == 200
        data = r.json()
        assert data["machines"] and data["types"]
        cnc1 = next(x for x in data["machines"] if x["id"] == "CNC1")
        assert cnc1["kind"] == "machining"
        assert any("SAMP-A-01" in u for u in cnc1["used_by"])


def test_user_role_cannot_write():
    m, admin, user = _setup()
    assert user.post("/machines", json={"id": "CNC8", "type": "CNC lathe", "hours": 19.5}).status_code == 403
    assert user.put("/machines", json={"id": "CNC1", "type": "x", "hours": 9.5, "version": 1}).status_code == 403
    assert user.post("/machines/delete", json={"id": "CNC1"}).status_code == 403
    assert user.post("/holidays", json={"date": "2026-11-08", "name": "Diwali"}).status_code == 403
    assert user.post("/holidays/delete", json={"date": "2026-11-08"}).status_code == 403


def test_add_machine_appears_in_pickers_and_plan():
    m, admin, _ = _setup()
    r = admin.post("/machines", json={"id": "CNC 8", "type": "CNC lathe", "hours": 19.5})
    assert r.status_code == 200, r.text
    assert r.json()["machine"]["id"] == "CNC8"
    assert "CNC8" in m._current_masters().machines
    ops = admin.get("/operators").json()["machines"]
    assert any(x["id"] == "CNC8" for x in ops)
    assert admin.post("/machines", json={"id": "CNC8", "type": "CNC lathe", "hours": 19.5}).status_code == 409


def test_edit_machine_with_version_check():
    m, admin, _ = _setup()
    cnc1 = next(x for x in admin.get("/machines").json()["machines"] if x["id"] == "CNC1")
    body = {"id": "CNC1", "type": cnc1["type"], "hours": 9.5, "version": cnc1["version"]}
    assert admin.put("/machines", json=body).status_code == 200
    assert m._current_masters().machines["CNC1"].available_hrs_per_day == 9.5
    assert admin.put("/machines", json=body).status_code == 409
    assert admin.put("/machines", json={**body, "id": "NOPE9"}).status_code == 404


def test_delete_machine_refused_while_used_then_allowed():
    m, admin, _ = _setup()
    r = admin.post("/machines/delete", json={"id": "CNC1"})
    assert r.status_code == 400 and "SAMP-A-01" in r.json()["detail"]
    assert admin.post("/machines", json={"id": "MX9", "type": "Manual Packing", "hours": 9.5}).status_code == 200
    assert admin.post("/machines/delete", json={"id": "MX9"}).status_code == 200
    assert admin.post("/machines/delete", json={"id": "MX9"}).status_code == 404


def test_holidays_add_list_delete():
    m, admin, user = _setup()
    r = admin.post("/holidays", json={"date": "2026-11-08", "name": "Diwali"})
    assert r.status_code == 200, r.text
    data = user.get("/holidays").json()
    assert data["weekly_off"] == "Thursday"
    assert {"date": "2026-11-08", "name": "Diwali"} in data["holidays"]
    assert date(2026, 11, 8) in m._current_masters().calendar.holidays
    assert admin.post("/holidays", json={"date": "2026-11-08", "name": "Again"}).status_code == 400
    assert admin.post("/holidays/delete", json={"date": "2026-11-08"}).status_code == 200
    assert admin.post("/holidays/delete", json={"date": "2026-11-08"}).status_code == 404


def test_invalid_machine_and_holiday_inputs():
    m, admin, _ = _setup()
    assert admin.post("/machines", json={"id": "CNC8", "type": "CNC lathe", "hours": 30}).status_code == 400
    assert admin.post("/machines", json={"id": "", "type": "CNC lathe", "hours": 9.5}).status_code == 400
    assert admin.post("/holidays", json={"date": "nope", "name": "x"}).status_code == 400
```

- [ ] **Step 2: Run** → FAIL (404s).

- [ ] **Step 3: Implement.** Models:

```python
class MachineSaveRequest(BaseModel):
    id: str
    type: str = ""
    hours: Optional[Union[int, float]] = None
    version: Optional[int] = None


class MachineIdRequest(BaseModel):
    id: str


class HolidayRequest(BaseModel):
    date: str
    name: str = ""


class HolidayDateRequest(BaseModel):
    date: str
```

Endpoints:

```python
# --------------------------------------------------------------------------- #
# Machines and holidays (2026-10-05, stage 2): edited in Settings, not the Excel.
# --------------------------------------------------------------------------- #
def _machine_users(mid):
    return shop_masters.machine_usage(
        mid, book_store.load_item_master(), book_store.load_operator_table(),
        book_store.load_machine_downtime(), book_store.load_frozen_ops())


def _machine_view(mid, m):
    from ppc_engine.loaders.normalize import machine_kind_from_type
    return {"id": mid, "name": m.get("name") or mid, "type": m.get("type") or "",
            "hours": m.get("hours"), "version": m.get("version", 1),
            "kind": machine_kind_from_type(m.get("type")).name.lower(),
            "used_by": _machine_users(mid)}


@app.get("/machines")
def get_machines():
    _current_masters()                        # seeds the tables once
    doc = book_store.load_machines_doc() or {"machines": {}}
    types = list(dict.fromkeys(m.get("type") for m in doc["machines"].values() if m.get("type")))
    return {"machines": [_machine_view(mid, m) for mid, m in doc["machines"].items()],
            "types": types,
            "standard_hours": {"two": shop_masters.TWO_SHIFT_HOURS,
                               "one": shop_masters.ONE_SHIFT_HOURS}}


def _save_machine(req: MachineSaveRequest, request: Request, create: bool):
    require_admin(request)
    _current_masters()
    doc = book_store.load_machines_doc() or {"machines": {}}
    raw_id = req.id.strip()
    mid = normalize_resource_id(raw_id) if raw_id else ""
    if not create and mid not in doc["machines"]:
        raise HTTPException(status_code=404, detail=f"No machine {mid or raw_id}.")
    item = {"name": doc["machines"].get(mid, {}).get("name") or raw_id,
            "type": req.type, "hours": req.hours}
    if create and mid in doc["machines"]:
        raise HTTPException(status_code=409, detail=f"Machine {mid} already exists.")
    errs = shop_masters.validate_machine(raw_id, item, doc["machines"], create)
    if errs:
        raise HTTPException(status_code=400, detail=" ".join(errs))
    try:
        new_doc = shop_masters.apply_machine_save(
            doc, mid, item, req.version, _ist_now().isoformat(timespec="seconds"),
            getattr(request.state, "user", "admin"), create)
    except shop_masters.VersionConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    book_store.save_machines_doc(new_doc)
    return {"machine": _machine_view(mid, new_doc["machines"][mid])}


@app.post("/machines")
def create_machine(req: MachineSaveRequest, request: Request):
    """Add a machine (admin). Appears at once in every machine picker."""
    return _save_machine(req, request, create=True)


@app.put("/machines")
def update_machine(req: MachineSaveRequest, request: Request):
    """Change a machine's type or working hours (admin). The number never changes."""
    return _save_machine(req, request, create=False)


@app.post("/machines/delete")
def delete_machine(req: MachineIdRequest, request: Request):
    """Remove a machine (admin). Refused while a routing step, an operator, a
    maintenance break or work in progress still names it."""
    require_admin(request)
    _current_masters()
    doc = book_store.load_machines_doc() or {"machines": {}}
    mid = normalize_resource_id(req.id.strip()) if req.id.strip() else ""
    if mid not in doc["machines"]:
        raise HTTPException(status_code=404, detail=f"No machine {mid or req.id}.")
    used = _machine_users(mid)
    if used:
        raise HTTPException(status_code=400,
                            detail=f"{mid} is still used by {', '.join(used)}. Change those first.")
    del doc["machines"][mid]
    book_store.save_machines_doc(doc)
    return {"deleted": mid}


@app.get("/holidays")
def get_holidays():
    _current_masters()
    doc = book_store.load_shop_calendar() or {"holidays": []}
    return {"weekly_off": shop_masters.WEEKLY_OFF, "holidays": doc["holidays"]}


@app.post("/holidays")
def add_holiday(req: HolidayRequest, request: Request):
    """Add a whole-shop holiday (admin)."""
    require_admin(request)
    _current_masters()
    doc = book_store.load_shop_calendar() or {"holidays": []}
    errs = shop_masters.validate_holiday(req.date, req.name, doc["holidays"], _ist_today())
    if errs:
        raise HTTPException(status_code=400, detail=" ".join(errs))
    doc = shop_masters.add_holiday(doc, req.date, req.name, _ist_now().isoformat(timespec="seconds"))
    book_store.save_shop_calendar(doc)
    return {"holidays": doc["holidays"]}


@app.post("/holidays/delete")
def delete_holiday(req: HolidayDateRequest, request: Request):
    """Remove a holiday (admin)."""
    require_admin(request)
    _current_masters()
    doc = book_store.load_shop_calendar() or {"holidays": []}
    try:
        doc = shop_masters.remove_holiday(doc, req.date)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"{req.date} is not a holiday.")
    book_store.save_shop_calendar(doc)
    return {"holidays": doc["holidays"]}
```

Import `normalize_resource_id` from `engine.loaders` if `api/main.py` does not already. Check `machine_kind_from_type(...)` returns an Enum with `.name` of `MACHINING`/`MANUAL`/`INSPECTION`; adapt the `kind` expression if it differs.

- [ ] **Step 4: Run** `python3.12 -m pytest tests/test_shop_api.py tests/test_role_parity.py tests/test_machine_downtime_api.py -q` → PASS; full suite.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_shop_api.py
git commit -m "feat(shop-masters): admin-gated endpoints for machines and holidays"
```

---

### Task 7: remove the Excel upload

**Files:**
- Modify: `api/main.py` (delete `@app.post("/upload")` and `_report_after_upload` if it has no other caller; `MAX_UPLOAD_BYTES` only if unused)
- Modify: `web/index.html` (delete `#upload-card`), `web/app.js` (delete `uploadExcel`, its wiring `_upBtn`, and the upload-only report renderer if it has no other caller)
- Modify: `tests/seed.py`; tests that post to `/upload`
- Test: `tests/test_shop_wiring.py` (append)

- [ ] **Step 1: Failing tests** (append)

```python
def test_upload_endpoint_is_gone():
    m = _api(); _seed_book()
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    r = c.post("/upload", files={"file": ("x.xlsx", build_sample_bytes())})
    assert r.status_code in (404, 405)
    assert book_store.load_masters_bytes() == build_sample_bytes()   # workbook on file kept


def test_no_upload_ui_left():
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / "web"
    html = (web / "index.html").read_text()
    js = (web / "app.js").read_text()
    assert 'id="upload-card"' not in html and 'id="upload-btn"' not in html
    assert 'fetch("/upload"' not in js
```

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Implement.**
  - Delete the `/upload` route. Grep `_report_after_upload`, `MAX_UPLOAD_BYTES`, `UploadFile`, `File` imports; delete only what becomes unused (the 413 size cap in the gatekeeper/security code, if it references `/upload` specifically, is removed with it; check `grep -n "upload" api/main.py api/auth.py`).
  - `tests/seed.py`: replace `upload_and_seed` with:

```python
class _Stored:
    """Stand-in for the old upload response (the endpoint is gone since stage 2)."""
    status_code = 200

    def json(self):
        return {"masters_updated": True, "orders_changed": 0}


def upload_and_seed(client, data: bytes, name: str = "sample.xlsx"):
    """Store the workbook's masters directly (as the removed upload did), then seed
    its orders. ``client`` is kept for call compatibility."""
    import api.main as m
    book_store.save_masters_bytes(data)
    m._MASTERS_CACHE["masters"] = None
    seed_orders(data)
    return _Stored()
```

  - For each other test that posts to `/upload` (`grep -rn '"/upload"' tests`): if the test's SUBJECT is upload behaviour (size cap, bad file, routing note, NO_ROUTING on upload, upload-first seeding), delete the test and list it in the report; if it only uses the upload to load masters, switch it to `book_store.save_masters_bytes(...)` plus `m._MASTERS_CACHE["masters"] = None`. `tests/test_oracle_e2e.py` talks to a real server: switch its upload step to writing the store the server reads, or skip that test with a reason if it cannot run without the endpoint; say which in the report.
  - `web/index.html`: delete the `#upload-card` div. Delete `#dataset-status` uses in JS accordingly.
  - `web/app.js`: delete `uploadExcel` and its button wiring; remove the upload-only data-gaps renderer only if nothing else calls it. Rewrite user copy that tells people to upload:
    - `machineSelectHtml` empty state: "Add machines in Settings > Machines".
    - the operators empty state ("The first Excel you upload fills this list automatically"): "No operators yet. Add one below."
    - the inputs-changed warnings ("Your Settings or your uploaded machine/operator/item data changed"): "Your settings, machines, holidays or item routings changed since the last search."
    - `web/index.html` operators explainer line "filled in once from your uploaded ...": drop the upload clause.
    - grep `web/` for `upload` and `Excel` and fix every remaining user-visible sentence; keep code comments accurate.

- [ ] **Step 4: Run** the full suite. Every remaining failure must be explained in the report (expected: none besides the known xlsxwriter one).

- [ ] **Step 5: Commit**

```bash
git add -A api/main.py web/index.html web/app.js tests
git commit -m "feat: the Excel upload is removed; masters live in the app"
```

---

### Task 8: Settings cards for Machines and Holidays

**Files:**
- Modify: `web/index.html` (two cards before the Operators & shifts card), `web/app.js`, `web/style.css`
- Test: `tests/test_shop_ui.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_shop_ui.py
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def test_cards_exist_and_are_visible_to_both_roles():
    html = (WEB / "index.html").read_text()
    for cid in ("machines-card", "holidays-card"):
        line = next(l for l in html.splitlines() if f'id="{cid}"' in l)
        assert "admin-only" not in line
    assert html.index('id="machines-card"') < html.index('id="operators-header"')


def test_writes_are_role_checked_and_copy_is_clean():
    js = (WEB / "app.js").read_text()
    section = js.split("// ===== Machines and holidays =====")[1].split("// ===== end Machines and holidays =====")[0]
    assert 'currentRole === "admin"' in section
    assert "—" not in section
    for path in ('"/machines"', '"/machines/delete"', '"/holidays"', '"/holidays/delete"'):
        assert path in section
```

(`operators-header` is the Operators card's header element id used by `loadOperators`; confirm it with grep and use the right id.)

- [ ] **Step 2: Run** → FAIL.

- [ ] **Step 3: Markup** (insert before the Operators & shifts card):

```html
    <div class="card" id="machines-card">
      <div class="card-head"><h2>Machines</h2></div>
      <p class="explainer">Every machine the plan can use. The type decides how a machine is planned: CNC and VMC types get the 90 minute setup and the extra 30% on cycle times, so pick it carefully.</p>
      <table class="tbl" id="machines-table"><thead><tr><th>Machine</th><th>Type</th><th>Working time</th><th>Used by</th><th></th></tr></thead><tbody></tbody></table>
      <div class="admin-only add-row" id="machine-add">
        <input id="machine-new-id" placeholder="Machine number, e.g. CNC8" aria-label="Machine number">
        <select id="machine-new-type" aria-label="Machine type"></select>
        <input id="machine-new-type-other" placeholder="New type" aria-label="New machine type" hidden>
        <select id="machine-new-hours" aria-label="Working time"><option value="19.5">2 shifts (19.5 h)</option><option value="9.5">1 shift (9.5 h)</option></select>
        <button type="button" class="btn" id="machine-add-btn">+ Add machine</button>
      </div>
    </div>

    <div class="card" id="holidays-card">
      <div class="card-head"><h2>Weekly off and holidays</h2></div>
      <p class="explainer">Weekly off: <strong id="weekly-off">Thursday</strong>. On a holiday the whole shop is closed, both shifts. Operator leave goes in Operator absences below.</p>
      <table class="tbl" id="holidays-table"><thead><tr><th>Date</th><th>Holiday</th><th></th></tr></thead><tbody></tbody></table>
      <div class="admin-only add-row" id="holiday-add">
        <input type="date" id="holiday-new-date" aria-label="Holiday date">
        <input id="holiday-new-name" placeholder="Name, e.g. Diwali" aria-label="Holiday name">
        <button type="button" class="btn" id="holiday-add-btn">+ Add holiday</button>
      </div>
    </div>
```

Match the class names (`tbl`, `btn`, `explainer`, `card-head`) to what the existing Settings cards use (grep the Operators card) and substitute.

- [ ] **Step 4: JS** (new section; reuse `escapeHtml`, `setStatus`, `runPlan`, `currentRole`, `$`, and the existing date formatter used by the absences panel for DD-MM-YYYY, found with `grep -n "function fmtDate\|toDDMMYYYY" web/app.js`):

```javascript
// ===== Machines and holidays =====
// Machines and the holiday list live in the app (2026-10-05, stage 2). Each write is
// one request and one re-plan; the user role sees both cards read-only.
let shopMachines = { machines: [], types: [] };

function machineHoursText(h) {
  if (h === 19.5) return "2 shifts (19.5 h)";
  if (h === 9.5) return "1 shift (9.5 h)";
  if (h === null || h === undefined) return "2 shifts (not set)";
  return `${h} h a day`;
}

async function shopWrite(url, method, body, okMsg) {
  try {
    const res = await fetch(url, { method, headers: { "Content-Type": "application/json" },
                                   body: JSON.stringify(body) });
    if (!res.ok) {
      let msg = await res.text();
      try { const d = JSON.parse(msg).detail; msg = typeof d === "string" ? d : msg; } catch (e) { /* text */ }
      setStatus(msg, true);
      return false;
    }
    setStatus(okMsg);
    await Promise.all([loadMachines(), loadHolidays()]);
    await runPlan(false);
    return true;
  } catch (e) { setStatus("Could not save: " + e.message, true); return false; }
}

async function loadMachines() {
  try {
    const res = await fetch("/machines");
    if (!res.ok) return;
    shopMachines = await res.json();
    renderMachines();
  } catch (e) { /* a convenience view */ }
}

function renderMachines() {
  const isAdmin = currentRole === "admin";
  const tbody = document.querySelector("#machines-table tbody");
  tbody.innerHTML = shopMachines.machines.map((m) => {
    const typeCell = isAdmin
      ? `<select class="mach-type" data-id="${escapeHtml(m.id)}">`
        + shopMachines.types.map((t) => `<option${t === m.type ? " selected" : ""}>${escapeHtml(t)}</option>`).join("")
        + `</select>`
      : escapeHtml(m.type);
    const hoursCell = isAdmin
      ? `<select class="mach-hours" data-id="${escapeHtml(m.id)}">`
        + [19.5, 9.5].map((h) => `<option value="${h}"${h === m.hours ? " selected" : ""}>${machineHoursText(h)}</option>`).join("")
        + (m.hours !== 19.5 && m.hours !== 9.5 ? `<option value="${escapeHtml(String(m.hours))}" selected>${escapeHtml(machineHoursText(m.hours))}</option>` : "")
        + `</select>`
      : escapeHtml(machineHoursText(m.hours));
    const used = m.used_by.length ? `${m.used_by.length} place(s)` : "Not used";
    const del = isAdmin ? `<button type="button" class="mach-del" data-id="${escapeHtml(m.id)}" title="Remove ${escapeHtml(m.name)}">✕</button>` : "";
    return `<tr><td>${escapeHtml(m.name)}</td><td>${typeCell}</td><td>${hoursCell}</td>`
      + `<td title="${escapeHtml(m.used_by.join("\n"))}">${escapeHtml(used)}</td><td>${del}</td></tr>`;
  }).join("") || `<tr><td colspan="5" class="empty">No machines yet.</td></tr>`;
  const sel = $("machine-new-type");
  sel.innerHTML = shopMachines.types.map((t) => `<option>${escapeHtml(t)}</option>`).join("")
    + `<option value="__other">Other type...</option>`;
  $("machine-add").style.display = isAdmin ? "" : "none";
}

async function loadHolidays() {
  try {
    const res = await fetch("/holidays");
    if (!res.ok) return;
    const data = await res.json();
    $("weekly-off").textContent = data.weekly_off;
    const isAdmin = currentRole === "admin";
    const today = new Date().toISOString().slice(0, 10);
    document.querySelector("#holidays-table tbody").innerHTML = data.holidays.map((h) =>
      `<tr class="${h.date < today ? "past" : ""}"><td>${escapeHtml(h.date.split("-").reverse().join("-"))}</td>`
      + `<td>${escapeHtml(h.name)}</td><td>${isAdmin ? `<button type="button" class="hol-del" data-date="${escapeHtml(h.date)}" title="Remove">✕</button>` : ""}</td></tr>`
    ).join("") || `<tr><td colspan="3" class="empty">No holidays entered.</td></tr>`;
    $("holiday-add").style.display = isAdmin ? "" : "none";
  } catch (e) { /* a convenience view */ }
}

function wireShopMasters() {
  const mt = $("machines-table");
  mt.addEventListener("change", (e) => {
    const t = e.target; const m = shopMachines.machines.find((x) => x.id === t.dataset.id);
    if (!m || currentRole !== "admin") return;
    const body = { id: m.id, type: m.type, hours: m.hours, version: m.version };
    if (t.classList.contains("mach-type")) body.type = t.value;
    else if (t.classList.contains("mach-hours")) body.hours = Number(t.value);
    else return;
    shopWrite("/machines", "PUT", body, `Saved ${m.name}. Updating the plan.`);
  });
  mt.addEventListener("click", (e) => {
    const t = e.target; if (!t.classList.contains("mach-del") || currentRole !== "admin") return;
    if (!confirm(`Remove machine ${t.dataset.id}?`)) return;
    shopWrite("/machines/delete", "POST", { id: t.dataset.id }, `Removed ${t.dataset.id}.`);
  });
  $("machine-new-type").addEventListener("change", (e) => {
    $("machine-new-type-other").hidden = e.target.value !== "__other";
  });
  $("machine-add-btn").addEventListener("click", async () => {
    if (currentRole !== "admin") return;
    const sel = $("machine-new-type").value;
    const type = sel === "__other" ? $("machine-new-type-other").value.trim() : sel;
    const ok = await shopWrite("/machines", "POST",
      { id: $("machine-new-id").value.trim(), type, hours: Number($("machine-new-hours").value) },
      "Machine added. Updating the plan.");
    if (ok) { $("machine-new-id").value = ""; $("machine-new-type-other").value = ""; }
  });
  $("holidays-table").addEventListener("click", (e) => {
    const t = e.target; if (!t.classList.contains("hol-del") || currentRole !== "admin") return;
    shopWrite("/holidays/delete", "POST", { date: t.dataset.date }, "Holiday removed. Updating the plan.");
  });
  $("holiday-add-btn").addEventListener("click", async () => {
    if (currentRole !== "admin") return;
    const ok = await shopWrite("/holidays", "POST",
      { date: $("holiday-new-date").value, name: $("holiday-new-name").value.trim() },
      "Holiday added. Updating the plan.");
    if (ok) { $("holiday-new-date").value = ""; $("holiday-new-name").value = ""; }
  });
}
// ===== end Machines and holidays =====
```

Call `wireShopMasters(); loadMachines(); loadHolidays();` in the boot routine next to `loadOperators()` / `loadMachineDowntime()`. After a machine write also refresh the other pickers that list machines: call `loadOperators()` and `loadMachineDowntime()` inside `shopWrite` after a `/machines` write, and set `imData = null` so the Item Process Master tab re-reads its machine list.

- [ ] **Step 5: CSS** (append; use the stylesheet's existing tokens, check with `grep -n "^\s*--" web/style.css`):

```css
/* Machines and holidays */
#machines-card select, #holidays-card select, #machines-card input, #holidays-card input { max-width: 100%; }
.add-row { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; }
#holidays-table tr.past td { color: var(--muted); }
#machines-table, #holidays-table { width: 100%; }
@media (max-width: 760px) { #machines-card .tbl, #holidays-card .tbl { display: block; overflow-x: auto; } }
```

- [ ] **Step 6: Run** `python3.12 -m pytest tests/test_shop_ui.py tests/test_role_parity.py -q` → PASS; `node --check web/app.js`; full suite.

- [ ] **Step 7: Browser check** on a throwaway local instance (never the live site): `STORE_DIR=$(mktemp -d) DEFAULT_SCHEDULER=new ADMIN_PASSWORD=1930rail USER_PASSWORD=anvitech12345678 python3.12 -m uvicorn api.main:app --port 8765`, put Test9 in the store first with a one-line Python (`book_store.save_masters_bytes(...)` with `STORE_DIR` set the same), log in as admin: Settings shows Machines and Holidays; change a machine's working time (plan refreshes); add CNC8 and find it in the Item Process Master machine picker and the Operators picker; removing CNC1 is refused with its users; add and remove a holiday; no Upload card anywhere; then as the user role: both cards read-only. Read the console. Stop the server.

- [ ] **Step 8: Commit**

```bash
git add web/index.html web/app.js web/style.css tests/test_shop_ui.py
git commit -m "feat(shop-masters): Machines and Weekly off and holidays cards in Settings"
```

---

### Task 9: verification, mutation testing, docs

**Files:**
- Create: `docs/superpowers/specs/2026-10-05-shop-masters-verification.md`
- Modify: `CLAUDE.md` (one new top bullet in CURRENT STATE, banner date), the spec's status line

- [ ] **Step 1: Byte-identical switch.** Reuse the stage-1 harness (its source is in `docs/superpowers/specs/2026-10-05-item-process-master-verification.md`). Baseline worktree: `git worktree add /Users/ritinwadekar/Desktop/anvitech-base 1199583`. Run Test5/8/9/9-ORIGINAL × flexible × clean / WIP+frozen in both; all 16 schedule hashes and the two-line quote must match. In the feature run, after seeding, make `openpyxl.load_workbook` and `ppc_engine.loaders.loader.open_workbook` raise, and plan again: hashes still match (proves no workbook is opened). If any hash differs, find the item/machine/date and why, and report it; do not change product code here. Remove the baseline worktree after.

- [ ] **Step 2: Role has no effect.** On Test9 (WIP+frozen), plan, then flip every new-engine operator's role (monkeypatch `_apply_app_operators`' role choice to `Role.HELPER` for all) and plan again: hash unchanged. Record it.

- [ ] **Step 3: Live-store copy.** Only if the owner supplies `MONGODB_URI`: copy every key to a local `STORE_DIR` read-only, compare `1199583` vs feature as in Step 1 plus one quote. Otherwise record "not run (no credential)".

- [ ] **Step 4: Every reader follows an edit.** Test9 local instance: set one CNC to 1 shift via `PUT /machines`, add a holiday inside the plan window via `POST /holidays`; show the plan, Analytics available hours, delay report (in-process if xlsxwriter missing), shift-wise, Daily Entry machine list, production analysis working minutes, quote, and the payload round trip each move.

- [ ] **Step 5: Mutation testing** with `python3.12 -B -m pytest` on the shop test files plus `tests/test_item_master*.py`, restoring each with `git checkout -- <file>`: (1) `_current_masters` never takes the tables-only branch; (2) `_new_masters` ignores the machines doc; (3) `parse_payload` ignores `machines`; (4) `run_candidate` skips `set_shop_masters`; (5) `_plan_fingerprint` drops the two digests; (6) `_inputs_signature` never appends them; (7) `machine_usage` returns `[]`; (8) `apply_machine_save` skips the version check; (9) the operator seed no longer reads the workbook; (10) `calendar_from_holiday_rows` returns no holidays. Report any mutation no test catches, by name.

- [ ] **Step 6: Full suite**, record counts.

- [ ] **Step 7: Docs.** Verification doc (harness sources, hash tables, role check, reader table, mutation table, timing of a cache-hit `/run` vs `1199583`, what was NOT run). CLAUDE.md: banner date 2026-10-05 (already) and ONE new top bullet in house style: what moved, the tables-only load, the operator-seed exception, Upload removed, measured results, the rule line **"Masters come from the app's tables. Nothing may open the stored workbook for planning; it is kept only as the one-time seed source and for rollback."**, and the deploy note: the live store's Machine master and holiday sheet are copied once at the first request after deploy; ask the owner to enter the 2026 holidays. Spec status line "implemented, unpushed".

- [ ] **Step 8: Commit**

```bash
git add docs/superpowers/specs/2026-10-05-shop-masters-verification.md docs/superpowers/specs/2026-10-05-shop-masters-no-excel-design.md CLAUDE.md
git commit -m "docs: machines and holidays verification and CLAUDE.md banner"
```

Do not push.
