"""Rule 7 — parallel machine trigger for large batches."""
from engine.config import Config
from engine.rules import rule7_parallel_machine


def test_trigger_fires_above_threshold():
    cfg = Config(parallel_trigger_qty=400)
    assert rule7_parallel_machine.should_parallelize(401, cfg) is True
    assert rule7_parallel_machine.should_parallelize(400, cfg) is False
    assert rule7_parallel_machine.should_parallelize(10, cfg) is False


def test_picks_free_alternate_machine():
    # CNC4 is busy until later; CNC2 is free -> pick CNC2 for the parallel setup.
    machine_free = {"CNC4": 1000, "CNC2": 0}
    chosen = rule7_parallel_machine.pick_parallel_machine(
        "CNC4", ["CNC4", "CNC2", "CNC5"], machine_free
    )
    assert chosen != "CNC4"
