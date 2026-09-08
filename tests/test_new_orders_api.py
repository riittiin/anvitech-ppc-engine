"""Add New Orders: storage, endpoints, role gating (2026-09-08 spec)."""
import importlib
import io
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store, loaders, orderbook
from engine.config import Config
from engine.models import Order, PlanRun, fmt_date
from tests.new_sample_workbook import build_new_sample_bytes, ITEM_A


def _rows(table):
    """``to_table()``'s rows are plain VALUE LISTS in ``table["columns"]``
    order, not dicts — zip them back into dicts for readable assertions."""
    return [dict(zip(table["columns"], row)) for row in table["rows"]]


def test_drafts_round_trip():
    book_store.save_new_order_drafts([{"so_no": "NEW-1", "item_code": "X",
                                       "item_name": "x", "qty": 10}])
    assert book_store.load_new_order_drafts() == [
        {"so_no": "NEW-1", "item_code": "X", "item_name": "x", "qty": 10}]


def test_drafts_default_to_empty():
    book_store.save_new_order_drafts([])
    assert book_store.load_new_order_drafts() == []


def test_queue_round_trips_and_clears():
    book_store.save_new_order_queue([("NEW-1", "X"), ("NEW-2", "Y")])
    assert book_store.load_new_order_queue() == [["NEW-1", "X"], ["NEW-2", "Y"]]
    book_store.clear_new_order_queue()
    assert book_store.load_new_order_queue() == []


# --- validation ---

def _masters():
    _so, masters = loaders.load_all(io.BytesIO(build_new_sample_bytes()))
    return masters


def _an_item(masters):
    return sorted(masters.routings)[0]


def test_a_good_line_validates():
    m = _masters()
    assert orderbook.validate_new_order_line("NEW-1", _an_item(m), 10, {}, {}, m) is None


def test_a_blank_so_number_is_refused():
    m = _masters()
    assert "SO number" in orderbook.validate_new_order_line("  ", _an_item(m), 10, {}, {}, m)


def test_a_zero_or_negative_quantity_is_refused():
    m = _masters()
    for bad in (0, -5):
        assert "quantity" in orderbook.validate_new_order_line("NEW-1", _an_item(m),
                                                               bad, {}, {}, m).lower()


def test_an_unknown_item_is_refused():
    m = _masters()
    assert "routing" in orderbook.validate_new_order_line("NEW-1", "GHOST", 10,
                                                          {}, {}, m).lower()


def test_a_duplicate_names_where_it_already_is():
    m = _masters()
    item = _an_item(m)
    existing = Order(so_no="NEW-1", item_code=item, item_name="x", ordered_qty=300,
                     delivery_date=date(2025, 3, 20))
    msg = orderbook.validate_new_order_line("new-1", item, 10,
                                            {existing.key: existing}, {}, m)
    assert "already in the book" in msg
    assert "300" in msg


def test_a_duplicate_of_a_COMPLETED_order_is_also_refused():
    m = _masters()
    item = _an_item(m)
    done = Order(so_no="NEW-1", item_code=item, item_name="x", ordered_qty=300,
                 delivery_date=date(2025, 3, 20), completed=True)
    assert "already in the book" in orderbook.validate_new_order_line(
        "NEW-1", item, 10, {}, {done.key: done}, m)


# --- occupancy is new-engine-only (2026-09-08, Task 10 requirement) --- #

def test_occupancy_is_rejected_outside_the_new_engine():
    """``rule6_allocate.run`` (classic) and ``flow_scheduler.run`` (flow) both
    declare ``**kw`` and would silently discard ``occupancy`` — a two-stage
    quote on either of those would look like it protects the existing plan
    while doing nothing. ``run_forward`` must refuse loudly instead."""
    from engine.pipeline import OccupancyRequiresNewEngineError, run_forward

    masters = _masters()
    cfg = Config(plan_start_date=date(2025, 3, 1), scheduler="classic")
    plan_run = PlanRun(so_lines=[])
    with pytest.raises(OccupancyRequiresNewEngineError) as exc:
        run_forward(plan_run, cfg, masters, occupancy={"machine": {}, "operator": {}})
    assert "classic" in str(exc.value)


# --- Task 10: draft endpoints and the quote endpoint --- #

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _api_with_new_engine(monkeypatch):
    """These endpoints plan through ``run_forward(occupancy=...)``, which only the
    new engine honours (see the ``OccupancyRequiresNewEngineError`` test above) —
    force ``DEFAULT_SCHEDULER=new`` and confirm the pin actually took. Without
    this, every test below would either fail loudly for the wrong reason or
    (with the env unset, as in the rest of the suite) run on the classic engine
    and prove nothing about the feature it claims to test."""
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    import api.main as m
    importlib.reload(m)
    resolved = m._resolve_config(m._load_plan_config())
    assert resolved.scheduler == "new", (
        f"DEFAULT_SCHEDULER=new did not take: resolved scheduler is "
        f"{resolved.scheduler!r}")
    return m


@pytest.fixture
def _api_module(monkeypatch):
    return _api_with_new_engine(monkeypatch)


@pytest.fixture
def admin_client(_api_module):
    c = TestClient(_api_module.app)
    r = c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    assert r.status_code in (200, 303), r.text
    return c


@pytest.fixture
def user_client(_api_module):
    c = TestClient(_api_module.app)
    r = c.post("/login", data={"username": "anvitech_user", "password": "anvitech12345678"})
    assert r.status_code in (200, 303), r.text
    return c


@pytest.fixture
def uploaded_masters(admin_client):
    """Upload the new-engine sample workbook (admin only) and return one item
    code it defines, for a draft line's item_code."""
    r = admin_client.post("/upload", files={
        "file": ("new.xlsx", build_new_sample_bytes(), XLSX_MIME)})
    assert r.status_code == 200, r.text
    return ITEM_A


@pytest.fixture
def add_new_order(admin_client):
    """PUT a single-line draft, quote it, accept it, and return the quoted
    completion date (an ISO date string) — the whole "type it, quote it,
    accept it" flow a director drives by hand, in one call, for the tests
    that only care about what ends up in the book (Task 11/12, 2026-09-08)."""
    def _add(so_no, item_code, qty):
        r = admin_client.put("/new-orders/drafts",
                             json={"drafts": [{"so_no": so_no, "item_code": item_code,
                                               "qty": qty}]})
        assert r.status_code == 200, r.text
        quote = admin_client.post("/new-orders/quote").json()
        added = admin_client.post("/new-orders/add", json={"stamp": quote["stamp"]})
        assert added.status_code == 200, added.text
        return quote["lines"][0]["completion"]
    return _add


@pytest.fixture
def upload_sample(admin_client):
    """Re-upload the new-engine sample workbook — a fresh upload, not the
    original one `uploaded_masters` already did."""
    def _upload():
        r = admin_client.post("/upload", files={
            "file": ("new2.xlsx", build_new_sample_bytes(), XLSX_MIME)})
        assert r.status_code == 200, r.text
        return r
    return _upload


def test_drafts_are_admin_only(user_client):
    assert user_client.get("/new-orders/drafts").status_code == 403
    assert user_client.put("/new-orders/drafts", json={"drafts": []}).status_code == 403
    assert user_client.post("/new-orders/quote").status_code == 403


def test_saving_and_reading_back_drafts(admin_client, uploaded_masters):
    item = uploaded_masters
    r = admin_client.put("/new-orders/drafts",
                         json={"drafts": [{"so_no": "NEW-1", "item_code": item,
                                           "qty": 10}]})
    assert r.status_code == 200
    got = admin_client.get("/new-orders/drafts").json()
    assert got["drafts"][0]["so_no"] == "NEW-1"
    assert got["drafts"][0]["item_name"]
    assert any(i["item_code"] == item for i in got["items"])


def test_a_bad_draft_line_is_refused_with_a_message(admin_client, uploaded_masters):
    r = admin_client.put("/new-orders/drafts",
                         json={"drafts": [{"so_no": "", "item_code": uploaded_masters,
                                           "qty": 10}]})
    assert r.status_code == 400
    assert "SO number" in r.json()["detail"]


def test_a_quote_returns_a_date_and_confirms_nothing_moved(admin_client,
                                                           uploaded_masters):
    admin_client.put("/new-orders/drafts",
                     json={"drafts": [{"so_no": "NEW-1",
                                       "item_code": uploaded_masters, "qty": 10}]})
    r = admin_client.post("/new-orders/quote")
    assert r.status_code == 200
    body = r.json()
    assert body["verified"] is True
    assert body["moved"] == []
    assert body["lines"][0]["completion"]
    assert body["existing_count"] >= 0
    assert body["stamp"]


def test_quoting_with_no_drafts_is_a_clear_400(admin_client, uploaded_masters):
    admin_client.put("/new-orders/drafts", json={"drafts": []})
    r = admin_client.post("/new-orders/quote")
    assert r.status_code == 400
    assert "no new orders" in r.json()["detail"].lower()


# --- Task 10 review fix: the intra-payload dedup + no-partial-persistence, --- #
# --- both correct but untested before (2026-09-08) --- #

def test_a_duplicate_line_within_the_same_payload_is_refused(admin_client,
                                                              uploaded_masters):
    r = admin_client.put("/new-orders/drafts",
                         json={"drafts": [
                             {"so_no": "NEW-1", "item_code": uploaded_masters, "qty": 10},
                             {"so_no": "NEW-1", "item_code": uploaded_masters, "qty": 5}]})
    assert r.status_code == 400
    assert "twice" in r.json()["detail"]


def test_a_bad_line_saves_no_line_from_the_same_payload(admin_client, uploaded_masters):
    admin_client.put("/new-orders/drafts", json={"drafts": []})   # start clean
    r = admin_client.put("/new-orders/drafts",
                         json={"drafts": [
                             {"so_no": "NEW-1", "item_code": uploaded_masters, "qty": 10},
                             {"so_no": "", "item_code": uploaded_masters, "qty": 5}]})
    assert r.status_code == 400
    # The first (valid) line must NOT have been persisted just because it came
    # before the bad one in the same payload — the whole PUT is all-or-nothing.
    assert admin_client.get("/new-orders/drafts").json()["drafts"] == []


# --- Task 12: accepting a quote, and clearing the queue --- #

def test_adding_saves_the_orders_with_the_quoted_delivery_date(admin_client,
                                                               uploaded_masters):
    admin_client.put("/new-orders/drafts",
                     json={"drafts": [{"so_no": "NEW-1",
                                       "item_code": uploaded_masters, "qty": 25}]})
    quote = admin_client.post("/new-orders/quote").json()
    r = admin_client.post("/new-orders/add", json={"stamp": quote["stamp"]})
    assert r.status_code == 200
    # GET /orders returns the same to_table() shape as POST /run's "orders" key:
    # {"columns": [...], "rows": [...]} — not a bare list of dicts — and the
    # delivery date column is "SO Delivery Date", rendered DD-MM-YYYY (fmt_date),
    # not the ISO string the quote returns.
    rows = {(o["SO No"], o["Item Code"]): o
            for o in _rows(admin_client.get("/orders").json()["orders"])}
    row = rows[("NEW-1", uploaded_masters)]
    quoted_date = date.fromisoformat(quote["lines"][0]["completion"])
    assert row["SO Delivery Date"] == fmt_date(quoted_date)


def test_adding_clears_the_drafts(admin_client, uploaded_masters):
    admin_client.put("/new-orders/drafts",
                     json={"drafts": [{"so_no": "NEW-1",
                                       "item_code": uploaded_masters, "qty": 25}]})
    quote = admin_client.post("/new-orders/quote").json()
    admin_client.post("/new-orders/add", json={"stamp": quote["stamp"]})
    assert admin_client.get("/new-orders/drafts").json()["drafts"] == []


def test_a_stale_quote_is_refused_and_says_to_requote(admin_client, uploaded_masters):
    admin_client.put("/new-orders/drafts",
                     json={"drafts": [{"so_no": "NEW-1",
                                       "item_code": uploaded_masters, "qty": 25}]})
    admin_client.post("/new-orders/quote")
    r = admin_client.post("/new-orders/add", json={"stamp": "not-the-real-stamp"})
    assert r.status_code == 409
    assert "Finish and Optimize" in r.json()["detail"]


def test_adding_is_admin_only(user_client):
    assert user_client.post("/new-orders/add", json={"stamp": "x"}).status_code == 403


def test_done_entering_clears_the_queue(admin_client, uploaded_masters, add_new_order):
    add_new_order("NEW-1", uploaded_masters, 25)
    assert book_store.load_new_order_queue()
    admin_client.post("/optimize/done")
    assert book_store.load_new_order_queue() == []


def test_an_upload_clears_the_queue(admin_client, uploaded_masters, add_new_order,
                                    upload_sample):
    add_new_order("NEW-1", uploaded_masters, 25)
    upload_sample()
    assert book_store.load_new_order_queue() == []
