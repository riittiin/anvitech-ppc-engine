"""FastAPI layer — order-book engine + per-rule trace, behind one login.

Endpoints:
  POST /upload        merge an uploaded workbook into the order book (+ masters)
  POST /run           plan the active order book (unifies old Run + Rerun MRP)
  POST /rerun         alias of /run (kept for compatibility)
  GET  /orders        the order-book dashboard (status / remaining per SO#)
  GET  /gantt         Gantt for the current plan
  POST /actuals       save a daily actual; mark-complete archives the order
  GET  /items         item metadata for the Rule 8 form
  GET  /report        loader validation report
  GET  /trace/{id}    a past run's trace
The web/ frontend is served at /.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import threading
import time
import uuid
from collections import OrderedDict, defaultdict
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from engine.config import Config, OVERLAP_SEQUENTIAL, OVERLAP_PERCENT
from engine.loaders import load_all
from engine.models import PlanRun, Actual, Masters, fmt_date
from engine.pipeline import run_forward, to_table, KEY_SEP
from engine import optimizer
from engine import optimize_service
from engine.gantt import build_gantt
from engine import book_store, orderbook
from engine.rules import (
    rule3_tiebreak_process_time as r3,
    rule4_setup_time as r4,
    rule5_overlap_mode as r5,
    rule6_allocate as r6,
    rule7_capture_actuals as r7,
)
from api import auth

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB cap on uploaded workbooks
MAX_LOGIN_BYTES = 8 * 1024            # tiny cap on the login form body

@asynccontextmanager
async def _lifespan(app):
    # Resolve the signing secret once at startup (avoids a first-request race /
    # latency blip). Lazy resolution still covers any path that skips startup.
    try:
        auth.get_secret()
    except Exception:
        pass
    yield


# Interactive docs disabled in the deployed app to shrink the attack surface
# (they were behind auth anyway — this is defense-in-depth).
app = FastAPI(title="Anvitech PPC Engine", lifespan=_lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)

# --------------------------------------------------------------------------- #
# Login + session gate. The whole app (UI + API + static) sits behind a signed
# session cookie, with two roles (admin / user). See engine-free api/auth.py.
# --------------------------------------------------------------------------- #
# Exact (method, path) allowlist of pages reachable WITHOUT a session. Matched
# exactly — never by prefix or extension — so no static file leaks past the gate.
_PUBLIC = {("GET", "/login"), ("POST", "/login"), ("POST", "/logout")}
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _is_https(request: Request) -> bool:
    if request.url.scheme == "https":
        return True
    return request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


@app.middleware("http")
async def gatekeeper(request: Request, call_next):
    method, path = request.method, request.url.path

    # CSRF: reject a state-changing request only when an Origin/Referer is present
    # AND its host doesn't match ours. Absent (curl / server-to-server) → allowed;
    # those carry no ambient cookie, so they're not a CSRF vector.
    if method not in _SAFE_METHODS:
        origin = request.headers.get("origin") or request.headers.get("referer")
        if origin:
            o = urlsplit(origin).netloc
            if o and o != request.headers.get("host", ""):
                return Response("cross-origin request rejected", status_code=403)

    if (method, path) in _PUBLIC:
        return await call_next(request)

    # Cloud-optimize worker endpoints: authenticated by a shared secret header
    # (server-to-server from the GitHub Actions runner — no session cookie).
    # Constant-time compare; with no OPTIMIZE_WORKER_SECRET configured these
    # paths simply fall through to the normal session gate (→ 401).
    if ((method == "GET" and path.startswith("/optimize/job/"))
            or (method == "POST" and path in ("/optimize/progress",
                                              "/optimize/result"))):
        if _worker_secret_ok(request):
            request.state.user = "cloud-worker"
            request.state.role = "worker"
            return await call_next(request)

    payload = auth.verify_token(request.cookies.get(auth.COOKIE_NAME))
    if payload is None:
        # Browser navigation → send to the login page; API/XHR → 401 (no
        # WWW-Authenticate header, so no browser Basic-Auth popup).
        if method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse("/login", status_code=302)
        return Response("authentication required", status_code=401)
    request.state.user = payload["u"]
    request.state.role = payload["role"]
    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; object-src 'none'; base-uri 'none'; "
        "frame-ancestors 'none'; form-action 'self'")
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    if _is_https(request):
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response


def require_admin(request: Request):
    """Raise 403 unless the verified session role is admin. Role comes ONLY from
    the signed session — never from a request body/header/query."""
    if getattr(request.state, "role", None) != auth.ADMIN:
        raise HTTPException(status_code=403, detail="admin only")


def require_password(request: Request, password: str):
    """Re-authenticate the signed-in admin by their password (a deliberate guard on
    destructive actions). Raises 403 if it doesn't match. Constant-time via auth."""
    user = getattr(request.state, "user", "")
    if auth.authenticate(user, password) != auth.ADMIN:
        raise HTTPException(status_code=403, detail="password confirmation failed")


# --- login / logout / identity ------------------------------------------- #
def _render_login(error: str = "") -> str:
    """Login page HTML with a server-controlled (constant, safe) error message."""
    html = (WEB_DIR / "login.html").read_text(encoding="utf-8")
    block = f'<div class="err">{error}</div>' if error else ""
    return html.replace("<!--ERROR-->", block)


def _set_session(response: Response, token: str, request: Request):
    response.set_cookie(
        auth.COOKIE_NAME, token, max_age=auth.MAX_AGE_SECONDS,
        httponly=True, samesite="lax", secure=_is_https(request), path="/")


@app.get("/login")
def login_page():
    return HTMLResponse(_render_login())


@app.post("/login")
async def login_submit(request: Request):
    if int(request.headers.get("content-length") or 0) > MAX_LOGIN_BYTES:
        return HTMLResponse(_render_login("Request too large."), status_code=413)
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    if auth.is_rate_limited(username):
        return HTMLResponse(
            _render_login("Too many attempts. Please wait a few minutes and try again."),
            status_code=429)
    role = auth.authenticate(username, password)
    if role is None:
        auth.record_failed_login(username)
        if auth.FAILED_LOGIN_DELAY:
            await asyncio.sleep(auth.FAILED_LOGIN_DELAY)
        return HTMLResponse(_render_login("Incorrect username or password."),
                            status_code=401)
    resp = RedirectResponse("/", status_code=303)
    _set_session(resp, auth.make_token(username, role), request)
    return resp


@app.post("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE_NAME, path="/")
    return resp


@app.get("/me")
def me(request: Request):
    return {"username": getattr(request.state, "user", None),
            "role": getattr(request.state, "role", None)}


# --------------------------------------------------------------------------- #
# Masters: from the latest uploaded workbook, else the bundled test file.
# Cached in-process, keyed by the workbook's content hash.
# --------------------------------------------------------------------------- #
# Recent plan traces, keyed by run_id, for the /trace/{id} endpoint. Bounded so it
# can't grow without limit (every save auto-re-plans) and leak memory on the worker.
_RUNS: "OrderedDict[str, dict]" = OrderedDict()
_RUNS_MAX = 40
_MASTERS_CACHE: dict = {"key": None, "masters": None}


def _store_run(run_id: str, trace: dict) -> None:
    _RUNS[run_id] = trace
    while len(_RUNS) > _RUNS_MAX:
        _RUNS.popitem(last=False)   # evict the oldest


def _store_env_key():
    """Identity of the active store config — so the masters cache resets when a
    test swaps STORE_DIR/backend, but stays warm in production."""
    return (os.environ.get("MONGODB_URI"),
            os.environ.get("UPSTASH_REDIS_REST_URL"),
            os.environ.get("STORE_DIR"))


def _current_masters():
    """Masters from the latest uploaded workbook, else the bundled test file.

    Parsed once and cached in-process; only re-read from the durable store when a
    new workbook is uploaded (which clears the cache) or the store config changes.
    Avoids pulling + re-parsing the (large) workbook blob on every request."""
    key = _store_env_key()
    if _MASTERS_CACHE["masters"] is not None and _MASTERS_CACHE["key"] == key:
        return _MASTERS_CACHE["masters"]
    raw = book_store.load_masters_bytes()
    if raw is None:
        # No workbook uploaded yet → empty masters; the UI prompts to upload.
        # (There is no bundled demo file anymore — production runs on uploads.)
        masters = Masters()
    else:
        _, masters = load_all(io.BytesIO(raw))
    _MASTERS_CACHE.update(key=key, masters=masters,
                          sha=hashlib.sha256(raw).hexdigest() if raw else "none")
    return masters


def _masters_sha() -> str:
    """Content hash of the stored masters workbook (cached with the parsed masters)."""
    _current_masters()   # warms the cache (and refreshes sha after an upload)
    return _MASTERS_CACHE.get("sha") or "none"


def _inputs_signature(config: Config) -> str:
    """Fingerprint of everything an applied optimization's numbers depend on: the
    masters workbook + the schedule-shaping settings. When either changes after
    Apply, replaying the saved ranks legitimately yields different numbers — the
    UI must SAY so instead of looking non-deterministic (live 2026-07-15 finding:
    the owner re-uploaded an edited workbook after Apply and saw "the same run"
    produce different results). Schedule-neutral knobs are excluded:
    balance_operator_load never moves timing, and expedite is forced off whenever
    ranks replay, so neither can alter an optimized plan."""
    d = config.to_dict()
    d.pop("balance_operator_load", None)
    d["expedite_window_min"] = 0
    blob = json.dumps([_masters_sha(), d], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _report_table(masters):
    return to_table([
        {"Kind": r["kind"], "Reference": r["ref"], "Message": r["message"]}
        for r in masters.report
    ])


def _report_for_book(masters, so_lines):
    """The validation report scoped to the CURRENT order book. Loader-level rows
    about the masters (pending machines, time coercions, …) pass through, but
    NO_ROUTING is re-derived from the live book: the stored workbook's own SO
    sheet can list orders that were never merged or were later deleted (live
    2026-07-15 bug: a "5 orders without routing" banner for ghost orders while
    every real order planned fine)."""
    rows = [r for r in masters.report if r["kind"] != "NO_ROUTING"]
    seen = set()
    for l in so_lines:
        if l.item_code not in masters.routings and l.item_code not in seen:
            seen.add(l.item_code)
            rows.append({"kind": "NO_ROUTING", "ref": l.item_code,
                         "message": f"SO item '{l.item_code}' has no routing in "
                                    f"Item's process Master; order skipped "
                                    f"(cannot schedule without a recipe)"})
    return to_table([
        {"Kind": r["kind"], "Reference": r["ref"], "Message": r["message"]}
        for r in rows
    ])


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class RunRequest(BaseModel):
    config: Optional[dict] = None
    persist: bool = False   # admin Plan-click persists; auto-load sends False


class DeleteRequest(BaseModel):
    # Each order is identified by its (SO number, item code) pair — an SO number is
    # not unique. Sent as [so_no, item_code] pairs.
    orders: List[List[str]] = []
    password: str = ""    # admin re-enters their password to confirm a delete


class ClearRequest(BaseModel):
    password: str = ""


class CommitRequest(BaseModel):
    # Each order is identified by its (SO number, item code) pair, sent as
    # [so_no, item_code] pairs — mirrors DeleteRequest.
    orders: List[List[str]] = []


class UrgentRequest(BaseModel):
    so: str
    item: str
    confirm: bool = False   # False = preview only; True = apply even if it pushes others


class OptimizeRequest(BaseModel):
    budget: str = "deep"    # one option (owner decision): ~1,000 plans total.
                            # Legacy "quick" (cached clients) maps to the same.


class ActualRequest(BaseModel):
    so_no: str
    item_code: str
    entry_date: str
    shift: str = ""
    item_name: str = ""
    process: str = ""
    # Quantities/times can never be negative — a negative would corrupt the
    # produced/rejected math and the plan. Rejected server-side (422), not just the UI.
    qty_produced: float = Field(default=0.0, ge=0)
    qty_rejected: float = Field(default=0.0, ge=0)
    actual_setup_min: float = Field(default=0.0, ge=0)
    no_power_min: float = Field(default=0.0, ge=0)
    no_operator_min: float = Field(default=0.0, ge=0)
    tool_problem_min: float = Field(default=0.0, ge=0)
    machine_breakdown_min: float = Field(default=0.0, ge=0)
    no_load_min: float = Field(default=0.0, ge=0)
    other_work_min: float = Field(default=0.0, ge=0)
    remarks: str = ""
    mark_complete: bool = False


# --------------------------------------------------------------------------- #
# Helper-tab augmentation (Rules 3/4/5/7 + Rule 6 machine view)
# --------------------------------------------------------------------------- #
def _machine_display(masters, mid):
    m = masters.machines.get(mid)
    return m.display_name if m else mid


def _augment_helpers(trace, plan_run, config, masters, actuals=None):
    if "rule3" in trace and trace["rule3"].get("reached", True) and plan_run.batches_prioritized:
        breakdown = r3.build_priority_breakdown(plan_run.batches_prioritized, config, masters)
        trace["rule3"]["tables"] = [
            {"title": "Priority breakdown — slack/critical-ratio per batch (lower slack = more urgent)",
             "table": to_table(breakdown)},
        ]

    if "rule6" in trace and trace["rule6"].get("reached", True):
        timeline, summary = r6.build_machine_view(
            plan_run.schedule, masters, config, plan_run.batches_prioritized)
        trace["rule6"]["tables"] = [
            {"title": "Machine timeline — per-machine queue (Idle before = working minutes the machine waited)",
             "table": to_table(timeline)},
            {"title": "Machine utilization", "table": to_table(summary)},
        ]
        # Analytics tab: utilization & bottlenecks derived from this plan.
        from engine import analytics as _an
        trace["analytics"] = _an.build_analytics(
            plan_run.schedule, masters, config, plan_run.batches_prioritized)
        # Shift-wise timeline (download-only view): every op split into per-shift segments
        # with the real operator each shift. Not rendered as a panel (can be ~900 rows) —
        # attached under its own key for the "Download shift-wise schedule" button. Only
        # meaningful when operator/shift logic is on.
        if config.apply_operator_logic:
            trace["rule6"]["shiftwise"] = to_table(r6.build_shiftwise_timeline(
                plan_run.schedule, masters, config, plan_run.batches_prioritized))
        # Operator/shift coverage: when each machine can run, and unmatched specialties.
        if config.apply_operator_logic:
            from engine.operator_coverage import machine_windows
            windows, cov = machine_windows(masters, config)
            first = (config.first_shift_start_hour * 60, config.first_shift_end_hour * 60)
            second = (config.first_shift_end_hour * 60, (24 + config.second_shift_end_hour) * 60)
            manual = (config.manual_start_hour * 60, config.manual_end_hour * 60)

            def _cov_label(mid):
                iv = windows.get(mid)
                if iv is None:
                    return "—"
                if not iv:
                    return "⚠ needs operator"
                parts = []
                if first in iv:
                    parts.append("1st shift")
                if second in iv:
                    parts.append("2nd shift")
                if manual in iv:
                    parts.append(f"manual {config.manual_start_hour:02d}:00–{config.manual_end_hour:02d}:00")
                return " + ".join(parts) or "—"

            cov_rows = [{"Machine": m.display_name,
                         "Available Hrs/Day": m.available_hrs_per_day,
                         "Runs": _cov_label(mid) + (" (provisional)" if m.provisional else "")}
                        for mid, m in sorted(masters.machines.items())]
            trace["rule6"]["tables"].append({
                "title": "Operator coverage — when each machine can run (from Available "
                         "Hrs/Day + which shifts have a qualified operator)",
                "table": to_table(cov_rows)})
            if cov.get("unmatched_specialties"):
                trace["rule6"]["tables"].append({
                    "title": "Operator specialties that match no machine — check the "
                             "spelling/name in Excel",
                    "table": to_table([{"Operator": u["operator"], "Specialty": u["specialty"]}
                                       for u in cov["unmatched_specialties"]])})

    if plan_run.schedule:
        e = plan_run.schedule[0]
        routing = masters.routings.get(e.item_code)
        proc = routing.processes[0] if routing else None
        cycle = proc.cycle_time if proc else None
        notes4 = [f"occupancy = cycle({cycle}) x qty({e.qty:g}) + setup({config.setup_time_min}) = {e.occupancy_min:g} min"]
    else:
        notes4 = ["no scheduled processes to illustrate"]
    trace["rule4"] = {
        "input": to_table([{"Cycle Time": "per process", "Qty": "batch qty", "Setup": config.setup_time_min}]),
        "output": to_table([
            {"Batch": e.batch_id, "Process": e.process_name, "Occupancy (min)": round(e.occupancy_min, 2)}
            for e in plan_run.schedule
        ]),
        "config": config.to_dict(), "notes": notes4, "error": None, "reached": True,
    }

    # Rule 5 applied to THIS plan: every operation handoff, what it waited under
    # sequential vs this run, and how much overlap pulled the next op earlier.
    pct = config.overlap_percent
    overlap_rows = r5.build_overlap_view(plan_run.schedule, config)
    total_pulled = sum(r["Pulled earlier (min)"] for r in overlap_rows)
    n_overlapped = sum(1 for r in overlap_rows if r["Overlap applied"].startswith("yes"))
    rule5_notes = [
        f"Active mode this run: {config.overlap_mode}"
        + (f" ({pct}% of cutting time)" if config.overlap_mode == OVERLAP_PERCENT else ""),
        f"{len(overlap_rows)} operation handoff(s) in this plan; {n_overlapped} overlapped, "
        f"pulling later operations {total_pulled:g} working-minutes earlier in total vs sequential.",
        "Overlap % applies to the cutting time only — the 90-min setup is excluded "
        "(the next machine is set up in parallel). A step with no cutting time "
        "(deburring, inspection, washing, packing) does not overlap; its successor "
        "waits for it to fully complete.",
    ]
    if not overlap_rows:
        rule5_notes.append("No scheduled operations yet — upload orders and click Plan.")
    trace["rule5"] = {
        "input": to_table([{
            "Overlap mode": config.overlap_mode,
            "Overlap %": pct,
            "Setup excluded from overlap (min)": config.setup_time_min,
            "Operation handoffs in plan": len(overlap_rows),
        }]),
        "output": to_table(overlap_rows),
        "config": config.to_dict(),
        "notes": rule5_notes,
        "error": None, "reached": True,
    }

    if actuals is None:
        actuals = book_store.load_actuals()
    total_down = sum(a.total_downtime_min() for a in actuals)
    # The 'Saved entries' list shows only the latest punched date (kept small +
    # rollback-able); the rollup + progress still cover ALL recorded actuals.
    visible = orderbook.actuals_on_latest_date(actuals)
    latest = orderbook.latest_actual_date(actuals)
    progress = orderbook.process_progress_rows(book_store.load_active_orders(), actuals, masters)
    tables = [{"title": "Per item code — output & downtime rollup (minutes summed across ALL entries)",
               "table": to_table(r7.aggregate_by_item(actuals))}]
    if progress:
        tables.insert(0, {
            "title": "Per-process progress — pieces cleared at each step (the floor's reality; "
                     "drives the next Plan's per-process schedule)",
            "table": to_table(progress)})
    list_note = (f"Showing the {fmt_date(latest)} entries (the latest day) — only these can be "
                 f"rolled back; earlier days are locked and kept in the rollup below."
                 if latest else "No entries yet.")
    trace["rule7"] = {
        "input": to_table([{"Source": "Daily Production Entry form → durable store"}]),
        "output": to_table(visible), "actuals_ids": [a.id for a in visible], "config": None,
        "notes": [
            f"{len(actuals)} actual(s) on record; total downtime {total_down:g} min.",
            list_note,
            "Good at the DISPATCH/last-step gate fulfils the order (remaining qty); "
            "good at each earlier step lets the next Plan skip that finished work. "
            "Marking complete on an entry archives that order.",
        ],
        "tables": tables,
        "error": None, "reached": True,
    }
    return trace


# --------------------------------------------------------------------------- #
# Core: plan the active order book
# --------------------------------------------------------------------------- #
# Machine/operator busy intervals from a plan — moved to engine/optimize_service
# so the cloud worker shares it; kept under the old name for _plan's use.
_reservations_from_schedule = optimize_service.reservations_from_schedule


def _plan(config: Config):
    masters = _current_masters()
    # Fingerprint of the plan-shaping inputs as REQUESTED (base config, before the
    # actuals-driven start advance) — compared against the applied optimization's.
    current_inputs_sig = _inputs_signature(config)
    active = book_store.load_active_orders()
    completed = book_store.load_completed_orders()
    actuals = book_store.load_actuals()

    so_lines = orderbook.active_so_lines(active, actuals, masters)   # remaining = ordered − finished good

    # Advance the plan clock past days already worked: once a day's production is
    # punched, the re-plan starts from the NEXT working day's first shift, not the
    # original date (a config COPY so the persisted config keeps its base date).
    eff_start = orderbook.effective_plan_start_date(actuals, config.plan_start_date,
                                                    masters.calendar)
    if eff_start != config.plan_start_date:
        config = replace(config, plan_start_date=eff_start)

    # An applied Optimize run (the saved rank map). A JOINT contest (2026-07-15)
    # ranked ALL active lines together — committed included — so its ranks replay
    # as ONE pass over every line (below). A LEGACY meta (no "joint" flag, ranks
    # computed open-only, still deployed) keeps today's open-pass replay inside the
    # two-pass plan. No saved optimization -> plans are byte-identical.
    prio = book_store.load_plan_priority()
    ranks = prio["ranks"] if prio else None
    joint = bool(prio and (prio.get("meta") or {}).get("joint"))

    # The optimized ranks ARE the prioritization. The Expedite window dynamically
    # re-sorts ops by slack at schedule time, which would OVERRIDE the ranks and cancel
    # the whole optimization out (the sequence stops mattering). So the ranked orders
    # are planned in the pure non-delay model (expedite off) — the optimizer found the
    # best order globally, which supersedes expedite's local tie-break. No ranks ->
    # config unchanged (expedite behaves exactly as the Settings tick sets it).
    ranked_config = replace(config, expedite_window_min=0) if ranks else config

    # The feedback loop is quantity-only: recorded times (downtime, actual setup) are
    # stored for the record and never affect the schedule.
    recovery_meta = {"active": False}
    joint_fallback = False
    protected, open_lines = orderbook.split_committed_open(so_lines)

    # Joint replay: the contest ranked committed lines in the same pool, so replay
    # its sequence over ALL active lines in one pass, then RE-VALIDATE the promise
    # ceiling on the produced schedule. The book can have drifted since the contest
    # (a tighter promise, more work) so the applied sequence is not trusted blindly.
    joint_plan = None
    if protected and joint and ranks:
        jr = PlanRun(so_lines=so_lines)
        jt = run_forward(jr, ranked_config, masters, priority_rank=ranks)
        if optimizer.promise_ceiling_ok(jr.schedule, so_lines):
            joint_plan = (jr, jt)          # keeps every promise -> use it as-is
        else:
            # Drift: the applied sequence would break a promise. Discard the ranks
            # ENTIRELY — they were computed for a single-pass joint pool and must
            # not drive the isolated open pass either — so the two-pass fallback
            # below is byte-identical to a plan with no optimization applied.
            # Bump the book so a fresh contest re-fits the current book.
            joint_fallback = True
            ranks = None
            ranked_config = config
            _bump_book_changed()

    if not protected or joint_plan is not None:
        # Single pass. Either an all-open book (unchanged — keeps the golden trace
        # byte-identical) or a validated joint replay over every active line.
        if joint_plan is not None:
            plan_run, trace = joint_plan
        else:
            plan_run = PlanRun(so_lines=so_lines)
            trace = run_forward(plan_run, ranked_config, masters, priority_rank=ranks)
    else:
        # Two-pass: committed/urgent orders are planned first, as if open orders
        # don't exist; open orders then backfill the gaps left by that pass —
        # so a later, larger open order can never push a committed order's plan.
        #
        # Promise recovery: if a disruption has made committed orders slip past their
        # promises, replay the auto-recovered committed order (a re-sequence that
        # protects the most promises). Fresh only when its signature matches this exact
        # committed set + promises; otherwise it is ignored and (if slip exists) a fresh
        # recovery search is kicked off in the background.
        rec = book_store.load_promise_recovery()
        rec_sig = _recovery_signature(protected)
        rec_ranks = rec["ranks"] if (rec and rec.get("meta", {}).get("signature") == rec_sig) else None
        prot_config = replace(config, expedite_window_min=0) if rec_ranks else config

        plan_protected = PlanRun(so_lines=protected)
        trace = run_forward(plan_protected, prot_config, masters, priority_rank=rec_ranks)
        reserved = _reservations_from_schedule(plan_protected.schedule)

        # Detect a promise slip; with no fresh recovery in hand, start the search.
        slip_m = _committed_slip_metrics(plan_protected.schedule, protected, config.plan_start_date)
        if rec_ranks and rec:
            m = rec.get("meta", {})
            recovery_meta = {"active": True, "saved_at": m.get("saved_at"),
                             "promises_saved": m.get("promises_saved", 0),
                             "slip_before": m.get("slip_before"), "slip_after": m.get("slip_after")}
        elif slip_m["promise_slip_days"] > 0:
            _maybe_start_recovery(protected, config, masters)
            recovery_meta = {"active": False, "computing": True}   # search running in the background

        plan_open = PlanRun(so_lines=open_lines)
        trace_open = run_forward(plan_open, ranked_config, masters, reserved=reserved,
                                 priority_rank=ranks)

        # Rule 1 numbers batches B001.. fresh per run_forward, so pass 1 and pass 2
        # both produce the same ids. The Gantt keys on batch_id, so a collision would
        # drop committed rows. Namespace the open pass's ids to keep them globally unique.
        for e in plan_open.schedule:
            e.batch_id = "O-" + e.batch_id
        for lst in (plan_open.batches, plan_open.batches_sorted, plan_open.batches_prioritized):
            for b in lst:
                if not b.batch_id.startswith("O-"):
                    b.batch_id = "O-" + b.batch_id

        # Merge for the downstream views (protected first).
        plan_run = PlanRun(so_lines=so_lines)
        plan_run.batches = plan_protected.batches + plan_open.batches
        plan_run.batches_sorted = plan_protected.batches_sorted + plan_open.batches_sorted
        plan_run.batches_prioritized = plan_protected.batches_prioritized + plan_open.batches_prioritized
        plan_run.schedule = plan_protected.schedule + plan_open.schedule

        # Merge the per-rule trace tables so each tab shows the full plan (rows concatenated).
        # rule1/2/3 have fixed keys → safe to concat rows. rule6's Operator column is
        # conditional, so rebuild its table from the merged schedule (union-of-keys columns).
        for rk in ("rule1", "rule2", "rule3"):
            base = trace.get(rk, {}).get("output")
            add = trace_open.get(rk, {}).get("output")
            if base and add and "rows" in base and "rows" in add:
                base["rows"] = base["rows"] + add["rows"]
        # Surface the open pass's "Optimized sequence applied" note on the visible tab.
        for n in trace_open.get("rule3", {}).get("notes", []):
            if n not in trace["rule3"]["notes"]:
                trace["rule3"]["notes"].append(n)
        if "rule6" in trace and trace["rule6"].get("output") is not None:
            trace["rule6"]["output"] = to_table([e.as_row() for e in plan_run.schedule])

    _augment_helpers(trace, plan_run, config, masters, actuals=actuals)

    # Rule 8 tab: the active order book by remaining qty. List EVERY active order
    # so the count matches the Orders tab; flag the ones with nothing left to make
    # (fully produced but not yet marked complete) — they aren't scheduled, which
    # is why they don't appear in the schedule/Gantt.
    finished = orderbook.finished_good_by_order(actuals, masters)
    started = orderbook.orders_with_actuals(actuals)
    # Status keyed by each order's (SO#, item) pair — an SO# alone isn't unique.
    status_by_order = {o.key: orderbook.derive_status(o, started) for o in active.values()}

    def _r8_row(o):
        remaining = max(o.ordered_qty - finished.get(o.key, 0.0), 0.0)
        return {"SO No": o.so_no, "Item Code": o.item_code, "Remaining Qty": remaining,
                "SO Delivery Date": fmt_date(o.delivery_date),
                "Status": status_by_order[o.key],
                "In this plan": "scheduled" if remaining > 0 else "no — fully produced, mark complete"}

    # Sort by the real date (not the DD-MM-YYYY display string).
    r8_rows = [_r8_row(o) for o in sorted(active.values(),
                                          key=lambda o: (o.delivery_date, o.so_no, o.item_code))]
    scheduled = sum(1 for r in r8_rows if r["Remaining Qty"] > 0)
    trace["rule8"] = {
        "input": to_table([{"Active orders": len(active),
                            "Scheduled (work remaining)": scheduled,
                            "Actuals applied": len(actuals)}]),
        "output": to_table(r8_rows),
        "config": config.to_dict(),
        "notes": [
            "Unified Plan: every active order is listed at its remaining qty "
            "(ordered − finished good at the DISPATCH/last-step gate). Production "
            "still mid-routing (WIP) does not reduce it. Completed orders are excluded.",
            "An order fully produced but not yet marked complete shows Remaining 0 "
            "and 'In this plan = no' — it isn't scheduled until you tick 'mark "
            "complete' on a Rule 7 entry to archive it.",
        ],
        "error": None, "reached": True,
    }

    run_id = uuid.uuid4().hex[:8]
    _store_run(run_id, trace)
    gantt = build_gantt(plan_run.schedule, plan_run.batches_prioritized, masters,
                        status_by_order=status_by_order)
    orders = to_table(orderbook.order_rows(active, completed, actuals, masters))

    # Expected completion per order (SO#, item) = the latest scheduled End across
    # every op tied to that order, keyed "SO\x1fitem" (a schedule entry can carry
    # several so_refs when Rule 1 consolidated same-item SOs into one batch).
    exp_end_dates = {}
    for e in plan_run.schedule:
        for r in (e.so_refs or []):
            k = f"{r}\x1f{e.item_code}"
            d = e.end.date()
            if k not in exp_end_dates or d > exp_end_dates[k]:
                exp_end_dates[k] = d
    exp_end = {k: d.isoformat() for k, d in exp_end_dates.items()}

    # Staleness info for the Optimize banner: how many of the currently planned
    # orders the applied optimization covers (uncovered = uploaded after it ran).
    optimize_meta = {"active": False}
    if prio:
        keys = {f"{l.so_no}{KEY_SEP}{l.item_code}" for l in so_lines}
        covered = sum(1 for k in keys if k in prio["ranks"])
        saved_sig = (prio.get("meta") or {}).get("inputs_sig")
        optimize_meta = {"active": True,
                         "saved_at": (prio.get("meta") or {}).get("saved_at"),
                         "covered": covered, "uncovered": len(keys) - covered,
                         # True when the masters/settings differ from what the
                         # optimization was computed on — its numbers will differ.
                         "inputs_changed": bool(saved_sig and saved_sig != current_inputs_sig)}

    result = {"run_id": run_id, "trace": trace, "report": _report_for_book(masters, so_lines),
              "gantt": gantt, "orders": orders, "config": config.to_dict(),
              "expected_end": exp_end, "optimize_meta": optimize_meta,
              "recovery_meta": recovery_meta,
              "auto_note": book_store.load_auto_note()}
    if joint_fallback:
        # Applied joint ranks drifted out of promise-feasibility; the two-pass
        # plan above ran instead. Surfaced so callers/tests can see the guard fired.
        result["joint_fallback"] = True
    return result


def _commit_orders(pairs):
    """Snapshot each order's current expected completion (from a fresh plan) as
    its promised date, and lock it into the committed lane. Unknown (so, item)
    pairs are silently skipped — set_commitment() returns False for them."""
    from datetime import datetime, date as _date
    result = _plan(_load_plan_config())
    exp = result.get("expected_end", {})
    now = datetime.now().isoformat(timespec="seconds")
    for so, item in pairs:
        iso = exp.get(f"{so}\x1f{item}")
        promised = _date.fromisoformat(iso) if iso else None
        book_store.set_commitment(so, item, "committed", promised, now)


def _preview_urgent_pushes(so, item):
    """If (so, item) is made urgent, which OTHER protected (committed/urgent)
    orders would get pushed past their already-promised date? Re-plans PASS 1
    (protected orders only, the same set `_plan` uses) on a scratch copy of the
    order book with the target order flipped to urgent, and compares each other
    protected order's new expected completion against its stored promise.

    Pure preview — never touches the durable store."""
    import copy as _copy

    masters = _current_masters()
    actuals = book_store.load_actuals()
    config = _load_plan_config()

    scratch = {k: _copy.copy(o) for k, o in book_store.load_active_orders().items()}
    tgt = scratch.get((so, item))
    if tgt is None:
        return []
    tgt.commitment = "urgent"
    tgt.promised_date = tgt.delivery_date

    lines = orderbook.active_so_lines(scratch, actuals, masters)
    protected, _open = orderbook.split_committed_open(lines)

    plan_run = PlanRun(so_lines=protected)
    run_forward(plan_run, config, masters)

    exp = {}
    for e in plan_run.schedule:
        for r in (e.so_refs or []):
            k = (r, e.item_code)
            d = e.end.date()
            if k not in exp or d > exp[k]:
                exp[k] = d

    pushed = []
    for k, o in scratch.items():
        if k == (so, item):
            continue
        if o.commitment in ("committed", "urgent") and o.promised_date:
            newd = exp.get(k)
            if newd and newd > o.promised_date:
                pushed.append({"so": o.so_no, "item": o.item_code,
                               "promised": o.promised_date.isoformat(),
                               "new": newd.isoformat()})
    return pushed


def _load_plan_config() -> Config:
    """The admin's last-saved plan config, or defaults. Never raises: a missing,
    unparseable, or invalid stored value falls back to ``Config()`` so a read
    endpoint can't be 500'd by a bad stored config."""
    raw = book_store.load_plan_config()
    if not raw:
        return Config()
    try:
        cfg = Config.from_dict(json.loads(raw))
        cfg.validate()
        return cfg
    except Exception:
        return Config()


# --------------------------------------------------------------------------- #
# Optimize: search the current book for a better batch sequence (admin action).
# One job at a time, in a background thread (Render free tier = 1 worker); the
# job snapshots its inputs at start so later book edits can't race it. Only the
# APPLIED result is durable (book_store.save_plan_priority) — a process restart
# mid-run just returns the job to idle, the applied ranks are unaffected.
# --------------------------------------------------------------------------- #
# Evaluation counts — deterministic. One option (owner cap, 2026-07-15): 1,000
# plans TOTAL per click (~2.3 s/plan on the free 0.1-CPU Render tier ≈ 40 min),
# split equally across the overlap contenders by sweep_optimize. Legacy "quick"
# (a cached client's request) runs the same single budget. The user can Stop any
# run and keep the best plan found so far.
_OPT_BUDGETS = {"deep": 1000, "quick": 1000}
_OPT_SEED = 42

_OPTIMIZE = {"state": "idle", "label": None, "budget_evals": 0, "evals": 0,
             "baseline": None, "best": None, "error": None, "elapsed_s": 0.0,
             "started_mono": None, "result": None, "cancel": False,
             "mode": "local", "job_id": None, "cloud_payload": None,
             "cloud_failed": False, "base_config": None, "auto": False}
_OPTIMIZE_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Cloud compute (GitHub Actions) — the full 2,400-plan contest runs on a free
# GitHub runner (2 vCPU ≈ 40× the Render instance) in ~8-10 min. Enabled by two
# env vars on Render; with either missing, Optimize computes locally exactly as
# before. The worker authenticates with OPTIMIZE_WORKER_SECRET; if the cloud
# never answers, a watchdog falls back to local compute so the button always
# works. Spec: docs/superpowers/specs/2026-07-15-optimize-settings-sweep-design.md.
# --------------------------------------------------------------------------- #
def _cloud_config():
    token = os.environ.get("GITHUB_DISPATCH_TOKEN", "").strip()
    secret = os.environ.get("OPTIMIZE_WORKER_SECRET", "").strip()
    if not token or not secret:
        return None
    return {"token": token, "secret": secret,
            "repo": os.environ.get("GITHUB_REPO", "riittiin/anvitech-ppc-engine"),
            "workflow": os.environ.get("OPTIMIZE_WORKFLOW", "optimize.yml"),
            "timeout_min": float(os.environ.get("OPTIMIZE_CLOUD_TIMEOUT_MIN", "20"))}


def _dispatch_workflow(cloud, job_id) -> bool:
    """Trigger the optimize workflow on GitHub (workflow_dispatch). Stdlib only.
    Token "manual" skips the GitHub call — the operator starts the worker
    themselves (local E2E testing, or any non-GitHub compute box)."""
    if cloud["token"] == "manual":
        return True
    import urllib.request
    url = (f"https://api.github.com/repos/{cloud['repo']}"
           f"/actions/workflows/{cloud['workflow']}/dispatches")
    body = json.dumps({"ref": "main", "inputs": {"job_id": job_id}}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {cloud['token']}",
        "Accept": "application/vnd.github+json",
        "Content-Type": "application/json",
        "User-Agent": "anvitech-ppc",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 204
    except Exception:
        return False


def _worker_secret_ok(request: Request) -> bool:
    secret = os.environ.get("OPTIMIZE_WORKER_SECRET", "")
    given = request.headers.get("x-worker-secret", "")
    return bool(secret) and hmac.compare_digest(secret, given)


# --------------------------------------------------------------------------- #
# Self-tuning trigger (spec 2026-07-15-self-tuning-plan, owner-revised):
# deliberate admin actions re-optimize IMMEDIATELY; punch entry re-optimizes
# only when the clerk presses "Done entering" (POST /optimize/done). No
# timers. AUTO_OPTIMIZE=0 is internal test isolation only — never user-facing.
# --------------------------------------------------------------------------- #
_AUTO = {"pending": False}
_AUTO_LOCK = threading.Lock()


def _auto_enabled() -> bool:
    return os.environ.get("AUTO_OPTIMIZE", "1") != "0"


def _current_book_sig() -> str:
    masters = _current_masters()
    actuals = book_store.load_actuals()
    lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                      actuals, masters)
    absences = (book_store.load_absences()
                if hasattr(book_store, "load_absences") else None)
    return optimize_service.book_signature(lines, absences=absences)


def _auto_note_write(text: str):
    from datetime import datetime as _dt
    book_store.save_auto_note({"text": text,
                               "at": _dt.now().isoformat(timespec="seconds")})


def _applied_book_sig():
    """The book_sig recorded against the last applied optimization, read
    directly (not via book_store.load_plan_priority(), which treats an empty
    ranks dict as "nothing applied" for planning purposes — a different
    concern from "does the last contest still match today's book")."""
    raw = book_store.get_store().kv_get(book_store.PLAN_PRIORITY_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return (data.get("meta") or {}).get("book_sig")


def _try_start_auto() -> bool:
    """The one decision point: start an auto contest now if it makes sense."""
    if not _auto_enabled():
        return False
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running":
            with _AUTO_LOCK:
                _AUTO["pending"] = True          # chain after the current run
            return False
    if _cloud_config() is None:
        _auto_note_write("Auto-optimize skipped — cloud compute unavailable; "
                         "will retry on the next change.")
        return False                             # auto is cloud-only
    applied_sig = _applied_book_sig()
    try:
        if applied_sig == _current_book_sig():
            return False                         # nothing material changed
    except Exception:
        return False
    try:
        _start_optimize(_OPT_BUDGETS["deep"], "auto", background=True, auto=True)
        return True
    except HTTPException:
        return False                             # e.g. nothing to optimize


def _bump_book_changed():
    """Call after DELIBERATE admin mutations (upload, delete, commitment,
    absence, Settings save). Punch saves do NOT call this — the clerk's
    Done button does."""
    _try_start_auto()


def _drain_pending_auto():
    """Called when a contest finishes: if the book changed mid-run, follow up."""
    with _AUTO_LOCK:
        pending, _AUTO["pending"] = _AUTO["pending"], False
    if pending:
        _try_start_auto()


def _start_optimize(budget_evals: int, label: str, background: bool = True,
                    auto: bool = False):
    """Snapshot the book + config and run the settings-sweep contest. With
    committed/urgent orders present, only the OPEN pass is searched (against
    the protected pass's reservations) — a found plan can never disturb a
    promise. Cloud-configured → dispatch the full contest to GitHub Actions;
    otherwise (or on any cloud failure) compute locally."""
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running":
            raise HTTPException(status_code=409,
                                detail="an optimization is already running")

        config = _load_plan_config()
        masters = _current_masters()
        base_config = config      # as saved — the basis for the inputs fingerprint
        actuals = book_store.load_actuals()
        orders = book_store.load_active_orders()
        try:
            setup = optimize_service.prepare_contest(orders, actuals, masters, config)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        cloud = _cloud_config()
        job_id = uuid.uuid4().hex
        if cloud:
            payload = optimize_service.build_payload(
                orders, actuals, book_store.load_masters_bytes(), base_config,
                seed=_OPT_SEED)
            denom = (optimize_service.CLOUD_BUDGET_PER_CANDIDATE
                     * len(optimizer.sweep_contenders(
                         setup.search_config.overlap_percent,
                         optimize_service.CLOUD_OVERLAP_CANDIDATES)))
        else:
            payload = None
            denom = optimizer.sweep_total_evals(budget_evals,
                                                setup.search_config.overlap_percent)

        _OPTIMIZE.update(state="running", label=label, budget_evals=denom,
                         evals=0, baseline=None, best=None, error=None, result=None,
                         elapsed_s=0.0, started_mono=time.monotonic(), cancel=False,
                         mode=("cloud" if cloud else "local"), job_id=job_id,
                         cloud_payload=payload, cloud_failed=False,
                         base_config=base_config, auto=bool(auto))

    def local_job():
        try:
            def on_progress(evals, best):
                _OPTIMIZE["evals"] = evals
                _OPTIMIZE["best"] = best

            # Honest baseline = the admin's CURRENT plan for these orders, under their
            # real config (expedite as set) — what they have if they DON'T optimize.
            real_baseline = _OPTIMIZE.get("baseline")
            if real_baseline is None:
                base_sched, base_lines = _all_lines_schedule(setup, masters, None, False)
                real_baseline = optimizer.plan_metrics(base_sched, base_lines,
                                                       setup.config.plan_start_date)

            # One-pool contest (2026-07-15): search ALL active lines together —
            # committed included — with the promise veto (``setup.feasible``)
            # replacing the per-candidate reservation guard. All-open books:
            # joint_target == target and feasible is None, so this is
            # byte-identical to the old open-only sweep.
            sw = optimizer.sweep_optimize(setup.joint_target, setup.search_config,
                                          masters, budget_evals=budget_evals,
                                          seed=_OPT_SEED, on_progress=on_progress,
                                          should_cancel=lambda: _OPTIMIZE.get("cancel"),
                                          candidate_setup=None,
                                          feasible=setup.feasible)
            res = sw.result
            _finalize_optimize(job_id, base_config, real_baseline, label,
                               winner_overlap=sw.overlap_percent, ranks=res.ranks,
                               best=res.best, evals=sw.evals, table=sw.table,
                               cancelled=sw.cancelled)
        except Exception as e:  # noqa: BLE001 — a failed search must report, not hang
            with _OPTIMIZE_LOCK:
                _OPTIMIZE.update(state="failed", error=str(e), cancel=False)

    def cloud_job():
        try:
            # Baseline is computed HERE (one plan, seconds) so the before/after
            # is ready whatever the worker does.
            base_sched, base_lines = _all_lines_schedule(setup, masters, None, False)
            real_baseline = optimizer.plan_metrics(base_sched, base_lines,
                                                   setup.config.plan_start_date)
            with _OPTIMIZE_LOCK:
                _OPTIMIZE["baseline"] = real_baseline

            if not _dispatch_workflow(cloud, job_id):
                with _OPTIMIZE_LOCK:
                    still_mine = (_OPTIMIZE["state"] == "running"
                                  and _OPTIMIZE["job_id"] == job_id)
                    if still_mine:
                        _OPTIMIZE["mode"] = "local"
                        _OPTIMIZE["budget_evals"] = optimizer.sweep_total_evals(
                            budget_evals, setup.search_config.overlap_percent)
                if still_mine:
                    local_job()
                return

            deadline = time.monotonic() + cloud["timeout_min"] * 60
            while True:
                time.sleep(2)
                with _OPTIMIZE_LOCK:
                    if (_OPTIMIZE["state"] != "running"
                            or _OPTIMIZE["job_id"] != job_id):
                        return           # the worker delivered (or the job was cleared)
                    # A worker-reported failure short-circuits the deadline.
                    timed_out = (time.monotonic() > deadline
                                 or _OPTIMIZE.get("cloud_failed"))
                    was_cancelled = _OPTIMIZE["cancel"]
                    if timed_out:
                        if was_cancelled:
                            # Cancelled and the cloud never answered → just stop.
                            _OPTIMIZE.update(state="failed", cancel=False,
                                             error="stopped — the cloud run did not "
                                                   "answer before the timeout")
                            return
                        _OPTIMIZE["mode"] = "local"
                        _OPTIMIZE["budget_evals"] = optimizer.sweep_total_evals(
                            budget_evals, setup.search_config.overlap_percent)
                        _OPTIMIZE["evals"] = 0
                if timed_out:
                    local_job()          # cloud never answered → compute here
                    return
        except Exception as e:  # noqa: BLE001
            with _OPTIMIZE_LOCK:
                _OPTIMIZE.update(state="failed", error=str(e), cancel=False)

    job = cloud_job if cloud else local_job
    if background:
        threading.Thread(target=job, daemon=True).start()
    else:
        job()
    return _optimize_status()


def _finalize_optimize(job_id, base_config, real_baseline, label, *,
                       winner_overlap, ranks, best, evals, table, cancelled):
    """Store a finished contest (local sweep OR cloud worker result) as the
    Optimize outcome — one place computes improved/inputs_sig for both paths."""
    improved = (best is not None and real_baseline is not None
                and optimizer.score(best) < optimizer.score(real_baseline))
    # Fingerprint against the WINNING settings: Apply persists the winning
    # overlap into the saved plan config, so the staleness check must
    # compare future plans against exactly that config.
    inputs_sig = _inputs_signature(replace(base_config,
                                           overlap_percent=winner_overlap))
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["job_id"] != job_id:
            return False
        _OPTIMIZE.update(
            state="done", evals=evals, baseline=real_baseline, best=best,
            cancel=False, cloud_payload=None, error=None,
            elapsed_s=round(time.monotonic() - _OPTIMIZE["started_mono"], 1),
            result={"ranks": ranks, "improved": improved,
                    "cancelled": cancelled,
                    "baseline": real_baseline, "best": best,
                    "budget": label, "seed": _OPT_SEED,
                    "inputs_sig": inputs_sig,
                    "best_overlap": winner_overlap,
                    "current_overlap": base_config.overlap_percent,
                    "sweep_table": table,
                    # Every contest is one-pool now (2026-07-15): ALL active
                    # lines compete, promise veto instead of a reserved wall.
                    "joint": True})
    if _OPTIMIZE.get("auto"):
        try:
            _auto_apply_result()
        except Exception:   # noqa: BLE001 — an auto note must never crash a result
            pass
    _drain_pending_auto()
    return True


def _optimize_status():
    with _OPTIMIZE_LOCK:
        elapsed = _OPTIMIZE["elapsed_s"]
        if _OPTIMIZE["state"] == "running" and _OPTIMIZE["started_mono"] is not None:
            elapsed = round(time.monotonic() - _OPTIMIZE["started_mono"], 1)
        res = _OPTIMIZE.get("result") or {}
        return {"state": _OPTIMIZE["state"], "budget": _OPTIMIZE["label"],
                "budget_evals": _OPTIMIZE["budget_evals"], "evals": _OPTIMIZE["evals"],
                "baseline": _OPTIMIZE["baseline"], "best": _OPTIMIZE["best"],
                "error": _OPTIMIZE["error"], "elapsed_s": elapsed,
                "improved": res.get("improved"), "cancelled": res.get("cancelled", False),
                "best_overlap": res.get("best_overlap"),
                "current_overlap": res.get("current_overlap"),
                "mode": _OPTIMIZE.get("mode", "local"),
                # Whether the current/last run was auto-triggered (self-tuning) rather
                # than a manual admin Start click — the UI uses this to suppress a
                # stale Apply/Discard panel after an auto contest has already applied
                # (or discarded) itself.
                "auto": bool(_OPTIMIZE.get("auto")),
                # The job id is only useful together with the worker secret
                # (manual-dispatch mode / debugging) — not sensitive by itself.
                "job_id": _OPTIMIZE.get("job_id") if _OPTIMIZE["state"] == "running" else None,
                "stopping": bool(_OPTIMIZE.get("cancel")) and _OPTIMIZE["state"] == "running"}


def _optimize_cancel():
    """Ask a running search to stop after its current plan; it keeps the best plan
    found so far (state becomes 'done'). No-op if nothing is running."""
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running":
            _OPTIMIZE["cancel"] = True
    return _optimize_status()


def _all_lines_schedule(setup, masters, ranks, joint):
    """A schedule covering ALL active lines, built the SAME way for both sides of
    the Optimize before/after (baseline and incumbent) so their scores are
    same-domain (the previous open-only-vs-all-lines mismatch is the bug this fixes).

    - No protected orders, or a JOINT rank set: one pass over every line with the
      ranks (expedite off). A joint set is re-validated against the promise ceiling;
      on drift the ranks are dropped and the two-pass build below is used (mirrors
      ``_plan``).
    - Legacy open-only ranks / no ranks with protected present: two-pass — pass 1 =
      protected under ``setup.config``, pass 2 = the open target under the ranked
      config with ``setup.reserved`` + the ranks — schedules merged.

    Pass 1 here approximates ``_plan``'s protected pass (it does NOT replay the
    promise-recovery ranks). That is acceptable: BOTH the baseline and the incumbent
    are built through this one helper, so the comparison stays internally consistent.
    Returns (schedule, all_lines)."""
    all_lines = list(setup.joint_target)
    protected = setup.protected
    ranked_config = replace(setup.config, expedite_window_min=0) if ranks else setup.config

    if not protected or (joint and ranks):
        pr = PlanRun(so_lines=list(all_lines))
        run_forward(pr, ranked_config, masters, priority_rank=ranks)
        if not protected or optimizer.promise_ceiling_ok(pr.schedule, all_lines):
            return pr.schedule, all_lines
        ranks, ranked_config = None, setup.config     # joint drift -> two-pass, no ranks

    p1 = PlanRun(so_lines=list(protected))
    run_forward(p1, setup.config, masters)
    p2 = PlanRun(so_lines=list(setup.target))
    run_forward(p2, ranked_config, masters, reserved=setup.reserved, priority_rank=ranks)
    return p1.schedule + p2.schedule, all_lines


def _incumbent_metrics():
    """Score of the plan users currently see: the applied ranks (if any) replayed
    on TODAY'S book, measured over ALL active lines (committed + open) — the same
    domain the contest's ``best`` is scored on. Expedite off when ranks replay."""
    config = _load_plan_config()
    masters = _current_masters()
    actuals = book_store.load_actuals()
    orders = book_store.load_active_orders()
    setup = optimize_service.prepare_contest(orders, actuals, masters, config)
    prio = book_store.load_plan_priority()
    ranks = (prio or {}).get("ranks") or None
    joint = bool(prio and (prio.get("meta") or {}).get("joint"))
    schedule, all_lines = _all_lines_schedule(setup, masters, ranks, joint)
    return optimizer.plan_metrics(schedule, all_lines, setup.config.plan_start_date)


def _auto_apply_result():
    """Called after an auto contest lands in state=done: apply iff strictly
    better than the incumbent; write the note either way."""
    from datetime import datetime
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


def _optimize_apply():
    """Persist the last completed run's ranks — from then on every Plan replays
    them (admin, user, and the auto re-plan after actuals)."""
    from datetime import datetime
    with _OPTIMIZE_LOCK:
        res = _OPTIMIZE.get("result")
        if _OPTIMIZE["state"] != "done" or not res:
            raise HTTPException(status_code=409,
                                detail="no completed optimization to apply")
        meta = {"saved_at": datetime.now().isoformat(timespec="seconds"),
                "budget": res["budget"], "seed": res["seed"],
                "baseline": res["baseline"], "best": res["best"],
                "covered": len(res["ranks"]),
                "inputs_sig": res.get("inputs_sig"),
                "best_overlap": res.get("best_overlap"),
                "book_sig": _current_book_sig(),
                "joint": res.get("joint")}
        book_store.save_plan_priority(res["ranks"], meta)
        # Settings sweep: the winning overlap becomes THE saved plan setting (the
        # single config every Plan loads and the Settings panel shows). Unchanged
        # winner -> no write, no churn.
        best_ov = res.get("best_overlap")
        if best_ov is not None:
            cfg = _load_plan_config()
            if cfg.overlap_percent != best_ov:
                book_store.save_plan_config(json.dumps(
                    replace(cfg, overlap_percent=best_ov).to_dict()))
        # Clear the in-memory job so a later page refresh doesn't re-show the
        # "Apply" panel for a plan that's already applied.
        _OPTIMIZE.update(state="idle", result=None, best=None, baseline=None)
        return meta


def _optimize_clear():
    book_store.clear_plan_priority()
    return {"cleared": True}


# --------------------------------------------------------------------------- #
# Promise recovery: when a disruption makes committed orders slip, automatically
# re-sequence the committed set to protect the most promises. No control — the
# planner triggers it. Runs in its OWN background slot (separate from the manual
# Optimize) so the two never collide. Only the persisted ranks are durable.
# --------------------------------------------------------------------------- #
_RECOVERY_BUDGET = 150   # promise-recovery search depth (most of the prize; see the design)
_RECOVERY = {"state": "idle", "signature": None}
_RECOVERY_LOCK = threading.Lock()


def _recovery_signature(protected):
    """A committed set's identity for cache freshness: each order's key AND its promised
    date. A new commit/uncommit or a changed promise changes the signature and re-triggers;
    a mere quantity change (feedback) does not — the recovered ORDER still replays."""
    return sorted(
        f"{l.so_no}{KEY_SEP}{l.item_code}\x1e{l.promised_date.isoformat() if l.promised_date else ''}"
        for l in protected)


def _committed_slip_metrics(schedule, protected, plan_start):
    return optimizer.promise_slip_metrics(schedule, protected, plan_start)


def _maybe_start_recovery(protected, config, masters):
    """Kick off the background promise-recovery search for THIS committed set, unless one
    is already running or a recovery for exactly this signature already exists."""
    sig = _recovery_signature(protected)
    with _RECOVERY_LOCK:
        if _RECOVERY["state"] == "running":
            return
        existing = book_store.load_promise_recovery()
        if existing and existing.get("meta", {}).get("signature") == sig:
            return                      # already recovered for this exact committed set
        _RECOVERY.update(state="running", signature=sig)

    lines, cfg, ms = list(protected), config, masters
    # Expedite would dynamically re-sort ops and cancel the recovered order (same as the
    # open-order optimizer) — search & replay in the pure non-delay model.
    search_cfg = replace(cfg, expedite_window_min=0)

    def job():
        try:
            from datetime import datetime
            base = PlanRun(so_lines=list(lines))
            run_forward(base, search_cfg, ms)
            bm = optimizer.promise_slip_metrics(base.schedule, lines, search_cfg.plan_start_date)
            res = optimizer.optimize(lines, search_cfg, ms, budget_evals=_RECOVERY_BUDGET,
                                     seed=_OPT_SEED, objective="promise_slip")
            meta = {"saved_at": datetime.now().isoformat(timespec="seconds"),
                    "signature": sig,
                    "slip_before": bm["promise_slip_days"], "slip_after": res.best["promise_slip_days"],
                    "promises_saved": max(bm["promises_missed"] - res.best["promises_missed"], 0)}
            # Safety: res.best <= baseline (seeded with promised-date order), so replaying
            # the ranks can never make committed orders worse. Save unconditionally so the
            # signature check stops us re-triggering the same search.
            book_store.save_promise_recovery(res.ranks, meta)
        except Exception:  # noqa: BLE001 — a failed recovery must not break planning
            pass
        finally:
            with _RECOVERY_LOCK:
                _RECOVERY["state"] = "idle"

    threading.Thread(target=job, daemon=True).start()


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post("/upload")
async def upload(request: Request, file: UploadFile = File(...)):
    """Merge an uploaded workbook into the order book. New SO numbers become
    pending orders; known ones are flagged. Masters are updated (latest-wins,
    kept if the file omits them). Admin only."""
    require_admin(request)
    if int(request.headers.get("content-length") or 0) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 10 MB)")
    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="file too large (max 10 MB)")
    try:
        so_lines, masters = load_all(io.BytesIO(contents))
    except Exception as e:  # noqa: BLE001 — surface parse failures to the user
        raise HTTPException(status_code=400, detail=f"Could not read Excel: {e}")

    masters_updated = False
    if masters.routings:  # only replace masters when the file actually has them
        book_store.save_masters_bytes(contents)
        _MASTERS_CACHE["masters"] = None  # invalidate cache → re-read on next plan
        masters_updated = True

    active = book_store.load_active_orders()
    completed = book_store.load_completed_orders()
    new_orders, flags = orderbook.merge_upload(
        so_lines, active, completed, first_seen=date.today().isoformat())
    book_store.add_orders(new_orders)

    book_lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                           book_store.load_actuals(), masters)
    result = {
        "name": file.filename,
        "added": len(new_orders),
        "flagged": flags,
        "masters_updated": masters_updated,
        "summary": {"items": len(masters.routings), "machines": len(masters.machines)},
        "report": _report_for_book(masters, book_lines),
    }
    _bump_book_changed()
    return result


@app.post("/run")
def run(request: Request, req: Optional[RunRequest] = None):
    """Plan the order book. Admin may set the config (and persist it on an
    explicit Plan click); a user always plans with the admin's saved config, so
    everyone sees one consistent plan."""
    role = getattr(request.state, "role", auth.USER)
    sent = req.config if req else None
    persist = bool(req.persist) if req else False

    # The persist flag is the single switch: an admin's explicit Plan click
    # (persist=True) applies AND saves the submitted config; everything else — an
    # admin auto-load on page open, or any user — plans with the saved config
    # (defaults if none saved). So everyone sees one consistent, planner-set plan.
    if role == auth.ADMIN and persist:
        try:
            config = Config.from_dict(sent)   # bad date/type raises here
            config.validate()
        except (ValueError, TypeError) as e:
            raise HTTPException(status_code=400, detail=f"invalid config: {e}")
        book_store.save_plan_config(json.dumps(config.to_dict()))
        _bump_book_changed()
    elif book_store.load_plan_config():
        config = _load_plan_config()   # a saved plan exists → everyone sees it
    elif sent is not None:
        # No saved plan yet → honor the caller's config (the web UI defaults, e.g.
        # operator logic / downtime ON), falling back to defaults if it's invalid.
        config = Config.from_dict(sent)
        try:
            config.validate()
        except ValueError:
            config = Config()
    else:
        config = _load_plan_config()   # engine defaults
    return _plan(config)


@app.post("/rerun")
def rerun(request: Request, req: Optional[RunRequest] = None):
    return run(request, req)  # unified — Run and Rerun are the same action now


@app.get("/orders")
def orders():
    active = book_store.load_active_orders()
    completed = book_store.load_completed_orders()
    actuals = book_store.load_actuals()
    return {"orders": to_table(orderbook.order_rows(active, completed, actuals, _current_masters()))}


@app.post("/orders/delete")
def delete_orders(req: DeleteRequest, request: Request):
    """Permanently delete the given orders — each a (SO number, item code) pair —
    plus their actuals. Admin only, guarded by re-entering the admin password."""
    require_admin(request)
    require_password(request, req.password)
    pairs = [(o[0], o[1]) for o in req.orders if len(o) == 2]
    n = book_store.delete_orders(pairs)
    _bump_book_changed()
    return {"deleted": n}


@app.post("/orders/clear")
def clear_orders(req: ClearRequest, request: Request):
    """Permanently delete ALL orders + actuals (masters are kept). Admin only, and
    guarded by re-entering the admin password."""
    require_admin(request)
    require_password(request, req.password)
    book_store.delete_all()
    _bump_book_changed()
    return {"cleared": True}


@app.post("/orders/commit")
def commit_orders_ep(req: CommitRequest, request: Request):
    """Lock the given orders into the committed lane: each order's current
    expected completion (from a fresh plan) becomes its locked promise. Admin
    only."""
    require_admin(request)
    _commit_orders([(o[0], o[1]) for o in req.orders if len(o) == 2])
    _bump_book_changed()
    return {"committed": len(req.orders)}


@app.post("/orders/uncommit")
def uncommit_orders_ep(req: CommitRequest, request: Request):
    """Release the given orders back to the open lane (clears their promise).
    Admin only."""
    require_admin(request)
    for o in req.orders:
        if len(o) == 2:
            book_store.clear_commitment(o[0], o[1])
    _bump_book_changed()
    return {"uncommitted": len(req.orders)}


@app.post("/orders/urgent")
def urgent_order_ep(req: UrgentRequest, request: Request):
    """Mark one order urgent. Previews which other committed/urgent orders would
    get pushed past their promise; without `confirm`, a non-empty preview is
    returned as a warning instead of being applied. Admin only."""
    require_admin(request)
    from datetime import datetime
    pushed = _preview_urgent_pushes(req.so, req.item)
    if pushed and not req.confirm:
        return {"warning": pushed}
    order = book_store.load_active_orders().get((req.so, req.item))
    book_store.set_commitment(req.so, req.item, "urgent",
                              order.delivery_date if order else None,
                              datetime.now().isoformat(timespec="seconds"))
    _bump_book_changed()
    return {"urgent": True}


@app.post("/optimize")
def optimize_ep(req: OptimizeRequest, request: Request):
    """Start a sequence-search job on the current order book (admin only).
    Returns the job status immediately; poll GET /optimize/status for progress."""
    require_admin(request)
    if req.budget not in _OPT_BUDGETS:
        raise HTTPException(status_code=400,
                            detail=f"budget must be one of {sorted(_OPT_BUDGETS)}")
    return _start_optimize(_OPT_BUDGETS[req.budget], req.budget)


@app.get("/optimize/status")
def optimize_status_ep():
    """Live progress of the current/last optimization (any logged-in role)."""
    return _optimize_status()


@app.post("/optimize/cancel")
def optimize_cancel_ep(request: Request):
    """Stop a running search; it keeps the best plan found so far. Admin only."""
    require_admin(request)
    return _optimize_cancel()


@app.post("/optimize/apply")
def optimize_apply_ep(request: Request):
    """Persist the last completed run's optimized order — every Plan replays it.
    Admin only."""
    require_admin(request)
    return _optimize_apply()


@app.post("/optimize/clear")
def optimize_clear_ep(request: Request):
    """Remove the applied optimization (back to the pure Rule-3 order). Admin only."""
    require_admin(request)
    return _optimize_clear()


@app.post("/optimize/done")
def optimize_done_ep(request: Request):
    """The Capture-tab 'Done entering' button — ANY logged-in role. Starts the
    self-tuning contest over today's book (no-op when nothing changed)."""
    started = _try_start_auto()
    return {"started": started, "state": _optimize_status()["state"]}


# --------------------------------------------------------------------------- #
# Cloud-worker endpoints — called by the GitHub Actions runner, authenticated
# by the OPTIMIZE_WORKER_SECRET header (see the gatekeeper bypass). They only
# answer for the currently running job id.
# --------------------------------------------------------------------------- #
def _require_worker(request: Request):
    if getattr(request.state, "role", None) != "worker":
        raise HTTPException(status_code=403, detail="worker secret required")


class WorkerProgress(BaseModel):
    job_id: str
    evals: int = 0
    best: Optional[dict] = None


class WorkerResult(BaseModel):
    job_id: str
    winner_overlap: Optional[int] = None
    ranks: dict = Field(default_factory=dict)
    best: Optional[dict] = None
    rows: list = Field(default_factory=list)
    evals: int = 0
    cancelled: bool = False
    error: Optional[str] = None


@app.get("/optimize/job/{job_id}")
def optimize_job_ep(job_id: str, request: Request):
    """The contest payload for the worker (book snapshot + config + budget)."""
    _require_worker(request)
    with _OPTIMIZE_LOCK:
        if (_OPTIMIZE["state"] == "running" and _OPTIMIZE.get("job_id") == job_id
                and _OPTIMIZE.get("cloud_payload")):
            return {"payload": _OPTIMIZE["cloud_payload"],
                    "cancel": bool(_OPTIMIZE["cancel"])}
    raise HTTPException(status_code=404, detail="no such running job")


@app.post("/optimize/progress")
def optimize_progress_ep(req: WorkerProgress, request: Request):
    """Worker heartbeat: updates the progress bar; the response tells the
    worker whether the admin pressed Stop (it then posts its best-so-far)."""
    _require_worker(request)
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running" and _OPTIMIZE.get("job_id") == req.job_id:
            _OPTIMIZE["evals"] = max(int(req.evals), _OPTIMIZE["evals"])
            if req.best:
                _OPTIMIZE["best"] = req.best
            return {"cancel": bool(_OPTIMIZE["cancel"])}
    raise HTTPException(status_code=404, detail="no such running job")


@app.post("/optimize/result")
def optimize_result_ep(req: WorkerResult, request: Request):
    """The finished contest from the worker. An error report makes the watchdog
    fall back to local compute immediately (the button must always work)."""
    _require_worker(request)
    with _OPTIMIZE_LOCK:
        running = (_OPTIMIZE["state"] == "running"
                   and _OPTIMIZE.get("job_id") == req.job_id)
        base_config = _OPTIMIZE.get("base_config")
        baseline = _OPTIMIZE.get("baseline")
        label = _OPTIMIZE.get("label")
        if running and (req.error or req.best is None or req.winner_overlap is None):
            _OPTIMIZE["cloud_failed"] = True     # watchdog → local fallback now
            _OPTIMIZE["error"] = req.error or "cloud worker returned no result"
            return {"ok": True, "fallback": "local"}
    if not running:
        raise HTTPException(status_code=404, detail="no such running job")
    stored = _finalize_optimize(req.job_id, base_config, baseline, label,
                                winner_overlap=req.winner_overlap,
                                ranks=req.ranks, best=req.best, evals=req.evals,
                                table=req.rows, cancelled=req.cancelled)
    if not stored:
        raise HTTPException(status_code=409, detail="job superseded")
    return {"ok": True}


@app.get("/gantt")
def gantt():
    return _plan(_load_plan_config())["gantt"]


@app.get("/trace/{run_id}")
def get_trace(run_id: str):
    if run_id not in _RUNS:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run_id, "trace": _RUNS[run_id]}


@app.get("/report")
def report():
    masters = _current_masters()
    so_lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                         book_store.load_actuals(), masters)
    return _report_for_book(masters, so_lines)


@app.get("/items")
def items():
    """Item metadata for the Daily Production Entry form."""
    masters = _current_masters()
    active = book_store.load_active_orders()
    completed = book_store.load_completed_orders()
    # Every SO from the orders tab (active + completed), keyed by (SO#, item).
    all_orders = {**completed, **active}   # active wins on any conflict
    items_map = {
        code: {"item_name": routing.description,
               "processes": [p.name for p in routing.processes]}
        for code, routing in masters.routings.items()
    }

    # Two-step picker: pick an SO number, then pick one of THAT SO's item lines.
    # Since an SO number can carry several items, map each SO# -> its open item
    # lines (item code + name), so the second dropdown lists exactly those.
    so_to_items = defaultdict(list)
    for o in active.values():
        so_to_items[o.so_no].append({"item_code": o.item_code, "item_name": o.item_name})
    for so in so_to_items:
        so_to_items[so].sort(key=lambda it: it["item_code"])

    return {
        "items": items_map,
        "shifts": ["1st shift", "2nd shift"],
        "so_to_items": dict(so_to_items),          # {so_no: [{item_code, item_name}, ...]}
        "so_nos": sorted({so for so, _ in all_orders}),      # all SO numbers (distinct)
        "open_so_nos": sorted(so_to_items.keys()),           # SO numbers with an open line
    }


@app.post("/actuals")
def post_actuals(req: ActualRequest):
    try:
        entry_date = date.fromisoformat(req.entry_date)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="entry_date must be YYYY-MM-DD")
    actual = Actual(
        so_no=req.so_no, item_code=req.item_code,
        entry_date=entry_date,
        qty_produced=req.qty_produced, qty_rejected=req.qty_rejected,
        shift=req.shift, item_name=req.item_name, process=req.process,
        actual_setup_min=req.actual_setup_min,
        no_power_min=req.no_power_min, no_operator_min=req.no_operator_min,
        tool_problem_min=req.tool_problem_min,
        machine_breakdown_min=req.machine_breakdown_min,
        no_load_min=req.no_load_min, other_work_min=req.other_work_min,
        remarks=req.remarks, mark_complete=req.mark_complete,
    )
    all_actuals = r7.run(actual)
    completed = False
    if req.mark_complete:
        completed = book_store.complete_order(req.so_no, req.item_code)
    visible = orderbook.actuals_on_latest_date(all_actuals)   # show only the latest day
    return {
        "saved": len(all_actuals),
        "completed_order": completed,
        "actuals": to_table(visible),
        "actuals_ids": [a.id for a in visible],
        "by_item": to_table(r7.aggregate_by_item(all_actuals)),
    }


class RollbackRequest(BaseModel):
    id: str


@app.post("/actuals/rollback")
def rollback_actual(req: RollbackRequest):
    """Roll back ONE saved actual (a mis-punched entry), returning that order to
    normal. If the rolled-back entry was the one that marked the order complete,
    the order is un-archived back to active — unless another remaining entry still
    marks it complete. Available to both roles (it fixes a capture mistake).

    Only the LATEST punched date's entries can be rolled back — earlier days are
    locked (keeps the list small; a completed day stays committed)."""
    before = book_store.load_actuals()
    target = next((a for a in before if a.id == req.id), None)
    if target is None:
        raise HTTPException(status_code=404, detail="entry not found (already rolled back?)")
    if target.entry_date != orderbook.latest_actual_date(before):
        raise HTTPException(
            status_code=400,
            detail="only the latest day's entries can be rolled back; earlier days are locked",
        )
    removed = book_store.delete_actual(req.id)
    if removed is None:
        raise HTTPException(status_code=404, detail="entry not found (already rolled back?)")

    uncompleted = False
    if removed.mark_complete and removed.so_no:
        remaining = book_store.load_actuals()
        still_complete = any(a.key == removed.key and a.mark_complete for a in remaining)
        if not still_complete and removed.key in book_store.load_completed_orders():
            uncompleted = book_store.uncomplete_order(removed.so_no, removed.item_code)

    all_actuals = book_store.load_actuals()
    active = book_store.load_active_orders()
    completed = book_store.load_completed_orders()
    visible = orderbook.actuals_on_latest_date(all_actuals)   # show only the latest day
    return {
        "removed": True,
        "uncompleted_order": uncompleted,
        "actuals": to_table(visible),
        "actuals_ids": [a.id for a in visible],
        "by_item": to_table(r7.aggregate_by_item(all_actuals)),
        "orders": to_table(orderbook.order_rows(active, completed, all_actuals, _current_masters())),
    }


# Static frontend (mounted last so the API routes above take precedence).
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
