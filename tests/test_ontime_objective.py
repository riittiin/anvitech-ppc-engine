"""On-time objective, engine side (spec 2026-08-06, REVISED 2026-09-05).

The owner's rule, in full: deliver on time; NO late day is free; finishing early is
a quarter as bad as finishing late (early is an inventory cost, not a customer one);
and misses must be SPREAD across orders rather than concentrated on a few.

2026-09-05 replaced the original "+/-4 days either side is free, early == late" rule.
Measured on five days of the live book: the 4-day band let the search move orders
into it at zero cost and inverted the ranking against total late-days in 17.0% of
pairs. Band 4 -> 0, early weighted 0.25, plus a flat 10 per late day so lateness is
never free anywhere. The spreading requirement is UNCHANGED and still binding — it
is what caps the linear weight at 18 (see ppc_engine/config.py).
"""
from datetime import date, datetime, timedelta

from engine import optimizer
from engine.models import SOLine, ScheduleEntry

PS = date(2026, 8, 6)
DUE = date(2026, 9, 1)


def _line(so, item, due=DUE):
    return SOLine(so_no=so, item_code=item, item_name=item, qty=10, delivery_date=due)


def _entry(so, item, end):
    return ScheduleEntry(batch_id=so, item_code=item, process_seq=1,
                         process_name="CNC", machine="CNC1", qty=10,
                         occupancy_min=60, start=datetime(2026, 8, 6, 8, 0),
                         end=end, so_refs=[so])


def _breach_for(days_off):
    """days_off > 0 = late, < 0 = early."""
    lines, sched = [], []
    for n, d in enumerate(days_off):
        so, item = f"SO{n}", f"IT-{n}"
        lines.append(_line(so, item))
        end = datetime(2026, 9, 1, 17, 0) + timedelta(days=d)
        sched.append(_entry(so, item, end))
    return optimizer.plan_metrics(sched, lines, PS)["ontime_breach"]


def test_finishing_early_costs_a_quarter_of_finishing_late():
    """Early is an inventory cost, not a customer one. 30 days early must cost far
    less than 30 days late — but still something, so the plan is not pulled absurdly
    early. late = 30^2 + 10*30 = 1200; early = (0.25*30)^2 = 56.25."""
    assert _breach_for([30]) == 1200.0
    assert _breach_for([-30]) == 56.25
    assert _breach_for([-30]) < _breach_for([30])
    assert _breach_for([-30]) > 0


def test_no_late_day_is_free():
    """The 2026-09-05 change. Every late day costs something, however small the miss.
    This is what stopped the search parking 26 orders inside a free band."""
    for d in (1, 2, 3, 4):
        assert _breach_for([d]) > 0.0, f"{d} days late must not be free"
    assert _breach_for([0]) == 0.0          # exactly on time is still free


def test_one_day_late_costs_the_square_plus_the_linear_term():
    """Pins band=0 and the linear weight: 1^2 + 10*1 = 11."""
    assert _breach_for([1]) == 11.0
    assert _breach_for([-1]) == 0.0625      # (0.25 * 1)^2, no linear term when early


def test_squaring_still_spreads_the_misses():
    """UNCHANGED owner requirement, and the reason the linear weight is capped at 18:
    ten orders 6 days out must still beat one order 30 days out.
    spread = 10 * (36 + 60) = 960;  concentrated = 900 + 300 = 1200."""
    concentrated = _breach_for([30])
    spread = _breach_for([6] * 10)
    assert spread == 960.0
    assert concentrated == 1200.0
    assert spread < concentrated


def test_cap_limits_the_squared_term_but_not_the_linear_one():
    """The 60-day cap still stops a doomed order swamping the SQUARED term, so 100
    and 64 days late square to the same 3600. The linear term is deliberately NOT
    capped — capping it would recreate exactly the free zone this change removed,
    just at the far end instead of the near end."""
    assert _breach_for([100]) == 60.0 ** 2 + 10 * 100
    assert _breach_for([64]) == 60.0 ** 2 + 10 * 64
    assert _breach_for([100]) > _breach_for([64])


def test_score_uses_ontime_breach_and_a_makespan_tiebreak():
    base = {"makespan_days": 50.0, "ontime_breach": 0.0}
    worse = {"makespan_days": 50.0, "ontime_breach": 10.0}
    assert optimizer.score(worse) - optimizer.score(base) == optimizer.ONTIME_WEIGHT * 10.0


def test_makespan_cannot_outrank_the_ontime_term():
    """Makespan is a TIE-BREAK. A plan one day shorter must never beat a plan with a
    genuinely better on-time result. At weight 0.1, 100 extra days of schedule are
    worth less than a single order 8 days off ((8-4)^2 = 16)."""
    shorter_but_worse = {"makespan_days": 10.0, "ontime_breach": 16.0}
    longer_but_better = {"makespan_days": 110.0, "ontime_breach": 0.0}
    assert optimizer.score(longer_but_better) < optimizer.score(shorter_but_worse)


def test_makespan_still_breaks_an_exact_tie():
    a = {"makespan_days": 50.0, "ontime_breach": 5.0}
    b = {"makespan_days": 60.0, "ontime_breach": 5.0}
    assert optimizer.score(a) < optimizer.score(b)


def test_plan_metrics_keeps_every_reported_field():
    """Global constraint: the UI and api read these. Losing one blanks a panel."""
    m = optimizer.plan_metrics([_entry("SO1", "IT-A", datetime(2026, 9, 20, 17, 0))],
                               [_line("SO1", "IT-A")], PS)
    for field in ("makespan_days", "late_orders", "total_late_days", "max_late_days",
                  "slip_severity", "ceiling_breach", "committed_promise_breach",
                  "max_committed_slip", "orders", "ontime_breach"):
        assert field in m, f"plan_metrics stopped reporting {field}"
    assert m["total_late_days"] == 19        # still reported even though score ignores it
    assert m["slip_severity"] == (19 - 2) ** 2
