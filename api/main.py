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
import io
import json
import os
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
from engine.pipeline import run_forward, to_table
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
    _MASTERS_CACHE.update(key=key, masters=masters)
    return masters


def _report_table(masters):
    return to_table([
        {"Kind": r["kind"], "Reference": r["ref"], "Message": r["message"]}
        for r in masters.report
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
def _plan(config: Config):
    masters = _current_masters()
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

    # The feedback loop is quantity-only: recorded times (downtime, actual setup) are
    # stored for the record and never affect the schedule.
    plan_run = PlanRun(so_lines=so_lines)
    trace = run_forward(plan_run, config, masters)
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
    return {"run_id": run_id, "trace": trace, "report": _report_table(masters),
            "gantt": gantt, "orders": orders, "config": config.to_dict()}


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

    return {
        "name": file.filename,
        "added": len(new_orders),
        "flagged": flags,
        "masters_updated": masters_updated,
        "summary": {"items": len(masters.routings), "machines": len(masters.machines)},
        "report": _report_table(masters),
    }


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
    return _report_table(_current_masters())


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
