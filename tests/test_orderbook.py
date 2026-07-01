"""Order-book pure logic: merge, status derivation, active lines, persistence."""
from datetime import date

from engine.models import SOLine, Order, Actual, Masters, Routing, Process, WorkCalendar
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
    assert orderbook.derive_status(o, set()) == orderbook.PENDING
    assert orderbook.derive_status(o, {"SO1"}) == orderbook.RUNNING       # has an actual
    o.completed = True
    assert orderbook.derive_status(o, {"SO1"}) == orderbook.COMPLETE


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
# Shared routing / masters / actual helpers (finished-gate + per-process tests)
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


# --------------------------------------------------------------------------- #
# Finished-goods gate: WIP (intermediate-process good) must NOT fulfil the order.
# Only good qty at the DISPATCH step (or the last step if no DISPATCH) counts.
# --------------------------------------------------------------------------- #
# A routing whose last step is a DISPATCH pass-through (5 of the 7 focus items).
_DISPATCH_ROUTING = _routing(
    "A", [("CNC FIRST SIDE", "CNC4"), ("WASHING", None), ("DISPATCH", None)]
)
# A routing whose last real step is PACKING and has NO DISPATCH (the other 2 items).
_PACKING_ROUTING = _routing(
    "B", [("CNC FIRST SIDE", "CNC4"), ("PACKING", "MPK1")]
)


def test_finished_gate_is_dispatch_when_present():
    assert orderbook.finished_gate(_DISPATCH_ROUTING) == "DISPATCH"


def test_finished_gate_falls_back_to_last_step_without_dispatch():
    assert orderbook.finished_gate(_PACKING_ROUTING) == "PACKING"


def test_finished_good_ignores_intermediate_process():
    masters = _masters(_DISPATCH_ROUTING)
    # 5 good produced at the FIRST process -> WIP, fulfils nothing.
    wip = _actual("A", "CNC FIRST SIDE", qty_produced=5)
    assert orderbook.finished_good_by_so([wip], masters) == {}


def test_finished_good_counts_at_dispatch():
    masters = _masters(_DISPATCH_ROUTING)
    done = _actual("A", "DISPATCH", qty_produced=5)
    assert orderbook.finished_good_by_so([done], masters) == {"SO1": 5.0}


def test_finished_good_counts_at_packing_when_no_dispatch():
    masters = _masters(_PACKING_ROUTING)
    done = Actual(so_no="SO2", item_code="B", entry_date=D, process="PACKING",
                  qty_produced=8, qty_rejected=1)        # good = 7
    assert orderbook.finished_good_by_so([done], masters) == {"SO2": 7.0}


def test_finished_gate_match_is_normalized():
    masters = _masters(_DISPATCH_ROUTING)
    # operator typed it lower-case with stray spaces — must still match.
    done = _actual("A", "  dispatch ", qty_produced=3)
    assert orderbook.finished_good_by_so([done], masters) == {"SO1": 3.0}


def test_active_so_lines_ignore_wip_keep_full_qty():
    """The user's bug: 5 good at the first process must NOT shrink the order."""
    active = {"SO1": _order("SO1", "A", 500, D)}
    masters = _masters(_DISPATCH_ROUTING)
    wip = _actual("A", "CNC FIRST SIDE", qty_produced=5)
    lines = orderbook.active_so_lines(active, [wip], masters)
    assert {l.so_no: l.qty for l in lines} == {"SO1": 500.0}     # full 500 still planned


def test_active_so_lines_reduce_only_on_dispatch():
    active = {"SO1": _order("SO1", "A", 500, D)}
    masters = _masters(_DISPATCH_ROUTING)
    done = _actual("A", "DISPATCH", qty_produced=5)
    lines = orderbook.active_so_lines(active, [done], masters)
    assert {l.so_no: l.qty for l in lines} == {"SO1": 495.0}


def test_status_running_on_any_actual_even_if_only_wip():
    """Work has started (first process) -> Running, even though nothing is finished."""
    o = _order("SO1", "A", 500, D)
    with_actuals = orderbook.so_nos_with_actuals(
        [_actual("A", "CNC FIRST SIDE", qty_produced=5)]
    )
    assert orderbook.derive_status(o, with_actuals) == orderbook.RUNNING
    o.completed = True
    assert orderbook.derive_status(o, with_actuals) == orderbook.COMPLETE


def test_status_pending_with_no_actuals():
    o = _order("SO1", "A", 500, D)
    assert orderbook.derive_status(o, orderbook.so_nos_with_actuals([])) == orderbook.PENDING


# --------------------------------------------------------------------------- #
# Per-process progress: each process must be re-planned at ordered − done-at-that
# -process, so finished steps aren't redone ("continue from reality").
# --------------------------------------------------------------------------- #
def test_completed_by_process_sums_good_per_step():
    acts = [_actual("A", "CNC FIRST SIDE", qty_produced=50),
            _actual("A", "cnc first side ", qty_produced=10),   # normalized -> same step
            _actual("A", "WASHING", qty_produced=20, qty_rejected=2)]
    cbp = orderbook.completed_by_process(acts)
    assert cbp[("SO1", "CNC FIRST SIDE")] == 60.0
    assert cbp[("SO1", "WASHING")] == 18.0                       # good = 20 − 2


def test_active_so_lines_carry_per_process_remaining():
    active = {"SO1": _order("SO1", "A", 500, D)}      # routing: CNC FIRST SIDE, WASHING, DISPATCH
    masters = _masters(_DISPATCH_ROUTING)
    acts = [_actual("A", "CNC FIRST SIDE", qty_produced=50),
            _actual("A", "WASHING", qty_produced=20)]
    line = orderbook.active_so_lines(active, acts, masters)[0]
    assert line.qty == 500                            # order remaining (nothing at DISPATCH gate yet)
    assert line.process_qty["CNC FIRST SIDE"] == 450  # 500 − 50 already cut
    assert line.process_qty["WASHING"] == 480         # 500 − 20
    assert line.process_qty["DISPATCH"] == 500        # gate: 0 done


def test_active_so_lines_no_actuals_leave_process_qty_none():
    # No actuals -> no per-process override -> Rule 6 keeps today's behaviour (golden stable).
    active = {"SO1": _order("SO1", "A", 500, D)}
    masters = _masters(_DISPATCH_ROUTING)
    line = orderbook.active_so_lines(active, [], masters)[0]
    assert line.process_qty is None


def test_process_progress_rows_show_done_and_remaining_per_step():
    active = {"SO1": _order("SO1", "A", 500, D)}     # routing: CNC FIRST SIDE, WASHING, DISPATCH
    masters = _masters(_DISPATCH_ROUTING)
    acts = [_actual("A", "CNC FIRST SIDE", qty_produced=50),
            _actual("A", "WASHING", qty_produced=20)]
    rows = orderbook.process_progress_rows(active, acts, masters)
    # one row per process of each order that has started; ordered by seq.
    first = next(r for r in rows if r["Process"] == "CNC FIRST SIDE")
    assert first["SO No"] == "SO1" and first["Completed"] == 50 and first["Remaining"] == 450
    wash = next(r for r in rows if r["Process"] == "WASHING")
    assert wash["Completed"] == 20 and wash["Remaining"] == 480


def test_process_progress_rows_empty_without_actuals():
    active = {"SO1": _order("SO1", "A", 500, D)}
    masters = _masters(_DISPATCH_ROUTING)
    assert orderbook.process_progress_rows(active, [], masters) == []


# Recorded downtime / setup time is captured for the record only and never affects
# the schedule (the feedback loop is quantity-only), so there is no downtime→plan
# attribution to test here.


# --------------------------------------------------------------------------- #
# effective_plan_start_date: the plan clock advances past days already worked, so a
# re-plan continues from the next working day's first shift (not the original date).
# --------------------------------------------------------------------------- #
_CAL = WorkCalendar()   # Thursday (weekday 3) off, no holidays


def _act(entry):
    return Actual(so_no="SO1", item_code="A", entry_date=entry, qty_produced=1)


def test_plan_start_no_actuals_is_the_config_date():
    assert orderbook.effective_plan_start_date([], date(2025, 3, 1), _CAL) == date(2025, 3, 1)


def test_plan_start_advances_to_day_after_latest_actual():
    # Punch dated 1 Mar (Sat) → plan continues 2 Mar (Sun is a working day; only Thu off).
    assert orderbook.effective_plan_start_date([_act(date(2025, 3, 1))],
                                               date(2025, 3, 1), _CAL) == date(2025, 3, 2)


def test_plan_start_skips_the_weekly_off_day():
    # Punch dated 5 Mar (Wed) → next is 6 Mar (Thursday, off) → lands on 7 Mar (Fri).
    assert orderbook.effective_plan_start_date([_act(date(2025, 3, 5))],
                                               date(2025, 3, 1), _CAL) == date(2025, 3, 7)


def test_plan_start_uses_the_latest_of_many_actuals():
    acts = [_act(date(2025, 3, 1)), _act(date(2025, 3, 3)), _act(date(2025, 3, 2))]
    assert orderbook.effective_plan_start_date(acts, date(2025, 3, 1), _CAL) == date(2025, 3, 4)


def test_plan_start_never_goes_before_the_config_date():
    # An old actual must not drag the plan earlier than its configured start.
    assert orderbook.effective_plan_start_date([_act(date(2025, 3, 1))],
                                               date(2025, 3, 10), _CAL) == date(2025, 3, 10)
