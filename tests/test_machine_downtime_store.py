"""Machine maintenance breaks: durable storage + the reservation intervals they
become. Exact mirror of the operator-absence pair (book_store.save_absence /
optimize_service.absence_reservations)."""
from datetime import datetime

from engine import book_store, optimize_service as svc


def test_save_assigns_an_id_and_round_trips():
    saved = book_store.save_machine_downtime(
        {"machine": "CNC3", "from_date": "2026-09-05", "to_date": "2026-09-07",
         "reason": "Spindle service"})
    assert saved["id"]
    assert saved["machine"] == "CNC3"
    rows = book_store.load_machine_downtime()
    assert rows == [saved]


def test_empty_store_is_an_empty_list():
    assert book_store.load_machine_downtime() == []


def test_delete_removes_one_row_and_reports_unknown_ids():
    a = book_store.save_machine_downtime(
        {"machine": "CNC3", "from_date": "2026-09-05", "to_date": "2026-09-05"})
    b = book_store.save_machine_downtime(
        {"machine": "VMC1", "from_date": "2026-09-08", "to_date": "2026-09-09"})
    assert book_store.delete_machine_downtime(a["id"]) is True
    assert [r["machine"] for r in book_store.load_machine_downtime()] == ["VMC1"]
    assert book_store.delete_machine_downtime("nope") is False
    assert len(book_store.load_machine_downtime()) == 1
    assert b["reason"] == ""          # optional field defaults, never missing


def test_reservations_cover_00_00_to_00_00_of_the_day_after():
    res = svc.downtime_reservations(
        [{"machine": "CNC3", "from_date": "2026-09-05", "to_date": "2026-09-06"}])
    assert set(res) == {"CNC3"}
    assert res["CNC3"] == [(datetime(2026, 9, 5, 0, 0), datetime(2026, 9, 7, 0, 0))]


def test_reservations_swap_a_reversed_range():
    res = svc.downtime_reservations(
        [{"machine": "CNC3", "from_date": "2026-09-06", "to_date": "2026-09-05"}])
    assert res["CNC3"] == [(datetime(2026, 9, 5, 0, 0), datetime(2026, 9, 7, 0, 0))]


def test_reservations_skip_malformed_and_blank_rows():
    res = svc.downtime_reservations([
        {"machine": "CNC3", "from_date": "not-a-date", "to_date": "2026-09-06"},
        {"machine": "", "from_date": "2026-09-05", "to_date": "2026-09-06"},
        {"from_date": "2026-09-05", "to_date": "2026-09-06"},
        {"machine": "VMC1", "from_date": "2026-09-05", "to_date": "2026-09-05"},
    ])
    assert set(res) == {"VMC1"}


def test_two_breaks_on_one_machine_both_reserve():
    res = svc.downtime_reservations([
        {"machine": "CNC3", "from_date": "2026-09-05", "to_date": "2026-09-05"},
        {"machine": "CNC3", "from_date": "2026-09-20", "to_date": "2026-09-20"},
    ])
    assert len(res["CNC3"]) == 2


def test_none_and_empty_are_an_empty_dict():
    assert svc.downtime_reservations(None) == {}
    assert svc.downtime_reservations([]) == {}
