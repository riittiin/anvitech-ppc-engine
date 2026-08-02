# Demand-aware "bottleneck" Operator Policy (Approach 2, Level 2) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a demand-aware `bottleneck` operator-assignment policy that keeps the operators needed by busy machines free (with a one-step strand look-ahead), make it a contest candidate against `scarce`, and measure it on Test8.

**Architecture:** Builds on the shelved branch `operator-assignment-optimize-dimension` (its `operator_pick` config knob, `_plan_config` wiring, contest axis, and persist/replay are reused unchanged). This plan adds: a per-machine demand weight computed once in `decode`, a `bottleneck` branch in `ppc_engine`'s operator picker, and swaps the swept policy set from `("scarce","balanced")` to `("scarce","bottleneck")`.

**Tech Stack:** Python, pytest. Live scheduler = vendored `ppc_engine/`; adapter/optimizer/API = `engine/`.

**Spec:** `docs/superpowers/specs/2026-08-03-operator-bottleneck-policy-design.md`

**Branch:** create `operator-bottleneck-policy` **forked from `operator-assignment-optimize-dimension`** (NOT from main) so the shelved branch's plumbing is present. The SDD setup handles this.

## Global Constraints

- **Default `operator_pick="scarce"` stays byte-identical to today.** Golden trace + full suite green.
- **Graceful degradation:** with an empty/flat demand map, the `bottleneck` pick must resolve to exactly the `scarce` order (same operator). This is a required invariant, tested.
- **Feasibility is free:** the pick only ever chooses among already-free, qualified, on-shift operators — no policy can produce an infeasible/double-booked schedule.
- **New engine only.** Classic/flow ignore `operator_pick` entirely; do not touch their paths.
- **`bottleneck` is optimizer-owned**, never a user knob (read-only in Settings).
- **Determinism:** every pick breaks ties by the existing `(flexibility, name)` order.
- **Commit** after each task with footer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
  Do **not** push (owner pushes/deploys manually).
- **Run tests** with `pytest` from the repo root. Baseline before this plan: 738 passed / 1 skipped.

---

### Task 1: Allow `bottleneck` as a valid `operator_pick`

**Files:**
- Modify: `engine/config.py` (the `operator_pick` validation in `validate()`, and the field comment)
- Test: `tests/test_operator_pick_dimension.py` (append)

**Interfaces:**
- Produces: `Config(operator_pick="bottleneck").validate()` passes; `"scarce"|"balanced"|"flexible"|"bottleneck"` are the four accepted values.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_operator_pick_dimension.py`:

```python
def test_operator_pick_accepts_bottleneck():
    Config(operator_pick="bottleneck").validate()  # must not raise
    assert Config.from_dict({"operator_pick": "bottleneck"}).operator_pick == "bottleneck"


def test_operator_pick_still_rejects_unknown():
    import pytest
    with pytest.raises(ValueError):
        Config(operator_pick="nope").validate()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_operator_pick_dimension.py::test_operator_pick_accepts_bottleneck -v`
Expected: FAIL (validate() rejects "bottleneck" — not yet in the allowed set).

- [ ] **Step 3: Add `bottleneck` to the allowed set**

In `engine/config.py`, change the `operator_pick` validation line (currently rejects anything not in `("scarce","balanced","flexible")`) to include `"bottleneck"`:

```python
        if self.operator_pick not in ("scarce", "balanced", "flexible", "bottleneck"):
            errs.append("operator_pick must be 'scarce', 'balanced', 'flexible', or 'bottleneck'")
```

Update the field's doc comment to add the new value:

```python
    #   "bottleneck": demand-aware — assign whoever's OTHER machines are least busy,
    #                 keeping free the operators needed by busy machines (with a
    #                 one-step strand look-ahead). Falls back to scarce with no demand.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_operator_pick_dimension.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/config.py tests/test_operator_pick_dimension.py
git commit -m "feat(config): accept 'bottleneck' as a valid operator_pick"
```

---

### Task 2: Per-machine demand weight (`machine_demand`)

**Files:**
- Modify: `ppc_engine/scheduler/staffing.py` (add `machine_demand(...)` next to `build_machine_pools`)
- Test: `tests/test_bottleneck_policy.py` (create)

**Interfaces:**
- Consumes: `operation_duration_min` (from `ppc_engine.scheduler.duration`), `Order`, `Masters`.
- Produces: `machine_demand(orders, masters, config) -> dict[str, float]` — per-machine remaining processing minutes, expected-share split across each op's machine options; OS/DISPATCH (empty `machine_options`) contribute nothing.

- [ ] **Step 1: Write the failing test**

Create `tests/test_bottleneck_policy.py`:

```python
"""Demand-aware 'bottleneck' operator policy (Approach 2, Level 2)."""
from datetime import date, datetime

from ppc_engine.config import PlanConfig
from ppc_engine.domain.calendar import ShopCalendar
from ppc_engine.domain.masters import Masters
from ppc_engine.domain.order import Order
from ppc_engine.domain.resources import Machine, MachineKind, Operator, Role, Shift
from ppc_engine.domain.routing import Operation, OperationKind, Routing
from ppc_engine.scheduler.staffing import (StaffingBoard, build_machine_pools,
                                           machine_demand)


def _mach(mid):
    return Machine(id=mid, type_text="CNC lathe", kind=MachineKind.MACHINING,
                   available_hrs_per_day=19.5)


def _cfg(**kw):
    # week_anchor=None => no rotation => operators stay on their base_shift.
    return PlanConfig(plan_start=datetime(2025, 3, 5, 8, 0), week_anchor=None,
                      setup_min=90.0, **kw)


def test_machine_demand_is_expected_share_over_options():
    masters = Masters(
        machines={"CNC1": _mach("CNC1"), "CNC2": _mach("CNC2")},
        routings={
            # op1 can run on CNC1 or CNC2 (2 options); machining => 90 + qty*cycle.
            "IT": Routing("IT", "", operations=(
                Operation(1, "CNC", OperationKind.MACHINING,
                          machine_options=("CNC1", "CNC2"), cycle_min=2.0),
                Operation(2, "DISPATCH", OperationKind.DISPATCH),  # no options -> ignored
            )),
        },
    )
    orders = [Order("SO1", "IT", "item", qty=10, due_date=date(2025, 4, 1))]
    d = machine_demand(orders, masters, _cfg())
    # duration = 90 + 10*2 = 110 min, split across 2 options => 55 each.
    assert d == {"CNC1": 55.0, "CNC2": 55.0}


def test_machine_demand_skips_os_and_dispatch_and_missing_routing():
    masters = Masters(
        machines={"CNC1": _mach("CNC1")},
        routings={"IT": Routing("IT", "", operations=(
            Operation(1, "BANDSAW OS", OperationKind.OUTSOURCED, machine_options=(), cycle_min=240.0),
            Operation(2, "DISPATCH", OperationKind.DISPATCH),
        ))},
    )
    orders = [
        Order("SO1", "IT", "item", qty=5, due_date=date(2025, 4, 1)),
        Order("SO2", "GHOST", "no-routing", qty=5, due_date=date(2025, 4, 1)),  # skipped
    ]
    assert machine_demand(orders, masters, _cfg()) == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bottleneck_policy.py -v`
Expected: FAIL (ImportError: `machine_demand` does not exist).

- [ ] **Step 3: Implement `machine_demand`**

In `ppc_engine/scheduler/staffing.py`, add the import at the top (next to the existing imports):

```python
from ppc_engine.scheduler.duration import operation_duration_min
```

and add this function directly below `build_machine_pools`:

```python
def machine_demand(orders, masters: Masters, config: PlanConfig) -> dict[str, float]:
    """Per-machine remaining processing minutes — the 'how busy is each machine' signal
    for the demand-aware 'bottleneck' pick. For every in-house op of every order, take
    the op's duration at its remaining qty (the same duration the scheduler uses) and add
    an expected share (duration / number of machine options) to each option's total.
    OS/DISPATCH ops (no machine_options) contribute nothing; an order whose item has no
    routing is skipped. Static for a plan — computed once."""
    demand: dict[str, float] = {}
    for order in orders:
        routing = masters.routings.get(order.item_code)
        if routing is None:
            continue
        for op in routing.operations:
            if not op.machine_options:
                continue
            op_qty = order.qty
            pr = getattr(order, "process_remaining", None)
            if pr is not None:
                op_qty = pr.get(op.seq, order.qty)
            dur = operation_duration_min(op, op_qty, config)
            if dur <= 0:
                continue
            share = dur / len(op.machine_options)
            for mid in op.machine_options:
                demand[mid] = demand.get(mid, 0.0) + share
    return demand
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_bottleneck_policy.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add ppc_engine/scheduler/staffing.py tests/test_bottleneck_policy.py
git commit -m "feat(ppc): machine_demand — per-machine remaining-minutes signal"
```

---

### Task 3: `StaffingBoard` demand + the `bottleneck` pick

**Files:**
- Modify: `ppc_engine/scheduler/staffing.py` (`StaffingBoard.__init__` gains `demand`; `candidate_operator` gains a `bottleneck` branch via a new `_bottleneck_pick` helper)
- Test: `tests/test_bottleneck_policy.py` (append)

**Interfaces:**
- Consumes: `machine_demand` output (Task 2).
- Produces: `StaffingBoard(pools, demand=None)`; `candidate_operator(..., config)` with `config.operator_pick == "bottleneck"` returns the least-precious-elsewhere free operator.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_bottleneck_policy.py`:

```python
def _board_and_masters(op_specs, machine_ids, demand):
    """op_specs: list of (name, frozenset(quals)). All OPERATOR role, FIRST shift."""
    machines = {mid: _mach(mid) for mid in machine_ids}
    operators = tuple(
        Operator(name=n, role=Role.OPERATOR, qualified_machines=frozenset(q),
                 base_shift=Shift.FIRST)
        for n, q in op_specs)
    masters = Masters(machines=machines, operators=operators, calendar=ShopCalendar())
    board = StaffingBoard(build_machine_pools(masters), demand)
    return board, masters


def _pick(board, masters, machine_id, cfg):
    day = date(2025, 3, 5)
    start = datetime(2025, 3, 5, 8, 0)
    end = datetime(2025, 3, 5, 12, 0)
    return board.candidate_operator(masters.machines[machine_id], day, Shift.FIRST,
                                    start, end, masters, cfg)


# Anil is MORE flexible (3 quiet machines); Bimal is LESS flexible (2) but is the SOLE
# operator for the busy machine CNC2. scarce wrongly burns Bimal on CNC1; bottleneck
# keeps Bimal free for CNC2 and puts Anil on CNC1.
_GRIND_OPS = [("Anil", {"CNC1", "CNC3", "CNC4"}), ("Bimal", {"CNC1", "CNC2"})]
_GRIND_MACHINES = ["CNC1", "CNC2", "CNC3", "CNC4"]


def test_scarce_burns_the_bottleneck_specialist():
    board, masters = _board_and_masters(_GRIND_OPS, _GRIND_MACHINES, {"CNC2": 1000.0})
    assert _pick(board, masters, "CNC1", _cfg(operator_pick="scarce")) == "Bimal"


def test_bottleneck_keeps_the_specialist_free():
    board, masters = _board_and_masters(_GRIND_OPS, _GRIND_MACHINES, {"CNC2": 1000.0})
    assert _pick(board, masters, "CNC1", _cfg(operator_pick="bottleneck")) == "Anil"


def test_bottleneck_with_empty_demand_equals_scarce():
    board, masters = _board_and_masters(_GRIND_OPS, _GRIND_MACHINES, {})
    assert (_pick(board, masters, "CNC1", _cfg(operator_pick="bottleneck"))
            == _pick(board, masters, "CNC1", _cfg(operator_pick="scarce")))


def test_bottleneck_single_candidate_unchanged():
    board, masters = _board_and_masters([("Solo", {"CNC1"})], ["CNC1"], {"CNC1": 500.0})
    assert _pick(board, masters, "CNC1", _cfg(operator_pick="bottleneck")) == "Solo"


def test_bottleneck_strand_discount_when_others_cover():
    # A THIRD operator also covers CNC2, so pulling Bimal onto CNC1 no longer strands
    # CNC2 -> Bimal's elsewhere-cost drops, and (being less flexible) he wins again.
    ops = _GRIND_OPS + [("Chetan", {"CNC2"})]
    board, masters = _board_and_masters(ops, _GRIND_MACHINES, {"CNC2": 1000.0})
    assert _pick(board, masters, "CNC1", _cfg(operator_pick="bottleneck")) == "Bimal"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_bottleneck_policy.py -v`
Expected: `test_scarce_burns_the_bottleneck_specialist` PASSES (scarce already exists); the four `bottleneck` tests FAIL (the branch doesn't exist yet — `candidate_operator` treats an unknown pick as scarce, so `test_bottleneck_keeps_the_specialist_free` fails returning "Bimal", and `test_bottleneck_strand_discount_when_others_cover` may pass by accident — that's fine, the key failing one is the GRIND differentiation).

- [ ] **Step 3: Add `demand` to the board and the `bottleneck` branch**

In `ppc_engine/scheduler/staffing.py`, change `StaffingBoard.__init__` to accept demand:

```python
    def __init__(self, pools: dict[str, tuple[Operator, ...]] | None = None,
                 demand: dict[str, float] | None = None) -> None:
```

and add, next to the other instance fields:

```python
        # machine id -> total remaining processing minutes (the 'bottleneck' pick's
        # demand signal). Empty => bottleneck degrades to scarce. See machine_demand().
        self._demand: dict[str, float] = demand or {}
```

In `candidate_operator`, after the `free` list is built and the `if not free: return None` guard, add the `bottleneck` branch alongside the existing ones:

```python
        pick = getattr(config, "operator_pick", "scarce")
        if pick == "flexible":
            return free[-1].name
        if pick == "balanced":
            return min(free, key=lambda o: (self._load.get(o.name, 0.0), o.flexibility, o.name)).name
        if pick == "bottleneck":
            return self._bottleneck_pick(machine, free, day, shift, start, end, masters, config)
        return free[0].name  # "scarce" (default): least flexible
```

Add the helper method to `StaffingBoard`:

```python
    def _bottleneck_pick(self, machine, free, day, shift, start, end, masters, config):
        """Assign the free operator we can most SPARE: the one whose OTHER machines carry
        the least demand, discounted by how many other operators are free to cover those
        machines right now. If an operator is the only free cover for a busy machine, that
        machine's full demand counts against pulling them here (one-step strand check).
        Ties -> the scarce order (flexibility, name) -> identical to scarce when demand is
        flat/empty."""
        def others_free(mprime, cand):
            return sum(
                1 for op in self._pools.get(mprime, ())
                if op.name != cand.name
                and effective_shift(op, day, config) == shift
                and masters.calendar.is_operator_available(op.name, day)
                and self.free_during(op.name, start, end))

        def cost(cand):
            total = 0.0
            for mprime in cand.qualified_machines:
                if mprime == machine.id:
                    continue
                d = self._demand.get(mprime, 0.0)
                if d <= 0:
                    continue
                total += d / (1 + others_free(mprime, cand))
            return total

        return min(free, key=lambda o: (cost(o), o.flexibility, o.name)).name
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_bottleneck_policy.py -v`
Expected: PASS (all). Then `pytest tests/test_new_engine.py -q` to confirm the board change didn't disturb existing decode behavior.

- [ ] **Step 5: Commit**

```bash
git add ppc_engine/scheduler/staffing.py tests/test_bottleneck_policy.py
git commit -m "feat(ppc): demand-aware 'bottleneck' operator pick with strand look-ahead"
```

---

### Task 4: Feed demand into `decode` + behavioral end-to-end

**Files:**
- Modify: `ppc_engine/scheduler/flow_scheduler.py` (compute demand, pass to `StaffingBoard` at line ~98)
- Test: `tests/test_bottleneck_policy.py` (append)

**Interfaces:**
- Consumes: `machine_demand` (Task 2), `StaffingBoard(..., demand=)` (Task 3), `new_engine._plan_config` (carries `operator_pick`, from the shelved branch).
- Produces: a full `decode(...)` under `operator_pick="bottleneck"` uses the live demand signal.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_bottleneck_policy.py`:

```python
from dataclasses import replace as _replace

from engine.config import Config
from engine.new_engine import _plan_config
from ppc_engine.scheduler import decode


def _routing(item, seq_machine):
    ops = tuple(Operation(i + 1, f"OP{i+1}", OperationKind.MACHINING,
                          machine_options=(m,), cycle_min=2.0)
                for i, m in enumerate(seq_machine))
    ops = ops + (Operation(len(ops) + 1, "DISPATCH", OperationKind.DISPATCH),)
    return Routing(item, "", operations=ops)


def _cnc(mid):
    return Machine(id=mid, type_text="CNC lathe", kind=MachineKind.MACHINING,
                   available_hrs_per_day=19.5)


def test_bottleneck_changes_the_decoded_plan_end_to_end():
    """Through _plan_config + decode (demand computed inside decode): scarce puts the
    less-flexible specialist Bimal on the routine machine CNC1 (stranding the busy CNC2);
    bottleneck puts Anil there and keeps Bimal for CNC2."""
    masters = Masters(
        machines={m: _cnc(m) for m in ("CNC1", "CNC2", "CNC3", "CNC4")},
        operators=(
            Operator("Anil", Role.OPERATOR, frozenset({"CNC1", "CNC3", "CNC4"}), Shift.FIRST),
            Operator("Bimal", Role.OPERATOR, frozenset({"CNC1", "CNC2"}), Shift.FIRST),
        ),
        routings={
            "ROUTINE": _routing("ROUTINE", ["CNC1"]),
            "BUSY": _routing("BUSY", ["CNC2"]),
        },
    )
    orders = [
        Order("R", "ROUTINE", "routine", qty=10, due_date=date(2025, 4, 1)),
        Order("B", "BUSY", "busy", qty=400, due_date=date(2025, 4, 1)),  # big -> CNC2 hot
    ]
    seq = [("R", "ROUTINE"), ("B", "BUSY")]
    cfg = Config(scheduler="new", plan_start_date=date(2025, 3, 5), apply_operator_logic=True)

    def cnc1_operator(pick):
        sched = decode(orders, seq, masters, _plan_config(_replace(cfg, operator_pick=pick)))
        segs = [s for s in sched.segments if s.machine_id == "CNC1" and s.operator]
        return segs[0].operator

    assert cnc1_operator("scarce") == "Bimal"
    assert cnc1_operator("bottleneck") == "Anil"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_bottleneck_policy.py::test_bottleneck_changes_the_decoded_plan_end_to_end -v`
Expected: FAIL — `decode` builds the board WITHOUT demand, so `bottleneck` sees an empty demand map and degrades to scarce → both return "Bimal".

- [ ] **Step 3: Pass demand into the board in `decode`**

In `ppc_engine/scheduler/flow_scheduler.py`, add `machine_demand` to the staffing import:

```python
from ppc_engine.scheduler.staffing import StaffingBoard, build_machine_pools, machine_demand
```

(If `StaffingBoard`/`build_machine_pools` are imported on a different line, add `machine_demand` to that same import.)

Then change the board construction (line ~98) from:

```python
    staffing = StaffingBoard(build_machine_pools(masters))
```
to:
```python
    staffing = StaffingBoard(build_machine_pools(masters),
                             machine_demand(orders, masters, config))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_bottleneck_policy.py -v && pytest tests/test_new_engine.py -q`
Expected: PASS (behavioral test passes; new_engine tests still green — demand is inert for scarce/default).

- [ ] **Step 5: Commit**

```bash
git add ppc_engine/scheduler/flow_scheduler.py tests/test_bottleneck_policy.py
git commit -m "feat(ppc): compute machine demand in decode and feed the staffing board"
```

---

### Task 5: Swap the contest candidate to `bottleneck`

**Files:**
- Modify: `engine/optimizer.py` (`OPERATOR_PICK_CANDIDATES`)
- Modify: `ppc_engine/config.py` (`PlanConfig.operator_pick` docstring — add `bottleneck`)
- Modify: `tests/test_operator_pick_dimension.py` (update the assertions that named `balanced`)
- Test: `tests/test_operator_pick_dimension.py`

**Interfaces:**
- Produces: `OPERATOR_PICK_CANDIDATES == ("scarce", "bottleneck")`; the contest sweeps `scarce` vs `bottleneck`.

- [ ] **Step 1: Update the tests first (they encode the new contract)**

In `tests/test_operator_pick_dimension.py`, update the three assertions that referenced `balanced` as a swept candidate:

- `test_operator_pick_candidates_are_scarce_and_balanced` → rename to `test_operator_pick_candidates_are_scarce_and_bottleneck` and assert:
```python
    from engine.optimizer import OPERATOR_PICK_CANDIDATES
    assert OPERATOR_PICK_CANDIDATES == ("scarce", "bottleneck")
```
- `test_operator_pick_contenders_put_current_first` → change the `scarce` case to:
```python
    assert operator_pick_contenders("scarce") == ["scarce", "bottleneck"]
```
  (the `"balanced"` and off-list `"flexible"` sub-assertions still hold — `operator_pick_contenders` just orders whatever is passed; leave them, but note the default `candidates=OPERATOR_PICK_CANDIDATES` now yields bottleneck).
- `test_contest_jobs_sweeps_operator_pick_for_new_engine` → change the expected pick set to:
```python
    assert picks == {"scarce", "bottleneck"}
```
- `test_sweep_optimize_sweeps_all_operator_picks` → replace the two `"balanced"` literals in the expected `calls` set and the winning policy with `"bottleneck"`:
```python
    assert set(calls) == {(False, "scarce"), (True, "scarce"),
                          (False, "bottleneck"), (True, "bottleneck")}
    assert res.operator_pick == "bottleneck"
```
  (and in that test's `fake_tune`, make the winner condition key off `"bottleneck"` instead of `"balanced"`: `late = 10 if config.operator_pick == "scarce" else 5`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_operator_pick_dimension.py -v`
Expected: FAIL (constant is still `("scarce","balanced")`).

- [ ] **Step 3: Change the constant + docstring**

In `engine/optimizer.py`, change:

```python
OPERATOR_PICK_CANDIDATES = ("scarce", "bottleneck")
```
and update the adjacent comment to say `balanced` was measured no-benefit on Test8 (2026-08-03) and dropped; `bottleneck` (demand-aware) is the new challenger; `balanced`/`flexible` remain valid engine values but are not swept.

In `ppc_engine/config.py`, add a line to the `operator_pick` docstring describing `"bottleneck"` (demand-aware; keeps busy-machine operators free; strand look-ahead; falls back to scarce with no demand).

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_operator_pick_dimension.py -v && pytest tests/test_optimize_service.py tests/test_optimize_shard.py -q`
Expected: PASS (dimension + contest/shard tests green with the new candidate).

- [ ] **Step 5: Commit**

```bash
git add engine/optimizer.py ppc_engine/config.py tests/test_operator_pick_dimension.py
git commit -m "feat(optimizer): sweep scarce vs bottleneck (drop measured-flat balanced)"
```

---

### Task 6: Settings label for `bottleneck`

**Files:**
- Modify: `web/app.js` (the operator-strategy label map added on the shelved branch)

**Interfaces:**
- Consumes: `/run` response `config.operator_pick` (already emitted).

- [ ] **Step 1: Add the label**

In `web/app.js`, in the `cfg-operatorpick-info` label map (added on the shelved branch), add the `bottleneck` entry:

```javascript
  if (opk) opk.textContent = ({
    scarce: "Save flexible people",
    balanced: "Spread work evenly",
    flexible: "Use flexible people first",
    bottleneck: "Send help where it's needed most",
  })[cfg.operator_pick] || "Save flexible people";
```

- [ ] **Step 2: Verify**

Run: `grep -n "bottleneck" web/app.js` — confirm the entry is present. Run `pytest -q` to confirm nothing broke (web edits don't affect Python). Live browser check is display-only and deferred to the owner (backend value is covered by the Task-5 tests).

- [ ] **Step 3: Commit**

```bash
git add web/app.js
git commit -m "feat(web): friendly label for the bottleneck operator strategy"
```

---

### Task 7: Full suite + Test8 before/after measurement

**Files:**
- Test: whole suite
- Reuse (scratchpad, not committed): the before/after harness `measure_test8.py` from the prior measurement.

- [ ] **Step 1: Run the full test suite**

Run: `pytest -q`
Expected: all pass (baseline 738/1-skipped + the new bottleneck tests; the 1 skip is the gitignored real-data file). Investigate any regression before proceeding.

- [ ] **Step 2: Measure `scarce` vs `bottleneck` on Test8**

Re-run the same controlled harness used for the coarse version (it partitions the single contest's rows by operator policy, so before/after share book, seed, overlap grid, and budget). With `OPERATOR_PICK_CANDIDATES == ("scarce","bottleneck")`, the harness now compares:
- **BEFORE** = best `scarce` candidate (== current live behavior),
- **AFTER** = best over `scarce` + `bottleneck`.

Run at budget 700, 6 processes (macOS: the harness needs the `if __name__ == "__main__":` guard — the existing scratchpad `measure_test8.py` already has it). Record makespan / late-days / worst and which policy won.

- [ ] **Step 3: Report + decide**

Summarize for the owner: did `bottleneck` beat `scarce`, by how much? This is the go/no-go for shipping Level 2, and the trigger for whether Level 3 (per-shift matching) is worth trying. No commit (measurement only).

---

## Self-Review

**Spec coverage:** §1 demand weight → Task 2 + wired in Task 4. §2 bottleneck pick (cost + strand look-ahead) → Task 3. §3 contest candidate swap → Task 5 (+ Task 1 makes the value valid). §4 Settings label → Task 6. Edge cases (flat→scarce, single candidate, GRIND, strand discount, absent) → Tasks 3–4 tests. Testing/measurement → Task 7. Level 3 / cross-training / fairness → out of scope, unchanged. ✓

**Placeholder scan:** No TBD/TODO; every code step has concrete code and exact anchors. ✓

**Type consistency:** `machine_demand(orders, masters, config) -> dict[str,float]` produced in Task 2, consumed by `StaffingBoard(pools, demand)` (Task 3) and `decode` (Task 4); `_bottleneck_pick` signature matches its `candidate_operator` call site; `OPERATOR_PICK_CANDIDATES` tuple shape unchanged (length 2) so `local_contest_multiplier`/`operator_pick_contenders` (reused, untouched) still behave; the config value `"bottleneck"` accepted in `Config.validate` (Task 1) before it is ever produced by the contest (Task 5). ✓
