# tests/test_shop_masters.py
from datetime import date

import pytest

from engine import book_store, shop_masters as sm
from tests.sample_workbook import build_sample_bytes

NOW = "2026-10-05T10:00:00"
TODAY = date(2026, 10, 5)


def test_seed_machines_canonical_keys_in_sheet_order():
    doc = sm.seed_machines(build_sample_bytes(), NOW)
    assert list(doc["machines"])                    # non-empty, insertion order kept
    for mid, m in doc["machines"].items():
        assert mid == mid.upper() and " " not in mid
        assert set(m) >= {"name", "type", "hours", "version"}
    assert doc["seed_digest"] == sm.machines_digest(doc)


def test_seed_canonical_keys(monkeypatch):
    monkeypatch.setattr(sm, "_machine_sheet_rows", lambda raw: [("CNC 4", "CNC lathe", 19.5)])
    doc = sm.seed_machines(b"x", NOW)
    assert list(doc["machines"]) == ["CNC4"]
    assert doc["machines"]["CNC4"]["name"] == "CNC 4"


def test_seed_keeps_blank_hours(monkeypatch):
    monkeypatch.setattr(sm, "_machine_sheet_rows", lambda raw: [("MD1", "Manual deburring", None)])
    doc = sm.seed_machines(b"x", NOW)
    assert doc["machines"]["MD1"]["hours"] is None
    assert sm.machine_rows(doc) == [("MD1", "Manual deburring", None)]


def test_seed_calendar_holidays_only(monkeypatch):
    monkeypatch.setattr(sm, "_holiday_sheet_rows",
                        lambda raw: [(date(2026, 8, 15), "Independence Day")])
    doc = sm.seed_calendar(b"x", NOW)
    assert doc["holidays"] == [{"date": "2026-08-15", "name": "Independence Day"}]
    assert sm.holiday_rows(doc) == [(date(2026, 8, 15), "Independence Day")]
    assert doc["seed_digest"] == sm.calendar_digest(doc)


def test_seed_returns_none_without_the_sheet():
    import io, openpyxl
    wb = openpyxl.Workbook(); buf = io.BytesIO(); wb.save(buf)
    assert sm.seed_machines(buf.getvalue(), NOW) is None
    assert sm.seed_calendar(buf.getvalue(), NOW) is None


def test_digests_ignore_bookkeeping():
    doc = {"machines": {"CNC1": {"name": "CNC1", "type": "CNC lathe", "hours": 19.5, "version": 1}}}
    d0 = sm.machines_digest(doc)
    doc["machines"]["CNC1"]["version"] = 7
    assert sm.machines_digest(doc) == d0
    doc["machines"]["CNC1"]["hours"] = 9.5
    assert sm.machines_digest(doc) != d0
    assert sm.machines_digest(None) == "none" and sm.calendar_digest(None) == "none"


EXISTING = {"CNC1": {"name": "CNC1", "type": "CNC lathe", "hours": 19.5, "version": 1}}


@pytest.mark.parametrize("mid,item,create,needle", [
    ("", {"type": "CNC lathe", "hours": 19.5}, True, "machine number"),
    ("CNC1", {"type": "CNC lathe", "hours": 19.5}, True, "already"),
    ("CNC8", {"type": "", "hours": 19.5}, True, "type"),
    ("CNC8", {"type": "CNC lathe", "hours": 0}, True, "hours"),
    ("CNC8", {"type": "CNC lathe", "hours": 25}, True, "hours"),
    ("CNC8", {"type": "CNC lathe", "hours": float("nan")}, True, "hours"),
    ("OS", {"type": "Outsourced", "hours": 9.5}, True, "OS"),
    ("CNC8/CNC9", {"type": "CNC lathe", "hours": 19.5}, True, "one machine"),
])
def test_validate_machine_refuses(mid, item, create, needle):
    errs = sm.validate_machine(mid, item, EXISTING, create)
    assert errs and any(needle in e for e in errs), errs


def test_validate_machine_accepts():
    assert sm.validate_machine("CNC 8", {"type": "CNC lathe", "hours": 19.5}, EXISTING, True) == []
    assert sm.validate_machine("CNC1", {"type": "CNC lathe", "hours": 9.5}, EXISTING, False) == []


def test_validate_holiday():
    existing = [{"date": "2026-11-08", "name": "Diwali"}]
    assert sm.validate_holiday("2026-12-25", "Christmas", existing, TODAY) == []
    assert sm.validate_holiday("2026-11-08", "Again", existing, TODAY)            # duplicate
    assert sm.validate_holiday("not-a-date", "X", existing, TODAY)
    assert sm.validate_holiday("2040-01-01", "Far", existing, TODAY)               # > 5 years
    assert sm.validate_holiday("2026-12-25", "  ", existing, TODAY)                # blank name


def test_machine_usage_names_every_user():
    item_doc = {"items": {"ITEM1": {"steps": [{"name": "CNC FIRST", "allotted": "CNC1/CNC2", "suggested": ""}]}}}
    operators = {"operators": [{"name": "Ravi", "machines_raw": "CNC1/VMC1"}]}
    downtime = [{"machine": "CNC1", "from_date": "2026-10-10", "to_date": "2026-10-11"}]
    frozen = [{"so_no": "SO5", "item_code": "ITEM1", "process": "CNC FIRST", "machine": "CNC1"}]
    used = sm.machine_usage("CNC1", item_doc, operators, downtime, frozen)
    assert any("ITEM1" in u for u in used)
    assert any("Ravi" in u for u in used)
    assert any("maintenance" in u for u in used)
    assert any("SO5" in u for u in used)
    assert sm.machine_usage("MD9", item_doc, operators, downtime, frozen) == []


def test_apply_machine_save_versions():
    doc = {"machines": dict(EXISTING)}
    out = sm.apply_machine_save(doc, "CNC1", {"name": "CNC1", "type": "CNC lathe", "hours": 9.5},
                                1, NOW, "anvitech", create=False)
    assert out["machines"]["CNC1"]["hours"] == 9.5 and out["machines"]["CNC1"]["version"] == 2
    assert doc["machines"]["CNC1"]["version"] == 1
    with pytest.raises(sm.VersionConflict):
        sm.apply_machine_save(out, "CNC1", {"name": "CNC1", "type": "x", "hours": 9.5}, 1, NOW, "a", False)
    new = sm.apply_machine_save(out, "CNC8", {"name": "CNC 8", "type": "CNC lathe", "hours": 19.5},
                                None, NOW, "a", create=True)
    assert list(new["machines"])[-1] == "CNC8"


def test_holiday_add_remove_keeps_date_order():
    doc = {"holidays": [{"date": "2026-11-08", "name": "Diwali"}]}
    doc = sm.add_holiday(doc, "2026-08-15", "Independence Day", NOW)
    assert [h["date"] for h in doc["holidays"]] == ["2026-08-15", "2026-11-08"]
    doc = sm.remove_holiday(doc, "2026-11-08")
    assert [h["date"] for h in doc["holidays"]] == ["2026-08-15"]
    with pytest.raises(KeyError):
        sm.remove_holiday(doc, "2026-01-01")


def test_store_round_trip():
    assert book_store.load_machines_doc() is None and book_store.load_shop_calendar() is None
    book_store.save_machines_doc({"machines": {}})
    book_store.save_shop_calendar({"holidays": []})
    assert book_store.load_machines_doc() == {"machines": {}}
    assert book_store.load_shop_calendar() == {"holidays": []}
