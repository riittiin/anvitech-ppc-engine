"""Machine maintenance: the plumbing that makes a break reach every plan —
the book signature, the cloud payload and the contest setup."""
import io
import json
from datetime import date

from engine import book_store, loaders, optimize_service as svc, orderbook
from engine.config import Config
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A


def _book():
    wb = build_sample_bytes()
    book_store.save_masters_bytes(wb)
    _so, masters = loaders.load_all(io.BytesIO(wb))
    book_store.add_orders([Order("SO1", ITEM_A, "A", 20, date(2025, 3, 20))])
    cfg = Config(plan_start_date=date(2025, 3, 1))
    return book_store.load_active_orders(), cfg, masters


def _lines():
    orders, _cfg, masters = _book()
    return orderbook.active_so_lines(orders, book_store.load_actuals(), masters)


DOWN = [{"machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"}]


def test_book_signature_moves_when_a_break_is_added():
    lines = _lines()
    assert svc.book_signature(lines) != svc.book_signature(lines, downtimes=DOWN)


def test_book_signature_is_byte_identical_for_the_empty_case():
    """No pre-existing caller's signature may move just because the parameter
    now exists (the same guard `frozen` carries)."""
    lines = _lines()
    base = svc.book_signature(lines)
    assert svc.book_signature(lines, downtimes=[]) == base
    assert svc.book_signature(lines, downtimes=None) == base
    assert svc.book_signature(lines, absences=[], frozen=[], downtimes=[]) == base


def test_book_signature_is_order_insensitive_and_restores():
    lines = _lines()
    two = DOWN + [{"machine": "VMC1", "from_date": "2025-03-09", "to_date": "2025-03-09"}]
    assert svc.book_signature(lines, downtimes=two) == \
           svc.book_signature(lines, downtimes=list(reversed(two)))
    a = svc.book_signature(lines, downtimes=two)
    assert svc.book_signature(lines, downtimes=DOWN) != a
    assert svc.book_signature(lines, downtimes=two) == a


def test_a_downtime_only_book_cannot_collide_with_a_frozen_only_book():
    """The appended element is tagged, so the two optional blocks are never
    structurally confusable."""
    lines = _lines()
    frozen = [{"so_no": "SO1", "item_code": ITEM_A, "op_seq": 2,
               "machine": "CNC1", "remaining_qty": 4}]
    assert svc.book_signature(lines, frozen=frozen) != \
           svc.book_signature(lines, downtimes=DOWN)


def test_payload_round_trips_machine_downtime():
    orders, cfg, _m = _book()
    payload = svc.build_payload(orders, [], build_sample_bytes(), cfg, seed=42,
                                machine_downtime=DOWN)
    assert payload["machine_downtime"] == DOWN
    payload = json.loads(json.dumps(payload))          # the network hop
    result = svc.parse_payload(payload)
    assert len(result) == 8
    assert result[-1] == DOWN


def test_an_older_payload_without_the_key_still_parses():
    orders, cfg, _m = _book()
    payload = svc.build_payload(orders, [], build_sample_bytes(), cfg, seed=42)
    payload.pop("machine_downtime")                    # a job dispatched pre-deploy
    assert svc.parse_payload(payload)[-1] == []


def test_prepare_contest_reserves_the_machine_alongside_the_operators():
    orders, cfg, masters = _book()
    setup = svc.prepare_contest(orders, [], masters, cfg, machine_downtime=DOWN)
    assert "CNC1" in setup.unavailable_reserved
    assert setup.machine_downtime == DOWN


def test_prepare_contest_with_no_breaks_reserves_nothing():
    orders, cfg, masters = _book()
    setup = svc.prepare_contest(orders, [], masters, cfg)
    assert setup.unavailable_reserved is None
    assert setup.machine_downtime == []


# --------------------------------------------------------------------------- #
# The plan itself
# --------------------------------------------------------------------------- #
import importlib
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient


def _api():
    import api.main
    return importlib.reload(api.main)


def _seed(m):
    wb = build_sample_bytes()
    book_store.save_masters_bytes(wb)
    book_store.add_orders([Order("SO1", ITEM_A, "A", 20, date(2025, 3, 20))])
    m._current_masters()          # trigger the one-time operator seed


def test_a_break_changes_the_book_signature_the_cache_keys_on():
    m = _api()
    _seed(m)
    before = m._current_book_sig()
    book_store.save_machine_downtime(
        {"machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"})
    assert m._current_book_sig() != before


def test_adding_a_break_invalidates_the_plan_cache():
    """Mirrors test_plan_cache.py::test_absence_invalidates. Without this the
    Settings panel would add a break and the screen would show the old plan."""
    m = _api()
    _seed(m)
    cfg = m._load_plan_config()
    first = m._plan(cfg)
    assert m._plan(cfg)["run_id"] == first["run_id"]        # cache hit
    book_store.save_machine_downtime(
        {"machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"})
    assert m._plan(cfg)["run_id"] != first["run_id"]        # recomputed


def test_the_plan_schedules_nothing_on_a_machine_that_is_out_of_service():
    m = _api()
    _seed(m)
    plain = m._plan(m._load_plan_config())
    used = {e.machine for e in m._PLAN_CACHE["artifacts"]["plan_run"].schedule}
    if "CNC1" not in used:
        pytest.skip("this sample book does not use CNC1; nothing to prove")
    start = m._resolve_config(m._load_plan_config()).plan_start_date
    book_store.save_machine_downtime(
        {"machine": "CNC1", "from_date": start.isoformat(),
         "to_date": (start + timedelta(days=3)).isoformat()})
    m._plan(m._load_plan_config())
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    clash = [e for e in sched
             if e.machine == "CNC1"
             and e.start.date() <= start + timedelta(days=3)]
    assert clash == [], f"work planned on an out-of-service machine: {clash[:3]}"


def test_removing_the_break_restores_the_original_plan():
    m = _api()
    _seed(m)
    cfg = m._load_plan_config()
    before = m._plan(cfg)
    row = book_store.save_machine_downtime(
        {"machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"})
    m._plan(cfg)
    book_store.delete_machine_downtime(row["id"])
    after = m._plan(cfg)
    assert after["expected_end"] == before["expected_end"]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
def _client(m, role="admin"):
    c = TestClient(m.app)
    creds = ({"username": "anvitech", "password": "1930rail"} if role == "admin"
             else {"username": "anvitech_user", "password": "anvitech12345678"})
    c.post("/login", data=creds)
    return c


def test_get_is_open_to_both_roles_and_lists_only_cnc_and_vmc():
    m = _api()
    _seed(m)
    for role in ("admin", "user"):
        r = _client(m, role).get("/machine-downtime")
        assert r.status_code == 200
        body = r.json()
        assert set(body) == {"downtime", "orphans", "machines"}
        assert body["downtime"] == [] and body["orphans"] == []
        ids = [x["id"] for x in body["machines"]]
        assert ids, "the picker must offer something"
        assert all(i.startswith(("CNC", "VMC")) for i in ids), ids


def test_the_picker_excludes_manual_and_inspection_stations():
    m = _api()
    _seed(m)
    ids = [x["id"] for x in _client(m).get("/machine-downtime").json()["machines"]]
    masters = m._current_masters()
    manual = [k for k in masters.machines if k.startswith(("MW", "MD", "MPK", "MI", "BS"))]
    assert manual, "the sample book should have manual stations to exclude"
    assert not (set(ids) & set(manual))


def test_post_and_delete_are_admin_only():
    m = _api()
    _seed(m)
    user = _client(m, "user")
    body = {"machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"}
    assert user.post("/machine-downtime", json=body).status_code == 403
    assert user.delete("/machine-downtime/whatever").status_code == 403
    assert book_store.load_machine_downtime() == []


def test_admin_can_add_and_remove_a_break():
    m = _api()
    _seed(m)
    admin = _client(m)
    r = admin.post("/machine-downtime", json={
        "machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06",
        "reason": "Spindle service"})
    assert r.status_code == 200
    row = r.json()["downtime"]
    assert row["machine"] == "CNC1" and row["reason"] == "Spindle service"
    assert len(admin.get("/machine-downtime").json()["downtime"]) == 1
    assert admin.delete(f"/machine-downtime/{row['id']}").json() == {"deleted": True}
    assert admin.get("/machine-downtime").json()["downtime"] == []


def test_bad_input_is_rejected_with_a_clear_message():
    m = _api()
    _seed(m)
    admin = _client(m)
    bad_date = admin.post("/machine-downtime", json={
        "machine": "CNC1", "from_date": "05-03-2025", "to_date": "2025-03-06"})
    assert bad_date.status_code == 400
    assert "YYYY-MM-DD" in bad_date.json()["detail"]
    unknown = admin.post("/machine-downtime", json={
        "machine": "NOPE9", "from_date": "2025-03-05", "to_date": "2025-03-06"})
    assert unknown.status_code == 400 and "NOPE9" in unknown.json()["detail"]
    assert admin.delete("/machine-downtime/nope").status_code == 404


def test_a_reversed_range_is_swapped_not_rejected():
    m = _api()
    _seed(m)
    r = _client(m).post("/machine-downtime", json={
        "machine": "CNC1", "from_date": "2025-03-06", "to_date": "2025-03-05"})
    assert r.status_code == 200
    row = r.json()["downtime"]
    assert (row["from_date"], row["to_date"]) == ("2025-03-05", "2025-03-06")


def test_an_absurd_date_range_is_rejected():
    """A fat-fingered to_date must not reach the store: several engine paths walk a
    break day by day, so a year-9999 end date would be millions of iterations per row
    on every plan. Validate at the boundary, where the input enters."""
    m = _api()
    _seed(m)
    r = _client(m).post("/machine-downtime", json={
        "machine": "CNC1", "from_date": "2025-03-05", "to_date": "9999-12-31"})
    assert r.status_code == 400
    assert "longer than a year" in r.json()["detail"]
    assert book_store.load_machine_downtime() == []


def test_a_long_but_plausible_break_is_accepted():
    """The bound must not reject a real, if unusual, outage."""
    m = _api()
    _seed(m)
    r = _client(m).post("/machine-downtime", json={
        "machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-09-05"})
    assert r.status_code == 200


def test_a_break_on_a_machine_that_left_the_master_is_flagged_not_fatal():
    m = _api()
    _seed(m)
    book_store.save_machine_downtime(
        {"machine": "GHOST1", "from_date": "2025-03-05", "to_date": "2025-03-06"})
    admin = _client(m)
    assert admin.get("/machine-downtime").json()["orphans"] == ["GHOST1"]
    rows = admin.post("/run", json={}).json()["report"]["rows"]
    pairs = {(r[0], r[1]) for r in rows}
    assert ("MACHINE_DOWNTIME_UNKNOWN", "GHOST1") in pairs
    # Non-blocking: the plan still ran.
    assert admin.post("/run", json={}).json()["gantt"] is not None


def test_adding_a_break_never_starts_a_contest(monkeypatch):
    """Only the Done button triggers a search — same rule as absences."""
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "manual")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", "s3")
    m = _api()
    _seed(m)
    starts = []
    monkeypatch.setattr(m, "_start_optimize",
                        lambda *a, **k: starts.append(1))
    admin = _client(m)
    r = admin.post("/machine-downtime", json={
        "machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"})
    admin.delete(f"/machine-downtime/{r.json()['downtime']['id']}")
    assert starts == []
