from datetime import datetime
from engine import freeze
from engine.models import ScheduleEntry


def test_schedule_projection_keeps_machine_ops_only():
    entries = [
        ScheduleEntry(batch_id="B1", item_code="IT-A", process_seq=2,
                      process_name="CNC first side", machine="CNC1", qty=100,
                      occupancy_min=120, start=datetime(2026,7,29,8,0),
                      end=datetime(2026,7,29,10,0), operator="Alpha", so_refs=["SO1"]),
        ScheduleEntry(batch_id="B1", item_code="IT-A", process_seq=9,
                      process_name="DISPATCH", machine="OS / Outsourced", qty=100,
                      occupancy_min=0, start=datetime(2026,7,29,10,0),
                      end=datetime(2026,7,29,10,0), operator="", so_refs=["SO1"]),
    ]
    rows = freeze.schedule_projection(entries)
    assert len(rows) == 1
    r = rows[0]
    assert r["machine"] == "CNC1" and r["operator"] == "Alpha"
    assert r["process_seq"] == 2 and r["so_refs"] == ["SO1"]
    assert r["start"] == "2026-07-29T08:00:00"
