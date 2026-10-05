from datetime import date

import pytest

from engine import item_master as im
from engine.models import Actual, Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B

NOW = "2026-10-05T10:00:00"


def _doc():
    return im.seed_doc(build_sample_bytes(), NOW)


def test_seed_reads_every_item_in_sheet_order():
    doc = _doc()
    assert list(doc["items"]) == [ITEM_A, ITEM_B]
    a = doc["items"][ITEM_A]
    assert a["description"] == "SAMPLE RING A" and a["version"] == 1
    assert a["steps"][1] == {"name": "CNC OS", "cycle": 5, "allotted": None,
                             "suggested": "CNC1/CNC2"}
    assert doc["seed_digest"] == im.digest(doc)


def test_seed_keeps_raw_cell_values(monkeypatch):
    rows = [("X1", "D", [("CNC", "6.5", None, "CNC1", None), ("INSP", None, None, "MI1", None)])]
    monkeypatch.setattr(im, "_sheet_rows", lambda raw: rows)
    doc = im.seed_doc(b"ignored", NOW)
    assert doc["items"]["X1"]["steps"][0]["cycle"] == "6.5"
    assert doc["items"]["X1"]["steps"][1]["cycle"] is None


def test_seed_matches_loader_item_codes():
    import io
    from engine.loaders import load_all
    _so, m = load_all(io.BytesIO(build_sample_bytes()))
    assert list(_doc()["items"]) == list(m.routings)


def test_seed_returns_none_without_routing_sheet():
    import io, openpyxl
    wb = openpyxl.Workbook(); buf = io.BytesIO(); wb.save(buf)
    assert im.seed_doc(buf.getvalue(), NOW) is None


def test_routing_rows_round_trip_order_and_text():
    rows = im.routing_rows(_doc())
    assert [r[0] for r in rows] == [ITEM_A, ITEM_B]
    assert rows[0][2][1] == ("CNC OS", 5, None, "CNC1/CNC2", None)


def test_digest_ignores_bookkeeping_but_sees_content():
    doc = _doc()
    d0 = im.digest(doc)
    doc["items"][ITEM_A]["version"] = 9
    doc["items"][ITEM_A]["updated_at"] = "later"
    assert im.digest(doc) == d0
    doc["items"][ITEM_A]["steps"][0]["cycle"] = 4
    assert im.digest(doc) != d0
    assert im.digest(None) == "none"


def test_step_info_kinds():
    assert im.step_info({"name": "DISPATCH", "cycle": None, "allotted": "", "suggested": ""})["kind"] == "dispatch"
    assert im.step_info({"name": "BAND SAW", "cycle": 2880, "allotted": "OS", "suggested": ""})["kind"] == "outsourced"
    cnc = im.step_info({"name": "CNC FIRST", "cycle": 6, "allotted": "CNC4", "suggested": "CNC5"})
    assert cnc == {"kind": "machine", "machining": True, "machines": ["CNC4"]}
    assert im.step_info({"name": "DEBUR", "cycle": 1, "allotted": "MD1", "suggested": ""})["machining"] is False


MACHINES = {"CNC1", "CNC2", "CNC4", "MI1", "MD1", "BS1"}


def _item(steps, desc="D"):
    return {"description": desc, "steps": steps}


def test_validate_accepts_a_good_item():
    item = _item([{"name": "CNC", "cycle": 6, "allotted": "CNC4", "suggested": ""},
                  {"name": "DISPATCH", "cycle": None, "allotted": "", "suggested": ""}])
    assert im.validate_item("X1", item, MACHINES, None) == []


@pytest.mark.parametrize("item,needle", [
    (_item([]), "at least one step"),
    (_item([{"name": f"S{i}", "cycle": 1, "allotted": "MD1", "suggested": ""} for i in range(13)]), "12 steps"),
    (_item([{"name": " ", "cycle": 1, "allotted": "MD1", "suggested": ""}]), "needs a name"),
    (_item([{"name": "CNC", "cycle": 1, "allotted": "CNC4", "suggested": ""},
            {"name": "cnc ", "cycle": 1, "allotted": "CNC4", "suggested": ""}]), "twice"),
    (_item([{"name": "CNC", "cycle": -1, "allotted": "CNC4", "suggested": ""}]), "0 or more"),
    (_item([{"name": "CNC", "cycle": "abc", "allotted": "CNC4", "suggested": ""}]), "a number"),
    (_item([{"name": "CNC", "cycle": 0, "allotted": "CNC4", "suggested": ""}]), "more than 0"),
    (_item([{"name": "DEBUR", "cycle": 2, "allotted": "", "suggested": ""}]), "needs a machine"),
    (_item([{"name": "CNC", "cycle": 2, "allotted": "CNC99", "suggested": ""}]), "CNC99"),
])
def test_validate_refuses(item, needle):
    errs = im.validate_item("X1", item, MACHINES, None)
    assert errs and any(needle in e for e in errs), errs


def test_validate_refuses_blank_code():
    item = _item([{"name": "CNC", "cycle": 6, "allotted": "CNC4", "suggested": ""}])
    assert any("item code" in e for e in im.validate_item("  ", item, MACHINES, None))


def test_unknown_machine_already_on_item_may_stay():
    old = _item([{"name": "CNC", "cycle": 6, "allotted": "CNC99", "suggested": ""}])
    new = _item([{"name": "CNC", "cycle": 7, "allotted": "CNC99", "suggested": ""}])
    assert im.validate_item("X1", new, MACHINES, old) == []


def _book(produced_on="CNC OS", qty=120, completed=False):
    o = Order("SO113", ITEM_A, "A", 200, date(2026, 10, 30))
    o.completed = completed
    acts = [Actual(so_no="SO113", item_code=ITEM_A, entry_date=date(2026, 10, 1), process=produced_on,
                   qty_produced=qty, qty_rejected=0, operator="X")]
    return {o.key: o}, acts


OLD = [{"name": "BANDSAW", "cycle": 3, "allotted": None, "suggested": "BS1"},
       {"name": "CNC OS", "cycle": 5, "allotted": None, "suggested": "CNC1/CNC2"},
       {"name": "INSP", "cycle": 2, "allotted": None, "suggested": "MI1"}]


def test_renaming_a_punched_step_is_refused_with_reason():
    orders, acts = _book()
    new = [dict(OLD[0]), dict(OLD[1], name="CNC FIRST SIDE"), dict(OLD[2])]
    errs = im.punch_safety_errors(ITEM_A, OLD, new, orders.values(), acts)
    assert errs == ["CNC OS has 120 punched on SO113. Finish or complete that order "
                    "before renaming or removing this step."]


def test_removing_a_punched_step_is_refused():
    orders, acts = _book()
    assert im.punch_safety_errors(ITEM_A, OLD, [OLD[0], OLD[2]], orders.values(), acts)


def test_reordering_punched_steps_is_refused():
    orders, acts = _book()
    acts.append(Actual(so_no="SO113", item_code=ITEM_A, entry_date=date(2026, 10, 1), process="INSP",
                       qty_produced=50, qty_rejected=0, operator="X"))
    new = [OLD[0], OLD[2], OLD[1]]
    errs = im.punch_safety_errors(ITEM_A, OLD, new, orders.values(), acts)
    assert errs and "same order" in errs[0]


def test_safe_edits_are_allowed():
    # Rebased for the final fix wave (F2): the renamed step used to be BANDSAW, which
    # sits BEFORE the punched CNC OS. A new name ahead of a punched step now counts as
    # a new step there and is refused (the rule cannot tell a rename from a new step),
    # so the rename moved to INSP, after the last punched step.
    orders, acts = _book()
    new = [dict(OLD[0], cycle=4, suggested="BS2"), dict(OLD[1], cycle=9),
           dict(OLD[2], name="FINAL INSP"),
           {"name": "WASH", "cycle": 1, "allotted": "MW1", "suggested": ""}]
    assert im.punch_safety_errors(ITEM_A, OLD, new, orders.values(), acts) == []


WASH = {"name": "WASH", "cycle": 1, "allotted": "MW1", "suggested": ""}


def test_a_new_step_before_a_punched_step_is_refused():
    orders, acts = _book()
    new = [OLD[0], WASH, OLD[1], OLD[2]]
    errs = im.punch_safety_errors(ITEM_A, OLD, new, orders.values(), acts)
    assert errs == ["WASH cannot go before CNC OS: CNC OS has 120 punched on SO113. "
                    "Add new steps after it, or finish that order first."]


def test_moving_an_unpunched_step_in_front_of_a_punched_step_is_refused():
    orders, acts = _book()
    new = [OLD[0], OLD[2], OLD[1]]          # INSP (unpunched) moved ahead of CNC OS
    errs = im.punch_safety_errors(ITEM_A, OLD, new, orders.values(), acts)
    assert errs == ["INSP cannot go before CNC OS: CNC OS has 120 punched on SO113. "
                    "Add new steps after it, or finish that order first."]


def test_a_new_step_after_the_last_punched_step_is_allowed():
    orders, acts = _book()
    assert im.punch_safety_errors(ITEM_A, OLD, [OLD[0], OLD[1], WASH, OLD[2]],
                                  orders.values(), acts) == []


def test_removing_an_unpunched_step_ahead_is_allowed():
    orders, acts = _book()
    assert im.punch_safety_errors(ITEM_A, OLD, [OLD[1], OLD[2]],
                                  orders.values(), acts) == []


def test_a_completed_order_does_not_block_a_new_step_in_front():
    orders, acts = _book(completed=True)
    assert im.punch_safety_errors(ITEM_A, OLD, [WASH] + OLD,
                                  orders.values(), acts) == []


def test_completed_order_does_not_block():
    orders, acts = _book(completed=True)
    assert im.punch_safety_errors(ITEM_A, OLD, [OLD[0], OLD[2]], orders.values(), acts) == []


def test_usage_lists_orders_and_drafts():
    orders, _ = _book()
    drafts = [{"so_no": "SO900", "item_code": ITEM_A, "qty": 5}]
    assert im.usage(ITEM_A, orders.values(), drafts) == ["SO113", "SO900 (Add New Orders draft)"]
    assert im.usage(ITEM_B, orders.values(), drafts) == []


def test_apply_save_versions_and_conflicts():
    doc = _doc()
    item = {"description": "NEW", "steps": doc["items"][ITEM_A]["steps"]}
    out = im.apply_save(doc, ITEM_A, item, expected_version=1, now_iso=NOW, user="anvitech", create=False)
    assert out["items"][ITEM_A]["version"] == 2
    assert out["items"][ITEM_A]["description"] == "NEW"
    assert doc["items"][ITEM_A]["version"] == 1          # input never mutated
    with pytest.raises(im.VersionConflict):
        im.apply_save(out, ITEM_A, item, expected_version=1, now_iso=NOW, user="x", create=False)
    with pytest.raises(im.VersionConflict):
        im.apply_save(out, ITEM_A, item, expected_version=None, now_iso=NOW, user="x", create=True)
    new = im.apply_save(out, "Z9", item, expected_version=None, now_iso=NOW, user="x", create=True)
    assert list(new["items"])[-1] == "Z9"               # appended, never sorted


def _one(step):
    return _item([step])


def test_machine_cycle_is_capped_at_a_day_per_piece():
    ok = {"name": "CNC", "cycle": im.MAX_MACHINE_CYCLE_MIN, "allotted": "CNC4", "suggested": ""}
    assert im.validate_item("X1", _one(ok), MACHINES, None) == []
    errs = im.validate_item("X1", _one(dict(ok, cycle=60000)), MACHINES, None)
    assert errs == ["Step 1 (CNC): the cycle time is minutes per piece and can be at "
                    "most 1440 minutes (one day)."]


def test_outsourced_block_is_capped_at_60_days():
    ok = {"name": "HEAT TREAT OS", "cycle": im.MAX_OUTSOURCED_BLOCK_MIN,
          "allotted": "OS", "suggested": ""}
    assert im.validate_item("X1", _one(ok), MACHINES, None) == []
    errs = im.validate_item("X1", _one(dict(ok, cycle=90 * 1440)), MACHINES, None)
    assert errs == ["Step 1 (HEAT TREAT OS): an outsourced step can take at most "
                    "86400 minutes (60 days)."]


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_cycle_is_refused(bad):
    for step in ({"name": "CNC", "cycle": bad, "allotted": "CNC4", "suggested": ""},
                 {"name": "HEAT TREAT OS", "cycle": bad, "allotted": "OS", "suggested": ""}):
        errs = im.validate_item("X1", _one(step), MACHINES, None)
        assert errs == [f"Step 1 ({step['name']}): the cycle time must be a number."]
