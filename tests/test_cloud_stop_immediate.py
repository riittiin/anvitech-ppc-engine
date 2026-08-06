"""Stop & keep best must END a cloud run immediately and keep what arrived.

Bug (2026-07-15, hit live 2026-08-06): the cloud wait loop read `cancel` every two
seconds but only acted on it INSIDE `if timed_out:`, so Stop did nothing until the
40-minute deadline and then produced no plan. In the live incident GitHub allocated
17 of 20 requested runners; the 3 that never started could never report, so the
collector's all-arrived condition could never be met, and 17 delivered shard results
sat with nothing willing to finalize them.

These tests pin the fix: cancel salvages whatever arrived, ends cleanly when nothing
did, and NEVER falls back to a local search.
"""
import pytest

pytest.importorskip("fastapi")


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _seed(m, *, shards, job_id="job-1", finalizing=False):
    """_OPTIMIZE as a running cloud job carrying `shards`."""
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(state="running", job_id=job_id, mode="cloud",
                           cancel=True, cloud_failed=False,
                           shards=dict(shards), shard_total=20,
                           shards_finalizing=finalizing, error=None)


def test_cancel_with_shards_finalizes_from_them(monkeypatch):
    """The headline case: 17 of 20 arrived, Stop must build a plan from them."""
    m = _api()
    _seed(m, shards={i: {"rows": [], "evals": 840} for i in range(17)})
    calls = []

    def _fake_finalize(job_id):
        calls.append(job_id)
        with m._OPTIMIZE_LOCK:          # a real finalize ends the job
            m._OPTIMIZE.update(state="done", cancel=False)
    monkeypatch.setattr(m, "_finalize_from_shards", _fake_finalize)

    m._cancel_cloud_job("job-1")

    assert calls == ["job-1"], "must salvage the arrived shards"
    assert m._OPTIMIZE["state"] == "done"


def test_cancel_with_no_shards_ends_cleanly(monkeypatch):
    """Owner ruling: Stop with nothing back ends the run and starts NOTHING."""
    m = _api()
    _seed(m, shards={})
    calls = []
    monkeypatch.setattr(m, "_finalize_from_shards", lambda j: calls.append(j))

    m._cancel_cloud_job("job-1")

    assert calls == [], "nothing to salvage — must not call finalize"
    assert m._OPTIMIZE["state"] == "failed"
    assert m._OPTIMIZE["cancel"] is False
    assert "stopped" in (m._OPTIMIZE["error"] or "").lower()


def test_cancel_never_leaves_cloud_failed_for_the_watchdog(monkeypatch):
    """The subtle one. _finalize_from_shards sets cloud_failed when the merged
    shards yield no eligible winner, and the watchdog reads that flag to START A
    LOCAL SEARCH. Under cancel that would turn Stop into 'begin a fresh 15-minute
    computation', which the owner explicitly rejected."""
    m = _api()
    _seed(m, shards={0: {"rows": [], "evals": 840}})

    def _fake_finalize(job_id):
        with m._OPTIMIZE_LOCK:          # merge found no winner: still running
            m._OPTIMIZE["cloud_failed"] = True
    monkeypatch.setattr(m, "_finalize_from_shards", _fake_finalize)

    m._cancel_cloud_job("job-1")

    assert m._OPTIMIZE["state"] == "failed", "must end, not linger for the watchdog"
    assert m._OPTIMIZE["cloud_failed"] is False, "must not trigger a local fallback"


def test_cancel_does_not_double_finalize(monkeypatch):
    """`shards_finalizing` is an atomic claim shared with the shard collector. If
    the collector already claimed it, cancel must not finalize a second time."""
    m = _api()
    _seed(m, shards={0: {"rows": [], "evals": 840}}, finalizing=True)
    calls = []
    monkeypatch.setattr(m, "_finalize_from_shards", lambda j: calls.append(j))

    m._cancel_cloud_job("job-1")

    assert calls == [], "the collector already owns the finalize"


def test_cancel_on_an_already_finished_job_is_a_noop(monkeypatch):
    """A cancel racing a normal completion must not clobber the finished result."""
    m = _api()
    _seed(m, shards={0: {"rows": [], "evals": 840}})
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(state="done", error=None)
    calls = []
    monkeypatch.setattr(m, "_finalize_from_shards", lambda j: calls.append(j))

    m._cancel_cloud_job("job-1")

    assert calls == []
    assert m._OPTIMIZE["state"] == "done", "a finished job must stay finished"


def test_cancel_for_a_different_job_id_is_a_noop(monkeypatch):
    """Stale cancel from a superseded run must not touch the current one."""
    m = _api()
    _seed(m, shards={0: {"rows": [], "evals": 840}}, job_id="job-CURRENT")
    calls = []
    monkeypatch.setattr(m, "_finalize_from_shards", lambda j: calls.append(j))

    m._cancel_cloud_job("job-OLD")

    assert calls == []
    assert m._OPTIMIZE["state"] == "running", "the current job must be untouched"
