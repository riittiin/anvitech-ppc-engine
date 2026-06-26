"""Loader tests — expected counts and the known non-blocking data quirks."""
from engine.loaders import (
    load_all, normalize_resource_id, parse_date, parse_resource_candidates,
)
from datetime import date


def test_counts(loaded):
    so_lines, masters = loaded
    assert len(so_lines) == 3                       # 3 schedulable SO lines
    assert len(masters.routings) == 2               # 2 item codes (SAMP-A/B)
    # Real (non-provisional) machines in the sample master: CNC1, CNC2, VMC1,
    # BS1, MI1, MW1 = 6.
    real = [m for m in masters.machines.values() if not m.provisional]
    assert len(real) == 6


def test_pending_master_data_is_nonblocking(loaded):
    _, masters = loaded
    pending = [r for r in masters.report if r["kind"] == "PENDING_MASTER_DATA"]
    refs = {r["ref"] for r in pending}
    # CNC9 is referenced by a routing but not in the master -> provisional.
    assert "CNC9" in refs
    assert masters.machines["CNC9"].provisional is True


def test_no_routing_count_is_zero(loaded):
    # Every SO item code in the sample has a routing -> no NO_ROUTING.
    _, masters = loaded
    assert [r for r in masters.report if r["kind"] == "NO_ROUTING"] == []


def test_calendar(loaded):
    _, masters = loaded
    assert masters.calendar.weekly_off_weekday == 3          # Thursday
    assert date(2025, 1, 1) in masters.calendar.holidays     # New Year holiday
    assert not masters.calendar.is_working_day(date(2025, 3, 13))  # a Thursday


def test_resource_normalization_matches_spaced_and_compact():
    # The master's 'CNC 4' and a routing's 'CNC4' must collapse onto one key.
    assert normalize_resource_id("CNC 4") == normalize_resource_id("CNC4") == "CNC4"
    assert normalize_resource_id("VMC 1") == "VMC1"


def test_parse_date_handles_string_and_datetime():
    from datetime import datetime
    assert parse_date("28/03/2025") == date(2025, 3, 28)
    assert parse_date(datetime(2025, 3, 28)) == date(2025, 3, 28)


# --------------------------------------------------------------------------- #
# Alternative ("preferred") machines: a cell like "CNC3/CNC6" = either machine
# --------------------------------------------------------------------------- #
def test_parse_resource_candidates_splits_alternatives():
    assert parse_resource_candidates("CNC3/CNC6") == ["CNC3", "CNC6"]
    assert parse_resource_candidates("CNC 3 / CNC 6") == ["CNC3", "CNC6"]   # spaces
    assert parse_resource_candidates("CNC6/CNC3") == ["CNC6", "CNC3"]       # order kept
    assert parse_resource_candidates("CNC3 or CNC6") == ["CNC3", "CNC6"]    # 'or'


def test_parse_resource_candidates_single_and_empty():
    assert parse_resource_candidates("VMC2") == ["VMC2"]
    assert parse_resource_candidates("") == []
    assert parse_resource_candidates(None) == []
    assert parse_resource_candidates("   ") == []
    assert parse_resource_candidates("CNC6/CNC6") == ["CNC6"]               # dedupe


def test_alternative_cells_register_each_machine_not_a_merged_id(loaded):
    _, masters = loaded
    # The sample's "CNC1/CNC2" alternative cell must register each machine
    # separately, never a merged id.
    for bogus in ("CNC1CNC2", "CNC2CNC1"):
        assert bogus not in masters.machines
    assert "CNC1" in masters.machines and not masters.machines["CNC1"].provisional
    assert "CNC2" in masters.machines and not masters.machines["CNC2"].provisional
