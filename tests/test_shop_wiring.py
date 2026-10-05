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
