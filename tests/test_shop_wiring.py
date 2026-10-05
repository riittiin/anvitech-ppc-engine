import json
from datetime import date

from engine import book_store, shop_masters as sm
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed_book():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
                           Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21))])


def test_first_masters_read_seeds_both_tables_once():
    m = _api(); _seed_book()
    masters = m._current_masters()
    mdoc, cdoc = book_store.load_machines_doc(), book_store.load_shop_calendar()
    assert set(mdoc["machines"]) <= set(masters.machines)
    assert cdoc is not None
    mdoc["machines"][next(iter(mdoc["machines"]))]["type"] = "EDITED"
    book_store.save_machines_doc(mdoc)
    m._current_masters()
    assert "EDITED" in json.dumps(book_store.load_machines_doc())     # not re-seeded


def test_masters_never_open_the_workbook_once_seeded(monkeypatch):
    m = _api(); _seed_book()
    m._current_masters()                                 # seeds everything
    m._MASTERS_CACHE["masters"] = None
    import openpyxl
    def boom(*a, **k):
        raise AssertionError("workbook opened")
    monkeypatch.setattr(openpyxl, "load_workbook", boom)
    from ppc_engine.loaders import loader as ppc_loader
    monkeypatch.setattr(ppc_loader, "open_workbook", boom)
    masters = m._current_masters()
    assert masters.routings and masters.machines and masters.operators


def test_masters_follow_a_machine_edit_and_a_holiday():
    m = _api(); _seed_book(); m._current_masters()
    mdoc = book_store.load_machines_doc()
    mid = next(iter(mdoc["machines"]))
    mdoc["machines"][mid]["hours"] = 9.5
    book_store.save_machines_doc(mdoc)
    assert m._current_masters().machines[mid].available_hrs_per_day == 9.5
    book_store.save_shop_calendar(sm.add_holiday(book_store.load_shop_calendar(),
                                                 "2026-11-08", "Diwali", "t"))
    assert date(2026, 11, 8) in m._current_masters().calendar.holidays


def test_operator_seed_still_reads_the_workbook_once():
    m = _api(); _seed_book()
    assert book_store.load_operator_table() is None
    m._current_masters()
    table = book_store.load_operator_table()
    assert table and table["operators"]


def test_signatures_unchanged_by_seeding_and_moved_by_edits(monkeypatch):
    m = _api(); _seed_book()
    cfg = m._load_plan_config()
    m._current_masters()
    seeded = m._inputs_signature(cfg)
    f0 = m._plan_fingerprint(cfg)
    real_m, real_c = book_store.load_machines_doc, book_store.load_shop_calendar
    monkeypatch.setattr(m.book_store, "load_machines_doc", lambda: None)
    monkeypatch.setattr(m.book_store, "load_shop_calendar", lambda: None)
    m._MASTERS_CACHE["masters"] = None
    assert m._inputs_signature(cfg) == seeded            # seeded tables == stage-1 signature
    monkeypatch.setattr(m.book_store, "load_machines_doc", real_m)
    monkeypatch.setattr(m.book_store, "load_shop_calendar", real_c)
    book_store.save_shop_calendar(sm.add_holiday(book_store.load_shop_calendar(),
                                                 "2026-11-08", "Diwali", "t"))
    assert m._inputs_signature(cfg) != seeded
    assert m._plan_fingerprint(cfg) != f0


def test_masters_sha_is_cached_not_refetched_per_call(monkeypatch):
    m = _api(); _seed_book()
    cfg = m._load_plan_config()
    m._plan_fingerprint(cfg)

    def boom():
        raise AssertionError("workbook fetched on a warm cache")
    real = book_store.load_masters_bytes
    monkeypatch.setattr(m.book_store, "load_masters_bytes", boom)
    m._plan_fingerprint(cfg)                      # must not raise
    old = m._masters_sha()
    m._MASTERS_CACHE["masters"] = None
    monkeypatch.setattr(m.book_store, "load_masters_bytes", real)
    book_store.save_masters_bytes(build_sample_bytes() + b"x")
    assert m._masters_sha() != old


def test_new_engine_masters_follow_the_tables_without_a_workbook(monkeypatch):
    from engine import new_engine
    new_engine._MASTERS_CACHE.clear()
    m = _api(); _seed_book(); m._current_masters()
    mdoc = book_store.load_machines_doc()
    mid = next(i for i, x in mdoc["machines"].items() if "CNC" in x["type"].upper())
    from ppc_engine.loaders import loader as ppc_loader
    def boom(*a, **k):
        raise AssertionError("workbook opened")
    monkeypatch.setattr(ppc_loader, "open_workbook", boom)
    mdoc["machines"][mid]["type"] = "Manual deburring"
    book_store.save_machines_doc(mdoc)
    from ppc_engine.domain.resources import MachineKind
    assert new_engine._new_masters(False).machines[mid].kind == MachineKind.MANUAL


def _payload(machines, calendar):
    from engine import optimize_service as svc
    from engine.config import Config
    from engine import item_master as im
    _seed_book()
    return svc.build_payload(book_store.load_active_orders(), [], build_sample_bytes(),
                             Config(plan_start_date=date(2025, 3, 1)), seed=1,
                             item_master=im.seed_doc(build_sample_bytes(), "t"),
                             machines=machines, shop_calendar=calendar)


def test_payload_round_trips_the_shop_tables():
    from engine import optimize_service as svc
    mdoc = sm.seed_machines(build_sample_bytes(), "t")
    cdoc = sm.add_holiday(sm.seed_calendar(build_sample_bytes(), "t"), "2026-11-08", "Diwali", "t")
    payload = json.loads(json.dumps(_payload(mdoc, cdoc)))
    assert payload["machines"] == mdoc and payload["shop_calendar"] == cdoc
    parsed = svc.parse_payload(payload)
    assert len(parsed) == 8
    assert date(2026, 11, 8) in parsed[2].calendar.holidays


def test_payload_without_shop_tables_falls_back():
    from engine import optimize_service as svc
    payload = json.loads(json.dumps(_payload(None, None)))
    payload.pop("machines"); payload.pop("shop_calendar")
    parsed = svc.parse_payload(payload)
    assert parsed[2].machines                          # read from the workbook it carries


def test_run_candidate_feeds_the_shop_tables(monkeypatch):
    import pytest
    from engine import new_engine, optimize_service as svc
    seen = {}
    monkeypatch.setattr(new_engine, "set_shop_masters", lambda md, cd: seen.update(m=md, c=cd))
    monkeypatch.setattr(svc, "prepare_contest", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop")))
    mdoc = sm.seed_machines(build_sample_bytes(), "t")
    cdoc = sm.seed_calendar(build_sample_bytes(), "t")
    payload = json.loads(json.dumps(_payload(mdoc, cdoc)))
    payload["config"]["scheduler"] = "new"
    try:
        with pytest.raises(RuntimeError, match="stop"):
            svc.run_candidate(payload, 50)
    finally:
        new_engine.set_masters_bytes(None)
        new_engine.clear_item_master_override()
    assert seen == {"m": mdoc, "c": cdoc}


def test_api_payload_call_site_passes_the_shop_tables():
    import inspect
    src = inspect.getsource(_api())
    assert "machines=book_store.load_machines_doc()" in src
    assert "shop_calendar=book_store.load_shop_calendar()" in src


def test_upload_endpoint_is_gone():
    m = _api(); _seed_book()
    from fastapi.testclient import TestClient
    c = TestClient(m.app)
    c.post("/login", data={"username": "anvitech", "password": "1930rail"})
    r = c.post("/upload", files={"file": ("x.xlsx", build_sample_bytes())})
    assert r.status_code in (404, 405)
    assert book_store.load_masters_bytes() == build_sample_bytes()   # workbook on file kept


def test_no_upload_ui_left():
    from pathlib import Path
    web = Path(__file__).resolve().parents[1] / "web"
    html = (web / "index.html").read_text()
    js = (web / "app.js").read_text()
    assert 'id="upload-card"' not in html and 'id="upload-btn"' not in html
    assert 'fetch("/upload"' not in js
