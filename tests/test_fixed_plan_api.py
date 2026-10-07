"""Fixed plan through the API (2026-10-06 spec)."""
import importlib
from datetime import date

import pytest
from fastapi.testclient import TestClient

from engine import book_store
from engine.models import Order
from tests.new_sample_workbook import ITEM_A, ITEM_B, SO1, SO2, build_new_sample_bytes


def _api_with_new_engine(monkeypatch):
    monkeypatch.setenv("DEFAULT_SCHEDULER", "new")
    import api.main as m
    importlib.reload(m)
    return m


@pytest.fixture
def api(monkeypatch):
    m = _api_with_new_engine(monkeypatch)
    book_store.save_masters_bytes(build_new_sample_bytes())
    book_store.add_orders([Order(SO1, ITEM_A, ITEM_A, 50, date(2025, 3, 20)),
                           Order(SO2, ITEM_B, ITEM_B, 100, date(2025, 3, 21))])
    client = TestClient(m.app)
    client.post("/login", data={"username": "anvitech", "password": "1930rail"})
    client.get("/operators")
    m._OPT_BUDGETS = {"quick": 15, "deep": 15}
    monkeypatch.delenv("GITHUB_DISPATCH_TOKEN", raising=False)
    monkeypatch.delenv("OPTIMIZE_WORKER_SECRET", raising=False)
    return m, client


def test_first_plan_seeds_the_published_plan_once(api):
    m, client = api
    # A stale snapshot from before go-live must be REPLACED, not adopted.
    book_store.save_last_applied_schedule([{"batch_id": "B1", "item_code": ITEM_A,
        "process_seq": 1, "process_name": "CNC FIRST SIDE", "machine": "CNC2",
        "operator": "", "start": "2020-01-01T08:00:00", "end": "2020-01-01T09:00:00",
        "so_refs": [SO1]}])
    assert book_store.load_published_meta() == {}
    client.post("/run", json={"persist": False})
    rows = book_store.load_last_applied_schedule()
    assert rows and book_store.load_published_meta().get("at")
    assert all(r["start"] != "2020-01-01T08:00:00" for r in rows)
    client.post("/run", json={"persist": False})
    assert book_store.load_last_applied_schedule() == rows


def test_apply_snapshots_the_plan_at_the_winning_settings(api):
    """Apply used to snapshot with the OLD overlap/flexible settings (written to the
    config only afterwards), so the published plan was not the plan approved."""
    m, client = api
    m._start_optimize(budget_evals=15, label="quick", background=False)
    # Force winning settings that differ from the saved ones so this discriminates.
    cfg = m._load_plan_config()
    knob = m._OPTIMIZE["result"].get("knob") or m.optimizer.knob_for(cfg)[0]
    cur = getattr(cfg, knob)
    m._OPTIMIZE["result"]["best_overlap"] = 90 if cur != 90 else 60
    m._OPTIMIZE["result"]["flexible_machines"] = not cfg.flexible_machines
    m._optimize_apply()
    rows = book_store.load_last_applied_schedule()
    # The plan approved = the FREE plan of the applied ranks at the settings now on
    # file (the winning ones). Not `_plan`'s output: since Task 5 every `_plan` is a
    # repair of the published plan, which keeps machines and order but may re-time.
    from engine import freeze
    setup = m.optimize_service.prepare_contest(
        book_store.load_active_orders(), book_store.load_actuals(),
        m._current_masters(), m._resolve_config(m._load_plan_config()),
        absences=book_store.load_absences(),
        operator_table=book_store.load_operator_table(),
        frozen=book_store.load_frozen_ops(),
        machine_downtime=book_store.load_machine_downtime())
    sched, _ = m._all_lines_schedule(setup, setup.masters,
                                     book_store.load_plan_priority()["ranks"])
    assert {(r["item_code"], r["process_name"], r["machine"], r["start"]) for r in rows} == \
        {(r["item_code"], r["process_name"], r["machine"], r["start"])
         for r in freeze.schedule_projection(sched)}


# --------------------------------------------------------------------------- #
# Task 5: every plan is a repair; Done only repairs.
# --------------------------------------------------------------------------- #
def _machines(m):
    """(machine per published step, the schedule) of a FRESH plan."""
    from engine import freeze
    m._PLAN_CACHE["key"] = None
    m._plan(m._load_plan_config())
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    out = {}
    for r in freeze.schedule_projection(sched):
        out.setdefault((r["item_code"], r["process_name"], tuple(r["so_refs"])),
                       set()).add(r["machine"])
    return out, sched


def test_done_never_starts_a_contest_and_keeps_machines(api, monkeypatch):
    m, client = api
    client.post("/run", json={"persist": False})               # seeds the published plan
    before, _ = _machines(m)
    starts = []
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: starts.append(1))
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    book_store.save_plan_priority({f"{SO2}\x1f{ITEM_B}": 1, f"{SO1}\x1f{ITEM_A}": 2},
                                  {"saved_at": "x"})            # a different job order
    r = client.post("/optimize/done")
    assert r.status_code == 200 and r.json()["started"] is False
    assert starts == []
    after, _ = _machines(m)
    assert after == before
    note = book_store.load_auto_note()["text"]
    assert "keep their machines" in note


def test_done_refreshes_the_frozen_set(api, monkeypatch):
    m, client = api
    client.post("/run", json={"persist": False})
    called = []
    real = m._compute_and_store_frozen
    monkeypatch.setattr(m, "_compute_and_store_frozen", lambda: called.append(1) or real())
    client.post("/optimize/done")
    assert called == [1]


def test_done_keeps_the_arrival_queue(api, monkeypatch):
    """Only Apply / an accepted earlier date end the queue now; Done never does."""
    m, client = api
    client.post("/run", json={"persist": False})
    monkeypatch.setattr(m, "_start_optimize", lambda *a, **k: None)
    monkeypatch.setenv("AUTO_OPTIMIZE", "1")
    book_store.save_new_order_queue([[[SO2, ITEM_B]]])
    client.post("/optimize/done")
    assert book_store.load_new_order_queue() == [[[SO2, ITEM_B]]]


def test_user_role_can_press_done(api):
    m, client = api
    u = TestClient(m.app)
    u.post("/login", data={"username": "anvitech_user", "password": "anvitech12345678"})
    assert u.post("/optimize/done").status_code == 200


def test_a_break_entered_after_publishing_raises_the_banner(api):
    m, client = api
    client.post("/run", json={"persist": False})
    rows = book_store.load_last_applied_schedule()
    mid = next(r["machine"] for r in rows)
    first = min(r["start"] for r in rows if r["machine"] == mid)[:10]
    r = client.post("/machine-downtime", json={"machine": mid, "from_date": first,
                                               "to_date": first, "reason": "service"})
    assert r.status_code == 200, r.text
    alerts = client.post("/run", json={"persist": False}).json()["fixed_plan_alerts"]
    assert any(a.startswith(f"{mid} is marked down") for a in alerts), alerts


def test_a_break_already_on_file_when_publishing_raises_no_banner(api):
    """The converse: the published plan already accounted for that break, so the
    admin is not told to Optimize for it (the publish records its id)."""
    m, client = api
    client.post("/run", json={"persist": False})
    rows = book_store.load_last_applied_schedule()
    mid = next(r["machine"] for r in rows)
    first = min(r["start"] for r in rows if r["machine"] == mid)[:10]
    r = client.post("/machine-downtime", json={"machine": mid, "from_date": first,
                                               "to_date": first, "reason": "service"})
    assert r.status_code == 200, r.text
    m._publish(rows)                                    # republished with the break on file
    alerts = client.post("/run", json={"persist": False}).json()["fixed_plan_alerts"]
    assert not any(a.startswith(f"{mid} is marked down") for a in alerts), alerts


def test_published_plan_is_in_the_cache_key(api):
    m, client = api
    client.post("/run", json={"persist": False})
    fp = m._plan_fingerprint(m._load_plan_config())
    rows = book_store.load_last_applied_schedule()
    rows[0]["machine"] = rows[0]["machine"] + "X"
    book_store.save_last_applied_schedule(rows)
    assert m._plan_fingerprint(m._load_plan_config()) != fp


def test_an_empty_book_still_records_that_the_plan_was_published(api):
    """Controller ruling 1: with nothing to plan the seed still writes its meta, or
    every call would re-seed and bust the plan cache."""
    m, client = api
    book_store.delete_all()
    client.post("/run", json={"persist": False})
    assert book_store.load_published_meta().get("at")
    assert book_store.load_last_applied_schedule() == []
    calls = []
    real = m._plan
    m._plan = lambda *a, **k: calls.append(k) or real(*a, **k)
    try:
        m._ensure_published_plan()
    finally:
        m._plan = real
    assert calls == []


def _add_new_order(client, so_no, item, qty):
    r = client.put("/new-orders/drafts",
                   json={"drafts": [{"so_no": so_no, "item_code": item, "qty": qty}]})
    assert r.status_code == 200, r.text
    quote = client.post("/new-orders/quote").json()
    added = client.post("/new-orders/add", json={"stamp": quote["stamp"]})
    assert added.status_code == 200, added.text


def _move_to_other_cnc(rows, pred):
    """Republish the matching CNC SECOND SIDE rows (options CNC1/CNC2) on the OTHER
    machine and first in its queue: a plan the free planner would not make."""
    moved = None
    for r in rows:
        if r["process_name"] == "CNC SECOND SIDE" and pred(r):
            r["machine"] = "CNC2" if r["machine"] == "CNC1" else "CNC1"
            r["start"] = "2000-01-01T00:00:00"
            moved = r["machine"]
    assert moved
    return moved


def test_a_new_order_keeps_the_machine_its_quote_gave_it(api):
    m, client = api
    client.post("/run", json={"persist": False})
    new_so = "SO-NEW-1"
    _add_new_order(client, new_so, ITEM_B, 10)
    rows = [r for r in book_store.load_last_applied_schedule() if new_so in r["so_refs"]]
    assert rows, "the new order's steps were not added to the published plan"
    pinned = {(r["process_name"], r["machine"]) for r in rows}
    client.post("/optimize/done")
    now, _ = _machines(m)
    got = {(k[1], mid) for k, mids in now.items() if new_so in k[2] for mid in mids}
    assert pinned <= got


def test_a_queued_new_order_is_held_to_its_published_machine(api):
    """Stage 2 (the arrival queue) is a repair too: a queued new order keeps the
    published machine even where its own free plan would pick another."""
    m, client = api
    client.post("/run", json={"persist": False})
    new_so = "SO-NEW-2"
    _add_new_order(client, new_so, ITEM_B, 10)
    assert book_store.load_new_order_queue()
    rows = book_store.load_last_applied_schedule()
    moved = _move_to_other_cnc(rows, lambda r: new_so in r["so_refs"])
    book_store.save_last_applied_schedule(rows)
    client.post("/optimize/done")
    now, _ = _machines(m)
    assert now[(ITEM_B, "CNC SECOND SIDE", (new_so,))] == {moved}


def test_every_plan_holds_a_published_machine_the_free_planner_would_not_pick(api):
    m, client = api
    client.post("/run", json={"persist": False})
    rows = book_store.load_last_applied_schedule()
    moved = _move_to_other_cnc(rows, lambda r: True)
    m._publish(rows)
    client.post("/optimize/done")
    now, _ = _machines(m)
    assert now[(ITEM_B, "CNC SECOND SIDE", (SO2,))] == {moved}


def test_a_break_entered_before_a_new_order_still_raises_the_banner(api):
    """Add New Orders appends to the published plan WITHOUT re-stamping the known
    machine breaks, or a break entered earlier would silently lose its banner."""
    m, client = api
    client.post("/run", json={"persist": False})
    rows = book_store.load_last_applied_schedule()
    mid = next(r["machine"] for r in rows)
    first = min(r["start"] for r in rows if r["machine"] == mid)[:10]
    r = client.post("/machine-downtime", json={"machine": mid, "from_date": first,
                                               "to_date": first, "reason": "service"})
    assert r.status_code == 200, r.text
    _add_new_order(client, "SO-NEW-3", ITEM_A, 10)
    alerts = client.post("/run", json={"persist": False}).json()["fixed_plan_alerts"]
    assert any(a.startswith(f"{mid} is marked down") for a in alerts), alerts


def test_the_plan_in_force_is_measured_as_the_repaired_plan(api):
    """`_incumbent_metrics` (the Optimize panel's "Now", the auto-apply gate) must
    measure the plan the floor sees, i.e. the repair of the published plan, not a
    free re-plan of the applied ranks."""
    m, client = api
    client.post("/run", json={"persist": False})
    rows = book_store.load_last_applied_schedule()
    _move_to_other_cnc(rows, lambda r: True)
    m._publish(rows)
    m._PLAN_CACHE["key"] = None
    m._plan(m._load_plan_config())
    art = m._PLAN_CACHE["artifacts"]
    shown = m.optimizer.plan_metrics(art["plan_run"].schedule, art["so_lines"],
                                     art["config"].plan_start_date)
    free = m._metrics_for_ranks(None)
    assert free["makespan_days"] != shown["makespan_days"]     # the case discriminates
    assert m._incumbent_metrics()["makespan_days"] == shown["makespan_days"]


def test_a_new_order_that_cannot_be_pinned_is_saved_and_said_so(api, monkeypatch):
    """Fix round 1 (I1): pinning used to fail silently, leaving the order to drift
    from its quote on every repair with nobody told."""
    m, client = api
    client.post("/run", json={"persist": False})
    r = client.put("/new-orders/drafts",
                   json={"drafts": [{"so_no": "SO-NEW-9", "item_code": ITEM_A, "qty": 10}]})
    assert r.status_code == 200, r.text
    quote = client.post("/new-orders/quote").json()

    def boom(*a, **k):
        raise RuntimeError("store unreachable")
    monkeypatch.setattr(m, "_pin_new_orders", boom)
    added = client.post("/new-orders/add", json={"stamp": quote["stamp"]})
    assert added.status_code == 200, added.text
    assert ("SO-NEW-9", ITEM_A) in book_store.load_active_orders()
    warning = added.json().get("warning") or ""
    assert "SO-NEW-9" in warning and "store unreachable" in warning
    note = (book_store.load_auto_note() or {}).get("text", "")
    assert "SO-NEW-9" in note and "Optimize" in note


def test_optimize_result_lists_moved_delivery_dates(api):
    m, client = api
    client.post("/run", json={"persist": False})
    m._start_optimize(budget_evals=15, label="quick", background=False)
    st = client.get("/optimize/status").json()
    assert "date_changes" in st
    now = client.post("/run", json={"persist": False}).json()["expected_end"]
    for row in st["date_changes"]:
        assert row["now"] == now[f"{row['so']}\x1f{row['item']}"]
        assert row["days"] != 0


def test_listed_dates_are_exactly_what_the_floor_gets_after_apply(api):
    """'After' must be the candidate published-then-repaired, so the list approved is
    the plan applied. Forces a result whose dates really move so this is not vacuous."""
    m, client = api
    # Big lines so jobs take days and contend: the 50/100-piece sample finishes in one
    # day whatever the sequence, which would make this test vacuous.
    book_store.add_orders([Order("NSO-003", ITEM_A, ITEM_A, 4000, date(2025, 3, 22)),
                           Order("NSO-004", ITEM_B, ITEM_B, 6000, date(2025, 3, 23))])
    client.post("/run", json={"persist": False})
    m._start_optimize(budget_evals=15, label="quick", background=False)
    cfg = m._load_plan_config()
    ranks = {k: -v for k, v in m._OPTIMIZE["result"]["ranks"].items()}  # a different winner
    now = client.post("/run", json={"persist": False}).json()["expected_end"]
    assert m.fixed_plan.date_changes(now, m._candidate_dates(ranks, 95, True))
    chosen = (95, True, None)
    ov, flex, _ = chosen
    # Re-finalize through the real path so the stored list is produced by the code.
    m._OPTIMIZE["result"]["best_overlap"] = ov
    m._OPTIMIZE["result"]["flexible_machines"] = flex
    job = m._OPTIMIZE["job_id"]
    m._finalize_optimize(job, cfg, m._OPTIMIZE.get("baseline"), "quick",
                         winner_overlap=ov, winner_flexible=flex, ranks=ranks,
                         best=m._OPTIMIZE.get("best"), evals=1, table=[], cancelled=False)
    listed = client.get("/optimize/status").json()["date_changes"]
    assert listed, "list must be non-empty when dates move"
    m._optimize_apply()
    after = client.post("/run", json={"persist": False}).json()["expected_end"]
    keys = {f"{r['so']}\x1f{r['item']}" for r in listed}
    for r in listed:
        assert after[f"{r['so']}\x1f{r['item']}"] == r["after"]
    for k, v in now.items():
        if k not in keys:
            assert after[k] == v


def test_a_new_order_is_placed_after_every_published_job(api):
    """A repair places jobs in the published plan's own order (`placed`). Stage 2
    numbers its placements from 0 again, so the new lines must be appended BEHIND
    every published job, exactly as the quote planned them (Task 6b)."""
    m, client = api
    client.post("/run", json={"persist": False})
    before = book_store.load_last_applied_schedule()
    assert before and all(isinstance(r.get("placed"), int) for r in before)
    # A published plan numbered higher than today's plan would number it (e.g. an
    # Optimize applied when the book was bigger): the offset must come from it.
    for r in before:
        r["placed"] += 1000
    book_store.save_last_applied_schedule(before)
    top = max(r["placed"] for r in before)
    _add_new_order(client, "SO-NEW-7", ITEM_B, 10)
    rows = book_store.load_last_applied_schedule()
    new = [r for r in rows if "SO-NEW-7" in r["so_refs"]]
    assert new and min(r["placed"] for r in new) > top
    assert rows[:len(before)] == before


def test_queued_new_orders_are_numbered_after_the_book_in_the_merged_plan(api):
    """`_plan` merges stage 1 and each queued accept; their placement numbers must
    not collide, or a plan published from the merged schedule (the go-live seed with
    a queue on file) would interleave new jobs with the book on the next repair."""
    from engine import freeze
    m, client = api
    client.post("/run", json={"persist": False})
    _add_new_order(client, "SO-NEW-8", ITEM_A, 10)
    m._PLAN_CACHE["key"] = None
    client.post("/run", json={"persist": False})
    sched = m._PLAN_CACHE["artifacts"]["plan_run"].schedule
    rows = freeze.schedule_projection(sched)
    old = [r["placed"] for r in rows if "SO-NEW-8" not in r["so_refs"]]
    new = [r["placed"] for r in rows if "SO-NEW-8" in r["so_refs"]]
    assert new and old and min(new) > max(old)
