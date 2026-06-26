"""Rule 6 — parallel split of an alternative-machine step (split_parallel)."""
from datetime import date

from engine.config import Config
from engine.models import Batch, Process, Routing, Machine, WorkCalendar, Masters
from engine.rules import rule6_allocate


def _masters(suggested="M/N", n_machines=2):
    machines = {"M": Machine("M", "M", "t"), "N": Machine("N", "N", "t")}
    masters = Masters(machines=machines, calendar=WorkCalendar())
    masters.routings["X"] = Routing(
        item_code="X", description="", customer="", rm_type="", moq=None,
        processes=[Process(1, "OP", cycle_time=10, total_time=None,
                           suggested_machine=suggested, allotted_machine=None)],
    )
    return masters


def _batch(qty):
    return Batch(batch_id="B1", item_code="X", item_name="X", qty=qty,
                 so_delivery_date=date(2025, 3, 7), source_so_refs=["SO"])


def _cfg(**kw):
    return Config(plan_start_date=date(2025, 3, 5), **kw)


def test_splits_qty_across_free_alternatives_in_parallel():
    masters = _masters("M/N")
    sched = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=True), masters=masters)
    assert len(sched) == 2                                   # one entry per machine
    assert {e.machine for e in sched} == {"M", "N"}
    assert sorted(e.qty for e in sched) == [25, 25]          # 50 split evenly
    assert {e.start for e in sched} == {sched[0].start}      # both start together (parallel)
    # Each half: 25*10 + 90 = 340 min; single machine would be 50*10 + 90 = 590.
    assert all(e.occupancy_min == 340 for e in sched)


def test_odd_quantity_splits_with_remainder():
    sched = rule6_allocate.run([_batch(51)], config=_cfg(split_parallel=True), masters=_masters("M/N"))
    assert sorted(e.qty for e in sched) == [25, 26]


def test_off_by_default_runs_on_one_machine():
    sched = rule6_allocate.run([_batch(50)], config=_cfg(), masters=_masters("M/N"))
    assert len(sched) == 1 and sched[0].qty == 50            # no split when flag off


def test_no_split_when_only_one_machine_listed():
    sched = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=True), masters=_masters("M"))
    assert len(sched) == 1 and sched[0].machine == "M"


def test_no_split_below_min_qty():
    cfg = _cfg(split_parallel=True, split_min_qty=10)
    sched = rule6_allocate.run([_batch(8)], config=cfg, masters=_masters("M/N"))
    assert len(sched) == 1                                   # 8 < 10 → not split


def test_no_split_when_second_machine_busy():
    # Seed N as busy well past the op's start → only M is free → no split.
    masters = _masters("M/N")
    sched = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=True),
                               masters=masters, machine_lost_min={"N": 600})
    assert len(sched) == 1 and sched[0].machine == "M"


def test_split_finishes_sooner_than_single():
    m = _masters("M/N")
    single = rule6_allocate.run([_batch(50)], config=_cfg(), masters=m)
    split = rule6_allocate.run([_batch(50)], config=_cfg(split_parallel=True), masters=_masters("M/N"))
    single_end = max(e.end for e in single)
    split_end = max(e.end for e in split)
    assert split_end < single_end
