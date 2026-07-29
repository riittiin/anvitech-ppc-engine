from datetime import date, datetime

from ppc_engine.domain.order import Order
from ppc_engine.objective.metrics import compute_metrics
from ppc_engine.scheduler.schedule import Schedule


def test_promise_slip_by_order():
    o = Order(so_no="SO1", item_code="IT-A", item_name="A", qty=10,
              due_date=date(2026, 8, 20), promise_date=date(2026, 8, 5))
    sched = Schedule(segments=tuple(),
                     completion={("SO1", "IT-A"): datetime(2026, 8, 10, 17, 0)})
    m = compute_metrics(sched, [o], datetime(2026, 7, 29, 8, 0))
    assert m.promise_slip_by_order[("SO1", "IT-A")] == 5   # 10-Aug - 5-Aug

    # order with no promise_date is absent from the map
    o2 = Order(so_no="SO2", item_code="IT-B", item_name="B", qty=10, due_date=date(2026, 8, 20))
    sched2 = Schedule(segments=tuple(), completion={("SO2", "IT-B"): datetime(2026, 8, 30, 17, 0)})
    m2 = compute_metrics(sched2, [o2], datetime(2026, 7, 29, 8, 0))
    assert ("SO2", "IT-B") not in m2.promise_slip_by_order
