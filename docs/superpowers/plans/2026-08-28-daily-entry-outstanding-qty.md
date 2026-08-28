# Daily Entry outstanding-quantity guard — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show the Daily Entry operator what a step still owes *before* he punches it, and warn him when the quantity he typed exactly finishes a different sales order for the same part.

**Architecture:** One new pure function in `engine/orderbook.py` derives per-(SO, item, step) `done / still_to_make / can_enter_now` by reusing `_process_totals` — the same internal `precedence_cap_error` and `active_so_lines` already count pieces with. It is served on the existing live-read `GET /items` (never on the cached `/run`). `web/app.js` renders it under the Process dropdown and runs a small pure cross-check before Save. Display only: no scheduled date moves, and the server write path is not modified.

**Tech Stack:** Python 3.12, FastAPI, pytest, vanilla ES5-flavoured JS (no framework, no build step), plain CSS.

**Spec:** `docs/superpowers/specs/2026-08-28-daily-entry-outstanding-qty-design.md`

## Global Constraints

These apply to **every task**. They are the owner's explicit instruction of 2026-08-28 and the spec's "Scope contract" section.

**Only these files may change:**

| File | Permitted change |
|---|---|
| `engine/orderbook.py` | **Add** `entry_key` and `entry_progress`. No existing function edited. |
| `api/main.py` | **Only** the body of `items()` — two keys added to its return dict. |
| `web/app.js` | **Only** the Daily Entry form region: `actualsFormHtml`, `wireActualsForm`, the `a-save` handler, plus new functions added next to them. |
| `web/style.css` | **Append** new class rules at the end only. No existing rule edited. |
| `tests/test_daily_entry_guard.py` | New file. |
| `CLAUDE.md` | Append one bullet (Task 5 only). |

**Must NOT be touched, at any point, for any reason:**

- `ppc_engine/` — the entire vendored scheduling package.
- `engine/rules/` — every rule module.
- `engine/new_engine.py`, `engine/freeze.py`, `engine/optimizer.py`, `engine/flow_scheduler.py`, `engine/pipeline.py`, `engine/analytics.py`, `engine/delay_report.py`, `engine/efficiency.py`, `engine/book_store.py`, `engine/models.py`, `engine/config.py`, `engine/loaders.py`.
- `api/main.py` **anywhere but the `items()` function** — in particular `_plan`, `_plan_fingerprint`, `_resolve_config`, `post_actuals`, `rollback_actual`, `_report_for_book`, `_try_start_auto`, and every optimize endpoint.
- `engine/orderbook.py`'s **existing** functions. `_process_totals`, `_norm`, `precedence_cap_error`, `active_so_lines`, `process_progress_rows`, `completed_by_process`, `order_rows` are **read and reused, never edited**.
- **Any existing test file.** If a change here makes an existing assertion fail, that is a signal the work has leaked out of scope: **stop and ask the owner.** Do not rebase the assertion.
- `web/style.css`'s **uncommitted** working-tree change (the 18-08 full-width `.view` edit). Leave it exactly as found; never stage it.

**Other constraints:**

- No new dependency. Standard library plus what is already imported.
- No new endpoint. `GET /items` is the carrier.
- Never put this data on `POST /run` — that response is cached by `_plan_fingerprint`, and live punch data inside it reproduces the 2026-08-08 stale-cache bug.
- The composite key separator is ASCII **unit separator**, written as the escape `"\x1f"` in Python and `"\u001f"` in JavaScript. Write the **escape**, never a literal invisible character in source. The two must produce the identical string.
- `web/app.js` matches the file's existing style: `function` declarations, `const`/`let`, template literals, `$("id")` lookups, `escapeHtml()` on every interpolated value.
- All user-facing copy is plain operator English, no em dashes (repo convention, 2026-08-05).
- Every task ends with `pytest` fully green.

---

### Task 1: `entry_progress` — the one new pure function

**Files:**
- Modify: `engine/orderbook.py` (append two functions at the end; touch nothing above)
- Test: `tests/test_daily_entry_guard.py` (create)

**Interfaces:**
- Consumes: `orderbook._process_totals(actuals, so_no, item_code) -> (produced: dict, good: dict)` and `orderbook._norm(name) -> str`, both already in the file.
- Produces:
  - `orderbook.entry_key(so_no: str, item_code: str) -> str` — `so_no + "\x1f" + item_code`
  - `orderbook.entry_progress(active_orders: dict, actuals: list, masters=None) -> dict` mapping `entry_key(...)` to
    `{"so_no": str, "item_code": str, "item_name": str, "ordered": float, "delivery_date": str (ISO), "steps": [{"seq": int, "process": str, "done": float, "still_to_make": float, "can_enter_now": float}, ...]}`
  - Task 2 serves this dict verbatim; Tasks 3 and 4 read these exact key names in JS.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_daily_entry_guard.py`:

```python
"""Daily Entry: what a step still owes, shown before the punch (2026-08-28 spec).

Two SO lines on the same item code are clubbed into one batch by Rule 1, so the
floor sees one pile of identical parts. 26-27SO149 (400) and 26-27SO150 (23) were
such a pair: SO150's 23 finished, the 23 were punched against SO149, and the order
book then told the directors the exact opposite of the truth.

These tests pin the numbers the form shows, so the operator never has to leave the
tab to work that out. The load-bearing one is
``test_can_enter_now_agrees_with_the_save_guard``: what the panel offers must be
exactly what ``precedence_cap_error`` accepts, or the form would invite a punch the
server then refuses.
"""
from datetime import date

from engine.models import Order, Actual, Masters, Routing, Process
from engine import orderbook

D = date(2025, 8, 1)


def _routing(item, names):
    return Routing(
        item_code=item, description="", customer="", rm_type="", moq=None,
        processes=[Process(seq=i + 1, name=n, cycle_time=1, total_time=1,
                           suggested_machine="M1", allotted_machine="M1")
                   for i, n in enumerate(names)],
    )


def _masters(*routings):
    return Masters(routings={r.item_code: r for r in routings})


def _order(so, item, qty, d=D, completed=False):
    return Order(so_no=so, item_code=item, item_name=item, ordered_qty=qty,
                 delivery_date=d, completed=completed)


def _act(so, item, process, prod, rej=0):
    return Actual(so_no=so, item_code=item, entry_date=D, process=process,
                  qty_produced=prod, qty_rejected=rej, item_name=item, operator="o")


# Two real steps, so there is always an upstream cap to check.
_R = _routing("A", ["CNC FIRST SIDE", "VMC FIRST SIDE"])


def _steps(progress, so, item):
    """{process name: step dict} for one order — order-independent lookups."""
    entry = progress[orderbook.entry_key(so, item)]
    return {s["process"]: s for s in entry["steps"]}


def test_unpunched_order_owes_the_whole_quantity():
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    prog = orderbook.entry_progress(active, [], _masters(_R))
    entry = prog[orderbook.entry_key("SO1", "A")]
    assert entry["ordered"] == 400
    assert entry["delivery_date"] == "2025-08-01"        # ISO; the browser formats it
    s = _steps(prog, "SO1", "A")
    assert s["CNC FIRST SIDE"]["done"] == 0
    assert s["CNC FIRST SIDE"]["still_to_make"] == 400
    # First step: nothing upstream limits it, so the cap is the ordered qty.
    assert s["CNC FIRST SIDE"]["can_enter_now"] == 400
    # The second step owes the full order but nothing has cleared step 1 yet.
    assert s["VMC FIRST SIDE"]["still_to_make"] == 400
    assert s["VMC FIRST SIDE"]["can_enter_now"] == 0


def test_rejects_net_across_entries():
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    acts = [_act("SO1", "A", "CNC FIRST SIDE", 100),
            _act("SO1", "A", "CNC FIRST SIDE", 0, rej=20)]   # scrapped the next day
    s = _steps(orderbook.entry_progress(active, acts, _masters(_R)), "SO1", "A")
    assert s["CNC FIRST SIDE"]["done"] == 80               # 100 produced - 20 rejected
    assert s["CNC FIRST SIDE"]["still_to_make"] == 320


def test_downstream_step_is_capped_by_what_cleared_upstream():
    """The 166/400 case: the step owes 400 but only 166 can be entered today."""
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    acts = [_act("SO1", "A", "CNC FIRST SIDE", 166)]
    s = _steps(orderbook.entry_progress(active, acts, _masters(_R)), "SO1", "A")
    assert s["VMC FIRST SIDE"]["still_to_make"] == 400     # what the order owes here
    assert s["VMC FIRST SIDE"]["can_enter_now"] == 166     # what has cleared step 1


def test_can_enter_now_shrinks_by_what_is_already_recorded_here():
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    acts = [_act("SO1", "A", "CNC FIRST SIDE", 166),
            _act("SO1", "A", "VMC FIRST SIDE", 100)]
    s = _steps(orderbook.entry_progress(active, acts, _masters(_R)), "SO1", "A")
    assert s["VMC FIRST SIDE"]["can_enter_now"] == 66      # 166 cleared - 100 done here


def test_completed_and_unrouted_orders_are_absent():
    active = {
        ("SO1", "A"): _order("SO1", "A", 400, completed=True),
        ("SO2", "ZZ"): _order("SO2", "ZZ", 10),            # no routing for ZZ
    }
    assert orderbook.entry_progress(active, [], _masters(_R)) == {}


def test_two_orders_on_one_item_stay_separate():
    """The owner's case: same item code, two SO lines, punches must not bleed."""
    active = {("SO149", "A"): _order("SO149", "A", 400),
              ("SO150", "A"): _order("SO150", "A", 23)}
    acts = [_act("SO150", "A", "CNC FIRST SIDE", 23)]
    prog = orderbook.entry_progress(active, acts, _masters(_R))
    assert _steps(prog, "SO150", "A")["CNC FIRST SIDE"]["still_to_make"] == 0
    assert _steps(prog, "SO149", "A")["CNC FIRST SIDE"]["still_to_make"] == 400


def test_can_enter_now_agrees_with_the_save_guard():
    """THE load-bearing test. Every number the panel offers as enterable must be
    accepted by the guard that runs on Save, and one piece more must be refused.
    If these two ever drift apart the form starts inviting punches the server
    rejects, which is the class of bug this feature exists to remove."""
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    acts = [_act("SO1", "A", "CNC FIRST SIDE", 166),
            _act("SO1", "A", "VMC FIRST SIDE", 100)]
    prog = orderbook.entry_progress(active, acts, _masters(_R))
    for step in prog[orderbook.entry_key("SO1", "A")]["steps"]:
        allowed = step["can_enter_now"]
        ok = acts + [_act("SO1", "A", step["process"], allowed)]
        assert orderbook.precedence_cap_error(
            ok, "SO1", "A", step["process"], _R, 400) is None, \
            f"panel offered {allowed} at {step['process']} but Save refuses it"
        over = acts + [_act("SO1", "A", step["process"], allowed + 1)]
        assert orderbook.precedence_cap_error(
            over, "SO1", "A", step["process"], _R, 400) is not None, \
            f"one more than {allowed} at {step['process']} should be refused"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3.12 -m pytest tests/test_daily_entry_guard.py -v`
Expected: every test FAILS with `AttributeError: module 'engine.orderbook' has no attribute 'entry_key'`.

- [ ] **Step 3: Write the implementation**

Append to the **end** of `engine/orderbook.py`. Do not edit anything above it.

```python
def entry_key(so_no: str, item_code: str) -> str:
    """The (SO number, item code) pair as one JSON-safe string, so the per-order
    progress below can travel to the browser as a plain object key. Same composite
    shape ``book_store`` uses for its hashes."""
    return f"{so_no}\x1f{item_code}"


def entry_progress(active_orders: dict, actuals, masters=None) -> dict:
    """Per open (SO, item): what each routing step has done and still owes — the
    numbers the Daily Entry form shows BEFORE a punch, so the operator never has to
    leave the tab and cross-reference the Orders tab by hand (2026-08-28 spec).

    Two quantities per step, deliberately different:

    * ``still_to_make`` = ordered - good recorded at THIS step. What the order owes here.
    * ``can_enter_now`` = upstream cap - pieces already recorded at this step, where the
      cap is the good qty that cleared the PREVIOUS step (or the ordered qty at the
      first step). This is exactly what ``precedence_cap_error`` enforces on Save.

    Built on ``_process_totals`` — the same accounting ``precedence_cap_error`` and
    ``active_so_lines`` use — so what this offers as enterable is by construction what
    the server accepts. Pure; reporting only; the scheduler never reads it.

    Orders that are completed, or whose item has no routing, are omitted (there is no
    step list to show)."""
    routings = masters.routings if masters else {}
    out = {}
    for o in active_orders.values():
        routing = routings.get(o.item_code)
        if o.completed or routing is None:
            continue
        produced, good = _process_totals(actuals, o.so_no, o.item_code)
        procs = sorted(routing.processes, key=lambda p: p.seq)
        steps = []
        for i, p in enumerate(procs):
            n = _norm(p.name)
            done_here = good.get(n, 0.0)
            cap = o.ordered_qty if i == 0 else good.get(_norm(procs[i - 1].name), 0.0)
            steps.append({
                "seq": p.seq,
                "process": p.name,
                "done": done_here,
                "still_to_make": max(o.ordered_qty - done_here, 0.0),
                "can_enter_now": max(cap - produced.get(n, 0.0), 0.0),
            })
        out[entry_key(o.so_no, o.item_code)] = {
            "so_no": o.so_no,
            "item_code": o.item_code,
            "item_name": o.item_name,
            "ordered": o.ordered_qty,
            "delivery_date": o.delivery_date.isoformat(),
            "steps": steps,
        }
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3.12 -m pytest tests/test_daily_entry_guard.py -v`
Expected: 7 passed.

- [ ] **Step 5: Run the whole suite — nothing else may move**

Run: `python3.12 -m pytest -q`
Expected: all pass, the previous count plus 7. If any pre-existing test fails, **stop and ask the owner** — adding a function cannot legitimately break one.

- [ ] **Step 6: Commit**

```bash
git add engine/orderbook.py tests/test_daily_entry_guard.py
git commit -m "feat(entry): entry_progress - what each step has done and still owes

Pure, reporting-only, built on _process_totals so what the Daily Entry form
offers as enterable is by construction what precedence_cap_error accepts on
Save. Two quantities per step: still_to_make (what the order owes here) and
can_enter_now (capped by what cleared the step before it).

Nothing reads it yet."
```

---

### Task 2: serve it on `GET /items`

**Files:**
- Modify: `api/main.py` — the `items()` function only (currently at `api/main.py:3078`)
- Test: `tests/test_daily_entry_guard.py` (append)

**Interfaces:**
- Consumes: `orderbook.entry_progress`, `orderbook.entry_key` from Task 1.
- Produces: `GET /items` gains two keys, read by Tasks 3 and 4:
  - `progress` — the `entry_progress` dict
  - `item_to_sos` — `{item_code: [so_no, ...]}`, open lines only, sorted by SO number

- [ ] **Step 1: Write the failing test**

Append to `tests/test_daily_entry_guard.py`:

```python
# --------------------------------------------------------------------------- #
# GET /items carries the progress. It is the right home: it already feeds this
# form, it is a LIVE store read (so the numbers refresh after every Save), and it
# is role-open (the floor logs in as `user`). Deliberately NOT on POST /run —
# that response is cached by _plan_fingerprint and live punch data inside it is
# the 2026-08-08 stale-cache bug again.
# --------------------------------------------------------------------------- #
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient          # noqa: E402

from api.main import app                            # noqa: E402
from api import auth                                # noqa: E402
from engine import book_store                       # noqa: E402
from tests.sample_workbook import build_sample_bytes, ITEM_A   # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SAMPLE = build_sample_bytes()
_ACCTS = auth._accounts()
_ADMIN = next(u for u, a in _ACCTS.items() if a["role"] == auth.ADMIN)
_ADMIN_PWD = _ACCTS[_ADMIN]["password"]


@pytest.fixture
def client():
    c = TestClient(app)
    assert c.post("/login", data={"username": _ADMIN, "password": _ADMIN_PWD}).status_code == 200
    c.post("/upload", files={"file": ("sample.xlsx", _SAMPLE, XLSX_MIME)})
    return c


def test_items_carries_progress_for_each_open_order(client):
    data = client.get("/items").json()
    assert "progress" in data and "item_to_sos" in data
    entry = next(iter(data["progress"].values()))
    assert {"so_no", "item_code", "ordered", "delivery_date", "steps"} <= set(entry)
    step = entry["steps"][0]
    assert {"seq", "process", "done", "still_to_make", "can_enter_now"} <= set(step)


def test_item_to_sos_lists_both_orders_sharing_an_item_code(client):
    """The clubbed case the whole feature exists for."""
    book_store.add_orders([_order("SO-CLUB", ITEM_A, 23)])
    data = client.get("/items").json()
    sos = data["item_to_sos"][ITEM_A]
    assert "SO-CLUB" in sos and len(sos) >= 2
    assert orderbook.entry_key("SO-CLUB", ITEM_A) in data["progress"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3.12 -m pytest tests/test_daily_entry_guard.py -k items_carries -v`
Expected: FAIL with `assert 'progress' in data`.

- [ ] **Step 3: Modify only the `items()` return**

In `api/main.py`, inside `items()` **only**. It already builds `so_to_items` from `active`; add the inverse map beside it, then two keys on the returned dict. Change nothing else in the file.

Add immediately after the existing `so_to_items` sort loop:

```python
    # Inverse of so_to_items: which open SO lines carry each item code. Two SO lines
    # on one item are clubbed into one batch by Rule 1, so the floor sees one pile of
    # identical parts — the Daily Entry form uses this to show the comparison the
    # operator would otherwise make by hand on the Orders tab (2026-08-28 spec).
    item_to_sos = defaultdict(list)
    for o in active.values():
        item_to_sos[o.item_code].append(o.so_no)
    for code in item_to_sos:
        item_to_sos[code].sort()
```

and add these two keys to the returned dict, after `"open_so_nos"`:

```python
        # What each routing step has done and still owes, per open (SO, item) — shown
        # under the Process dropdown before the punch. Live read, never cached.
        "progress": orderbook.entry_progress(active, book_store.load_actuals(), masters),
        "item_to_sos": dict(item_to_sos),          # {item_code: [so_no, ...]}
```

- [ ] **Step 4: Run to verify it passes**

Run: `python3.12 -m pytest tests/test_daily_entry_guard.py -v`
Expected: 9 passed.

- [ ] **Step 5: Confirm the plan cache is untouched**

Run: `python3.12 -m pytest tests/test_plan_cache_freshness.py tests/test_plan_cache.py tests/test_plan_consistency.py -q`
Expected: all pass. These pin that `/run`'s cached response is keyed correctly; this task must not have gone near it.

- [ ] **Step 6: Run the whole suite and commit**

```bash
python3.12 -m pytest -q
git add api/main.py tests/test_daily_entry_guard.py
git commit -m "feat(entry): GET /items carries per-step progress and the clubbed-order map

/items already feeds the Daily Entry form, is a live store read (so the numbers
refresh after every Save) and is role-open, which the floor's user login needs.
Deliberately not on POST /run: that response is cached by _plan_fingerprint and
live punch data inside it is the 2026-08-08 stale-cache bug again.

Only items() changed in api/main.py."
```

---

### Task 3: the outstanding block and the clubbed-order table

**Files:**
- Modify: `web/app.js` — `actualsFormHtml` and `wireActualsForm` only, plus new functions added beside them
- Modify: `web/style.css` — append at end of file only

**Interfaces:**
- Consumes: `ITEMS.progress` and `ITEMS.item_to_sos` from Task 2; the existing `$()`, `escapeHtml()`, `isoToDdmmyyyy()`, `fillItemFromSO()`, `fillItemMeta()` helpers already in `app.js`.
- Produces, used by Task 4:
  - `jsEntryKey(soNo, itemCode) -> string` — must mirror `orderbook.entry_key` exactly
  - `progressEntry(soNo, itemCode) -> object|null`
  - `stepOf(entry, processName) -> object|null`
  - `sosForItem(itemCode) -> array of entry objects`, soonest delivery first
  - `qty(n) -> string`
  - `renderOutstanding()` — re-renders the block from the current form values

- [ ] **Step 1: Add the mount point to the form**

In `actualsFormHtml`, the `left` column currently ends with the Process field. Change that final line from:

```js
    fr("Process <span class=auto>(dropdown)</span>", `<select id="a-process"></select>`);
```

to:

```js
    fr("Process <span class=auto>(dropdown)</span>", `<select id="a-process"></select>`) +
    `<div id="a-outstanding" class="a-outstanding"></div>`;
```

- [ ] **Step 2: Add the render functions**

Insert immediately **after** the existing `fillItemMeta` function in `web/app.js`:

```js
// ---- What this step still owes (2026-08-28 spec) --------------------------
// Two SO lines on the same item code are clubbed into one batch, so the floor sees
// one pile of identical parts. The operator used to leave this tab and work out on
// the Orders tab which SO a counted quantity belonged to; that lookup is slow and
// gets skipped when he is rushing. These functions put the answer on the screen he
// is already on.

// MUST mirror engine/orderbook.entry_key exactly: same separator, same order.
// The separator is written as the \u001f ESCAPE, never as a literal invisible
// character, so it survives copy-paste and stays visible in review.
function jsEntryKey(soNo, itemCode) { return soNo + "\u001f" + itemCode; }

function progressEntry(soNo, itemCode) {
  const p = ITEMS && ITEMS.progress ? ITEMS.progress[jsEntryKey(soNo, itemCode)] : null;
  return p || null;
}

function stepOf(entry, processName) {
  if (!entry || !entry.steps) return null;
  return entry.steps.find((s) => s.process === processName) || null;
}

// Every open SO carrying this item code, soonest delivery first. One entry means
// there is nothing to confuse and the comparison table is not drawn at all.
function sosForItem(itemCode) {
  const list = (ITEMS && ITEMS.item_to_sos && ITEMS.item_to_sos[itemCode]) || [];
  return list
    .map((so) => progressEntry(so, itemCode))
    .filter(Boolean)
    .sort((a, b) => (a.delivery_date < b.delivery_date ? -1
                   : a.delivery_date > b.delivery_date ? 1
                   : a.so_no < b.so_no ? -1 : 1));
}

function qty(n) { return Number(n).toLocaleString("en-IN"); }

function renderOutstanding() {
  const mount = $("a-outstanding");
  if (!mount) return;
  const so = $("a-so").value.trim();
  const item = $("a-item").value.trim();
  const proc = $("a-process").value;
  const entry = so && item ? progressEntry(so, item) : null;
  const step = stepOf(entry, proc);
  if (!entry || !step) { mount.innerHTML = ""; return; }

  const others = sosForItem(item);
  let club = "";
  if (others.length > 1) {
    const rows = others.map((e) => {
      const s = stepOf(e, proc);
      const picked = e.so_no === so;
      const cells = `<td>${escapeHtml(e.so_no)}</td>`
        + `<td>${escapeHtml(isoToDdmmyyyy(e.delivery_date))}</td>`
        + `<td class="num">${qty(e.ordered)}</td>`
        + `<td class="num">${s ? qty(s.done) : "-"}</td>`
        + `<td class="num">${s ? qty(s.still_to_make) : "-"}</td>`;
      return picked
        ? `<tr class="ao-picked">${cells}<td>you picked this</td></tr>`
        : `<tr class="ao-other" data-so="${escapeHtml(e.so_no)}" title="Switch to this order">`
          + `${cells}<td class="ao-switch">use this one</td></tr>`;
    }).join("");
    club = `<div class="ao-club">
        <div class="ao-warn">This part is on ${others.length} open orders. Check you have the right one.</div>
        <table><thead><tr><th>SO No</th><th>Due</th><th class="num">Ordered</th>
          <th class="num">Done</th><th class="num">Still to make</th><th></th></tr></thead>
          <tbody>${rows}</tbody></table>
      </div>`;
  }

  // "Still to make" is what the order owes at this step. "You can enter up to" is
  // capped by what cleared the step before it, the rule the server already enforces
  // on Save. When they are the same number the second line says nothing, so it is
  // hidden; it appears only when it is telling the operator something.
  const capLine = step.can_enter_now < step.still_to_make
    ? `<div class="ao-cap">You can enter up to <b>${qty(step.can_enter_now)}</b> today`
      + `<span class="ao-why">only ${qty(step.can_enter_now + step.done)} pieces have cleared the step before this one</span></div>`
    : "";

  mount.innerHTML = club + `
    <div class="ao-block">
      <div class="ao-head">${escapeHtml(entry.so_no)} &middot; ${escapeHtml(entry.item_code)}
        &middot; ${escapeHtml(step.process)}
        <span class="ao-due">due ${escapeHtml(isoToDdmmyyyy(entry.delivery_date))}</span></div>
      <div class="ao-line"><span>Ordered</span><b>${qty(entry.ordered)}</b></div>
      <div class="ao-line"><span>Done at this step</span><b>${qty(step.done)}</b></div>
      <div class="ao-line ao-main"><span>Still to make</span><b>${qty(step.still_to_make)}</b></div>
      ${capLine}
    </div>`;

  // Clicking another order's row switches the form to it: the whole Orders-tab trip
  // in one click.
  mount.querySelectorAll("tr.ao-other").forEach((tr) => {
    tr.onclick = () => {
      $("a-so").value = tr.getAttribute("data-so");
      fillItemFromSO();
      if ($("a-item").value !== item) { $("a-item").value = item; fillItemMeta(); }
      $("a-process").value = proc;
      renderOutstanding();
    };
  });
}
```

- [ ] **Step 3: Call it whenever the pickers change**

In `wireActualsForm`, the three existing listener lines read:

```js
  $("a-so").addEventListener("change", fillItemFromSO);   // step 1: pick SO No -> fill Item dropdown
  $("a-item").addEventListener("change", fillItemMeta);   // step 2: pick Item -> name + processes
  fillItemMeta();
```

Replace them with:

```js
  $("a-so").addEventListener("change", () => { fillItemFromSO(); renderOutstanding(); });
  $("a-item").addEventListener("change", () => { fillItemMeta(); renderOutstanding(); });
  $("a-process").addEventListener("change", renderOutstanding);
  fillItemMeta();
  renderOutstanding();
```

- [ ] **Step 4: Append the styles**

Append to the **end** of `web/style.css`. Edit no existing rule:

```css
/* ---------------- Daily Entry: what this step still owes (2026-08-28) ---------- */
/* Shown under the Process dropdown so the operator never has to leave the tab and
   cross-reference the Orders tab by hand to find out what a step still needs. */
.a-outstanding { margin: 6px 0 14px; }
.ao-block { border: 1px solid var(--border); border-radius: 8px; padding: 10px 12px; background: rgba(255,255,255,.02); }
.ao-head { font-weight: 600; margin-bottom: 8px; }
.ao-due { float: right; font-weight: 400; color: var(--muted); }
.ao-line { display: flex; justify-content: space-between; padding: 2px 0; }
.ao-line span { color: var(--muted); }
.ao-main { border-top: 1px solid var(--border); margin-top: 4px; padding-top: 6px; font-size: 15px; }
.ao-main b { color: var(--accent); }
.ao-cap { margin-top: 8px; padding-top: 6px; border-top: 1px dashed var(--border); }
.ao-why { display: block; color: var(--muted); font-size: 12px; margin-top: 2px; }
.ao-club { margin-bottom: 10px; border: 1px solid var(--danger); border-radius: 8px; padding: 10px 12px; }
.ao-warn { font-weight: 600; color: var(--danger); margin-bottom: 8px; }
.ao-club table { width: 100%; border-collapse: collapse; }
.ao-club th, .ao-club td { padding: 4px 8px; text-align: left; border-bottom: 1px solid var(--border); }
.ao-club th.num, .ao-club td.num { text-align: right; }
.ao-picked { font-weight: 600; }
.ao-other { cursor: pointer; }
.ao-other:hover { background: rgba(255,255,255,.06); }
.ao-switch { color: var(--accent); }
```

- [ ] **Step 5: Verify by hand in the browser**

```bash
python3.12 -m uvicorn api.main:app --port 8123
```

Log in, upload the sample, open Daily Entry, then confirm:
1. Picking an SO + item + process shows the Ordered / Done at this step / Still to make block.
2. On a first routing step with no punches, the "You can enter up to" line is **absent** (the cap equals still_to_make, so it would say nothing).
3. Punch some pieces at step 1, then select step 2: the "You can enter up to" line now **appears** with the punched number.
4. No console errors.

If the sample book has no two SOs on one item code, the clubbed table cannot be exercised here; Task 4 covers the logic and Task 5 Step 5 covers it on the owner's real workbook.

- [ ] **Step 6: Commit**

Name both files explicitly. Do **not** use `git add -A`, and do not stage `web/style.css` wholesale — that file also carries an unrelated uncommitted 18-08 change that must stay in the working tree. Stage only the appended block:

```bash
git add web/app.js
git add -p web/style.css      # accept ONLY the appended .a-outstanding block; skip the .view hunk
git diff --cached --stat
git commit -m "feat(entry): show what the picked step still owes, and the other orders on that part

Under the Process dropdown: Ordered, Done at this step, Still to make. The
separate you-can-enter-up-to line appears only when the step before this one caps
it lower, so it never states the obvious.

When the item sits on more than one open order, the comparison the operator used
to make by hand on the Orders tab is drawn above it, soonest delivery first, and
each other row is clickable to switch the form to that order."
```

After committing, confirm the 18-08 change is still present and unstaged:

```bash
git diff web/style.css | grep -c "full window width" && echo "18-08 change preserved"
```

---

### Task 4: the Save-time quantity cross-check

**Files:**
- Modify: `web/app.js` — the `a-save` handler inside `wireActualsForm`, plus two new functions
- Test: `tests/test_daily_entry_guard.py` (append)

`web/style.css` needs nothing new here — the dialog reuses the existing `.modal-overlay` / `.modal` / `.modal-actions` rules.

**Interfaces:**
- Consumes: `jsEntryKey`, `progressEntry`, `stepOf`, `sosForItem`, `qty` from Task 3; the existing `.modal-overlay` markup used by `deleteWithPassword`.
- Produces: `quantityFitsAnotherSo(typed, pickedSo, itemCode, processName) -> null | {picked, pickedStep, matches}` and `confirmWrongSo(check, typed, processName) -> Promise<"switch"|"save"|"cancel">`.

- [ ] **Step 1: Write the tests for the decision rule**

The dialog is browser-only, but *when it fires* is arithmetic over `/items` data, and that is what must not drift. Append to `tests/test_daily_entry_guard.py` a Python mirror of the rule, driven by real `entry_progress` output:

```python
# --------------------------------------------------------------------------- #
# The cross-check rule. web/app.js implements exactly this in
# quantityFitsAnotherSo(); this pins the arithmetic against real entry_progress
# output so the two cannot drift. It is the owner's actual case: 26-27SO149 x 400
# and 26-27SO150 x 23 on one item code, 23 typed against SO149.
# --------------------------------------------------------------------------- #
def _fits_another_so(progress, item_to_sos, typed, picked_so, item, process):
    """Mirror of quantityFitsAnotherSo() in web/app.js. Returns the other open SOs
    whose remaining at this step is exactly the typed quantity."""
    if typed <= 0:
        return []
    sos = item_to_sos.get(item, [])
    if len(sos) < 2:
        return []
    steps = {s["process"]: s
             for s in progress[orderbook.entry_key(picked_so, item)]["steps"]}
    if typed == steps[process]["still_to_make"]:
        return []                      # fits the order he picked, stay silent
    out = []
    for so in sos:
        if so == picked_so:
            continue
        other = progress.get(orderbook.entry_key(so, item))
        if not other:
            continue
        st = next((s for s in other["steps"] if s["process"] == process), None)
        if st and typed == st["still_to_make"]:
            out.append(so)
    return out


_CLUB_ACTIVE = {("SO149", "A"): _order("SO149", "A", 400, date(2025, 9, 20)),
                ("SO150", "A"): _order("SO150", "A", 23, date(2025, 9, 12))}
_CLUB_MAP = {"A": ["SO149", "SO150"]}
_STEP = "CNC FIRST SIDE"


def _club_progress(acts=()):
    return orderbook.entry_progress(_CLUB_ACTIVE, list(acts), _masters(_R))


def test_cross_check_fires_on_the_owners_actual_mistake():
    """23 typed against SO149, which still needs 400. 23 is exactly SO150's whole
    order. This is the entry that told the directors the opposite of the truth."""
    assert _fits_another_so(_club_progress(), _CLUB_MAP, 23, "SO149", "A", _STEP) == ["SO150"]


def test_cross_check_is_silent_when_the_right_order_is_picked():
    assert _fits_another_so(_club_progress(), _CLUB_MAP, 23, "SO150", "A", _STEP) == []


def test_cross_check_is_silent_on_a_partial_punch():
    """20 of SO150's 23 matches nothing exactly. Warning on a normal partial entry
    would train the floor to click straight through the warnings that matter."""
    assert _fits_another_so(_club_progress(), _CLUB_MAP, 20, "SO149", "A", _STEP) == []
    assert _fits_another_so(_club_progress(), _CLUB_MAP, 20, "SO150", "A", _STEP) == []


def test_cross_check_is_silent_when_the_part_is_on_one_order_only():
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    prog = orderbook.entry_progress(active, [], _masters(_R))
    assert _fits_another_so(prog, {"A": ["SO1"]}, 400, "SO1", "A", _STEP) == []


def test_cross_check_goes_quiet_once_so150_is_punched():
    """Once SO150's 23 are recorded its remaining is 0, so a later 23 against SO149
    no longer matches it and the warning correctly stops firing."""
    acts = [_act("SO150", "A", _STEP, 23)]
    assert _fits_another_so(_club_progress(acts), _CLUB_MAP, 23, "SO149", "A", _STEP) == []
```

- [ ] **Step 2: Run them**

Run: `python3.12 -m pytest tests/test_daily_entry_guard.py -k cross_check -v`
Expected: 5 passed.

**Be honest about what this proves.** `_fits_another_so` is a test-side mirror, so these pass as soon as they are written — they are not a RED-first test of shipped code. What they lock down is the *arithmetic*, against real `entry_progress` output, so the rule cannot drift silently. The shipped JS is verified by hand in Step 5 and mutated directly in Step 6. **Do not skip either.**

- [ ] **Step 3: Add the cross-check and the dialog to `web/app.js`**

Insert immediately **after** `renderOutstanding` from Task 3:

```js
// Returns null when there is nothing to say, else {picked, pickedStep, matches}.
// Fires only when the typed quantity does NOT fit the picked order but EXACTLY
// finishes another open order for the same part (the owner's 2026-08-28 case). A
// partial punch matches nothing and stays silent on purpose: warning on a perfectly
// normal entry would train the floor to click through the warnings that matter.
function quantityFitsAnotherSo(typed, pickedSo, itemCode, processName) {
  if (!(typed > 0)) return null;
  const others = sosForItem(itemCode);
  if (others.length < 2) return null;
  const pickedEntry = progressEntry(pickedSo, itemCode);
  const pickedStep = stepOf(pickedEntry, processName);
  if (!pickedStep) return null;
  if (typed === pickedStep.still_to_make) return null;   // fits the one he picked
  const matches = others.filter((e) => {
    if (e.so_no === pickedSo) return false;
    const s = stepOf(e, processName);
    return s && typed === s.still_to_make;
  });
  if (!matches.length) return null;
  return { picked: pickedEntry, pickedStep: pickedStep, matches: matches };
}

// Three-way confirm. Resolves "switch" (re-point to the single matching order and
// save), "save" (save exactly as typed) or "cancel". With several matches there is
// no single right answer, so no switch button is offered.
function confirmWrongSo(check, typed, processName) {
  return new Promise((resolve) => {
    const one = check.matches.length === 1 ? check.matches[0] : null;
    const names = check.matches.map((m) => m.so_no).join(", ");
    const ov = document.createElement("div");
    ov.className = "modal-overlay";
    ov.innerHTML = `
      <div class="modal">
        <h3>Check this before saving</h3>
        <p>You entered <b>${qty(typed)}</b> pieces against
           <b>${escapeHtml(check.picked.so_no)}</b>, which still needs
           <b>${qty(check.pickedStep.still_to_make)}</b> at
           ${escapeHtml(processName)}.</p>
        <p>${qty(typed)} is exactly what <b>${escapeHtml(names)}</b> still
           needs${one ? " (due " + escapeHtml(isoToDdmmyyyy(one.delivery_date)) + ")" : ""}.</p>
        <div class="modal-actions">
          <button id="ws-cancel">Cancel</button>
          <button id="ws-save">Save as ${escapeHtml(check.picked.so_no)}</button>
          ${one ? `<button id="ws-switch" class="primary">Switch to ${escapeHtml(one.so_no)} and save</button>` : ""}
        </div>
      </div>`;
    document.body.appendChild(ov);
    const done = (v) => { ov.remove(); resolve(v); };
    ov.querySelector("#ws-cancel").onclick = () => done("cancel");
    ov.querySelector("#ws-save").onclick = () => done("save");
    if (one) ov.querySelector("#ws-switch").onclick = () => done("switch");
  });
}
```

- [ ] **Step 4: Wire it into the Save handler**

In the `a-save` handler in `wireActualsForm`, find the existing duplicate guard:

```js
    // Guard against re-saving the exact same entry (the #1 cause of duplicates).
    if (actualIsDuplicate(body) && !confirm(
```

Insert **immediately before** that comment:

```js
    // Wrong-SO guard (2026-08-28): the typed quantity does not fit the picked order
    // but exactly finishes another open order for the same part. Never blocks, since
    // the operator is the one who can see the parts. "Switch and save" re-points the
    // entry in one action rather than leaving a half-switched form on screen, which
    // would be its own new way to get this wrong.
    const _wrongSo = quantityFitsAnotherSo(
      body.qty_produced, body.so_no, body.item_code, body.process);
    if (_wrongSo) {
      const choice = await confirmWrongSo(_wrongSo, body.qty_produced, body.process);
      if (choice === "cancel") return;
      if (choice === "switch") {
        body.so_no = _wrongSo.matches[0].so_no;
        $("a-so").value = body.so_no;      // keep the form honest about what was saved
      }
    }
```

- [ ] **Step 5: Verify the dialog by hand**

Restart the app and create two open orders on one item code (upload the sample, then add a second SO line for the same item). In Daily Entry:

1. Pick the larger SO, type the smaller SO's exact remaining, click Save. The dialog must appear naming the other order.
2. Click **Switch and save**. The entry must land on the **other** SO — confirm in the saved-entries list below the form, and confirm the SO dropdown now shows that order.
3. Pick the smaller SO and type its exact remaining: **no dialog**, saves straight through.
4. Type a partial quantity: **no dialog**.
5. Click **Cancel** on the dialog: nothing is saved, and the form keeps what was typed.
6. No console errors in any of the five.

- [ ] **Step 6: Mutation-check the guard**

Fixtures in this repo have a documented habit of passing vacuously (CLAUDE.md, 2026-08-09), so prove each clause is load-bearing. Revert each **individually** in `_fits_another_so`, run `python3.12 -m pytest tests/test_daily_entry_guard.py -k cross_check -q`, confirm at least one test FAILS, then restore it:

| Mutation | Test that must fail |
|---|---|
| Delete the `typed == still_to_make` early return | `test_cross_check_is_silent_when_the_right_order_is_picked` |
| Delete the `len(sos) < 2` early return | `test_cross_check_is_silent_when_the_part_is_on_one_order_only` |
| Change the match from `==` to `<=` | `test_cross_check_is_silent_on_a_partial_punch` |
| Drop the `so == picked_so` skip | `test_cross_check_is_silent_when_the_right_order_is_picked` |

If any mutation leaves every test passing, that clause is untested: **write the test that catches it before moving on.** Then apply the same four mutations to the JS `quantityFitsAnotherSo` and re-run the Step 5 browser checks to confirm each one visibly breaks the dialog.

- [ ] **Step 7: Commit**

```bash
python3.12 -m pytest -q
git add web/app.js tests/test_daily_entry_guard.py
git commit -m "feat(entry): warn when the quantity typed finishes a different order

Fires only when the typed qty does not fit the picked order but exactly finishes
another open order for the same part. Offers switch-and-save, save-as-typed, or
cancel; with several matches there is no single right answer so no switch button
is offered. Never blocks: the operator is the one who can see the parts.

A partial punch matches nothing and stays silent on purpose, since warning on a
normal entry would train the floor to click past the warnings that matter.

Mutation-checked: each clause of the rule, reverted on its own, fails a test."
```

---

### Task 5: prove the scope held, and document it

**Files:**
- Modify: `CLAUDE.md` (append one bullet under the current-state banner)

**Interfaces:**
- Consumes: everything from Tasks 1-4. Produces: nothing code reads.

- [ ] **Step 1: Full suite, no rebased assertions**

```bash
python3.12 -m pytest -q
git status --short
```

Expected: green, at the previous total plus the new file's tests. **No existing test file may appear in `git status`.** If one does, the work leaked out of scope — revert it and ask the owner.

- [ ] **Step 2: Prove no plan moved**

```bash
python3.12 -m pytest -k golden -q
python3.12 -m pytest tests/test_plan_consistency.py tests/test_cross_tab_consistency.py -q
```

Expected: green with **no** `REGEN_GOLDEN`. The golden trace is a byte-level snapshot of a full plan; if this display-only feature had moved a scheduled date, this is what catches it.

- [ ] **Step 3: Prove the scope contract held**

Let `BASE` be the commit before Task 1 (find it with `git log --oneline`).

```bash
git diff --stat $BASE..HEAD | cat
```

The changed-file list must contain **only**: `engine/orderbook.py`, `api/main.py`, `web/app.js`, `web/style.css`, `tests/test_daily_entry_guard.py`, `CLAUDE.md`, and the plan/spec docs. Any other path is a scope breach and gets reverted.

```bash
git diff --name-only $BASE..HEAD \
  | grep -E '^(ppc_engine/|engine/rules/|engine/(new_engine|freeze|optimizer|flow_scheduler|pipeline|analytics|models|config|loaders|book_store)\.py)' \
  && echo "SCOPE BREACH" || echo "scope clean"
```

Expected: `scope clean`.

```bash
git diff $BASE..HEAD -- api/main.py | cat
```

Expected: two hunks, both inside `items()` — one adding `item_to_sos`, one adding the two return keys. Nothing else.

```bash
git diff $BASE..HEAD -- web/style.css | cat
git status --short
```

Expected: the committed CSS diff is **append-only** (no removed lines), and `git status --short` still shows `M web/style.css` for the untouched 18-08 working-tree change.

- [ ] **Step 4: Append the CLAUDE.md bullet**

Add under the current-state banner, above the 2026-08-11 clubbed-batch bullet:

```markdown
> - **THE DAILY ENTRY FORM NOW SHOWS WHAT A STEP STILL OWES, BEFORE THE PUNCH
>   (2026-08-28, owner report).** `26-27SO149` (400) and `26-27SO150` (23) share an
>   item code, so Rule 1 clubs them and the floor sees one pile of identical parts.
>   SO150's 23 finished; the 23 were punched against **SO149**. The book then told
>   the directors the opposite of the truth — SO149 377 outstanding when none were
>   made, SO150 open forever when it was done. **The PLAN was never wrong** (the batch
>   owes the same total either way); the BOOKS were, and the books are what the
>   directors read. The operator was not careless: attribution is knowable, but only
>   by leaving Daily Entry, opening the **Orders tab**, and working out by hand which
>   SO a counted quantity belongs to — slow, and skipped when he is rushing.
>   **Fix, display only:** under the Process dropdown, `Ordered / Done at this step /
>   Still to make`, plus a separate **"you can enter up to"** line that appears only
>   when the previous step caps it lower (the rule `precedence_cap_error` already
>   enforces on Save, turned from a red error after the click into a number before
>   it). When the item sits on **2+ open SO lines** the comparison he used to make by
>   hand is drawn above it, soonest delivery first, each other row clickable to
>   switch. On Save, a three-way confirm fires when the typed qty does **not** fit the
>   picked order but **exactly** finishes another one — switch-and-save, save-as-typed,
>   or cancel. Never blocks; a partial punch stays silent on purpose (warning on a
>   normal entry trains the floor to click past the warnings that matter).
>   **The one load-bearing decision:** `orderbook.entry_progress` is built on
>   **`_process_totals`**, the same internal `precedence_cap_error` and
>   `active_so_lines` count with, so what the panel offers as enterable is by
>   construction what the server accepts — pinned by
>   `test_can_enter_now_agrees_with_the_save_guard`. Served on **`GET /items`** (live
>   read, already fetched per render, role-open); deliberately **NOT** on `/run`,
>   whose response is cached by `_plan_fingerprint`. Mutation-checked: each clause of
>   the cross-check, reverted on its own, fails a test. **Not one scheduled date
>   moves** — golden trace unchanged. Regression: `tests/test_daily_entry_guard.py`.
>   **Deliberately NOT built (owner's call, prevention first):** no way to move a past
>   mis-punch to the right SO. Undo is still latest-day only, so the 23 already on
>   SO149 need a direct database correction.
>   **Rule: when the software makes an operator leave the screen to look something
>   up, that lookup is the bug. Put the answer where he already is.**
```

- [ ] **Step 5: Verify on the owner's real workbook**

Do **not** test on the live site. Per `parallel-verification-method-2026-08-04`, run a throwaway local instance on the real book:

```bash
python3.12 -m uvicorn api.main:app --port 8124
```

Upload `Test9.xlsx`, find a real item code carried by two open SO lines, and confirm in the browser:
1. The clubbed table lists both, with their real quantities and due dates, soonest first.
2. The outstanding block's numbers match what the Orders tab reports for the same order.
3. Clicking the other row switches the form to it.
4. The cross-check fires on a quantity that exactly finishes the other order.
5. The plan's expected completion dates are unchanged versus the same commit without this feature (the golden test already proves this; this is the real-book confirmation).

Report what was checked and on which book. If Test9 has no clubbed pair, say so rather than claiming the check passed.

- [ ] **Step 6: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: record the Daily Entry outstanding-qty guard

Includes what was deliberately not built (no way to move a past mis-punch; the 23
already on SO149 still need a direct database correction) and the rule the bug
taught: when the software makes an operator leave the screen to look something up,
that lookup is the bug."
```

- [ ] **Step 7: Stop. Do not push.**

Pushing to `main` auto-deploys to the live site (Render auto-deploy is ON). The owner decides when this reaches the floor. Report what shipped, what was measured, and wait.

---

## Self-Review

**Spec coverage.** Every section of the spec maps to a task: the outstanding block and its two distinct quantities → Task 3; the clubbed-order table → Task 3; the Save-time cross-check with its three buttons and its several-matches rule → Task 4; the `entry_progress` / `_process_totals` reuse → Task 1; `/items` as the carrier and the explicit "not on `/run`" → Task 2; the scope contract and its `git diff --stat` proof → Global Constraints plus Task 5 Step 3; the spec's eight tests → Task 1 (tests 1-6), Task 2 (test 7), Task 4 (test 8); the mutation check → Task 4 Step 6; "not one scheduled date moves" → Task 5 Step 2; the two stated limits (no past-punch repair, silence on partials) → Task 4 Step 1's tests and the CLAUDE.md bullet.

**Placeholders.** None. Every code step carries the actual code; every verification step carries the actual command and its expected output.

**Type consistency.** `entry_key` (`"\x1f"`) and `jsEntryKey` (`"\u001f"`) produce the identical string — both written as escapes, never as a literal invisible character. Step keys (`seq`, `process`, `done`, `still_to_make`, `can_enter_now`) and entry keys (`so_no`, `item_code`, `item_name`, `ordered`, `delivery_date`, `steps`) are defined once in Task 1's Interfaces block and used unchanged in Tasks 2, 3 and 4. `sosForItem` returns entry **objects**, not SO strings — which is what both `quantityFitsAnotherSo` and the clubbed table consume. `qty()` is defined in Task 3 and reused by Task 4's dialog.

**Known wrinkle, stated rather than hidden.** Task 4's Python tests mirror the JS rule rather than executing it — this repo has no JS test runner, and adding one is outside the scope contract. They pin the arithmetic against real `entry_progress` output so the rule cannot drift silently; the wiring is covered by the hands-on checks in Task 4 Step 5 and Task 5 Step 5, and the JS itself is mutated in Task 4 Step 6. Task 4 Step 2 says this plainly instead of pretending the mirror is a RED-first test of shipped code.
