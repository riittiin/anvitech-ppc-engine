"""Persistence for the order book, actuals, and uploaded masters.

Thin layer over ``storage.get_store()``. Orders are a hash keyed by SO number
(per-field writes never clash); actuals are an append-only list (no overwrite);
the latest uploaded workbook is a single value. All durable in production
(Upstash) and on local disk in dev.
"""
from __future__ import annotations

import base64
import json
import uuid

from .models import Order, Actual
from .storage import get_store

ORDERS_KEY = "anvitech:orders"             # hash: so_no -> Order json
COMPLETED_KEY = "anvitech:orders:completed"  # hash: so_no -> Order json (archive)
ACTUALS_KEY = "anvitech:actuals"           # list of Actual json
MASTERS_KEY = "anvitech:masters"           # kv: base64 of the latest workbook
PLAN_CONFIG_KEY = "anvitech:plan_config"   # kv: json of the admin's saved Config


# --- orders --- #
def load_active_orders() -> dict:
    h = get_store().hgetall(ORDERS_KEY)
    return {sn: Order.from_json(json.loads(v)) for sn, v in h.items()}


def load_completed_orders() -> dict:
    h = get_store().hgetall(COMPLETED_KEY)
    return {sn: Order.from_json(json.loads(v)) for sn, v in h.items()}


def add_orders(orders) -> None:
    s = get_store()
    for o in orders:
        s.hset(ORDERS_KEY, o.so_no, json.dumps(o.to_json()))


def delete_orders(so_nos) -> int:
    """Permanently delete orders (active or archived) by SO number, and purge
    their production actuals. Returns how many SO numbers were targeted."""
    s = get_store()
    targets = set(so_nos)
    for sn in targets:
        s.hdel(ORDERS_KEY, sn)
        s.hdel(COMPLETED_KEY, sn)
    remaining = [a for a in load_actuals() if a.so_no not in targets]
    s.list_set(ACTUALS_KEY, [json.dumps(a.to_json()) for a in remaining])
    return len(targets)


def delete_all() -> None:
    """Wipe all orders + actuals (keeps the uploaded masters)."""
    s = get_store()
    s.delete_key(ORDERS_KEY)
    s.delete_key(COMPLETED_KEY)
    s.delete_key(ACTUALS_KEY)


def complete_order(so_no: str) -> bool:
    """Move an active order into the completed archive. Returns False if unknown."""
    s = get_store()
    active = load_active_orders()
    o = active.get(so_no)
    if o is None:
        return False
    o.completed = True
    s.hset(COMPLETED_KEY, so_no, json.dumps(o.to_json()))
    s.hdel(ORDERS_KEY, so_no)
    return True


def uncomplete_order(so_no: str) -> bool:
    """Move a completed order BACK to active (un-archive). Returns False if it
    isn't in the completed archive. Used when a 'mark complete' entry is rolled back."""
    s = get_store()
    o = load_completed_orders().get(so_no)
    if o is None:
        return False
    o.completed = False
    s.hset(ORDERS_KEY, so_no, json.dumps(o.to_json()))
    s.hdel(COMPLETED_KEY, so_no)
    return True


# --- actuals (append-safe) --- #
def load_actuals() -> list:
    """Load all actuals. Backfills a stable id on any legacy entry that lacks one
    (one-time persist) so rollback can target an exact entry."""
    raw = get_store().list_all(ACTUALS_KEY)
    actuals = [Actual.from_json(json.loads(v)) for v in raw]
    if any(not a.id for a in actuals):
        for a in actuals:
            if not a.id:
                a.id = uuid.uuid4().hex
        get_store().list_set(ACTUALS_KEY, [json.dumps(a.to_json()) for a in actuals])
    return actuals


def append_actual(actual: Actual) -> None:
    if not actual.id:
        actual.id = uuid.uuid4().hex
    get_store().list_append(ACTUALS_KEY, json.dumps(actual.to_json()))


def delete_actual(actual_id: str):
    """Remove the actual with ``actual_id``. Returns the removed Actual, or None
    if no entry matched."""
    actuals = load_actuals()
    target = next((a for a in actuals if a.id == actual_id), None)
    if target is None:
        return None
    remaining = [a for a in actuals if a.id != actual_id]
    get_store().list_set(ACTUALS_KEY, [json.dumps(a.to_json()) for a in remaining])
    return target


# --- masters workbook --- #
def save_masters_bytes(raw: bytes) -> None:
    get_store().kv_set(MASTERS_KEY, base64.b64encode(raw).decode("ascii"))


def load_masters_bytes():
    raw = get_store().kv_get(MASTERS_KEY)
    return base64.b64decode(raw) if raw else None


# --- plan config (admin's saved scheduling settings) --- #
def save_plan_config(raw_json: str) -> None:
    get_store().kv_set(PLAN_CONFIG_KEY, raw_json)


def load_plan_config():
    return get_store().kv_get(PLAN_CONFIG_KEY)
