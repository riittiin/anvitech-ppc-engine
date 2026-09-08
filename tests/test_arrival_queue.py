"""First come, first served: a queued new order never moves the book (2026-09-08).

Response-key corrections vs the original design sketch (verified against the
running API rather than assumed):

  * ``POST /run``'s "orders" key is ``to_table(...)`` — ``{"columns": [...],
    "rows": [...]}`` — not a bare list of dicts. Iterate ``body["orders"]["rows"]``.
  * There is no "Expected completion" row field. Expected completion lives in
    ``body["expected_end"]``, keyed ``f"{so_no}\\x1f{item_code}"``, ISO date
    strings — comparing that dict directly is both simpler and exactly what
    "did this order's date move" means.
  * The delivery-date column is "SO Delivery Date" (DD-MM-YYYY via
    ``engine.models.fmt_date``), not "Delivery Date".

The fixtures that drive the new-engine API (``admin_client``, ``user_client``,
``uploaded_masters``, ``add_new_order``) are defined once in
``tests.test_new_orders_api`` and imported here rather than duplicated.
"""
from dataclasses import replace
from datetime import date, timedelta

from engine import book_store
from tests.new_sample_workbook import ITEM_B, SO1, ITEM_A

from tests.test_new_orders_api import (   # noqa: F401 -- fixtures, used by name
    _api_module, admin_client, user_client, uploaded_masters, add_new_order,
)


def _rows(table):
    """``to_table()``'s rows are plain VALUE LISTS in ``table["columns"]``
    order, not dicts — zip them back into dicts for readable assertions."""
    return [dict(zip(table["columns"], row)) for row in table["rows"]]


def test_with_an_empty_queue_the_plan_is_unchanged(admin_client, uploaded_masters):
    book_store.clear_new_order_queue()
    a = admin_client.post("/run").json()
    b = admin_client.post("/run").json()
    assert a["expected_end"] == b["expected_end"]


def test_a_queued_order_does_not_move_any_existing_order(admin_client,
                                                         uploaded_masters,
                                                         add_new_order):
    before = admin_client.post("/run").json()["expected_end"]
    add_new_order("NEW-1", uploaded_masters, 25)
    after = admin_client.post("/run").json()["expected_end"]
    for key, date_before in before.items():
        assert after.get(key) == date_before, f"{key} moved"


def test_a_queued_order_appears_in_the_plan_with_a_date(admin_client,
                                                        uploaded_masters,
                                                        add_new_order):
    quoted = add_new_order("NEW-1", uploaded_masters, 25)
    body = admin_client.post("/run").json()
    key = f"NEW-1\x1f{uploaded_masters}"
    assert body["expected_end"].get(key) == quoted
    rows = _rows(body["orders"])
    assert any(r["SO No"] == "NEW-1" and r["Item Code"] == uploaded_masters
              for r in rows)


def test_orders_added_one_at_a_time_never_disturb_each_other(admin_client,
                                                             uploaded_masters,
                                                             add_new_order):
    first = add_new_order("NEW-1", uploaded_masters, 25)
    add_new_order("NEW-2", uploaded_masters, 25)
    body = admin_client.post("/run").json()
    key = f"NEW-1\x1f{uploaded_masters}"
    assert body["expected_end"].get(key) == first


def test_two_queued_lines_each_reproduce_their_own_quoted_date(admin_client,
                                                               uploaded_masters,
                                                               add_new_order):
    """2026-09-08 review finding 2: `_plan`'s stage 2 must replay the queue's
    OWN recorded arrival order (each accept's winning arrangement), or it can
    silently re-sequence two-or-more queued lines differently from what each
    was quoted against, moving an already-accepted order. Checks BOTH dates,
    not just the first — the first alone does not exercise the sequencing
    between the two queued lines the way this finding is about.

    Uses two DIFFERENT items (Item A, Item B) deliberately: same-item queued
    lines get CONSOLIDATED by Rule 1 once they are planned TOGETHER in one
    stage-2 pass, which is a genuinely different physical batch than the two
    separate single-line batches each was quoted as — no priority_rank fix
    can make a consolidated run reproduce two unconsolidated quotes, because
    consolidation is a joint decision over the whole queued set, not a
    per-line one. That is a real, separate limitation (see the task report),
    not the bug this finding is about; different items sidestep it entirely
    and isolate the sequencing question finding 2 actually raises."""
    first = add_new_order("NEW-1", uploaded_masters, 25)
    second = add_new_order("NEW-2", ITEM_B, 25)
    body = admin_client.post("/run").json()
    assert body["expected_end"].get(f"NEW-1\x1f{uploaded_masters}") == first
    assert body["expected_end"].get(f"NEW-2\x1f{ITEM_B}") == second


def test_a_contended_book_proves_stage_two_protects_existing_orders(
        admin_client, uploaded_masters, add_new_order):
    """2026-09-08 review, finding 3: the sample book is normally so
    uncontended that one-stage and two-stage planning land on the same date
    for every existing order, so `test_a_queued_order_does_not_move_any_
    existing_order` above still passes with stage 2 deleted entirely (proven
    by mutation below). This builds a fixture that genuinely contends:

      * CNC2 is taken out of service, forcing every Item A step onto the one
        remaining CNC1 (both the existing order and the new one need it).
      * SO1 is re-dated to be due SOON rather than already overdue, so it is
        not automatically the most urgent order in the book regardless of
        stage (an overdue order always sorts first by Rule 2's date order,
        which would mask the effect this test needs to show).

    A large new order competing for CNC1 then measurably displaces SO1 once
    the two are planned in ONE pool — proving the two-stage split, not a
    lucky uncontended fixture, is what protects it. Measured directly before
    writing this test: without the re-date, or without the downtime, the
    one-stage mutation below does NOT move SO1 either (both conditions are
    load-bearing for the fixture to contend at all)."""
    today = date.today()
    active = book_store.load_active_orders()
    so1 = replace(active[(SO1, ITEM_A)], delivery_date=today + timedelta(days=3))
    book_store.add_orders([so1])
    admin_client.post("/machine-downtime", json={
        "machine": "CNC2", "from_date": today.isoformat(),
        "to_date": (today + timedelta(days=60)).isoformat(), "reason": "test"})

    before = admin_client.post("/run").json()["expected_end"].get(f"{SO1}\x1f{ITEM_A}")
    add_new_order("NEW-1", uploaded_masters, 500)
    after = admin_client.post("/run").json()["expected_end"].get(f"{SO1}\x1f{ITEM_A}")
    assert after == before, f"SO1 moved from {before} to {after}"


def test_the_plan_cache_notices_the_queue(admin_client, uploaded_masters,
                                          add_new_order):
    admin_client.post("/run")
    add_new_order("NEW-1", uploaded_masters, 25)
    rows = _rows(admin_client.post("/run").json()["orders"])
    assert any(r["SO No"] == "NEW-1" for r in rows), \
        "a cached plan was served after the book changed"


def test_clearing_the_queue_alone_busts_the_plan_cache(admin_client,
                                                       uploaded_masters,
                                                       add_new_order):
    """`_plan_fingerprint` must include the queue itself, not just the book: an
    order can leave the queue (Done entering, an applied deep search, an
    upload) with the book's own order ROWS unchanged, so `book_rows` alone
    would not notice — a plan computed while the order was still queued would
    keep being served, complete with a stale "planned behind" rule6 note.
    `trace` is returned verbatim on a cache hit (unlike `orders`/`auto_note`/
    `queue_note`, which are rebuilt live), so its rule6 notes are the tell."""
    add_new_order("NEW-1", uploaded_masters, 25)
    first = admin_client.post("/run").json()
    assert any("planned behind" in n for n in first["trace"]["rule6"]["notes"]), \
        "test setup: the queued order should have produced a 'planned behind' note"
    book_store.clear_new_order_queue()
    second = admin_client.post("/run").json()
    assert not any("planned behind" in n for n in second["trace"]["rule6"]["notes"]), \
        "a plan computed while the order was still queued was served after the " \
        "queue was cleared"


def test_a_deleted_queued_order_drops_out_without_special_handling(
        admin_client, uploaded_masters, add_new_order):
    add_new_order("NEW-1", uploaded_masters, 25)
    # /orders/delete's real request shape (tests/test_api.py): {"orders": [[so,
    # item], ...], "password": ...} — not {"keys": [...]}.
    r = admin_client.post("/orders/delete",
                          json={"orders": [["NEW-1", uploaded_masters]],
                                "password": "1930rail"})
    assert r.status_code == 200
    body = admin_client.post("/run").json()
    assert all(r["SO No"] != "NEW-1" for r in _rows(body["orders"]))


def test_a_queue_on_a_non_new_engine_plans_in_one_stage_with_a_visible_note(
        monkeypatch):
    """A deployment running the retired classic/flow engine that also happens to
    have a queue on file (e.g. the saved scheduler was switched back after the
    queue was built under the new engine) must never 500 on every plan just
    because two-stage planning (``run_forward(occupancy=...)``) is new-engine
    only — and must never silently drop the queued order from the plan either
    (the silent-omission class this codebase has been bitten by before). Both
    are proved directly: the whole book, queued order included, plans in ONE
    stage, and the reason is on screen (mandatory beyond the briefs, 2026-09-08).
    """
    monkeypatch.delenv("DEFAULT_SCHEDULER", raising=False)
    import importlib
    import api.main as m
    importlib.reload(m)
    from fastapi.testclient import TestClient
    from tests.sample_workbook import build_sample_bytes, SO1, ITEM_A

    resolved = m._resolve_config(m._load_plan_config())
    assert resolved.scheduler == "classic", (
        f"expected the code default 'classic' with no env override, got "
        f"{resolved.scheduler!r}")

    c = TestClient(m.app)
    r = c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    assert r.status_code in (200, 303), r.text
    xlsx_mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    up = c.post("/upload", files={"file": ("sample.xlsx", build_sample_bytes(), xlsx_mime)})
    assert up.status_code == 200, up.text

    # A queue can exist on a classic deployment only as leftover state — the
    # /new-orders/* endpoints themselves require the new engine to compute a
    # quote at all, so simulate the leftover directly rather than through them.
    book_store.save_new_order_queue([[SO1, ITEM_A]])
    try:
        body = c.post("/run").json()
        rows = _rows(body["orders"])
        assert any(row["SO No"] == SO1 for row in rows), \
            "the queued order vanished from the plan"
        assert "classic" in body["queue_note"]
    finally:
        book_store.clear_new_order_queue()
