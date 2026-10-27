"""Add New Orders, "ask for an earlier date": the date the director types is the date
he gets, whenever the shop can make it at all (2026-10-27, director's complaint).

He typed 25 Oct, pressed No, and the search handed back 26 or 27 Oct: the new order
was one of ~60 orders weighed against each other, so the search traded its two days
for other orders' days. The typed date is now a MUST-FINISH date
(``Order.must_finish_by`` -> ``SOLine`` -> ``Batch`` -> ppc ``Order``), weighed above
everything else in both scorers, and the search gets a seed with those orders first.
When even that cannot make the date, the result names the earliest date the plan can
give (``target_misses``), and the typed date is still the one saved.
"""
from __future__ import annotations

import io
from datetime import date, datetime, timedelta

import pytest

from engine import book_store, loaders, new_engine, optimizer, pipeline
from engine.config import Config
from engine.models import Order, PlanRun, SOLine
from engine.rules import rule1_consolidate
from ppc_engine.config import PlanConfig
from ppc_engine.objective.metrics import PlanMetrics
from ppc_engine.objective.objective import score as ppc_score
from tests.new_sample_workbook import ITEM_A, build_new_sample_bytes

START = date(2025, 3, 3)
CONF = Config(scheduler="new", plan_start_date=START, apply_operator_logic=True,
              consolidation_window_days=0)


@pytest.fixture()
def masters():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    return loaders.load_all(io.BytesIO(wb))[1]


def _book(new_due, must_finish):
    """Ten existing orders of the same item (no clubbing: window 0), two due each day
    from 04-03, competing for the one VMC, plus the new order NEW."""
    lines = [SOLine(so_no=f"EX{i}", item_code=ITEM_A, item_name="a", qty=50,
                    delivery_date=START + timedelta(days=1 + i // 2)) for i in range(10)]
    lines.append(SOLine(so_no="NEW", item_code=ITEM_A, item_name="a", qty=50,
                        delivery_date=new_due,
                        must_finish_by=new_due if must_finish else None))
    return lines


def _search(lines, masters):
    res = new_engine.optimize_sequence(lines, CONF, masters, budget_evals=80, seed=1)
    pr = PlanRun(so_lines=lines)
    pipeline.run_forward(pr, CONF, masters, priority_rank=res.ranks)
    return res, optimizer.expected_completion(pr.schedule)[("NEW", ITEM_A)]


def test_the_date_asked_for_is_met_when_the_shop_can_make_it(masters):
    asked = date(2025, 3, 5)
    # The complaint, reproduced: as one order among many, the search gives it up.
    _res, got_before = _search(_book(asked, must_finish=False), masters)
    assert got_before > asked, "fixture no longer reproduces the complaint"
    # As a must-finish date, it is met, at the other orders' expense.
    res, got = _search(_book(asked, must_finish=True), masters)
    assert got <= asked
    assert res.best["target_breach"] == 0
    assert res.best["target_misses"] == []


def test_an_impossible_date_names_the_earliest_date_the_plan_gives(masters):
    asked = date(2025, 3, 4)
    res, got = _search(_book(asked, must_finish=True), masters)
    assert got > asked                       # cannot be made at all on this book
    [miss] = res.best["target_misses"]
    assert (miss["so_no"], miss["target"], miss["expected"]) == (
        "NEW", asked.isoformat(), got.isoformat())
    assert miss["days"] == (got - asked).days


def test_a_met_date_beats_any_plan_that_misses_it_in_both_scorers():
    """Dominance, not a preference: a whole book of misses (far beyond a real book's
    on-time term) still scores below one day past a must-finish date."""
    huge = {"ontime_breach": 1e6, "makespan_days": 500.0}
    one_day = {"ontime_breach": 0.0, "makespan_days": 1.0, "target_breach": 1.0}
    assert optimizer.score(huge) < optimizer.score(one_day)

    cfg = PlanConfig(plan_start=datetime(2025, 3, 3, 8))
    assert cfg.target_weight == optimizer.TARGET_WEIGHT
    lateness = {(f"S{i}", "X"): 60.0 for i in range(500)}
    many_late = PlanMetrics(0.0, 0.0, 0, 500.0, lateness, {}, {("N", "X"): 0.0})
    one_miss = PlanMetrics(0.0, 0.0, 0, 1.0, {}, {}, {("N", "X"): 1.0})
    assert ppc_score(many_late, cfg) < ppc_score(one_miss, cfg)


def test_no_must_finish_date_scores_exactly_as_before():
    lines = [SOLine(so_no="A", item_code="X", item_name="x", qty=1,
                    delivery_date=START)]
    m = optimizer.plan_metrics([], lines, START)
    assert "target_breach" not in m and "target_misses" not in m


def test_rule1_gives_a_clubbed_batch_the_tightest_must_finish_date():
    cfg = Config(consolidation_window_days=10)
    lines = [SOLine(so_no="A", item_code="X", item_name="x", qty=5,
                    delivery_date=date(2025, 3, 10), must_finish_by=date(2025, 3, 12)),
             SOLine(so_no="B", item_code="X", item_name="x", qty=5,
                    delivery_date=date(2025, 3, 11), must_finish_by=date(2025, 3, 9)),
             SOLine(so_no="C", item_code="X", item_name="x", qty=5,
                    delivery_date=date(2025, 3, 12))]
    [batch] = rule1_consolidate.run(lines, cfg)
    assert batch.must_finish_by == date(2025, 3, 9)


def test_an_order_without_the_date_serializes_byte_identically():
    o = Order(so_no="A", item_code="X", item_name="x", ordered_qty=1,
              delivery_date=START)
    assert "must_finish_by" not in o.to_json()
    o.must_finish_by = date(2025, 3, 9)
    assert Order.from_json(o.to_json()).must_finish_by == date(2025, 3, 9)


def test_the_cloud_payload_carries_the_date(masters):
    from engine import optimize_service
    o = Order(so_no="NEW", item_code=ITEM_A, item_name="a", ordered_qty=50,
              delivery_date=date(2025, 3, 5), must_finish_by=date(2025, 3, 5))
    payload = optimize_service.build_payload(
        {o.key: o}, [], book_store.load_masters_bytes(), CONF, seed=1,
        candidates=[50], budget_per_candidate=1)
    orders = optimize_service.parse_payload(payload)[0]
    assert orders[o.key].must_finish_by == date(2025, 3, 5)


def test_the_new_order_first_seed_alone_makes_the_date(masters):
    """Budget 1: the search evaluates only its seeds. The existing orders are all
    overdue, so every dispatch-rule seed (EDD, slack, SPT, LPT) puts the new order
    behind some of them and misses its date by 1 to 6 days; only the seed with the
    must-finish orders first makes it. A real book has ~70 orders and a few hundred
    plans per candidate, so this seed is what guarantees the date is tried at all."""
    asked = START + timedelta(days=1)
    lines = [SOLine(so_no=f"EX{i}", item_code=ITEM_A, item_name="a",
                    qty=(30 if i % 2 else 70),
                    delivery_date=START - timedelta(days=2 + i)) for i in range(10)]
    lines.append(SOLine(so_no="NEW", item_code=ITEM_A, item_name="a", qty=50,
                        delivery_date=asked, must_finish_by=asked))
    res = new_engine.optimize_sequence(lines, CONF, masters, budget_evals=1, seed=1)
    assert res.best["target_breach"] == 0


def test_the_book_hands_the_date_to_planning():
    from engine.orderbook import active_so_lines
    o = Order(so_no="NEW", item_code="X", item_name="x", ordered_qty=5,
              delivery_date=date(2025, 3, 5), must_finish_by=date(2025, 3, 5))
    [line] = active_so_lines({o.key: o}, [])
    assert line.must_finish_by == date(2025, 3, 5)


def test_the_planner_receives_the_batch_date(masters):
    lines = _book(date(2025, 3, 5), must_finish=True)
    batches = rule1_consolidate.run(lines, CONF)
    orders, _ = new_engine._orders_from_batches(batches, new_engine._new_masters(False))
    assert [o.must_finish_by for o in orders if o.must_finish_by] == [date(2025, 3, 5)]


def test_the_orders_table_carries_the_day_each_order_was_added():
    """The Orders tab tags an order added in the last 7 days NEW from this field."""
    from engine.orderbook import order_rows
    o = Order(so_no="A", item_code="X", item_name="x", ordered_qty=1,
              delivery_date=START, first_seen="2026-10-25")
    [row] = order_rows({o.key: o}, {}, [])
    assert row["Added on"] == "2026-10-25"
