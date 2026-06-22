"""Rule 8 — Capture daily actuals.

After each shift, the previous period's actual production (qty produced/rejected,
setup time, downtime reason) is entered and persisted. This is the ONLY thing the
app writes — to ``data/actuals.json`` (Test2.xlsx stays read-only).

``save_actuals``/``load_actuals`` own the JSON store. ``run`` appends one or more
entries and returns the full, current list of actuals.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from ..models import Actual
from ..storage import get_kv

# Key used when a durable store (Upstash) is configured.
ACTUALS_KEY = "anvitech:actuals"

def _default_store() -> Path:
    """Where actuals are persisted.

    * ACTUALS_PATH env var wins (set this to a durable store in production).
    * On Vercel (read-only FS) fall back to /tmp/actuals.json — note this is
      EPHEMERAL: wiped on cold starts. Use a real store (Vercel KV/Postgres) for
      durable actuals; see README deployment notes.
    * Locally: ./data/actuals.json.
    """
    env = os.environ.get("ACTUALS_PATH")
    if env:
        return Path(env)
    if os.environ.get("VERCEL"):
        return Path("/tmp/actuals.json")
    return Path(__file__).resolve().parent.parent.parent / "data" / "actuals.json"


DEFAULT_STORE = _default_store()


def load_actuals(store_path=None):
    kv = get_kv()
    if kv is not None:
        raw = kv.get(ACTUALS_KEY)
        return [Actual.from_json(d) for d in json.loads(raw)] if raw else []
    p = Path(store_path or DEFAULT_STORE)
    if not p.exists():
        return []
    with p.open("r", encoding="utf-8") as fh:
        raw = json.load(fh)
    return [Actual.from_json(d) for d in raw]


def save_actuals(actuals, store_path=None):
    kv = get_kv()
    if kv is not None:
        kv.set(ACTUALS_KEY, json.dumps([a.to_json() for a in actuals]))
        return
    p = Path(store_path or DEFAULT_STORE)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump([a.to_json() for a in actuals], fh, indent=2)


def aggregate_by_item(actuals):
    """Roll up output + downtime per item code — this is where each entry's
    minutes 'get added for that item code' (e.g. all No-operator minutes summed).
    Returns one row per item code."""
    agg = {}
    order = []
    for a in actuals:
        if a.item_code not in agg:
            order.append(a.item_code)
            agg[a.item_code] = {
                "Item Code": a.item_code,
                "Item Name": a.item_name,
                "Entries": 0,
                "Qty Produced": 0.0,
                "Qty Rejected": 0.0,
                "Good Qty": 0.0,
                "Setup (min)": 0.0,
                "No Power": 0.0,
                "No Operator": 0.0,
                "Tool Problem": 0.0,
                "M/c Breakdown": 0.0,
                "No Load": 0.0,
                "Other Work": 0.0,
                "Total Downtime (min)": 0.0,
            }
        d = agg[a.item_code]
        if not d["Item Name"] and a.item_name:
            d["Item Name"] = a.item_name
        d["Entries"] += 1
        d["Qty Produced"] += a.qty_produced
        d["Qty Rejected"] += a.qty_rejected
        d["Good Qty"] += a.good_qty()
        d["Setup (min)"] += a.actual_setup_min
        d["No Power"] += a.no_power_min
        d["No Operator"] += a.no_operator_min
        d["Tool Problem"] += a.tool_problem_min
        d["M/c Breakdown"] += a.machine_breakdown_min
        d["No Load"] += a.no_load_min
        d["Other Work"] += a.other_work_min
        d["Total Downtime (min)"] += a.total_downtime_min()
    return [agg[c] for c in order]


def run(new_entries, config=None, notes=None, masters=None, store_path=None, **kw):
    """Append ``new_entries`` (Actual or list[Actual]) to the store; return all."""
    notes = notes if notes is not None else []
    store_path = store_path or DEFAULT_STORE
    if isinstance(new_entries, Actual):
        new_entries = [new_entries]
    existing = load_actuals(store_path)
    existing.extend(new_entries or [])
    save_actuals(existing, store_path)
    for a in (new_entries or []):
        notes.append(
            f"Recorded {a.qty_produced:g} produced / {a.qty_rejected:g} rejected "
            f"(good {a.good_qty():g}) for {a.item_code} (SO {a.so_no}) on "
            f"{a.entry_date.isoformat()}; setup {a.actual_setup_min:g} min, "
            f"downtime {a.total_downtime_min():g} min"
        )
    return existing
