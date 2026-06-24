"""Order-book pure logic: merge, status derivation, active lines, persistence."""
from datetime import date

from engine.models import SOLine, Order, Actual, Masters, Routing, Process
from engine import orderbook, book_store


def _so(no, item, qty, d):
    return SOLine(so_no=no, item_code=item, item_name=item, qty=qty, delivery_date=d)


def _order(no, item, qty, d, completed=False):
    return Order(so_no=no, item_code=item, item_name=item, ordered_qty=qty,
                 delivery_date=d, completed=completed)


D = date(2025, 8, 1)


def test_merge_adds_unseen_and_flags_known():
    active = {"SO1": _order("SO1", "A", 10, D)}
    completed = {"SO9": _order("SO9", "Z", 5, D, completed=True)}
    lines = [_so("SO1", "A", 10, D), _so("SO2", "B", 20, D), _so("SO9", "Z", 5, D)]

    new, flags = orderbook.merge_upload(lines, active, completed)
    assert [o.so_no for o in new] == ["SO2"]                 # only the unseen one added
    reasons = {f["so_no"]: f["reason"] for f in flags}
    assert "duplicate" in reasons["SO1"]
    assert "already completed" in reasons["SO9"]


def test_merge_flags_changed_order_without_modifying():
    active = {"SO1": _order("SO1", "A", 10, D)}
    new, flags = orderbook.merge_upload([_so("SO1", "A", 99, D)], active, {})
    assert new == []
    assert "changed" in flags[0]["reason"]
    assert active["SO1"].ordered_qty == 10                   # original untouched


def test_status_derivation():
    o = _order("SO1", "A", 10, D)
    assert orderbook.derive_status(o, {}) == orderbook.PENDING
    assert orderbook.derive_status(o, {"SO1": 4}) == orderbook.RUNNING
    o.completed = True
    assert orderbook.derive_status(o, {"SO1": 4}) == orderbook.COMPLETE


def test_active_so_lines_use_remaining_and_skip_done():
    active = {
        "SO1": _order("SO1", "A", 10, D),      # produced 4 -> remaining 6
        "SO2": _order("SO2", "B", 5, D),       # produced 5 -> remaining 0 -> skipped
        "SO3": _order("SO3", "C", 8, D, completed=True),   # completed -> skipped
    }
    actuals = [
        Actual("SO1", "A", D, qty_produced=4),
        Actual("SO2", "B", D, qty_produced=5),
    ]
    lines = orderbook.active_so_lines(active, actuals)
    by = {l.so_no: l.qty for l in lines}
    assert by == {"SO1": 6}                                  # only SO1, at remaining 6


def test_delete_orders_removes_order_and_its_actuals():
    book_store.add_orders([_order("SO1", "A", 10, D), _order("SO2", "B", 5, D)])
    book_store.append_actual(Actual("SO1", "A", D, qty_produced=4))
    book_store.append_actual(Actual("SO2", "B", D, qty_produced=2))

    book_store.delete_orders(["SO1"])
    assert "SO1" not in book_store.load_active_orders()
    assert "SO2" in book_store.load_active_orders()
    remaining = book_store.load_actuals()
    assert [a.so_no for a in remaining] == ["SO2"]   # SO1's actual purged


def test_delete_all_wipes_orders_and_actuals():
    book_store.add_orders([_order("SO1", "A", 10, D)])
    book_store.append_actual(Actual("SO1", "A", D, qty_produced=4))
    book_store.delete_all()
    assert book_store.load_active_orders() == {}
    assert book_store.load_completed_orders() == {}
    assert book_store.load_actuals() == []


def test_persistence_round_trip_and_complete():
    book_store.add_orders([_order("SO1", "A", 10, D)])
    book_store.append_actual(Actual("SO1", "A", D, qty_produced=4))

    active = book_store.load_active_orders()
    assert active["SO1"].ordered_qty == 10
    assert len(book_store.load_actuals()) == 1

    assert book_store.complete_order("SO1") is True
    assert "SO1" not in book_store.load_active_orders()
    assert book_store.load_completed_orders()["SO1"].completed is True


# --------------------------------------------------------------------------- #
# machine_lost_minutes: downtime + setup overrun attributed per machine
# --------------------------------------------------------------------------- #
def _routing(item, procs):
    """procs = list of (process_name, suggested_machine)."""
    return Routing(
        item_code=item, description="", customer="", rm_type="", moq=None,
        processes=[Process(seq=i + 1, name=name, cycle_time=1, total_time=None,
                           suggested_machine=sm, allotted_machine=None)
                   for i, (name, sm) in enumerate(procs)],
    )


def _masters(*routings):
    return Masters(routings={r.item_code: r for r in routings})


def _actual(item, process, **kw):
    return Actual(so_no="SO1", item_code=item, entry_date=D, process=process, **kw)


def test_lost_minutes_downtime_plus_setup_overrun_per_machine():
    masters = _masters(_routing("X", [("CNC FIRST SIDE", "CNC 4")]))
    # setup overrun 120-90=30, plus 30 breakdown = 60, on canonical "CNC4".
    a = _actual("X", "CNC FIRST SIDE", actual_setup_min=120, machine_breakdown_min=30)
    lost, unattributed = orderbook.machine_lost_minutes([a], masters, planned_setup_min=90)
    assert lost == {"CNC4": 60.0}
    assert unattributed == []


def test_lost_minutes_setup_under_plan_does_not_give_time_back():
    masters = _masters(_routing("X", [("CNC FIRST SIDE", "CNC 4")]))
    # actual setup 60 < planned 90 -> overrun clamped to 0; only the 20 downtime counts.
    a = _actual("X", "CNC FIRST SIDE", actual_setup_min=60, no_power_min=20)
    lost, _ = orderbook.machine_lost_minutes([a], masters, planned_setup_min=90)
    assert lost == {"CNC4": 20.0}


def test_lost_minutes_accumulate_on_same_machine():
    masters = _masters(_routing("X", [("CNC FIRST SIDE", "CNC 4")]))
    a1 = _actual("X", "CNC FIRST SIDE", no_power_min=15)
    a2 = _actual("X", "CNC FIRST SIDE", tool_problem_min=25)
    lost, _ = orderbook.machine_lost_minutes([a1, a2], masters, planned_setup_min=90)
    assert lost == {"CNC4": 40.0}


def test_lost_minutes_unmatched_process_is_unattributed_not_fatal():
    masters = _masters(_routing("X", [("CNC FIRST SIDE", "CNC 4")]))
    a = _actual("X", "TYPO PROCESS", no_power_min=15)
    lost, unattributed = orderbook.machine_lost_minutes([a], masters, planned_setup_min=90)
    assert lost == {}
    assert len(unattributed) == 1 and unattributed[0]["process"] == "TYPO PROCESS"


def test_lost_minutes_missing_routing_is_unattributed():
    masters = _masters(_routing("X", [("CNC FIRST SIDE", "CNC 4")]))
    a = _actual("ZZZ", "CNC FIRST SIDE", no_power_min=15)   # no routing for ZZZ
    lost, unattributed = orderbook.machine_lost_minutes([a], masters, planned_setup_min=90)
    assert lost == {}
    assert len(unattributed) == 1 and unattributed[0]["item_code"] == "ZZZ"


def test_lost_minutes_synthetic_station_when_no_suggested_machine():
    # A finishing step with no suggested machine -> Rule 6 names a station after the
    # process; lost time must key on that same id (normalize of the process name).
    masters = _masters(_routing("X", [("DEBURRING", None)]))
    a = _actual("X", "DEBURRING", no_operator_min=12)
    lost, _ = orderbook.machine_lost_minutes([a], masters, planned_setup_min=90)
    assert lost == {"DEBURRING": 12.0}


def test_lost_minutes_zero_loss_is_ignored():
    masters = _masters(_routing("X", [("CNC FIRST SIDE", "CNC 4")]))
    a = _actual("X", "CNC FIRST SIDE", actual_setup_min=90)   # no overrun, no downtime
    lost, unattributed = orderbook.machine_lost_minutes([a], masters, planned_setup_min=90)
    assert lost == {} and unattributed == []
