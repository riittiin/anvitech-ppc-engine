"""Tests for engine/operator_master.py (pure) + its book_store persistence.

See docs/superpowers/specs/2026-07-18-operator-master-rotation-design.md.
"""
from datetime import date

from engine import book_store, operator_master as om


# --------------------------------------------------------------------------- #
# seed_rows_from_masters
# --------------------------------------------------------------------------- #
def test_seed_copies_name_machines_shift_and_defaults_unpinned(loaded):
    _, masters = loaded
    rows = om.seed_rows_from_masters(masters)

    assert len(rows) == len(masters.operators)
    for row, op in zip(rows, masters.operators):
        assert row["name"] == op.name
        assert row["machines_raw"] == op.preferred_machines_raw
        assert row["shift"] == op.shift
        assert row["pinned"] is False
        assert row["id"]  # non-empty


def test_seed_gives_each_row_a_distinct_id(loaded):
    _, masters = loaded
    rows = om.seed_rows_from_masters(masters)
    ids = [r["id"] for r in rows]
    assert len(ids) == len(set(ids))


# --------------------------------------------------------------------------- #
# last_friday / next_rotation
# --------------------------------------------------------------------------- #
def test_last_friday_on_a_friday_is_itself():
    fri = date(2026, 7, 17)
    assert fri.weekday() == 4
    assert om.last_friday(fri) == fri


def test_last_friday_mid_week():
    # Wed 2026-07-15 -> Fri 2026-07-10
    assert om.last_friday(date(2026, 7, 15)) == date(2026, 7, 10)


def test_next_rotation_from_a_friday_is_next_week():
    fri = date(2026, 7, 17)
    assert om.next_rotation(fri) == date(2026, 7, 24)


def test_next_rotation_mid_week():
    assert om.next_rotation(date(2026, 7, 15)) == date(2026, 7, 17)


# --------------------------------------------------------------------------- #
# rotate_table
# --------------------------------------------------------------------------- #
def _row(name, shift, pinned=False, machines_raw="CNC 1"):
    return {"id": name, "name": name, "machines_raw": machines_raw,
            "shift": shift, "pinned": pinned}


def test_rotate_flips_unpinned_two_shift_operators_across_one_friday():
    table = {"week_anchor": "2026-07-10",
             "operators": [_row("A", "First shift"), _row("B", "Second shift")]}
    new_table, flips = om.rotate_table(table, date(2026, 7, 15))  # 1 Friday (07-10 excl -> 07-17? )
    # Anchor 07-10 (a Friday) is exclusive, so the next Friday after it is
    # 07-17, which is AFTER 07-15 -> zero Fridays counted yet.
    assert flips == 0
    assert new_table is table

    new_table, flips = om.rotate_table(table, date(2026, 7, 17))
    assert flips == 1
    by_name = {r["name"]: r for r in new_table["operators"]}
    assert by_name["A"]["shift"] == "Second shift"
    assert by_name["B"]["shift"] == "First shift"
    assert new_table["week_anchor"] == "2026-07-17"


def test_rotate_never_flips_a_pinned_operator():
    table = {"week_anchor": "2026-07-10",
             "operators": [_row("Pinned", "First shift", pinned=True)]}
    new_table, flips = om.rotate_table(table, date(2026, 7, 17))
    assert flips == 1
    assert new_table["operators"][0]["shift"] == "First shift"


def test_rotate_never_flips_a_blank_shift_operator():
    table = {"week_anchor": "2026-07-10", "operators": [_row("Manual", "")]}
    new_table, flips = om.rotate_table(table, date(2026, 7, 17))
    assert flips == 1
    assert new_table["operators"][0]["shift"] == ""


def test_rotate_catch_up_two_fridays_nets_no_change_for_unpinned():
    table = {"week_anchor": "2026-07-03",
             "operators": [_row("A", "First shift")]}
    # Fridays after 07-03 up to 07-24: 07-10 and 07-17 (2 Fridays elapsed if
    # today is right after the second one, before a third).
    new_table, flips = om.rotate_table(table, date(2026, 7, 20))
    assert flips == 2
    assert new_table["operators"][0]["shift"] == "First shift"  # net no-op
    assert new_table["week_anchor"] == "2026-07-17"


def test_rotate_idempotent_same_day():
    table = {"week_anchor": "2026-07-10", "operators": [_row("A", "First shift")]}
    once, flips1 = om.rotate_table(table, date(2026, 7, 17))
    assert flips1 == 1
    twice, flips2 = om.rotate_table(once, date(2026, 7, 17))
    assert flips2 == 0
    assert twice is once
    assert twice["operators"][0]["shift"] == "Second shift"


def test_rotate_anchor_advances_to_last_counted_friday():
    table = {"week_anchor": "2026-06-01", "operators": []}
    new_table, flips = om.rotate_table(table, date(2026, 7, 15))
    assert new_table["week_anchor"] == "2026-07-10"
    assert flips > 0


def test_rotate_missing_anchor_treated_as_last_friday_no_flip_on_first_call():
    table = {"operators": [_row("A", "First shift")]}
    new_table, flips = om.rotate_table(table, date(2026, 7, 15))
    assert flips == 0
    assert new_table is table


def test_rotate_blank_anchor_treated_as_last_friday_no_flip_on_first_call():
    table = {"week_anchor": "", "operators": [_row("A", "First shift")]}
    new_table, flips = om.rotate_table(table, date(2026, 7, 15))
    assert flips == 0
    assert new_table is table


# --------------------------------------------------------------------------- #
# to_operators
# --------------------------------------------------------------------------- #
def test_to_operators_matches_excel_loaded_operator_field_by_field(loaded):
    _, masters = loaded
    rows = om.seed_rows_from_masters(masters)
    converted = om.to_operators(rows)

    assert len(converted) == len(masters.operators)
    for got, want in zip(converted, masters.operators):
        assert got.name == want.name
        assert got.preferred_machines_raw == want.preferred_machines_raw
        assert got.machines == want.machines
        assert got.shift == want.shift


# --------------------------------------------------------------------------- #
# book_store round-trip
# --------------------------------------------------------------------------- #
def test_operator_table_store_round_trip():
    assert book_store.load_operator_table() is None
    table = {"week_anchor": "2026-07-10",
             "operators": [_row("A", "First shift")]}
    book_store.save_operator_table(table)
    assert book_store.load_operator_table() == table


def test_operator_table_store_overwrite():
    book_store.save_operator_table({"week_anchor": "2026-07-10", "operators": []})
    table2 = {"week_anchor": "2026-07-17", "operators": [_row("A", "Second shift")]}
    book_store.save_operator_table(table2)
    assert book_store.load_operator_table() == table2
