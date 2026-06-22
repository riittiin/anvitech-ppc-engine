"""Loader tests — expected counts and the known non-blocking data quirks."""
from engine.loaders import load_all, normalize_resource_id, parse_date
from datetime import date


def test_counts(loaded):
    so_lines, masters = loaded
    assert len(so_lines) == 8                      # 8 annotated SO lines
    assert len(masters.routings) == 85             # ~85 distinct item codes
    # Real machines from the master (non-provisional): CNC1-5, VMC1-2, BS1-2,
    # CL1, MM1, DR1, MD1, MP1, MPK1, MI1, MW1, MA1 = 18.
    real = [m for m in masters.machines.values() if not m.provisional]
    assert len(real) == 18


def test_pending_master_data_is_nonblocking(loaded):
    _, masters = loaded
    pending = [r for r in masters.report if r["kind"] == "PENDING_MASTER_DATA"]
    refs = {r["ref"] for r in pending}
    # CNC7 is referenced by a routing but not in the master -> provisional.
    assert "CNC7" in refs
    assert masters.machines["CNC7"].provisional is True


def test_no_routing_count_is_zero(loaded):
    # Every SO item code currently has a routing (docs say 0 NO_ROUTING cases).
    _, masters = loaded
    assert [r for r in masters.report if r["kind"] == "NO_ROUTING"] == []


def test_calendar(loaded):
    _, masters = loaded
    assert masters.calendar.weekly_off_weekday == 3          # Thursday
    assert date(2025, 1, 26) in masters.calendar.holidays    # Republic Day
    assert not masters.calendar.is_working_day(date(2025, 6, 19))  # a Thursday


def test_resource_normalization_matches_spaced_and_compact():
    # The master's 'CNC 4' and a routing's 'CNC4' must collapse onto one key.
    assert normalize_resource_id("CNC 4") == normalize_resource_id("CNC4") == "CNC4"
    assert normalize_resource_id("VMC 1") == "VMC1"


def test_parse_date_handles_string_and_datetime():
    from datetime import datetime
    assert parse_date("28/03/2025") == date(2025, 3, 28)
    assert parse_date(datetime(2025, 3, 28)) == date(2025, 3, 28)
