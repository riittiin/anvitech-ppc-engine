from datetime import date

from engine.config import Config
from engine.models import Batch, Masters, Routing
from engine.rules import rule3_tiebreak_process_time as r3


def _b(bid, promised, item="X"):
    return Batch(batch_id=bid, item_code=item, item_name=item, qty=10,
                 so_delivery_date=date(2026, 7, 20), commitment="committed",
                 promised_date=promised)


def _masters_for(batches):
    m = Masters()
    for b in batches:
        m.routings[b.item_code] = Routing(b.item_code, "", "", "", None, processes=[])
    return m


def test_protected_batches_sort_by_promised_date():
    # Batch-id order (A < Z) is OPPOSITE to promised-date order (Z earlier than A).
    # Feed batches in non-promise order. Rule 3 must reorder by promised_date.
    # Without the fix, batch_id tiebreak would give ["A", "Z"] (wrong).
    batches = [_b("A", date(2026, 7, 28)), _b("Z", date(2026, 7, 22))]
    out = r3.run(batches, config=Config(), masters=_masters_for(batches))
    assert [b.batch_id for b in out] == ["Z", "A"]
