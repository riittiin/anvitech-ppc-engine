"""Persistence for the order book, actuals, and uploaded masters.

Thin layer over ``storage.get_store()``. Orders are a hash keyed by the
**(SO number, item code)** pair — encoded as a single composite string, since a
hash field must be a string (see ``_skey``) — because an SO number is NOT unique;
only the pair is. Per-field writes never clash; actuals are an append-only list
(no overwrite); the latest uploaded workbook is a single value. All durable in
production (Upstash) and on local disk in dev.
"""
from __future__ import annotations

import base64
import json
import uuid

from .models import Order, Actual
from .storage import get_store

ORDERS_KEY = "anvitech:orders"             # hash: "so_no\x1fitem_code" -> Order json
COMPLETED_KEY = "anvitech:orders:completed"  # hash: same composite field (archive)
ACTUALS_KEY = "anvitech:actuals"           # list of Actual json
MASTERS_KEY = "anvitech:masters"           # kv: base64 of the latest workbook
PLAN_CONFIG_KEY = "anvitech:plan_config"   # kv: json of the admin's saved Config

_SEP = "\x1f"   # ASCII unit separator — never appears in an SO# or item code


def _skey(so_no: str, item_code: str) -> str:
    """The hash-field string that stores one order = its (SO#, item) pair. Using a
    control char keeps it collision-free vs any real SO#/item text."""
    return f"{so_no}{_SEP}{item_code}"


# --- orders --- #
def load_active_orders() -> dict:
    """Active orders keyed by the ``(so_no, item_code)`` tuple (the order's identity)."""
    h = get_store().hgetall(ORDERS_KEY)
    orders = [Order.from_json(json.loads(v)) for v in h.values()]
    return {o.key: o for o in orders}


def load_completed_orders() -> dict:
    h = get_store().hgetall(COMPLETED_KEY)
    orders = [Order.from_json(json.loads(v)) for v in h.values()]
    return {o.key: o for o in orders}


def add_orders(orders) -> None:
    s = get_store()
    for o in orders:
        s.hset(ORDERS_KEY, _skey(o.so_no, o.item_code), json.dumps(o.to_json()))


def delete_orders(order_keys) -> int:
    """Permanently delete orders (active or archived) by their ``(so_no, item_code)``
    pair, and purge those orders' production actuals. Returns how many order keys
    were targeted. Only the exact item line is removed — a sibling line sharing the
    SO number is left intact."""
    s = get_store()
    targets = {tuple(k) for k in order_keys}
    for so_no, item_code in targets:
        s.hdel(ORDERS_KEY, _skey(so_no, item_code))
        s.hdel(COMPLETED_KEY, _skey(so_no, item_code))
    remaining = [a for a in load_actuals() if a.key not in targets]
    s.list_set(ACTUALS_KEY, [json.dumps(a.to_json()) for a in remaining])
    return len(targets)


def delete_all() -> None:
    """Wipe all orders + actuals (keeps the uploaded masters)."""
    s = get_store()
    s.delete_key(ORDERS_KEY)
    s.delete_key(COMPLETED_KEY)
    s.delete_key(ACTUALS_KEY)


def complete_order(so_no: str, item_code: str) -> bool:
    """Move one active order — the ``(so_no, item_code)`` line — into the completed
    archive. Returns False if unknown. A sibling item line on the same SO is
    unaffected."""
    s = get_store()
    o = load_active_orders().get((so_no, item_code))
    if o is None:
        return False
    o.completed = True
    field = _skey(so_no, item_code)
    s.hset(COMPLETED_KEY, field, json.dumps(o.to_json()))
    s.hdel(ORDERS_KEY, field)
    return True


def uncomplete_order(so_no: str, item_code: str) -> bool:
    """Move a completed order — the ``(so_no, item_code)`` line — BACK to active
    (un-archive). Returns False if it isn't in the completed archive. Used when a
    'mark complete' entry is rolled back."""
    s = get_store()
    o = load_completed_orders().get((so_no, item_code))
    if o is None:
        return False
    o.completed = False
    field = _skey(so_no, item_code)
    s.hset(ORDERS_KEY, field, json.dumps(o.to_json()))
    s.hdel(COMPLETED_KEY, field)
    return True


def set_commitment(so_no: str, item_code: str, commitment: str,
                   promised_date, committed_at: str) -> bool:
    """Set an ACTIVE order's commitment lane + promised date. `commitment` is
    'committed' or 'urgent'; `promised_date` is a date or None; `committed_at` is an
    ISO datetime string (passed in so this stays deterministic). False if unknown."""
    s = get_store()
    o = load_active_orders().get((so_no, item_code))
    if o is None:
        return False
    o.commitment = commitment
    o.promised_date = promised_date
    o.committed_at = committed_at
    s.hset(ORDERS_KEY, _skey(so_no, item_code), json.dumps(o.to_json()))
    return True


def clear_commitment(so_no: str, item_code: str) -> bool:
    """Reset an active order back to the Open lane (clears promise). False if unknown."""
    s = get_store()
    o = load_active_orders().get((so_no, item_code))
    if o is None:
        return False
    o.commitment = "open"
    o.promised_date = None
    o.committed_at = None
    s.hset(ORDERS_KEY, _skey(so_no, item_code), json.dumps(o.to_json()))
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
