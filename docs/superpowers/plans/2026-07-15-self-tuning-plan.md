# Self-Tuning Plan Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The plan re-optimizes itself whenever production changes the book, all
orders compete equally for all time under one law (no committed order ever ends
after its promised date), and operators can be marked absent in-app.

**Architecture:** Three phases, each independently shippable. Phase 1: a book
fingerprint + debounced background trigger that runs the existing cloud contest
and auto-applies strictly-better results. Phase 2: the contest searches ALL
orders jointly with a promise-ceiling veto; `_plan` re-validates on every
replay and falls back to the existing two-pass shape. Phase 3: absences stored
in the book store become operator blocked-intervals via Rule 6's existing
`reserved=` mechanism. Spec: `docs/superpowers/specs/2026-07-15-self-tuning-plan-design.md`.

**Tech Stack:** Python 3 / FastAPI / pytest; vanilla JS frontend; existing
cloud-optimize plumbing (`engine/optimize_service.py`, `scripts/cloud_optimize_worker.py`).

## Global Constraints

- All-open book, no absences, `AUTO_OPTIMIZE=0` ⇒ plans byte-identical to today; golden trace untouched (`REGEN_GOLDEN` never used in this plan).
- Rules stay pure; the order book + book_store are the only stateful layers; NO rule logic duplicated (reuse `run_forward`, `optimize_service`).
- Commit messages end with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- Run tests with `python3 -m pytest` (never `python`). Full suite green after every task.
- Score = `optimizer.score` (late-days + 10×makespan) — never reweighted.
- Promise comparison is DAY-level: `entry.end.date() <= promised_date`.
- Env knobs (exact names): `AUTO_OPTIMIZE` (default "1"), `AUTO_OPTIMIZE_QUIET_MIN` (default "10"), `AUTO_OPTIMIZE_SPACING_MIN` (default "60").
- Store keys (exact): `anvitech:absences`, `anvitech:auto_note`; plan-priority meta gains `book_sig` and `joint` fields.
- Branch: all work on `self-tuning-plan`.

---

## Phase 1 — Auto re-optimize

### Task 1: `book_signature` in optimize_service

**Files:**
- Modify: `engine/optimize_service.py` (add function at module level, after `reservations_from_schedule`)
- Test: `tests/test_optimize_service.py` (append)

**Interfaces:**
- Produces: `optimize_service.book_signature(so_lines, absences=None) -> str` (sha256 hex). Deterministic; changes when any order's remaining qty / per-process remaining / lane / promise / the absence list changes; insensitive to list order.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_optimize_service.py`)

```python
def test_book_signature_tracks_material_changes():
    orders, actuals, raw, masters, cfg = _book()
    from engine import orderbook
    lines = orderbook.active_so_lines(orders, actuals, masters)
    s0 = svc.book_signature(lines)
    assert s0 == svc.book_signature(list(reversed(lines)))   # order-insensitive
    lines[0].qty -= 1                                        # production happened
    assert svc.book_signature(lines) != s0
    lines[0].qty += 1
    lines[0].commitment = "committed"                        # lane change
    lines[0].promised_date = date(2025, 4, 1)
    assert svc.book_signature(lines) != s0
    lines[0].commitment, lines[0].promised_date = "open", None
    assert svc.book_signature(lines) == s0                   # restored ⇒ same sig
    assert svc.book_signature(lines, absences=[{"operator": "X",
        "from_date": "2025-03-02", "to_date": "2025-03-03"}]) != s0
```

- [ ] **Step 2: Run to verify failure**

Run: `python3 -m pytest tests/test_optimize_service.py::test_book_signature_tracks_material_changes -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'book_signature'`

- [ ] **Step 3: Implement** (in `engine/optimize_service.py`; add `import hashlib, json` to the imports)

```python
def book_signature(so_lines, absences=None):
    """Fingerprint of the BOOK state an optimization was computed on: which
    orders, how much work each still needs (headline + per-process), their
    lanes/promises, and the operator absences. When production moves any of
    these, an applied optimization is stale — the auto trigger compares this
    signature. (Masters + settings are covered by api._inputs_signature.)"""
    rows = sorted(
        (l.so_no, l.item_code, round(float(l.qty), 3),
         json.dumps(l.process_qty or {}, sort_keys=True, default=str),
         getattr(l, "commitment", "open") or "open",
         str(getattr(l, "promised_date", None)))
        for l in so_lines)
    blob = json.dumps([rows, sorted((a.get("operator", ""), a.get("from_date", ""),
                                     a.get("to_date", "")) for a in (absences or []))],
                      default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
```

- [ ] **Step 4: Run to verify pass**: same command, Expected: PASS
- [ ] **Step 5: Commit**: `git add -A && git commit -m "feat: book_signature — fingerprint of the book state an optimization was computed on"`

### Task 2: Test isolation — `AUTO_OPTIMIZE=0` everywhere by default

**Files:**
- Create: `tests/conftest.py` (if absent) or Modify: append fixture
- Test: the fixture itself is exercised by every later task

**Interfaces:**
- Produces: autouse fixture `_no_auto_optimize` setting `AUTO_OPTIMIZE=0` for every test; dedicated auto tests override with `monkeypatch.setenv("AUTO_OPTIMIZE", "1")` AFTER module reload.

- [ ] **Step 1: Check whether `tests/conftest.py` exists**: `ls tests/conftest.py` — create or append accordingly.
- [ ] **Step 2: Add the fixture**

```python
import os
import pytest


@pytest.fixture(autouse=True)
def _no_auto_optimize(monkeypatch):
    """The self-tuning trigger must never fire spontaneously inside tests —
    endpoints bump the book-changed state, and without this the api module
    would spawn background contest threads mid-suite. Dedicated auto tests
    re-enable with monkeypatch.setenv('AUTO_OPTIMIZE', '1')."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
```

- [ ] **Step 3: Run the full suite**: `python3 -m pytest -q` Expected: all pass (fixture is inert today).
- [ ] **Step 4: Commit**: `git commit -am "test: default AUTO_OPTIMIZE=0 in the suite (isolation for the coming trigger)"`

### Task 3: The trigger — `_bump_book_changed` + debounced auto loop

**Files:**
- Modify: `api/main.py` (new section after `_worker_secret_ok`, ~line 880)
- Test: Create `tests/test_auto_optimize.py`

**Interfaces:**
- Consumes: `_start_optimize(budget_evals, label, background)` (existing), `_cloud_config()` (existing), `optimize_service.book_signature` (Task 1), `book_store.load_plan_priority()` (existing — meta dict).
- Produces: `_bump_book_changed()` — call after any book mutation; `_AUTO` state dict; `_auto_note_write(text)` + `book_store.save_auto_note/load_auto_note`; `_start_optimize(..., auto=True)` keyword (behavior wired in Task 4); loop thread `_auto_loop` (daemon, singleton).

- [ ] **Step 1: Write failing tests** (`tests/test_auto_optimize.py`)

```python
"""Phase 1: the self-tuning trigger. Debounce (quiet window + spacing),
cloud-only rule, and the kill switch. The contest itself is stubbed."""
import time
from datetime import date

import pytest

pytest.importorskip("fastapi")
from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
        Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
    ])


def _auto_env(monkeypatch, quiet_s=0.2, spacing_s=0.5):
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    monkeypatch.setenv("AUTO_OPTIMIZE_QUIET_MIN", str(quiet_s / 60))
    monkeypatch.setenv("AUTO_OPTIMIZE_SPACING_MIN", str(spacing_s / 60))
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "manual")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3")


def test_bump_then_quiet_window_starts_one_auto_contest(monkeypatch):
    _auto_env(monkeypatch)
    m = _api()
    _seed_book()
    starts = []
    monkeypatch.setattr(m, "_start_optimize",
                        lambda budget_evals, label, background=True, auto=False:
                        starts.append((label, auto)))
    m._bump_book_changed()
    m._bump_book_changed()          # burst — still one contest
    time.sleep(0.35)                # quiet window (0.2 s) passes
    m._auto_tick()                  # deterministic tick instead of the thread
    assert starts == [("auto", True)]
    m._auto_tick()
    assert len(starts) == 1         # no re-fire without a new bump


def test_spacing_is_honored(monkeypatch):
    _auto_env(monkeypatch, quiet_s=0.05, spacing_s=10)
    m = _api()
    _seed_book()
    starts = []
    monkeypatch.setattr(m, "_start_optimize",
                        lambda *a, **k: starts.append(1))
    m._bump_book_changed(); time.sleep(0.1); m._auto_tick()
    m._bump_book_changed(); time.sleep(0.1); m._auto_tick()   # inside spacing
    assert len(starts) == 1


def test_auto_is_cloud_only(monkeypatch):
    _auto_env(monkeypatch)
    m = _api()
    _seed_book()
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN")               # no cloud
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    m._bump_book_changed(); time.sleep(0.35); m._auto_tick()
    assert starts == []
    assert "retry" in (book_store.load_auto_note() or {}).get("text", "")


def test_kill_switch(monkeypatch):
    m = _api()                                                # AUTO_OPTIMIZE=0 fixture
    _seed_book()
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    m._bump_book_changed(); time.sleep(0.35); m._auto_tick()
    assert starts == []


def test_no_fire_when_signature_matches_applied(monkeypatch):
    _auto_env(monkeypatch)
    m = _api()
    _seed_book()
    # Pretend an optimization was applied for exactly the current book state.
    sig = m._current_book_sig()
    book_store.save_plan_priority({}, {"saved_at": "t", "book_sig": sig})
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    m._bump_book_changed(); time.sleep(0.35); m._auto_tick()
    assert starts == []
```

- [ ] **Step 2: Run to verify failure**: `python3 -m pytest tests/test_auto_optimize.py -v` — Expected: FAIL (`_bump_book_changed` undefined).
- [ ] **Step 3: Implement in `api/main.py`.** Add to `engine/book_store.py`:

```python
AUTO_NOTE_KEY = "anvitech:auto_note"


def save_auto_note(note: dict) -> None:
    get_store().kv_set(AUTO_NOTE_KEY, json.dumps(note))


def load_auto_note():
    raw = get_store().kv_get(AUTO_NOTE_KEY)
    return json.loads(raw) if raw else None
```

Add to `api/main.py` (after `_worker_secret_ok`):

```python
# --------------------------------------------------------------------------- #
# Self-tuning trigger (spec 2026-07-15-self-tuning-plan): production changes
# the book -> after a quiet window (+ spacing), run the contest in the cloud
# and auto-apply strictly-better results. AUTO_OPTIMIZE=0 kills everything.
# --------------------------------------------------------------------------- #
_AUTO = {"changed_mono": None, "last_start_mono": 0.0, "thread": None}
_AUTO_LOCK = threading.Lock()


def _auto_enabled() -> bool:
    return os.environ.get("AUTO_OPTIMIZE", "1") != "0"


def _auto_quiet_s() -> float:
    return float(os.environ.get("AUTO_OPTIMIZE_QUIET_MIN", "10")) * 60


def _auto_spacing_s() -> float:
    return float(os.environ.get("AUTO_OPTIMIZE_SPACING_MIN", "60")) * 60


def _current_book_sig() -> str:
    masters = _current_masters()
    actuals = book_store.load_actuals()
    lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                      actuals, masters)
    return optimize_service.book_signature(lines,
                                           absences=book_store.load_absences()
                                           if hasattr(book_store, "load_absences")
                                           else None)


def _auto_note_write(text: str):
    book_store.save_auto_note({"text": text,
                               "at": datetime.now().isoformat(timespec="seconds")})


def _bump_book_changed():
    """Call after ANY action that changes the plannable book (punch, upload,
    delete, commitment, absence, settings save). Cheap — just stamps time."""
    if not _auto_enabled():
        return
    with _AUTO_LOCK:
        _AUTO["changed_mono"] = time.monotonic()
        if _AUTO["thread"] is None or not _AUTO["thread"].is_alive():
            t = threading.Thread(target=_auto_loop, daemon=True)
            _AUTO["thread"] = t
            t.start()


def _auto_tick() -> bool:
    """One decision: should an auto contest start now? (Split from the loop so
    tests drive it deterministically.) Returns True when one was started."""
    if not _auto_enabled():
        return False
    with _AUTO_LOCK:
        changed = _AUTO["changed_mono"]
        if changed is None:
            return False
        now = time.monotonic()
        if now - changed < _auto_quiet_s():
            return False                              # still inside the burst
        if now - _AUTO["last_start_mono"] < _auto_spacing_s():
            return False                              # spacing
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running":
            return False                              # one at a time
    if _cloud_config() is None:
        with _AUTO_LOCK:
            _AUTO["changed_mono"] = None
        _auto_note_write("Auto-optimize skipped — cloud compute unavailable; "
                         "will retry on the next change.")
        return False                                  # auto is cloud-only
    prio = book_store.load_plan_priority()
    applied_sig = ((prio or {}).get("meta") or {}).get("book_sig")
    try:
        cur_sig = _current_book_sig()
    except Exception:
        return False
    if applied_sig == cur_sig:
        with _AUTO_LOCK:
            _AUTO["changed_mono"] = None              # nothing actually changed
        return False
    with _AUTO_LOCK:
        _AUTO["changed_mono"] = None
        _AUTO["last_start_mono"] = time.monotonic()
    try:
        _start_optimize(_OPT_BUDGETS["deep"], "auto", background=True, auto=True)
        return True
    except HTTPException:
        return False                                  # e.g. nothing to optimize


def _auto_loop():
    """Daemon: checks the tick every 15 s while a change is pending."""
    while _auto_enabled():
        time.sleep(15)
        with _AUTO_LOCK:
            pending = _AUTO["changed_mono"] is not None
        if not pending:
            return
        _auto_tick()
```

Add the `auto=False` keyword to `_start_optimize(budget_evals, label, background=True, auto=False)` and stash it: in the `_OPTIMIZE.update(...)` running-state call add `auto=bool(auto)`; add `"auto": False` to the `_OPTIMIZE` init dict. Add `from datetime import datetime` if not imported (it is via `datetime` module usage — check; `_commit_orders` imports locally, so add `from datetime import datetime` at the top-level imports of the new code or reuse a local import inside `_auto_note_write`).

- [ ] **Step 4: Run**: `python3 -m pytest tests/test_auto_optimize.py -v` — Expected: PASS (5 tests). Note `test_no_fire_when_signature_matches_applied` needs `_current_book_sig` exactly as named.
- [ ] **Step 5: Full suite**: `python3 -m pytest -q` — Expected: all pass.
- [ ] **Step 6: Commit**: `git commit -am "feat: self-tuning trigger — book-change bump, quiet window + spacing debounce, cloud-only, kill switch"`

### Task 4: Auto-apply on contest completion

**Files:**
- Modify: `api/main.py` — `_finalize_optimize` (tail) + a new `_auto_apply_result`; `_optimize_apply` stores `book_sig` in meta.
- Test: append to `tests/test_auto_optimize.py`

**Interfaces:**
- Consumes: `_finalize_optimize(job_id, base_config, real_baseline, label, *, winner_overlap, ranks, best, evals, table, cancelled)` (existing), `pipeline.run_forward(plan, cfg, masters, priority_rank=)` + `pipeline.apply_priority_rank` (existing), `optimizer.plan_metrics`, `optimize_service.prepare_contest`.
- Produces: after a contest that was started with `auto=True` finishes, the result is applied iff strictly better than the incumbent (saved ranks replayed on today's book, or the plain plan when none); `anvitech:auto_note` written on both outcomes; `_optimize_apply()` (manual too) now also stores `book_sig` in `plan_priority` meta.

- [ ] **Step 1: Write failing tests** (append to `tests/test_auto_optimize.py`)

```python
def test_auto_contest_applies_only_when_strictly_better(monkeypatch):
    _auto_env(monkeypatch, quiet_s=0.05, spacing_s=0.05)
    m = _api()
    _seed_book()
    m._bump_book_changed(); time.sleep(0.1)
    assert m._auto_tick()                              # real contest, sample book
    t0 = time.time()
    while m._optimize_status()["state"] == "running" and time.time() - t0 < 60:
        time.sleep(0.05)
    st = m._optimize_status()
    assert st["state"] == "done"
    note = book_store.load_auto_note()
    saved = book_store.load_plan_priority()
    if saved:                                          # applied ⇒ strictly better + meta
        assert "auto" in note["text"].lower() or "re-optimized" in note["text"]
        assert saved["meta"]["book_sig"] == m._current_book_sig()
    else:                                              # not applied ⇒ honest note
        assert "still best" in note["text"]


def test_manual_apply_also_records_book_sig(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    m._start_optimize(budget_evals=15, label="deep", background=False)
    m._optimize_apply()
    assert book_store.load_plan_priority()["meta"]["book_sig"] == m._current_book_sig()
```

- [ ] **Step 2: Run to verify failure**: `python3 -m pytest tests/test_auto_optimize.py -k "auto_contest_applies or records_book_sig" -v` — Expected: FAIL (no book_sig in meta / no auto-apply).
- [ ] **Step 3: Implement.** In `_optimize_apply` (api/main.py ~1052) where meta is saved, add `"book_sig": _current_book_sig()` to the meta dict. Add:

```python
def _incumbent_metrics():
    """Score of the plan users currently see: the applied ranks (if any)
    replayed on TODAY'S book, else the plain plan. Expedite off when ranks
    replay — mirrors _plan."""
    config = _load_plan_config()
    masters = _current_masters()
    actuals = book_store.load_actuals()
    orders = book_store.load_active_orders()
    setup = optimize_service.prepare_contest(orders, actuals, masters, config)
    prio = book_store.load_plan_priority()
    ranks = (prio or {}).get("ranks") or None
    pr = PlanRun(so_lines=list(setup.target))
    run_forward(pr, setup.search_config if ranks else setup.config, masters,
                reserved=setup.reserved, priority_rank=ranks)
    return optimizer.plan_metrics(pr.schedule, setup.target,
                                  setup.config.plan_start_date)


def _auto_apply_result():
    """Called after an auto contest lands in state=done: apply iff strictly
    better than the incumbent; write the note either way."""
    with _OPTIMIZE_LOCK:
        res = _OPTIMIZE.get("result") or {}
        best = res.get("best")
    if not best:
        _auto_note_write("Auto-optimize finished with no plan — kept current.")
        return
    try:
        inc = _incumbent_metrics()
    except Exception as e:  # noqa: BLE001
        _auto_note_write(f"Auto-optimize finished but could not compare: {e}")
        return
    stamp = datetime.now().strftime("%H:%M")
    if optimizer.score(best) < optimizer.score(inc):
        meta = _optimize_apply()          # persists ranks + overlap + inputs_sig + book_sig
        ov = res.get("best_overlap"); cur = res.get("current_overlap")
        ov_txt = f", overlap {cur} → {ov}" if ov != cur else ""
        _auto_note_write(f"Plan auto-re-optimized {stamp} — "
                         f"{best['total_late_days']} late-days "
                         f"(was {inc['total_late_days']}){ov_txt}.")
    else:
        _auto_note_write(f"Checked {stamp} — current plan still best "
                         f"({inc['total_late_days']} late-days).")
```

At the END of `_finalize_optimize` (after the lock block, before `return True`), add:

```python
    if _OPTIMIZE.get("auto"):
        try:
            _auto_apply_result()
        except Exception:   # noqa: BLE001 — an auto note must never crash a result
            pass
```

(`datetime` import: `from datetime import datetime` — add at the imports block near `from datetime import date`.)

- [ ] **Step 4: Run**: `python3 -m pytest tests/test_auto_optimize.py -v` — Expected: PASS.
- [ ] **Step 5: Full suite** + **Step 6: Commit**: `git commit -am "feat: auto-apply — strictly-better-or-nothing, incumbent = replayed applied plan, note either way; apply records book_sig"`

### Task 5: Wire the bumps + surface the note

**Files:**
- Modify: `api/main.py` — call `_bump_book_changed()` at the end of the mutating endpoints: `/actuals` save handler, `/actuals/rollback`, `/upload`, `/orders/delete`, `/orders/clear`, `/orders/commit`, `/orders/urgent`, `/orders/uncommit`, and the `persist=True` branch of `/run`. `_plan` return dict gains `"auto_note": book_store.load_auto_note()`.
- Modify: `web/app.js` — render the note; `web/index.html` — a `#auto-note` element next to `#recovery-note`.
- Test: append to `tests/test_auto_optimize.py`

**Interfaces:**
- Consumes: `_bump_book_changed()` (Task 3).
- Produces: every `/run` response carries `auto_note` (dict `{text, at}` or null).

- [ ] **Step 1: Failing test**

```python
def test_mutating_endpoints_bump_and_run_returns_note(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "0")
    m = _api()
    _seed_book()
    bumps = []
    monkeypatch.setattr(m, "_bump_book_changed", lambda: bumps.append(1))
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    c.post("/actuals", json={"so_no": "SO1", "item_code": ITEM_A,
                             "entry_date": "2025-03-05", "qty_produced": 1,
                             "process": "", "shift": "A"})
    assert bumps                                       # punch bumped
    book_store.save_auto_note({"text": "hello", "at": "t"})
    r = c.post("/run", json={})
    assert r.json()["auto_note"]["text"] == "hello"
```

(Adjust the `/actuals` body to the real `ActualRequest` fields — check `class ActualRequest` in `api/main.py` and use its required fields exactly.)

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** Append `_bump_book_changed()` as the LAST statement (before `return`) of each listed endpoint handler. In `_plan`'s return dict add `"auto_note": book_store.load_auto_note()`. In `web/index.html` add after the recovery-note element:

```html
<div id="auto-note" class="recovery-note muted hidden"></div>
```

In `web/app.js`, in the function that renders the run result where `recovery_meta` is handled, add:

```javascript
function renderAutoNote(d) {
  const el = $("auto-note");
  if (!el) return;
  const n = d.auto_note;
  if (n && n.text) { el.classList.remove("hidden"); el.textContent = n.text; }
  else { el.classList.add("hidden"); el.textContent = ""; }
}
```

and call `renderAutoNote(data)` wherever the run payload is rendered (next to the existing recovery-note call).

- [ ] **Step 4: Run tests; Step 5: full suite; Step 6: Commit**: `git commit -am "feat: book-change bumps on all mutating endpoints; auto note surfaced on /run + Orders tab"`

### Task 6: Phase-1 verification gate (manual, no code)

- [ ] Local server dress rehearsal with `GITHUB_DISPATCH_TOKEN=manual`, `AUTO_OPTIMIZE=1`, `AUTO_OPTIMIZE_QUIET_MIN=0.1`, `AUTO_OPTIMIZE_SPACING_MIN=0.1`: upload the real file (ask the owner which), punch one actual via the API, wait ~30 s, confirm `/optimize/status` shows an auto run; run the worker script manually; confirm auto-apply + note in `/run`.
- [ ] Full suite green; golden untouched (`git diff --stat tests/golden_trace.json` empty).
- [ ] Commit any fixes; report Phase-1 results to the owner before starting Phase 2.

---

## Phase 2 — One-pool contest with the promise veto

### Task 7: `promise_ceiling_ok` + veto inside `optimize()`

**Files:**
- Modify: `engine/optimizer.py` — new function + `optimize(..., feasible=None)` parameter; infeasible plans score `float("inf")`.
- Test: `tests/test_promise_veto.py` (create)

**Interfaces:**
- Consumes: schedule entries (`e.so_refs`, `e.item_code`, `e.end`), `SOLine.commitment/promised_date`.
- Produces: `optimizer.promise_ceiling_ok(schedule, so_lines) -> bool`; `optimizer.optimize(..., feasible=callable_or_None)` — `feasible(schedule) -> bool`; when it returns False the candidate's score is `inf` and it can never be `best`. `OptimizeResult.best` is `None` when NO candidate was feasible.

- [ ] **Step 1: Failing tests** (`tests/test_promise_veto.py`)

```python
"""The promise ceiling: end.date() <= promised_date for every committed/urgent
order, or the candidate plan is discarded (score = inf)."""
import io
from datetime import date, timedelta

from engine import optimizer
from engine.config import Config, OVERLAP_PERCENT
from engine.loaders import load_all
from engine.models import PlanRun
from engine.pipeline import run_forward
from tests.sample_workbook import build_sample_bytes


def _lines(cfg_overlap=80):
    so, masters = load_all(io.BytesIO(build_sample_bytes()))
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=cfg_overlap,
                 plan_start_date=date(2025, 3, 1))
    cfg.validate()
    return so, masters, cfg


def _end_date(schedule, key):
    d = None
    for e in schedule:
        for r in (e.so_refs or []):
            if (r, e.item_code) == key and (d is None or e.end.date() > d):
                d = e.end.date()
    return d


def test_promise_ceiling_ok_day_level():
    so, masters, cfg = _lines()
    pr = PlanRun(so_lines=list(so)); run_forward(pr, cfg, masters)
    k = (so[0].so_no, so[0].item_code)
    end = _end_date(pr.schedule, k)
    so[0].commitment, so[0].promised_date = "committed", end
    assert optimizer.promise_ceiling_ok(pr.schedule, so)          # on the day = fine
    so[0].promised_date = end - timedelta(days=1)
    assert not optimizer.promise_ceiling_ok(pr.schedule, so)      # one day late = veto
    so[0].commitment, so[0].promised_date = "open", None
    assert optimizer.promise_ceiling_ok(pr.schedule, so)          # open = never vetoed


def test_optimize_feasible_gate_yields_none_when_all_vetoed():
    so, masters, cfg = _lines()
    r = optimizer.optimize(so, cfg, masters, budget_evals=8, seed=42,
                           feasible=lambda schedule: False)
    assert r.best is None and not r.ranks


def test_optimize_feasible_gate_passthrough_when_always_true():
    so, masters, cfg = _lines()
    a = optimizer.optimize(so, cfg, masters, budget_evals=8, seed=42)
    b = optimizer.optimize(so, cfg, masters, budget_evals=8, seed=42,
                           feasible=lambda schedule: True)
    assert a.best == b.best and a.ranks == b.ranks
```

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement in `engine/optimizer.py`.**

```python
def promise_ceiling_ok(schedule, so_lines) -> bool:
    """The owner's law (spec 2026-07-15): no committed/urgent order may END
    after its promised date. Day-level: end.date() <= promised. Orders without
    a promise are never vetoed."""
    promised = {(l.so_no, l.item_code): l.promised_date for l in so_lines
                if getattr(l, "commitment", "open") in ("committed", "urgent")
                and getattr(l, "promised_date", None)}
    if not promised:
        return True
    ends = {}
    for e in schedule:
        for r in (e.so_refs or []):
            k = (r, e.item_code)
            if k in promised:
                d = e.end.date()
                if k not in ends or d > ends[k]:
                    ends[k] = d
    return all(ends.get(k, promised[k]) <= promised[k] for k in promised)
```

In `optimize(...)`: add keyword `feasible=None`. Locate the single place a candidate plan is scored (the internal evaluate function that calls `plan_metrics` then `score`); after computing the schedule, insert:

```python
        if feasible is not None and not feasible(plan_run.schedule):
            metrics = None                      # vetoed candidate
            cand_score = float("inf")
```

and ensure the best-tracking only accepts finite scores; when the search ends with no finite best, return `OptimizeResult()` (empty best/ranks). Read the existing evaluate/best-tracking code first and keep its structure — the change is only "infeasible ⇒ inf, inf never wins".

- [ ] **Step 4: Run**: `python3 -m pytest tests/test_promise_veto.py tests/test_optimizer.py -v` — PASS (existing optimizer tests must be untouched).
- [ ] **Step 5: Commit**: `git commit -am "feat: promise-ceiling veto — feasible= gate in optimize(); day-level end<=promise check"`

### Task 8: The contest goes one-pool (joint search space)

**Files:**
- Modify: `engine/optimize_service.py` — `prepare_contest` gains joint mode; `run_candidate` passes the veto; Modify `api/main.py::_start_optimize` to search ALL lines.
- Test: append to `tests/test_promise_veto.py`

**Interfaces:**
- Consumes: Task 7's `feasible=` gate.
- Produces: `prepare_contest(..., joint=True)` → `setup.target` = ALL active lines and `setup.feasible` = `lambda schedule: optimizer.promise_ceiling_ok(schedule, setup.target)` (None when no promises); `setup.joint` bool; `run_candidate`/`run_contest` pass `feasible` through to `optimize()`; contest results carry `"joint": True` so Apply stores it in meta. With NO promised orders present, joint mode's target/behavior is byte-identical to today (all-open books already search everything).

- [ ] **Step 1: Failing tests**

```python
def test_joint_contest_searches_all_lanes_but_never_breaks_a_promise():
    from engine import optimize_service as svc
    so, masters, cfg = _lines()
    pr = PlanRun(so_lines=list(so)); run_forward(pr, cfg, masters)
    k0 = (so[0].so_no, so[0].item_code)
    so[0].commitment = "committed"
    so[0].promised_date = _end_date(pr.schedule, k0)      # promise = today's end
    orders = {(l.so_no, l.item_code): l for l in so}      # duck-typed for signature
    setup = svc.prepare_contest_lines(so, cfg, masters)   # helper below
    assert setup.joint and len(setup.target) == len(so)   # committed included
    row = svc.run_candidate_lines(setup, cfg.overlap_percent, budget=10, seed=42)
    if row["best"] is not None:                           # a feasible winner exists
        # replay its ranks and check the promise held
        pr2 = PlanRun(so_lines=list(so))
        run_forward(pr2, setup.search_config, masters, priority_rank=row["ranks"])
        assert optimizer.promise_ceiling_ok(pr2.schedule, so)
```

(Implementer: name the internal helpers as you structure them — the REQUIRED
behaviors are: joint target includes committed lines; the veto is applied via
`optimize(feasible=)`; a winning plan's replayed schedule passes
`promise_ceiling_ok`. Adjust this test to the final function names, keeping the
three assertions.)

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement.** In `prepare_contest`: when promised orders exist, KEEP computing `protected/reserved/candidate_setup` (still needed by `_plan`'s fallback and the incumbent), and ADD:

```python
    joint_target = so_lines                       # everyone competes for all time
    feasible = ((lambda schedule: optimizer.promise_ceiling_ok(schedule, so_lines))
                if protected else None)
```

Extend `ContestSetup` with `joint_target: list`, `feasible: object = None`. In `run_candidate`: use `setup.joint_target` as the search lines and pass `feasible=setup.feasible` (and drop the per-candidate `candidate_setup` reservations in joint mode — the veto replaces the guard; keep `reserved=None` for the joint search). In `api/main.py::_start_optimize`: the contest target becomes `setup.joint_target`; the baseline stays the two-pass incumbent. The result dict gains `"joint": bool(setup.feasible is not None or not setup.protected)` — set `"joint": True` always going forward; `_optimize_apply` stores it in meta.

- [ ] **Step 4: Run new + ALL optimizer/cloud/sweep tests**; expect the all-open equivalence tests still byte-identical.
- [ ] **Step 5: Commit**: `git commit -am "feat: one-pool contest — all lanes compete, promise veto replaces the open-only wall"`

### Task 9: `_plan` replays joint ranks with re-validation + fallback

**Files:**
- Modify: `api/main.py::_plan` — joint branch; Test: `tests/test_joint_replay.py` (create)

**Interfaces:**
- Consumes: `plan_priority` meta `joint: True` (Task 8), `optimizer.promise_ceiling_ok`.
- Produces: `_plan` behavior — meta.joint ⇒ ONE pass over all active lines with `priority_rank=ranks`, expedite off; then `promise_ceiling_ok` re-checked on the produced schedule: pass ⇒ use it; fail ⇒ discard, run today's two-pass exactly as now, and call `_bump_book_changed()`. Non-joint saved ranks (legacy) ⇒ existing behavior untouched.

- [ ] **Step 1: Failing test** (sketch — implementer adapts seeding to `_seed_book` patterns from `tests/test_optimize_cloud.py`)

```python
def test_joint_ranks_replay_and_drift_falls_back(monkeypatch):
    """(a) joint ranks replay as one pass and keep promises; (b) mutate the book
    so the replay would break a promise -> _plan falls back to two-pass and the
    result STILL shows no broken promise that two-pass protects."""
```

Write it concretely: seed two orders, commit SO1 with a comfortable promise,
run a tiny joint contest inline (budget 10), `_optimize_apply()`, call
`m._plan(m._load_plan_config())` and assert SO1's `expected_end` ≤ promise.
Then tighten SO1's promise in the store to *yesterday of its expected end*
(`book_store.set_commitment(...)` with an earlier date) and re-plan: assert the
plan still returns (fallback ran — check via `optimize_meta` or a marker the
implementer adds: `result["joint_fallback"] = True`).

- [ ] **Step 2: Run to verify failure.**
- [ ] **Step 3: Implement** in `_plan` where saved ranks are loaded/passed today (the open-pass `priority_rank=` site): if `meta.get("joint")`: build one `PlanRun` over ALL active lines with `priority_rank=ranks` and expedite-off config; check `promise_ceiling_ok(plan.schedule, so_lines)`; on pass use this plan for everything downstream (schedule/gantt/rule tabs), skipping the two-pass merge; on fail set `joint_fallback=True` in the response, ignore ranks, run the existing two-pass path unchanged, and `_bump_book_changed()`.
- [ ] **Step 4: Run** new + `tests/test_two_pass_gantt.py` + full suite. **Step 5: Commit**: `git commit -am "feat: joint-rank replay with promise re-validation; drift falls back to two-pass and re-triggers"`

### Task 10: Least-damage mode auto-apply comparison

**Files:**
- Modify: `api/main.py::_auto_apply_result`; Test: append `tests/test_auto_optimize.py`

**Interfaces:**
- Consumes: `optimizer.promise_slip_metrics(schedule, lines, start)` (existing) — returns `promise_slip_days`, `promises_missed`.
- Produces: when the finished contest's best is None (all vetoed) the auto path does NOT apply anything and notes "promises can't all be kept — least-damage mode active" (recovery handles sequencing, existing); when both incumbent and candidate carry broken promises (legacy/fallback state), apply only on strictly fewer `promises_missed`, or equal missed and strictly fewer `promise_slip_days`.

- [ ] **Step 1: Failing test**: assert `_auto_apply_result` with a stubbed `_OPTIMIZE["result"] = {"best": None}` writes a note containing "least-damage" and leaves `plan_priority` unchanged.
- [ ] **Step 2/3: Implement** — extend `_auto_apply_result`'s `if not best:` branch:

```python
    if not best:
        _auto_note_write("Auto-optimize: promises can't all be kept — "
                         "least-damage mode active (see red flags); kept current plan.")
        return
```

- [ ] **Step 4/5: Run + commit**: `git commit -am "feat: least-damage auto-apply rule — never applies over an infeasible contest"`

### Task 11: Phase-2 measurement gate (REAL book — hard stop if worse)

- [ ] Ask the owner for the current real file + settings (per the standing data-file rule).
- [ ] Script (scratchpad): joint contest vs today's open-only contest on the real book with committed orders set as on the live site (pull `/orders` for lanes). Compare score + verify `promise_ceiling_ok` on every applied candidate.
- [ ] Joint ≥ open-only AND 100% veto compliance ⇒ proceed. Otherwise STOP and report numbers to the owner — do not ship Phase 2.
- [ ] Full suite; golden untouched; commit fixes.

---

## Phase 3 — Operator absence entry

### Task 12: Absence store (book_store CRUD)

**Files:**
- Modify: `engine/book_store.py`; Test: append `tests/test_book_store.py`

**Interfaces:**
- Produces: `book_store.save_absence({"operator","from_date","to_date"}) -> dict` (adds uuid `id`), `load_absences() -> list[dict]`, `delete_absence(id) -> bool`. Store key `anvitech:absences` (kv, JSON list). Dates ISO strings, inclusive.

- [ ] **Step 1: Failing tests**

```python
def test_absence_crud_round_trip():
    a = book_store.save_absence({"operator": "Mahesh",
                                 "from_date": "2026-07-16", "to_date": "2026-07-18"})
    assert a["id"] and book_store.load_absences() == [a]
    assert book_store.delete_absence(a["id"]) is True
    assert book_store.load_absences() == []
    assert book_store.delete_absence("nope") is False
```

- [ ] **Step 2/3: Implement**

```python
ABSENCES_KEY = "anvitech:absences"


def load_absences() -> list:
    raw = get_store().kv_get(ABSENCES_KEY)
    return json.loads(raw) if raw else []


def save_absence(a: dict) -> dict:
    import uuid as _uuid
    a = {"id": _uuid.uuid4().hex, "operator": a["operator"],
         "from_date": a["from_date"], "to_date": a["to_date"]}
    rows = load_absences() + [a]
    get_store().kv_set(ABSENCES_KEY, json.dumps(rows))
    return a


def delete_absence(absence_id: str) -> bool:
    rows = load_absences()
    keep = [r for r in rows if r.get("id") != absence_id]
    if len(keep) == len(rows):
        return False
    get_store().kv_set(ABSENCES_KEY, json.dumps(keep))
    return True
```

- [ ] **Step 4/5: Run + commit**: `git commit -am "feat: absence store — anvitech:absences CRUD"`

### Task 13: Absences block the person in every plan

**Files:**
- Modify: `engine/optimize_service.py` — `absence_reservations` + wire into `prepare_contest(absences=)` + payload; `api/main.py::_plan` + `_start_optimize` pass absences; worker payload passthrough.
- Test: `tests/test_absences_engine.py` (create)

**Interfaces:**
- Consumes: Rule 6's `reserved={operator_name: [(start_dt, end_dt)]}` (existing, honored in op assignment).
- Produces: `optimize_service.absence_reservations(absences) -> dict` (name → [(datetime 00:00 from, datetime 00:00 day-after-to)]); `prepare_contest(..., absences=None)` merges them into pass-1 AND the contest (`reserved` for baseline, merged into every candidate); `build_payload(..., absences=)` / `parse_payload` round-trip them; `_plan` merges them into BOTH passes' `reserved=`.

- [ ] **Step 1: Failing tests**

```python
"""An absent person is never assigned work inside the absence window, in any
pass; payload round-trips absences; signature includes them (Task 1 covered)."""
def test_absent_operator_gets_no_work_in_window():
    # Plan the sample book normally; find any operator with assigned entries;
    # mark them absent for the whole plan window; re-plan; assert none of the
    # schedule entries inside the window name that operator.
```

Write concretely with the sample workbook: plan once, pick `op =` the first
scheduled entry's operator, absence covering `[min(start).date(), max(end).date()]`,
re-plan via `prepare_contest(..., absences=[...])` + `run_forward(reserved=merged)`,
assert `all(e.operator != op for e in schedule if e.operator)`.

- [ ] **Step 2/3: Implement**

```python
def absence_reservations(absences):
    """Absence rows -> Rule 6 operator reservations: the person is 'busy'
    from 00:00 of from_date to 00:00 of the day AFTER to_date (inclusive)."""
    from datetime import datetime, date, timedelta
    res = {}
    for a in absences or []:
        try:
            f = date.fromisoformat(a["from_date"])
            t = date.fromisoformat(a["to_date"])
        except (KeyError, ValueError):
            continue                                   # malformed row — skip
        if t < f:
            f, t = t, f
        interval = (datetime.combine(f, datetime.min.time()),
                    datetime.combine(t + timedelta(days=1), datetime.min.time()))
        res.setdefault(a.get("operator", ""), []).append(interval)
    res.pop("", None)
    return res


def merge_reservations(a, b):
    out = {k: list(v) for k, v in (a or {}).items()}
    for k, v in (b or {}).items():
        out.setdefault(k, []).extend(v)
    return out
```

`prepare_contest(orders, actuals, masters, config, absences=None)`: compute
`ab = absence_reservations(absences)`; pass-1 `run_forward(..., reserved=ab or None)`;
`setup.reserved = merge_reservations(reserved, ab) or None`; contest candidates use
`reserved=merge_reservations(candidate_reserved, ab)`. `build_payload` gains
`"absences": list(absences or [])`; `parse_payload` returns them as a 5th value —
UPDATE ALL existing callers/tests of `parse_payload` in the same commit.
`api/main.py`: `_start_optimize`/`_incumbent_metrics`/`_current_book_sig` load
`book_store.load_absences()` and pass through; `_plan` merges `absence_reservations`
into pass-1 and pass-2 `reserved=`.

- [ ] **Step 4: Run new + service + cloud tests (payload shape changed) + full suite.**
- [ ] **Step 5: Commit**: `git commit -am "feat: absences block the person in every pass, contest, and cloud payload"`

### Task 14: Analytics stays honest under absences

**Files:**
- Modify: `engine/analytics.py` — operator available-hours subtract absence-day shift hours; `api/main.py` passes absences into `build_analytics`.
- Test: append `tests/test_analytics.py`

**Interfaces:**
- Produces: `build_analytics(schedule, masters, config, batches, absences=None)`; an operator absent D working days in the window loses D × their per-day shift hours from "Available (hrs)" (floor 0); default `None` byte-identical.

- [ ] **Step 1: Failing test**: compute analytics without absences, note one operator's `Available (hrs)`; recompute with a 2-working-day absence for them; assert available drops by exactly `2 × their per-day hours` (derive per-day = old available ÷ working days in window) and no operator exceeds 100%.
- [ ] **Step 2/3: Implement**: in the operator section where available hours are computed per person, subtract `sum(shift_hours_per_day for each absence day that is a working day inside the plan window)`; clamp ≥ busy? No — clamp ≥ 0 and let Status compute; keep util capped semantics as today (busy can only come from assigned work, and Task 13 guarantees none inside absences).
- [ ] **Step 4/5: Run + full suite + commit**: `git commit -am "feat: analytics operator capacity honest under absences"`

### Task 15: `/absences` endpoints + orphan reporting

**Files:**
- Modify: `api/main.py` — `GET /absences` (any role), `POST /absences` (admin), `DELETE /absences/{id}` (admin); orphan rows in `_report_for_book`; bumps call `_bump_book_changed()`.
- Test: `tests/test_absences_api.py` (create)

**Interfaces:**
- Consumes: Task 12 CRUD, Task 3 `_bump_book_changed`.
- Produces: `POST /absences {operator, from_date, to_date}` → 200 `{absence}` / 400 on unknown operator (validated against `masters.operators` names) or bad dates; `GET /absences` → `{"absences": [...], "orphans": [names]}`; DELETE → `{"deleted": true}`. Report: absence rows whose operator is no longer in masters appear as kind `ABSENT_OPERATOR_UNKNOWN` (non-blocking).

- [ ] **Step 1: Failing tests**: role gating (user POST → 403, GET → 200), happy path CRUD via TestClient (admin cookie), unknown operator → 400, orphan listed after masters change (save masters without that operator; simpler: post absence for a name not in masters is blocked — orphan test seeds the absence directly via `book_store.save_absence` then GET shows it under `orphans`).
- [ ] **Step 2/3: Implement** — follow the `/orders/commit` endpoint pattern (pydantic `AbsenceRequest`, `require_admin`, date validation with `date.fromisoformat` in try/except → 400; operator name must be in `{o.name for o in _current_masters().operators}`); call `_bump_book_changed()` after POST and DELETE.
- [ ] **Step 4/5: Run + full suite + commit**: `git commit -am "feat: /absences endpoints — validated, role-gated, orphan-tolerant, trigger-bumping"`

### Task 16: Settings UI + final verification

**Files:**
- Modify: `web/index.html` (Settings panel block), `web/app.js` (render + calls), `web/style.css` if needed.
- Test: browser drive against a local server (manual steps below) + full suite.

**Interfaces:**
- Consumes: Task 15 endpoints; `/items`-style masters data for the operator list — add operator names to the `/run` config payload OR a lightweight `GET /absences` response field `operators: [names]` (implementer: extend the GET response — one source).

- [ ] **Step 1: UI block** in the Settings panel (admin-only wrapper class `admin-only`):

```html
<fieldset class="cfg-group admin-only">
  <legend>Operator absences</legend>
  <div class="cfg-row">
    <select id="absence-operator"></select>
    <input type="date" id="absence-from" />
    <input type="date" id="absence-to" />
    <button id="absence-add" class="ghost-btn">Mark absent</button>
  </div>
  <ul id="absence-list" class="muted"></ul>
</fieldset>
```

- [ ] **Step 2: app.js** — `loadAbsences()` (GET, fill select from `operators`, render list rows with a ✕ button calling DELETE then reload + `runPlan(false)`), `absence-add` onclick POST then reload + `runPlan(false)`; call `loadAbsences()` at init for admins. Dates display DD-MM-YYYY like the rest of the app (reuse the existing date-echo helper).
- [ ] **Step 3: Browser verification (required)** — `STORE_DIR=/tmp/abs_e2e uvicorn ... --port 8016`, log in as admin, add an absence for a real operator, confirm: appears in list; Gantt/shift-wise shows no work for them those days; Analytics available-hours drops; remove → restored. Log in as user: block visible read-only, no buttons.
- [ ] **Step 4: Full suite + golden check + commit**: `git commit -am "feat: operator absence entry UI (Settings) — add/list/remove, replans on change"`

### Task 17: Docs + handoff (closes the plan)

- [ ] Update `CLAUDE.md` (code map: auto trigger, veto, absences), `RULES.md` (feedback section: the three layers + the promise ceiling law), `HANDOFF.md` (latest-session block).
- [ ] Full suite; `python3 -m pytest -q` green; golden untouched.
- [ ] Commit: `git commit -am "docs: self-tuning plan — CLAUDE/RULES/HANDOFF in lockstep"`. Report to the owner with the phase-by-phase verification results; deploy only on the owner's "push to main".
