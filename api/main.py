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
import csv
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
from engine import efficiency
from engine import operator_master
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
                                              "/optimize/result",
                                              "/optimize/scheduled"))):
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
    """Masters from the latest uploaded workbook, else empty masters.

    The PARSED WORKBOOK is cached in-process (keyed by content hash / store
    config); the app-owned operator table is overlaid on EVERY call so display
    always reflects today's rotation and a freshly-emptied store re-seeds. The
    cache never holds operators — they belong to the store, not the workbook."""
    key = _store_env_key()
    if _MASTERS_CACHE["masters"] is not None and _MASTERS_CACHE["key"] == key:
        base = _MASTERS_CACHE["masters"]
    else:
        raw = book_store.load_masters_bytes()
        if raw is None:
            # No workbook uploaded yet → empty masters; the UI prompts to upload.
            # (There is no bundled demo file anymore — production runs on uploads.)
            base = Masters()
        else:
            _, base = load_all(io.BytesIO(raw))
        _MASTERS_CACHE.update(key=key, masters=base,
                              sha=hashlib.sha256(raw).hexdigest() if raw else "none")
    return _with_operator_overlay(base)


def _with_operator_overlay(base):
    """Overlay the app-owned operator table (seed-once, rotate as-of TODAY for
    display) onto a COPY of the cached workbook masters — the cache is never
    mutated. Seeds the store the first time masters carry operators and no table
    exists yet; persists a lazy rotation advance (flips>0). Empty store + no
    workbook operators → the base is returned unchanged (nothing to overlay)."""
    today = _ist_today()
    table = book_store.load_operator_table()
    if table is None:
        if not base.operators:
            return base                       # nothing to seed from yet
        table = {"week_anchor": operator_master.last_friday(today).isoformat(),
                 "operators": operator_master.seed_rows_from_masters(base)}
        book_store.save_operator_table(table)
    rotated, flips = operator_master.rotate_table(table, today)
    if flips > 0:
        book_store.save_operator_table(rotated)   # lazy catch-up, idempotent
    return replace(base, operators=operator_master.to_operators(
        rotated.get("operators", [])))


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
    # The scheduler's own semantics version: saved ranks were scored under a
    # specific allocation policy, so a deploy that changes it (e.g. the
    # scarce-first operator pick, 2026-07-19) must flag the applied plan stale
    # and let the scheduled contest re-run — otherwise the ranks replay under
    # new semantics behind a green banner (review-caught deploy hole).
    d["scheduler_fingerprint"] = r6.SCHEDULER_FINGERPRINT
    from engine import flow_scheduler as _flow
    d["flow_fingerprint"] = _flow.FLOW_FINGERPRINT
    # Operators live in the store now, so the masters sha no longer covers them.
    # Fold ONLY the table's sorted row CONTENT (name/machines/shift/pin) into
    # the blob so a rotation or an operator edit correctly flags the applied
    # optimization stale (spec 2026-07-18). ids are excluded (churn only) — and
    # so is week_anchor: a net-no-op double-Friday catch-up advances the anchor
    # while leaving every shift identical, and hashing it would make the
    # signature differ from the applied meta until the next Apply, firing a full
    # (non-applying) scheduled contest on every tick. A genuine single rotation
    # still changes the persisted shift content, so it is still caught.
    table = book_store.load_operator_table()
    op_blob = None
    if table:
        op_blob = sorted([[r.get("name", ""), r.get("machines_raw", ""),
                           r.get("shift", ""), bool(r.get("pinned"))]
                          for r in table.get("operators", [])])
    blob = json.dumps([_masters_sha(), d, op_blob], sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def _report_table(masters):
    return to_table([
        {"Kind": r["kind"], "Reference": r["ref"], "Message": r["message"]}
        for r in masters.report
    ])


def _report_after_upload(masters):
    """The validation report for the FILE just uploaded (upload endpoint only).

    Unlike ``_report_for_book`` (book-scoped, used by /run|/gantt|/report), this
    is deliberately loader-scoped: it returns ``masters.report`` AS-IS, so it
    keeps the loader's NO_ROUTING rows for every SO item the file's routings
    can't cover — those items are dropped before they ever reach the order book,
    so the book-scoped report never sees them and the admin would otherwise have
    no way to learn which item codes need a routing added. Also appends the
    absence-orphan rows (ABSENT_OPERATOR_UNKNOWN) for parity with the plan
    report. Does not touch or replay `_report_for_book`; /run stays book-scoped."""
    rows = list(masters.report)
    for name in _absence_orphans(masters):
        rows.append({"kind": "ABSENT_OPERATOR_UNKNOWN", "ref": name,
                     "message": f"absence entry for an operator not in the "
                                f"current masters: ignored"})
    return to_table([
        {"Kind": r["kind"], "Reference": r["ref"], "Message": r["message"]}
        for r in rows
    ])


def _absence_orphans(masters, absences=None) -> list:
    """Names on file in operator absences that are no longer in the current
    masters' Operator & shift Master (e.g. removed/renamed on a re-upload).
    Sorted for stable output. ``absences`` (optional) is the already-loaded
    list — pass it when the caller has one, to avoid a redundant store read;
    ``None`` (default) loads it here."""
    names = {o.name for o in masters.operators}
    if absences is None:
        absences = book_store.load_absences()
    return sorted({a["operator"] for a in absences
                  if a["operator"] not in names})


def _report_for_book(masters, so_lines, absences=None):
    """The validation report scoped to the CURRENT order book. Loader-level rows
    about the masters (pending machines, time coercions, …) pass through, but
    NO_ROUTING is re-derived from the live book: the stored workbook's own SO
    sheet can list orders that were never merged or were later deleted (live
    2026-07-15 bug: a "5 orders without routing" banner for ghost orders while
    every real order planned fine). Also appends a non-blocking row per absence
    entry whose operator is no longer in the masters (ABSENT_OPERATOR_UNKNOWN) —
    the entry is ignored by planning, not an error. ``absences`` (optional) is
    the already-loaded list — pass it when the caller has one (e.g. `/run`),
    to avoid a redundant store read; ``None`` (default) loads it here."""
    rows = [r for r in masters.report if r["kind"] != "NO_ROUTING"]
    seen = set()
    for l in so_lines:
        if l.item_code not in masters.routings and l.item_code not in seen:
            seen.add(l.item_code)
            rows.append({"kind": "NO_ROUTING", "ref": l.item_code,
                         "message": f"SO item '{l.item_code}' has no routing in "
                                    f"Item's process Master; order skipped "
                                    f"(cannot schedule without a recipe)"})
    for name in _absence_orphans(masters, absences=absences):
        rows.append({"kind": "ABSENT_OPERATOR_UNKNOWN", "ref": name,
                     "message": f"absence entry for an operator not in the "
                                f"current masters: ignored"})
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


class AbsenceRequest(BaseModel):
    operator: str
    from_date: str
    to_date: str


class OperatorRequest(BaseModel):
    name: str
    machines_raw: str = ""
    shift: str = ""


class OperatorPatchRequest(BaseModel):
    machines_raw: Optional[str] = None
    shift: Optional[str] = None
    pinned: Optional[bool] = None


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
    operator: str = ""
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
            {"title": "Priority breakdown: slack/critical-ratio per batch (lower slack = more urgent)",
             "table": to_table(breakdown)},
        ]

    if "rule6" in trace and trace["rule6"].get("reached", True):
        timeline, summary = r6.build_machine_view(
            plan_run.schedule, masters, config, plan_run.batches_prioritized)
        trace["rule6"]["tables"] = [
            {"title": "Machine timeline: per-machine queue (Idle before = working minutes the machine waited)",
             "table": to_table(timeline)},
            {"title": "Machine utilization", "table": to_table(summary)},
        ]
        # Analytics tab: utilization & bottlenecks derived from this plan.
        # Operator absences reduce each person's Available (hrs) so utilization
        # stays honest (Task 13 already keeps busy work out of absent days).
        from engine import analytics as _an
        trace["analytics"] = _an.build_analytics(
            plan_run.schedule, masters, config, plan_run.batches_prioritized,
            absences=book_store.load_absences())
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
                    return "-"
                if not iv:
                    return "⚠ needs operator"
                parts = []
                if first in iv:
                    parts.append("1st shift")
                if second in iv:
                    parts.append("2nd shift")
                if manual in iv:
                    parts.append(f"manual {config.manual_start_hour:02d}:00–{config.manual_end_hour:02d}:00")
                return " + ".join(parts) or "-"

            cov_rows = [{"Machine": m.display_name,
                         "Available Hrs/Day": m.available_hrs_per_day,
                         "Runs": _cov_label(mid) + (" (provisional)" if m.provisional else "")}
                        for mid, m in sorted(masters.machines.items())]
            trace["rule6"]["tables"].append({
                "title": "Operator coverage: when each machine can run (from Available "
                         "Hrs/Day + which shifts have a qualified operator)",
                "table": to_table(cov_rows)})
            if cov.get("unmatched_specialties"):
                trace["rule6"]["tables"].append({
                    "title": "Operator specialties that match no machine: check the "
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
        "Overlap % applies to the cutting time only: the 90-min setup is excluded "
        "(the next machine is set up in parallel). A step with no cutting time "
        "(deburring, inspection, washing, packing) does not overlap; its successor "
        "waits for it to fully complete.",
    ]
    if not overlap_rows:
        rule5_notes.append("No scheduled operations yet. Upload orders and click Plan.")
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
    tables = [{"title": "Per item code: output & downtime rollup (minutes summed across ALL entries)",
               "table": to_table(r7.aggregate_by_item(actuals))}]
    if progress:
        tables.insert(0, {
            "title": "Per-process progress: pieces cleared at each step (the floor's reality; "
                     "drives the next Plan's per-process schedule)",
            "table": to_table(progress)})
    list_note = (f"Showing the {fmt_date(latest)} entries (the latest day): only these can be "
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
def _plan(config: Config):
    masters = _current_masters()
    # Fingerprint of the plan-shaping inputs as REQUESTED (base config, before the
    # actuals-driven start advance) — compared against the applied optimization's.
    # Computed on the SAVED (unresolved) config: an auto plan_start_date is None
    # here, so a moving 'today' can never look like a settings change.
    current_inputs_sig = _inputs_signature(config)
    # The response echoes the SAVED (unresolved) config — auto mode (null) must
    # survive the round-trip, or the Settings UI would reflect today back as a
    # FIXED date and the next save would silently lose auto (live 2026-07-18 bug).
    saved_config_dict = config.to_dict()
    # Resolve auto (None) plan start -> today (IST) so the engine never sees None.
    config = _resolve_config(config)
    # What the auto/fixed start resolved to — a separate, display-only response
    # key (the date the plan clock starts from, before any actuals advance).
    resolved_plan_start = config.plan_start_date.isoformat()
    active = book_store.load_active_orders()
    completed = book_store.load_completed_orders()
    actuals = book_store.load_actuals()
    # Operator absences: physical unavailability, not a promise reservation —
    # reserved in the single plan pass. Lanes never reserve time. Loaded once
    # and reused below for the validation report (ABSENT_OPERATOR_UNKNOWN).
    absences_raw = book_store.load_absences()
    ab = optimize_service.absence_reservations(absences_raw)

    so_lines = orderbook.active_so_lines(active, actuals, masters)   # remaining = ordered − finished good

    # Advance the plan clock past days already worked: once a day's production is
    # punched, the re-plan starts from the NEXT working day's first shift, not the
    # original date (a config COPY so the persisted config keeps its base date).
    eff_start = orderbook.effective_plan_start_date(actuals, config.plan_start_date,
                                                    masters.calendar)
    if eff_start != config.plan_start_date:
        config = replace(config, plan_start_date=eff_start)

    # Operators effective AS OF the plan's start (Friday shift-1 rule): a plan
    # that BEGINS on/after a rotation Friday runs the rotated shifts, even when
    # computed on the off day. `_current_masters()` already overlaid as-of TODAY;
    # re-overlay as-of `eff_start` onto this (already fresh) masters copy so the
    # schedule, Gantt, and analytics all agree. Pure view — nothing persisted.
    op_table = book_store.load_operator_table()
    if op_table:
        masters = replace(masters, operators=operator_master.operators_as_of(
            op_table, eff_start))

    # An applied Optimize run (the saved rank map) is replayed as ONE pass over
    # every active line. Lanes (open/committed/urgent) are pure status labels —
    # they have no scheduling effect (owner pivot, 2026-07-16). No saved
    # optimization -> plans are byte-identical to the pre-optimize plan.
    prio = book_store.load_plan_priority()
    ranks = prio["ranks"] if prio else None

    # The optimized ranks ARE the prioritization. The Expedite window dynamically
    # re-sorts ops by slack at schedule time, which would OVERRIDE the ranks and cancel
    # the whole optimization out (the sequence stops mattering). So the ranked orders
    # are planned in the pure non-delay model (expedite off) — the optimizer found the
    # best order globally, which supersedes expedite's local tie-break. No ranks ->
    # config unchanged (expedite behaves exactly as the Settings tick sets it).
    ranked_config = replace(config, expedite_window_min=0) if ranks else config

    # ONE pass over all active lines. Operator absences (physical unavailability)
    # reserve time; lanes never do. The feedback loop is quantity-only: recorded
    # times (downtime, actual setup) are stored for the record, never scheduled.
    plan_run = PlanRun(so_lines=so_lines)
    trace = run_forward(plan_run, ranked_config, masters, reserved=ab or None,
                        priority_rank=ranks)

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
                "In this plan": "scheduled" if remaining > 0 else "no, fully produced, mark complete"}

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
            "and 'In this plan = no': it isn't scheduled until you tick 'mark "
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

    result = {"run_id": run_id, "trace": trace,
              "report": _report_for_book(masters, so_lines, absences=absences_raw),
              "gantt": gantt, "orders": orders,
              # SAVED (unresolved) config — null plan_start_date = auto survives
              # the round-trip; the resolved start is a separate display key.
              "config": saved_config_dict,
              "resolved_plan_start": resolved_plan_start,
              "expected_end": exp_end, "optimize_meta": optimize_meta,
              "auto_note": book_store.load_auto_note()}
    return result


def _commit_orders(pairs):
    """Snapshot each order's current expected completion (from a fresh plan) as
    its promised date, and lock it into the committed lane. Unknown (so, item)
    pairs are silently skipped — set_commitment() returns False for them."""
    from datetime import date as _date
    result = _plan(_load_plan_config())
    exp = result.get("expected_end", {})
    now = _ist_now().isoformat(timespec="seconds")
    for so, item in pairs:
        iso = exp.get(f"{so}\x1f{item}")
        promised = _date.fromisoformat(iso) if iso else None
        book_store.set_commitment(so, item, "committed", promised, now)


def _load_plan_config() -> Config:
    """The admin's last-saved plan config, or defaults. Never raises: a missing,
    unparseable, or invalid stored value falls back to ``Config()`` so a read
    endpoint can't be 500'd by a bad stored config.

    Engine selection: the code default is ``classic`` (the kept engine the test suite
    validates). Production runs the new operator-stable engine by setting the env var
    ``DEFAULT_SCHEDULER=new`` — applied only when the admin hasn't explicitly saved a
    scheduler choice, so an explicit Settings choice always wins and tests (no env) stay
    on classic."""
    raw = book_store.load_plan_config()
    saved = {}
    if raw:
        try:
            saved = json.loads(raw)
        except Exception:
            saved = {}
    try:
        cfg = Config.from_dict(saved)
        cfg.validate()
    except Exception:
        cfg = Config()
    # DEFAULT_SCHEDULER is the DEPLOY-level engine selector and is AUTHORITATIVE: it
    # overrides any scheduler stored in the saved config, so switching the deployed engine
    # never requires clearing an old MongoDB config (which pins the retired flow/classic).
    # Normalised for stray whitespace/case. Unset (e.g. in tests) -> the saved/Config value.
    env_sched = (os.environ.get("DEFAULT_SCHEDULER") or "").strip().lower()
    if env_sched in ("new", "classic", "flow"):
        cfg.scheduler = env_sched
    return cfg


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
            # 40, not 20: flow-scheduler evals are ~5x slower than classic, so the
            # GitHub Actions runner needs ~25 min for a full flow contest. At 20 the
            # cloud (fast, 2 vCPU) got cut off ~79% done and fell back to Render's
            # 0.1-CPU local compute (live 2026-07-19). 40 lets GitHub always finish
            # first, so the slow local path is never hit. GitHub cost stays $0 (free
            # minutes, spending cap 0); a run is ~25 min, far under the 2000/month.
            "timeout_min": float(os.environ.get("OPTIMIZE_CLOUD_TIMEOUT_MIN", "40"))}


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
# Scheduled trigger (spec 2026-07-18-scheduled-optimize, supersedes the
# event-triggered 2026-07-15-self-tuning-plan Phase 1): the job order is
# re-optimized only twice a week (Mon/Fri 11:00 IST), via the GitHub cron
# hitting POST /optimize/scheduled. No event triggers — uploads, commits/
# urgent/uncommit, deletes, Settings saves, and absences no longer start a
# contest; new orders arrive Open and wait for the next scheduled run (or the
# owner's manual Optimize button). AUTO_OPTIMIZE=0 is internal test isolation
# only — never user-facing.
# --------------------------------------------------------------------------- #
def _auto_enabled() -> bool:
    return os.environ.get("AUTO_OPTIMIZE", "1") != "0"


def _ist_now():
    """Server runs UTC; the auto notes reference a named local (IST) clock."""
    from datetime import datetime as _dt, timedelta as _td
    return _dt.utcnow() + _td(hours=5, minutes=30)


def _ist_today():
    """Today's date on the named local (IST) clock — the LIVE plan start.
    Every date the app reasons from (plan clock, rotation, first-seen) follows
    this, not a frozen test-era date (go-live 2026-07-19)."""
    return _ist_now().date()


def _resolve_config(config: Config) -> Config:
    """Resolve an 'auto' plan start (plan_start_date is None) to today (IST) so
    the pure engine NEVER sees None. Called at every planning entry; a config
    that already carries an explicit date is returned unchanged. The SAVED config
    keeps None — this resolution is per-run only, never persisted."""
    if config.plan_start_date is None:
        return replace(config, plan_start_date=_ist_today())
    return config


def _current_book_sig() -> str:
    masters = _current_masters()
    actuals = book_store.load_actuals()
    lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                      actuals, masters)
    absences = book_store.load_absences()
    return optimize_service.book_signature(lines, absences=absences)


def _auto_note_write(text: str):
    book_store.save_auto_note({"text": text,
                               "at": _ist_now().isoformat(timespec="seconds")})


def _applied_plan_meta():
    """The meta dict recorded against the last applied optimization, read
    directly (not via book_store.load_plan_priority(), which treats an empty
    ranks dict as "nothing applied" for planning purposes — a different concern
    from "does the last contest still match today's book/inputs"). ``None`` when
    nothing has ever been applied."""
    raw = book_store.get_store().kv_get(book_store.PLAN_PRIORITY_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    return data.get("meta") or {}


def _applied_book_sig():
    return (_applied_plan_meta() or {}).get("book_sig")


def _try_start_auto() -> bool:
    """The one decision point: start an auto contest now if it makes sense.
    Invoked only by POST /optimize/scheduled (the twice-weekly GitHub cron)."""
    if not _auto_enabled():
        return False
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running":
            return False                         # one contest at a time
    if _cloud_config() is None:
        _auto_note_write("Auto-optimize skipped: cloud compute unavailable; "
                         "will retry on the next scheduled run.")
        return False                             # auto is cloud-only
    # Skip only when NOTHING material changed since the last applied plan — and
    # "material" now includes the inputs fingerprint (masters + settings +
    # operator rotation/edits), not just the book. After a Friday rotation the
    # book_sig is unchanged but the operators differ, so a book-only check would
    # wrongly skip the re-optimize (spec 2026-07-18 DECISION). A legacy applied
    # meta without an inputs_sig doesn't force a run on the missing field alone.
    meta = _applied_plan_meta() or {}
    try:
        book_same = (meta.get("book_sig") == _current_book_sig())
        applied_inputs = meta.get("inputs_sig")
        inputs_same = (applied_inputs is None
                       or applied_inputs == _inputs_signature(_load_plan_config()))
        if book_same and inputs_same:
            return False                         # nothing material changed
    except Exception:
        return False
    try:
        _start_optimize(_OPT_BUDGETS["deep"], "auto", background=True, auto=True)
        return True
    except HTTPException:
        return False                             # e.g. nothing to optimize


def _start_optimize(budget_evals: int, label: str, background: bool = True,
                    auto: bool = False):
    """Snapshot the book + config and run the settings-sweep contest. ONE pool
    over every active line — lanes (Open/Committed/Urgent) are status labels
    with no scheduling effect. Operator absences are reserved as blocked
    machine/operator intervals, same as in a normal Plan. Cloud-configured →
    dispatch the full contest to GitHub Actions; otherwise (or on any cloud
    failure) compute locally."""
    with _OPTIMIZE_LOCK:
        if _OPTIMIZE["state"] == "running":
            raise HTTPException(status_code=409,
                                detail="an optimization is already running")

        config = _load_plan_config()
        masters = _current_masters()
        base_config = config      # as saved (auto -> None) — the inputs fingerprint basis
        # Flow evals are ~15s each on the free instance — a full local search would
        # run for hours. Cap the LOCAL-path budget hard for flow (the cloud path
        # sizes itself via cloud_candidates); classic keeps its full budget.
        if getattr(base_config, "scheduler", "classic") == "flow":
            budget_evals = min(budget_evals, optimizer.FLOW_LOCAL_BUDGET)
        # The new engine runs its own search in-process (its cloud worker is not wired yet);
        # cap the budget so a click finishes on the free tier (the Stop button also applies).
        new_engine_run = getattr(base_config, "scheduler", "classic") == "new"
        if new_engine_run:
            budget_evals = min(budget_evals, 200)
        config = _resolve_config(config)   # None -> today (IST) for the engine/contest
        actuals = book_store.load_actuals()
        orders = book_store.load_active_orders()
        absences = book_store.load_absences()
        operator_table = book_store.load_operator_table()
        try:
            setup = optimize_service.prepare_contest(orders, actuals, masters, config,
                                                     absences=absences,
                                                     operator_table=operator_table)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

        # The new engine now runs its contest on GitHub Actions too (the worker seeds its
        # masters from the payload workbook). If the cloud run fails/times out, the watchdog
        # falls back to the in-process sweep (budget-capped above) — so the button always works.
        cloud = _cloud_config()
        job_id = uuid.uuid4().hex
        if cloud:
            # The contest lineup must match the scheduler MODE: chunk counts under
            # flow, overlap % under classic. Passing the wrong list would make the
            # worker replace flow_chunks with 60..95 (invalid — validate caps at 50)
            # and the whole cloud contest would error out, never running in flow mode.
            _cands = optimize_service.cloud_candidates(setup.search_config)
            payload = optimize_service.build_payload(
                orders, actuals, book_store.load_masters_bytes(), config,
                seed=_OPT_SEED, candidates=_cands, absences=absences,
                operator_table=operator_table)
            _knob, _ = optimizer.knob_for(setup.search_config)
            if new_engine_run:
                # golden-section runs ~13 probes at NEW_CLOUD_BUDGET_PER_EVAL plans each.
                denom = 13 * optimize_service.NEW_CLOUD_BUDGET_PER_EVAL
            else:
                denom = (optimize_service.CLOUD_BUDGET_PER_CANDIDATE
                         * len(optimizer.sweep_contenders(
                             getattr(setup.search_config, _knob), _cands)))
        else:
            payload = None
            _knob, _kcands = optimizer.knob_for(setup.search_config)
            denom = optimizer.sweep_total_evals(
                budget_evals, getattr(setup.search_config, _knob), _kcands)

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
                base_sched, base_lines = _all_lines_schedule(setup, setup.masters, None)
                real_baseline = optimizer.plan_metrics(base_sched, base_lines,
                                                       setup.config.plan_start_date)

            # One pool: search ALL active lines together (lanes are status labels
            # with no scheduling effect). Only operator absences reserve time.
            sw = optimizer.sweep_optimize(setup.target, setup.search_config,
                                          setup.masters, budget_evals=budget_evals,
                                          seed=_OPT_SEED, on_progress=on_progress,
                                          should_cancel=lambda: _OPTIMIZE.get("cancel"),
                                          base_reserved=setup.absence_reserved)
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
            base_sched, base_lines = _all_lines_schedule(setup, setup.masters, None)
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
                        _k, _kc = optimizer.knob_for(setup.search_config)
                        _OPTIMIZE["budget_evals"] = optimizer.sweep_total_evals(
                            budget_evals, getattr(setup.search_config, _k), _kc)
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
                                             error="stopped: the cloud run did not "
                                                   "answer before the timeout")
                            return
                        _OPTIMIZE["mode"] = "local"
                        _k, _kc = optimizer.knob_for(setup.search_config)
                        _OPTIMIZE["budget_evals"] = optimizer.sweep_total_evals(
                            budget_evals, getattr(setup.search_config, _k), _kc)
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
    _knob, _ = optimizer.knob_for(base_config)
    inputs_sig = _inputs_signature(replace(base_config,
                                           **{_knob: winner_overlap}))
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
                    "current_overlap": getattr(base_config, _knob),
                    "knob": _knob,
                    "sweep_table": table})
    if _OPTIMIZE.get("auto"):
        try:
            _auto_apply_result()
        except Exception:   # noqa: BLE001 — an auto note must never crash a result
            pass
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
                # which config field the value tunes: overlap_percent (classic)
                # or flow_chunks (flow) — the UI labels the result with this
                "knob": res.get("knob"),
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


def _all_lines_schedule(setup, masters, ranks):
    """A schedule covering ALL active lines, built the SAME way `_plan` builds it
    (one pass, saved ranks replayed with expedite off, operator absences reserved)
    so both sides of the Optimize before/after are scored on the same domain.
    Returns (schedule, all_lines)."""
    all_lines = list(setup.target)
    ranked_config = replace(setup.config, expedite_window_min=0) if ranks else setup.config
    pr = PlanRun(so_lines=list(all_lines))
    run_forward(pr, ranked_config, masters, reserved=setup.absence_reserved,
                priority_rank=ranks)
    return pr.schedule, all_lines


def _incumbent_metrics():
    """Score of the plan users currently see: the applied ranks (if any) replayed
    on TODAY'S book, measured over ALL active lines — the same domain the contest's
    ``best`` is scored on. Expedite off when ranks replay."""
    config = _resolve_config(_load_plan_config())   # None -> today (IST) for the engine
    masters = _current_masters()
    actuals = book_store.load_actuals()
    orders = book_store.load_active_orders()
    absences = book_store.load_absences()
    setup = optimize_service.prepare_contest(orders, actuals, masters, config,
                                             absences=absences,
                                             operator_table=book_store.load_operator_table())
    prio = book_store.load_plan_priority()
    ranks = (prio or {}).get("ranks") or None
    schedule, all_lines = _all_lines_schedule(setup, setup.masters, ranks)
    return optimizer.plan_metrics(schedule, all_lines, setup.config.plan_start_date)


def _auto_apply_result():
    """Called after an auto contest lands in state=done: apply iff strictly
    better than the incumbent; write the note either way."""
    with _OPTIMIZE_LOCK:
        res = _OPTIMIZE.get("result") or {}
        best = res.get("best")
    if not best:
        _auto_note_write("Auto-optimize found no plan. Kept the current plan.")
        return
    try:
        # `best` was scored on the book as it stood when this contest started
        # (its snapshot); `_incumbent_metrics()` below scores the incumbent
        # against TODAY's book. If production punched an actual mid-run, that's
        # a transiently mismatched comparison — the next scheduled run
        # re-compares against the fresh book and self-heals.
        inc = _incumbent_metrics()
    except Exception as e:  # noqa: BLE001
        _auto_note_write(f"Auto-optimize finished but could not compare: {e}")
        return
    stamp = _ist_now().strftime("%H:%M")
    if optimizer.score(best) < optimizer.score(inc):
        meta = _optimize_apply()          # persists ranks + overlap + inputs_sig + book_sig
        ov = res.get("best_overlap"); cur = res.get("current_overlap")
        word = "chunks" if res.get("knob") == "flow_chunks" else "overlap"
        ov_txt = f", {word} {cur} → {ov}" if ov != cur else ""
        _auto_note_write(f"Plan auto-re-optimized {stamp}: "
                         f"{best['total_late_days']} late-days "
                         f"(was {inc['total_late_days']}){ov_txt}.")
    else:
        _auto_note_write(f"Checked {stamp}: current plan still best "
                         f"({inc['total_late_days']} late-days).")


def _optimize_apply():
    """Persist the last completed run's ranks — from then on every Plan replays
    them (admin, user, and the auto re-plan after actuals)."""
    with _OPTIMIZE_LOCK:
        res = _OPTIMIZE.get("result")
        if _OPTIMIZE["state"] != "done" or not res:
            raise HTTPException(status_code=409,
                                detail="no completed optimization to apply")
        meta = {"saved_at": _ist_now().isoformat(timespec="seconds"),
                "budget": res["budget"], "seed": res["seed"],
                "baseline": res["baseline"], "best": res["best"],
                "covered": len(res["ranks"]),
                "inputs_sig": res.get("inputs_sig"),
                "best_overlap": res.get("best_overlap"),
                "book_sig": _current_book_sig()}
        book_store.save_plan_priority(res["ranks"], meta)
        # Settings sweep: the winning overlap becomes THE saved plan setting (the
        # single config every Plan loads and the Settings panel shows). Unchanged
        # winner -> no write, no churn.
        best_ov = res.get("best_overlap")
        if best_ov is not None:
            cfg = _load_plan_config()
            knob = res.get("knob") or optimizer.knob_for(cfg)[0]
            if getattr(cfg, knob) != best_ov:
                book_store.save_plan_config(json.dumps(
                    replace(cfg, **{knob: best_ov}).to_dict()))
        # Clear the in-memory job so a later page refresh doesn't re-show the
        # "Apply" panel for a plan that's already applied.
        _OPTIMIZE.update(state="idle", result=None, best=None, baseline=None)
        return meta


def _optimize_clear():
    book_store.clear_plan_priority()
    return {"cleared": True}


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
        so_lines, active, completed, first_seen=_ist_today().isoformat())
    book_store.add_orders(new_orders)

    result = {
        "name": file.filename,
        "added": len(new_orders),
        "flagged": flags,
        "masters_updated": masters_updated,
        "summary": {"items": len(masters.routings), "machines": len(masters.machines)},
        "report": _report_after_upload(masters),
    }
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
    return {"deleted": n}


@app.post("/orders/clear")
def clear_orders(req: ClearRequest, request: Request):
    """Permanently delete ALL orders + actuals (masters are kept). Admin only, and
    guarded by re-entering the admin password."""
    require_admin(request)
    require_password(request, req.password)
    book_store.delete_all()
    return {"cleared": True}


@app.post("/orders/commit")
def commit_orders_ep(req: CommitRequest, request: Request):
    """Lock the given orders into the committed lane: each order's current
    expected completion (from a fresh plan) becomes its locked promise. Admin
    only."""
    require_admin(request)
    _commit_orders([(o[0], o[1]) for o in req.orders if len(o) == 2])
    return {"committed": len(req.orders)}


@app.post("/orders/uncommit")
def uncommit_orders_ep(req: CommitRequest, request: Request):
    """Release the given orders back to the open lane (clears their promise).
    Admin only."""
    require_admin(request)
    for o in req.orders:
        if len(o) == 2:
            book_store.clear_commitment(o[0], o[1])
    return {"uncommitted": len(req.orders)}


@app.post("/orders/urgent")
def urgent_order_ep(req: UrgentRequest, request: Request):
    """Mark one order urgent: set its commitment to urgent and snapshot its SO
    delivery date as the informational promise. Lanes are status labels only —
    urgent has no scheduling effect. Admin only."""
    require_admin(request)
    order = book_store.load_active_orders().get((req.so, req.item))
    book_store.set_commitment(req.so, req.item, "urgent",
                              order.delivery_date if order else None,
                              _ist_now().isoformat(timespec="seconds"))
    return {"urgent": True}


@app.get("/absences")
def get_absences():
    """Operator absences on file, any logged-in role (read-only view feeds the
    Task 16 UI dropdown/list). `orphans` flags absences whose operator name is
    no longer in the current masters — informational, also surfaced in the
    validation report as ABSENT_OPERATOR_UNKNOWN."""
    masters = _current_masters()
    absences = book_store.load_absences()
    return {"absences": absences,
            "orphans": _absence_orphans(masters, absences=absences),
            "operators": sorted({o.name for o in masters.operators})}


@app.post("/absences")
def create_absence(req: AbsenceRequest, request: Request):
    """Record an operator absence window. Admin only. Dates must parse
    (YYYY-MM-DD); a reversed range is accepted and normalized (swapped) rather
    than rejected. The operator must be a name in the current masters."""
    require_admin(request)
    try:
        d_from = date.fromisoformat(req.from_date)
        d_to = date.fromisoformat(req.to_date)
    except ValueError:
        raise HTTPException(status_code=400, detail="dates must be YYYY-MM-DD")
    if d_to < d_from:
        d_from, d_to = d_to, d_from
    names = {o.name for o in _current_masters().operators}
    if req.operator not in names:
        raise HTTPException(status_code=400,
                            detail=f"unknown operator '{req.operator}'")
    saved = book_store.save_absence({"operator": req.operator,
                                     "from_date": d_from.isoformat(),
                                     "to_date": d_to.isoformat()})
    return {"absence": saved}


@app.delete("/absences/{absence_id}")
def delete_absence_ep(absence_id: str, request: Request):
    """Remove an absence entry. Admin only."""
    require_admin(request)
    if not book_store.delete_absence(absence_id):
        raise HTTPException(status_code=404, detail="absence not found")
    return {"deleted": True}


_VALID_SHIFTS = {"First shift", "Second shift", ""}


@app.get("/operators")
def get_operators():
    """The app-owned operator/shift table, any logged-in role. Calling
    `_current_masters()` first ensures the one-time seed-from-workbook (if a
    workbook exists and the table has never been seeded) and any due Friday
    rotation are applied and persisted (as-of TODAY, for display) before the
    rows are read back. `next_rotation` is the next Friday after today,
    regardless of whether the table exists yet."""
    _current_masters()
    table = book_store.load_operator_table()
    rows = table.get("operators", []) if table else []
    return {"operators": rows,
            "next_rotation": operator_master.next_rotation(_ist_today()).isoformat()}


@app.post("/operators")
def create_operator(req: OperatorRequest, request: Request):
    """Add a new operator to the app-owned table. Admin only. `name` must be
    non-empty and unique among existing rows (case-insensitive, stripped);
    `shift` must be one of the 3 exact values. `_current_masters()` runs FIRST
    so the seed-once-from-workbook path can never be suppressed by a direct
    POST on a fresh deploy (a bare table here would permanently skip migrating
    the workbook's operators). After that, a still-None table means there is
    genuinely nothing to seed from (fresh install, no workbook) — POST then
    creates it (`week_anchor` = the most recent Friday). No optimize trigger —
    scheduled-only contest rules stand."""
    require_admin(request)
    name = req.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    if req.shift not in _VALID_SHIFTS:
        raise HTTPException(status_code=400,
                            detail="shift must be 'First shift', 'Second shift', or ''")
    _current_masters()   # seed-once + lazy rotation before we read the table
    table = book_store.load_operator_table()
    if table is None:
        table = {"week_anchor": operator_master.last_friday(_ist_today()).isoformat(),
                 "operators": []}
    existing = {r.get("name", "").strip().lower() for r in table["operators"]}
    if name.lower() in existing:
        raise HTTPException(status_code=400,
                            detail=f"operator '{name}' already exists")
    row = {"id": uuid.uuid4().hex, "name": name, "machines_raw": req.machines_raw,
           "shift": req.shift, "pinned": False}
    table["operators"].append(row)
    book_store.save_operator_table(table)
    return {"operator": row}


@app.patch("/operators/{operator_id}")
def update_operator(operator_id: str, req: OperatorPatchRequest, request: Request):
    """Partially update an operator's `machines_raw`/`shift`/`pinned`. Admin
    only. 404 if the id is unknown (including when no table exists yet);
    `shift`, if given, is validated the same as POST. `_current_masters()`
    runs first (same seed-once guarantee as POST/GET, call-order independent)."""
    require_admin(request)
    if req.shift is not None and req.shift not in _VALID_SHIFTS:
        raise HTTPException(status_code=400,
                            detail="shift must be 'First shift', 'Second shift', or ''")
    _current_masters()   # seed-once + lazy rotation before we read the table
    table = book_store.load_operator_table()
    rows = table.get("operators", []) if table else []
    for row in rows:
        if row.get("id") == operator_id:
            if req.machines_raw is not None:
                row["machines_raw"] = req.machines_raw
            if req.shift is not None:
                row["shift"] = req.shift
            if req.pinned is not None:
                row["pinned"] = req.pinned
            book_store.save_operator_table(table)
            return {"operator": row}
    raise HTTPException(status_code=404, detail="operator not found")


@app.delete("/operators/{operator_id}")
def delete_operator(operator_id: str, request: Request):
    """Remove an operator. Admin only. 404 if the id is unknown (including
    when no table exists yet). Absences referencing the removed name become
    orphans — non-blocking, covered by the existing ABSENT_OPERATOR_UNKNOWN
    report row (same as a masters re-upload that drops an operator).
    `_current_masters()` runs first (same seed-once guarantee as POST/GET,
    call-order independent)."""
    require_admin(request)
    _current_masters()   # seed-once + lazy rotation before we read the table
    table = book_store.load_operator_table()
    rows = table.get("operators", []) if table else []
    keep = [r for r in rows if r.get("id") != operator_id]
    if len(keep) == len(rows):
        raise HTTPException(status_code=404, detail="operator not found")
    table["operators"] = keep
    book_store.save_operator_table(table)
    return {"deleted": True}


def _validate_year_month(year: int, month: int) -> None:
    """Shared 400 guard for both efficiency endpoints. 2000-2100 is a generous
    sanity window (not a business rule) — just enough to reject fat-fingered
    input without hard-coding "current year"."""
    if not (1 <= month <= 12):
        raise HTTPException(status_code=400, detail="month must be 1-12")
    if not (2000 <= year <= 2100):
        raise HTTPException(status_code=400, detail="year must be 2000-2100")


def _efficiency_rows(year: int, month: int) -> list:
    masters = _current_masters()
    actuals = book_store.load_actuals()
    absences = book_store.load_absences()
    config = _load_plan_config()
    return efficiency.monthly_report(actuals, absences, masters, config, year, month)


@app.get("/efficiency")
def efficiency_report(year: int, month: int, request: Request):
    """Monthly operator efficiency report (admin only). Pure reporting — no
    schedule/plan impact. See engine/efficiency.py for the formula."""
    require_admin(request)
    _validate_year_month(year, month)
    return {"year": year, "month": month, "rows": _efficiency_rows(year, month)}


@app.get("/efficiency.csv")
def efficiency_report_csv(year: int, month: int, request: Request):
    """Same report as a CSV download (admin only), built server-side (unlike the
    Rule-6 schedule CSVs, which are generated client-side in app.js from the
    on-screen table — this one has no on-screen table to scrape until Preview
    is clicked, and admin-only auth is easier to enforce server-side)."""
    require_admin(request)
    _validate_year_month(year, month)
    rows = _efficiency_rows(year, month)
    columns = list(rows[0].keys()) if rows else list(efficiency.REPORT_COLUMNS)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["-" if row[c] is None else row[c] for c in columns])
    filename = f"operator-efficiency-{year:04d}-{month:02d}.csv"
    return Response(
        content="﻿" + buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


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


@app.post("/optimize/scheduled")
def optimize_scheduled_ep(request: Request):
    """The twice-weekly trigger (GitHub cron; worker-secret auth). All of
    _try_start_auto's guards apply — cloud-only, one-at-a-time, and the
    book-fingerprint skip when nothing changed since the last applied plan."""
    _require_worker(request)
    started = _try_start_auto()
    return {"started": started, "state": _optimize_status()["state"]}


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
    operator = req.operator.strip()
    if not operator:
        raise HTTPException(status_code=400, detail="operator is required")
    known_operators = {o.name for o in _current_masters().operators}
    if operator not in known_operators:
        raise HTTPException(status_code=400, detail=f"unknown operator '{operator}'")
    actual = Actual(
        so_no=req.so_no, item_code=req.item_code,
        entry_date=entry_date,
        qty_produced=req.qty_produced, qty_rejected=req.qty_rejected,
        shift=req.shift, item_name=req.item_name, process=req.process,
        operator=operator,
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
