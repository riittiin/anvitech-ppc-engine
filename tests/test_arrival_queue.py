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


def test_two_lines_in_one_accept_sharing_an_item_are_clubbed_into_one_batch(
        admin_client, uploaded_masters):
    """2026-09-11 review, finding B corrected: the reviewer's own instruction
    was the reason Defect B stayed open — chaining "each queued order in
    arrival order" fixed sequential SINGLE-LINE accepts, but `engine.quote.
    quote` plans every draft line of ONE accept POOLED in a single `_stage2`
    call, so two draft lines sharing an item code get CLUBBED by Rule 1 and
    run as one batch, paying the CNC setup once. Planning one line per chain
    step could never reproduce that: two lines of the same accept would run
    as two separate batches, get two different (often much later) dates, and
    pay the 90-minute setup twice. The chain unit has to be the ACCEPT, not
    the order, for this to hold.

    Proves both the date and the mechanism: one PUT with two draft lines
    sharing an item code, one quote, one add -- the two lines quote the SAME
    date (Rule 1 clubbed them at quote time), the Orders tab shows that same
    date for both after accepting, and at the source there is exactly ONE
    batch and exactly one CNC step scheduled for it, not two."""
    r = admin_client.put("/new-orders/drafts",
                         json={"drafts": [
                             {"so_no": "NEW-1", "item_code": uploaded_masters, "qty": 25},
                             {"so_no": "NEW-2", "item_code": uploaded_masters, "qty": 25}]})
    assert r.status_code == 200, r.text
    quote = admin_client.post("/new-orders/quote").json()
    assert quote["verified"] is True, quote
    quoted = {l["so_no"]: l["completion"] for l in quote["lines"]}
    assert quoted["NEW-1"] == quoted["NEW-2"], (
        "two lines pooled into one accept, sharing an item, should be clubbed "
        "by Rule 1 and quote the same date")

    r = admin_client.post("/new-orders/add", json={"stamp": quote["stamp"]})
    assert r.status_code == 200, r.text

    body = admin_client.post("/run").json()
    key1, key2 = f"NEW-1\x1f{uploaded_masters}", f"NEW-2\x1f{uploaded_masters}"
    assert body["expected_end"].get(key1) == quoted["NEW-1"]
    assert body["expected_end"].get(key2) == quoted["NEW-2"]

    # At the source: one clubbed batch, one CNC step (one setup), not two.
    import api.main as m
    art = m._PLAN_CACHE.get("artifacts")
    covering_both = [b for b in art["plan_run"].batches_prioritized
                     if {"NEW-1", "NEW-2"} <= set(b.source_so_refs or [])]
    assert len(covering_both) == 1, (
        f"NEW-1 and NEW-2 should be clubbed into exactly one batch, found "
        f"{len(covering_both)} batches covering both")
    bid = covering_both[0].batch_id
    cnc_entries = [e for e in art["plan_run"].schedule
                  if e.batch_id == bid and "CNC" in e.process_name.upper()]
    assert len(cnc_entries) == 1, (
        f"expected ONE CNC step for the clubbed batch (one setup), found "
        f"{len(cnc_entries)} (paying the 90-minute setup more than once)")


def test_a_contended_multi_item_accept_reproduces_its_own_quote(
        admin_client, uploaded_masters):
    """A genuinely contended two-item accept (CNC2 taken out of service, so
    both items' CNC steps compete for the single remaining CNC1) must quote
    a date each line then keeps on the very next screen. This is a real
    regression pin, but it does NOT discriminate the specific mechanism
    (`priority_rank` derived from the accept's own winning arrangement) on
    this fixture, measured directly: it passes both with and without that
    `priority_rank` here, because the sample workbook has only two distinct
    items, so a pooled accept can only ever produce two distinct batches, and
    `engine.quote.quote`'s exhaustive 2-permutation search over two items
    happens to land on the same order Rule 3 already picks unranked for this
    specific pair. The mechanism itself is proven load-bearing on the real
    books instead (10-line accepts, mostly distinct items, so quote()'s
    24-arrangement SAMPLED search can and does find an order Rule 3's own
    unranked tie-break does not stumble onto): removing it there reintroduces
    mismatches on 8, 7 and 3 of 10 lines on Test5/8/9 respectively (see the
    task report for the exact numbers). Kept here anyway because "does the
    quoted date survive a genuinely contended accept" is worth pinning even
    without a fixture that also isolates the mechanism."""
    today = date.today()
    admin_client.post("/machine-downtime", json={
        "machine": "CNC2", "from_date": today.isoformat(),
        "to_date": (today + timedelta(days=60)).isoformat(), "reason": "test"})
    r = admin_client.put("/new-orders/drafts",
                         json={"drafts": [
                             {"so_no": "NEW-1", "item_code": uploaded_masters, "qty": 400},
                             {"so_no": "NEW-2", "item_code": ITEM_B, "qty": 400}]})
    assert r.status_code == 200, r.text
    quote = admin_client.post("/new-orders/quote").json()
    assert quote["verified"] is True, quote
    quoted = {l["so_no"]: l["completion"] for l in quote["lines"]}
    assert quoted["NEW-1"] != quoted["NEW-2"], (
        "test setup: the two items should genuinely contend and NOT quote "
        "the same date (they are different items, never clubbed)")

    r = admin_client.post("/new-orders/add", json={"stamp": quote["stamp"]})
    assert r.status_code == 200, r.text

    body = admin_client.post("/run").json()
    shown1 = body["expected_end"].get(f"NEW-1\x1f{uploaded_masters}")
    shown2 = body["expected_end"].get(f"NEW-2\x1f{ITEM_B}")
    assert shown1 == quoted["NEW-1"], f"NEW-1: quoted {quoted['NEW-1']}, shown {shown1}"
    assert shown2 == quoted["NEW-2"], f"NEW-2: quoted {quoted['NEW-2']}, shown {shown2}"


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
    book_store.save_new_order_queue([[[SO1, ITEM_A]]])  # one group, one line
    try:
        body = c.post("/run").json()
        rows = _rows(body["orders"])
        assert any(row["SO No"] == SO1 for row in rows), \
            "the queued order vanished from the plan"
        assert "classic" in body["queue_note"]
    finally:
        book_store.clear_new_order_queue()


def test_gantt_never_republishes_an_existing_orders_date_and_the_new_order_gets_its_own_row(
        admin_client, uploaded_masters, add_new_order):
    """2026-09-11 review, Defect A (Critical): `rule1_consolidate` restarts its
    batch-id counter (B001, B002, ...) on every call, and `_plan` calls the
    rule chain more than once (stage 1, then stage 2), so a queued order's
    batch id collided with a pre-existing order's. `build_gantt` groups rows
    by `batch_id` and publishes `completion = max(end)`, so a new order's
    bars were glued onto the pre-existing order's Gantt row, republishing
    that order's completion as the new order's end date -- and the new order
    had no row of its own at all."""
    before = admin_client.post("/run").json()
    before_rows = {r["batch_id"]: r for r in before["gantt"]["rows"]}
    assert before_rows, "test setup: the existing book should already have Gantt rows"

    quoted = add_new_order("NEW-1", uploaded_masters, 25)

    after = admin_client.post("/run").json()
    after_rows = {r["batch_id"]: r for r in after["gantt"]["rows"]}

    for bid, row in before_rows.items():
        assert bid in after_rows, f"pre-existing batch {bid} ({row['so_no']}) vanished"
        # Check identity, not only the date: a collision can glue a new order's
        # bars onto this row WITHOUT moving the published date at all (a tiny
        # fixture can have both orders finish on the same calendar day by pure
        # coincidence) but still silently swaps whose row this is.
        assert after_rows[bid]["so_no"] == row["so_no"], (
            f"{bid} was {row['so_no']}'s row, now shows {after_rows[bid]['so_no']} "
            f"just because a new order was added")
        assert after_rows[bid]["completion"] == row["completion"], (
            f"{bid} ({row['so_no']}) moved from {row['completion']} to "
            f"{after_rows[bid]['completion']} just because a new order was added")

    new_rows = [r for r in after["gantt"]["rows"]
               if "NEW-1" in [s.strip() for s in r["so_no"].split(",")]]
    assert len(new_rows) == 1, (
        f"NEW-1 should have exactly one Gantt row of its own, found {len(new_rows)}")
    # The Gantt renders DD-MM-YYYY; the quote returns an ISO date string.
    assert new_rows[0]["completion"] == date.fromisoformat(quoted).strftime("%d-%m-%Y")


def test_machine_timeline_never_republishes_an_existing_orders_dates(
        admin_client, uploaded_masters, add_new_order):
    """Same root cause as the Gantt test above, different consumer:
    `rule6_allocate.build_machine_view` looks up both "Expected completion"
    and "SO Del date" by `batch_id`, which can collide across stages — a
    collision makes `batch_by_id.get(e.batch_id)` resolve to whichever
    Batch object is LAST in the dict for that id, so every row sharing the
    id (including the pre-existing order's) reads the wrong batch's SO
    Delivery Date. Grouped by "SO No" (each schedule entry's OWN field,
    never corrupted by a batch-id collision) so this checks the published
    values for a known-correct identity, exactly what a director reads off
    the row. "SO Del date" is the more reliable check of the two here — two
    real orders' actual delivery dates are essentially never equal, where
    two orders' newly-computed COMPLETION dates can coincidentally match on
    a small fixture and mask the exact same corruption (measured directly:
    the two orders in the sample book both land on the same completion date,
    but their real SO Delivery Dates differ by about a year)."""
    before = admin_client.post("/run").json()
    before_rows = _rows(before["trace"]["rule6"]["tables"][0]["table"])
    before_by_so = {r["SO No"]: (r["Expected completion"], r["SO Del date"])
                    for r in before_rows if r.get("Expected completion")}
    assert before_by_so, "test setup: the existing book should publish completions"

    add_new_order("NEW-1", uploaded_masters, 25)

    after = admin_client.post("/run").json()
    after_rows = _rows(after["trace"]["rule6"]["tables"][0]["table"])
    after_by_so = {r["SO No"]: (r["Expected completion"], r["SO Del date"])
                   for r in after_rows if r.get("Expected completion")}

    for so_no, (completion, delivery) in before_by_so.items():
        got = after_by_so.get(so_no)
        assert got == (completion, delivery), (
            f"machine timeline {so_no} was (completion={completion}, "
            f"delivery={delivery}), now shows {got} just because a new "
            f"order was added")


def test_five_sequential_adds_never_move_an_already_accepted_new_order(
        admin_client, uploaded_masters, add_new_order):
    """2026-09-11 review, Defect B (Critical): the plan re-planned ALL queued
    lines together in one pool ordered by `priority_rank`, which steers
    Rule 3's ordering but does not PIN a placement, so the greedy dispatcher
    could displace an already-accepted new order once a later one arrived.
    Chaining (each queued order planned against the accumulated occupancy of
    stage 1 plus every queued order chained before it, exactly like
    /new-orders/quote plans each accept) makes the plan and the quote the
    same computation, and makes first come first served hold for new orders
    too, not only for the pre-existing book."""
    accepted = []
    for i in range(1, 6):
        so_no = f"NEW-{i}"
        item = uploaded_masters if i % 2 else ITEM_B
        quoted = add_new_order(so_no, item, 25)
        body = admin_client.post("/run").json()
        key = f"{so_no}\x1f{item}"
        assert body["expected_end"].get(key) == quoted, (
            f"{so_no}: quoted {quoted}, but /run shows "
            f"{body['expected_end'].get(key)} on the very next screen")
        for pso, pitem, pquoted in accepted:
            pkey = f"{pso}\x1f{pitem}"
            assert body["expected_end"].get(pkey) == pquoted, (
                f"{pso} moved from {pquoted} to {body['expected_end'].get(pkey)} "
                f"after {so_no} was added")
        accepted.append((so_no, item, quoted))


def test_the_merged_batch_list_covers_a_queued_orders_own_batch(
        admin_client, uploaded_masters, add_new_order):
    """Smaller finding from the same review: `_report_for_book` receives the
    MERGED schedule but only stage 1's batches, so
    `new_engine.batch_quantity_violations` silently skips every stage-2
    entry ("an entry from a batch we weren't given") -- a queued order's own
    quantity was never actually checked, and on the owner's real books this
    produced 1 to 5 false BATCH_QTY_SHORT rows too. Proven at the source:
    the merged batch list `_report_for_book` is handed must include the
    queued order's own batch."""
    add_new_order("NEW-1", uploaded_masters, 25)
    admin_client.post("/run")
    import api.main as m
    art = m._PLAN_CACHE.get("artifacts")
    covering = [b.batch_id for b in art["plan_run"].batches_prioritized
               if "NEW-1" in (b.source_so_refs or [])]
    assert covering, "NEW-1's own batch must be in the batches _report_for_book uses"
