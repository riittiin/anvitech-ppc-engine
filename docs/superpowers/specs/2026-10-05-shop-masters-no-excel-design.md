# Machines and holidays move into the app; the Excel upload goes (stage 2)

**Date:** 2026-10-05 · **Status:** implemented, unpushed (verification: `2026-10-05-shop-masters-verification.md`)
**Branch:** `shop-masters` (from `origin/main` @ `1199583`, which carries stage 1)
**Stage:** 2 of 2. Stage 1 (Item Process Master) is live: `docs/superpowers/specs/2026-10-05-item-process-master-tab-design.md`.

## 1. Why

After stage 1, the uploaded workbook still supplies the **Machine master** and the
**Weekly off & holiday master**. The owner wants nothing to depend on uploading an
Excel: both move into Settings, and the Upload button is removed.

**Success means:**
1. No planning or reporting path opens the workbook once the app's tables exist: the
   screen plan, every report, the quote, and the cloud / Mac / GitHub optimize
   workers all build the shop from the app's tables.
2. The switch moves nothing: right after the one-time seed, plans are byte-identical
   to stage 1's on Test5/8/9 and on a copy of the live store.
3. An admin can add a machine, change its type or shifts, add and remove holidays,
   all in Settings, without a file.
4. The Upload button and `POST /upload` are gone; the workbook already on file is
   kept untouched as the seed source and for rollback.

## 2. What the workbook still feeds, measured on `1199583`

| Sheet | Fields read | Used by the plan? | Decision |
|---|---|---|---|
| Machine master | Machine No, Machine Type, Available Hrs/Day | yes: id, type → kind (machining / manual / inspection), two-shift vs single-shift | move to app |
| Machine master | Hr Rate | **no reader** outside loader/model | drop |
| Weekly off & holiday | weekly off day | classic reads the sheet; **ppc_engine hardcodes Thursday** (`masters_loader.load_calendar`) | show Thursday, not editable (owner) |
| Weekly off & holiday | Holiday rows | yes: whole shop closed | move to app |
| Weekly off & holiday | Leave rows | ppc per-operator leave | drop: Settings > Operator absences already owns this; real books carry two April-2025 rows only |
| Operator & shift sheet | Role | `new_engine._apply_app_operators` inherits role by name, but role reaches only `worktime._shift_for`, which ignores it since rotation was removed (`week_anchor=None`, 2026-08-05) | **no move needed**: role has no plan effect today; app-added people already get an inferred role. Proven by verification (section 8). |
| Sales Order sheet | (ignored since 2026-10-03) | no | nothing |

Real books (Test5/9): 26 machines across 12 type strings (CNC lathe, Vertical
Machining center, Manual Packing/Inspection/Washing/deburring/Punching/Assembly,
Band saw cutting machine, Measuring Machine, Center lathe, Drilling M/c), hours
19.5 (CNC/VMC) or 9.5 (everything else). Holidays: three in 2025 and one in 2026
(15-08-2026). **The 2026 holiday list is effectively empty; the owner should add the
rest of 2026 once the card exists.**

## 3. Storage

Two new store keys, same shape and rules as `anvitech:item_process_master`
(one JSON document each, insertion order kept, never sorted, `seed_digest` stored
at seed time):

**`anvitech:machines`**
```json
{"seeded_at": "...", "seeded_from_sha": "...", "seed_digest": "...",
 "machines": {"CNC4": {"name": "CNC 4", "type": "CNC lathe", "hours": 19.5, "version": 1}}}
```
- Key = canonical id (`normalize_resource_id`, `CNC 4` ≡ `CNC4`); `name` = as written
  (display); `type` = raw type text; `hours` = raw Available Hrs/Day value (number,
  or null when the sheet cell was blank, preserved so both loaders read it as before).

**`anvitech:shop_calendar`**
```json
{"seeded_at": "...", "seeded_from_sha": "...", "seed_digest": "...",
 "holidays": [{"date": "2026-08-15", "name": "Independence Day"}]}
```
- Weekly off is not stored: Thursday, as the engine already enforces.
- Leave rows are not seeded.

## 4. Seeding (once)

Same as stage 1: the first time masters are needed, each table whose key is empty is
filled from the workbook on file, through the SAME reader the loaders use (one
reading of each sheet). After that the workbook is never read for planning. No
workbook on file (a fresh install) means empty tables; machines and holidays are
then added by hand. The operator table's existing one-time seed keeps reading the
workbook's operator sheet when (and only when) that table is empty.

## 5. How the tables reach every planner

Same pattern as stage 1, extended so a workbook is no longer needed at all:

- Each loader's per-row rule is extracted once and used for both sources:
  classic `_machine_from_row(...)`, `_calendar_from_rows(...)`; ppc
  `machines_from_rows(...)`, `calendar_from_rows(...)`. Each sheet reader becomes
  "read rows" + that rule (the stage-1 refactor shape).
- Both `load_all`s gain `machine_rows=` and `holiday_rows=` next to `routing_rows=`.
  **When all three are given, no workbook is opened** (`path` may be None): operators
  come from the app overlay as today, SO lines are empty (orders come from the book).
- The masters builders (`api.main._current_masters`, `new_engine._new_masters`,
  `optimize_service.parse_payload` / `run_candidate`) pass all three tables. The
  optimize payload carries `machines` and `shop_calendar` beside `item_master`;
  `masters_xlsx_b64` keeps being sent only so a job in flight across the deploy, or
  an old worker, still parses; a payload with all three tables never opens it.
- Caches, fingerprints and the inputs signature fold in the two new digests exactly as
  stage 1 folded in the routing table: plan fingerprint always; inputs signature only
  once a table differs from its `seed_digest`, so the switch itself never marks an
  applied optimization stale.
- `SCHEDULER_FINGERPRINT` is not bumped (no work moves).

## 6. Settings UI

Two new cards, placed before **Operators & shifts** (owner's layout choice). Admin
edits; user role sees them read-only (role checks in JS, writes `require_admin`).

**Machines**
- Table: Machine (name), Type, Shifts ("2 shifts, 19.5 h" / "1 shift, 9.5 h" / for a
  seeded non-standard value, "N h a day"), remove.
- "+ Add machine": machine number (canonicalized; must be new), Type picked from a
  list (the types already in the table, plus "Other…" for a new type typed once),
  Shifts (2 shifts = 19.5 h, 1 shift = 9.5 h).
- Edit Type and Shifts in place; the machine number is never renamed (routings,
  operators, punches, downtime and frozen work refer to it).
- A new machine appears at once in every machine picker (Item Process Master steps,
  Operators, Machine maintenance).
- Remove is refused, with what still uses it, while the machine is named by: an
  Item Process Master step, an operator's machines, a maintenance break, or a frozen
  (in-progress) operation.
- A note under the table: CNC/VMC types get the 90-minute setup and the +30% planning
  allowance; the type decides that, so pick it carefully.

**Weekly off & holidays**
- "Weekly off: Thursday" (shown, not editable).
- Holidays list (date DD-MM-YYYY, name), newest-upcoming first, past ones greyed;
  add (date + name, one holiday per date) and remove. A holiday closes the whole shop,
  both shifts.

**Upload card**: removed (section 7).

## 7. Removing the upload

- `POST /upload` is deleted; the Upload card is removed from `web/index.html` and its
  JS from `web/app.js`; copy elsewhere that tells people to upload the Excel
  (explainers, the empty-state text "Upload your Excel to list machines", the
  operators panel's "filled in once from your uploaded" line) is rewritten.
- `anvitech:masters` (the stored workbook) is NOT deleted: it is the seed source and
  keeps a rollback to stage 1 working.
- Tests that seed data through `/upload` (8 files) switch to the store
  (`book_store.save_masters_bytes`, already used by most tests).

## 8. API

| Method | Path | Role | Purpose |
|---|---|---|---|
| GET | `/machines` | any | `{machines: [{id, name, type, hours, kind, used_by: [str]}], types: [str]}` |
| POST | `/machines` | admin | add `{id, type, hours}` |
| PUT | `/machines` | admin | edit `{id, type, hours, version}` (409 stale) |
| POST | `/machines/delete` | admin | `{id}`; 400 with what uses it; 404 unknown |
| GET | `/holidays` | any | `{weekly_off: "Thursday", holidays: [{date, name}]}` |
| POST | `/holidays` | admin | add `{date, name}`; 400 duplicate date / bad date |
| POST | `/holidays/delete` | admin | `{date}` |

Ids travel in the JSON body (stage-1 rule). Validation: id non-empty after
canonicalizing; type non-empty; hours a finite number in (0, 24]; date a real date
within ±5 years of today. Each write re-plans once from the client; none starts an
optimization. Existing `GET /operators`' `machines` list and the downtime picker read
the machines table through `_current_masters()`, unchanged.

## 9. Verification (before release)

1. **Byte-identical switch**: the stage-1 harness (Test5/8/9/9-ORIGINAL × flexible ×
   clean / WIP+frozen) against `1199583`: 16/16 schedule hashes equal, the quote equal,
   with masters built from the three tables and NO workbook opened (assert it, e.g.
   by making the stored workbook unreadable after seeding).
2. **Live-store copy** (needs `MONGODB_URI` from the owner): same comparison on a
   read-only copy, with live WIP, frozen ops, applied ranks and absences.
3. **Every reader follows an edit**: change a machine to 1 shift, add a holiday inside
   the plan window; show the plan, Analytics, delay report, shift-wise, Daily Entry
   machine list, production analysis working minutes, quote and the payload all move.
4. **Role has no effect** (the section-2 claim): flip every operator's role and show
   the plan hash is unchanged.
5. **Mutation testing** of each wiring point and rule; report any that fail no test.
6. **Browser run** as both roles, throwaway local instance, never the live site.
7. Full suite on `python3.12 -m pytest`.

## 10. Not in stage 2

A changeable weekly off day; half-day holidays; hour rate; per-machine calendars
beyond the existing Machine maintenance; an Excel import or export of any table.

## 11. Risks

- **Seeding from a stale workbook.** The live workbook's machine and holiday sheets
  are copied once. Machines are stable; holidays are known stale (section 2), so the
  release note asks the owner to enter the 2026 holidays.
- **A path that still opens the workbook** would plan from it while Settings shows the
  tables. Covered by verification 1 (workbook made unreadable) and a grep of
  `load_all(` / `load_masters_bytes` / `open_workbook` callers.
- **Removing a machine type's meaning by retyping it** (e.g. CNC4 changed to
  "Manual") silently drops its setup and +30% allowance. Guarded by the note in the
  card and by the edit being explicit; not otherwise blocked.
