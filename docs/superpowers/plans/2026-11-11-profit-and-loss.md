# Profit and Loss Tab Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An admin-only "Profit and Loss" tab showing, for every eligible completed order, its planned and actual machine-time cost on CNC/VMC and inspection steps.

**Architecture:** One pure module `engine/profit_loss.py` (snapshot extraction from a plan, actual minutes from punches, rows with costs). `_plan` writes a planned snapshot for every unstarted order on its compute path (store key `anvitech:pnl_planned`, frozen per order at its first punch). Rates are a new optional field on the app's Machines table. `GET /profit-loss` (admin) assembles rows; `web/` gets one admin-only tab and a rate column on the Machines card.

**Tech Stack:** Python 3.12, FastAPI, pytest, vanilla JS/HTML/CSS in `web/`.

**Spec:** `docs/superpowers/specs/2026-11-11-profit-and-loss-design.md` (read it first; this plan argues from it).

## Global Constraints

- Base branch: **`origin/main` (518b11c or later)**, in a fresh worktree via superpowers:using-git-worktrees. NOT the `optimizer-search-fixes` tree.
- Run tests with `python3.12 -m pytest` (system python3 is 3.14 and breaks openpyxl). `test_production_analysis` needs xlsxwriter; use a `--system-site-packages` venv if it is missing locally.
- **No plan effect.** `_plan` response, `_plan_fingerprint`, `_inputs_signature`, `machines_digest` and `SCHEDULER_FINGERPRINT` must be unchanged. A failure inside the P&L snapshot must never break a plan (catch, log, continue).
- Counted steps: machine kind `MACHINING` or `INSPECTION` per `ppc_engine.loaders.normalize.machine_kind_from_type(<Machines table type>)`. Everything else (manual, OS, off-machine, dispatch) is never counted.
- Planned CNC/VMC run time is the ORIGINAL standard (no `CNC_VMC_PLANNING_FACTOR`); setup included.
- Actual minutes per punch = `shift_minutes` (capped at the shift's working minutes, scaled per section 7b) minus the six `Actual.DOWNTIME_FIELDS`, floor 0. Setup stays in.
- Go-live rates, ₹/hour: `CNC1 CNC4 CNC5 CNC7 = 450`, `CNC3 CNC6 = 650`, `VMC1 = 500`, `VMC2 VMC3 = 650`, `CMM = 850`. Every other machine: no rate.
- A missing rate is "rate not set", never ₹0. A missing punch is "not punched", never 0 hours.
- Admin only: `GET /profit-loss` 403 for the user role; nav link `admin-only`; `showView` redirects a non-admin away from `#profitloss`; machine `rate` absent from `GET /machines` for the user role.
- UI copy: plain operator English, no em dashes. Money shown as `₹12,345` (Indian grouping via `toLocaleString("en-IN")`), hours to one decimal.
- Pushing to `main` deploys to production. Do not push without the owner's go.

## Review Focus

1. **A Machines save that omits `rate` must keep the stored rate.** The type/hours editor PUTs without a rate today; a rate silently wiped by an unrelated edit is the most likely real-world bug. Test in Task 5.
2. **Two punches on one machine in one shift with the default 630 minutes each** must not count 1,260 minutes. Test in Task 3.
3. **An order completed with no counted steps at all** (only manual work) must not appear as a ₹0 row; it goes to `not_shown` with a reason. Test in Task 4.
4. **A rate entered as 0 or blank** means "not set" (rate must be > 0 or empty; 400 otherwise). Test in Task 5.
5. **A completed order un-completed by Rollback** must disappear from the tab on the next load (rows are read live from the completed archive, never cached). Test in Task 6.

---

### Task 1: Planned snapshot from a plan (pure)

**Files:**
- Create: `engine/profit_loss.py`
- Test: `tests/test_profit_loss.py`

**Interfaces:**
- Consumes: `ScheduleEntry` and `Batch` (`engine/models.py`), `masters.routings` (classic loader `Process` with `.name`, `.cycle_time`), `planning_time.planning_cycle_time(proc, config)`, `loaders.normalize_process_name`.
- Produces:
  - `COUNTED_KINDS = {"machining", "inspection"}` (the `MachineKind(...).value` strings).
  - `machine_kinds(machines_doc: dict) -> dict[str, str]`: machine id → kind value, from the Machines table types.
  - `planned_steps(schedule: list, batches: list, masters, config, kinds: dict) -> dict[tuple[str,str], list[dict]]`: `(so_no, item_code)` → `[{"process": str (as named in the routing), "step": str (normalized name), "machine": str, "setup_min": float, "run_min": float}]`, counted steps only.

Algorithm (not determined by the signature):
1. Skip entries whose `machine` kind is not counted (unknown machine id → not counted).
2. Aggregate entries per operation key `(batch_id, process_seq, resumed)`: `O` = sum of `occupancy_min`, `Q` = the first entry's `qty`, machine = the first entry's.
3. `Cp` = `planning_cycle_time(proc, config)`, `Co` = `proc.cycle_time` for that item's routing step named `process_name` (matched normalized). `setup_op = max(0, O - Q*Cp)` when `Cp` is a number, else 0.
4. Lines on the bar: `entry.piece_refs or entry.so_refs`. Each line's pieces: from `batch.line_process_qty[so] = (qty, pq)`, `pq.get(normalize(name), qty) if pq else qty`; if the batch has no `line_process_qty` or only one line, that line's pieces = `Q`. Share = pieces / sum(pieces) (equal shares if the sum is 0).
5. Per line: `setup_min = setup_op * share`; `run_min = pieces * Co` when `Co` is a number, else `(O - setup_op) * share / CNC_VMC_PLANNING_FACTOR` for machining and `(O - setup_op) * share` for inspection.

- [ ] **Step 1: Write the failing tests**

Build entries and batches by hand (no plan run). Machines table types: `CNC4` "CNC lathe", `CMM` "CMM" (use whatever type strings `machine_kind_from_type` maps to MACHINING / INSPECTION / MANUAL; check `ppc_engine/loaders/normalize.py`), `MD1` a manual type.

```python
def test_single_line_cnc_step_unpads_run_and_keeps_setup():
    # item X: CNC FIRST SIDE cycle 10 (original), qty 20, config scheduler="new", setup 90
    # entry occupancy = 90 + 20*13 = 350
    out = planned_steps([entry(CNC4, 350, qty=20)], [batch(lines={"SO1": 20})], masters, cfg, kinds)
    (s,) = out[("SO1", "X")]
    assert s["machine"] == "CNC4" and s["setup_min"] == pytest.approx(90)
    assert s["run_min"] == pytest.approx(200)          # 20 x 10, the 30% is gone

def test_inspection_step_is_counted_and_never_unpadded():
    # CMM cycle 5, qty 20, occupancy 100
    assert planned_steps(...)[("SO1","X")][0]["run_min"] == pytest.approx(100)

def test_manual_step_is_not_counted():
    assert planned_steps([entry(MD1, 60, qty=20)], ...) == {}

def test_clubbed_batch_splits_by_each_lines_pieces():
    # SO1 30 pieces + SO2 10 pieces on one CNC4 bar, occupancy 90 + 40*13
    out = planned_steps(...)
    assert out[("SO1","X")][0]["setup_min"] == pytest.approx(67.5)
    assert out[("SO2","X")][0]["setup_min"] == pytest.approx(22.5)
    assert out[("SO1","X")][0]["run_min"] == pytest.approx(300)
    assert out[("SO2","X")][0]["run_min"] == pytest.approx(100)

def test_one_operation_in_two_blocks_pays_setup_once():
    # two entries, same (batch, seq), occupancies 200 + 150, qty 20 each (op qty)
    assert planned_steps(...)[("SO1","X")][0]["setup_min"] == pytest.approx(90)

def test_piece_refs_limit_the_lines_on_a_bar():
    # entry.piece_refs == ["SO2"]: SO1 gets no share of that bar
    assert ("SO1","X") not in planned_steps(...)
```

- [ ] **Step 2: Run** `python3.12 -m pytest tests/test_profit_loss.py -v`. Expected: FAIL (module missing).
- [ ] **Step 3: Implement** `COUNTED_KINDS`, `machine_kinds`, `planned_steps` in `engine/profit_loss.py` as above. Module docstring: pure, reporting-only, no store, no HTTP, cites the spec.
- [ ] **Step 4: Run** the same command. Expected: PASS.
- [ ] **Step 5: Commit** `feat(profit-loss): planned machine minutes per order from the plan`.

---

### Task 2: Store the planned snapshot, frozen at the first punch

**Files:**
- Modify: `engine/book_store.py` (new key next to `MACHINES_KEY`)
- Modify: `engine/profit_loss.py`
- Modify: `api/main.py` (`_plan`, compute path, just before `_PLAN_CACHE.update(...)`)
- Test: `tests/test_profit_loss.py`, `tests/test_profit_loss_api.py` (new)

**Interfaces:**
- Consumes: Task 1 `planned_steps`, `machine_kinds`.
- Produces:
  - `book_store.PNL_PLANNED_KEY = "anvitech:pnl_planned"`; `load_pnl_planned() -> dict` (`{"so\x1fitem": {"steps": [...], "at": iso}}`, `{}` when absent); `save_pnl_planned(doc: dict) -> None`.
  - `profit_loss.merge_snapshot(stored: dict, fresh: dict, started: set, now_iso: str) -> dict`: returns a new dict; for each key in `fresh` NOT in `started`, sets `{"steps": fresh[key], "at": now_iso}`; leaves started keys and keys absent from `fresh` untouched. Keys are `f"{so}\x1f{item}"` (the existing composite-field convention).
  - `api.main._snapshot_pnl(plan_run, masters, config, actuals)`: computes `fresh`, `started = {a.key for a in actuals}`, merges, saves only if the result differs from what was loaded. Wrapped in `try/except Exception` with a logged warning.

- [ ] **Step 1: Write the failing tests**

```python
def test_merge_overwrites_unstarted_orders():
    out = merge_snapshot({"A\x1fX": {"steps": [old], "at": "t0"}}, {("A","X"): [new]}, set(), "t1")
    assert out["A\x1fX"] == {"steps": [new], "at": "t1"}

def test_merge_never_touches_a_started_order():
    out = merge_snapshot({"A\x1fX": {"steps": [old], "at": "t0"}}, {("A","X"): [new]}, {("A","X")}, "t1")
    assert out["A\x1fX"]["steps"] == [old]

def test_an_order_started_before_any_snapshot_never_gets_one():
    assert "A\x1fX" not in merge_snapshot({}, {("A","X"): [new]}, {("A","X")}, "t1")

def test_merge_keeps_orders_no_longer_planned():   # completed orders leave the plan
    assert "A\x1fX" in merge_snapshot({"A\x1fX": {...}}, {}, set(), "t1")
```

In `tests/test_profit_loss_api.py` (follow `tests/test_role_parity.py`'s TestClient + local-store fixture pattern, sample workbook seeded, production-like config `DEFAULT_SCHEDULER=new`):

```python
def test_plan_writes_a_snapshot_for_unstarted_orders(admin): ...   # /run, then load_pnl_planned() has every planned (so,item) with counted steps
def test_snapshot_freezes_at_first_punch(admin): ...               # snapshot, punch order A, change plan (e.g. machine downtime), /run: A unchanged, others updated
def test_plan_response_is_identical_with_the_snapshot_disabled(admin, monkeypatch): ...  # monkeypatch _snapshot_pnl to a no-op; /run results equal except run_id
def test_a_failing_snapshot_never_breaks_the_plan(admin, monkeypatch): ...  # make planned_steps raise; /run still 200 with a schedule
```

- [ ] **Step 2: Run** `python3.12 -m pytest tests/test_profit_loss.py tests/test_profit_loss_api.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** the store functions, `merge_snapshot`, `_snapshot_pnl`, and call it once on `_plan`'s compute path (never on a cache hit) with `plan_run`, `masters`, `ranked_config`, `actuals`.
- [ ] **Step 4: Run** the same command, then the full suite `python3.12 -m pytest -q`. Expected: all pass (record the count).
- [ ] **Step 5: Commit** `feat(profit-loss): record each unstarted order's planned minutes, frozen at its first punch`.

---

### Task 3: Actual minutes from punches (pure)

**Files:**
- Modify: `engine/profit_loss.py`
- Test: `tests/test_profit_loss.py`

**Interfaces:**
- Consumes: `Actual` (`engine/models.py`; fields `machine`, `entry_date`, `shift`, `shift_minutes`, `qty_produced`, `actual_setup_min`, `process`, `DOWNTIME_FIELDS`), `production_analysis.cycle_time_for(masters, item, process)`, `working: dict` from `operator_coverage.shift_working_minutes(config)` (`{"1st shift": 630, "2nd shift": 570}`).
- Produces: `actual_minutes(actuals: list, masters, kinds: dict, working: dict) -> dict[tuple[str,str], list[dict]]`: `(so, item)` → `[{"step": normalized process, "process": str, "machine": str, "minutes": float}]` for every punch on a counted machine with minutes > 0, plus `{"step", "process", "machine": "", "minutes": None}` for a punch with a blank machine (Task 4 uses it to exclude the order). Punches with `shift_minutes` 0 are dropped (that step reads "not punched").

Algorithm: `h = min(shift_minutes, working.get(shift.strip()))` (no cap when the shift is not in `working`). Group ALL punches with a machine by `(machine, entry_date, shift.strip())`. If the group has a cap and `sum(h) > cap`, each `h_i = cap * w_i / sum(w)` with `w_i = qty_produced * Co + actual_setup_min` (`Co` from `cycle_time_for`, falling back to the punch's `cycle_time_min`, else 0); equal shares if `sum(w) == 0`. Then `minutes = max(0, h_i - sum(DOWNTIME_FIELDS))`. Kind filter is applied AFTER scaling (a manual job on the same machine id cannot exist, but the grouping must not depend on eligibility).

- [ ] **Step 1: Write the failing tests**

```python
def test_minutes_are_shift_minutes_minus_downtime_with_setup_kept():
    a = punch("SO1","X","CNC4", mins=600, setup=90, no_power=30, breakdown=20)
    assert actual_minutes([a], ...)[("SO1","X")][0]["minutes"] == 550

def test_minutes_never_count_past_the_shift():
    assert actual_minutes([punch(mins=700, shift="1st shift")], ...)[...][0]["minutes"] == 630

def test_two_default_punches_on_one_machine_share_the_shift():
    # SO149 made 30 pcs, SO150 made 10, cycle 10, both left at 630, no setup
    out = actual_minutes([punch("SO149", qty=30, mins=630), punch("SO150", qty=10, mins=630)], ...)
    assert out[("SO149","X")][0]["minutes"] == pytest.approx(472.5)
    assert out[("SO150","X")][0]["minutes"] == pytest.approx(157.5)

def test_punches_that_fit_inside_the_shift_are_used_as_typed():
    out = actual_minutes([punch("SO1", mins=300), punch("SO2", mins=200)], ...)
    assert [out[k][0]["minutes"] for k in (("SO1","X"),("SO2","X"))] == [300, 200]

def test_downtime_larger_than_minutes_floors_at_zero(): ...
def test_blank_minutes_punch_is_dropped(): ...
def test_manual_machine_punch_is_not_counted(): ...
def test_blank_machine_punch_is_flagged_with_minutes_none(): ...
```

- [ ] **Step 2: Run** `python3.12 -m pytest tests/test_profit_loss.py -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** `actual_minutes`.
- [ ] **Step 4: Run** the same. Expected: PASS.
- [ ] **Step 5: Commit** `feat(profit-loss): actual machine minutes from punches, shared when a shift is overbooked`.

---

### Task 4: Rows with costs (pure)

**Files:**
- Modify: `engine/profit_loss.py`
- Test: `tests/test_profit_loss.py`

**Interfaces:**
- Consumes: Task 2 snapshot doc, Task 3 output, completed orders (`book_store.load_completed_orders()` values: `Order` with `so_no`, `item_code`, `item_name`, `ordered_qty`, `completed_on`).
- Produces: `build_report(completed: list, snapshot: dict, actual: dict, rates: dict[str, float]) -> dict` with:
  - `rows`: one per completed order that has a snapshot, newest `completed_on` first: `{so_no, item_code, item_name, qty, completed_on (DD-MM-YYYY or ""), planned_hours, planned_cost, actual_hours, actual_cost, diff_cost, diff_pct, not_punched: [process], rates_missing: [machine], steps: [...]}`. Costs are `None` when any machine on that side lacks a rate; hours are always numbers. `diff_cost = actual_cost - planned_cost` and `diff_pct = diff_cost / planned_cost * 100` (None when either cost is None or planned is 0).
  - `steps` per row, in snapshot order then actual-only steps: `{process, planned_machine, planned_hours, planned_cost, actual_machines: [str], actual_hours, actual_cost, status: "ok" | "not punched" | "rate not set"}`. Planned and actual are matched by normalized step name; several entries with the same name are summed.
  - `totals`: sums over rows with both costs present, plus `orders`, `incomplete` (rows with `not_punched` or `rates_missing`).
  - `not_shown`: `[{so_no, item_code, reason}]` for completed orders WITH a snapshot that are excluded: a punch with a blank machine (`"a punch has no machine, so its time cannot be priced"`), or no counted steps planned and none punched (`"no CNC, VMC or inspection work"`).
  - `older_count`: completed orders with no snapshot (started before Profit and Loss began).
- Cost = `minutes / 60 * rate`, rounded to whole rupees only at display (keep floats here; round to 2 decimals in the response).

- [ ] **Step 1: Write the failing tests**

```python
def test_owner_example_planned_and_actual_cost():
    # planned 100 h on a ₹100/h machine, actual 120 h
    r = build_report([order("SO1")], snap(("SO1","X"), [step("CNC4", setup=0, run=6000)]),
                     {("SO1","X"): [act("CNC4", 7200)]}, {"CNC4": 100})["rows"][0]
    assert (r["planned_cost"], r["actual_cost"], r["diff_cost"]) == (10000, 12000, 2000)
    assert r["diff_pct"] == pytest.approx(20)

def test_unpunched_step_flags_the_order_incomplete():   # spec 7a, option b
    r = build_report(... snapshot has CNC4 + CMM, actual only CNC4 ...)["rows"][0]
    assert r["not_punched"] == ["INSPECTION"] and r["actual_hours"] == pytest.approx(...)
    assert [s["status"] for s in r["steps"]] == ["ok", "not punched"]

def test_machine_without_rate_is_rate_not_set_never_zero():
    r = build_report(..., rates={})["rows"][0]
    assert r["planned_cost"] is None and r["rates_missing"] == ["CNC4"]

def test_order_without_snapshot_is_counted_as_older_not_shown():
    rep = build_report([order("OLD")], {}, {}, {})
    assert rep["rows"] == [] and rep["older_count"] == 1

def test_blank_machine_punch_moves_order_to_not_shown(): ...
def test_order_with_no_counted_work_is_not_a_zero_row(): ...   # Review Focus 3
def test_totals_skip_rows_without_both_costs(): ...
```

- [ ] **Step 2: Run** `python3.12 -m pytest tests/test_profit_loss.py -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** `build_report`.
- [ ] **Step 4: Run** the same. Expected: PASS.
- [ ] **Step 5: Commit** `feat(profit-loss): per-order planned vs actual cost rows`.

---

### Task 5: Machine rates on the Machines table

**Files:**
- Modify: `engine/shop_masters.py` (`apply_machine_save`, new `validate_rate`, new `seed_default_rates`, `DEFAULT_RATES`)
- Modify: `api/main.py` (`MachineSaveRequest`, `_save_machine`, `_machine_view`, `get_machines`)
- Modify: `web/app.js` (`renderMachines` and the machine save call), `web/index.html` if the card has a static header
- Test: `tests/test_shop_masters.py`, `tests/test_profit_loss_api.py`

**Interfaces:**
- Produces:
  - `shop_masters.DEFAULT_RATES = {"CNC1": 450, "CNC4": 450, "CNC5": 450, "CNC7": 450, "CNC3": 650, "CNC6": 650, "VMC1": 500, "VMC2": 650, "VMC3": 650, "CMM": 850}`.
  - `seed_default_rates(doc: dict) -> dict`: if `doc.get("rates_seeded")` is falsy, set `rate` from `DEFAULT_RATES` on each existing machine that has no `rate` key, set `rates_seeded = True`; returns a new doc. Idempotent. Called where `get_machines` and `GET /profit-loss` load the doc (save when it changed).
  - `apply_machine_save(doc, mid, item, ...)` carries `item["rate"]` into the saved entry.
  - `validate_rate(rate) -> list[str]`: `None` ok; otherwise a finite number > 0 and ≤ 100000, else `["Rate per hour must be more than 0, or left empty."]`.
  - `MachineSaveRequest.rate: Optional[float] = None`. In `_save_machine`: if `"rate" not in req.model_fields_set`, keep the stored rate; else use `req.rate` (None clears it).
  - `_machine_view(mid, m, role)` includes `"rate"` only when `role == auth.ADMIN`.
  - `rates_from_doc(doc) -> dict[str, float]` in `engine/profit_loss.py`.

- [ ] **Step 1: Write the failing tests**

```python
def test_save_without_rate_keeps_the_stored_rate(admin):        # Review Focus 1
    # PUT /machines {id: CNC4, type, hours, version} with no "rate" key -> GET shows rate 450
def test_rate_zero_or_negative_is_rejected(admin):              # Review Focus 4: 400
def test_rate_blank_clears_it(admin):                           # PUT rate=None -> no rate
def test_default_rates_seed_once_and_never_overwrite(admin):    # edit CNC4 to 500, reload: still 500
def test_user_role_never_sees_rates(user):                      # GET /machines: no "rate" key anywhere
def test_rate_has_no_plan_effect(admin):                        # machines_digest and _plan_fingerprint equal before/after a rate edit
```

- [ ] **Step 2: Run** `python3.12 -m pytest tests/test_shop_masters.py tests/test_profit_loss_api.py -v`. Expected: new tests FAIL.
- [ ] **Step 3: Implement** the backend, then the UI: a "Rate per hour (₹)" column in `renderMachines`, editable in the same edit row as type/hours, sent as `rate` (empty input → `null`), the column and input built only when `currentRole === "admin"` (JS-built markup needs its own role check, 2026-08-09 rule).
- [ ] **Step 4: Run** the same, then the full suite. Expected: PASS.
- [ ] **Step 5: Commit** `feat(machines): an hourly rate per machine, admin only, no plan effect`.

---

### Task 6: `GET /profit-loss` and the tab

**Files:**
- Modify: `api/main.py` (new endpoint near the production analysis endpoints)
- Modify: `web/index.html` (nav link + `<section class="view" id="view-profitloss">`), `web/app.js` (`VIEWS`, `showView` routing + non-admin redirect, `renderProfitLoss`), `web/style.css` (only if needed; reuse existing table classes)
- Modify: `tests/test_role_parity.py` (`ALLOWED_ADMIN_ONLY_TABS = {"neworders", "itemmaster", "profitloss"}`)
- Test: `tests/test_profit_loss_api.py`

**Interfaces:**
- Consumes: Tasks 2-5.
- Produces: `GET /profit-loss` (`require_admin`) → `build_report(...)` output, built live on every call from `load_completed_orders()`, `load_pnl_planned()`, `load_actuals()`, the current masters (for cycle times), `machine_kinds`/`rates_from_doc` on the seeded Machines doc, `shift_working_minutes(config)`. Never cached.

Screen (spec section 9): heading "Profit and Loss"; one line under it: "Machine time cost of finished orders, CNC, VMC and inspection only. Selling price and profit will be added later."; table columns `SO No | Item | Qty | Completed on | Planned hrs | Planned cost | Actual hrs | Actual cost | Difference | Difference %`; difference red when > 0; "rate not set" / "actual incomplete: N step(s) not punched" shown in the row; click a row to toggle its step breakdown; totals row ("Totals (N orders, M incomplete)"); below the table, the `not_shown` list and "N finished orders started before Profit and Loss began are not shown." when `older_count > 0`; empty state "No finished orders to show yet."

- [ ] **Step 1: Write the failing tests**

```python
def test_profit_loss_is_admin_only(user): assert user.get("/profit-loss").status_code == 403
def test_end_to_end_row(admin):
    # sample book: /run (snapshot), punch every counted step of one order with known minutes on CNC4/CMM,
    # mark complete; GET /profit-loss has one row with planned/actual hours as computed by hand
def test_rollback_of_completion_removes_the_row(admin):   # Review Focus 5
def test_already_started_order_never_appears(admin):     # punch before the first /run with the feature, complete it -> older_count 1, rows []
```

Plus the updated whole-nav invariant in `test_role_parity.py`.

- [ ] **Step 2: Run** `python3.12 -m pytest tests/test_profit_loss_api.py tests/test_role_parity.py -v`. Expected: FAIL.
- [ ] **Step 3: Implement** endpoint and tab.
- [ ] **Step 4: Run** the same, then the full suite. Expected: PASS. Then drive the tab in a browser on a throwaway local instance (`uvicorn api.main:app`, local store, sample or Test9 workbook) as admin and as user: admin sees the tab and rates; user sees neither and `#profitloss` redirects; no console errors.
- [ ] **Step 5: Commit** `feat(profit-loss): admin-only Profit and Loss tab`.

---

### Task 7: Verification on real data, mutation tests, docs

**Files:**
- Modify: `CLAUDE.md` (new banner bullet at the top of CURRENT STATE)
- Create: `docs/superpowers/specs/2026-11-11-profit-and-loss-verification.md` (harness + numbers)

- [ ] **Step 1: Live-store copy.** With the owner's `MONGODB_URI`, copy every key to a local store (READ-ONLY on Atlas, nothing written back; method in memory `reading-live-anvitech-data`). On the copy: `/run` before and after the feature commit gives identical schedules (hash machine, operator, start, end, qty per entry); the snapshot holds exactly the active orders with zero punches; every order with a punch is absent; for each clubbed batch, members' planned minutes sum to the batch op's minutes (setup + run, unpadded).
- [ ] **Step 2: Simulate one order end to end on the copy:** punch its counted steps, mark complete, check the row by hand.
- [ ] **Step 3: Mutation-test** each load-bearing part, reverting one at a time and confirming at least one test fails (use `python3.12 -B` or sleep 1 s after restoring, stale bytecode masked a mutation before): the zero-punch gate in `merge_snapshot`; the unpad (`run = pieces * Co` → occupancy-based padded); the downtime deduction; the overbooked-shift scaling; the counted-kind filter; `require_admin` on `/profit-loss`; the `model_fields_set` keep-rate rule; the user-role rate strip. Record which are load-bearing and any that are not, said plainly.
- [ ] **Step 4: Docs.** CLAUDE.md banner bullet: what it does, the snapshot-at-first-punch rule, the store key, rates on the Machines table with defaults seeded once, measured results, what was not verified. Full suite count recorded.
- [ ] **Step 5: Commit** `docs: Profit and Loss verification and CLAUDE.md`. Then stop and ask the owner before pushing to `main`.
