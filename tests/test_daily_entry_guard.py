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


def test_cleared_before_is_none_on_first_step_and_upstream_good_after():
    """``cleared_before`` is the number the UI is allowed to name as "the step before
    this one cleared". First step: nothing is before it, so it must be None, never a
    number that reads as a real step."""
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    acts = [_act("SO1", "A", "CNC FIRST SIDE", 166)]
    s = _steps(orderbook.entry_progress(active, acts, _masters(_R)), "SO1", "A")
    assert s["CNC FIRST SIDE"]["cleared_before"] is None
    assert s["VMC FIRST SIDE"]["cleared_before"] == 166


def test_first_step_never_claims_an_upstream_step_when_rejects_exist():
    """Reproduction of the live bug: 400 ordered, 100 produced / 20 rejected at the
    FIRST routing step. There is no step before this one. ``can_enter_now +
    done`` (the old, wrong formula the UI used) equals 380 here — a number that
    looks like a real upstream clearance but names a step that does not exist.
    ``cleared_before`` must say None, so the UI can never invent one."""
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    acts = [_act("SO1", "A", "CNC FIRST SIDE", 100, rej=20)]
    s = _steps(orderbook.entry_progress(active, acts, _masters(_R)), "SO1", "A")
    step = s["CNC FIRST SIDE"]
    assert step["done"] == 80
    assert step["still_to_make"] == 320
    assert step["can_enter_now"] == 300
    assert step["cleared_before"] is None


def test_cleared_before_is_true_upstream_good_not_can_enter_now_plus_done_when_rejects_exist():
    """A later step where rejects happen at THAT step. ``cleared_before`` must stay
    the true upstream good qty (166) — not ``can_enter_now + done`` (66 + 80 = 146),
    which is what the old, wrong UI formula computed and is off by the reject
    count."""
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    acts = [_act("SO1", "A", "CNC FIRST SIDE", 166),
            _act("SO1", "A", "VMC FIRST SIDE", 100, rej=20)]
    s = _steps(orderbook.entry_progress(active, acts, _masters(_R)), "SO1", "A")
    step = s["VMC FIRST SIDE"]
    assert step["done"] == 80          # 100 produced - 20 rejected at this step
    assert step["can_enter_now"] == 66  # 166 cleared upstream - 100 produced here
    assert step["cleared_before"] == 166
    assert step["can_enter_now"] + step["done"] != step["cleared_before"]


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


# --------------------------------------------------------------------------- #
# GET /items carries the progress. It is the right home: it already feeds this
# form, it is a LIVE store read (so the numbers refresh after every Save), and it
# is role-open (the floor logs in as `user`). Deliberately NOT on POST /run —
# that response is cached by _plan_fingerprint and live punch data inside it is
# the 2026-08-08 stale-cache bug again.
# --------------------------------------------------------------------------- #
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient          # noqa: E402

from api.main import app                            # noqa: E402
from api import auth                                # noqa: E402
from engine import book_store                       # noqa: E402
from tests.sample_workbook import build_sample_bytes, ITEM_A   # noqa: E402

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_SAMPLE = build_sample_bytes()
_ACCTS = auth._accounts()
_ADMIN = next(u for u, a in _ACCTS.items() if a["role"] == auth.ADMIN)
_ADMIN_PWD = _ACCTS[_ADMIN]["password"]


@pytest.fixture
def client():
    c = TestClient(app)
    assert c.post("/login", data={"username": _ADMIN, "password": _ADMIN_PWD}).status_code == 200
    c.post("/upload", files={"file": ("sample.xlsx", _SAMPLE, XLSX_MIME)})
    return c


def test_items_carries_progress_for_each_open_order(client):
    data = client.get("/items").json()
    assert "progress" in data and "item_to_sos" in data
    entry = next(iter(data["progress"].values()))
    assert {"so_no", "item_code", "ordered", "delivery_date", "steps"} <= set(entry)
    step = entry["steps"][0]
    assert {"seq", "process", "done", "still_to_make", "can_enter_now"} <= set(step)


def test_item_to_sos_lists_both_orders_sharing_an_item_code(client):
    """The clubbed case the whole feature exists for."""
    book_store.add_orders([_order("SO-CLUB", ITEM_A, 23)])
    data = client.get("/items").json()
    sos = data["item_to_sos"][ITEM_A]
    assert "SO-CLUB" in sos and len(sos) >= 2
    assert orderbook.entry_key("SO-CLUB", ITEM_A) in data["progress"]


# --------------------------------------------------------------------------- #
# The cross-check rule. web/app.js implements exactly this in
# quantityFitsAnotherSo(); this pins the arithmetic against real entry_progress
# output so the two cannot drift. It is the owner's actual case: 26-27SO149 x 400
# and 26-27SO150 x 23 on one item code, 23 typed against SO149.
# --------------------------------------------------------------------------- #
def _fits_another_so(progress, item_to_sos, typed, picked_so, item, process):
    """Mirror of quantityFitsAnotherSo() in web/app.js. Returns the other open SOs
    whose remaining at this step is exactly the typed quantity.

    This mirror pins the match SET, not its ORDER: the shipped JS orders its
    result by delivery date (via ``sosForItem``), this mirror by SO number, and on
    3,737 differential checks against real data the two never disagreed on which
    SOs matched -- only on the order of a 10-case tail, which is harmless today
    because the switch button only appears for a single match. If a switch is ever
    offered on multiple matches, ``matches[0]`` becomes meaningful and this mirror
    must be taught the same delivery-date ordering."""
    if typed <= 0:
        return []
    sos = item_to_sos.get(item, [])
    if len(sos) < 2:
        return []
    steps = {s["process"]: s
             for s in progress[orderbook.entry_key(picked_so, item)]["steps"]}
    if typed == steps[process]["still_to_make"]:
        return []                      # fits the order he picked, stay silent
    out = []
    for so in sos:
        if so == picked_so:
            continue
        other = progress.get(orderbook.entry_key(so, item))
        if not other:
            continue
        st = next((s for s in other["steps"] if s["process"] == process), None)
        if st and typed == st["still_to_make"]:
            out.append(so)
    return out


_CLUB_ACTIVE = {("SO149", "A"): _order("SO149", "A", 400, date(2025, 9, 20)),
                ("SO150", "A"): _order("SO150", "A", 23, date(2025, 9, 12))}
_CLUB_MAP = {"A": ["SO149", "SO150"]}
_STEP = "CNC FIRST SIDE"


def _club_progress(acts=()):
    return orderbook.entry_progress(_CLUB_ACTIVE, list(acts), _masters(_R))


def test_cross_check_fires_on_the_owners_actual_mistake():
    """23 typed against SO149, which still needs 400. 23 is exactly SO150's whole
    order. This is the entry that told the directors the opposite of the truth."""
    assert _fits_another_so(_club_progress(), _CLUB_MAP, 23, "SO149", "A", _STEP) == ["SO150"]


def test_cross_check_is_silent_when_the_right_order_is_picked():
    assert _fits_another_so(_club_progress(), _CLUB_MAP, 23, "SO150", "A", _STEP) == []


def test_cross_check_is_silent_on_a_partial_punch():
    """20 of SO150's 23 matches nothing exactly. Warning on a normal partial entry
    would train the floor to click straight through the warnings that matter."""
    assert _fits_another_so(_club_progress(), _CLUB_MAP, 20, "SO149", "A", _STEP) == []
    assert _fits_another_so(_club_progress(), _CLUB_MAP, 20, "SO150", "A", _STEP) == []


def test_cross_check_is_silent_when_the_part_is_on_one_order_only():
    active = {("SO1", "A"): _order("SO1", "A", 400)}
    prog = orderbook.entry_progress(active, [], _masters(_R))
    assert _fits_another_so(prog, {"A": ["SO1"]}, 400, "SO1", "A", _STEP) == []


def test_cross_check_goes_quiet_once_so150_is_punched():
    """Once SO150's 23 are recorded its remaining is 0, so a later 23 against SO149
    no longer matches it and the warning correctly stops firing."""
    acts = [_act("SO150", "A", _STEP, 23)]
    assert _fits_another_so(_club_progress(acts), _CLUB_MAP, 23, "SO149", "A", _STEP) == []


# --------------------------------------------------------------------------- #
# Two extra tests, added during mutation-checking (task-4 Step 6). The five
# tests above pass unchanged even with the "typed == picked's own still_to_make"
# early return deleted, or with the "len(sos) < 2" early return deleted --
# because the main loop already skips ``picked_so`` on its own, both guards
# were unexercised by any fixture above. These two isolate each one.
# --------------------------------------------------------------------------- #
def test_cross_check_is_silent_when_typed_fits_picked_even_if_it_also_fits_another():
    """Two SOs on one item happen to need the SAME remaining quantity (50 each).
    Typed 50 against the one he picked: it fits what he picked, so stay silent --
    even though it ALSO happens to fit the other order exactly. Without the
    "fits picked" early return the loop still finds the other SO's match and
    would wrongly warn."""
    active = {("SO-TIE1", "A"): _order("SO-TIE1", "A", 50),
              ("SO-TIE2", "A"): _order("SO-TIE2", "A", 50)}
    prog = orderbook.entry_progress(active, [], _masters(_R))
    tie_map = {"A": ["SO-TIE1", "SO-TIE2"]}
    assert _fits_another_so(prog, tie_map, 50, "SO-TIE1", "A", _STEP) == []


def test_cross_check_is_silent_when_the_picked_items_routing_is_missing():
    """``item_to_sos`` is built from every active order regardless of routing, but
    ``entry_progress`` has no entry for one with no routing at all -- so with only
    one SO on this (unrouted) item code, ``progress`` has nothing to look up for
    it. The ``len(sos) < 2`` guard must return before that lookup, or this raises
    KeyError instead of staying silent."""
    active = {("SO1", "ZZ"): _order("SO1", "ZZ", 10)}     # ZZ has no routing in _R
    prog = orderbook.entry_progress(active, [], _masters(_R))
    assert prog == {}
    assert _fits_another_so(prog, {"ZZ": ["SO1"]}, 10, "SO1", "ZZ", _STEP) == []


def test_cross_check_is_silent_on_a_zero_quantity_punch():
    """A downtime-only / rejects-only entry types 0 good pieces. SO150's 23 are
    already punched, so ITS still_to_make is 0 -- without the ``typed > 0`` guard the
    cross-check would match that 0 and offer to switch a zero-quantity punch onto
    SO150. Punching nothing never "exactly finishes" anything."""
    acts = [_act("SO150", "A", _STEP, 23)]
    assert _fits_another_so(_club_progress(acts), _CLUB_MAP, 0, "SO149", "A", _STEP) == []
