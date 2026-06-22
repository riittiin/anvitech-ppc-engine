"""Rule 1 — consolidation, seeded with the SO Remarks oracle.

Item 61249291-01 has three lines: 21/03 + 28/03 (7 days apart -> clubbed) and
10/04 (outside the 10-day window -> separate).
"""
from engine.rules import rule1_consolidate


def _batches_for(loaded, item_code, config):
    so_lines, masters = loaded
    lines = [s for s in so_lines if s.item_code == item_code]
    return rule1_consolidate.run(lines, config=config, masters=masters)


def test_club_within_window_and_split_outside(loaded, config):
    batches = _batches_for(loaded, "61249291-01", config)
    assert len(batches) == 2

    clubbed = next(b for b in batches if len(b.source_so_refs) == 2)
    separate = next(b for b in batches if len(b.source_so_refs) == 1)

    # 21/03 (qty 5) + 28/03 (qty 10) -> qty 15, SO delivery date = earliest (21/03).
    assert clubbed.qty == 15
    assert clubbed.so_delivery_date.isoformat() == "2025-03-21"
    # 10/04 line stands alone.
    assert separate.qty == 10
    assert separate.so_delivery_date.isoformat() == "2025-04-10"


def test_window_is_configurable(loaded, config):
    config.consolidation_window_days = 30  # now 10/04 is within window of 21/03
    batches = _batches_for(loaded, "61249291-01", config)
    assert len(batches) == 1
    assert batches[0].qty == 25
