"""Fixed plan, version A0 final fix wave (2026-10-07 review).

I2. "Go back to standard plan" publishes a whole-book plan, so it ends the Add New
    Orders arrival queue, exactly as Apply does.
m4. Apply: if the frozen set cannot be saved after the publish, nothing is half
    applied: the previous published plan and frozen set come back, ranks unsaved.
m5. A finished search whose "After" numbers could not be worked out from the plan the
    floor gets says so (it used to keep the search's own numbers silently).
m6/m7. The screens: the reason a date list is unknown is shown; the user lead on the
    Optimize notice waits until the role is known."""
from pathlib import Path

import pytest
from fastapi import HTTPException

from engine import book_store
from tests.new_sample_workbook import ITEM_A, ITEM_B, SO1, SO2
from tests.test_fixed_plan_api import api  # noqa: F401  (fixture)

RANKS = {f"{SO2}\x1f{ITEM_B}": 1, f"{SO1}\x1f{ITEM_A}": 2}
JS = Path("web/app.js").read_text()


def test_going_back_to_the_standard_plan_ends_the_arrival_queue(api):
    m, client = api
    client.post("/run", json={"persist": False})
    book_store.save_plan_priority(RANKS, {"saved_at": "x"})
    book_store.save_new_order_queue([[[SO2, ITEM_B]]])
    assert client.post("/optimize/clear").status_code == 200
    assert book_store.load_new_order_queue() == []


def test_a_failed_go_back_keeps_the_arrival_queue(api, monkeypatch):
    m, client = api
    client.post("/run", json={"persist": False})
    book_store.save_new_order_queue([[[SO2, ITEM_B]]])

    def fail(_rows):
        raise RuntimeError("store down")
    monkeypatch.setattr(m, "_publish", fail)
    with pytest.raises(HTTPException):
        m._optimize_clear()
    assert book_store.load_new_order_queue() == [[[SO2, ITEM_B]]]


def _frozen_save_fails(m, monkeypatch, calls):
    real = book_store.save_frozen_ops

    def flaky(rows):
        calls.append(rows)
        real(rows)
        if len(calls) == 1:   # the new set lands but the store reports a failure
            raise RuntimeError("store hiccup")   # (a timeout); the restore succeeds
    monkeypatch.setattr(book_store, "save_frozen_ops", flaky)


def test_apply_with_a_failed_frozen_save_applies_nothing(api, monkeypatch):
    m, client = api
    client.post("/run", json={"persist": False})
    m._start_optimize(budget_evals=15, label="quick", background=False)
    book_store.save_frozen_ops([{"marker": "old"}])
    rows, meta = book_store.load_last_applied_schedule(), book_store.load_published_meta()
    prio, cfg = book_store.load_plan_priority(), book_store.load_plan_config()
    book_store.save_new_order_queue([[[SO2, ITEM_B]]])
    _frozen_save_fails(m, monkeypatch, calls := [])
    r = client.post("/optimize/apply")
    assert r.status_code == 500, r.text
    assert "Nothing was changed" in r.text
    assert book_store.load_last_applied_schedule() == rows
    assert book_store.load_published_meta() == meta
    assert book_store.load_frozen_ops() == [{"marker": "old"}]
    assert book_store.load_plan_priority() == prio
    assert book_store.load_plan_config() == cfg
    assert book_store.load_new_order_queue() == [[[SO2, ITEM_B]]]
    assert m._OPTIMIZE["state"] == "done"                    # still there to retry


def test_go_back_with_a_failed_frozen_save_changes_nothing(api, monkeypatch):
    m, client = api
    client.post("/run", json={"persist": False})
    book_store.save_plan_priority(RANKS, {"saved_at": "x"})
    book_store.save_frozen_ops([{"marker": "old"}])
    rows = book_store.load_last_applied_schedule()
    _frozen_save_fails(m, monkeypatch, [])
    with pytest.raises(HTTPException) as e:
        m._optimize_clear()
    assert e.value.status_code == 500 and "Nothing was changed" in e.value.detail
    assert book_store.load_last_applied_schedule() == rows
    assert book_store.load_frozen_ops() == [{"marker": "old"}]
    assert book_store.load_plan_priority()["ranks"] == RANKS


def test_numbers_that_cannot_be_worked_out_say_why(api, monkeypatch):
    m, client = api
    client.post("/run", json={"persist": False})

    def boom(*a, **k):
        raise RuntimeError("metrics failed")
    monkeypatch.setattr(m, "_candidate_metrics", boom)
    m._start_optimize(budget_evals=15, label="quick", background=False)
    st = client.get("/optimize/status").json()
    assert "metrics failed" in (st.get("metrics_error") or "")


def test_numbers_worked_out_carry_no_error(api):
    m, client = api
    client.post("/run", json={"persist": False})
    m._start_optimize(budget_evals=15, label="quick", background=False)
    st = client.get("/optimize/status").json()
    assert st.get("metrics_error") is None


def test_the_screen_shows_why_numbers_or_dates_are_unknown():
    body = JS.split("function renderOptimizeResult(st)")[1].split("\nfunction ")[0]
    seg = body.split("st.date_changes === null")[1][:400]
    assert "st.date_changes_error" in seg
    assert "st.metrics_error" in body


def test_the_user_lead_waits_for_the_role():
    assert "let currentRole = null;" in JS
    body = JS.split("function renderFixedPlanAlerts(alerts)")[1].split("\nfunction ")[0]
    assert 'currentRole === "user"' in body and 'currentRole !== "admin"' not in body
