"""Fixed plan, Task 9 Part D: three corrections from the fix-wave re-review.

1. "Go back to standard plan" publishes an EMPTY plan only when the book really has
   nothing to plan; any other error raises and changes nothing.
2. It builds everything, publishes, and only then forgets the ranks: a failed
   publish changes nothing (as Apply does).
3. A finished search with no job order to apply reports the plan the floor would get
   (the repaired candidate) in its numbers, the same plan its date list comes from."""
import pytest
from fastapi import HTTPException

from engine import book_store
from tests.new_sample_workbook import ITEM_A, ITEM_B, SO1, SO2
from tests.test_fixed_plan_api import api  # noqa: F401  (fixture)

RANKS = {f"{SO2}\x1f{ITEM_B}": 1, f"{SO1}\x1f{ITEM_A}": 2}


def _seed(m, client):
    client.post("/run", json={"persist": False})
    book_store.save_plan_priority(RANKS, {"saved_at": "x"})
    return book_store.load_last_applied_schedule()


def test_an_empty_book_goes_back_to_an_empty_published_plan(api):
    m, client = api
    _seed(m, client)
    book_store.delete_all()
    assert client.post("/optimize/clear").status_code == 200
    assert book_store.load_last_applied_schedule() == []
    assert book_store.load_plan_priority() is None


def test_any_other_error_is_raised_and_nothing_changes(api, monkeypatch):
    m, client = api
    rows = _seed(m, client)

    def broken(*a, **kw):
        raise ValueError("a routing could not be read")
    monkeypatch.setattr(m, "_repaired_candidate", broken)
    with pytest.raises(ValueError):
        m._optimize_clear()
    assert book_store.load_last_applied_schedule() == rows
    assert book_store.load_plan_priority()["ranks"] == RANKS


def test_a_failed_publish_forgets_nothing(api, monkeypatch):
    m, client = api
    rows = _seed(m, client)

    def fail(_rows):
        raise RuntimeError("store down")
    monkeypatch.setattr(m, "_publish", fail)
    with pytest.raises(HTTPException) as e:
        m._optimize_clear()
    assert e.value.status_code == 500 and "Nothing was changed" in e.value.detail
    assert book_store.load_plan_priority()["ranks"] == RANKS
    assert book_store.load_last_applied_schedule() == rows


def test_a_result_with_no_job_order_reports_the_plan_the_floor_gets(api):
    m, client = api
    client.post("/run", json={"persist": False})
    m._start_optimize(budget_evals=15, label="quick", background=False)
    cfg = m._load_plan_config()
    job = m._OPTIMIZE["job_id"]
    ov = m._OPTIMIZE["result"]["best_overlap"]
    flex = m._OPTIMIZE["result"]["flexible_machines"]
    free_number = {"total_late_days": 98765, "makespan_days": 1.0}
    m._finalize_optimize(job, cfg, m._OPTIMIZE.get("baseline"), "quick",
                         winner_overlap=ov, winner_flexible=flex, ranks={},
                         best=dict(free_number), evals=1, table=[], cancelled=False)
    best = m._OPTIMIZE["result"]["best"]
    cand = m._repaired_candidate(None, m._candidate_config(ov, flex))
    assert best["total_late_days"] == m._candidate_metrics(cand)["total_late_days"]
    assert best["total_late_days"] != free_number["total_late_days"]
