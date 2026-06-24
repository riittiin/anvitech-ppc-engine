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

import base64
import binascii
import io
import os
import secrets
import uuid
from datetime import date
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.config import Config, OVERLAP_SEQUENTIAL, OVERLAP_PERCENT
from engine.loaders import load_all
from engine.models import PlanRun, Actual
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

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Anvitech PPC Engine")

# --------------------------------------------------------------------------- #
# Login — the whole app (UI + API + static) sits behind one id + password.
# --------------------------------------------------------------------------- #
APP_USERNAME = os.environ.get("APP_USERNAME", "anvitech")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "ppc2025")
_AUTH_REALM = 'Basic realm="Anvitech PPC Engine"'


def _credentials_ok(auth_header: Optional[str]) -> bool:
    if not auth_header or not auth_header.startswith("Basic "):
        return False
    try:
        user, _, pwd = base64.b64decode(auth_header[6:]).decode("utf-8").partition(":")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return False
    return (secrets.compare_digest(user, APP_USERNAME)
            and secrets.compare_digest(pwd, APP_PASSWORD))


@app.middleware("http")
async def basic_auth(request, call_next):
    if not _credentials_ok(request.headers.get("authorization")):
        return Response(status_code=401, headers={"WWW-Authenticate": _AUTH_REALM})
    return await call_next(request)


@app.middleware("http")
async def no_cache(request, call_next):
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


# --------------------------------------------------------------------------- #
# Masters: from the latest uploaded workbook, else the bundled test file.
# Cached in-process, keyed by the workbook's content hash.
# --------------------------------------------------------------------------- #
_RUNS: dict = {}
_MASTERS_CACHE: dict = {"key": None, "masters": None}


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
        _, masters = load_all()  # bundled Test2.xlsx (so the app works pre-upload)
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


class DeleteRequest(BaseModel):
    so_nos: List[str] = []


class ActualRequest(BaseModel):
    so_no: str
    item_code: str
    entry_date: str
    shift: str = ""
    item_name: str = ""
    process: str = ""
    qty_produced: float = 0.0
    qty_rejected: float = 0.0
    actual_setup_min: float = 0.0
    no_power_min: float = 0.0
    no_operator_min: float = 0.0
    tool_problem_min: float = 0.0
    machine_breakdown_min: float = 0.0
    no_load_min: float = 0.0
    other_work_min: float = 0.0
    remarks: str = ""
    mark_complete: bool = False


# --------------------------------------------------------------------------- #
# Helper-tab augmentation (Rules 3/4/5/7 + Rule 6 machine view)
# --------------------------------------------------------------------------- #
def _machine_display(masters, mid):
    m = masters.machines.get(mid)
    return m.display_name if m else mid


def _augment_helpers(trace, plan_run, config, masters, lost=None, unattributed=None, actuals=None):
    if "rule3" in trace and trace["rule3"].get("reached", True) and plan_run.batches_prioritized:
        breakdown = r3.build_priority_breakdown(plan_run.batches_prioritized, config, masters)
        trace["rule3"]["tables"] = [
            {"title": "Priority breakdown — slack/critical-ratio per batch (lower slack = more urgent)",
             "table": to_table(breakdown)},
        ]

    if "rule6" in trace and trace["rule6"].get("reached", True):
        timeline, summary = r6.build_machine_view(plan_run.schedule, masters, config)
        trace["rule6"]["tables"] = [
            {"title": "Machine timeline — per-machine queue (Idle before = working minutes the machine waited)",
             "table": to_table(timeline)},
            {"title": "Machine utilization", "table": to_table(summary)},
        ]
        # Downtime loop-back: show the lost time fed into each machine's availability.
        if config.apply_downtime_to_plan:
            lost_rows = [{"Machine": _machine_display(masters, mid),
                          "Lost time fed to plan (min)": round(mins, 1)}
                         for mid, mins in sorted((lost or {}).items())]
            trace["rule6"]["tables"].append({
                "title": "Downtime fed back into the plan — recorded downtime + setup "
                         "overrun delays each machine's availability (per machine)",
                "table": to_table(lost_rows or [{"Machine": "—", "Lost time fed to plan (min)": 0}]),
            })
            if unattributed:
                trace["rule6"]["tables"].append({
                    "title": "Unattributed downtime — could not match the Process to a "
                             "machine (check the process name); NOT fed into the plan",
                    "table": to_table([
                        {"SO No": u["so_no"], "Item Code": u["item_code"],
                         "Process": u["process"], "Lost (min)": round(u["lost"], 1)}
                        for u in unattributed]),
                })

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
    trace["rule7"] = {
        "input": to_table([{"Source": "Daily Production Entry form → durable store"}]),
        "output": to_table(actuals), "config": None,
        "notes": [
            f"{len(actuals)} actual(s) on record; total downtime {total_down:g} min.",
            "Good qty (produced − rejected) drives each order's remaining qty; "
            "marking complete on an entry archives that order.",
        ],
        "tables": [{"title": "Per item code — output & downtime rollup (minutes summed across entries)",
                    "table": to_table(r7.aggregate_by_item(actuals))}],
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

    so_lines = orderbook.active_so_lines(active, actuals)        # remaining qty per order

    # Downtime loop-back (opt-in): recorded downtime + setup overrun → per-machine
    # availability delays fed into Rule 6.
    if config.apply_downtime_to_plan:
        lost, unattributed = orderbook.machine_lost_minutes(actuals, masters, config.setup_time_min)
    else:
        lost, unattributed = {}, []

    plan_run = PlanRun(so_lines=so_lines)
    trace = run_forward(plan_run, config, masters, machine_lost_min=lost)
    _augment_helpers(trace, plan_run, config, masters, lost=lost,
                     unattributed=unattributed, actuals=actuals)

    # Rule 8 tab: the active order book by remaining qty. List EVERY active order
    # so the count matches the Orders tab; flag the ones with nothing left to make
    # (fully produced but not yet marked complete) — they aren't scheduled, which
    # is why they don't appear in the schedule/Gantt.
    good = orderbook.produced_good_by_so(actuals)
    status_by_so = {sn: orderbook.derive_status(o, good) for sn, o in active.items()}

    def _r8_row(o):
        remaining = max(o.ordered_qty - good.get(o.so_no, 0.0), 0.0)
        return {"SO No": o.so_no, "Item Code": o.item_code, "Remaining Qty": remaining,
                "SO Delivery Date": o.delivery_date.isoformat(),
                "Status": status_by_so[o.so_no],
                "In this plan": "scheduled" if remaining > 0 else "no — fully produced, mark complete"}

    r8_rows = sorted((_r8_row(o) for o in active.values()),
                     key=lambda r: (r["SO Delivery Date"], r["SO No"]))
    scheduled = sum(1 for r in r8_rows if r["Remaining Qty"] > 0)
    trace["rule8"] = {
        "input": to_table([{"Active orders": len(active),
                            "Scheduled (work remaining)": scheduled,
                            "Actuals applied": len(actuals)}]),
        "output": to_table(r8_rows),
        "config": config.to_dict(),
        "notes": [
            "Unified Plan: every active order is listed at its remaining qty "
            "(ordered − good produced). Completed orders are excluded.",
            "An order fully produced but not yet marked complete shows Remaining 0 "
            "and 'In this plan = no' — it isn't scheduled until you tick 'mark "
            "complete' on a Rule 7 entry to archive it.",
        ],
        "error": None, "reached": True,
    }

    run_id = uuid.uuid4().hex[:8]
    _RUNS[run_id] = trace
    gantt = build_gantt(plan_run.schedule, plan_run.batches_prioritized, masters,
                        status_by_so=status_by_so)
    orders = to_table(orderbook.order_rows(active, completed, actuals))
    return {"run_id": run_id, "trace": trace, "report": _report_table(masters),
            "gantt": gantt, "orders": orders}


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """Merge an uploaded workbook into the order book. New SO numbers become
    pending orders; known ones are flagged. Masters are updated (latest-wins,
    kept if the file omits them)."""
    contents = await file.read()
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
def run(req: Optional[RunRequest] = None):
    config = Config.from_dict(req.config if req else None)
    try:
        config.validate()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return _plan(config)


@app.post("/rerun")
def rerun(req: Optional[RunRequest] = None):
    return run(req)  # unified — Run and Rerun are the same action now


@app.get("/orders")
def orders():
    active = book_store.load_active_orders()
    completed = book_store.load_completed_orders()
    actuals = book_store.load_actuals()
    return {"orders": to_table(orderbook.order_rows(active, completed, actuals))}


@app.post("/orders/delete")
def delete_orders(req: DeleteRequest):
    """Permanently delete the given SO numbers (orders + their actuals)."""
    n = book_store.delete_orders(req.so_nos)
    return {"deleted": n}


@app.post("/orders/clear")
def clear_orders():
    """Permanently delete ALL orders + actuals (masters are kept)."""
    book_store.delete_all()
    return {"cleared": True}


@app.get("/gantt")
def gantt():
    return _plan(Config())["gantt"]


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
    items_map = {
        code: {"item_name": routing.description,
               "processes": [p.name for p in routing.processes]}
        for code, routing in masters.routings.items()
    }
    return {
        "items": items_map,
        "shifts": ["1st shift", "2nd shift"],
        "so_to_item": {o.so_no: o.item_code for o in active.values()},
        "open_so_nos": sorted(active.keys()),
    }


@app.post("/actuals")
def post_actuals(req: ActualRequest):
    actual = Actual(
        so_no=req.so_no, item_code=req.item_code,
        entry_date=date.fromisoformat(req.entry_date),
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
        completed = book_store.complete_order(req.so_no)
    return {
        "saved": len(all_actuals),
        "completed_order": completed,
        "actuals": to_table(all_actuals),
        "by_item": to_table(r7.aggregate_by_item(all_actuals)),
    }


# Static frontend (mounted last so the API routes above take precedence).
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
