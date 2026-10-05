# Item Process Master tab: routings move out of Excel into the app

**Date:** 2026-10-05 · **Status:** design approved in conversation, awaiting spec review
**Branch:** `item-process-master` (from `origin/main` @ `c123fa1`)
**Stage:** 1 of 2 in removing the Excel upload entirely (see "Staging" below)

## 1. Why

New items and routing changes still arrive by editing the "Item's process Master"
sheet and uploading the workbook. That sheet is a 5-columns-per-step grid (up to 12
steps across), so finding an item, reading its route, or changing one cycle time is
slow and easy to get wrong. The owner wants the routing master inside the app, on
its own tab, easy to read and easy to edit, holding only the fields the plan uses.

**Success means:**
1. Every place that plans or reports reads routings from the app's table: the
   screen plan, both loaders, the quote, production analysis, and the cloud / Mac
   optimize workers. No path reads the Excel routing sheet once the table exists.
2. Switching from Excel to the table moves nothing: right after the one-time seed,
   the plan is byte-identical to the Excel-fed plan.
3. An admin can find an item, change a cycle time or a machine, add / insert / remove
   / reorder steps, create a new item (blank or copied), and save, without ever
   typing a machine id or knowing the Excel's column layout.
4. No edit can silently discard recorded production.

## 2. Staging

| Stage | What moves into the app | Upload button |
|---|---|---|
| **1 (this spec)** | Item's process Master | stays: Machine master + Weekly off & holiday still come from the workbook |
| 2 (own spec, later) | Machine master, Weekly off & holiday | removed once stage 2 ships |

Stage 1 must not make stage 2 harder: the routing table never depends on workbook
structure, so stage 2 repeats the same pattern for machines and the calendar.

## 3. Fields: what is kept and what is dropped

Measured on the code (`origin/main`): of everything the loaders read per item, only
these are used anywhere.

**Per item (kept):** `item_code` (key; fixed once created, since orders, punches,
ranks and frozen rows are keyed by it), `description` (shown on Schedule, Gantt,
delay report, production analysis).

**Per step (kept), in routing order:** `name`, `cycle` (minutes per piece, the
ORIGINAL time; for outsourced steps the flat block in minutes, entered in hours in
the UI), `allotted` (raw machine text), `suggested` (raw machine text).

**Dropped:** Total Time, Customer, RM type, MOQ. `Process.total_time`,
`Routing.customer`, `Routing.rm_type`, `Routing.moq` have zero readers outside the
loader and the model; they stay on the dataclasses (as blank/None) so no signature
changes.

The CNC/VMC +30% planning allowance (`engine/planning_time.py`) is unchanged. The
table holds original times; the planner pads at plan time exactly as today. The tab
says so under every CNC/VMC step.

## 4. Storage

New store key **`anvitech:item_process_master`**:

```json
{
  "seeded_at": "2026-10-05T10:12:00",
  "seeded_from_sha": "<sha256 of the workbook it was seeded from>",
  "items": {
    "9611416360": {
      "description": "Bracket housing",
      "version": 3,
      "updated_at": "2026-10-05T11:40:00",
      "updated_by": "anvitech",
      "steps": [
        {"name": "BAND SAW OS", "cycle": 2880, "allotted": "OS", "suggested": ""},
        {"name": "CNC FIRST SIDE", "cycle": 6.5, "allotted": "CNC4", "suggested": "CNC5"}
      ]
    }
  }
}
```

- **Raw text is stored, not a pre-classified kind.** `allotted` / `suggested` hold
  the same text the Excel cell held. Classification (machining / manual /
  inspection / outsourced / dispatch) keeps happening in one place,
  `ppc_engine.loaders.normalize.classify_operation`, on the same inputs. This is
  what makes guarantee 2 (byte-identical) hold by construction. The UI's step
  "type" is a view over these fields (section 7), never stored.
- **One document**, read and written whole (≈85 items × ≤12 steps, a few tens of
  KB). `book_store.load_item_master()` / `save_item_master(doc)` /
  `save_item(code, item, expected_version)`.

## 5. Seeding (once)

Same pattern as operators (2026-07-18):

- The first time the table is needed and the store key is **empty** and a workbook
  is on file, the table is filled from the workbook's routing sheet by the
  existing loader, row for row, step for step, raw text preserved.
- After that the workbook's routing sheet is **never read again**, including on a
  later upload. The upload response and the Upload panel say: "Item routings are now
  managed in the Item Process Master tab; the routing sheet in this file was not
  read."
- Empty key and no workbook means an empty table; the tab offers "+ New item".
- Seeding is triggered from the one wiring point (section 6), so no API path can
  observe an unseeded state on a deploy that already has a workbook.

## 6. How the table reaches every planner

**One parse rule per loader, two sources.** Each of the two existing loaders already
turns one Excel row into a routing:

- `engine/loaders.py::_load_routings` → classic `Routing` / `Process`
- `ppc_engine/loaders/masters_loader.py::load_routings` → new-engine `Routing` /
  `Operation` (incl. `classify_operation`, `flexible_machines`, `ROUTING_GAP`)

Each loader's per-row body is extracted into a function taking
`(code, description, steps)`, where `steps` is the list of
`(name, cycle, total, suggested, allotted)` it already reads from the cells. The
workbook path calls it per sheet row (unchanged behaviour); the table path calls it
per stored item. There is then exactly ONE rule per loader for "steps → routing",
whatever the source. The two loaders' existing subtle differences (classic
`continue`s past a blank step, ppc `break`s) are irrelevant for the table, which
never stores a blank step, and stay as-is for the workbook path.

**Wiring points:**

1. **Classic masters** (`api.main._current_masters`): after loading the workbook and
   applying the operator overlay, replace `masters.routings` with the table's
   routings and rebuild everything derived from routings: provisional machines
   (`PENDING_MASTER_DATA`), `DUPLICATE_PROCESS`, and the report rows. New helper
   `_with_item_master_overlay(base)`.
2. **New-engine masters** (`engine/new_engine._new_masters`): after
   `load_all(...)`, replace `masters.routings` with the table's routings built by
   the ppc parse rule at the requested `flexible`, then re-run ppc's
   provisional-machine registration (`masters_loader` step that registers machines
   referenced by a routing but missing from the Machine master). The
   `_MASTERS_CACHE` key becomes `(workbook sha, table digest, flexible)`. Padding
   (`pad_routings`) applies after, unchanged.
3. **Cloud / Mac / GitHub workers** (`engine/optimize_service`): `build_payload`
   gains `item_master=<the stored doc>`; `parse_payload` hands it to the same
   overlay (new_engine gets it via a `set_item_master(doc)` sibling of
   `set_masters_bytes`). A payload without the field (old job in flight during a
   deploy) falls back to the workbook's sheet, which is what that job was built
   from. Oracle/Mac workers hard-reset to `origin/main` before every job; GitHub
   runs repo code, so all three pick this up on deploy.
4. **Readers that use routings directly** (15 `.routings` references in
   `api/main.py`, `engine/quote.py`, `engine/production_analysis.py`) all read
   through `_current_masters()` or the new-engine masters already, so they inherit
   the overlay. The plan verifies this with a grep and one test per reader, not by
   assertion.

**Caches and signatures** (the 2026-08-08 rule: a cache must be keyed on everything
it displays):

- `_plan_fingerprint` and `_inputs_signature` gain a digest of the table (items +
  steps + descriptions; `version` / `updated_*` excluded). An edit therefore
  refreshes every screen and marks an applied optimization "settings changed since
  the last search", through the existing mechanism.
- `SCHEDULER_FINGERPRINT` is **not** bumped: the switch moves no work (proven in
  section 10). Edits change inputs, which `_inputs_signature` already covers.

## 7. The tab

**Navigation:** a new tab "Item Process Master", visible to both roles. Writes are
admin-only, enforced on the server (403) and hidden for the user role. The
role-parity whole-nav invariant (`tests/test_role_parity.py`) is unaffected because
the tab is visible to both roles.

**Layout (owner's choice: list + step editor):**

```
Search [9611416360      ]  [+ New item]
+----------------------+-------------------------------------------------+
| 9611416360  6 steps  | 9611416360  Bracket housing        [Edit]       |
| 9611443241  8 steps  |-------------------------------------------------|
| 2109801     3 steps  | # Type      Process          Cycle  Allotted  Suggested
| 61240807-01 9 steps  | 1 Outsourced BAND SAW OS      48 h                |
| ...                  | 2 Machine   CNC FIRST SIDE   6.5 m [CNC4 x] [CNC5 x]
|                      | 3 Machine   CNC SECOND SIDE  4.0 m [CNC4 x] [+ add]
| (!) 3 items need     | 4 Machine   DEBURING         1.2 m [MD1 x]  [MD2 x]
|     a machine        | 5 Machine   INSP             0.5 m [MI1 x]
|                      | 6 Dispatch  DISPATCH           -   (milestone)
|                      |   CNC/VMC times get +30% in the plan             |
+----------------------+-------------------------------------------------+
```

- **Item list:** searchable by code or description, shows step count and a "used by
  N open orders" hint; items with a step that has no machine and is not
  outsourced/dispatch (today's `ROUTING_GAP`) carry a warning flag, with a filter
  "needs a machine".
- **Step type** is shown and chosen as Machine / Outsourced / Dispatch. It maps onto
  the raw fields exactly as `classify_operation` reads them: Outsourced writes
  `allotted = "OS"` and cycle in minutes from the hours box; Dispatch is a step
  named DISPATCH with no machine and no cycle; Machine stores the picked ids.
  A seeded step is displayed with the type `classify_operation` gives it.
- **Machines are picked, never typed**: chips with ✕ plus a "+ add" dropdown of
  Machine-master machines grouped by type, joined with `/` (the operator-picker
  convention, 2026-08-04). A seeded token the scheduler cannot match shows as a red
  "unknown" chip; it may stay (forgiving master principle: it becomes a provisional
  machine as today) but cannot be newly added.
- **Editing (owner's choice):** Edit opens the item; change any step, insert a step
  above/below, remove a step, move a step up/down, change the description; then
  **Save** (one write, one re-plan) or **Cancel** (discards). Navigating away with
  unsaved edits asks first.
- **New item:** "+ New item" asks for the item code and description, then starts
  blank or as a **copy of an existing item's steps**.
- **Delete item:** refused while any active order or any Add New Orders draft/queue
  line uses the item code, with the list of those orders.

## 8. Validation on Save (server-side; the UI shows the same messages)

Shape rules:
- item code non-empty, trimmed, and unique (on create); never changed after create.
- at least one step; at most 12 (the workbook and ppc loader limit).
- every step name non-empty; names distinct after `normalize_process_name` (punches
  and per-step progress are keyed by that, today's `DUPLICATE_PROCESS` rule, now a
  hard refusal instead of a report row).
- cycle a number ≥ 0; a Machine step needs cycle > 0 and at least one machine.

**Punch safety (owner's choice: block, say why):** for every **active** order of
this item, take the steps that have recorded quantity (`completed_by_process`, the
same accounting planning uses). Save is refused if any of those step names would
disappear (renamed or removed), or if their order relative to each other would
change (that would let a downstream step show more pieces than its new upstream,
which `precedence_cap_error` forbids at capture). Message names the step, the
quantity and the order: *"CNC FIRST SIDE has 120 punched on 26-27SO113. Finish or
complete that order before renaming or removing this step."* Cycle times, machines,
descriptions and new steps are always allowed.

**Two admins:** Save sends the `version` it loaded; a mismatch returns 409 "Someone
else saved this item since you opened it; reload to see their change." No silent
last-write-wins.

## 9. API

All under the existing gatekeeper; writes `require_admin`.

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/item-master` | any | `{items: [{code, description, steps, version, open_orders, needs_machine}], machines: [...], seeded}` |
| POST | `/item-master` | admin | create `{code, description, steps}` (or `copy_from`) |
| PUT | `/item-master/{code}` | admin | save `{description, steps, version}` |
| DELETE | `/item-master/{code}` | admin | delete if unused |

Every write: validate (section 8), save, then the client calls `runPlan(false)`
once. No write starts an optimization (only Done does, as today).

## 10. Verification (what must be shown before this ships)

1. **Byte-identical switch.** On Test5, Test8, Test9 (book-size and with real WIP /
   derived frozen sets) and on a READ-ONLY copy of the live store: plan from the
   workbook, seed the table, plan from the table. Hash machine, operator, start,
   end, qty on every entry, both engines' masters and `flexible` True/False: equal
   on every run. Also the quote (`/new-orders/quote`) on the store copy.
2. **Every reader switched.** For each consumer of routings (screen plan, delay
   report, Gantt, shift-wise, production analysis, quote, `/items`, cloud payload
   round-trip): change one cycle time in the table and show that consumer moves,
   with the workbook untouched.
3. **Upload no longer reaches routings.** Upload a workbook with a different
   routing sheet after seeding: plan unchanged.
4. **Safety rules.** Rename/remove/reorder a punched step on an active order: 400
   with the message. Same on a completed order or an unpunched step: allowed.
   Delete an in-use item: refused.
5. **Mutation testing.** Revert each load-bearing part (classic overlay, new-engine
   overlay, payload field, fingerprint digest, punch-safety rule, version check)
   one at a time and confirm at least one test fails; report any that do not, by
   name.
6. **Browser run** of the tab on a throwaway local instance with Test9 as both
   roles: search, edit, insert, reorder, copy, save, cancel, the refusal messages,
   no console errors.
7. Full suite on `python3.12 -m pytest`.

## 11. Not in stage 1

Edit history / audit log of routing changes; bulk import or Excel export of the
table; editing machines or holidays (stage 2); removing the upload (after stage 2);
renaming an item code; carrying punches across a step rename.

## 12. Risks

- **A reader that bypasses the overlay** would plan from Excel while the screen
  shows the table. Covered by verification 2 and a grep for `load_routings` /
  `_load_routings` / `masters_bytes` callers.
- **Stale optimize jobs during deploy** carry no table; they fall back to the
  workbook they were built from (section 6.3) and their result is judged by the
  existing apply gates.
- **The local `main` is 8 commits behind `origin/main`.** This work is based on
  `origin/main`; the uncommitted `optimizer-search-fixes` branch is untouched.
