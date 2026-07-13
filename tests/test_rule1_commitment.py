from datetime import date
from engine.config import Config
from engine.models import SOLine
from engine.rules import rule1_consolidate


def test_same_item_different_lanes_are_not_merged():
    lines = [
        SOLine("SO1", "X", "X", 10, date(2026, 7, 20), commitment="committed",
               promised_date=date(2026, 7, 22)),
        SOLine("SO2", "X", "X", 10, date(2026, 7, 21), commitment="open"),
    ]
    batches = rule1_consolidate.run(lines, config=Config(), masters=None)
    assert len(batches) == 2                      # not merged across lanes
    lanes = sorted(b.commitment for b in batches)
    assert lanes == ["committed", "open"]


def test_committed_batch_carries_promised_date():
    lines = [SOLine("SO1", "X", "X", 10, date(2026, 7, 20), commitment="committed",
                    promised_date=date(2026, 7, 22))]
    b = rule1_consolidate.run(lines, config=Config(), masters=None)[0]
    assert b.commitment == "committed" and b.promised_date == date(2026, 7, 22)
