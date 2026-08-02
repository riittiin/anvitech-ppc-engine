# Operator Assignment as an Optimize Dimension — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the Optimize contest sweep the operator-assignment policy (`scarce` vs `balanced`) as a fourth search dimension and keep the winner, so the plan uses whichever way of matching operators to machines delivers the fewest late days.

**Architecture:** Mirrors the 2026-07-29 machine-set dimension exactly. A new optimizer-owned `Config.operator_pick` field flows through `new_engine._plan_config` into the live ppc scheduler (which already implements the three policies). The contest gains an operator-pick axis in `optimize_service.contest_jobs`; the winner persists and replays like the tuned overlap and machine-set. No scheduler, rule, or scoring logic changes — the policies already exist and are feasible by construction.

**Tech Stack:** Python, FastAPI, pytest. The vendored `ppc_engine/` package is the live scheduler; `engine/` is the adapter/optimizer/API layer.

**Spec:** `docs/superpowers/specs/2026-08-02-operator-assignment-optimize-dimension-design.md`

## Global Constraints

- **Default `operator_pick="scarce"` is byte-identical to today.** The golden trace and all ~690 existing tests must stay green.
- **New engine only.** `scheduler in ("classic","flow")` must see only `"scarce"` — the contest never sweeps the dimension for them, and their plans are unchanged.
- **Optimizer-owned, never a user knob.** `operator_pick` is read-only in the UI, set only when Optimize applies a winner, and replayed by every subsequent Plan (like `overlap_percent`/`flexible_machines`).
- **Swept policies:** `OPERATOR_PICK_CANDIDATES = ("scarce", "balanced")`. `flexible` stays implemented and unit-tested but is NOT in the contest.
- **Feasibility is free:** any policy picks only among free, qualified, on-shift operators, so no policy can produce an infeasible plan. No scoring/objective term is added.
- **Commit** after each task with the repo's footer:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
  Do **not** push — the owner pushes and deploys manually.
- **Run tests** with `pytest` from the repo root.

---

### Task 1: `Config.operator_pick` field + validation

**Files:**
- Modify: `engine/config.py` (add field after `flexible_machines` at line 108; validation in `validate()`; blank-coercion in `from_dict`)
- Test: `tests/test_operator_pick_dimension.py` (create)

**Interfaces:**
- Produces: `Config.operator_pick: str` (default `"scarce"`), validated to `{"scarce","balanced","flexible"}`, round-trips via the existing `to_dict`/`from_dict`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_operator_pick_dimension.py`:

```python
"""Operator-assignment (operator_pick) as a 4th Optimize dimension (2026-08-02)."""
import json

import pytest

from engine.config import Config


def test_operator_pick_defaults_to_scarce():
    assert Config().operator_pick == "scarce"


def test_operator_pick_round_trips():
    c = Config(operator_pick="balanced")
    assert Config.from_dict(c.to_dict()).operator_pick == "balanced"
    # to_dict must carry it (no special-casing needed — it's a plain str field).
    assert c.to_dict()["operator_pick"] == "balanced"


def test_operator_pick_blank_coerces_to_scarce():
    assert Config.from_dict({"operator_pick": ""}).operator_pick == "scarce"
    assert Config.from_dict({"operator_pick": None}).operator_pick == "scarce"


def test_operator_pick_invalid_is_rejected():
    with pytest.raises(ValueError):
        Config(operator_pick="nope").validate()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_operator_pick_dimension.py -v`
Expected: FAIL (`test_operator_pick_invalid_is_rejected` fails because there is no validation yet; the round-trip passes only after the field exists — expect collection/attribute failures until Step 3).

- [ ] **Step 3: Add the field, validation, and blank-coercion**

In `engine/config.py`, immediately after the `flexible_machines: bool = False` field (line 108), add:

```python
    # How the OPTIMIZER matches a free operator to a machine for each shift (2026-08-02).
    # Swept by the Optimize contest like overlap_percent / flexible_machines — NOT a user
    # knob (read-only in Settings; set only when Optimize applies a winner). New engine
    # only; classic/flow ignore it. Values:
    #   "scarce"   : the LEAST-flexible free operator first (today's behaviour — keeps
    #                versatile people free for machines only they can run). Default.
    #   "balanced" : the LEAST-loaded free operator (spread work evenly), tie -> scarce.
    #   "flexible" : the MOST-flexible free operator (kept for A/B; NOT swept by the contest).
    operator_pick: str = "scarce"
```

In `validate()`, add this check next to the `flexible_machines` check (after line 189):

```python
        if self.operator_pick not in ("scarce", "balanced", "flexible"):
            errs.append("operator_pick must be 'scarce', 'balanced', or 'flexible'")
```

In `from_dict`, add blank-coercion alongside the existing `priority_window_days` block (after line 230):

```python
            if key == "operator_pick":
                # UI/legacy blanks -> the default policy (the optimizer owns this field).
                if value in (None, "", "none", "null"):
                    value = "scarce"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_operator_pick_dimension.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/config.py tests/test_operator_pick_dimension.py
git commit -m "feat(config): add optimizer-owned operator_pick knob (default scarce)"
```

---

### Task 2: optimizer constant, contender helper, SweepResult field

**Files:**
- Modify: `engine/optimizer.py` (add `OPERATOR_PICK_CANDIDATES` + `operator_pick_contenders` near `OVERLAP_CANDIDATES`/`sweep_contenders`, ~line 360–393; add `operator_pick` field to `SweepResult` at line 415)
- Test: `tests/test_operator_pick_dimension.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `optimizer.OPERATOR_PICK_CANDIDATES: tuple = ("scarce", "balanced")`
  - `optimizer.operator_pick_contenders(current="scarce", candidates=OPERATOR_PICK_CANDIDATES) -> list[str]` — current policy first, then the rest.
  - `optimizer.SweepResult.operator_pick: str = "scarce"` (the winning policy).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_operator_pick_dimension.py`:

```python
def test_operator_pick_candidates_are_scarce_and_balanced():
    from engine.optimizer import OPERATOR_PICK_CANDIDATES
    assert OPERATOR_PICK_CANDIDATES == ("scarce", "balanced")


def test_operator_pick_contenders_put_current_first():
    from engine.optimizer import operator_pick_contenders
    assert operator_pick_contenders("balanced")[0] == "balanced"
    assert operator_pick_contenders("scarce") == ["scarce", "balanced"]
    # An off-list current policy still joins its own contest, first.
    assert operator_pick_contenders("flexible")[0] == "flexible"
    assert set(operator_pick_contenders("flexible")) == {"flexible", "scarce", "balanced"}


def test_sweepresult_defaults_operator_pick_scarce():
    from engine.optimizer import SweepResult
    assert SweepResult().operator_pick == "scarce"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_operator_pick_dimension.py -k operator_pick_candidates or operator_pick_contenders or sweepresult -v`
Expected: FAIL (ImportError / AttributeError — names don't exist yet).

- [ ] **Step 3: Add the constant, helper, and field**

In `engine/optimizer.py`, after the `OVERLAP_CANDIDATES = (70, 80, 85, 88)` block (line 360), add:

```python
# The operator-assignment policies the contest sweeps (2026-08-02, new engine only).
# "flexible" is a valid engine policy but is dropped from the contest for cost — it is
# the inverse of "scarce" and rarely wins (see the operator-assignment design spec).
OPERATOR_PICK_CANDIDATES = ("scarce", "balanced")


def operator_pick_contenders(current="scarce", candidates=OPERATOR_PICK_CANDIDATES):
    """The operator-pick contest lineup: the CURRENT policy first (Stop-safety + tie
    privilege), then the remaining candidates in order. An off-list current policy
    still joins its own contest, first — mirroring sweep_contenders()."""
    cur = current or "scarce"
    return [cur] + [p for p in candidates if p != cur]
```

In the `SweepResult` dataclass, add the field right after `flexible_machines` (line 415):

```python
    operator_pick: str = "scarce"       # the winning operator-assignment policy (new engine)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_operator_pick_dimension.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/optimizer.py tests/test_operator_pick_dimension.py
git commit -m "feat(optimizer): operator_pick candidates, contender ordering, SweepResult field"
```

---

### Task 3: Wire `operator_pick` into the live engine (`_plan_config`)

This is the enabling fix — today `_plan_config` omits `operator_pick`, so production is stuck on `scarce`.

**Files:**
- Modify: `engine/new_engine.py` (`_plan_config`, line 178–191)
- Test: `tests/test_operator_pick_dimension.py` (append — fast wiring test) and `tests/test_new_engine.py` (append — behavioral decode test using its fixtures)

**Interfaces:**
- Consumes: `Config.operator_pick` (Task 1).
- Produces: `_plan_config(config).operator_pick` equals `config.operator_pick`, so `decode`/`run`/`optimize_sequence`/`tune` all honour it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_operator_pick_dimension.py`:

```python
def test_plan_config_carries_operator_pick():
    from engine.new_engine import _plan_config
    assert _plan_config(Config()).operator_pick == "scarce"
    assert _plan_config(Config(operator_pick="balanced")).operator_pick == "balanced"
    assert _plan_config(Config(operator_pick="flexible")).operator_pick == "flexible"
```

Append to `tests/test_new_engine.py` (reusing its `old_book`/`new_masters` fixtures, `_orders_from_batches`, `_plan_config`, `decode`, `rule1_consolidate`, `_CONF`, all already imported at the top of that file):

```python
def test_operator_pick_changes_the_live_new_engine_plan(old_book, new_masters):
    """Behavioural proof the policy reaches the real scheduler (not just a field
    copy): scarce and flexible assign DIFFERENT operators on the fully-staffed
    sample, and each policy is deterministic. Verifies wiring in code, per the
    standing 'test behaviour before explaining it' lesson."""
    from dataclasses import replace as _replace
    so_lines, _ = old_book
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, _ = _orders_from_batches(batches, new_masters)
    seq = [(b.batch_id, b.item_code) for b in batches]

    def assignments(pick):
        cfg = _replace(_CONF, operator_pick=pick)
        sched = decode(orders, seq, new_masters, _plan_config(cfg))
        return {(s.order_key, s.op_seq, s.machine_id): s.operator
                for s in sched.segments if s.machine_id and s.operator}

    scarce = assignments("scarce")
    assert scarce == assignments("scarce"), "scarce pick is not deterministic"
    assert scarce != assignments("flexible"), "operator_pick had no effect on the plan"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_operator_pick_dimension.py::test_plan_config_carries_operator_pick tests/test_new_engine.py::test_operator_pick_changes_the_live_new_engine_plan -v`
Expected: FAIL (`_plan_config` returns a PlanConfig whose `operator_pick` is always the ppc default `"scarce"`, so the balanced/flexible assertions fail).

- [ ] **Step 3: Pass `operator_pick` through `_plan_config`**

In `engine/new_engine.py`, inside the `PlanConfig(...)` constructor in `_plan_config` (after the `consolidation_window=0.0,` line, ~line 187), add:

```python
        operator_pick=getattr(config, "operator_pick", "scarce"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_operator_pick_dimension.py::test_plan_config_carries_operator_pick tests/test_new_engine.py::test_operator_pick_changes_the_live_new_engine_plan -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/new_engine.py tests/test_operator_pick_dimension.py tests/test_new_engine.py
git commit -m "feat(new_engine): thread operator_pick into PlanConfig (was hard-wired to scarce)"
```

---

### Task 4: Sweep operator policies in the local fallback (`new_engine.sweep_optimize`)

**Files:**
- Modify: `engine/new_engine.py` (`sweep_optimize`, lines 500–532)
- Test: `tests/test_operator_pick_dimension.py` (append)

**Interfaces:**
- Consumes: `optimizer.operator_pick_contenders`, `Config.operator_pick`, `SweepResult.operator_pick` (Tasks 1–2).
- Produces: `sweep_optimize` runs `tune` once per `(flexible, operator_pick)` combination and returns a `SweepResult` whose `operator_pick` is the winner's policy.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_operator_pick_dimension.py`:

```python
def test_sweep_optimize_sweeps_all_operator_picks(monkeypatch):
    """The local fallback tries every (machine-set × operator-pick) pass and keeps
    the best. tune() is stubbed so the test is fast and deterministic."""
    from engine import new_engine

    calls = []

    def fake_tune(so_lines, config, masters, **kw):
        calls.append((config.flexible_machines, config.operator_pick))
        # Make "balanced" the strict winner so we can assert the returned policy.
        late = 10 if config.operator_pick == "scarce" else 5
        return ({("b", "i"): 0}, 80, {"total_late_days": late, "makespan_days": 0}, 5)

    monkeypatch.setattr(new_engine, "tune", fake_tune)
    res = new_engine.sweep_optimize(["x"], Config(scheduler="new"), object(),
                                    budget_evals=40)
    assert set(calls) == {(False, "scarce"), (True, "scarce"),
                          (False, "balanced"), (True, "balanced")}
    assert res.operator_pick == "balanced"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_operator_pick_dimension.py::test_sweep_optimize_sweeps_all_operator_picks -v`
Expected: FAIL (only `(False,"scarce")`/`(True,"scarce")` are explored; `res.operator_pick` is `scarce`).

- [ ] **Step 3: Add the operator-pick loop**

In `engine/new_engine.py`, replace the body of `sweep_optimize` (lines 505–532) with the version below. Changes: import `operator_pick_contenders`; nest the machine-set loop inside an operator-pick loop; carry `pick` in the `best` tuple and the returned `SweepResult`.

```python
    from dataclasses import replace
    from engine.optimizer import (OptimizeResult, SweepResult, score,
                                  operator_pick_contenders)

    per = max(15, int(budget_evals) // 10)
    best = None                       # (ranks, overlap_pct, metrics, plans, flex, pick)
    offset = {"n": 0}
    picks = operator_pick_contenders(getattr(config, "operator_pick", "scarce"))

    for pick in picks:
        for flex in (False, True):
            def _step(plans, _best, _flex=flex):
                if on_progress:
                    on_progress(offset["n"] + plans, (best or (None,) * 3)[2] if best else {})
            cfg = replace(config, flexible_machines=flex, operator_pick=pick)
            ranks, overlap_pct, metrics, plans = tune(so_lines, cfg, masters,
                                                      budget_per_eval=per, seed=seed, on_step=_step,
                                                      reserved=base_reserved, frozen=frozen)
            offset["n"] += plans
            if ranks and (best is None or score(metrics) < score(best[2])):
                best = (ranks, overlap_pct, metrics, plans, flex, pick)

    if best is None:
        return SweepResult(overlap_percent=int(round(_plan_config(config).overlap * 100)),
                           knob="overlap", flexible_machines=False,
                           operator_pick=getattr(config, "operator_pick", "scarce"),
                           result=OptimizeResult(evals=0, best=None), table=[], evals=offset["n"],
                           cancelled=False)
    ranks, overlap_pct, metrics, plans, flex, pick = best
    result = OptimizeResult(ranks=ranks, best=metrics, evals=offset["n"], improved=True, cancelled=False)
    return SweepResult(overlap_percent=overlap_pct, knob="overlap", flexible_machines=flex,
                       operator_pick=pick, result=result, table=[], evals=offset["n"], cancelled=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_operator_pick_dimension.py::test_sweep_optimize_sweeps_all_operator_picks -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/new_engine.py tests/test_operator_pick_dimension.py
git commit -m "feat(new_engine): local sweep tries every operator policy, keeps the best"
```

---

### Task 5: Operator-pick axis in the contest service

**Files:**
- Modify: `engine/optimize_service.py` (`pick_winner` 266–277, `run_candidate` 280–306, `_pool_run` 318–331, `contest_jobs` 334–348, `_run_jobs` 351–390, `merge_shard_rows` 393–410; add `local_contest_multiplier` helper)
- Test: `tests/test_operator_pick_dimension.py` (append)

**Interfaces:**
- Consumes: `optimizer.operator_pick_contenders`, `optimizer.OPERATOR_PICK_CANDIDATES`.
- Produces:
  - `contest_jobs(payload) -> list[tuple[int,bool,str]]` — `(overlap, flexible, operator_pick)` triples.
  - `run_candidate(payload, overlap, flexible=False, operator_pick="scarce", *, on_progress=None, should_cancel=None) -> dict` — row now includes `"pick"`.
  - `pick_winner(current_overlap, current_flexible, current_pick, rows) -> dict|None`.
  - `merge_shard_rows(...)` result dict now includes `"winner_pick"`.
  - `local_contest_multiplier(config) -> int` — `2 * len(OPERATOR_PICK_CANDIDATES)` for the new engine, else `1`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_operator_pick_dimension.py`:

```python
def _payload(scheduler="new", overlap=50):
    from engine.config import Config
    cfg = Config(scheduler=scheduler, overlap_percent=overlap)
    return {"config": cfg.to_dict(), "candidates": (70, 80)}


def test_contest_jobs_sweeps_operator_pick_for_new_engine():
    from engine import optimize_service
    jobs = optimize_service.contest_jobs(_payload("new"))
    assert all(len(t) == 3 for t in jobs)
    picks = {pick for (_ov, _flex, pick) in jobs}
    assert picks == {"scarce", "balanced"}
    # sequence contenders (current 50 + 70 + 80) × machine-sets(2) × picks(2)
    assert len(jobs) == 3 * 2 * 2


def test_contest_jobs_classic_stays_scarce_single_pass():
    from engine import optimize_service
    jobs = optimize_service.contest_jobs(_payload("classic"))
    assert {pick for (_ov, _flex, pick) in jobs} == {"scarce"}
    assert all(flex is False for (_ov, flex, _pick) in jobs)


def test_pick_winner_tie_prefers_current_pick():
    from engine import optimize_service
    m = {"total_late_days": 5, "makespan_days": 0}
    rows = [
        {"overlap": 80, "flexible": False, "pick": "balanced", "eligible": True, "best": m},
        {"overlap": 80, "flexible": False, "pick": "scarce", "eligible": True, "best": m},
    ]
    win = optimize_service.pick_winner(80, False, "scarce", rows)
    assert win["pick"] == "scarce"


def test_merge_shard_rows_carries_winner_pick():
    from engine import optimize_service
    rows = [{"overlap": 80, "flexible": True, "pick": "balanced", "eligible": True,
             "best": {"total_late_days": 1, "makespan_days": 0}, "evals": 5, "ranks": {}}]
    out = optimize_service.merge_shard_rows(_payload("new"), rows, 5, False)
    assert out["winner_pick"] == "balanced"
    assert "pick" in out["rows"][0]


def test_local_contest_multiplier():
    from engine import optimize_service
    from engine.config import Config
    assert optimize_service.local_contest_multiplier(Config(scheduler="new")) == 4
    assert optimize_service.local_contest_multiplier(Config(scheduler="classic")) == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_operator_pick_dimension.py -k "contest_jobs or pick_winner or merge_shard or multiplier" -v`
Expected: FAIL (triples are 2-tuples today; `pick_winner` takes 3 args; no `winner_pick`; no `local_contest_multiplier`).

- [ ] **Step 3: Add the operator-pick axis**

In `engine/optimize_service.py`:

**`pick_winner`** (replace lines 266–277):

```python
def pick_winner(current_overlap, current_flexible, current_pick, rows):
    """Best score wins; an exact tie keeps the current (overlap, machine-set, pick)."""
    def _is_current(r):
        return (r.get("overlap") == current_overlap
                and bool(r.get("flexible")) == bool(current_flexible)
                and r.get("pick", "scarce") == current_pick)
    ordered = sorted(rows, key=lambda r: (not _is_current(r), r.get("overlap")))
    best = None
    for r in ordered:
        if not r.get("eligible") or r.get("best") is None:
            continue
        if best is None or optimizer.score(r["best"]) < optimizer.score(best["best"]):
            best = r
    return best
```

**`run_candidate`** (change the signature at line 280, the `cfg` at line 298, and the return at line 305):

```python
def run_candidate(payload: dict, overlap: int, flexible: bool = False,
                  operator_pick: str = "scarce", *, on_progress=None,
                  should_cancel=None) -> dict:
```

Inside it, replace the `cfg = replace(...)` line (298) with:

```python
    cfg = replace(setup.search_config, flexible_machines=bool(flexible),
                  operator_pick=str(operator_pick), **{knob: int(overlap)})
```

and the return (305–306) with:

```python
    return {"overlap": int(overlap), "flexible": bool(flexible),
            "pick": str(operator_pick), "eligible": True,
            "best": res.best, "evals": res.evals, "ranks": res.ranks,
            "cancelled": res.cancelled}
```

**`_pool_run`** (replace lines 318–331):

```python
def _pool_run(args):
    payload, overlap, flexible, pick = args
    last = {"evals": 0}

    def cb(evals, _best):
        delta, last["evals"] = evals - last["evals"], evals
        c = _POOL["counter"]
        if c is not None:
            with c.get_lock():
                c.value += delta

    stop = _POOL["stop"]
    return run_candidate(payload, overlap, flexible, pick, on_progress=cb,
                         should_cancel=(lambda: bool(stop.value)) if stop else None)
```

**`contest_jobs`** (replace the body's last two statements, lines 342–348):

```python
    # The machine-set + operator-pick dimensions only affect the new engine — gate
    # both on scheduler so classic/flow cloud contests stay single-pass and byte-
    # identical to their local counterpart.
    is_new = getattr(config, "scheduler", "classic") == "new"
    machine_sets = (False, True) if is_new else (False,)
    picks = (optimizer.operator_pick_contenders(getattr(config, "operator_pick", "scarce"))
             if is_new else ("scarce",))
    return [(ov, flex, pick)
            for pick in picks
            for flex in machine_sets
            for ov in contenders]
```

**`_run_jobs`** (update both branches to unpack triples). Replace the sequential loop header (line 358) and the `run_candidate` call (368):

```python
        for ov, flex, pick in pairs:
```
```python
            row = run_candidate(payload, ov, flex, pick, on_progress=cb,
                                should_cancel=should_cancel)
```

and the subprocess `jobs` list (line 377):

```python
        jobs = [(payload, ov, flex, pick) for ov, flex, pick in pairs]
```

**`merge_shard_rows`** (update the winner call and both returns, lines 397–410):

```python
    config = Config.from_dict(payload["config"])
    knob, _ = optimizer.knob_for(config)
    cur_value = getattr(config, knob)
    cur_flex = bool(getattr(config, "flexible_machines", False))
    cur_pick = getattr(config, "operator_pick", "scarce")
    winner = pick_winner(cur_value, cur_flex, cur_pick, rows)
    table = [{k: r[k] for k in ("overlap", "flexible", "pick", "eligible", "best", "evals")
              if k in r} for r in rows]
    if winner is None:
        return {"winner_overlap": cur_value, "winner_flexible": cur_flex,
                "winner_pick": cur_pick, "rows": table,
                "knob": knob, "best": None, "ranks": {}, "evals": evals,
                "cancelled": cancelled}
    return {"winner_overlap": winner["overlap"], "winner_flexible": bool(winner["flexible"]),
            "winner_pick": winner.get("pick", "scarce"),
            "rows": table, "knob": knob, "best": winner["best"],
            "ranks": winner.get("ranks", {}), "evals": evals, "cancelled": cancelled}
```

**`local_contest_multiplier`** — add this new function directly below `cloud_budget` (after line 79):

```python
def local_contest_multiplier(config) -> int:
    """How many (machine-set x operator-pick) passes new_engine.sweep_optimize runs
    locally. The API multiplies sweep_total_evals (which counts overlap contenders
    only) by this to size the progress bar. Classic/flow run a single pass."""
    if getattr(config, "scheduler", "classic") == "new":
        return 2 * len(optimizer.OPERATOR_PICK_CANDIDATES)
    return 1
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_operator_pick_dimension.py -v && pytest tests/test_optimize_service.py -v`
Expected: PASS (new tests pass; existing optimize_service tests still green — update any that call `pick_winner`/`run_candidate` positionally by adding the new arg if they fail).

- [ ] **Step 5: Commit**

```bash
git add engine/optimize_service.py tests/test_operator_pick_dimension.py
git commit -m "feat(optimize_service): sweep operator_pick as a 4th contest dimension"
```

---

### Task 6: Carry the winner through finalize / apply / status / worker-result (api/main.py)

**Files:**
- Modify: `api/main.py` (`_metrics_for_ranks` 1634; `_finalize_optimize` 1463–1517; local `_finalize_optimize` call 1305–1309; the four `_mult` sites 1260, 1381, 1421, 1440; `_optimize_status` return 1526–1544; `_optimize_apply` 1783–1793; `WorkerResult` model 2343; `optimize_result_ep` 2416–2420; `_finalize_from_shards` call 2477–2482)
- Test: `tests/test_operator_pick_dimension.py` (append)

**Interfaces:**
- Consumes: `SweepResult.operator_pick`, `merge_shard_rows`'s `winner_pick`, `optimize_service.local_contest_multiplier`, `Config.operator_pick`.
- Produces: the applied plan config carries `operator_pick`; the staleness signature reflects it; the progress budget accounts for it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_operator_pick_dimension.py`:

```python
def test_inputs_signature_reflects_operator_pick():
    from api import main as m
    from engine.config import Config
    base = m._inputs_signature(Config())
    assert m._inputs_signature(Config(operator_pick="balanced")) != base


def test_apply_persists_the_winning_operator_pick(monkeypatch):
    from api import main as m
    from engine.config import Config
    saved = {}
    monkeypatch.setattr(m, "_load_plan_config", lambda: Config(scheduler="new"))
    monkeypatch.setattr(m, "_incumbent_metrics",
                        lambda: {"max_late_days": 100, "max_committed_slip": 0,
                                 "total_late_days": 100})
    monkeypatch.setattr(m, "_current_book_sig", lambda: "bs")
    monkeypatch.setattr(m.book_store, "save_plan_priority", lambda *a, **k: None)
    monkeypatch.setattr(m.book_store, "save_plan_config",
                        lambda s: saved.update(cfg=json.loads(s)))
    # The schedule-snapshot block is wrapped in try/except; force it to bail early.
    monkeypatch.setattr(m.book_store, "load_active_orders",
                        lambda: (_ for _ in ()).throw(RuntimeError("skip snapshot")))
    m._OPTIMIZE.update(state="done", started_mono=0.0, result={
        "ranks": {"b\x1fi": 0}, "best": {"total_late_days": 10, "max_committed_slip": 0},
        "baseline": {}, "budget": "deep", "seed": 1, "inputs_sig": "x",
        "best_overlap": 85, "current_overlap": 50, "knob": "overlap_percent",
        "flexible_machines": True, "operator_pick": "balanced"})
    m._optimize_apply()
    assert saved["cfg"]["operator_pick"] == "balanced"
    assert saved["cfg"]["flexible_machines"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_operator_pick_dimension.py -k "inputs_signature or apply_persists" -v`
Expected: `test_inputs_signature_reflects_operator_pick` PASSES already (to_dict folds the field in automatically — keep it as a regression guard); `test_apply_persists_the_winning_operator_pick` FAILS (apply doesn't read/persist `operator_pick` yet).

- [ ] **Step 3: Thread `operator_pick` through api/main.py**

**`_metrics_for_ranks`** (line 1634) — add the param and apply it. Change the signature:

```python
def _metrics_for_ranks(ranks, overlap=None, flexible=None, operator_pick=None, *,
                       with_distribution=True):
```

and add, right after the `if flexible is not None:` block (after line 1648):

```python
        if operator_pick is not None:
            config = replace(config, operator_pick=str(operator_pick))
```

**`_finalize_optimize`** (line 1463) — add the param, pass it to the recompute, fold it into the fingerprint, and record it. Change the signature (1463–1465):

```python
def _finalize_optimize(job_id, base_config, real_baseline, label, *,
                       winner_overlap, winner_flexible=False, winner_pick="scarce",
                       ranks, best, evals, table, cancelled):
```

Change the recompute call (1476):

```python
        _local_best = _metrics_for_ranks(ranks, winner_overlap, winner_flexible, winner_pick)
```

Change the inputs_sig `replace` (1485–1487):

```python
    inputs_sig = _inputs_signature(replace(base_config,
                                           flexible_machines=bool(winner_flexible),
                                           operator_pick=str(winner_pick),
                                           **{_knob: winner_overlap}))
```

Add two keys to the `result=` dict (after the `"current_flexible": ...` line, 1504):

```python
                    "operator_pick": str(winner_pick),
                    "current_operator_pick": getattr(base_config, "operator_pick", "scarce"),
```

**Local job finalize call** (1305–1309) — pass the swept policy:

```python
            _finalize_optimize(job_id, base_config, real_baseline, label,
                               winner_overlap=sw.overlap_percent,
                               winner_flexible=sw.flexible_machines,
                               winner_pick=sw.operator_pick, ranks=res.ranks,
                               best=res.best, evals=sw.evals, table=sw.table,
                               cancelled=sw.cancelled)
```

**The four local-fallback `_mult` sites** (1260, 1381, 1421, 1440) — replace each two-line `_mult = 2 if ... else 1` assignment with the helper (the surrounding `_knob`/`_k`/`_kc` lookups stay). Each becomes:

```python
            _mult = optimize_service.local_contest_multiplier(setup.search_config)
```

(At 1260 the variable is used on the next line's `denom`; at 1381/1421/1440 it feeds `_OPTIMIZE["budget_evals"]`. Only the `_mult = ...` line changes — keep everything else.)

**`_optimize_status`** (add two keys after `"current_flexible": ...`, line 1534):

```python
                "operator_pick": res.get("operator_pick"),
                "current_operator_pick": res.get("current_operator_pick"),
```

**`_optimize_apply`** (1783–1793) — read and persist the winning policy. After `best_flex = res.get("flexible_machines")` (1784) add:

```python
        best_pick = res.get("operator_pick")
```

and after the `if best_flex is not None:` block (1790–1791) add:

```python
        if best_pick is not None:
            target = replace(target, operator_pick=str(best_pick))
```

**`WorkerResult`** (line 2343) — add a field:

```python
    winner_pick: Optional[str] = "scarce"
```

**`optimize_result_ep`** finalize call (2416–2420) — pass it:

```python
    stored = _finalize_optimize(req.job_id, base_config, baseline, label,
                                winner_overlap=req.winner_overlap,
                                winner_flexible=bool(req.winner_flexible),
                                winner_pick=(req.winner_pick or "scarce"),
                                ranks=req.ranks, best=req.best, evals=req.evals,
                                table=req.rows, cancelled=req.cancelled)
```

**`_finalize_from_shards`** finalize call (2477–2482) — pass the merged winner:

```python
    _finalize_optimize(job_id, base_config, baseline, label,
                       winner_overlap=merged["winner_overlap"],
                       winner_flexible=bool(merged["winner_flexible"]),
                       winner_pick=merged.get("winner_pick", "scarce"),
                       ranks=merged["ranks"], best=merged["best"],
                       evals=merged["evals"], table=merged["rows"],
                       cancelled=merged["cancelled"])
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_operator_pick_dimension.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/main.py tests/test_operator_pick_dimension.py
git commit -m "feat(api): persist/replay the winning operator_pick; size progress for the new dimension"
```

---

### Task 7: Post the winning policy from the cloud worker (non-shard path)

The live 20-way matrix path posts raw rows (which now carry `"pick"`) and needs no change; only the single-worker `/optimize/result` path posts a pre-merged winner.

**Files:**
- Modify: `scripts/cloud_optimize_worker.py` (result post, lines 126–130)
- Test: covered by Task 5's `test_merge_shard_rows_carries_winner_pick` / `run_contest` returning `winner_pick`; add a one-line assertion here.

**Interfaces:**
- Consumes: `run_contest(...)["winner_pick"]`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_operator_pick_dimension.py`:

```python
def test_run_contest_result_exposes_winner_pick(monkeypatch):
    """The single-worker path posts run_contest(...)['winner_pick']; guarantee the key
    exists so scripts/cloud_optimize_worker.py can forward it."""
    from engine import optimize_service
    monkeypatch.setattr(optimize_service, "contest_jobs", lambda p: [])
    out = optimize_service.run_contest(_payload("new"))
    assert "winner_pick" in out
```

- [ ] **Step 2: Run test to verify it passes-or-fails**

Run: `pytest tests/test_operator_pick_dimension.py::test_run_contest_result_exposes_winner_pick -v`
Expected: PASS already after Task 5 (merge_shard_rows adds `winner_pick`). This test locks the contract the worker relies on.

- [ ] **Step 3: Forward `winner_pick` in the worker**

In `scripts/cloud_optimize_worker.py`, in the `/optimize/result` POST body (lines 126–130), add the `winner_pick` line:

```python
        _call("POST", "/optimize/result", {
            "job_id": JOB_ID, "winner_overlap": out["winner_overlap"],
            "winner_flexible": out.get("winner_flexible", False),
            "winner_pick": out.get("winner_pick", "scarce"),
            "ranks": out["ranks"], "best": out["best"], "rows": out["rows"],
            "evals": out["evals"], "cancelled": out["cancelled"]})
```

- [ ] **Step 4: Run the test suite for the touched modules**

Run: `pytest tests/test_operator_pick_dimension.py tests/test_optimize_service.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/cloud_optimize_worker.py tests/test_operator_pick_dimension.py
git commit -m "feat(worker): forward the winning operator_pick on the single-worker result path"
```

---

### Task 8: Read-only "Operator strategy" line in Settings

**Files:**
- Modify: `web/index.html` (after the machine-set read-only `<p>`, line 168)
- Modify: `web/app.js` (after the machine-set echo, line 226)

**Interfaces:**
- Consumes: the `/run` response `config.operator_pick` (already emitted via `Config.to_dict`).

- [ ] **Step 1: Add the read-only line in `web/index.html`**

Immediately after the machine-set `<p class="cfg-readonly">...</p>` block (closing at line 168), add:

```html
          <p class="cfg-readonly">Operator strategy (tuned by Optimize — you don't set this):
            <span id="cfg-operatorpick-info">Save flexible people</span>. How the optimizer
            matches operators to machines to get the most orders out on time.</p>
```

- [ ] **Step 2: Reflect the saved value in `web/app.js`**

Immediately after the machine-set echo (line 226, `if (ms) ms.textContent = ...`), add:

```javascript
  const opk = $("cfg-operatorpick-info");
  if (opk) opk.textContent = ({
    scarce: "Save flexible people",
    balanced: "Spread work evenly",
    flexible: "Use flexible people first",
  })[cfg.operator_pick] || "Save flexible people";
```

- [ ] **Step 3: Verify in the browser**

Start the app (`uvicorn api.main:app --reload`), log in as admin, open Settings, and confirm the "Operator strategy" line renders and shows "Save flexible people" on a default config. (No unit test — this is a display-only echo; the value's correctness is covered by the backend tests.)

- [ ] **Step 4: Commit**

```bash
git add web/index.html web/app.js
git commit -m "feat(web): show the Optimize-chosen operator strategy read-only in Settings"
```

---

### Task 9: Full suite + Test8 before/after measurement

**Files:**
- Test: whole suite
- Create (scratchpad, not committed): a measurement script

- [ ] **Step 1: Run the full test suite**

Run: `pytest`
Expected: all pass (~690+ before this work; the new tests add to that). Investigate any regression before proceeding — a broken golden or new-engine test means the default is no longer byte-identical.

- [ ] **Step 2: Measure scarce-only vs operator-aware on Test8**

The owner's `Test8.xlsx` is gitignored real data. With it uploaded to a local instance (or loaded via `loaders.load_all`), run a local contest twice on the same book and plan-start:
1. Baseline: pin `OPERATOR_PICK_CANDIDATES = ("scarce",)` (temporarily) and run `optimizer.sweep_optimize` on the new engine.
2. New: restore `("scarce", "balanced")` and run again.

For each, record from `plan_metrics`: `makespan_days`, `total_late_days`, `max_late_days` (worst), the lateness bands, and **which `operator_pick` won**. Write the numbers into the design spec's "Test8 re-measurement" expectation and report them to the owner. This is the data that decides whether Approach 2 (bottleneck-first policy) is worth building.

- [ ] **Step 3: Report + hand off**

Summarize for the owner: did `balanced` ever beat `scarce`, by how much, and the measured wall-clock delta (so they can tune `OPTIMIZE_CLOUD_BUDGET_PER_CANDIDATE`). No commit for this task (measurement only).

---

## Self-Review

**Spec coverage:**
- §1 config field → Task 1. §2 `_plan_config` wiring → Task 3. §3 contest axis (contest_jobs/run_candidate/pick_winner/merge_shard_rows/local fallback) → Tasks 4–5. §4 apply/replay → Task 6. §5 progress budget → Task 6 (`local_contest_multiplier` + the four `_mult` sites). §6 UI → Task 8. Testing/edge cases → Tasks 1–7 + Task 9. Cost/measurement → Task 9. Deferred (Approach 2/3) → out of scope, unchanged. Worker parity → Task 7. ✓ All covered.
- `_inputs_signature`: no code change needed (the field auto-folds via `to_dict`); Task 6 adds the regression guard. ✓

**Placeholder scan:** No TBD/TODO; every step has concrete code and exact line anchors. ✓

**Type consistency:** `operator_pick: str` throughout; candidate triples `(overlap:int, flexible:bool, pick:str)` consistent across `contest_jobs`/`_run_jobs`/`_pool_run`/`run_candidate`; `pick_winner(current_overlap, current_flexible, current_pick, rows)` matches its sole caller `merge_shard_rows`; `SweepResult.operator_pick` matches its producer (`new_engine.sweep_optimize`) and consumer (`_finalize_optimize` via `sw.operator_pick`); `winner_pick` key consistent across `merge_shard_rows` → `_finalize_from_shards`/`WorkerResult`/`optimize_result_ep`. ✓
