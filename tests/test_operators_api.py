"""GET/POST/PATCH/DELETE /operators: role-gated CRUD over the app-owned
operator/shift master table (Task 3 of the operator-master-rotation plan).
`_current_masters()` seeds the table once from the uploaded workbook and
applies any due Friday rotation before GET reads it back; POST/PATCH/DELETE
never touch the workbook and never trigger the scheduled-only optimize
contest."""
from datetime import date

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from engine import book_store, orderbook
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
        Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
    ])


def _admin_client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    return c


def _user_client(m):
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech_user",
                           "password": "anvitech12345678"})
    return c


# --- GET: seeding + shape ------------------------------------------------ #
def test_get_before_any_upload_returns_empty_list(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    r = admin.get("/operators")
    assert r.status_code == 200
    body = r.json()
    assert body["operators"] == []
    assert body["next_rotation"] == m.operator_master.next_rotation(date.today()).isoformat()


def test_get_after_upload_seeds_from_masters(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    r = admin.get("/operators")
    assert r.status_code == 200
    names = {row["name"] for row in r.json()["operators"]}
    assert "Operator One" in names
    assert "Operator Two" in names
    row_one = next(row for row in r.json()["operators"] if row["name"] == "Operator One")
    assert "id" in row_one
    assert row_one["pinned"] is False


# --- role gating ---------------------------------------------------------- #
def test_user_can_get_but_not_mutate(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    user = _user_client(m)

    r = user.get("/operators")
    assert r.status_code == 200

    r = user.post("/operators", json={"name": "New Hire"})
    assert r.status_code == 403

    # Grab a real id as admin to attempt user-side PATCH/DELETE against.
    op_id = admin.get("/operators").json()["operators"][0]["id"]

    r = user.patch(f"/operators/{op_id}", json={"pinned": True})
    assert r.status_code == 403

    r = user.delete(f"/operators/{op_id}")
    assert r.status_code == 403


# --- POST ------------------------------------------------------------------ #
def test_post_creates_table_when_none_existed(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    assert book_store.load_operator_table() is None

    r = admin.post("/operators", json={"name": "New Hire",
                                       "machines_raw": "CNC 1",
                                       "shift": "First shift"})
    assert r.status_code == 200
    row = r.json()["operator"]
    assert row["name"] == "New Hire"
    assert row["machines_raw"] == "CNC 1"
    assert row["shift"] == "First shift"
    assert row["pinned"] is False
    assert "id" in row

    table = book_store.load_operator_table()
    assert table is not None
    assert table["week_anchor"] == m.operator_master.last_friday(date.today()).isoformat()
    assert len(table["operators"]) == 1


def test_post_defaults_machines_raw_and_shift_to_blank(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    r = admin.post("/operators", json={"name": "Blank Defaults"})
    assert r.status_code == 200
    row = r.json()["operator"]
    assert row["machines_raw"] == ""
    assert row["shift"] == ""


def test_post_appends_to_existing_table(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    admin.get("/operators")  # trigger seed
    before = len(book_store.load_operator_table()["operators"])

    r = admin.post("/operators", json={"name": "New Hire"})
    assert r.status_code == 200

    after = book_store.load_operator_table()["operators"]
    assert len(after) == before + 1
    assert any(row["name"] == "New Hire" for row in after)


def test_post_empty_name_is_400(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    r = admin.post("/operators", json={"name": "   "})
    assert r.status_code == 400


def test_post_duplicate_name_case_insensitive_is_400(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    admin.get("/operators")  # trigger seed ("Operator One" exists)

    r = admin.post("/operators", json={"name": "  operator one  "})
    assert r.status_code == 400


def test_post_invalid_shift_is_400(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    r = admin.post("/operators", json={"name": "Bad Shift", "shift": "Night shift"})
    assert r.status_code == 400


def test_post_no_optimize_trigger(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "manual")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3")
    m = _api(); _seed_book()
    admin = _admin_client(m)
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))

    r = admin.post("/operators", json={"name": "New Hire"})
    assert r.status_code == 200
    assert not starts


# --- PATCH ------------------------------------------------------------------ #
def test_patch_updates_partial_fields(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    op_id = admin.get("/operators").json()["operators"][0]["id"]

    r = admin.patch(f"/operators/{op_id}", json={"pinned": True})
    assert r.status_code == 200
    assert r.json()["operator"]["pinned"] is True

    # Other fields untouched by a partial patch.
    r2 = admin.get("/operators")
    row = next(row for row in r2.json()["operators"] if row["id"] == op_id)
    assert row["pinned"] is True
    assert row["machines_raw"] != ""  # seeded value preserved


def test_patch_updates_machines_raw_and_shift(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    op_id = admin.get("/operators").json()["operators"][0]["id"]

    r = admin.patch(f"/operators/{op_id}",
                    json={"machines_raw": "CNC 3/CNC 4", "shift": "Second shift"})
    assert r.status_code == 200
    row = r.json()["operator"]
    assert row["machines_raw"] == "CNC 3/CNC 4"
    assert row["shift"] == "Second shift"


def test_patch_invalid_shift_is_400(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    op_id = admin.get("/operators").json()["operators"][0]["id"]

    r = admin.patch(f"/operators/{op_id}", json={"shift": "Nope"})
    assert r.status_code == 400


def test_patch_unknown_id_is_404(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    admin.get("/operators")  # trigger seed
    r = admin.patch("/operators/not-a-real-id", json={"pinned": True})
    assert r.status_code == 404


def test_patch_unknown_id_is_404_when_table_never_created(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    r = admin.patch("/operators/not-a-real-id", json={"pinned": True})
    assert r.status_code == 404


# --- DELETE ------------------------------------------------------------------ #
def test_delete_happy_path(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    op_id = admin.get("/operators").json()["operators"][0]["id"]

    r = admin.delete(f"/operators/{op_id}")
    assert r.status_code == 200 and r.json() == {"deleted": True}

    remaining_ids = [row["id"] for row in admin.get("/operators").json()["operators"]]
    assert op_id not in remaining_ids

    # deleting again (unknown id now) -> 404
    r = admin.delete(f"/operators/{op_id}")
    assert r.status_code == 404


def test_delete_unknown_id_is_404(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    r = admin.delete("/operators/not-a-real-id")
    assert r.status_code == 404


def test_delete_unknown_id_is_404_when_table_never_created(monkeypatch):
    m = _api()
    admin = _admin_client(m)
    r = admin.delete("/operators/not-a-real-id")
    assert r.status_code == 404


# --- orphan absences after delete (reuses tests/test_absences_api.py pattern) - #
def test_delete_orphans_absences_non_blocking(monkeypatch):
    m = _api(); _seed_book()
    admin = _admin_client(m)
    rows = admin.get("/operators").json()["operators"]
    op_one = next(row for row in rows if row["name"] == "Operator One")

    book_store.save_absence({"operator": "Operator One",
                             "from_date": "2025-03-10",
                             "to_date": "2025-03-12"})

    r = admin.delete(f"/operators/{op_one['id']}")
    assert r.status_code == 200

    masters = m._current_masters()
    so_lines = orderbook.active_so_lines(book_store.load_active_orders(),
                                         book_store.load_actuals(), masters)
    report = m._report_for_book(masters, so_lines)
    cols = report["columns"]
    kinds_refs = [(dict(zip(cols, row))["Kind"], dict(zip(cols, row))["Reference"])
                 for row in report["rows"]]
    assert ("ABSENT_OPERATOR_UNKNOWN", "Operator One") in kinds_refs

    r = admin.get("/absences")
    assert "Operator One" in r.json()["orphans"]


# --- no event trigger for PATCH/DELETE (scheduled-optimize design) -------- #
def test_patch_and_delete_do_not_start_a_contest(monkeypatch):
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "manual")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3")
    m = _api(); _seed_book()
    admin = _admin_client(m)
    op_id = admin.get("/operators").json()["operators"][0]["id"]
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))

    r = admin.patch(f"/operators/{op_id}", json={"pinned": True})
    assert r.status_code == 200
    assert not starts

    r = admin.delete(f"/operators/{op_id}")
    assert r.status_code == 200
    assert not starts
