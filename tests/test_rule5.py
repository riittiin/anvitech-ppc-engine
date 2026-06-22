"""Rule 5 — overlap mode: sequential vs 50% overlap."""
from engine.config import Config, OVERLAP_SEQUENTIAL, OVERLAP_PERCENT
from engine.rules import rule5_overlap_mode


def test_sequential_waits_for_full_previous():
    cfg = Config(overlap_mode=OVERLAP_SEQUENTIAL)
    assert rule5_overlap_mode.elapsed_before_next(200, cfg) == 200


def test_overlap_half():
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=50)
    assert rule5_overlap_mode.elapsed_before_next(200, cfg) == 100


def test_overlap_custom_percent():
    cfg = Config(overlap_mode=OVERLAP_PERCENT, overlap_percent=25)
    assert rule5_overlap_mode.elapsed_before_next(200, cfg) == 50
