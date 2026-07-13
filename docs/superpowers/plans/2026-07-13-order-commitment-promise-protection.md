# Order Commitment & Promise Protection — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the owner lock a promised delivery date on an order so newly-arriving (unpromised) orders can never push it later, plus an "urgent" path that slots an order in by its delivery date.

**Architecture:** Add a `commitment` lane (`open`/`committed`/`urgent`) + `promised_date` to each order in the persistent order book. Planning becomes **two-pass**: schedule the protected (committed+urgent) orders first as if open orders don't exist, then schedule the open orders into the free machine/operator intervals left over — never overrunning a committed block. One engine change: Rule 6 gains an optional `reserved=` argument.

**Tech Stack:** Python 3 + FastAPI backend, plain-Python engine (pure `run()` rules), vanilla HTML/JS frontend, `pytest`. Durable store via `engine/storage.py` (Mongo/Upstash/local file).

## Global Constraints

- **Every rule is a pure function** `def run(input, config, masters) -> output`; only `pipeline.py`/`api._plan` know order. Never call one rule from another.
- **Order identity is the `(SO No, Item Code)` pair.** All book/store lookups key on it.
- **Defaults must be byte-identical to today.** New order field default `commitment="open"`, `promised_date=None`; Rule 6 `reserved=None`. The golden trace must not change: `python3 -m pytest -k golden` stays green *without* `REGEN_GOLDEN`.
- **TDD**: write the failing test, watch it fail, minimal code, watch it pass, commit. One behaviour per test.
- **Run the full suite** `python3 -m pytest -q` (expect 241 passing at start) after each task; it must stay green.
- **Dates:** store/compare as `datetime.date` internally; display DD-MM-YYYY via existing `fmt_date`.
- **Branch:** all work on `committed-orders`. Do **not** push to `main` (owner says "push to main" explicitly).
- **Admin-only** mutations enforced server-side via `require_admin(request)` (raises 403), mirroring `/orders/delete`.

---

### Task 1: Commitment fields on `Order` and `SOLine`

**Files:**
- Modify: `engine/models.py` (`Order` class ~396–439; `SOLine` class ~42–56)
- Test: `tests/test_models_commitment.py` (create)

**Interfaces:**
- Produces: `Order` gains `commitment: str = "open"`, `promised_date: Optional[date] = None`, `committed_at: Optional[str] = None` (ISO string). `Order.to_json`/`from_json` round-trip them. `SOLine` gains `commitment: str = "open"`, `promised_date: Optional[date] = None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_models_commitment.py
from datetime import date
from engine.models import Order, SOLine


def test_order_defaults_to_open_and_no_promise():
    o = Order(so_no="SO1", item_code="X", item_name="X", ordered_qty=10,
              delivery_date=date(2026, 7, 20))
    assert o.commitment == "open"
    assert o.promised_date is None
    assert o.committed_at is None


def test_order_commitment_round_trips_through_json():
    o = Order(so_no="SO1", item_code="X", item_name="X", ordered_qty=10,
              delivery_date=date(2026, 7, 20), commitment="committed",
              promised_date=date(2026, 7, 22), committed_at="2026-07-13T09:00:00")
    back = Order.from_json(o.to_json())
    assert back.commitment == "committed"
    assert back.promised_date == date(2026, 7, 22)
    assert back.committed_at == "2026-07-13T09:00:00"


def test_soline_defaults_to_open():
    s = SOLine(so_no="SO1", item_code="X", item_name="X", qty=10,
               delivery_date=date(2026, 7, 20))
    assert s.commitment == "open"
    assert s.promised_date is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_models_commitment.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'commitment'`.

- [ ] **Step 3: Add the fields**

In `engine/models.py`, `SOLine` — add after `process_qty` field:
```python
    # Commitment lane carried into planning: "open" (default) | "committed" | "urgent".
    commitment: str = "open"
    promised_date: Optional[date] = None
```

`Order` — add after `first_seen: str = ""`:
```python
    commitment: str = "open"            # "open" | "committed" | "urgent"
    promised_date: Optional[date] = None  # locked promise (None while open)
    committed_at: Optional[str] = None    # ISO datetime string, snapshot time
```

`Order.to_json` — add to the returned dict:
```python
            "commitment": self.commitment,
            "promised_date": self.promised_date.isoformat() if self.promised_date else None,
            "committed_at": self.committed_at,
```

`Order.from_json` — add to the `cls(...)` call:
```python
            commitment=d.get("commitment", "open"),
            promised_date=(date.fromisoformat(d["promised_date"])
                           if d.get("promised_date") else None),
            committed_at=d.get("committed_at"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_models_commitment.py -v` → PASS.
Run: `python3 -m pytest -q` → still 241 passed (defaults unchanged).

- [ ] **Step 5: Commit**

```bash
git add engine/models.py tests/test_models_commitment.py
git commit -m "Order/SOLine gain commitment lane + promised_date (default open, no-op)"
```

---

### Task 2: Persist commit / uncommit in the order book store

**Files:**
- Modify: `engine/book_store.py` (add after `uncomplete_order`, ~104)
- Test: `tests/test_book_store_commitment.py` (create)

**Interfaces:**
- Produces:
  - `set_commitment(so_no, item_code, commitment, promised_date) -> bool` — sets an **active** order's `commitment`/`promised_date`/`committed_at` (stamps `committed_at` via the passed value; see below) and re-persists. `promised_date` is a `date` or `None`. Returns False if the order is unknown.
  - `clear_commitment(so_no, item_code) -> bool` — resets to `open`/`None`. Returns False if unknown.
- Consumes: existing `get_store()`, `load_active_orders()`, `_skey`, `ORDERS_KEY`, `Order.to_json`.

Note on `committed_at`: `Date.now()` is available in normal runtime (this is not a Workflow script). Pass the ISO timestamp in from the API layer to keep `book_store` deterministic and testable. `set_commitment` takes an explicit `committed_at` string.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_book_store_commitment.py
from datetime import date
from engine import book_store
from engine.models import Order


def _seed():
    book_store.delete_all()
    book_store.add_orders([Order(so_no="SO1", item_code="X", item_name="X",
                                 ordered_qty=10, delivery_date=date(2026, 7, 20))])


def test_set_commitment_persists_lane_and_promise():
    _seed()
    ok = book_store.set_commitment("SO1", "X", "committed",
                                   date(2026, 7, 22), "2026-07-13T09:00:00")
    assert ok is True
    o = book_store.load_active_orders()[("SO1", "X")]
    assert o.commitment == "committed"
    assert o.promised_date == date(2026, 7, 22)
    assert o.committed_at == "2026-07-13T09:00:00"


def test_clear_commitment_resets_to_open():
    _seed()
    book_store.set_commitment("SO1", "X", "urgent", date(2026, 7, 25), "2026-07-13T09:00:00")
    assert book_store.clear_commitment("SO1", "X") is True
    o = book_store.load_active_orders()[("SO1", "X")]
    assert o.commitment == "open" and o.promised_date is None


def test_set_commitment_unknown_order_returns_false():
    _seed()
    assert book_store.set_commitment("NOPE", "Y", "committed", None, "t") is False
```

The `conftest.py` `_isolate_store` fixture already points the store at a temp dir per test.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_book_store_commitment.py -v`
Expected: FAIL — `AttributeError: module 'engine.book_store' has no attribute 'set_commitment'`.

- [ ] **Step 3: Implement the two functions**

Add to `engine/book_store.py` after `uncomplete_order`:
```python
def set_commitment(so_no: str, item_code: str, commitment: str,
                   promised_date, committed_at: str) -> bool:
    """Set an ACTIVE order's commitment lane + promised date. `commitment` is
    'committed' or 'urgent'; `promised_date` is a date or None; `committed_at` is an
    ISO datetime string (passed in so this stays deterministic). False if unknown."""
    s = get_store()
    o = load_active_orders().get((so_no, item_code))
    if o is None:
        return False
    o.commitment = commitment
    o.promised_date = promised_date
    o.committed_at = committed_at
    s.hset(ORDERS_KEY, _skey(so_no, item_code), json.dumps(o.to_json()))
    return True


def clear_commitment(so_no: str, item_code: str) -> bool:
    """Reset an active order back to the Open lane (clears promise). False if unknown."""
    s = get_store()
    o = load_active_orders().get((so_no, item_code))
    if o is None:
        return False
    o.commitment = "open"
    o.promised_date = None
    o.committed_at = None
    s.hset(ORDERS_KEY, _skey(so_no, item_code), json.dumps(o.to_json()))
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_book_store_commitment.py -v` → PASS.
Run: `python3 -m pytest -q` → still green.

- [ ] **Step 5: Commit**

```bash
git add engine/book_store.py tests/test_book_store_commitment.py
git commit -m "book_store: set_commitment / clear_commitment (persist lane + promise)"
```

---

### Task 3: Order book — carry lane into planning + split helper + dashboard columns

**Files:**
- Modify: `engine/orderbook.py` (`active_so_lines` ~193–224; `order_rows` ~255–285)
- Test: `tests/test_orderbook_commitment.py` (create)

**Interfaces:**
- Produces:
  - `active_so_lines(...)` now sets `commitment`/`promised_date` on each emitted `SOLine` from its `Order`.
  - `split_committed_open(so_lines) -> (protected, open_lines)` — partitions a list of `SOLine` into `protected` (commitment in {"committed","urgent"}) and `open_lines` (else).
  - `order_rows(...)` gains two columns per row: `"Lane"` (`open`/`committed`/`urgent`) and `"Promised"` (DD-MM-YYYY string or `""`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_orderbook_commitment.py
from datetime import date
from engine import orderbook
from engine.models import Order


def _orders():
    return {
        ("SO1", "A"): Order("SO1", "A", "A", 10, date(2026, 7, 20),
                            commitment="committed", promised_date=date(2026, 7, 22)),
        ("SO2", "B"): Order("SO2", "B", "B", 10, date(2026, 7, 25)),   # open
    }


def test_active_so_lines_carry_lane_and_promise():
    lines = orderbook.active_so_lines(_orders(), actuals=[], masters=None)
    by_item = {l.item_code: l for l in lines}
    assert by_item["A"].commitment == "committed"
    assert by_item["A"].promised_date == date(2026, 7, 22)
    assert by_item["B"].commitment == "open"


def test_split_committed_open_partitions_lines():
    lines = orderbook.active_so_lines(_orders(), actuals=[], masters=None)
    protected, open_lines = orderbook.split_committed_open(lines)
    assert [l.item_code for l in protected] == ["A"]
    assert [l.item_code for l in open_lines] == ["B"]


def test_order_rows_show_lane_and_promised():
    rows = orderbook.order_rows(_orders(), {}, actuals=[], masters=None)
    a = next(r for r in rows if r["Item Code"] == "A")
    assert a["Lane"] == "committed"
    assert a["Promised"] == "22-07-2026"
    b = next(r for r in rows if r["Item Code"] == "B")
    assert b["Lane"] == "open" and b["Promised"] == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_orderbook_commitment.py -v`
Expected: FAIL — `active_so_lines` SOLine has no `commitment` value set / `split_committed_open` missing / `order_rows` has no `"Lane"`.

- [ ] **Step 3: Implement**

In `active_so_lines`, the `SOLine(...)` construction — add:
```python
        lines.append(SOLine(
            so_no=o.so_no, item_code=o.item_code, item_name=o.item_name,
            qty=remaining, delivery_date=o.delivery_date, process_qty=pq,
            commitment=o.commitment, promised_date=o.promised_date,
        ))
```

Add a new function near `active_so_lines`:
```python
def split_committed_open(so_lines):
    """Partition SO-lines into (protected, open). Protected = committed or urgent."""
    protected = [l for l in so_lines if l.commitment in ("committed", "urgent")]
    open_lines = [l for l in so_lines if l.commitment not in ("committed", "urgent")]
    return protected, open_lines
```

In `order_rows`, inside the `row(o, status)` dict, add:
```python
            "Lane": o.commitment,
            "Promised": fmt_date(o.promised_date) if o.promised_date else "",
```
(`fmt_date` is already imported in `orderbook.py`; verify with `grep -n "def fmt_date\|fmt_date" engine/orderbook.py` — it is used by `order_rows` already.)

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_orderbook_commitment.py -v` → PASS.
Run: `python3 -m pytest -q` → green.

- [ ] **Step 5: Commit**

```bash
git add engine/orderbook.py tests/test_orderbook_commitment.py
git commit -m "orderbook: carry lane into SO-lines, split_committed_open, Lane/Promised columns"
```

---

### Task 4: Rule 1 — carry lane onto batches; never merge across lanes

**Files:**
- Modify: `engine/rules/rule1_consolidate.py`
- Test: `tests/test_rule1_commitment.py` (create)

**Interfaces:**
- Produces: `Batch` gains `commitment: str = "open"` and `promised_date: Optional[date] = None` (Task 4a in `engine/models.py`). Rule 1 sets them from the merged SO-lines and only merges lines sharing `(item_code, commitment, promised_date)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rule1_commitment.py
from datetime import date
from engine.config import Config
from engine.models import SOLine
from engine.rules import rule1_consolidate


def test_same_item_different_lanes_are_not_merged():
    lines = [
        SOLine("SO1", "X", "X", 10, date(2026, 7, 20), commitment="committed",
               promised_date=date(2026, 7, 22)),
        SOLine("SO2", "X", "X", 10, date(2026, 7, 21), commitment="open"),
    ]
    batches = rule1_consolidate.run(lines, config=Config(), masters=None)
    assert len(batches) == 2                      # not merged across lanes
    lanes = sorted(b.commitment for b in batches)
    assert lanes == ["committed", "open"]


def test_committed_batch_carries_promised_date():
    lines = [SOLine("SO1", "X", "X", 10, date(2026, 7, 20), commitment="committed",
                    promised_date=date(2026, 7, 22))]
    b = rule1_consolidate.run(lines, config=Config(), masters=None)[0]
    assert b.commitment == "committed" and b.promised_date == date(2026, 7, 22)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_rule1_commitment.py -v`
Expected: FAIL — same-item lines merged into 1 batch / `Batch` has no `commitment`.

- [ ] **Step 3: Implement**

First add to `engine/models.py` `Batch` (after `process_qty`):
```python
    commitment: str = "open"
    promised_date: Optional[date] = None
```

Then in `engine/rules/rule1_consolidate.py`: read the current grouping key (usually item_code + a date window). Change the group key to include the lane so cross-lane lines never share a group:
```python
    # key: same item AND same lane AND same promised date may consolidate.
    group_key = (line.item_code, line.commitment, line.promised_date)
```
When constructing each `Batch`, set `commitment=` and `promised_date=` from the group's lines (all identical within a group). Inspect the file to match the existing batch-construction call and add the two kwargs.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_rule1_commitment.py -v` → PASS.
Run: `python3 -m pytest -k golden -q` → PASS (sample workbook has only open orders, one lane; grouping unchanged for them).
Run: `python3 -m pytest -q` → green.

- [ ] **Step 5: Commit**

```bash
git add engine/models.py engine/rules/rule1_consolidate.py tests/test_rule1_commitment.py
git commit -m "Rule 1: carry commitment/promised_date onto batches; never merge across lanes"
```

---

### Task 5: Rule 3 — order the protected group by promised date

**Files:**
- Modify: `engine/rules/rule3_tiebreak_process_time.py`
- Test: `tests/test_rule3_commitment.py` (create)

**Interfaces:**
- Produces: when the batches passed in are protected (any batch has `commitment` in {"committed","urgent"}), Rule 3 sorts them by `(promised_date, committed-tiebreak)` ascending. Open batches keep the existing slack order. Since planning runs one lane per pass (Task 7), a batch list is single-lane; Rule 3 detects it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rule3_commitment.py
from datetime import date
from engine.config import Config
from engine.models import Batch
from engine.rules import rule3_tiebreak_process_time as r3


def _b(bid, promised, item="X"):
    return Batch(batch_id=bid, item_code=item, item_name=item, qty=10,
                 so_delivery_date=date(2026, 7, 20), commitment="committed",
                 promised_date=promised)


def test_protected_batches_sort_by_promised_date():
    # feed later-promised first; Rule 3 must reorder to earliest-promised first
    batches = [_b("LATE", date(2026, 7, 28)), _b("EARLY", date(2026, 7, 22))]
    out = r3.run(batches, config=Config(), masters=_masters_for(batches))
    assert [b.batch_id for b in out] == ["EARLY", "LATE"]
```

Add a tiny masters helper at the top of the test file so Rule 3's routing lookups don't crash (it reads routings for the slack metric; for protected sort we bypass slack, but the function still runs). Use a minimal `Masters` with an empty routing per item:
```python
from engine.models import Masters, Routing
def _masters_for(batches):
    m = Masters()
    for b in batches:
        m.routings[b.item_code] = Routing(b.item_code, "", "", "", None, processes=[])
    return m
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_rule3_commitment.py -v`
Expected: FAIL — order is unchanged / by slack, not by promised date.

- [ ] **Step 3: Implement**

Read `rule3_tiebreak_process_time.run`. It currently computes a slack (or metric) per batch and sorts. Add, at the start of the sort, a lane branch:
```python
    protected = [b for b in batches if getattr(b, "commitment", "open") in ("committed", "urgent")]
    if protected and len(protected) == len(batches):
        # Single protected pass: order by the locked promise (earliest first),
        # then delivery date as a stable tiebreak. Slack is irrelevant here — the
        # promise is the commitment we schedule to.
        return sorted(batches, key=lambda b: (b.promised_date or b.so_delivery_date,
                                               b.so_delivery_date, b.batch_id))
    # ... existing slack-based sort unchanged (open pass / legacy) ...
```
Place this branch before the existing slack computation/return.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_rule3_commitment.py -v` → PASS.
Run: `python3 -m pytest -k golden -q` → PASS (sample is all-open → existing branch).
Run: `python3 -m pytest -q` → green.

- [ ] **Step 5: Commit**

```bash
git add engine/rules/rule3_tiebreak_process_time.py tests/test_rule3_commitment.py
git commit -m "Rule 3: protected batches ordered by promised date (open pass unchanged)"
```

---

### Task 6: Rule 6 — machine/operator reservations (the two-pass core)

**Files:**
- Modify: `engine/rules/rule6_allocate.py` (`run(...)` signature ~249; the candidate feasible-start loop ~412–427 and `_allocate_op` ~176–196)
- Test: `tests/test_rule6_reservations.py` (create)

**Interfaces:**
- Produces: `rule6_allocate.run(batches, config=None, notes=None, masters=None, machine_lost_min=None, reserved=None, **kw)`. `reserved` is `{resource_id: [(start_dt, end_dt), ...]}` covering machines **and** operators. When set, a scheduled op may not overlap any reserved interval on its machine or its operator; it is pushed to the earliest working-time start whose whole `[start, end]` clears every reservation on both. `reserved=None` (default; every existing caller) is byte-identical to today.
- Consumes: existing `WorkClock` (`clk.advance`), `machine_free`, `operator_free`.

**Approach (monotonic reservation-skip, non-preemptive):** after computing an op's candidate `feasible` start and its `end` for a machine+operator, if `[feasible, end]` overlaps a reserved interval on the machine or the operator, jump `feasible` to the end of the latest overlapping reservation (re-advanced to a working time), recompute `end`, and re-check. This runs the op continuously in the earliest free window big enough — filling committed gaps the scheduler's cursor reaches, never overrunning a committed block.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rule6_reservations.py
from datetime import date, datetime
from engine.config import Config
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters
from engine.rules import rule6_allocate


def _masters():
    m = Masters(machines={"M": Machine("M", "M", "mill")}, calendar=WorkCalendar())
    m.routings["X"] = Routing("X", "", "", "", None, processes=[
        Process(1, "op", cycle_time=10, total_time=None, suggested_machine="M",
                allotted_machine=None)])
    return m


def _batch():
    return Batch("B1", "X", "X", 1, date(2026, 3, 7), source_so_refs=["SO"])


def test_reserved_none_is_unchanged():
    cfg = Config(plan_start_date=date(2025, 3, 5))
    base = rule6_allocate.run([_batch()], config=cfg, masters=_masters())
    same = rule6_allocate.run([_batch()], config=cfg, masters=_masters(), reserved=None)
    assert [(e.machine, e.start, e.end) for e in base] == \
           [(e.machine, e.start, e.end) for e in same]


def test_op_is_pushed_past_a_reserved_block_on_its_machine():
    cfg = Config(plan_start_date=date(2025, 3, 5))          # Wed 08:00 start
    # Reserve machine M for the first 2 hours; the 10-min op must start at/after 10:00.
    reserved = {"M": [(datetime(2025, 3, 5, 8, 0), datetime(2025, 3, 5, 10, 0))]}
    sched = rule6_allocate.run([_batch()], config=cfg, masters=_masters(), reserved=reserved)
    op = sched[0]
    assert op.start >= datetime(2025, 3, 5, 10, 0)
    assert op.end <= datetime(2025, 3, 5, 10, 30)          # ran right after the block
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_rule6_reservations.py -v`
Expected: `test_reserved_none_is_unchanged` PASS (kwarg not yet accepted → actually errors); first make the signature accept `reserved`. Expected overall FAIL — `run() got an unexpected keyword argument 'reserved'`, then the push test fails.

- [ ] **Step 3: Implement**

1. Signature: change `def run(batches, config=None, notes=None, masters=None, machine_lost_min=None, **kw):` → add `reserved=None` before `**kw`. Normalise once at the top of `run`:
```python
    reserved = reserved or {}
```

2. Add a module-level helper (near `_allocate_op`):
```python
def _clear_reservations(feasible, end, clk, res_lists):
    """Push (feasible, end) forward until the op runs continuously clear of every
    reserved interval in res_lists (a list of [(start,end),...] for the machine and
    the operator). Recomputes end via the same working-time span. Returns the new
    (feasible, end). Non-preemptive: the whole op must clear each reservation."""
    span = end - feasible  # NOTE: recompute properly with clk below
    for _ in range(64):
        conflict = None
        for lst in res_lists:
            for (rs, re_) in lst:
                if feasible < re_ and end > rs:          # overlap
                    if conflict is None or re_ > conflict:
                        conflict = re_
        if conflict is None:
            return feasible, end
        feasible = clk.advance(conflict, 0)              # first working minute after block
        end = clk.advance(feasible, (end - feasible_prev_span(...)))  # see step below
    return feasible, end
```

Because `end` must be recomputed from the op's *working-minute* occupancy (not a naive span), integrate the check **inside** `run`'s candidate loop where `occ`/`end` are known rather than as a standalone span helper. Concretely, in the candidate loop (~412–427) after `cand_feasible` is computed and in `_allocate_op` where the op end is computed via `end_of(clk, f, q)`, wrap the placement: compute `end = end_of(clk, f, qty)`, then:
```python
        res_lists = [reserved.get(m, []), reserved.get(op, [])]  # op = chosen operator, may be ""
        while True:
            conflict_end = None
            for lst in res_lists:
                for rs, re_ in lst:
                    if f < re_ and end > rs and (conflict_end is None or re_ > conflict_end):
                        conflict_end = re_
            if conflict_end is None:
                break
            f = clk.advance(conflict_end, 0)
            end = end_of(clk, f, qty)
```
Apply the same clearance to the candidate feasibility used for choosing the machine (so a reserved machine looks busy and an alternative may be picked). Keep it minimal: the single-entry path in `_allocate_op` is the primary one; add the clearance there before returning the entry, and pass `reserved` + the resolved operator into `_allocate_op`.

*(Implementer: thread `reserved` from `run` into `_allocate_op` as a new keyword arg `reserved=None`; the reservation lists for the operator use the operator name chosen in `_allocate_op`. Guard with `if reserved:` so the `None` path is untouched.)*

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_rule6_reservations.py -v` → PASS.
Run: `python3 -m pytest -k golden -q` → PASS (all existing callers pass `reserved=None`).
Run: `python3 -m pytest -q` → green.

- [ ] **Step 5: Commit**

```bash
git add engine/rules/rule6_allocate.py tests/test_rule6_reservations.py
git commit -m "Rule 6: optional machine/operator reservations (open ops avoid committed blocks)"
```

---

### Task 7: Two-pass Plan in `api._plan`

**Files:**
- Modify: `api/main.py` (`_plan` ~445–510)
- Test: `tests/test_two_pass_plan.py` (create)

**Interfaces:**
- Consumes: `orderbook.active_so_lines`, `orderbook.split_committed_open`, `run_forward`, `rule6_allocate.run` (via `run_forward`), `ScheduleEntry`.
- Produces: `_plan` schedules protected orders (pass 1), extracts their machine+operator busy intervals, schedules open orders (pass 2) with those reservations, and merges both schedules into the returned trace/gantt/orders. Helper `_reservations_from_schedule(schedule) -> dict` (machine id → intervals, operator name → intervals).

**Design note:** `run_forward` (`engine/pipeline.py`) currently calls `rule6_allocate.run(...)` without `reserved`. Add an optional `reserved=None` parameter to `run_forward` that it forwards to `rule6_allocate.run`. (Small change; keep default `None` so existing callers are unchanged.)

- [ ] **Step 1: Write the failing integration test**

```python
# tests/test_two_pass_plan.py
import io, datetime
from engine.loaders import load_all
from tests.sample_workbook import build_sample_bytes


def _api():
    import importlib, api.main as m
    importlib.reload(m)
    return m


def test_open_order_never_moves_a_committed_order(monkeypatch, tmp_path):
    monkeypatch.setenv("STORE_DIR", str(tmp_path / "s"))
    m = _api()
    from engine import book_store
    from engine.models import Order
    from datetime import date
    # Upload the sample masters so routings exist.
    book_store.save_masters_bytes(build_sample_bytes())
    # One committed order, plan it, note its completion; then add a big open order.
    book_store.add_orders([Order("SOc", "A", "A", 10, date(2025, 3, 20),
                                 commitment="committed", promised_date=date(2025, 3, 20))])
    from engine.config import Config
    r1 = m._plan(Config(plan_start_date=date(2025, 3, 5)))
    # committed order's expected completion (from gantt or trace rule6 output)
    before = _committed_end(r1)
    book_store.add_orders([Order("SOo", "A", "A", 5000, date(2025, 3, 25))])  # huge open
    r2 = m._plan(Config(plan_start_date=date(2025, 3, 5)))
    after = _committed_end(r2)
    assert after == before          # the committed order's schedule did not move
```

Provide `_committed_end(result)` as a small helper in the test that reads the committed order's rows from `result["trace"]["rule6"]["output"]` (filter by SO "SOc") and returns the max end. Use the item code (`ITEM_A` = "A") that exists in `tests/sample_workbook.py`; confirm the exact sample item codes with `python3 -c "import tests.sample_workbook as s; print(s.ITEM_A)"`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_two_pass_plan.py -v`
Expected: FAIL — with a single-pass plan the 5000-piece open order competes and pushes the committed order's end later (`after > before`).

- [ ] **Step 3: Implement the two-pass split in `_plan`**

In `api/main.py` `_plan`, replace the single `so_lines`/`run_forward` block with:
```python
    all_lines = orderbook.active_so_lines(active, actuals, masters)  # remaining qty
    protected, open_lines = orderbook.split_committed_open(all_lines)

    eff_start = orderbook.effective_plan_start_date(actuals, config.plan_start_date, masters.calendar)
    if eff_start != config.plan_start_date:
        config = replace(config, plan_start_date=eff_start)

    # Pass 1: protected orders, as if open orders don't exist.
    plan_protected = PlanRun(so_lines=protected)
    trace = run_forward(plan_protected, config, masters)
    reserved = _reservations_from_schedule(plan_protected.schedule)

    # Pass 2: open orders backfill the free machine/operator intervals left by pass 1.
    plan_open = PlanRun(so_lines=open_lines)
    trace_open = run_forward(plan_open, config, masters, reserved=reserved)

    # Merge for display: combined schedule + batches. Protected first.
    plan_run = PlanRun(so_lines=all_lines)
    plan_run.schedule = plan_protected.schedule + plan_open.schedule
    plan_run.batches_prioritized = (plan_protected.batches_prioritized
                                    + plan_open.batches_prioritized)
    # Rebuild the rule6 trace table from the merged schedule so tabs/downloads show all.
    trace["rule6"]["output"] = to_table([e.as_row() for e in plan_run.schedule])
```
Add the helper near `_plan`:
```python
def _reservations_from_schedule(schedule):
    """Machine id → busy intervals and operator name → busy intervals, from a plan."""
    res = {}
    for e in schedule:
        if e.machine and "OS" not in e.machine and "Off-machine" not in e.machine:
            res.setdefault(e.machine, []).append((e.start, e.end))
        if getattr(e, "operator", ""):
            res.setdefault(e.operator, []).append((e.start, e.end))
    return res
```
And add `reserved=None` to `run_forward` in `engine/pipeline.py`, forwarding it to the `rule6_allocate.run(...)` call.

*(Implementer: the rest of `_plan` — gantt, orders, analytics via `_augment_helpers` — consumes `plan_run.schedule`/`batches_prioritized`, which now hold the merged plan, so they work unchanged. Verify the machine-wise view, gantt, and analytics still render with the merged schedule.)*

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_two_pass_plan.py -v` → PASS.
Run: `python3 -m pytest -k golden -q` → PASS (sample all-open → protected empty, pass 1 no-op, pass 2 == today's single pass; confirm the merged output equals the old output for an all-open book).
Run: `python3 -m pytest -q` → green.

- [ ] **Step 5: Commit**

```bash
git add api/main.py engine/pipeline.py tests/test_two_pass_plan.py
git commit -m "Two-pass Plan: protected orders first, open orders backfill the gaps"
```

---

### Task 8: Commit / urgent / uncommit endpoints + warning preview

**Files:**
- Modify: `api/main.py` (request models near the top; endpoints after `/orders/clear` ~642)
- Test: `tests/test_commit_endpoints.py` (create)

**Interfaces:**
- Produces:
  - `POST /orders/commit` `{orders: [[so,item],...]}` (admin) — for each, snapshot its current expected completion from a fresh plan and `book_store.set_commitment(..., "committed", promised_date, now_iso)`.
  - `POST /orders/urgent` `{so, item, confirm: bool=False}` (admin) — preview which promises it pushes; if `confirm` false and any pushed, return `{"warning": [...]}`; else `set_commitment(..., "urgent", delivery_date, now_iso)`.
  - `POST /orders/uncommit` `{orders: [[so,item],...]}` (admin) — `book_store.clear_commitment(...)`.
- Consumes: `_plan`, `require_admin`, `book_store`, `datetime.now`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_commit_endpoints.py  (drive functions directly; auth covered elsewhere)
from datetime import date
def test_commit_snapshots_current_expected_as_promise(monkeypatch, tmp_path):
    monkeypatch.setenv("STORE_DIR", str(tmp_path / "s"))
    import importlib, api.main as m; importlib.reload(m)
    from engine import book_store; from engine.models import Order
    book_store.save_masters_bytes(__import__("tests.sample_workbook", fromlist=["build_sample_bytes"]).build_sample_bytes())
    book_store.add_orders([Order("SO1", "A", "A", 10, date(2025, 3, 20))])
    m._commit_orders([("SO1", "A")])                     # internal helper (below)
    o = book_store.load_active_orders()[("SO1", "A")]
    assert o.commitment == "committed"
    assert o.promised_date is not None                   # a real date was snapshotted
```

- [ ] **Step 2: Run to verify it fails**

Run: `python3 -m pytest tests/test_commit_endpoints.py -v` → FAIL (`_commit_orders` missing).

- [ ] **Step 3: Implement helpers + endpoints**

Add an internal helper (testable without HTTP) and thin endpoints:
```python
def _order_expected_end(plan_result, so, item):
    """Latest expected completion date for one order from a plan result, or None."""
    ends = [r for r in plan_result["trace"]["rule6"]["output"]["rows"]]  # list-of-lists
    # Map columns → index once:
    cols = plan_result["trace"]["rule6"]["output"]["columns"]
    return _max_end_for(cols, ends, so, item)   # implement: parse End per row, return date

def _commit_orders(pairs):
    from datetime import datetime
    masters = _current_masters()
    result = _plan(_load_plan_config())
    now = datetime.now().isoformat(timespec="seconds")
    for so, item in pairs:
        promised = _order_expected_end(result, so, item)
        book_store.set_commitment(so, item, "committed", promised, now)
```
Then:
```python
@app.post("/orders/commit")
def commit_orders(req: CommitRequest, request: Request):
    require_admin(request)
    _commit_orders([(o[0], o[1]) for o in req.orders if len(o) == 2])
    return {"committed": len(req.orders)}

@app.post("/orders/uncommit")
def uncommit_orders(req: CommitRequest, request: Request):
    require_admin(request)
    for o in req.orders:
        if len(o) == 2:
            book_store.clear_commitment(o[0], o[1])
    return {"uncommitted": len(req.orders)}

@app.post("/orders/urgent")
def urgent_order(req: UrgentRequest, request: Request):
    require_admin(request)
    from datetime import datetime
    # Preview: mark urgent in a scratch, re-plan pass-1, diff other promises.
    pushed = _preview_urgent_pushes(req.so, req.item)
    if pushed and not req.confirm:
        return {"warning": pushed}
    order = book_store.load_active_orders().get((req.so, req.item))
    book_store.set_commitment(req.so, req.item, "urgent",
                              order.delivery_date if order else None,
                              datetime.now().isoformat(timespec="seconds"))
    return {"urgent": True}
```
Add `CommitRequest(orders: list)` and `UrgentRequest(so: str, item: str, confirm: bool = False)` Pydantic models beside the existing request models. Implement `_preview_urgent_pushes(so, item)` per the spec (re-run pass 1 with the order set urgent in a scratch copy of active orders, compare each other protected order's new expected vs its stored `promised_date`, return `[{"so":..,"item":..,"promised":"..","new":".."}]`). Implement `_max_end_for` by locating the "SO No"/"Item Code"/"End" columns and parsing the max end date.

- [ ] **Step 4: Run tests**

Run: `python3 -m pytest tests/test_commit_endpoints.py -v` → PASS.
Run: `python3 -m pytest -q` → green.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_commit_endpoints.py
git commit -m "API: commit / urgent (with push-warning preview) / uncommit endpoints"
```

---

### Task 9: Orders tab UI — lane badges, Promised/Current, buttons, warning modal

**Files:**
- Modify: `web/app.js` (Orders tab render + actions), `web/index.html` (modal markup if needed), `web/style.css` (badge + slip styles)
- Test: manual browser verification (documented steps) — no unit test framework for the vanilla JS.

**Interfaces:**
- Consumes: `/orders` rows now carry `"Lane"` and `"Promised"`; the plan's Gantt/`/orders` "Remaining"/expected feed "Current expected". `/orders/commit`, `/orders/urgent`, `/orders/uncommit`.

- [ ] **Step 1: Render lane badge + Promised + Current expected columns**

In the Orders-tab table render (find where `orders` rows are drawn in `web/app.js`), add a **Lane** badge cell (colour by `Open`/`Committed`/`Urgent`), the **Promised** cell (from row `"Promised"`), and a **Current expected** cell (the order's expected completion — reuse the value already shown in the Gantt "Expected completion"; join by `(SO No, Item Code)`). If `Current expected` date > `Promised` date, add class `slip` (red) to the row.

- [ ] **Step 2: Add per-row action buttons (admin only)**

Add **Commit**, **Commit as Urgent**, **Uncommit** buttons per row (and a bulk **Commit selected** using the existing multi-select checkboxes). Hide them for `body.role-user` (mirror how admin-only controls are already hidden). Wire:
```js
function commitOrders(pairs){ return postJson('/orders/commit', {orders: pairs}); }
function uncommitOrders(pairs){ return postJson('/orders/uncommit', {orders: pairs}); }
async function markUrgent(so, item){
  let res = await postJson('/orders/urgent', {so, item, confirm:false});
  if (res.warning && res.warning.length){
    if (!confirmWarningModal(res.warning)) return;      // owner cancels
    res = await postJson('/orders/urgent', {so, item, confirm:true});
  }
  await refreshOrders(); await runPlan(false);
}
```
After any commit/uncommit, **re-plan** (`runPlan(false)`) and refresh the Orders table so lanes + dates update.

- [ ] **Step 3: Warning modal**

`confirmWarningModal(list)` shows a modal listing each pushed promise — `"SO-478 (item B): 27-Jul → 29-Jul (past its promise)"` — with **Proceed** / **Cancel**. Reuse the existing password-confirm modal pattern in `web/` (there's already a modal for delete). Return true on Proceed.

- [ ] **Step 4: Browser-verify (the project norm for UI)**

Run locally (fresh store), upload `Test5.xlsx`, log in as admin, and confirm end-to-end:
```bash
rm -rf data/store
STORE_DIR=data/store nohup python3 -m uvicorn api.main:app --port 8011 >/tmp/uv.log 2>&1 &
curl -s -c /tmp/ck.txt -X POST http://127.0.0.1:8011/login -d "username=anvitech&password=1930rail" >/dev/null
curl -s -b /tmp/ck.txt -F "file=@Test5.xlsx" http://127.0.0.1:8011/upload >/dev/null
```
Drive a browser (as in prior sessions): commit a first batch, upload/add a second batch, re-plan, and **confirm the committed orders' Current expected dates did not move** and the second batch shows Open behind them. Mark one Open order Urgent and confirm the warning modal appears when it would push a promise.

- [ ] **Step 5: Commit**

```bash
git add web/app.js web/index.html web/style.css
git commit -m "Orders tab: lane badges, Promised vs Current, Commit/Urgent/Uncommit + warning modal"
```

---

### Task 10: Docs — RULES.md, CLAUDE.md, HANDOFF.md

**Files:** Modify `RULES.md`, `CLAUDE.md`, `HANDOFF.md`.

- [ ] **Step 1:** In `RULES.md`, add an "Order commitment (lanes) & two-pass planning" subsection under the Rule 6 / order-book area describing the three lanes and the two-pass mechanism; add the config-summary note.
- [ ] **Step 2:** In `CLAUDE.md`, add `commitment`/`promised_date` to the `orderbook.py`/`book_store.py`/`models.py` bullets and note the two-pass `_plan` + Rule 6 `reserved`.
- [ ] **Step 3:** In `HANDOFF.md`, add the feature under "What changed most recently".
- [ ] **Step 4: Commit**
```bash
git add RULES.md CLAUDE.md HANDOFF.md
git commit -m "Docs: order commitment lanes + two-pass promise protection"
```

---

## Self-Review

**Spec coverage:**
- Three lanes → Tasks 1,3,4,5 (data + carry + priority). ✓
- Commit locks current expected → Task 8 (`_commit_orders` snapshots plan end). ✓
- Urgent slots by delivery date → Task 5 (promised=delivery date sort) + Task 8 (urgent sets promised=delivery date). ✓
- Two-pass mechanism / gap-fill → Tasks 6 (reservations) + 7 (two-pass `_plan`). ✓
- Warning preview → Task 8 (`_preview_urgent_pushes` + `/orders/urgent`). ✓
- Promised vs Current + slip flag → Task 9 (UI). ✓
- Persistence → Task 2. ✓
- Consolidation guard → Task 4. ✓
- Defaults byte-identical / golden unchanged → asserted in Tasks 1,4,5,6,7. ✓
- Non-goals (no auto-commit, no padding, no multi-level) → nothing added for them. ✓

**Placeholder scan:** Task 6's `_clear_reservations` sketch is illustrative; the *authoritative* implementation is the inline loop in Step 3 (integrated with `end_of`) — the standalone sketch must be dropped by the implementer in favour of the inline version (called out in the step). Task 8's `_max_end_for`/`_preview_urgent_pushes`/`_order_expected_end` are described with exact inputs/outputs but need the column-parsing body filled from the actual `to_table` shape — implementer writes a unit test per helper first.

**Type consistency:** `commitment`/`promised_date`/`committed_at` names identical across `Order`, `SOLine`, `Batch`, `book_store`, `orderbook`. `reserved` is `{id: [(datetime,datetime)]}` in Rule 6 and produced identically by `_reservations_from_schedule`. `set_commitment(so,item,commitment,promised_date,committed_at)` signature matches its callers in Task 8.
