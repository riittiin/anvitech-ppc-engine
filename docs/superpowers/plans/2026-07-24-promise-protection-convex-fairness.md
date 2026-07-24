# Promise Protection via a Convex Fairness Term — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the optimizer from avoidably pushing any single order far past its due date (the live Aug 8 → Aug 23 slip), by replacing the "protect only the worst order" fairness guard with a soft, capped, convex per-order tardiness penalty — applied in both scoring layers — plus a Thursday "what moved later" note.

**Architecture:** Production runs `scheduler="new"` (`ppc_engine`). Two scoring functions decide plans: `ppc_engine/objective/objective.py::score` (drives which job **sequence** wins) and `engine/optimizer.py::score`/`plan_metrics` (the overlap-contest winner-pick and the Thursday auto-apply "strictly better" gate). We add the **same** convex severity term to both, driven by new tunable weights, then enrich the auto-apply note. No hard veto (avoids the 2026-07-16 collapse); the penalty is a soft, capped add-on so the search never becomes infeasible.

**Tech Stack:** Python, FastAPI, pytest. Pure functions; frozen dataclasses (`PlanConfig`, `PlanMetrics`).

## Global Constraints

- **No hard constraint / feasibility gate.** Severity is a soft, capped *penalty* only — copied verbatim from the spec's non-goals. A plan is never rejected as illegal.
- **Protection is per-order vs the order's own SO delivery date** (`delivery_date` / `due_date`). No manual marking; `commitment`/`promised_date` stay informational and untouched.
- **The two score functions stay numerically consistent:** `ppc_engine/config.py` `severity_tolerance_days` / `severity_weight` / `severity_cap_days` must equal `engine/optimizer.py` `SEVERITY_TOLERANCE_DAYS` / `SEVERITY_WEIGHT` / `SEVERITY_CAP_DAYS`.
- **Weights are MEASURED, not guessed.** Ship with the behaviour-driven defaults below, then Task 5 tunes them on the real book and locks the final values; every default carries a "re-measure before moving" comment like the existing `MAKESPAN_WEIGHT`.
- **Golden trace must stay unchanged** — the classic engine's rule output is untouched; only the optimizer's *choice of sequence* and the note text change.
- **Severity math (identical in both layers):** for each order with signed lateness `g` days (completion − due; early = negative), `overage = min(severity_cap_days, max(0, g − severity_tolerance_days))`; `severity = Σ overage²`. Score adds `severity_weight × severity`.

---

### Task 1: Convex severity term in the `ppc_engine` objective (primary lever)

**Files:**
- Modify: `ppc_engine/config.py:80-87` (add three weights after `makespan_weight`)
- Modify: `ppc_engine/objective/objective.py` (add `_severity`, extend `score`)
- Test: `tests/test_promise_protection.py` (create)

**Interfaces:**
- Consumes: `PlanMetrics.lateness_by_order` (`dict[(so,item) -> signed days late]`), `PlanConfig`.
- Produces: `ppc_engine.objective.objective.score(metrics, config) -> float` — same signature, now reputation-aware.

- [ ] **Step 1: Write the failing test**

Create `tests/test_promise_protection.py`:

```python
from dataclasses import replace
from datetime import datetime

from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.objective.objective import score


def _metrics(latenesses, makespan=40.0):
    """Build a PlanMetrics from a list of signed per-order lateness (days)."""
    lb = {("SO", str(i)): float(v) for i, v in enumerate(latenesses)}
    tard = [max(0.0, v) for v in latenesses]
    return PlanMetrics(
        total_tardiness_days=sum(tard),
        max_tardiness_days=max(tard) if tard else 0.0,
        late_order_count=sum(1 for t in tard if t > 0),
        makespan_days=makespan,
        lateness_by_order=lb,
    )


def test_convex_term_protects_the_second_worst_order():
    # X is structurally impossible (~20 late and sets the max); B is savable.
    #   sacrifice: X=20, B pushed to 15   (what the old objective picked)
    #   protect:   X=22, B rescued to 2   (spread a little onto the doomed order)
    cfg = PlanConfig(plan_start=datetime(2025, 3, 1))
    sacrifice = _metrics([20.0, 15.0])
    protect = _metrics([22.0, 2.0])

    # OLD objective (severity off) WRONGLY prefers sacrificing B — the live bug:
    old = replace(cfg, severity_weight=0.0)
    assert score(protect, old) > score(sacrifice, old)

    # NEW objective (default convex term) prefers protecting B:
    assert score(protect, cfg) < score(sacrifice, cfg)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_promise_protection.py::test_convex_term_protects_the_second_worst_order -v`
Expected: FAIL — either `AttributeError`/`TypeError` (no `severity_weight` on `PlanConfig`) or an assertion failure (term not yet applied).

- [ ] **Step 3: Add the config weights**

In `ppc_engine/config.py`, immediately after line 87 (`makespan_weight: float = 0.1`), add:

```python

    # Reputation guard (2026-07-24 spec). A CONVEX, capped per-order tardiness
    # penalty. Unlike fairness_weight (which only shields the SINGLE worst order),
    # this penalizes EVERY order's lateness on an accelerating curve, so no savable
    # order is sacrificed for the aggregate. MEASURED on the real book (Task 5) —
    # re-measure before moving. Must equal engine/optimizer.py SEVERITY_* .
    #   severity_tolerance_days (T): first T late days cost nothing extra.
    #   severity_weight (mu):        strength of the squared overage.
    #   severity_cap_days:           overage capped at this many days before
    #                                squaring, so an impossible order can't dominate.
    severity_tolerance_days: float = 2.0
    severity_weight: float = 2.0
    severity_cap_days: float = 30.0
```

- [ ] **Step 4: Extend the objective**

In `ppc_engine/objective/objective.py`, replace the `score` function with:

```python
def _severity(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Convex, capped per-order tardiness — the reputation guard. Each order's
    lateness beyond a tolerance is squared (accelerating) and capped, so a savable
    order is never dumped for the aggregate and one impossible order can't dominate.
    Protects EVERY order, not just the single worst (that was max_tardiness's blind
    spot — see the 2026-07-24 spec)."""
    tol = config.severity_tolerance_days
    cap = config.severity_cap_days
    total = 0.0
    for late in metrics.lateness_by_order.values():
        over = late - tol            # `late` is signed; early/on-time -> <= 0
        if over <= 0.0:
            continue
        if over > cap:
            over = cap
        total += over * over
    return total


def score(metrics: PlanMetrics, config: PlanConfig) -> float:
    """Score a plan from its metrics. Lower is better."""
    return (
        metrics.total_tardiness_days
        + config.severity_weight * _severity(metrics, config)
        + config.fairness_weight * metrics.max_tardiness_days
        + config.makespan_weight * metrics.makespan_days
    )
```

(The `λ·max_tardiness` term is kept for now; Task 5's sweep decides whether to reduce `fairness_weight` toward 0 once the convex sum subsumes it.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/test_promise_protection.py::test_convex_term_protects_the_second_worst_order -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add ppc_engine/config.py ppc_engine/objective/objective.py tests/test_promise_protection.py
git commit -m "feat(objective): convex per-order tardiness guard in ppc_engine

Replaces the max-only fairness blind spot: penalizes every order's slip
on a capped accelerating curve so no savable order is sacrificed for the
aggregate. Soft penalty only (no feasibility gate)."
```

---

### Task 2: Mirror the convex term in `engine/optimizer.py` (acceptance parity)

**Files:**
- Modify: `engine/optimizer.py:32-63` (add constants; extend `plan_metrics` return + `score`)
- Test: `tests/test_promise_protection.py` (append)

**Interfaces:**
- Consumes: `plan_metrics(schedule, so_lines, plan_start) -> dict` (existing).
- Produces: the dict now also carries `"slip_severity": float`; `score(metrics)` folds in `SEVERITY_WEIGHT * metrics["slip_severity"]`. Used by `optimize_service.py:246` (contest winner-pick) and `api/main.py:_auto_apply_result` (Thursday gate) — both inherit automatically.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_promise_protection.py`:

```python
from datetime import date
from types import SimpleNamespace as NS

from engine import optimizer


def test_optimizer_score_convex_protects_second_worst():
    # Same scenario as the ppc test, in the old-space metrics dict. Makespan is
    # equal on both plans, so only the severity term can flip the preference.
    sacrifice = {"total_late_days": 35, "makespan_days": 40.0,
                 "slip_severity": (20 - 2) ** 2 + (15 - 2) ** 2}
    protect = {"total_late_days": 24, "makespan_days": 40.0,
               "slip_severity": (22 - 2) ** 2 + 0}
    assert optimizer.score(protect) < optimizer.score(sacrifice)


def test_plan_metrics_slip_severity_is_convex_and_capped():
    # One order 15 days late: overage 13 -> 169. The first 2 days (tolerance) are free.
    entry = NS(end=__import__("datetime").datetime(2025, 3, 16, 10, 0),
               so_refs=["SO1"], item_code="A")
    lines = [NS(so_no="SO1", item_code="A", delivery_date=date(2025, 3, 1))]
    m = optimizer.plan_metrics([entry], lines, date(2025, 3, 1))
    assert m["max_late_days"] == 15
    assert m["slip_severity"] == (15 - 2) ** 2  # 169.0


def test_plan_metrics_severity_zero_within_tolerance():
    entry = NS(end=__import__("datetime").datetime(2025, 3, 3, 10, 0),
               so_refs=["SO1"], item_code="A")   # 2 days late == tolerance 2 -> free
    lines = [NS(so_no="SO1", item_code="A", delivery_date=date(2025, 3, 1))]
    m = optimizer.plan_metrics([entry], lines, date(2025, 3, 1))
    assert m["slip_severity"] == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_promise_protection.py -k "optimizer_score_convex or slip_severity or within_tolerance" -v`
Expected: FAIL — `KeyError: 'slip_severity'` (score reads a field `plan_metrics` doesn't yet return) / missing severity in `score`.

- [ ] **Step 3: Add the mirrored constants**

In `engine/optimizer.py`, immediately after line 40 (`MAKESPAN_WEIGHT = 40.0`), add:

```python

# Reputation guard — the mirror of ppc_engine's convex severity term (2026-07-24
# spec). Kept numerically EQUAL to ppc_engine/config.py severity_* so the overlap
# contest winner-pick and the Thursday auto-apply gate judge plans the same
# reputation-aware way the sequence search does. Measured — re-measure before moving.
SEVERITY_TOLERANCE_DAYS = 2.0   # == ppc_engine severity_tolerance_days (T)
SEVERITY_WEIGHT = 2.0           # == ppc_engine severity_weight (mu)
SEVERITY_CAP_DAYS = 30.0        # == ppc_engine severity_cap_days
```

- [ ] **Step 4: Compute `slip_severity` in `plan_metrics`**

In `engine/optimizer.py`, in `plan_metrics`, the block currently reads (lines ~87-95):

```python
    gaps = [(expected[k] - due[k]).days for k in expected if k in due]
    late = [g for g in gaps if g > 0]
    return {
        "makespan_days": round(makespan, 2),
        "late_orders": len(late),
        "total_late_days": int(sum(late)),
        "max_late_days": int(max(late)) if late else 0,
        "orders": len(gaps),
    }
```

Replace it with:

```python
    gaps = [(expected[k] - due[k]).days for k in expected if k in due]
    late = [g for g in gaps if g > 0]
    slip_severity = 0.0
    for g in gaps:
        over = g - SEVERITY_TOLERANCE_DAYS
        if over > 0:
            if over > SEVERITY_CAP_DAYS:
                over = SEVERITY_CAP_DAYS
            slip_severity += float(over * over)
    return {
        "makespan_days": round(makespan, 2),
        "late_orders": len(late),
        "total_late_days": int(sum(late)),
        "max_late_days": int(max(late)) if late else 0,
        "slip_severity": round(slip_severity, 2),
        "orders": len(gaps),
    }
```

- [ ] **Step 5: Fold severity into `score`**

In `engine/optimizer.py`, replace the `score` function (lines ~61-63) with:

```python
def score(metrics: dict) -> float:
    """Lower is better: delivery lateness + makespan + convex per-order slip guard.
    ``slip_severity`` (added by plan_metrics) makes a big single-order slip cost far
    more than the same days spread thin — the acceptance-side mirror of the sequence
    search's objective (2026-07-24 spec). ``.get`` keeps any legacy metrics dict safe."""
    return (metrics["total_late_days"]
            + MAKESPAN_WEIGHT * metrics["makespan_days"]
            + SEVERITY_WEIGHT * metrics.get("slip_severity", 0.0))
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_promise_protection.py -v`
Expected: PASS (all four tests).

- [ ] **Step 7: Commit**

```bash
git add engine/optimizer.py tests/test_promise_protection.py
git commit -m "feat(optimizer): mirror the convex slip guard in the old-space score

plan_metrics now returns slip_severity; score folds it in with SEVERITY_WEIGHT,
kept numerically equal to ppc_engine. The overlap contest and the Thursday
auto-apply gate now judge plans reputation-aware, matching the sequence search."
```

---

### Task 3: Thursday "what moved later" note

**Files:**
- Modify: `api/main.py` (add `_expected_by_order`, `_movers`, `_format_movers`, `_movement_note`; wire into `_auto_apply_result:1404-1411`)
- Test: `tests/test_movement_note.py` (create)

**Interfaces:**
- Consumes: a schedule (list of entries with `.end`, `.so_refs`, `.item_code`), `_all_lines_schedule`, `optimize_service.prepare_contest`.
- Produces: `_movement_note(new_ranks) -> str` (a `" ⚠ …"` suffix, or `""`); appended to the applied auto-note.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_movement_note.py`:

```python
from datetime import date, datetime
from types import SimpleNamespace as NS

from api.main import _expected_by_order, _movers, _format_movers


def test_expected_by_order_takes_latest_end_per_order():
    e1 = NS(end=datetime(2025, 3, 10, 9, 0), so_refs=["SO1"], item_code="A")
    e2 = NS(end=datetime(2025, 3, 14, 9, 0), so_refs=["SO1"], item_code="A")  # later
    got = _expected_by_order([e1, e2])
    assert got[("SO1", "A")] == date(2025, 3, 14)


def test_movers_flags_only_orders_that_moved_later_beyond_threshold():
    old = {("SO1", "A"): date(2025, 3, 10), ("SO2", "B"): date(2025, 3, 10),
           ("SO3", "C"): date(2025, 3, 10)}
    new = {("SO1", "A"): date(2025, 3, 16),   # +6d -> flagged
           ("SO2", "B"): date(2025, 3, 11),   # +1d -> NOT (threshold is >1)
           ("SO3", "C"): date(2025, 3, 4)}    # earlier -> NOT
    out = _movers(old, new, threshold=1)
    assert out == [(("SO1", "A"), 6, date(2025, 3, 16))]


def test_format_movers_empty_is_blank():
    assert _format_movers([]) == ""


def test_format_movers_lists_worst_first_and_counts_overflow():
    movers = [(("SO1", "A"), 6, date(2025, 3, 16)),
              (("SO2", "B"), 4, date(2025, 3, 14)),
              (("SO3", "C"), 3, date(2025, 3, 13)),
              (("SO4", "D"), 2, date(2025, 3, 12))]
    s = _format_movers(movers)
    assert s.startswith(" ⚠ 4 order(s) now finish later than before: ")
    assert "SO1-A +6d" in s and "16-Mar" in s
    assert "+1 more" in s          # 4 movers, only top 3 named
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_movement_note.py -v`
Expected: FAIL — `ImportError` (`_expected_by_order` etc. not defined).

- [ ] **Step 3: Add the pure helpers**

In `api/main.py`, immediately after `_all_lines_schedule` (ends at line 1363), add:

```python
# How many days later an order must move (vs the plan that was on screen) before the
# Thursday note flags it. 1 = only flag moves of 2+ days. Reporting-only threshold.
_MOVE_LATER_THRESHOLD_DAYS = 1


def _expected_by_order(schedule):
    """Each order's expected completion DATE: the latest entry end across its
    processes, keyed (so_no, item_code). Mirrors optimizer.plan_metrics' expected
    map — the customer-facing 'when will it be done'."""
    expected = {}
    for e in schedule:
        d = e.end.date()
        for ref in (e.so_refs or []):
            k = (ref, e.item_code)
            if k not in expected or d > expected[k]:
                expected[k] = d
    return expected


def _movers(exp_old, exp_new, threshold):
    """(key, days_later, new_date) for every order whose new expected date is more
    than ``threshold`` days later than before. Worst mover first."""
    out = []
    for k, nd in exp_new.items():
        od = exp_old.get(k)
        if od is not None and (nd - od).days > threshold:
            out.append((k, (nd - od).days, nd))
    out.sort(key=lambda m: m[1], reverse=True)
    return out


def _format_movers(movers):
    """One-line ' ⚠ N order(s) now finish later …' suffix; '' when nothing moved."""
    if not movers:
        return ""
    top = movers[:3]
    parts = [f"{k[0]}-{k[1]} +{d}d (→ {nd.strftime('%d-%b')})" for k, d, nd in top]
    more = f", +{len(movers) - 3} more" if len(movers) > 3 else ""
    return (f" ⚠ {len(movers)} order(s) now finish later than before: "
            + "; ".join(parts) + more + ".")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_movement_note.py -v`
Expected: PASS (four tests).

- [ ] **Step 5: Add the `_movement_note` composer**

In `api/main.py`, directly below `_format_movers`, add:

```python
def _movement_note(new_ranks):
    """Compare the winning plan to the currently-applied one (both replayed on
    today's book) and summarize which orders now finish later. '' if none.
    Must be called BEFORE _optimize_apply() persists the new ranks."""
    config = _resolve_config(_load_plan_config())
    masters = _current_masters()
    setup = optimize_service.prepare_contest(
        book_store.load_active_orders(), book_store.load_actuals(), masters, config,
        absences=book_store.load_absences(),
        operator_table=book_store.load_operator_table())
    prio = book_store.load_plan_priority()
    old_ranks = (prio or {}).get("ranks") or None
    old_sched, _ = _all_lines_schedule(setup, setup.masters, old_ranks)
    new_sched, _ = _all_lines_schedule(setup, setup.masters, new_ranks or None)
    movers = _movers(_expected_by_order(old_sched),
                     _expected_by_order(new_sched), _MOVE_LATER_THRESHOLD_DAYS)
    return _format_movers(movers)
```

- [ ] **Step 6: Wire it into the applied branch of `_auto_apply_result`**

In `api/main.py:_auto_apply_result`, the applied branch currently reads (lines ~1404-1411):

```python
    if optimizer.score(best) < optimizer.score(inc):
        meta = _optimize_apply()          # persists ranks + overlap + inputs_sig + book_sig
        ov = res.get("best_overlap"); cur = res.get("current_overlap")
        word = "chunks" if res.get("knob") == "flow_chunks" else "overlap"
        ov_txt = f", {word} {cur} → {ov}" if ov != cur else ""
        _auto_note_write(f"Plan auto-re-optimized {stamp}: "
                         f"{best['total_late_days']} late-days "
                         f"(was {inc['total_late_days']}){ov_txt}.")
```

Replace it with (compute the note BEFORE apply persists the new ranks):

```python
    if optimizer.score(best) < optimizer.score(inc):
        try:
            move = _movement_note(res.get("ranks") or None)
        except Exception:  # noqa: BLE001 - the note is advisory; never block an apply
            move = ""
        meta = _optimize_apply()          # persists ranks + overlap + inputs_sig + book_sig
        ov = res.get("best_overlap"); cur = res.get("current_overlap")
        word = "chunks" if res.get("knob") == "flow_chunks" else "overlap"
        ov_txt = f", {word} {cur} → {ov}" if ov != cur else ""
        _auto_note_write(f"Plan auto-re-optimized {stamp}: "
                         f"{best['total_late_days']} late-days "
                         f"(was {inc['total_late_days']}){ov_txt}.{move}")
```

- [ ] **Step 7: Run the note + auto-apply tests**

Run: `pytest tests/test_movement_note.py tests/test_optimize_service.py -v`
Expected: PASS (note unit tests green; existing auto-apply tests still green — the note is a pure suffix).

- [ ] **Step 8: Commit**

```bash
git add api/main.py tests/test_movement_note.py
git commit -m "feat(auto-optimize): Thursday 'what moved later' note

After an auto-apply, name the orders that now finish later than the plan
that was on screen, so slips are surfaced the same week instead of found by
hand. Pure reporting, computed from two schedules already built at apply time."
```

---

### Task 4: Behaviour regression on the sample workbook + full suite

**Files:**
- Test: `tests/test_promise_protection.py` (append an end-to-end guard)
- Modify (if needed): existing optimizer/new-engine tests whose *expected* values shift because the improved objective now picks a different (better) sequence.

**Interfaces:**
- Consumes: `tests/new_sample_workbook.py` (the code-generated Test5-format book), `engine.new_engine.optimize_sequence`.

- [ ] **Step 1: Write the end-to-end guard**

Append to `tests/test_promise_protection.py`. This runs the real new-engine sequence search on the code-generated sample book (same load pattern as `tests/test_new_engine.py`: seed the workbook into the store, load via `loaders.load_all`) at a fixed seed/budget, and asserts the search completes and its reported metrics carry the convex guard field end-to-end. (A value-robust guard — it holds for any `severity_weight >= 0`, so Task 5's tuning can't break it.)

```python
import io
from datetime import date

from engine import book_store, loaders, new_engine
from engine.config import Config
from tests.new_sample_workbook import build_new_sample_bytes


def test_new_engine_sequence_search_runs_reputation_aware():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)                 # new_engine reads masters from the store
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    config = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
                    apply_operator_logic=True)
    res = new_engine.optimize_sequence(so_lines, config, masters,
                                       budget_evals=60, seed=42)
    # The search produced a ranked plan and reported metrics including the guard.
    assert res.ranks
    assert res.best["total_late_days"] >= 0
    # slip_severity is present on the reported metrics (mirror wired end-to-end):
    assert "slip_severity" in res.best
```

- [ ] **Step 2: Run the guard to verify it fails**

Run: `pytest tests/test_promise_protection.py::test_new_engine_sequence_search_runs_reputation_aware -v`
Expected: FAIL on `assert "slip_severity" in res.best` if Task 2's `plan_metrics` change hasn't propagated (the field flows through `new_engine.optimize_sequence` → `plan_metrics`).

- [ ] **Step 3: Make it pass**

No new product code should be required if Tasks 1-2 are complete (the field flows through `new_engine.optimize_sequence` → `plan_metrics`). If the factory names differ, correct only the imports in the test.

- [ ] **Step 4: Run the FULL suite**

Run: `pytest -q`
Expected: PASS. Likely-affected files if any fail: `tests/test_optimizer.py`, `tests/test_new_engine.py`, `tests/test_optimize_service.py` — because a better objective can now pick a *different, better* sequence, so a test asserting an exact best sequence/metric may shift. For each failure: confirm the new value is genuinely better or equal on late-days/makespan (not a regression), then update the expected value **with a one-line comment** explaining the objective change. Do **not** loosen a test to hide a real regression.

- [ ] **Step 5: Confirm the golden trace is unchanged**

Run: `pytest -k golden -v`
Expected: PASS with no regen needed — the classic engine's rule output is untouched. If it fails, STOP: something changed the classic path that shouldn't have.

- [ ] **Step 6: Commit**

```bash
git add tests/
git commit -m "test: end-to-end reputation-aware guard + suite updates for the convex objective"
```

---

### Task 5: Measure and lock the weights on the real book (owner-in-the-loop)

**Files:**
- Modify: `ppc_engine/config.py` + `engine/optimizer.py` (final tuned `severity_tolerance_days`/`severity_weight`/`severity_cap_days` == `SEVERITY_*`; possibly reduced `fairness_weight`)
- Create: `scripts/measure_severity_sweep.py` (a throwaway measurement harness)
- Modify: `docs/superpowers/specs/2026-07-24-promise-protection-convex-fairness-design.md` (record the chosen values + the before/after numbers)

**Interfaces:**
- Consumes: the real uploaded book (gitignored — the **owner runs this step**, or provides the workbook), `engine.new_engine.optimize_sequence`, `optimizer.plan_metrics`.

- [ ] **Step 1: Reproduce the failure first**

Write `scripts/measure_severity_sweep.py` that loads the real book at the state that produced Aug 8 → Aug 23 (or the current book), runs `optimize_sequence` with `severity_weight=0` (old behaviour), and prints the worst single-order slip on savable orders plus total late-days and makespan. Confirm it reproduces an *avoidable* big slip. No fix is trusted until the bug is seen.

Run: `python scripts/measure_severity_sweep.py --severity-weight 0`
Expected: a plan whose worst savable-order slip is large (the reproduced bug).

- [ ] **Step 2: Sweep the weights**

Extend the script to loop a small grid — `severity_tolerance_days ∈ {2,3,5}`, `severity_weight ∈ {1,2,4,8}`, `severity_cap_days ∈ {20,30}` — and for each print: **worst savable-order slip**, **total late-days**, **makespan**. Also try `fairness_weight ∈ {30, 0}` to settle whether the max term still earns its place.

Run: `python scripts/measure_severity_sweep.py --sweep`
Expected: a table. Pick the point that **eliminates the avoidable big slips** while keeping total late-days and makespan within a small tolerance of today's baseline (≈ 1528 / 72.7 d). This is the dominance point, exactly like `MAKESPAN_WEIGHT = 40`.

- [ ] **Step 3: Set the final values in both files**

Update `ppc_engine/config.py` and the `SEVERITY_*` constants in `engine/optimizer.py` to the chosen values (kept equal). If the sweep showed `fairness_weight=0` is as good or better, set it to 0 with a comment; otherwise leave 30. Update the code comments and the spec with the chosen numbers and the measured before/after.

- [ ] **Step 4: Re-run the full suite at the final values**

Run: `pytest -q`
Expected: PASS. Re-tune any expected-value tests only if the *final* weights shifted a sample-book sequence (same rule as Task 4 Step 4).

- [ ] **Step 5: Delete the throwaway harness and commit**

```bash
git rm scripts/measure_severity_sweep.py
git add ppc_engine/config.py engine/optimizer.py docs/superpowers/specs/2026-07-24-promise-protection-convex-fairness-design.md tests/
git commit -m "chore: lock measured convex-guard weights (real-book sweep)

Records the tuned severity_tolerance_days/severity_weight/severity_cap_days
(== engine/optimizer SEVERITY_*) and the before/after worst-slip + aggregate
numbers in the spec. fairness_weight decision recorded in-comment."
```

---

## Deployment (after Task 5)

Standard: `pytest` green → Render dashboard → **Manual Deploy → Deploy latest commit** (auto-deploy is ON per the latest memory, but confirm). No env var, no schema, no UI change beyond the richer auto-note text. Watch the first Thursday auto-optimize note for the "what moved later" line.
