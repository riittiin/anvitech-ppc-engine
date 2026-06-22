"""Rule 3 — tiebreak by total process time, seeded with the sheet's own example.

The SO Remarks document three facts the metric must satisfy:
  * 61240807-01 has the HIGHEST total process time,
  * 61247047-01 has the LOWEST,
  * 61241949-01 has more process time than 61247047-01.
Only summing per-process CYCLE times reproduces all three.
"""
from datetime import date

from engine.config import Config
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters
from engine.rules import rule2_sort_by_date, rule3_tiebreak_process_time


def _batch_from_routing(masters, item_code, d):
    return Batch(batch_id=item_code, item_code=item_code, item_name=item_code,
                 qty=10, so_delivery_date=d, source_so_refs=[item_code])


def test_higher_process_time_wins_same_date(loaded, config):
    _, masters = loaded
    d = date(2025, 3, 7)
    # Same delivery date; 61241949-01 (more cycle time) must beat 61247047-01 (lowest).
    batches = [
        _batch_from_routing(masters, "61247047-01", d),
        _batch_from_routing(masters, "61241949-01", d),
    ]
    ordered = rule2_sort_by_date.run(batches, config=config, masters=masters)
    ordered = rule3_tiebreak_process_time.run(ordered, config=config, masters=masters)
    assert [b.item_code for b in ordered] == ["61241949-01", "61247047-01"]


def test_61240807_has_highest_total_process_time(loaded):
    _, masters = loaded
    tpt = {c: masters.routings[c].total_cycle_time()
           for c in ["61240807-01", "61249291-01", "61247047-01", "61241949-01"]}
    assert tpt["61240807-01"] == max(tpt.values())
    assert tpt["61247047-01"] == min(tpt.values())


def _slack_masters():
    masters = Masters(
        machines={"M": Machine(machine_no="M", display_name="M", machine_type="t")},
        calendar=WorkCalendar(),
    )
    # 20 min/pc, single process -> a 100-pc batch needs far more work than a 1-pc one.
    masters.routings["X"] = Routing(
        item_code="X", description="", customer="", rm_type="", moq=None,
        processes=[Process(1, "op", cycle_time=20, total_time=None,
                           suggested_machine="M", allotted_machine=None)],
    )
    return masters


def test_heavy_later_order_beats_trivial_earlier_order():
    """The scenario that broke the naive rule: 100 pcs due one day LATER must
    outrank 1 pc due earlier, because its slack (lateness risk) is far lower."""
    masters = _slack_masters()
    cfg = Config(plan_start_date=date(2025, 6, 2))  # metric=slack, no window (defaults)
    big = Batch(batch_id="BIG", item_code="X", item_name="X", qty=100,
                so_delivery_date=date(2025, 6, 18), source_so_refs=["BIG"])
    small = Batch(batch_id="SMALL", item_code="X", item_name="X", qty=1,
                  so_delivery_date=date(2025, 6, 17), source_so_refs=["SMALL"])

    ordered = rule3_tiebreak_process_time.run([small, big], config=cfg, masters=masters)
    assert [b.batch_id for b in ordered] == ["BIG", "SMALL"]


def test_legacy_window_zero_keeps_strict_date_order():
    """With window=0, a later-due heavy order does NOT jump an earlier one."""
    masters = _slack_masters()
    cfg = Config(plan_start_date=date(2025, 6, 2), priority_window_days=0)
    big = Batch(batch_id="BIG", item_code="X", item_name="X", qty=100,
                so_delivery_date=date(2025, 6, 18), source_so_refs=["BIG"])
    small = Batch(batch_id="SMALL", item_code="X", item_name="X", qty=1,
                  so_delivery_date=date(2025, 6, 17), source_so_refs=["SMALL"])

    ordered = rule3_tiebreak_process_time.run([small, big], config=cfg, masters=masters)
    assert [b.batch_id for b in ordered] == ["SMALL", "BIG"]  # earlier date stays first
