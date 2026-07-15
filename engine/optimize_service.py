"""Shared Optimize service — ONE code path for the settings-sweep contest,
used by BOTH the API's local compute (``api.main._start_optimize``) and the
GitHub Actions cloud worker (``scripts/cloud_optimize_worker.py``).

Pure: no storage, no HTTP. Callers hand in the book (orders + actuals), the
masters workbook bytes, and the saved config; the payload helpers serialize
exactly those via the models' own ``to_json``/``from_json``, so the worker
reconstructs the very objects the API uses and calls the very same functions.
With the fixed seed the search is deterministic — a cloud run of the same
contest is byte-identical to a local run (CLAUDE.md principle: planning logic
is never duplicated, and new surfaces must reuse the plan's own machinery).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
from dataclasses import dataclass, field, replace

from engine import optimizer, orderbook
from engine.config import Config
from engine.loaders import load_all
from engine.models import Actual, Masters, Order

# The full fair contest the owner signed off on (2026-07-15): EVERY overlap
# contender at the same full depth — 6 candidates × 400 = 2,400 plans when the
# current overlap is one of the six. Only the cloud (GitHub Actions, 2 vCPU)
# can afford it; Render's 0.1-CPU instance runs the trimmed local fallback
# (optimizer.OVERLAP_CANDIDATES at 1,000 plans total).
CLOUD_OVERLAP_CANDIDATES = (50, 60, 70, 80, 90, 100)
CLOUD_BUDGET_PER_CANDIDATE = 400


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


def reservations_from_schedule(schedule):
    """Machine id → busy intervals and operator name → busy intervals, from a
    plan (used to reserve the committed pass's machine/operator time in the
    open pass). Moved here from api.main so the cloud worker shares it."""
    res = {}
    for e in schedule:
        m = e.machine or ""
        if m and "OS" not in m and "Off-machine" not in m and "Outsourced" not in m:
            res.setdefault(m, []).append((e.start, e.end))
        op = getattr(e, "operator", "") or ""
        if op:
            res.setdefault(op, []).append((e.start, e.end))
    return res


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


# --------------------------------------------------------------------------- #
# Payload — the book snapshot a cloud worker needs, serialized losslessly.
# --------------------------------------------------------------------------- #
def build_payload(orders: dict, actuals, masters_bytes, config: Config, *,
                  seed: int, candidates=CLOUD_OVERLAP_CANDIDATES,
                  budget_per_candidate=CLOUD_BUDGET_PER_CANDIDATE,
                  absences=None) -> dict:
    """Snapshot everything one contest depends on. JSON-safe."""
    return {
        "orders": [o.to_json() for o in orders.values()],
        "actuals": [a.to_json() for a in actuals],
        "masters_xlsx_b64": (base64.b64encode(masters_bytes).decode()
                             if masters_bytes else None),
        "config": config.to_dict(),
        "seed": seed,
        "candidates": list(candidates),
        "budget_per_candidate": budget_per_candidate,
        "absences": list(absences or []),
    }


def parse_payload(payload: dict):
    """Rebuild (orders, actuals, masters, config, absences) — the exact
    objects the API planned with, via the models' own from_json and the
    normal loader."""
    orders = {}
    for d in payload["orders"]:
        o = Order.from_json(d)
        orders[o.key] = o
    actuals = [Actual.from_json(d) for d in payload["actuals"]]
    raw = payload.get("masters_xlsx_b64")
    if raw:
        _, masters = load_all(io.BytesIO(base64.b64decode(raw)))
    else:
        masters = Masters()
    config = Config.from_dict(payload["config"])
    config.validate()
    absences = list(payload.get("absences") or [])
    return orders, actuals, masters, config, absences


# --------------------------------------------------------------------------- #
# Contest setup — the pre-search work _start_optimize used to do inline.
# --------------------------------------------------------------------------- #
@dataclass
class ContestSetup:
    target: list = field(default_factory=list)   # the lines the search may reorder (ALL active)
    config: Config = None                        # effective (start advanced), expedite as saved
    search_config: Config = None                 # effective, expedite forced off
    # The promise rule was discarded (owner pivot, 2026-07-15): lanes are pure
    # status labels with no scheduling effect. These fields survive only so the
    # not-yet-updated ``api.main`` keeps its field surface; they are now inert:
    #   protected      — always ``[]`` (no lane is protected)
    #   reserved       — == ``absence_reserved`` (only physical unavailability)
    #   candidate_setup / feasible — always ``None`` (no guard, no veto)
    #   joint_target   — == ``target`` (every active line competes as one pool)
    # The next task deletes these dead fields together with their api callers.
    protected: list = field(default_factory=list)
    reserved: dict = None
    candidate_setup: object = None
    joint_target: list = field(default_factory=list)
    feasible: object = None
    # Operator absences: physical unavailability (not a promise reservation).
    # ``absence_reserved`` is the raw ``absence_reservations(absences)`` dict.
    absences: list = field(default_factory=list)
    absence_reserved: object = None


def prepare_contest(orders: dict, actuals, masters, config: Config,
                    absences=None) -> ContestSetup:
    """Everything the sweep needs, from the raw book. Every active line competes
    in ONE pool — lanes (open/committed/urgent) have no scheduling effect. Only
    operator absences reserve time. Raises ValueError when there is nothing to
    optimize (no active orders with work remaining)."""
    ab = absence_reservations(absences)
    so_lines = orderbook.active_so_lines(orders, actuals, masters)
    eff = orderbook.effective_plan_start_date(actuals, config.plan_start_date,
                                              masters.calendar)
    if eff != config.plan_start_date:
        config = replace(config, plan_start_date=eff)

    target = so_lines
    if not target:
        raise ValueError("nothing to optimize — no active orders with work remaining")

    # The batch sequence only has leverage when Expedite is off (see the
    # 2026-07-13 Expedite↔Optimize fix) — search in the pure non-delay model.
    search_config = replace(config, expedite_window_min=0)

    reserved = ab or None   # only physical unavailability reserves time now

    return ContestSetup(target=target, config=config, search_config=search_config,
                        protected=[], reserved=reserved,
                        candidate_setup=None,
                        joint_target=target, feasible=None,
                        absences=list(absences or []), absence_reserved=(ab or None))


# --------------------------------------------------------------------------- #
# The contest itself — per-candidate runs + the shared winner rule.
# --------------------------------------------------------------------------- #
def pick_winner(current_overlap, rows):
    """The ONE winner rule (shared by sweep_optimize's sequential loop and the
    cloud worker's parallel rows): best score wins; an exact tie keeps the
    current setting (it is considered first)."""
    ordered = sorted(rows, key=lambda r: (r.get("overlap") != current_overlap,
                                          r.get("overlap")))
    best = None
    for r in ordered:
        if not r.get("eligible") or r.get("best") is None:
            continue
        if best is None or optimizer.score(r["best"]) < optimizer.score(best["best"]):
            best = r
    return best


def run_candidate(payload: dict, overlap: int, *, on_progress=None,
                  should_cancel=None) -> dict:
    """One contender, fully self-contained (safe to run in a subprocess): it
    rebuilds the book from the payload and searches every active line as one
    pool (lanes have no scheduling effect). ``reserved=`` is only the operator
    absences (physical unavailability). Returns a sweep-table row (+ ranks for
    the winner)."""
    orders, actuals, masters, config, absences = parse_payload(payload)
    setup = prepare_contest(orders, actuals, masters, config, absences=absences)
    cfg = replace(setup.search_config, overlap_percent=int(overlap))
    res = optimizer.optimize(setup.target, cfg, masters,
                             reserved=setup.absence_reserved,
                             budget_evals=int(payload["budget_per_candidate"]),
                             seed=int(payload["seed"]),
                             on_progress=on_progress, should_cancel=should_cancel)
    return {"overlap": int(overlap), "eligible": True, "best": res.best,
            "evals": res.evals, "ranks": res.ranks, "cancelled": res.cancelled}


# Subprocess plumbing: a plain module-level initializer + runner so the pool
# works under both fork (Linux runner) and spawn (macOS dev) start methods.
_POOL = {"counter": None, "stop": None}


def _pool_init(counter, stop):
    _POOL["counter"], _POOL["stop"] = counter, stop


def _pool_run(args):
    payload, overlap = args
    last = {"evals": 0}

    def cb(evals, _best):
        delta, last["evals"] = evals - last["evals"], evals
        c = _POOL["counter"]
        if c is not None:
            with c.get_lock():
                c.value += delta

    stop = _POOL["stop"]
    return run_candidate(payload, overlap, on_progress=cb,
                         should_cancel=(lambda: bool(stop.value)) if stop else None)


def run_contest(payload: dict, *, processes=1, on_progress=None,
                should_cancel=None, poll_seconds=5.0) -> dict:
    """The full fair contest from a payload. ``processes > 1`` fans the
    contenders out to subprocesses (per-eval progress via a shared counter);
    ``processes == 1`` runs them sequentially in-process. Returns
    {winner_overlap, rows, best, ranks, evals, cancelled}."""
    config = Config.from_dict(payload["config"])
    contenders = optimizer.sweep_contenders(config.overlap_percent,
                                            payload["candidates"])
    rows, done_evals, cancelled = [], 0, False

    if processes <= 1:
        for ov in contenders:
            if should_cancel and should_cancel():
                cancelled = True
                break
            base = done_evals

            def cb(evals, best, _base=base):
                if on_progress:
                    on_progress(_base + evals, best)

            row = run_candidate(payload, ov, on_progress=cb,
                                should_cancel=should_cancel)
            rows.append(row)
            done_evals += row.get("evals", 0)
            cancelled = cancelled or bool(row.get("cancelled"))
    else:
        import multiprocessing as mp
        import time as _time
        ctx = mp.get_context()
        counter = ctx.Value("i", 0)
        stop = ctx.Value("b", 0)
        with ctx.Pool(processes=processes, initializer=_pool_init,
                      initargs=(counter, stop)) as pool:
            async_res = pool.map_async(_pool_run,
                                       [(payload, ov) for ov in contenders])
            while not async_res.ready():
                async_res.wait(poll_seconds)
                if on_progress:
                    on_progress(counter.value, None)
                if should_cancel and should_cancel():
                    stop.value = 1
            rows = async_res.get()
        done_evals = sum(r.get("evals", 0) for r in rows)
        cancelled = bool(stop.value) or any(r.get("cancelled") for r in rows)

    if on_progress:
        on_progress(done_evals, None)
    winner = pick_winner(config.overlap_percent, rows)
    table = [{k: r[k] for k in ("overlap", "eligible", "best", "evals")
              if k in r} for r in rows]
    if winner is None:
        return {"winner_overlap": config.overlap_percent, "rows": table,
                "best": None, "ranks": {}, "evals": done_evals,
                "cancelled": cancelled}
    return {"winner_overlap": winner["overlap"], "rows": table,
            "best": winner["best"], "ranks": winner.get("ranks", {}),
            "evals": done_evals, "cancelled": cancelled}
