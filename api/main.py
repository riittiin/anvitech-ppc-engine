"""FastAPI layer — a thin wrapper exposing the engine + trace as JSON.

Endpoints (Design spec §10):
  POST /run           run Rules 1-7, return the full trace
  GET  /trace/{id}    fetch a past run's trace
  POST /actuals       save a daily actual (Rule 8)
  POST /rerun         Rule 9: re-plan from actuals + balance
  GET  /report        loader validation report (PENDING_MASTER_DATA, ...)
The web/ frontend (per-rule tabs) is served at /.
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
from typing import Optional

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from engine.config import Config, OVERLAP_SEQUENTIAL, OVERLAP_PERCENT
from engine.loaders import load_all
from engine.models import PlanRun, Actual
from engine.pipeline import run_forward, to_table
from engine.gantt import build_gantt
from engine.storage import get_kv
from engine.rules import (
    rule3_tiebreak_process_time as r3,
    rule4_setup_time as r4,
    rule5_overlap_mode as r5,
    rule6_allocate as r6,
    rule7_parallel_machine as r7,
    rule8_capture_actuals as r8,
    rule9_rerun_mrp as r9,
)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

app = FastAPI(title="Anvitech PPC Engine")

# --------------------------------------------------------------------------- #
# Login — the whole app (UI + API + static) sits behind one id + password.
# Set APP_USERNAME / APP_PASSWORD env vars in production (Vercel project settings).
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
    # constant-time compares avoid leaking length/contents via timing.
    return (secrets.compare_digest(user, APP_USERNAME)
            and secrets.compare_digest(pwd, APP_PASSWORD))


@app.middleware("http")
async def basic_auth(request, call_next):
    """Gate every request. Browsers prompt once, then cache for the session."""
    if not _credentials_ok(request.headers.get("authorization")):
        return Response(status_code=401, headers={"WWW-Authenticate": _AUTH_REALM})
    return await call_next(request)


@app.middleware("http")
async def no_cache(request, call_next):
    """Always serve fresh assets — avoids the browser running a stale app.js/css."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# --------------------------------------------------------------------------- #
# Cached source data + in-memory run store
# --------------------------------------------------------------------------- #
_STATE: dict = {"so_lines": None, "masters": None}     # default static Test2.xlsx
_RUNS: dict = {}        # run_id -> trace
_DATASETS: dict = {}    # dataset_id -> {"so_lines", "masters", "name"} (uploaded workbooks)


def _dataset_key(dataset_id: str) -> str:
    return f"anvitech:dataset:{dataset_id}"


def _data(dataset_id: Optional[str] = None):
    """Return (so_lines, masters) for the given uploaded dataset, or the bundled
    test workbook (Test2.xlsx) when no/unknown dataset is given.

    Uploaded datasets are cached in memory; when a durable store (Upstash) is
    configured they're also persisted there, so a different/cold instance can
    re-load them."""
    if dataset_id and dataset_id in _DATASETS:
        d = _DATASETS[dataset_id]
        return d["so_lines"], d["masters"]
    if dataset_id:
        kv = get_kv()
        if kv is not None:
            raw = kv.get(_dataset_key(dataset_id))
            if raw:
                so_lines, masters = load_all(io.BytesIO(base64.b64decode(raw)))
                _DATASETS[dataset_id] = {"so_lines": so_lines, "masters": masters, "name": dataset_id}
                return so_lines, masters
    if _STATE["so_lines"] is None:
        so_lines, masters = load_all()
        _STATE["so_lines"], _STATE["masters"] = so_lines, masters
    return _STATE["so_lines"], _STATE["masters"]


def _report_table(masters):
    return to_table([
        {"Kind": r["kind"], "Reference": r["ref"], "Message": r["message"]}
        for r in masters.report
    ])


def _augment_helpers(trace, plan_run, config, masters):
    """Populate the helper tabs (Rules 4/5/7) and Rule 8 with representative
    content, since they are consumed inside Rule 6 rather than run standalone.
    Also attaches the machine-centric tables to the Rule 6 trace entry."""
    # Rule 3 — smart-priority breakdown (work needed / time available / slack /
    # critical ratio) so the user sees WHY each batch ranks where.
    if "rule3" in trace and trace["rule3"].get("reached", True) and plan_run.batches_prioritized:
        breakdown = r3.build_priority_breakdown(plan_run.batches_prioritized, config, masters)
        trace["rule3"]["tables"] = [
            {"title": "Priority breakdown — slack/critical-ratio per batch (lower slack = more urgent)",
             "table": to_table(breakdown)},
        ]

    # Rule 6 — machine-wise view (per-machine queue + utilization). Attached as
    # generic extra "tables" the frontend renders under the input/output.
    if "rule6" in trace and trace["rule6"].get("reached", True):
        timeline, summary = r6.build_machine_view(plan_run.schedule, masters, config)
        trace["rule6"]["tables"] = [
            {
                "title": "Machine timeline — per-machine queue (Idle before = working minutes the machine waited)",
                "table": to_table(timeline),
            },
            {"title": "Machine utilization", "table": to_table(summary)},
        ]

    # Rule 4 — occupancy worked from the first scheduled process.
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

    # Rule 5 — overlap offset for both modes, on a 200-min example.
    seq = r5.elapsed_before_next(200, Config(overlap_mode=OVERLAP_SEQUENTIAL))
    ov = r5.elapsed_before_next(200, Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=config.overlap_percent))
    trace["rule5"] = {
        "input": to_table([{"Previous occupancy (min)": 200}]),
        "output": to_table([
            {"Mode": "sequential", "Next starts after (min)": seq},
            {"Mode": f"overlap {config.overlap_percent}%", "Next starts after (min)": ov},
        ]),
        "config": config.to_dict(),
        "notes": [f"active mode this run: {config.overlap_mode}"],
        "error": None, "reached": True,
    }

    # Rule 7 — parallel trigger evaluated per batch.
    rows7 = []
    for b in plan_run.batches_prioritized:
        rows7.append({
            "Batch": b.batch_id, "Item": b.item_code, "Qty": b.qty,
            "Trigger (>%d)" % config.parallel_trigger_qty: r7.should_parallelize(b.qty, config),
        })
    trace["rule7"] = {
        "input": to_table([{"Parallel trigger qty": config.parallel_trigger_qty}]),
        "output": to_table(rows7),
        "config": config.to_dict(),
        "notes": ["no batch exceeds the trigger in this dataset" if not any(
            r7.should_parallelize(b.qty, config) for b in plan_run.batches_prioritized) else "parallel CNC applied"],
        "error": None, "reached": True,
    }

    # Rule 8 — current saved actuals + per-item downtime/output rollup.
    actuals = r8.load_actuals()
    total_down = sum(a.total_downtime_min() for a in actuals)
    trace["rule8"] = {
        "input": to_table([{"Source": "Daily Production Entry form → data/actuals.json"}]),
        "output": to_table(actuals),
        "config": None,
        "notes": [
            f"{len(actuals)} actual(s) on record; total downtime {total_down:g} min.",
            "Good qty (produced − rejected) feeds Rule 9's balance; downtime is "
            "aggregated per item code below.",
        ],
        "tables": [
            {"title": "Per item code — output & downtime rollup (minutes summed across entries)",
             "table": to_table(r8.aggregate_by_item(actuals))},
        ],
        "error": None, "reached": True,
    }
    return trace


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class RunRequest(BaseModel):
    config: Optional[dict] = None
    dataset_id: Optional[str] = None  # uploaded workbook; None -> bundled test file


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


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    """Parse an uploaded masters/SO Excel and cache it as a dataset the run
    endpoints can use. The frontend keeps the returned dataset_id and sends it
    with each /run, /rerun, /items, /gantt call."""
    contents = await file.read()
    try:
        so_lines, masters = load_all(io.BytesIO(contents))
    except Exception as e:  # noqa: BLE001 — surface any parse failure to the user
        raise HTTPException(status_code=400, detail=f"Could not read Excel: {e}")

    dataset_id = uuid.uuid4().hex[:8]
    _DATASETS[dataset_id] = {"so_lines": so_lines, "masters": masters, "name": file.filename}
    # Persist the raw workbook to the durable store (if configured) so the dataset
    # survives cold starts / other instances.
    kv = get_kv()
    if kv is not None:
        kv.set(_dataset_key(dataset_id), base64.b64encode(contents).decode("ascii"))
    return {
        "dataset_id": dataset_id,
        "name": file.filename,
        "summary": {
            "so_lines": len(so_lines),
            "items": len(masters.routings),
            "machines": len(masters.machines),
        },
        "report": _report_table(masters),
    }


@app.post("/run")
def run(req: Optional[RunRequest] = None):
    so_lines, masters = _data(req.dataset_id if req else None)
    config = Config.from_dict(req.config if req else None)
    try:
        config.validate()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    plan_run = PlanRun(so_lines=so_lines)
    trace = run_forward(plan_run, config, masters)
    _augment_helpers(trace, plan_run, config, masters)

    # Rule 9 isn't part of the forward run; guide the user to the Rerun button.
    trace["rule9"] = {
        "input": {"columns": [], "rows": []},
        "output": {"columns": [], "rows": []},
        "config": None,
        "notes": ["Use the 'Rerun MRP' button: re-plans from balance (SO qty − produced) by re-calling Rules 1–7."],
        "error": None, "reached": True,
    }

    run_id = uuid.uuid4().hex[:8]
    _RUNS[run_id] = trace
    gantt = build_gantt(plan_run.schedule, plan_run.batches_prioritized, masters)
    return {"run_id": run_id, "trace": trace, "report": _report_table(masters), "gantt": gantt}


@app.get("/gantt")
def gantt(dataset_id: Optional[str] = None):
    """Gantt for a default-config forward run (so the tab works before any Run)."""
    so_lines, masters = _data(dataset_id)
    config = Config()
    plan_run = PlanRun(so_lines=so_lines)
    run_forward(plan_run, config, masters)
    return build_gantt(plan_run.schedule, plan_run.batches_prioritized, masters)


@app.get("/trace/{run_id}")
def get_trace(run_id: str):
    if run_id not in _RUNS:
        raise HTTPException(status_code=404, detail="run not found")
    return {"run_id": run_id, "trace": _RUNS[run_id]}


@app.get("/report")
def report(dataset_id: Optional[str] = None):
    _, masters = _data(dataset_id)
    return _report_table(masters)


@app.get("/items")
def items(dataset_id: Optional[str] = None):
    """Item metadata for the Daily Production Entry form: item name (auto-prompt)
    and process list (dropdown) per item code, plus SO→item and the shift list."""
    so_lines, masters = _data(dataset_id)
    so_by_item = {}
    for s in so_lines:
        so_by_item.setdefault(s.item_code, set()).add(s.so_no)
    items_map = {
        code: {
            "item_name": routing.description,
            "processes": [p.name for p in routing.processes],
            "so_nos": sorted(so_by_item.get(code, [])),
        }
        for code, routing in masters.routings.items()
    }
    return {
        "items": items_map,
        "shifts": ["1st shift", "2nd shift"],
        "so_to_item": {s.so_no: s.item_code for s in so_lines},
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
        remarks=req.remarks,
    )
    all_actuals = r8.run(actual)
    return {
        "saved": len(all_actuals),
        "actuals": to_table(all_actuals),
        "by_item": to_table(r8.aggregate_by_item(all_actuals)),
    }


@app.post("/rerun")
def rerun(req: Optional[RunRequest] = None):
    so_lines, masters = _data(req.dataset_id if req else None)
    config = Config.from_dict(req.config if req else None)
    actuals = r8.load_actuals()
    result = r9.run(so_lines, config=config, masters=masters, actuals=actuals)

    trace = result["trace"]
    _augment_helpers(trace, result["plan_run"], config, masters)
    # Rule 9 tab: show the balance-quantity SO lines that were re-planned.
    trace["rule9"] = {
        "input": to_table([{"Original SO lines": len(so_lines), "Actuals applied": len(actuals)}]),
        "output": to_table(result["so_lines"]),
        "config": config.to_dict(),
        "notes": [f"re-planned {len(result['so_lines'])} balance SO line(s)"],
        "error": None, "reached": True,
    }

    run_id = uuid.uuid4().hex[:8]
    _RUNS[run_id] = trace
    gantt = build_gantt(result["plan_run"].schedule,
                        result["plan_run"].batches_prioritized, masters)
    return {"run_id": run_id, "trace": trace, "report": _report_table(masters), "gantt": gantt}


# Static frontend (mounted last so the API routes above take precedence).
app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
