"""Fixed plan, final review fix wave (2026-10-06).

C1  an accepted earlier date publishes a plan that contains the accepted orders;
I2  the Optimize panel's numbers describe the plan the floor gets after Apply;
I3  Apply rebuilds the frozen set from the plan it publishes;
I4  a failed publish applies nothing;
I6  "Go back to standard plan" republishes the standard plan;
plus the minors (publish lock, date-list failure is said)."""
import time
from datetime import date, timedelta

from engine import book_store, freeze
from engine.models import Actual, Order
from tests.new_sample_workbook import ITEM_A, ITEM_B, SO1, SO2
from tests.test_fixed_plan_api import _move_to_other_cnc, api  # noqa: F401  (fixture)


def _plan_sig(m):
    """Every entry of a FRESH plan: (orders, step, machine, operator, start, end, qty)."""
    m._PLAN_CACHE["key"] = None
    m._plan(m._load_plan_config())
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    return sorted((tuple(e.so_refs or ()), e.process_name, e.machine, e.operator or "",
                   e.start, e.end, e.qty) for e in sched)


def _half_finished_job_on_a_down_machine(m, client):
    """SO2's CNC SECOND SIDE is part done on its published machine, and that machine
    is then marked down for two months. A free plan (Optimize) releases the pin and
    moves the job; a repair would make it wait. Returns the down machine."""
    book_store.add_orders([Order("NSO-003", ITEM_B, ITEM_B, 3000, date(2025, 3, 22))])
    client.post("/run", json={"persist": False})                 # seeds the published plan
    today = m._ist_today()
    for proc, qty in (("BANDSAW OS", 100), ("CNC SECOND SIDE", 40)):
        book_store.append_actual(Actual(so_no=SO2, item_code=ITEM_B, entry_date=today,
                                        qty_produced=qty, process=proc,
                                        operator="Alpha", shift="1st shift"))
    client.post("/optimize/done")                                # freezes the part-done step
    frozen = book_store.load_frozen_ops()
    assert [f["process"] for f in frozen] == ["CNC SECOND SIDE"], frozen
    down = frozen[0]["machine"]
    r = client.post("/machine-downtime", json={
        "machine": down, "from_date": today.isoformat(),
        "to_date": (today + timedelta(days=60)).isoformat(), "reason": "service"})
    assert r.status_code == 200, r.text
    return down


def _cnc2_machines(sig):
    return {x[2] for x in sig if x[1] == "CNC SECOND SIDE" and SO2 in x[0]}


# --------------------------------------------------------------------------- #
# I3: Apply rebuilds the frozen set from the plan it publishes.
# --------------------------------------------------------------------------- #
def test_a_half_finished_job_the_search_moved_stays_moved_after_apply(api):
    m, client = api
    down = _half_finished_job_on_a_down_machine(m, client)
    m._start_optimize(budget_evals=15, label="quick", background=False)
    m._optimize_apply()
    assert down not in _cnc2_machines(_plan_sig(m))
    assert all(f["machine"] != down for f in book_store.load_frozen_ops())


def test_apply_then_done_with_no_new_punch_changes_nothing(api):
    m, client = api
    _half_finished_job_on_a_down_machine(m, client)
    m._start_optimize(budget_evals=15, label="quick", background=False)
    m._optimize_apply()
    applied = _plan_sig(m)
    client.post("/optimize/done")
    assert _plan_sig(m) == applied


# --------------------------------------------------------------------------- #
# I2 / I8: the panel's numbers and the date list describe the plan after Apply.
# --------------------------------------------------------------------------- #
def test_the_panel_after_numbers_are_the_plan_the_floor_gets(api):
    m, client = api
    # A small extra line (found by search) so the repair of the candidate really
    # lands on other dates than its free plan: the resumed half-finished job runs
    # first on its new machine and pushes the others past a day boundary.
    book_store.add_orders([Order("NSO-005", ITEM_A, ITEM_A, 20, date(2025, 3, 24))])
    _half_finished_job_on_a_down_machine(m, client)
    m._start_optimize(budget_evals=15, label="quick", background=False)
    res = m._OPTIMIZE["result"]
    shown = res["best"]["total_late_days"]
    listed = res["date_changes"]
    assert listed is not None
    free = m._metrics_for_ranks(res["ranks"], res["best_overlap"], res["flexible_machines"])
    assert free["total_late_days"] != shown, "fixture is vacuous: free == repaired"
    now = client.post("/run", json={"persist": False}).json()["expected_end"]
    m._optimize_apply()
    got = m._incumbent_metrics()
    assert shown == got["total_late_days"]
    after = client.post("/run", json={"persist": False}).json()["expected_end"]
    moved = {f"{r['so']}\x1f{r['item']}": r["after"] for r in listed}
    for k, v in after.items():
        assert moved.get(k, now.get(k)) == v, k


# --------------------------------------------------------------------------- #
# I4: a failed publish applies nothing.
# --------------------------------------------------------------------------- #
def test_a_failed_publish_applies_nothing(api, monkeypatch):
    m, client = api
    client.post("/run", json={"persist": False})
    m._start_optimize(budget_evals=15, label="quick", background=False)
    m._OPTIMIZE["result"]["best_overlap"] = 90 if m._load_plan_config().overlap_percent != 90 else 60
    book_store.save_new_order_queue([[[SO2, ITEM_B]]])
    prio, cfg = book_store.load_plan_priority(), book_store.load_plan_config()
    rows = book_store.load_last_applied_schedule()

    def boom(rows):
        raise RuntimeError("store unreachable")
    monkeypatch.setattr(m, "_publish", boom)
    r = client.post("/optimize/apply")
    assert r.status_code == 500, r.text
    assert "store unreachable" in r.text
    assert book_store.load_plan_priority() == prio
    assert book_store.load_plan_config() == cfg
    assert book_store.load_new_order_queue() == [[[SO2, ITEM_B]]]
    assert book_store.load_last_applied_schedule() == rows
    assert m._OPTIMIZE["state"] == "done"                    # still there to retry


# --------------------------------------------------------------------------- #
# C1: an accepted earlier date publishes a plan containing the accepted orders.
# --------------------------------------------------------------------------- #
def _finished_prepone(m, client, so_no, item, qty, target):
    r = client.put("/new-orders/drafts",
                   json={"drafts": [{"so_no": so_no, "item_code": item, "qty": qty}]})
    assert r.status_code == 200, r.text
    r = client.post("/new-orders/prepone", json={"targets": {f"{so_no}\x1f{item}": target}})
    assert r.status_code == 200, r.text
    t0 = time.time()
    while m._OPTIMIZE["state"] == "running" and time.time() - t0 < 30:
        time.sleep(0.05)
    assert m._OPTIMIZE["state"] == "done" and m._OPTIMIZE["kind"] == "quote"


def test_accepting_an_earlier_date_publishes_the_accepted_orders(api):
    m, client = api
    client.post("/run", json={"persist": False})
    _finished_prepone(m, client, "SO-EARLY-1", ITEM_A, 40, "2025-03-10")
    r = client.post("/new-orders/prepone/accept")
    assert r.status_code == 200, r.text
    rows = book_store.load_last_applied_schedule()
    mine = [r for r in rows if "SO-EARLY-1" in r["so_refs"]]
    assert {r["process_name"] for r in mine} >= {"CNC FIRST SIDE", "VMC FIRST SIDE",
                                                 "INSPECTION"}, mine
    # And the plan the floor now sees holds them where the search put them.
    sig = _plan_sig(m)
    for r in mine:
        assert any(x[1] == r["process_name"] and x[2] == r["machine"]
                   and "SO-EARLY-1" in x[0] for x in sig), r


def test_a_preponed_result_compares_against_the_repaired_candidate(api, monkeypatch):
    """`_quote_movement`'s "after" is the candidate published then repaired, drafts
    included, the same helper the Optimize panel uses (one definition)."""
    m, client = api
    client.post("/run", json={"persist": False})
    calls = []
    real = m._repaired_candidate

    def spy(*a, **k):
        calls.append(k.get("extra_orders"))
        return real(*a, **k)
    monkeypatch.setattr(m, "_repaired_candidate", spy)
    _finished_prepone(m, client, "SO-EARLY-2", ITEM_A, 40, "2025-03-10")
    assert calls and all(c and c[0].so_no == "SO-EARLY-2" for c in calls), calls
    assert m._OPTIMIZE["result"]["quote_movement"] is not None


# --------------------------------------------------------------------------- #
# I6: "Go back to standard plan" republishes the standard plan.
# --------------------------------------------------------------------------- #
def test_going_back_to_the_standard_plan_republishes_it(api):
    m, client = api
    client.post("/run", json={"persist": False})
    book_store.save_plan_priority({f"{SO2}\x1f{ITEM_B}": 1, f"{SO1}\x1f{ITEM_A}": 2},
                                  {"saved_at": "x"})
    rows = book_store.load_last_applied_schedule()
    _move_to_other_cnc(rows, lambda r: True)
    m._publish(rows)
    r = client.post("/optimize/clear")
    assert r.status_code == 200, r.text
    assert book_store.load_plan_priority() is None
    setup = m.optimize_service.prepare_contest(
        book_store.load_active_orders(), book_store.load_actuals(),
        m._current_masters(), m._resolve_config(m._load_plan_config()),
        absences=book_store.load_absences(),
        operator_table=book_store.load_operator_table(),
        frozen=book_store.load_frozen_ops(),
        machine_downtime=book_store.load_machine_downtime())
    free, _ = m._all_lines_schedule(setup, setup.masters, None)

    def key(rs):
        return sorted((r["item_code"], r["process_name"], tuple(r["so_refs"]),
                       r["machine"], r["start"]) for r in rs)
    assert key(book_store.load_last_applied_schedule()) == key(
        freeze.schedule_projection(free))


# --------------------------------------------------------------------------- #
# Minors.
# --------------------------------------------------------------------------- #
def test_every_publish_holds_the_publish_lock(api, monkeypatch):
    m, client = api
    held = []
    real = book_store.save_last_applied_schedule

    def spy(rows):
        held.append(m._PUBLISH_LOCK.locked())
        return real(rows)
    monkeypatch.setattr(book_store, "save_last_applied_schedule", spy)
    client.post("/run", json={"persist": False})                       # go-live seed
    r = client.put("/new-orders/drafts",
                   json={"drafts": [{"so_no": "SO-LOCK", "item_code": ITEM_A, "qty": 10}]})
    quote = client.post("/new-orders/quote").json()
    assert client.post("/new-orders/add", json={"stamp": quote["stamp"]}).status_code == 200
    assert len(held) >= 2 and all(held), held


def test_a_date_list_that_cannot_be_worked_out_says_why(api, monkeypatch):
    m, client = api
    client.post("/run", json={"persist": False})

    def boom(*a, **k):
        raise RuntimeError("replay failed")
    monkeypatch.setattr(m.fixed_plan, "date_changes", boom)
    m._start_optimize(budget_evals=15, label="quick", background=False)
    st = client.get("/optimize/status").json()
    assert st["date_changes"] is None
    assert "replay failed" in (st.get("date_changes_error") or "")


def test_a_preponed_result_shows_the_dates_the_floor_gets_after_accept(api):
    """C1 + I2 for Add New Orders: what the "what moved" screen says each existing
    order's date becomes is exactly the date the floor sees once accepted. Uses the
    down-machine book, where the free candidate and its repair really differ."""
    m, client = api
    _half_finished_job_on_a_down_machine(m, client)
    _finished_prepone(m, client, "SO-EARLY-3", ITEM_A, 40, "2025-03-10")
    move = m._OPTIMIZE["result"]["quote_movement"]
    assert move is not None
    before = client.post("/run", json={"persist": False}).json()["expected_end"]
    assert client.post("/new-orders/prepone/accept").status_code == 200
    after = client.post("/run", json={"persist": False}).json()["expected_end"]
    # "moved" lists the orders that finish LATER (`_movers`); every other existing
    # order must not finish later than it does now.
    told = {f"{r['so_no']}\x1f{r['item_code']}": r["after"] for r in move["moved"]}
    for k, v in before.items():
        if k in told:
            assert after[k] == told[k], k
        else:
            assert after[k] <= v, k
    # And its headline total over the existing book is the floor's after accept.
    due = {f"{o.so_no}\x1f{o.item_code}": o.delivery_date
           for o in book_store.load_active_orders().values()}
    late = sum(max(0, (date.fromisoformat(after[k]) - due[k]).days) for k in before)
    assert move["late_days_after"] == late
