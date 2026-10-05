# Item Process Master Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the item routings (Item's process Master) out of the uploaded Excel into an app-owned table with its own tab, so every planner reads routings from the app and an admin edits them in a friendly editor.

**Architecture:** A store key `anvitech:item_process_master` holds the routings as raw step text (same text the Excel cells held). It is seeded once from the workbook on file. Each of the two existing loaders gains ONE "rows → routings" function used for both the workbook and the table, and an optional `routing_rows=` parameter on its `load_all`. Every masters builder (`api.main._current_masters`, `new_engine._new_masters`, `optimize_service.parse_payload` / `run_candidate`) passes the table's rows in. A pure module `engine/item_master.py` owns seeding, digest, validation and punch safety; four admin-gated endpoints and a new tab sit on top.

**Tech Stack:** Python 3.12, FastAPI, openpyxl, plain HTML/JS/CSS (no framework), pytest.

**Spec:** `docs/superpowers/specs/2026-10-05-item-process-master-tab-design.md`

## Global Constraints

- Run every test with `python3.12 -m pytest` (system `python3` is 3.14; openpyxl crashes on import there).
- Work on branch `item-process-master` in worktree `~/Desktop/anvitech-ipm`. **Never push.** Pushing to `main` deploys to the live site; the owner decides.
- Store key, exactly: `anvitech:item_process_master`.
- At most **12** steps per item (workbook and ppc loader limit).
- Machine ids in a step are joined with `/` (the one separator both parsers agree on).
- The stored `items` dict keeps **insertion order** (seed = sheet order, new items appended). Never sort it: routing dict order reaches the scheduler, and byte-identical plans depend on it.
- Step cycle is stored in **minutes** (outsourced: flat block minutes); the UI shows outsourced in hours.
- The CNC/VMC +30% planning allowance stays in `engine/planning_time.py`; the table holds ORIGINAL times.
- `SCHEDULER_FINGERPRINT` is **not** bumped.
- `parse_payload` keeps returning its **8-tuple** (four callers assert the arity).
- Every write endpoint calls `require_admin(request)` (403 for the user role). The tab itself is visible to both roles.
- User-visible copy is plain operator English with **no em dashes**.
- The item code travels in the JSON body, never in the URL path (real codes contain dots, e.g. `61243661-01..`).

## Review Focus

1. **Seeded cycle cells that are not numbers** (a text "6.5", a blank): the seed must store them unchanged so both loaders read them exactly as before. Pinned in Task 3 (`test_seed_keeps_raw_cell_values`).
2. **Numeric item codes** (Excel gives `9611416360` as an int): seed key must equal what the loaders produced (`str(...).strip()`). Pinned in Task 3 (`test_seed_matches_loader_item_codes`).
3. **An old optimize job in flight during the deploy** (payload has no `item_master`): it must plan from the workbook it was built from, not crash. Pinned in Task 7 (`test_payload_without_item_master_falls_back_to_workbook`).
4. **Re-saving an unedited seeded item** must not change its digest (an int cycle must not become a float), or every applied optimization would be flagged stale for nothing. Pinned in Task 8 (`test_resaving_unchanged_item_keeps_digest`).
5. **Two admins saving the same item**: the second gets 409, nothing is overwritten. Pinned in Task 8 (`test_stale_version_is_refused`).

---

## File Structure

| File | Responsibility |
|---|---|
| `ppc_engine/loaders/masters_loader.py` (modify) | `routing_rows_from_sheet(wb)` + `routings_from_rows(rows, report, flexible)`; `load_routings` becomes their composition |
| `ppc_engine/loaders/loader.py` (modify) | `load_all(path, flexible_machines=False, routing_rows=None)` |
| `engine/loaders.py` (modify) | `_routing_from_steps(...)`, `_load_routings_from_rows(...)`; `load_all(xlsx_path, routing_rows=None)` |
| `engine/item_master.py` (create) | Pure: `seed_doc`, `routing_rows`, `digest`, `step_info`, `validate_item`, `punch_safety_errors`, `usage`, `apply_save`, `VersionConflict` |
| `engine/book_store.py` (modify) | `ITEM_MASTER_KEY`, `load_item_master`, `save_item_master` |
| `api/main.py` (modify) | seed-once + overlay in `_current_masters`; fingerprint + signature; upload; payload call site; 4 endpoints |
| `engine/new_engine.py` (modify) | `set_item_master` / `clear_item_master_override` / `_item_master_doc`; `_new_masters` reads the table |
| `engine/optimize_service.py` (modify) | payload field `item_master`; `parse_payload` + `run_candidate` use it |
| `web/index.html`, `web/app.js`, `web/style.css` (modify) | the tab |
| `tests/test_routing_rows.py` (create) | loader equivalence (Tasks 1-2) |
| `tests/test_item_master.py` (create) | pure module (Task 3) |
| `tests/test_item_master_wiring.py` (create) | store + api + new_engine + payload wiring (Tasks 4-7) |
| `tests/test_item_master_api.py` (create) | endpoints (Task 8) |
| `tests/test_item_master_ui.py` (create) | static markup checks (Task 9) |

---

### Task 1: ppc loader reads routings from rows

**Files:**
- Modify: `ppc_engine/loaders/masters_loader.py:123-201`
- Modify: `ppc_engine/loaders/loader.py` (`load_all`)
- Test: `tests/test_routing_rows.py`

**Interfaces:**
- Produces: `routing_rows_from_sheet(wb) -> list[tuple[str, str, list[tuple]]]` where each step tuple is `(name, cycle_raw, total_raw, suggested_raw, allotted_raw)`.
- Produces: `routings_from_rows(rows, report, flexible_machines=False) -> dict[str, Routing]`.
- Produces: `ppc_engine.loaders.loader.load_all(path, flexible_machines=False, routing_rows=None) -> LoadResult`. `routing_rows=None` reads the sheet (unchanged behaviour).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_routing_rows.py
"""Both loaders read routings through ONE rows->routings function, whatever the
source (workbook sheet or the app's Item Process Master table)."""
import io

from ppc_engine.loaders import loader as ppc_loader
from ppc_engine.loaders.masters_loader import routing_rows_from_sheet
from ppc_engine.loaders.workbook import open_workbook
from tests.new_sample_workbook import build_new_sample_bytes
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _ppc_rows(raw):
    wb = open_workbook(io.BytesIO(raw))
    try:
        return routing_rows_from_sheet(wb)
    finally:
        wb.close()


def test_ppc_rows_carry_raw_steps():
    rows = _ppc_rows(build_sample_bytes())
    by_code = {code: (desc, steps) for code, desc, steps in rows}
    desc, steps = by_code[ITEM_A]
    assert desc == "SAMPLE RING A"
    assert [s[0] for s in steps] == ["BANDSAW", "CNC OS", "INSP"]
    assert steps[1][1] == 5 and steps[1][3] == "CNC1/CNC2"
    assert [s[0] for s in by_code[ITEM_B][1]] == ["CNC", "WASHING"]


def test_ppc_load_all_from_rows_equals_from_sheet():
    for raw in (build_sample_bytes(), build_new_sample_bytes()):
        rows = _ppc_rows(raw)
        for flexible in (False, True):
            a = ppc_loader.load_all(io.BytesIO(raw), flexible_machines=flexible)
            b = ppc_loader.load_all(io.BytesIO(raw), flexible_machines=flexible,
                                    routing_rows=rows)
            assert a.masters.routings == b.masters.routings
            assert list(a.masters.routings) == list(b.masters.routings)
            assert a.masters.machines == b.masters.machines
            assert [(g.kind, g.ref) for g in a.report.gaps] == \
                   [(g.kind, g.ref) for g in b.report.gaps]


def test_ppc_rows_register_provisional_machines():
    """CNC9 is used only by ITEM_B's routing: the rows path must still register it."""
    rows = [(ITEM_B, "PIN", [("CNC", 4, None, "CNC9", None)])]
    res = ppc_loader.load_all(io.BytesIO(build_sample_bytes()), routing_rows=rows)
    assert "CNC9" in res.masters.machines
    assert list(res.masters.routings) == [ITEM_B]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3.12 -m pytest tests/test_routing_rows.py -v`
Expected: FAIL with `ImportError: cannot import name 'routing_rows_from_sheet'`

- [ ] **Step 3: Implement**

In `ppc_engine/loaders/masters_loader.py`, replace the body of `load_routings` (lines 123-201) with these three functions. Keep the existing docstring on `load_routings`. The per-step logic is moved verbatim; only the iteration source changes.

```python
def _block_is_process(header_row, start, n) -> bool:
    if start >= len(header_row):
        return False
    norm = "".join(str(header_row[start] or "").lower().split())
    return norm == f"process{n}"


def routing_rows_from_sheet(wb) -> list:
    """The "Item's process Master" as plain rows: ``[(code, description, steps)]``,
    each step ``(name, cycle_raw, total_raw, suggested_raw, allotted_raw)`` exactly
    as the cells hold them. Stops at the first blank step (an item using fewer
    processes). The ONE reading of the sheet: ``load_routings`` and the app's
    one-time seed of the Item Process Master both use it."""
    ws = find_sheet(wb, "Item's process Master", "Items process Master")
    rows = rows_of(ws)
    h = locate_header_row(rows, "Item code", "Item Code")
    t = Table.from_rows(rows, h)
    c_code = t.col("Item code", "Item Code")
    c_desc = t.col("Item Description", "Item Desc")
    proc1 = None
    for i, cell in enumerate(t.header):
        norm = "".join(str(cell or "").lower().split())
        if norm == "process1":
            proc1 = i
            break
    if proc1 is None:
        raise KeyError("could not find the 'Process 1' column in the routing sheet")
    out = []
    for row in t.data_rows:
        code = str(t.get(row, c_code) or "").strip()
        if not code:
            continue
        desc = str(t.get(row, c_desc) or "").strip()
        steps = []
        for p in range(12):
            start = proc1 + p * 5
            if not _block_is_process(t.header, start, p + 1):
                break  # no more process blocks
            name = t.get(row, start)
            if name is None or str(name).strip() == "":
                break  # this item uses fewer processes
            steps.append((str(name).strip(), t.get(row, start + 1), t.get(row, start + 2),
                          t.get(row, start + 3), t.get(row, start + 4)))
        out.append((code, desc, steps))
    return out


def routings_from_rows(rows, report: DataReport, flexible_machines: bool = False) -> dict:
    """``[(code, description, steps)]`` -> {item_code -> Routing}. The ONE rule that
    turns routing steps into operations, whatever their source (the workbook sheet
    or the app's Item Process Master table). A later row with the same code
    replaces the earlier one, as a sheet row always did."""
    routings: dict[str, Routing] = {}
    for code, desc, steps in rows:
        ops: list[Operation] = []
        for i, (name, cyc, _total, suggested, allotted) in enumerate(steps):
            name = str(name).strip()
            cyc = float(cyc) if isinstance(cyc, (int, float)) else 0.0
            kind = classify_operation(name, suggested, allotted, cyc)
            if kind in (OperationKind.MACHINING, OperationKind.MANUAL, OperationKind.INSPECTION):
                allot_opts = parse_machine_options(allotted)
                sug_opts = parse_machine_options(suggested)
                if flexible_machines:
                    options = tuple(dict.fromkeys(allot_opts + sug_opts))
                else:
                    options = allot_opts or sug_opts
                if not options:
                    report.add(
                        GapKind.ROUTING_GAP,
                        code,
                        f"step '{name}' (seq {i + 1}) has no machine and isn't outsourced",
                    )
            else:
                options = ()
            ops.append(Operation(i + 1, name, kind, options, cyc))
        routings[code] = Routing(code, desc, tuple(ops))
    return routings


def load_routings(wb, report: DataReport, flexible_machines: bool = False) -> dict[str, Routing]:
    # (keep the existing docstring here)
    return routings_from_rows(routing_rows_from_sheet(wb), report, flexible_machines)
```

In `ppc_engine/loaders/loader.py`, change `load_all`:

```python
from ppc_engine.loaders.masters_loader import (
    load_calendar,
    load_machines,
    load_operators,
    load_routings,
    register_provisional_machines,
    routings_from_rows,
)


def load_all(path, flexible_machines: bool = False, routing_rows=None) -> LoadResult:
    """Load a workbook at ``path`` into a LoadResult.

    ``flexible_machines`` (see load_routings): False = machining ops locked to their
    Allotted machine; True = ops may use any machine in their Suggested set.

    ``routing_rows`` (``[(code, description, steps)]``): when given, routings come
    from these rows (the app's Item Process Master) and the workbook's routing
    sheet is not read. None = read the sheet, as before.
    """
    wb = open_workbook(path)
    report = DataReport()

    machines = load_machines(wb)
    operators = load_operators(wb)
    calendar = load_calendar(wb)
    if routing_rows is None:
        routings = load_routings(wb, report, flexible_machines=flexible_machines)
    else:
        routings = routings_from_rows(routing_rows, report, flexible_machines=flexible_machines)
    register_provisional_machines(machines, routings, report)

    masters = Masters(machines=machines, operators=operators, routings=routings, calendar=calendar)
    orders = load_orders(wb)
    _block_unschedulable(masters, orders, report)

    return LoadResult(masters=masters, orders=orders, report=report)
```

- [ ] **Step 4: Run tests**

Run: `python3.12 -m pytest tests/test_routing_rows.py tests/test_new_engine.py tests/test_flexible_machines.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add ppc_engine/loaders/masters_loader.py ppc_engine/loaders/loader.py tests/test_routing_rows.py
git commit -m "refactor(loader): ppc routings read through one rows->routings rule; load_all takes routing_rows"
```

---

### Task 2: classic loader reads routings from rows

**Files:**
- Modify: `engine/loaders.py:329-377` (`_load_routings`) and `load_all` (line ~511)
- Test: `tests/test_routing_rows.py` (append)

**Interfaces:**
- Consumes: `routing_rows_from_sheet` (Task 1) only in the test.
- Produces: `engine.loaders.load_all(xlsx_path, routing_rows=None) -> (so_lines, masters)`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_routing_rows.py`)

```python
from engine import loaders as classic


def test_classic_load_all_from_rows_equals_from_sheet():
    for raw in (build_sample_bytes(), build_new_sample_bytes()):
        rows = _ppc_rows(raw)
        so_a, m_a = classic.load_all(io.BytesIO(raw))
        so_b, m_b = classic.load_all(io.BytesIO(raw), routing_rows=rows)
        assert list(m_a.routings) == list(m_b.routings)
        for code in m_a.routings:
            ra, rb = m_a.routings[code], m_b.routings[code]
            assert ra.description == rb.description
            assert [(p.seq, p.name, p.cycle_time, p.suggested_machine, p.allotted_machine)
                    for p in ra.processes] == \
                   [(p.seq, p.name, p.cycle_time, p.suggested_machine, p.allotted_machine)
                    for p in rb.processes]
        assert set(m_a.machines) == set(m_b.machines)
        assert [s.key for s in so_a] == [s.key for s in so_b]


def test_classic_rows_report_duplicate_process():
    rows = [(ITEM_A, "RING", [("CNC", 3, None, "CNC1", None),
                              ("cnc ", 4, None, "CNC2", None)])]
    _so, m = classic.load_all(io.BytesIO(build_sample_bytes()), routing_rows=rows)
    assert any(r["kind"] == "DUPLICATE_PROCESS" and r["ref"] == ITEM_A for r in m.report)
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3.12 -m pytest tests/test_routing_rows.py -k classic -v`
Expected: FAIL with `TypeError: load_all() got an unexpected keyword argument 'routing_rows'`

- [ ] **Step 3: Implement** in `engine/loaders.py`. Replace `_load_routings` with:

```python
def _routing_from_steps(code, description, steps, masters: Masters,
                        customer: str = "", rm_type: str = "", moq=None) -> Routing:
    """One routing from its steps ``(name, cycle, total, suggested, allotted)``. A
    ``None`` step or a blank name is skipped but still takes its sequence number
    (the workbook's fixed 5-column blocks). The ONE classic rule for turning steps
    into Processes, whether they come from the sheet or the Item Process Master."""
    processes = []
    for p, step in enumerate(steps):
        if step is None:
            continue
        name, cyc, total, sm, am = step
        if not name or str(name).strip() == "":
            continue
        processes.append(
            Process(
                seq=p + 1,
                name=str(name).strip(),
                cycle_time=_num(cyc, masters, f"{code} P{p+1} cycle"),
                total_time=_num(total, masters, f"{code} P{p+1} total"),
                suggested_machine=(str(sm).strip() if sm else None),
                allotted_machine=(str(am).strip() if am else None),
            )
        )
    # (move the existing DUPLICATE_PROCESS comment block here unchanged)
    _by_name: dict = {}
    for pr in processes:
        _by_name.setdefault(normalize_process_name(pr.name), []).append(pr.seq)
    for _nm, _seqs in _by_name.items():
        if len(_seqs) > 1:
            masters.add_report(
                "DUPLICATE_PROCESS", str(code),
                f"process name {_nm!r} appears at steps {_seqs}: per-step progress "
                f"would merge — give each step a distinct name")
    return Routing(item_code=code, description=description, customer=customer,
                   rm_type=rm_type, moq=moq, processes=processes)


def _load_routings(wb, masters: Masters):
    ws = _find_sheet(wb, "Item's process Master")
    if ws is None:
        masters.add_report("MISSING_SHEET", "Item's process Master", "sheet not found")
        return
    for row in ws.iter_rows(min_row=3, values_only=True):
        item_code = _cell(row, 3)
        if item_code is None or str(item_code).strip() == "":
            continue  # blank / separator row
        code = str(item_code).strip()
        steps = []
        for p in range(MAX_PROCESSES):
            base = ROUTING_FIRST_PROCESS_COL + p * 5
            steps.append((_cell(row, base), _cell(row, base + 1), _cell(row, base + 2),
                          _cell(row, base + 3), _cell(row, base + 4)))
        masters.routings[code] = _routing_from_steps(
            code,
            str(_cell(row, 2)).strip() if _cell(row, 2) else "",
            steps, masters,
            customer=str(_cell(row, 1)).strip() if _cell(row, 1) else "",
            rm_type=str(_cell(row, 6)).strip() if _cell(row, 6) else "",
            moq=_num(_cell(row, 10)),
        )


def _load_routings_from_rows(rows, masters: Masters):
    """Routings from the app's Item Process Master rows ``[(code, desc, steps)]``.
    Customer / RM type / MOQ are not kept by the app (nothing reads them)."""
    for code, desc, steps in rows:
        masters.routings[str(code).strip()] = _routing_from_steps(
            str(code).strip(), desc or "", steps, masters)
```

Change `load_all`:

```python
def load_all(xlsx_path, routing_rows=None):
    """(keep the existing docstring, then add:)

    ``routing_rows`` (``[(code, description, steps)]``): when given, routings come
    from the app's Item Process Master and the workbook's routing sheet is not read.
    """
    wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    masters = Masters()
    try:
        _load_machines(wb, masters)
        _load_operators(wb, masters)
        _load_calendar(wb, masters)
        if routing_rows is None:
            _load_routings(wb, masters)
        else:
            _load_routings_from_rows(routing_rows, masters)
        so_lines = _load_so_lines(wb, masters)
    finally:
        wb.close()
    # (rest unchanged)
```

- [ ] **Step 4: Run tests**

Run: `python3.12 -m pytest tests/test_routing_rows.py tests/test_loaders.py tests/test_golden.py -q` (if a listed file does not exist, drop it; `ls tests | grep -i "loader\|golden"` first).
Expected: all PASS, golden unchanged.

- [ ] **Step 5: Commit**

```bash
git add engine/loaders.py tests/test_routing_rows.py
git commit -m "refactor(loader): classic routings read through one steps->routing rule; load_all takes routing_rows"
```

---

### Task 3: pure `engine/item_master.py`

**Files:**
- Create: `engine/item_master.py`
- Test: `tests/test_item_master.py`

**Interfaces:**
- Consumes: `routing_rows_from_sheet` (Task 1); `ppc_engine.loaders.normalize.classify_operation`, `parse_machine_options`; `engine.loaders.normalize_process_name`; `engine.orderbook._process_totals`.
- Produces (all pure):
  - `MAX_STEPS = 12`
  - `class VersionConflict(Exception)`
  - `seed_doc(raw: bytes, now_iso: str) -> dict | None`
  - `routing_rows(doc: dict) -> list[tuple[str, str, list[tuple]]]`
  - `digest(doc: dict | None) -> str`
  - `step_info(step: dict) -> {"kind": "machine"|"outsourced"|"dispatch", "machining": bool, "machines": list[str]}`
  - `validate_item(code: str, item: dict, machine_ids: set, old_item: dict | None) -> list[str]`
  - `punch_safety_errors(code: str, old_steps: list, new_steps: list, orders, actuals) -> list[str]`
  - `usage(code: str, orders, drafts) -> list[str]`
  - `apply_save(doc: dict, code: str, item: dict, expected_version: int | None, now_iso: str, user: str, create: bool) -> dict`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_item_master.py
from datetime import date

import pytest

from engine import item_master as im
from engine.models import Actual, Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B

NOW = "2026-10-05T10:00:00"


def _doc():
    return im.seed_doc(build_sample_bytes(), NOW)


def test_seed_reads_every_item_in_sheet_order():
    doc = _doc()
    assert list(doc["items"]) == [ITEM_A, ITEM_B]
    a = doc["items"][ITEM_A]
    assert a["description"] == "SAMPLE RING A" and a["version"] == 1
    assert a["steps"][1] == {"name": "CNC OS", "cycle": 5, "allotted": None,
                             "suggested": "CNC1/CNC2"}
    assert doc["seed_digest"] == im.digest(doc)


def test_seed_keeps_raw_cell_values(monkeypatch):
    rows = [("X1", "D", [("CNC", "6.5", None, "CNC1", None), ("INSP", None, None, "MI1", None)])]
    monkeypatch.setattr(im, "_sheet_rows", lambda raw: rows)
    doc = im.seed_doc(b"ignored", NOW)
    assert doc["items"]["X1"]["steps"][0]["cycle"] == "6.5"
    assert doc["items"]["X1"]["steps"][1]["cycle"] is None


def test_seed_matches_loader_item_codes():
    import io
    from engine.loaders import load_all
    _so, m = load_all(io.BytesIO(build_sample_bytes()))
    assert list(_doc()["items"]) == list(m.routings)


def test_seed_returns_none_without_routing_sheet():
    import io, openpyxl
    wb = openpyxl.Workbook(); buf = io.BytesIO(); wb.save(buf)
    assert im.seed_doc(buf.getvalue(), NOW) is None


def test_routing_rows_round_trip_order_and_text():
    rows = im.routing_rows(_doc())
    assert [r[0] for r in rows] == [ITEM_A, ITEM_B]
    assert rows[0][2][1] == ("CNC OS", 5, None, "CNC1/CNC2", None)


def test_digest_ignores_bookkeeping_but_sees_content():
    doc = _doc()
    d0 = im.digest(doc)
    doc["items"][ITEM_A]["version"] = 9
    doc["items"][ITEM_A]["updated_at"] = "later"
    assert im.digest(doc) == d0
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 4
    assert im.digest(doc) != d0
    assert im.digest(None) == "none"


def test_step_info_kinds():
    assert im.step_info({"name": "DISPATCH", "cycle": None, "allotted": "", "suggested": ""})["kind"] == "dispatch"
    assert im.step_info({"name": "BAND SAW", "cycle": 2880, "allotted": "OS", "suggested": ""})["kind"] == "outsourced"
    cnc = im.step_info({"name": "CNC FIRST", "cycle": 6, "allotted": "CNC4", "suggested": "CNC5"})
    assert cnc == {"kind": "machine", "machining": True, "machines": ["CNC4"]}
    assert im.step_info({"name": "DEBUR", "cycle": 1, "allotted": "MD1", "suggested": ""})["machining"] is False


MACHINES = {"CNC1", "CNC2", "CNC4", "MI1", "MD1", "BS1"}


def _item(steps, desc="D"):
    return {"description": desc, "steps": steps}


def test_validate_accepts_a_good_item():
    item = _item([{"name": "CNC", "cycle": 6, "allotted": "CNC4", "suggested": ""},
                  {"name": "DISPATCH", "cycle": None, "allotted": "", "suggested": ""}])
    assert im.validate_item("X1", item, MACHINES, None) == []


@pytest.mark.parametrize("item,needle", [
    (_item([]), "at least one step"),
    (_item([{"name": f"S{i}", "cycle": 1, "allotted": "MD1", "suggested": ""} for i in range(13)]), "12 steps"),
    (_item([{"name": " ", "cycle": 1, "allotted": "MD1", "suggested": ""}]), "needs a name"),
    (_item([{"name": "CNC", "cycle": 1, "allotted": "CNC4", "suggested": ""},
            {"name": "cnc ", "cycle": 1, "allotted": "CNC4", "suggested": ""}]), "twice"),
    (_item([{"name": "CNC", "cycle": -1, "allotted": "CNC4", "suggested": ""}]), "0 or more"),
    (_item([{"name": "CNC", "cycle": "abc", "allotted": "CNC4", "suggested": ""}]), "a number"),
    (_item([{"name": "CNC", "cycle": 0, "allotted": "CNC4", "suggested": ""}]), "more than 0"),
    (_item([{"name": "DEBUR", "cycle": 2, "allotted": "", "suggested": ""}]), "needs a machine"),
    (_item([{"name": "CNC", "cycle": 2, "allotted": "CNC99", "suggested": ""}]), "CNC99"),
])
def test_validate_refuses(item, needle):
    errs = im.validate_item("X1", item, MACHINES, None)
    assert errs and any(needle in e for e in errs), errs


def test_validate_refuses_blank_code():
    item = _item([{"name": "CNC", "cycle": 6, "allotted": "CNC4", "suggested": ""}])
    assert any("item code" in e for e in im.validate_item("  ", item, MACHINES, None))


def test_unknown_machine_already_on_item_may_stay():
    old = _item([{"name": "CNC", "cycle": 6, "allotted": "CNC99", "suggested": ""}])
    new = _item([{"name": "CNC", "cycle": 7, "allotted": "CNC99", "suggested": ""}])
    assert im.validate_item("X1", new, MACHINES, old) == []


def _book(produced_on="CNC OS", qty=120, completed=False):
    o = Order("SO113", ITEM_A, "A", 200, date(2026, 10, 30))
    o.completed = completed
    acts = [Actual(so_no="SO113", item_code=ITEM_A, process=produced_on,
                   qty_produced=qty, qty_rejected=0, operator="X")]
    return {o.key: o}, acts


OLD = [{"name": "BANDSAW", "cycle": 3, "allotted": None, "suggested": "BS1"},
       {"name": "CNC OS", "cycle": 5, "allotted": None, "suggested": "CNC1/CNC2"},
       {"name": "INSP", "cycle": 2, "allotted": None, "suggested": "MI1"}]


def test_renaming_a_punched_step_is_refused_with_reason():
    orders, acts = _book()
    new = [dict(OLD[0]), dict(OLD[1], name="CNC FIRST SIDE"), dict(OLD[2])]
    errs = im.punch_safety_errors(ITEM_A, OLD, new, orders.values(), acts)
    assert errs == ["CNC OS has 120 punched on SO113. Finish or complete that order "
                    "before renaming or removing this step."]


def test_removing_a_punched_step_is_refused():
    orders, acts = _book()
    assert im.punch_safety_errors(ITEM_A, OLD, [OLD[0], OLD[2]], orders.values(), acts)


def test_reordering_punched_steps_is_refused():
    orders, acts = _book()
    acts.append(Actual(so_no="SO113", item_code=ITEM_A, process="INSP",
                       qty_produced=50, qty_rejected=0, operator="X"))
    new = [OLD[0], OLD[2], OLD[1]]
    errs = im.punch_safety_errors(ITEM_A, OLD, new, orders.values(), acts)
    assert errs and "same order" in errs[0]


def test_safe_edits_are_allowed():
    orders, acts = _book()
    new = [dict(OLD[0], name="BAND SAW CUT"), dict(OLD[1], cycle=9), OLD[2],
           {"name": "WASH", "cycle": 1, "allotted": "MW1", "suggested": ""}]
    assert im.punch_safety_errors(ITEM_A, OLD, new, orders.values(), acts) == []


def test_completed_order_does_not_block():
    orders, acts = _book(completed=True)
    assert im.punch_safety_errors(ITEM_A, OLD, [OLD[0], OLD[2]], orders.values(), acts) == []


def test_usage_lists_orders_and_drafts():
    orders, _ = _book()
    drafts = [{"so_no": "SO900", "item_code": ITEM_A, "qty": 5}]
    assert im.usage(ITEM_A, orders.values(), drafts) == ["SO113", "SO900 (Add New Orders draft)"]
    assert im.usage(ITEM_B, orders.values(), drafts) == []


def test_apply_save_versions_and_conflicts():
    doc = _doc()
    item = {"description": "NEW", "steps": doc["items"][ITEM_A]["steps"]}
    out = im.apply_save(doc, ITEM_A, item, expected_version=1, now_iso=NOW, user="anvitech", create=False)
    assert out["items"][ITEM_A]["version"] == 2
    assert out["items"][ITEM_A]["description"] == "NEW"
    assert doc["items"][ITEM_A]["version"] == 1          # input never mutated
    with pytest.raises(im.VersionConflict):
        im.apply_save(out, ITEM_A, item, expected_version=1, now_iso=NOW, user="x", create=False)
    with pytest.raises(im.VersionConflict):
        im.apply_save(out, ITEM_A, item, expected_version=None, now_iso=NOW, user="x", create=True)
    new = im.apply_save(out, "Z9", item, expected_version=None, now_iso=NOW, user="x", create=True)
    assert list(new["items"])[-1] == "Z9"               # appended, never sorted
```

Before writing the code, confirm `Actual`'s constructor field names with `grep -n "class Actual" -A20 engine/models.py` and adjust the test's `Actual(...)` keywords to match (keep the values).

- [ ] **Step 2: Run to verify it fails**

Run: `python3.12 -m pytest tests/test_item_master.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'engine.item_master'`

- [ ] **Step 3: Implement** `engine/item_master.py`

```python
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
        if find_sheet(wb, "Item's process Master", "Items process Master") is None:
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
```

Note on `apply_save`: it stores the step fields exactly as given (a seeded `None` stays `None`). The endpoint (Task 8, `_keep_blank_as_stored`) turns a `""` sent back by the browser into `None` where the stored value was `None`, so re-saving an unedited seeded item leaves the digest unchanged (Review Focus 4).

- [ ] **Step 4: Run tests**

Run: `python3.12 -m pytest tests/test_item_master.py -q`
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/item_master.py tests/test_item_master.py
git commit -m "feat(item-master): pure module for the app-owned routing table"
```

---

### Task 4: store key

**Files:**
- Modify: `engine/book_store.py` (constants near line 29; functions after `save_operator_table`, ~line 348)
- Test: `tests/test_item_master_wiring.py` (create)

**Interfaces:**
- Produces: `book_store.ITEM_MASTER_KEY = "anvitech:item_process_master"`, `load_item_master() -> dict | None`, `save_item_master(doc: dict) -> None`.

- [ ] **Step 1: Failing test**

```python
# tests/test_item_master_wiring.py
"""The Item Process Master reaches every planner: store, api masters, new engine,
cloud payload."""
import io
import json
from datetime import date

import pytest

from engine import book_store, item_master as im
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def test_store_round_trip():
    assert book_store.load_item_master() is None
    doc = im.seed_doc(build_sample_bytes(), "2026-10-05T10:00:00")
    book_store.save_item_master(doc)
    assert book_store.load_item_master() == doc
    assert list(book_store.load_item_master()["items"]) == [ITEM_A, ITEM_B]
```

- [ ] **Step 2: Run** `python3.12 -m pytest tests/test_item_master_wiring.py -q` → FAIL (`AttributeError: ... load_item_master`).

- [ ] **Step 3: Implement** in `engine/book_store.py`:

```python
ITEM_MASTER_KEY = "anvitech:item_process_master"  # kv: json {seeded_at, seeded_from_sha, seed_digest, items}
```

```python
def load_item_master():
    """The app-owned Item Process Master (routings), or ``None`` if never seeded."""
    raw = get_store().kv_get(ITEM_MASTER_KEY)
    return json.loads(raw) if raw else None


def save_item_master(doc: dict) -> None:
    get_store().kv_set(ITEM_MASTER_KEY, json.dumps(doc))
```

- [ ] **Step 4: Run** the same test → PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/book_store.py tests/test_item_master_wiring.py
git commit -m "feat(item-master): store key anvitech:item_process_master"
```

---

### Task 5: api masters read the table (seed once), cache keys, upload

**Files:**
- Modify: `api/main.py`: `_current_masters` (~337), `_inputs_signature` (~390-431), `_plan_fingerprint` (~1560), `upload` (~2751), plus a new `_item_master_doc()` helper next to `_current_masters`.
- Test: `tests/test_item_master_wiring.py` (append)

**Interfaces:**
- Consumes: Tasks 2-4.
- Produces: `api.main._item_master_doc() -> dict | None` (seeds once). Upload response gains `"routings_note": str | None`.

- [ ] **Step 1: Failing tests** (append)

```python
def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
                           Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21))])


def test_first_masters_read_seeds_the_table_once():
    m = _api(); _seed_book()
    assert book_store.load_item_master() is None
    masters = m._current_masters()
    doc = book_store.load_item_master()
    assert list(doc["items"]) == list(masters.routings) == [ITEM_A, ITEM_B]
    doc["items"][ITEM_A]["description"] = "EDITED"
    book_store.save_item_master(doc)
    m._current_masters()
    assert book_store.load_item_master()["items"][ITEM_A]["description"] == "EDITED"


def test_masters_follow_a_table_edit():
    m = _api(); _seed_book()
    m._current_masters()
    doc = book_store.load_item_master()
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 30
    book_store.save_item_master(doc)
    proc = m._current_masters().routings[ITEM_A].processes[0]
    assert proc.cycle_time == 30


def test_inputs_signature_unchanged_by_seeding_but_moved_by_an_edit(monkeypatch):
    m = _api(); _seed_book()
    cfg = m._load_plan_config()
    m._current_masters()                         # seeds
    seeded = m._inputs_signature(cfg)
    # Without the table part the formula is the pre-feature one; a freshly seeded
    # table must not change the signature (no applied optimization goes stale on deploy).
    real_load = book_store.load_item_master
    monkeypatch.setattr(m.book_store, "load_item_master", lambda: None)
    m._MASTERS_CACHE["masters"] = None
    assert m._inputs_signature(cfg) == seeded
    monkeypatch.setattr(m.book_store, "load_item_master", real_load)
    doc = book_store.load_item_master()
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 30
    book_store.save_item_master(doc)
    assert m._inputs_signature(cfg) != seeded


def test_plan_fingerprint_moves_with_the_table():
    m = _api(); _seed_book()
    cfg = m._load_plan_config()
    f0 = m._plan_fingerprint(cfg)
    doc = book_store.load_item_master()
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 30
    book_store.save_item_master(doc)
    assert m._plan_fingerprint(cfg) != f0


def _admin(m):
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return c


def _workbook_with_cycle(cycle):
    from tests.sample_workbook import build_workbook
    wb = build_workbook()
    wb["Item's process Master"].cell(row=3, column=14).value = cycle   # ITEM_A step 1 cycle
    buf = io.BytesIO(); wb.save(buf); return buf.getvalue()


def test_upload_after_seed_does_not_touch_routings():
    m = _api(); _seed_book()
    m._current_masters()
    r = _admin(m).post("/upload", files={"file": ("x.xlsx", _workbook_with_cycle(99))})
    assert r.status_code == 200, r.text
    assert "Item Process Master" in r.json()["routings_note"]
    assert m._current_masters().routings[ITEM_A].processes[0].cycle_time == 3


def test_upload_without_routing_sheet_is_accepted_once_seeded():
    m = _api(); _seed_book()
    m._current_masters()
    from tests.sample_workbook import build_workbook
    wb = build_workbook(); del wb["Item's process Master"]
    buf = io.BytesIO(); wb.save(buf)
    r = _admin(m).post("/upload", files={"file": ("x.xlsx", buf.getvalue())})
    assert r.status_code == 200, r.text
    assert list(m._current_masters().routings) == [ITEM_A, ITEM_B]
```

Check `build_workbook`'s Item sheet: header row 2, ITEM_A on row 3, column index 13 (0-based) is "Process 1 cycle time", i.e. openpyxl `column=14`. If `_set` uses 1-based indices instead, adjust the column so the cell is ITEM_A's Process 1 cycle (value 3 in the sample).

- [ ] **Step 2: Run** `python3.12 -m pytest tests/test_item_master_wiring.py -q` → the new tests FAIL.

- [ ] **Step 3: Implement** in `api/main.py`. Add `from engine import item_master` to the engine imports.

```python
def _item_master_doc():
    """The app-owned Item Process Master, seeded ONCE from the workbook on file
    the first time it is needed (same pattern as operators, 2026-07-18). After that
    the workbook's routing sheet is never read again, a later upload included."""
    doc = book_store.load_item_master()
    if doc is None:
        raw = book_store.load_masters_bytes()
        if raw is not None:
            doc = item_master.seed_doc(raw, _ist_now().isoformat(timespec="seconds"))
            if doc is not None:
                book_store.save_item_master(doc)
    return doc


def _current_masters():
    """(keep docstring; add:) Routings come from the app's Item Process Master
    (seeded once from the workbook); the parsed masters are cached per (store,
    table digest), so a routing edit re-parses once."""
    doc = _item_master_doc()
    key = (_store_env_key(), item_master.digest(doc))
    if _MASTERS_CACHE["masters"] is not None and _MASTERS_CACHE["key"] == key:
        base = _MASTERS_CACHE["masters"]
    else:
        raw = book_store.load_masters_bytes()
        if raw is None:
            base = Masters()
        else:
            _, base = load_all(io.BytesIO(raw),
                               routing_rows=item_master.routing_rows(doc) if doc else None)
        _MASTERS_CACHE.update(key=key, masters=base,
                              sha=hashlib.sha256(raw).hexdigest() if raw else "none")
    return _with_operator_overlay(base)
```

In `_inputs_signature`, replace the final two lines:

```python
    parts = [_masters_sha(), d, op_blob]
    # Item routings live in the app now (2026-10-05). Fold the table in ONLY once it
    # differs from what it was seeded from: right after the seed it equals the
    # workbook (already covered by the masters sha), so an applied optimization is
    # not flagged stale by the switch itself, while any real edit flags it.
    doc = book_store.load_item_master()
    if doc and item_master.digest(doc) != doc.get("seed_digest"):
        parts.append(["item_master", item_master.digest(doc)])
    blob = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
```

In `_plan_fingerprint`'s `parts` dict, after `"masters": _masters_sha(),` add:

```python
        # Routings are app-owned (Item Process Master); an edit must refresh every screen.
        "item_master": item_master.digest(book_store.load_item_master()),
```

In `upload`, replace the `load_all` / `if not masters.routings` block and the return:

```python
    doc = book_store.load_item_master()
    try:
        _so_lines_ignored, masters = load_all(
            io.BytesIO(contents),
            routing_rows=item_master.routing_rows(doc) if doc else None)
    except Exception as e:  # noqa: BLE001 — surface parse failures to the user
        raise HTTPException(status_code=400, detail=f"Could not read Excel: {e}")

    if doc is None and not masters.routings:
        raise HTTPException(  # (keep existing detail text)
            ...)
    book_store.save_masters_bytes(contents)
    _MASTERS_CACHE["masters"] = None  # invalidate cache → re-read on next plan

    return {
        "name": file.filename,
        "masters_updated": True,
        "orders_changed": 0,
        "summary": {"items": len(masters.routings), "machines": len(masters.machines)},
        "report": _report_after_upload(masters),
        "routings_note": ("Item routings are now managed in the Item Process Master tab. "
                          "The routing sheet in this file was not read.") if doc else None,
    }
```

Update the `upload` docstring's first line to "Replace the masters (machines, holidays, operator seed) from an uploaded workbook. Item routings are app-owned (Item Process Master) once seeded."

- [ ] **Step 4: Reader sweep.** Run:
`grep -n "load_all(\|load_masters_bytes()" api/main.py engine/*.py`
Expected sites only: `_current_masters`, `upload`, `_masters_sha` (via cache), the payload call site (Task 7), `new_engine._new_masters` (Task 6), `optimize_service.parse_payload` (Task 7). Any other site that parses the workbook for routings must be routed through `_current_masters()`; record the sweep result in the commit message.

- [ ] **Step 5: Run** `python3.12 -m pytest tests/test_item_master_wiring.py tests/test_operators_api.py tests/test_plan_cache.py tests/test_plan_cache_freshness.py -q` → PASS. Then the full suite `python3.12 -m pytest -q -x`. Fix any test that asserted the old upload refusal for a routing-less file only if it ran with a seeded table (it should not need changing; report any change).

- [ ] **Step 6: Commit**

```bash
git add api/main.py tests/test_item_master_wiring.py
git commit -m "feat(item-master): app masters read routings from the table, seeded once; cache keys; upload leaves routings alone"
```

---

### Task 6: new engine reads the table

**Files:**
- Modify: `engine/new_engine.py:66-100`
- Test: `tests/test_item_master_wiring.py` (append)

**Interfaces:**
- Produces: `new_engine.set_item_master(doc: dict | None)`, `new_engine.clear_item_master_override()`, `new_engine._item_master_doc() -> dict | None`. Override semantics: while set, the given doc is used (`None` = read the workbook's routing sheet); when cleared, the store is read.

- [ ] **Step 1: Failing tests** (append)

```python
def test_new_engine_masters_follow_the_table():
    from engine import new_engine
    new_engine._MASTERS_CACHE.clear()
    m = _api(); _seed_book(); m._current_masters()
    before = new_engine._new_masters(False).routings[ITEM_A].operations[0].cycle_min
    doc = book_store.load_item_master()
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 30
    book_store.save_item_master(doc)
    after = new_engine._new_masters(False).routings[ITEM_A].operations[0].cycle_min
    assert before == 3.0 and after == 30.0      # BANDSAW is manual: no +30%


def test_new_engine_override_wins_and_none_means_workbook():
    from engine import new_engine
    new_engine._MASTERS_CACHE.clear()
    _seed_book()
    doc = im.seed_doc(build_sample_bytes(), "t")
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 44
    try:
        new_engine.set_item_master(doc)
        assert new_engine._new_masters(False).routings[ITEM_A].operations[0].cycle_min == 44.0
        new_engine.set_item_master(None)
        assert new_engine._new_masters(False).routings[ITEM_A].operations[0].cycle_min == 3.0
    finally:
        new_engine.clear_item_master_override()
```

- [ ] **Step 2: Run** → FAIL (`AttributeError: set_item_master`).

- [ ] **Step 3: Implement** in `engine/new_engine.py` (add `from engine import item_master` at the imports):

```python
# The Item Process Master injected out-of-band (the cloud worker carries it in its
# payload, like the workbook). _UNSET = read the store; None = read the workbook's
# own routing sheet (a payload built before the table existed).
_UNSET = object()
_OVERRIDE_ITEM_MASTER = _UNSET


def set_item_master(doc) -> None:
    global _OVERRIDE_ITEM_MASTER
    _OVERRIDE_ITEM_MASTER = doc


def clear_item_master_override() -> None:
    global _OVERRIDE_ITEM_MASTER
    _OVERRIDE_ITEM_MASTER = _UNSET


def _item_master_doc():
    if _OVERRIDE_ITEM_MASTER is not _UNSET:
        return _OVERRIDE_ITEM_MASTER
    return book_store.load_item_master()


def _new_masters(flexible: bool = False):
    """(keep docstring; change last sentence to:) Cached by (workbook sha, Item
    Process Master digest, flexible)."""
    raw = _OVERRIDE_BYTES if _OVERRIDE_BYTES is not None else book_store.load_masters_bytes()
    if not raw:
        raise RuntimeError("new_engine: no masters workbook available (store empty and none injected)")
    doc = _item_master_doc()
    h = hashlib.sha256(raw).hexdigest()
    d = item_master.digest(doc)
    key = (h, d, bool(flexible))
    cached = _MASTERS_CACHE.get(key)
    if cached is None:
        for k in [k for k in _MASTERS_CACHE if k[:2] != (h, d)]:
            del _MASTERS_CACHE[k]
        cached = load_all(io.BytesIO(raw), flexible_machines=bool(flexible),
                          routing_rows=item_master.routing_rows(doc) if doc else None).masters
        # Planning schedules CNC/VMC at cycle + 30%; the table keeps the original
        # (engine/planning_time.py is the one place that rule lives).
        cached = replace(cached, routings=pad_routings(cached.routings))
        _MASTERS_CACHE[key] = cached
    return cached
```

Check `grep -rn "_MASTERS_CACHE" tests/ engine/` for code that builds keys of the old 2-tuple shape; a test that only calls `.clear()` needs no change.

- [ ] **Step 4: Run** `python3.12 -m pytest tests/test_item_master_wiring.py tests/test_new_engine.py tests/test_planning_cycle_time.py tests/test_flexible_machines.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/new_engine.py tests/test_item_master_wiring.py
git commit -m "feat(item-master): new engine plans from the table; worker override hook"
```

---

### Task 7: cloud / Mac / GitHub payload carries the table

**Files:**
- Modify: `engine/optimize_service.py` (`build_payload` ~197, `parse_payload` ~230, `run_candidate` ~345)
- Modify: `api/main.py` payload call site (~1870)
- Test: `tests/test_item_master_wiring.py` (append)

**Interfaces:**
- Produces: `build_payload(..., item_master=None)` adds key `"item_master"`; `parse_payload` returns the same 8-tuple, its `masters` built from the payload's table when present.

- [ ] **Step 1: Failing tests** (append)

```python
def _payload(doc):
    from engine import optimize_service as svc
    from engine.config import Config
    _seed_book()
    orders = book_store.load_active_orders()
    return svc.build_payload(orders, [], build_sample_bytes(),
                             Config(plan_start_date=date(2025, 3, 1)), seed=1,
                             item_master=doc)


def test_payload_round_trips_the_table():
    from engine import optimize_service as svc
    doc = im.seed_doc(build_sample_bytes(), "t")
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 44
    payload = json.loads(json.dumps(_payload(doc)))
    assert payload["item_master"] == doc
    parsed = svc.parse_payload(payload)
    assert len(parsed) == 8
    assert parsed[2].routings[ITEM_A].processes[0].cycle_time == 44


def test_payload_without_item_master_falls_back_to_workbook():
    from engine import optimize_service as svc
    payload = json.loads(json.dumps(_payload(None)))
    payload.pop("item_master")                      # a job built before this deploy
    parsed = svc.parse_payload(payload)
    assert parsed[2].routings[ITEM_A].processes[0].cycle_time == 3


def test_run_candidate_feeds_the_table_to_the_new_engine(monkeypatch):
    from engine import new_engine, optimize_service as svc
    seen = {}
    monkeypatch.setattr(new_engine, "set_item_master", lambda d: seen.setdefault("doc", d))
    monkeypatch.setattr(svc, "prepare_contest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    doc = im.seed_doc(build_sample_bytes(), "t")
    payload = json.loads(json.dumps(_payload(doc)))
    payload["config"]["scheduler"] = "new"
    try:
        with pytest.raises(RuntimeError, match="stop"):
            svc.run_candidate(payload, 50)
    finally:
        new_engine.set_masters_bytes(None)       # run_candidate set it; don't leak
    assert seen["doc"] == doc


def test_api_payload_call_site_passes_the_table():
    import inspect
    m = _api()
    src = inspect.getsource(m)
    assert "item_master=book_store.load_item_master()" in src
```

- [ ] **Step 2: Run** → FAIL (`TypeError: build_payload() got an unexpected keyword argument 'item_master'`).

- [ ] **Step 3: Implement** in `engine/optimize_service.py`:

`build_payload` signature gains `item_master=None`; docstring gains "``item_master`` (the app-owned routing table) is carried verbatim so the worker plans from the routings the screen shows." Add to the returned dict:

```python
        # The app-owned Item Process Master (routings). Read with .get by
        # parse_payload / run_candidate so a job built before this key existed
        # still plans, from the workbook's own routing sheet.
        "item_master": item_master,
```

In `parse_payload` replace the masters block:

```python
    raw = payload.get("masters_xlsx_b64")
    doc = payload.get("item_master")
    if raw:
        from engine import item_master as _im
        _, masters = load_all(io.BytesIO(base64.b64decode(raw)),
                              routing_rows=_im.routing_rows(doc) if doc else None)
    else:
        masters = Masters()
```

In `run_candidate`, inside the `if ... == "new":` block, after `set_masters_bytes(...)`:

```python
        new_engine.set_item_master(payload.get("item_master"))
```

In `api/main.py`, the `build_payload(` call (~1870): add the argument `item_master=book_store.load_item_master(),` after `seed_ranks=seed_ranks`. Keep it on one line exactly as written, the test greps for it.

- [ ] **Step 4: Run** `python3.12 -m pytest tests/test_item_master_wiring.py tests/test_optimize_cloud.py tests/test_optimize_service.py tests/test_optimize_shard.py tests/test_machine_downtime_api.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/optimize_service.py api/main.py tests/test_item_master_wiring.py
git commit -m "feat(item-master): optimize payload carries the table; old jobs fall back to the workbook"
```

---

### Task 8: endpoints

**Files:**
- Modify: `api/main.py` (new section after the operators endpoints, ~line 3495; request models next to `DraftLine` ~645)
- Test: `tests/test_item_master_api.py` (create)

**Interfaces:**
- Consumes: Tasks 3-5, `planning_time.CNC_VMC_PLANNING_FACTOR`, `_machine_options(masters)`.
- Produces:
  - `GET /item-master` (any role) → `{"items": [{"code", "description", "version", "steps": [{"name","cycle","allotted","suggested","kind","machining","machines"}], "open_orders": [str], "needs_machine": bool}], "machines": [...as _machine_options], "planning_factor": 1.3, "seeded": bool}`
  - `POST /item-master` (admin) body `{code, description, steps}` → `{"item": {...}}` (409 if exists)
  - `PUT /item-master` (admin) body `{code, description, steps, version}` → `{"item": {...}}` (404 unknown, 409 stale, 400 invalid / punch safety)
  - `POST /item-master/delete` (admin) body `{code}` → `{"deleted": code}` (400 in use, 404 unknown)

- [ ] **Step 1: Failing tests**

```python
# tests/test_item_master_api.py
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store, item_master as im
from engine.models import Actual, Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


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


def _get_item(c, code):
    return next(i for i in c.get("/item-master").json()["items"] if i["code"] == code)


def test_get_lists_items_with_kinds_for_both_roles():
    m, admin, user = _setup()
    for c in (admin, user):
        r = c.get("/item-master")
        assert r.status_code == 200
        data = r.json()
        assert [i["code"] for i in data["items"]] == [ITEM_A, ITEM_B]
        a = data["items"][0]
        assert a["open_orders"] == ["SO1"]
        assert a["steps"][1]["kind"] == "machine" and a["steps"][1]["machining"] is True
        assert data["planning_factor"] == pytest.approx(1.3)
        assert any(mm["id"] == "CNC1" for mm in data["machines"])


def test_user_role_cannot_write():
    m, admin, user = _setup()
    a = _get_item(admin, ITEM_A)
    body = {"code": ITEM_A, "description": "X", "steps": a["steps"], "version": a["version"]}
    assert user.put("/item-master", json=body).status_code == 403
    assert user.post("/item-master", json={**body, "code": "NEW1"}).status_code == 403
    assert user.post("/item-master/delete", json={"code": ITEM_B}).status_code == 403


def test_save_changes_the_plan_input():
    m, admin, _ = _setup()
    a = _get_item(admin, ITEM_A)
    steps = [dict(s) for s in a["steps"]]
    steps[0]["cycle"] = 30
    r = admin.put("/item-master", json={"code": ITEM_A, "description": a["description"],
                                        "steps": steps, "version": a["version"]})
    assert r.status_code == 200, r.text
    assert r.json()["item"]["version"] == a["version"] + 1
    assert m._current_masters().routings[ITEM_A].processes[0].cycle_time == 30


def test_resaving_unchanged_item_keeps_digest():
    m, admin, _ = _setup()
    d0 = im.digest(book_store.load_item_master())
    a = _get_item(admin, ITEM_A)
    r = admin.put("/item-master", json={"code": ITEM_A, "description": a["description"],
                                        "steps": a["steps"], "version": a["version"]})
    assert r.status_code == 200, r.text
    assert im.digest(book_store.load_item_master()) == d0


def test_stale_version_is_refused():
    m, admin, _ = _setup()
    a = _get_item(admin, ITEM_A)
    body = {"code": ITEM_A, "description": "one", "steps": a["steps"], "version": a["version"]}
    assert admin.put("/item-master", json=body).status_code == 200
    r = admin.put("/item-master", json={**body, "description": "two"})
    assert r.status_code == 409
    assert "Someone else saved" in r.json()["detail"]
    assert _get_item(admin, ITEM_A)["description"] == "one"


def test_invalid_item_is_refused_with_every_reason():
    m, admin, _ = _setup()
    r = admin.post("/item-master", json={"code": "NEW1", "description": "",
                                         "steps": [{"name": "", "cycle": 1, "allotted": "MD1", "suggested": ""}]})
    assert r.status_code == 400
    assert "Step 1 needs a name." in r.json()["detail"]


def test_create_then_duplicate_code_conflicts():
    m, admin, _ = _setup()
    body = {"code": "NEW1", "description": "N",
            "steps": [{"name": "CNC", "cycle": 6, "allotted": "CNC1", "suggested": ""}]}
    assert admin.post("/item-master", json=body).status_code == 200
    assert admin.post("/item-master", json=body).status_code == 409
    codes = [i["code"] for i in admin.get("/item-master").json()["items"]]
    assert codes[-1] == "NEW1"


def test_renaming_a_punched_step_is_refused():
    m, admin, _ = _setup()
    book_store.add_actual(Actual(so_no="SO1", item_code=ITEM_A, process="BANDSAW",
                                 qty_produced=4, qty_rejected=0, operator="X"))
    a = _get_item(admin, ITEM_A)
    steps = [dict(s) for s in a["steps"]]
    steps[0]["name"] = "BAND SAW"
    r = admin.put("/item-master", json={"code": ITEM_A, "description": a["description"],
                                        "steps": steps, "version": a["version"]})
    assert r.status_code == 400
    assert "BANDSAW has 4 punched on SO1" in r.json()["detail"]


def test_delete_refused_while_in_use_and_allowed_when_not():
    m, admin, _ = _setup()
    r = admin.post("/item-master/delete", json={"code": ITEM_A})
    assert r.status_code == 400 and "SO1" in r.json()["detail"]
    assert admin.post("/item-master/delete", json={"code": ITEM_B}).status_code == 200
    assert admin.post("/item-master/delete", json={"code": ITEM_B}).status_code == 404


def test_dotted_item_codes_work():
    m, admin, _ = _setup()
    body = {"code": "61243661-01..", "description": "dots",
            "steps": [{"name": "CNC", "cycle": 6, "allotted": "CNC1", "suggested": ""}]}
    assert admin.post("/item-master", json=body).status_code == 200
    assert admin.post("/item-master/delete", json={"code": "61243661-01.."}).status_code == 200
```

Confirm the punch-recording function's name with `grep -n "^def .*actual" engine/book_store.py` and use it in place of `book_store.add_actual` if it differs.

- [ ] **Step 2: Run** `python3.12 -m pytest tests/test_item_master_api.py -q` → FAIL (404s).

- [ ] **Step 3: Implement.** Request models (next to `DraftLine`):

```python
class ItemStep(BaseModel):
    name: str = ""
    cycle: Optional[Union[int, float, str]] = None   # str only so a seeded text cell
    allotted: Optional[str] = None                    # round-trips and validate_item
    suggested: Optional[str] = None                   # refuses it with a clear message


class ItemSaveRequest(BaseModel):
    code: str
    description: str = ""
    steps: list[ItemStep] = []
    version: Optional[int] = None


class ItemCodeRequest(BaseModel):
    code: str
```

(Add `Union` to the `typing` import if absent.)

Endpoints:

```python
# --------------------------------------------------------------------------- #
# Item Process Master (2026-10-05): routings edited in the app, not the Excel.
# --------------------------------------------------------------------------- #
def _item_view(code, it, orders, drafts):
    steps = [{**s, **item_master.step_info(s)} for s in it.get("steps", [])]
    return {"code": code, "description": it.get("description") or "",
            "version": it.get("version", 1), "steps": steps,
            "open_orders": item_master.usage(code, orders, drafts),
            "needs_machine": any(s["kind"] == "machine" and not s["machines"] for s in steps)}


@app.get("/item-master")
def get_item_master():
    masters = _current_masters()          # seeds the table once
    doc = book_store.load_item_master() or {"items": {}}
    orders = list(book_store.load_active_orders().values())
    drafts = book_store.load_new_order_drafts()
    return {"items": [_item_view(c, it, orders, drafts) for c, it in doc["items"].items()],
            "machines": _machine_options(masters),
            "planning_factor": planning_time.CNC_VMC_PLANNING_FACTOR,
            "seeded": bool(doc["items"])}


def _keep_blank_as_stored(new_steps, old_steps):
    """A field the client sends back as "" stays None when it was stored as None,
    so re-saving an unedited seeded item changes nothing (not even the digest)."""
    out = []
    for i, s in enumerate(new_steps):
        s = dict(s)
        old = old_steps[i] if i < len(old_steps) else None
        for f in ("allotted", "suggested"):
            if (s.get(f) or "") == "" and old is not None and old.get(f) is None:
                s[f] = None
        if old is not None and isinstance(old.get("cycle"), int) \
                and isinstance(s.get("cycle"), float) and s["cycle"] == old["cycle"]:
            s["cycle"] = old["cycle"]
        out.append(s)
    return out


def _save_item(req: ItemSaveRequest, request: Request, create: bool):
    require_admin(request)
    masters = _current_masters()
    doc = book_store.load_item_master() or {"items": {}}
    code = req.code.strip()
    old = doc["items"].get(code)
    if not create and old is None:
        raise HTTPException(status_code=404, detail=f"No item with code {code}.")
    item = {"description": req.description.strip(),
            "steps": [s.model_dump() for s in req.steps]}
    errs = item_master.validate_item(code, item, set(masters.machines), old)
    if old is not None and not errs:
        errs = item_master.punch_safety_errors(
            code, old["steps"], item["steps"],
            book_store.load_active_orders().values(), book_store.load_actuals())
    if errs:
        raise HTTPException(status_code=400, detail=" ".join(errs))
    if old is not None:
        item["steps"] = _keep_blank_as_stored(item["steps"], old["steps"])
    try:
        new_doc = item_master.apply_save(
            doc, code, item, req.version, _ist_now().isoformat(timespec="seconds"),
            getattr(request.state, "user", "admin"), create)
    except item_master.VersionConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    if "seed_digest" not in new_doc and "seed_digest" in doc:
        new_doc["seed_digest"] = doc["seed_digest"]
    book_store.save_item_master(new_doc)
    return {"item": _item_view(code, new_doc["items"][code],
                               list(book_store.load_active_orders().values()),
                               book_store.load_new_order_drafts())}


@app.post("/item-master")
def create_item(req: ItemSaveRequest, request: Request):
    """Create an item (admin). 409 if the code exists. Does not start an optimization."""
    return _save_item(req, request, create=True)


@app.put("/item-master")
def update_item(req: ItemSaveRequest, request: Request):
    """Save an item's description and steps (admin). 409 when someone saved it
    since the caller loaded it; 400 with every reason when it is invalid or would
    orphan recorded production on an open order."""
    return _save_item(req, request, create=False)


@app.post("/item-master/delete")
def delete_item(req: ItemCodeRequest, request: Request):
    """Delete an item (admin). Refused while an open order or an Add New Orders
    draft line uses it."""
    require_admin(request)
    _current_masters()
    doc = book_store.load_item_master() or {"items": {}}
    code = req.code.strip()
    if code not in doc["items"]:
        raise HTTPException(status_code=404, detail=f"No item with code {code}.")
    used = item_master.usage(code, book_store.load_active_orders().values(),
                             book_store.load_new_order_drafts())
    if used:
        raise HTTPException(status_code=400,
                            detail=f"{code} is still used by {', '.join(used)}. "
                                   f"Complete or remove those first.")
    del doc["items"][code]
    book_store.save_item_master(doc)
    return {"deleted": code}
```

Note `apply_save` deep-copies the doc, so `seed_digest` is already carried; the explicit copy-over line is harmless belt-and-braces. Delete it if a reviewer prefers.

- [ ] **Step 4: Run** `python3.12 -m pytest tests/test_item_master_api.py tests/test_role_parity.py -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_item_master_api.py
git commit -m "feat(item-master): admin-gated endpoints with validation, punch safety and version check"
```

---

### Task 9: the tab

**Files:**
- Modify: `web/index.html` (nav ~line 34, new `<section>` before `view-settings` ~line 194)
- Modify: `web/app.js` (`VIEWS` line 51, `renderView` ~91, new section of functions near the operator picker ~2700; `uploadExcel` status for `routings_note`)
- Modify: `web/style.css` (append)
- Test: `tests/test_item_master_ui.py` (create)

**Interfaces:**
- Consumes: Task 8 endpoints; existing `escapeHtml`, `setStatus`, `runPlan`, `currentRole`, `$`.

- [ ] **Step 1: Failing test**

```python
# tests/test_item_master_ui.py
"""The Item Process Master tab exists for both roles; writes are admin-gated."""
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "web"


def test_tab_is_visible_to_both_roles():
    html = (WEB / "index.html").read_text()
    line = next(l for l in html.splitlines() if 'data-view="itemmaster"' in l)
    assert "admin-only" not in line
    assert 'id="view-itemmaster"' in html


def test_view_is_routed_and_writes_are_role_checked():
    js = (WEB / "app.js").read_text()
    assert '"itemmaster"' in js.split("const VIEWS")[1].split("\n")[0]
    assert 'v === "itemmaster"' in js
    body = js.split("function renderItemMaster")[1]
    assert 'currentRole === "admin"' in body


def test_no_em_dashes_in_tab_copy():
    js = (WEB / "app.js").read_text()
    section = js.split("// ===== Item Process Master =====")[1].split("// ===== end Item Process Master =====")[0]
    html = (WEB / "index.html").read_text()
    tab = html.split('id="view-itemmaster"')[1].split("</section>")[0]
    for text in (section, tab):
        assert "—" not in text
```

- [ ] **Step 2: Run** `python3.12 -m pytest tests/test_item_master_ui.py -q` → FAIL.

- [ ] **Step 3: Markup.** In `web/index.html`, after the Daily Entry nav link:

```html
      <a class="nav-item" data-view="itemmaster" href="#itemmaster">Item Process Master</a>
```

Before `<section class="view" id="view-settings">`:

```html
  <section class="view" id="view-itemmaster">
    <div class="card">
      <div class="im-toolbar">
        <input type="search" id="im-search" placeholder="Search item code or description" aria-label="Search items">
        <label class="im-filter"><input type="checkbox" id="im-needs"> Only items that need a machine</label>
        <button type="button" class="btn admin-only" id="im-new">+ New item</button>
      </div>
      <div class="im-layout">
        <ul class="im-list" id="im-list" aria-label="Items"></ul>
        <div class="im-detail" id="im-detail"><p class="empty">Pick an item on the left.</p></div>
      </div>
    </div>
  </section>
```

- [ ] **Step 4: JS.** In `web/app.js`: add `"itemmaster"` to `VIEWS` (before `"settings"`); in `renderView` add `else if (v === "itemmaster") renderItemMaster();`. Add this block after the operator-picker functions:

```javascript
// ===== Item Process Master =====
// Routings live in the app (2026-10-05), not the Excel. The server decides what kind
// each step is (it runs the planner's own classifier and returns `kind`), so this
// screen never re-derives it. Edits are made on a local copy of ONE item and sent
// with a single Save, which is one re-plan; Cancel throws the copy away.
let imData = null;          // last GET /item-master
let imSelected = null;      // item code shown on the right
let imDraft = null;         // {isNew, code, description, version, steps[]} while editing
let imLoading = null;

async function loadItemMaster() {
  if (imLoading) return imLoading;
  imLoading = (async () => {
    try {
      const res = await fetch("/item-master");
      if (!res.ok) { setStatus("Could not load the Item Process Master.", true); return; }
      imData = await res.json();
    } finally { imLoading = null; }
  })();
  return imLoading;
}

function imMachineName(id) {
  const m = (imData && imData.machines || []).find((x) => x.id === id);
  return m ? m.name : id;
}

function imTokens(raw) {
  const out = [];
  String(raw || "").split(/[/,]/).forEach((tok) => {
    const id = tok.replace(/\s+/g, "").toUpperCase();
    if (!id || id === "OS" || out.includes(id)) return;
    out.push(id);
  });
  return out;
}

function imKnown(id) {
  return (imData && imData.machines || []).some((m) => m.id === id && !m.provisional);
}

async function renderItemMaster() {
  if (!imData) await loadItemMaster();
  if (!imData) return;
  const isAdmin = currentRole === "admin";
  const q = ($("im-search").value || "").trim().toLowerCase();
  const onlyNeeds = $("im-needs").checked;
  const items = imData.items.filter((it) =>
    (!q || it.code.toLowerCase().includes(q) || (it.description || "").toLowerCase().includes(q))
    && (!onlyNeeds || it.needs_machine));
  $("im-list").innerHTML = items.length ? items.map((it) =>
    `<li><button type="button" class="im-item${it.code === imSelected ? " active" : ""}" data-code="${escapeHtml(it.code)}">`
    + `<span class="im-code">${escapeHtml(it.code)}</span>`
    + `<span class="im-meta">${it.steps.length} steps${it.needs_machine ? ' <span class="im-warn" title="A step has no machine">needs a machine</span>' : ""}</span>`
    + `<span class="im-desc">${escapeHtml(it.description || "")}</span></button></li>`).join("")
    : `<li class="empty">No items match.</li>`;
  $("im-new").style.display = isAdmin ? "" : "none";
  renderItemDetail();
}

function imStepRowView(s, factor) {
  const kindLabel = { machine: "Machine", outsourced: "Outsourced", dispatch: "Dispatch" }[s.kind];
  let cycle = "-";
  if (s.kind === "outsourced" && typeof s.cycle === "number") cycle = `${+(s.cycle / 60).toFixed(2)} h`;
  else if (s.kind === "machine" && s.cycle !== null && s.cycle !== "") cycle = `${escapeHtml(String(s.cycle))} min`;
  const chips = (raw) => imTokens(raw).map((id) => imKnown(id)
    ? `<span class="mach-chip">${escapeHtml(imMachineName(id))}</span>`
    : `<span class="mach-chip unknown" title="Not in your Machine master">⚠ ${escapeHtml(id)}</span>`).join("") || "";
  const planNote = s.machining && typeof s.cycle === "number"
    ? `<div class="im-note">Planned at ${+(s.cycle * factor).toFixed(2)} min (CNC/VMC +${Math.round((factor - 1) * 100)}%)</div>` : "";
  return `<td>${escapeHtml(kindLabel)}</td><td>${escapeHtml(s.name)}${planNote}</td><td>${cycle}</td>`
    + `<td>${s.kind === "machine" ? chips(s.allotted) : (s.kind === "outsourced" ? "Outsourced" : "Milestone")}</td>`
    + `<td>${s.kind === "machine" ? chips(s.suggested) : ""}</td>`;
}

function renderItemDetail() {
  const box = $("im-detail");
  if (imDraft) { renderItemEditor(); return; }
  const it = imData.items.find((x) => x.code === imSelected);
  if (!it) { box.innerHTML = `<p class="empty">Pick an item on the left.</p>`; return; }
  const isAdmin = currentRole === "admin";
  const used = it.open_orders.length ? `Used by ${it.open_orders.length} open order(s): ${escapeHtml(it.open_orders.join(", "))}` : "Not used by any open order.";
  box.innerHTML = `<div class="im-head"><div><h3>${escapeHtml(it.code)}</h3><div class="im-sub">${escapeHtml(it.description || "")}</div><div class="im-sub">${used}</div></div>`
    + (isAdmin ? `<div class="im-actions"><button type="button" class="btn" id="im-edit">Edit</button>`
      + `<button type="button" class="btn secondary" id="im-delete">Delete item</button></div>` : "")
    + `</div><table class="im-steps"><thead><tr><th>#</th><th>Type</th><th>Process</th><th>Cycle</th><th>Allotted</th><th>Suggested</th></tr></thead><tbody>`
    + it.steps.map((s, i) => `<tr><td>${i + 1}</td>${imStepRowView(s, imData.planning_factor)}</tr>`).join("")
    + `</tbody></table>`;
}

function imBlankStep() { return { name: "", cycle: null, allotted: "", suggested: "", kind: "machine" }; }

function imStartEdit(it, isNew) {
  imDraft = { isNew, code: it.code, description: it.description || "", version: it.version,
              steps: it.steps.map((s) => ({ name: s.name, cycle: s.cycle, allotted: s.allotted,
                                            suggested: s.suggested, kind: s.kind })),
              dirty: isNew };
  renderItemDetail();
}

function imMachinePicker(i, field, raw) {
  const picked = imTokens(raw);
  const chips = picked.map((id) =>
    `<span class="mach-chip${imKnown(id) ? "" : " unknown"}">${imKnown(id) ? "" : "⚠ "}${escapeHtml(imMachineName(id))}`
    + `<button type="button" class="mach-x im-mx" data-i="${i}" data-f="${field}" data-id="${escapeHtml(id)}" title="Remove">✕</button></span>`).join("");
  const groups = new Map();
  (imData.machines || []).forEach((m) => {
    if (m.provisional || picked.includes(m.id)) return;
    const label = m.type || "Other";
    if (!groups.has(label)) groups.set(label, []);
    groups.get(label).push(m);
  });
  const opts = Array.from(groups).map(([label, rows]) => `<optgroup label="${escapeHtml(label)}">`
    + rows.map((m) => `<option value="${escapeHtml(m.id)}">${escapeHtml(m.name)}</option>`).join("") + `</optgroup>`).join("");
  return chips + `<select class="im-madd" data-i="${i}" data-f="${field}"><option value="">+ add</option>${opts}</select>`;
}

function renderItemEditor() {
  const d = imDraft;
  const rows = d.steps.map((s, i) => {
    const cycleInput = s.kind === "dispatch" ? "-"
      : s.kind === "outsourced"
        ? `<input type="number" min="0" step="0.5" class="im-cyc" data-i="${i}" value="${typeof s.cycle === "number" ? +(s.cycle / 60).toFixed(2) : ""}"> h`
        : `<input type="number" min="0" step="0.01" class="im-cyc" data-i="${i}" value="${s.cycle === null || s.cycle === undefined ? "" : escapeHtml(String(s.cycle))}"> min`;
    return `<tr><td>${i + 1}</td>`
      + `<td><select class="im-kind" data-i="${i}">`
      + ["machine", "outsourced", "dispatch"].map((k) => `<option value="${k}"${s.kind === k ? " selected" : ""}>${{ machine: "Machine", outsourced: "Outsourced", dispatch: "Dispatch" }[k]}</option>`).join("")
      + `</select></td>`
      + `<td><input type="text" class="im-name" data-i="${i}" value="${escapeHtml(s.name)}" placeholder="Process name"></td>`
      + `<td class="im-cycell">${cycleInput}</td>`
      + `<td>${s.kind === "machine" ? imMachinePicker(i, "allotted", s.allotted) : ""}</td>`
      + `<td>${s.kind === "machine" ? imMachinePicker(i, "suggested", s.suggested) : ""}</td>`
      + `<td class="im-rowbtns"><button type="button" class="im-up" data-i="${i}" title="Move up"${i === 0 ? " disabled" : ""}>↑</button>`
      + `<button type="button" class="im-down" data-i="${i}" title="Move down"${i === d.steps.length - 1 ? " disabled" : ""}>↓</button>`
      + `<button type="button" class="im-ins" data-i="${i}" title="Insert a step below">+</button>`
      + `<button type="button" class="im-del" data-i="${i}" title="Remove this step">✕</button></td></tr>`;
  }).join("");
  $("im-detail").innerHTML = `<div class="im-head"><div><h3>${d.isNew ? "New item " : ""}${escapeHtml(d.code)}</h3>`
    + `<label class="im-sub">Description <input type="text" id="im-desc" value="${escapeHtml(d.description)}"></label></div>`
    + `<div class="im-actions"><button type="button" class="btn" id="im-save">Save</button>`
    + `<button type="button" class="btn secondary" id="im-cancel">Cancel</button></div></div>`
    + `<table class="im-steps editing"><thead><tr><th>#</th><th>Type</th><th>Process</th><th>Cycle</th><th>Allotted</th><th>Suggested</th><th></th></tr></thead><tbody>${rows}</tbody></table>`
    + `<button type="button" class="btn secondary" id="im-addstep"${d.steps.length >= 12 ? " disabled" : ""}>+ Add step at the end</button>`
    + `<p class="im-note">Cycle times are the original times. The plan adds ${Math.round((imData.planning_factor - 1) * 100)}% to CNC/VMC steps.</p>`
    + `<p class="im-error" id="im-error" role="alert"></p>`;
}

function imMutate(fn) { fn(imDraft); imDraft.dirty = true; renderItemEditor(); }

function imSetKind(i, kind) {
  imMutate((d) => {
    const s = d.steps[i];
    if (kind === "outsourced") { s.allotted = "OS"; s.suggested = ""; }
    if (kind === "dispatch") { s.allotted = ""; s.suggested = ""; s.cycle = null; if (!/DISPATCH/i.test(s.name)) s.name = "DISPATCH"; }
    if (kind === "machine" && s.allotted === "OS") s.allotted = "";
    s.kind = kind;
  });
}

async function imSave() {
  const d = imDraft;
  const body = { code: d.code, description: d.description,
                 steps: d.steps.map((s) => ({ name: s.name, cycle: s.cycle, allotted: s.allotted || "", suggested: s.suggested || "" })) };
  if (!d.isNew) body.version = d.version;
  const res = await fetch("/item-master", { method: d.isNew ? "POST" : "PUT",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!res.ok) {
    let msg = await res.text();
    try { msg = JSON.parse(msg).detail || msg; } catch (e) { /* plain text */ }
    $("im-error").textContent = msg;
    return;
  }
  const saved = (await res.json()).item;
  imDraft = null; imSelected = saved.code; imData = null;
  setStatus(`Saved ${saved.code}. Updating the plan.`);
  await renderItemMaster();
  await runPlan(false);
}

async function imDelete(code) {
  if (!confirm(`Delete item ${code}? This cannot be undone.`)) return;
  const res = await fetch("/item-master/delete", { method: "POST",
    headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code }) });
  if (!res.ok) { let m = await res.text(); try { m = JSON.parse(m).detail; } catch (e) { /* */ } setStatus(m, true); return; }
  imSelected = null; imData = null;
  setStatus(`Deleted ${code}.`);
  await renderItemMaster();
  await runPlan(false);
}

function imNewItem() {
  const code = (prompt("Item code for the new item?") || "").trim();
  if (!code) return;
  if (imData.items.some((x) => x.code === code)) { setStatus(`Item ${code} already exists.`, true); return; }
  const from = (prompt("Copy the steps of another item? Type its code, or leave blank to start empty.") || "").trim();
  const src = from ? imData.items.find((x) => x.code === from) : null;
  if (from && !src) { setStatus(`No item ${from} to copy from. Starting empty.`, true); }
  imSelected = code;
  imStartEdit({ code, description: src ? src.description : "", version: null,
                steps: src ? src.steps : [imBlankStep()] }, true);
}

function imLeaveOk() { return !imDraft || !imDraft.dirty || confirm("You have unsaved changes to this item. Throw them away?"); }

function wireItemMaster() {
  $("im-search").addEventListener("input", () => renderItemMaster());
  $("im-needs").addEventListener("change", () => renderItemMaster());
  $("im-new").addEventListener("click", () => { if (imLeaveOk()) { imDraft = null; imNewItem(); } });
  $("im-list").addEventListener("click", (e) => {
    const b = e.target.closest(".im-item"); if (!b) return;
    if (!imLeaveOk()) return;
    imDraft = null; imSelected = b.dataset.code; renderItemMaster();
  });
  const box = $("im-detail");
  box.addEventListener("click", (e) => {
    const t = e.target; const i = +t.dataset.i;
    if (t.id === "im-edit") imStartEdit(imData.items.find((x) => x.code === imSelected), false);
    else if (t.id === "im-delete") imDelete(imSelected);
    else if (t.id === "im-cancel") { if (imLeaveOk()) { imDraft = null; renderItemMaster(); } }
    else if (t.id === "im-save") imSave();
    else if (t.id === "im-addstep") imMutate((d) => d.steps.push(imBlankStep()));
    else if (t.classList.contains("im-up")) imMutate((d) => d.steps.splice(i - 1, 0, d.steps.splice(i, 1)[0]));
    else if (t.classList.contains("im-down")) imMutate((d) => d.steps.splice(i + 1, 0, d.steps.splice(i, 1)[0]));
    else if (t.classList.contains("im-ins")) imMutate((d) => { if (d.steps.length < 12) d.steps.splice(i + 1, 0, imBlankStep()); });
    else if (t.classList.contains("im-del")) imMutate((d) => d.steps.splice(i, 1));
    else if (t.classList.contains("im-mx")) imMutate((d) => {
      const s = d.steps[i]; s[t.dataset.f] = imTokens(s[t.dataset.f]).filter((x) => x !== t.dataset.id).join("/");
    });
  });
  box.addEventListener("change", (e) => {
    const t = e.target; const i = +t.dataset.i;
    if (t.classList.contains("im-kind")) imSetKind(i, t.value);
    else if (t.classList.contains("im-madd") && t.value) imMutate((d) => {
      const s = d.steps[i]; const ids = imTokens(s[t.dataset.f]); ids.push(t.value); s[t.dataset.f] = ids.join("/");
    });
  });
  box.addEventListener("input", (e) => {
    const t = e.target; if (!imDraft) return; const i = +t.dataset.i;
    if (t.id === "im-desc") imDraft.description = t.value;
    else if (t.classList.contains("im-name")) imDraft.steps[i].name = t.value;
    else if (t.classList.contains("im-cyc")) {
      const v = t.value === "" ? null : Number(t.value);
      imDraft.steps[i].cycle = v === null ? null : (imDraft.steps[i].kind === "outsourced" ? v * 60 : v);
    } else return;
    imDraft.dirty = true;     // typing never re-renders, so focus stays in the box
  });
  window.addEventListener("beforeunload", (e) => { if (imDraft && imDraft.dirty) { e.preventDefault(); e.returnValue = ""; } });
}
// ===== end Item Process Master =====
```

Call `wireItemMaster();` from the boot routine where the other panels are wired (find it with `grep -n "loadOperators()" web/app.js`, add the call next to the boot-time call). In `showView`, before switching away from `itemmaster`, guard: at the top of `showView` add `if (activeView === "itemmaster" && v !== "itemmaster" && !imLeaveOk()) return;`. In the upload success handler (`grep -n "masters_updated\|/upload" web/app.js`), append `data.routings_note` to the status message when present.

- [ ] **Step 5: CSS** (append to `web/style.css`; reuse existing color tokens, check names with `grep -n "^\s*--" web/style.css | head -30` and substitute the token names used there):

```css
/* Item Process Master */
.im-toolbar { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 12px; }
.im-toolbar input[type=search] { flex: 1 1 260px; min-width: 0; }
.im-layout { display: grid; grid-template-columns: minmax(220px, 300px) 1fr; gap: 16px; }
.im-list { list-style: none; margin: 0; padding: 0; max-height: 70vh; overflow-y: auto; border-right: 1px solid var(--border); }
.im-item { display: grid; grid-template-columns: 1fr auto; gap: 2px 8px; width: 100%; text-align: left; padding: 8px 10px; background: none; border: 0; border-bottom: 1px solid var(--border); cursor: pointer; }
.im-item.active, .im-item:hover { background: var(--row-hover); }
.im-code { font-weight: 600; }
.im-meta { font-size: 12px; color: var(--muted); }
.im-desc { grid-column: 1 / -1; font-size: 12px; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.im-warn { color: var(--danger); font-weight: 600; }
.im-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; flex-wrap: wrap; margin-bottom: 8px; }
.im-head h3 { margin: 0; }
.im-sub { color: var(--muted); font-size: 13px; }
.im-actions { display: flex; gap: 8px; }
.im-steps { width: 100%; border-collapse: collapse; }
.im-steps th, .im-steps td { padding: 6px 8px; border-bottom: 1px solid var(--border); vertical-align: top; text-align: left; }
.im-steps input[type=number] { width: 80px; }
.im-steps input.im-name { width: 100%; min-width: 140px; }
.im-rowbtns button { padding: 2px 6px; }
.im-note { font-size: 12px; color: var(--muted); }
.im-error { color: var(--danger); white-space: pre-wrap; }
@media (max-width: 760px) {
  .im-layout { grid-template-columns: 1fr; }
  .im-list { max-height: 40vh; border-right: 0; }
  .im-steps { display: block; overflow-x: auto; }
}
```

- [ ] **Step 6: Run** `python3.12 -m pytest tests/test_item_master_ui.py tests/test_role_parity.py -q` → PASS.

- [ ] **Step 7: Browser check** on a throwaway local instance (never the live site): in the worktree run `STORE_DIR=$(mktemp -d) DEFAULT_SCHEDULER=new ADMIN_PASSWORD=1930rail USER_PASSWORD=anvitech12345678 python3.12 -m uvicorn api.main:app --port 8765`, log in as admin, upload `~/Desktop/Anvitech Rebuilt/Test9.xlsx`, open the tab. Check: search, the "needs a machine" filter, Edit, change a cycle, insert, reorder, remove, switch a step to Outsourced (hours) and Dispatch, add and remove machine chips, Save (the plan refreshes), Cancel, + New item blank and copied, Delete of an unused item, the refusal for a used item, the 409 (save from two tabs), a narrow window (≤ 760 px), then log in as the user role and confirm no edit controls. Read the console for errors. Native `confirm()`/`prompt()` block browser automation; drive those steps by hand or by HTTP.

- [ ] **Step 8: Commit**

```bash
git add web/index.html web/app.js web/style.css tests/test_item_master_ui.py
git commit -m "feat(item-master): Item Process Master tab: searchable list, step editor, machine picker"
```

---

### Task 10: verification, mutation testing, docs

**Files:**
- Create: `docs/superpowers/specs/2026-10-05-item-process-master-verification.md` (results + harness source)
- Modify: `CLAUDE.md` (new top bullet in the CURRENT STATE banner)

- [ ] **Step 1: Byte-identical switch harness.** Save as `$SCRATCH/verify_switch.py` (scratchpad, not committed; paste its source into the verification doc):

```python
"""Plan each real book on the CURRENT checkout and print one hash per run.
Run it in the feature worktree AND in a pristine worktree of origin/main
(`git worktree add ../anvitech-base origin/main`); every line must match."""
import hashlib, io, os, sys, tempfile
from datetime import date

BOOKS = sys.argv[1:]

def run(book, flexible, wip):
    os.environ["STORE_DIR"] = tempfile.mkdtemp()
    os.environ["DEFAULT_SCHEDULER"] = "new"
    import importlib
    from engine import book_store, loaders, new_engine
    new_engine._MASTERS_CACHE.clear()
    import api.main as m; importlib.reload(m)
    raw = open(book, "rb").read()
    book_store.save_masters_bytes(raw)
    so_lines, _ = loaders.load_all(io.BytesIO(raw))
    from engine.models import Order, Actual
    orders = [Order(s.so_no, s.item_code, s.item_code, s.qty, s.delivery_date) for s in so_lines]
    book_store.add_orders(orders)
    if wip:
        masters = m._current_masters()
        for o in orders[:30]:
            first = masters.routings[o.item_code].processes[0].name
            book_store.add_actual(Actual(so_no=o.so_no, item_code=o.item_code, process=first,
                                         qty_produced=max(1, o.qty // 2), qty_rejected=0,
                                         operator=masters.operators[0].name))
    cfg = m._load_plan_config()
    cfg.plan_start_date = date(2026, 10, 6)
    cfg.apply_operator_logic = True
    cfg.overlap_percent = 80
    cfg.flexible_machines = flexible
    m._plan(cfg)
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    h = hashlib.sha256(repr(sorted((e.batch_id, e.process_seq, e.machine, e.operator,
                                    e.start.isoformat(), e.end.isoformat(), e.qty)
                                   for e in sched)).encode()).hexdigest()
    print(os.path.basename(book), flexible, wip, len(sched), h)

for b in BOOKS:
    for flexible in (False, True):
        for wip in (False, True):
            run(b, flexible, wip)
```

Adjust field names (`Order(...)` positional args, `SOLine.qty`/`delivery_date`, `Actual(...)` keywords, `add_actual`, `Config` attribute names) to what `engine/models.py`, `engine/book_store.py` and `engine/config.py` actually define. Run in both worktrees:
`python3.12 $SCRATCH/verify_switch.py "../Anvitech Rebuilt/Test5.xlsx" "../Anvitech Rebuilt/Test8.xlsx" "../Anvitech Rebuilt/Test9.xlsx" > out.txt` and `diff` the two outputs. Expected: identical (12 lines). If any line differs, STOP and investigate before continuing (likely a book with a blank step in the middle of a routing, which the classic loader used to read past; report it to the owner rather than papering over it).

- [ ] **Step 2: Live-store copy.** Ask the owner for `MONGODB_URI` (memory: reading-live-anvitech-data). With it, copy every key to a local `STORE_DIR` (read-only on the live side; nothing written back), then plan once on `origin/main` and once on the feature branch from that copy and compare hashes as in Step 1, plus one `/new-orders/quote` with two lines on each. If the owner does not provide the URI, record "live copy not run" in the verification doc.

- [ ] **Step 3: Every reader moves.** On a Test9 local instance with the table seeded, change one CNC step's cycle time through `PUT /item-master` and show each of these changes, with the workbook untouched: `/run` schedule, `/gantt`, `/delay-report.xlsx`, the shift-wise download, the production analysis cycle time, `/new-orders/quote`, and `optimize_service.parse_payload(build_payload(...))`. Record each before/after in the verification doc.

- [ ] **Step 4: Mutation testing.** Revert each part individually, run `python3.12 -B -m pytest tests/test_routing_rows.py tests/test_item_master.py tests/test_item_master_wiring.py tests/test_item_master_api.py -q`, restore, and record which tests failed:
  1. `_current_masters` passes `routing_rows=None` always.
  2. `_new_masters` passes `routing_rows=None` always.
  3. `parse_payload` ignores `item_master`.
  4. `run_candidate` does not call `set_item_master`.
  5. `_plan_fingerprint` drops `item_master`.
  6. `_inputs_signature` never appends the table.
  7. `punch_safety_errors` returns `[]`.
  8. `apply_save` skips the version comparison.
  9. `_keep_blank_as_stored` returns `new_steps` unchanged.
  Use `-B` (stale bytecode masked a mutation on 2026-09-22). Any mutation that fails no test is reported by name in the verification doc.

- [ ] **Step 5: Full suite.** `python3.12 -m pytest -q`. Record the pass/skip counts.

- [ ] **Step 6: Docs.** Write the verification doc (harness source, both hash tables, reader table, mutation table, what was NOT run). Add a CLAUDE.md banner bullet at the top of CURRENT STATE: what moved (routings now app-owned, `anvitech:item_process_master`, seeded once), the one rule per loader, the four wiring points, the inputs-signature "only once it differs from the seed" rule, punch safety, the measured results, and the rule: **"Routings come from the Item Process Master. Never read the workbook's routing sheet for planning; any new masters builder must pass `routing_rows=`."** Mark the spec's status line "implemented, unpushed".

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-10-05-item-process-master-verification.md docs/superpowers/specs/2026-10-05-item-process-master-tab-design.md CLAUDE.md
git commit -m "docs: Item Process Master verification and CLAUDE.md banner"
```

Do not push. Report to the owner with the numbers and ask whether to deploy.
