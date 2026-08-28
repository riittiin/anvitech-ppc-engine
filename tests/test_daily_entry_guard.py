"""Daily Entry: what a step still owes, shown before the punch (2026-08-28 spec).

Two SO lines on the same item code are clubbed into one batch by Rule 1, so the
floor sees one pile of identical parts. 26-27SO149 (400) and 26-27SO150 (23) were
such a pair: SO150's 23 finished, the 23 were punched against SO149, and the order
book then told the directors the exact opposite of the truth.

These tests pin the numbers the form shows, so the operator never has to leave the
tab to work that out. The load-bearing one is
``test_can_enter_now_agrees_with_the_save_guard``: what the panel offers must be
exactly what ``precedence_cap_error`` accepts, or the form would invite a punch the
server then refuses.
"""
from datetime import date

from engine.models import Order, Actual, Masters, Routing, Process
from engine import orderbook

D = date(2025, 8, 1)


def _routing(item, names):
    return Routing(
        item_code=item, description="", customer="", rm_type="", moq=None,
        processes=[Process(seq=i + 1, name=n, cycle_time=1, total_time=1,
                           suggested_machine="M1", allotted_machine="M1")
                   for i, n in enumerate(names)],
    )


def _masters(*routings):
    return Masters(routings={r.item_code: r for r in routings})


def _order(so, item, qty, d=D, completed=False):
    return Order(so_no=so, item_code=item, item_name=item, ordered_qty=qty,
                 delivery_date=d, completed=completed)


def _act(so, item, process, prod, rej=0):
    return Actual(so_no=so, item_code=item, entry_date=D, process=process,
                  qty_produced=prod, qty_rejected=rej, item_name=item, operator="o")


# Two real steps, so there is always an upstream cap to check.
_R = _routing("A", ["CNC FIRST SIDE", "VMC FIRST SIDE"])


def _steps(progress, so, item):
    """{process name: step dict} for one order — order-independent lookups."""
    entry = progress[orderbook.entry_key(so, item)]
    return {s["process"]: s for s in entry["steps"]}


def test_unpunched_order_owes_the_whole_quantity():
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    prog = orderbook.entry_progress(active, [], _masters(_R))
    entry = prog[orderbook.entry_key("SO1", "A")]
    assert entry["ordered"] == 400
    assert entry["delivery_date"] == "2025-08-01"        # ISO; the browser formats it
    s = _steps(prog, "SO1", "A")
    assert s["CNC FIRST SIDE"]["done"] == 0
    assert s["CNC FIRST SIDE"]["still_to_make"] == 400
    # First step: nothing upstream limits it, so the cap is the ordered qty.
    assert s["CNC FIRST SIDE"]["can_enter_now"] == 400
    # The second step owes the full order but nothing has cleared step 1 yet.
    assert s["VMC FIRST SIDE"]["still_to_make"] == 400
    assert s["VMC FIRST SIDE"]["can_enter_now"] == 0


def test_rejects_net_across_entries():
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    acts = [_act("SO1", "A", "CNC FIRST SIDE", 100),
            _act("SO1", "A", "CNC FIRST SIDE", 0, rej=20)]   # scrapped the next day
    s = _steps(orderbook.entry_progress(active, acts, _masters(_R)), "SO1", "A")
    assert s["CNC FIRST SIDE"]["done"] == 80               # 100 produced - 20 rejected
    assert s["CNC FIRST SIDE"]["still_to_make"] == 320


def test_downstream_step_is_capped_by_what_cleared_upstream():
    """The 166/400 case: the step owes 400 but only 166 can be entered today."""
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    acts = [_act("SO1", "A", "CNC FIRST SIDE", 166)]
    s = _steps(orderbook.entry_progress(active, acts, _masters(_R)), "SO1", "A")
    assert s["VMC FIRST SIDE"]["still_to_make"] == 400     # what the order owes here
    assert s["VMC FIRST SIDE"]["can_enter_now"] == 166     # what has cleared step 1


def test_can_enter_now_shrinks_by_what_is_already_recorded_here():
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    acts = [_act("SO1", "A", "CNC FIRST SIDE", 166),
            _act("SO1", "A", "VMC FIRST SIDE", 100)]
    s = _steps(orderbook.entry_progress(active, acts, _masters(_R)), "SO1", "A")
    assert s["VMC FIRST SIDE"]["can_enter_now"] == 66      # 166 cleared - 100 done here


def test_completed_and_unrouted_orders_are_absent():
    active = {
        ("SO1", "A"): _order("SO1", "A", 400, completed=True),
        ("SO2", "ZZ"): _order("SO2", "ZZ", 10),            # no routing for ZZ
    }
    assert orderbook.entry_progress(active, [], _masters(_R)) == {}


def test_two_orders_on_one_item_stay_separate():
    """The owner's case: same item code, two SO lines, punches must not bleed."""
    active = {("SO149", "A"): _order("SO149", "A", 400),
              ("SO150", "A"): _order("SO150", "A", 23)}
    acts = [_act("SO150", "A", "CNC FIRST SIDE", 23)]
    prog = orderbook.entry_progress(active, acts, _masters(_R))
    assert _steps(prog, "SO150", "A")["CNC FIRST SIDE"]["still_to_make"] == 0
    assert _steps(prog, "SO149", "A")["CNC FIRST SIDE"]["still_to_make"] == 400


def test_can_enter_now_agrees_with_the_save_guard():
    """THE load-bearing test. Every number the panel offers as enterable must be
    accepted by the guard that runs on Save, and one piece more must be refused.
    If these two ever drift apart the form starts inviting punches the server
    rejects, which is the class of bug this feature exists to remove."""
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    acts = [_act("SO1", "A", "CNC FIRST SIDE", 166),
            _act("SO1", "A", "VMC FIRST SIDE", 100)]
    prog = orderbook.entry_progress(active, acts, _masters(_R))
    for step in prog[orderbook.entry_key("SO1", "A")]["steps"]:
        allowed = step["can_enter_now"]
        ok = acts + [_act("SO1", "A", step["process"], allowed)]
        assert orderbook.precedence_cap_error(
            ok, "SO1", "A", step["process"], _R, 400) is None, \
            f"panel offered {allowed} at {step['process']} but Save refuses it"
        over = acts + [_act("SO1", "A", step["process"], allowed + 1)]
        assert orderbook.precedence_cap_error(
            over, "SO1", "A", step["process"], _R, 400) is not None, \
            f"one more than {allowed} at {step['process']} should be refused"
