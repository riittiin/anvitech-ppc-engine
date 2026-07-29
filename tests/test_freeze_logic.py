from datetime import datetime
from engine import freeze
from engine.models import ScheduleEntry
from engine.loaders import normalize_process_name as _np


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


class _Line:
    def __init__(self, so_no, item_code, process_qty):
        self.so_no, self.item_code, self.process_qty = so_no, item_code, process_qty


class _Op:
    def __init__(self, seq, name):
        self.seq, self.name = seq, name


class _Routing:
    def __init__(self, ops):
        self.operations = ops


class _Masters:
    def __init__(self, routings):
        self.routings = routings


def test_compute_frozen_set_picks_partially_punched_steps():
    applied = [{"batch_id": "B1", "item_code": "IT-A", "process_seq": 2,
                "process_name": "CNC first side", "machine": "CNC1", "operator": "Alpha",
                "start": "2026-07-29T08:00:00", "end": "2026-07-29T10:00:00",
                "so_refs": ["SO1"]}]
    masters = _Masters({"IT-A": _Routing([_Op(1, "CNC prep"), _Op(2, "CNC first side")])})
    # Step seq 2 has remaining 40 (partially done) → frozen.
    lines = [_Line("SO1", "IT-A", {_np("CNC first side"): 40, _np("CNC prep"): 0})]
    good = {("SO1", "IT-A", _np("CNC first side")): 60,   # 60 done, 40 left → in progress
            ("SO1", "IT-A", _np("CNC prep")): 100}        # fully done → not frozen
    rows = freeze.compute_frozen_set(applied, lines, good, masters)
    assert len(rows) == 1
    r = rows[0]
    assert r["so_no"] == "SO1" and r["op_seq"] == 2 and r["machine"] == "CNC1"
    assert r["operator"] == "Alpha" and r["remaining_qty"] == 40
    assert r["prev_start"] == "2026-07-29T08:00:00"


def test_compute_frozen_set_skips_step_not_in_last_plan():
    masters = _Masters({"IT-A": _Routing([_Op(2, "CNC first side")])})
    lines = [_Line("SO1", "IT-A", {_np("CNC first side"): 40})]
    good = {("SO1", "IT-A", _np("CNC first side")): 60}
    assert freeze.compute_frozen_set([], lines, good, masters) == []  # no applied row
