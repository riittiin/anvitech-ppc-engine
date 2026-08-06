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
import time
from datetime import date

import pytest

pytest.importorskip("fastapi")

from engine import book_store
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B

SECRET = "test-worker-secret"


def _api():
    import importlib
    import api.main as m
    importlib.reload(m)
    return m


def _cloud_env(monkeypatch, timeout_min="5"):
    """Cloud env with the Oracle claim window off (0) — the loop-level tests
    below drive the WAIT loop, not the claim window (that's already covered
    in tests/test_optimize_cloud.py)."""
    monkeypatch.setenv("GITHUB_DISPATCH_TOKEN", "fake-token")
    monkeypatch.setenv("OPTIMIZE_WORKER_SECRET", SECRET)
    monkeypatch.setenv("OPTIMIZE_CLOUD_TIMEOUT_MIN", timeout_min)
    monkeypatch.setenv("ORACLE_CLAIM_TIMEOUT_MIN", "0")


def _seed_book_for_contest():
    book_store.save_masters_bytes(build_sample_bytes())
    book_store.add_orders([
        Order("SO1", ITEM_A, ITEM_A, 10, date(2025, 3, 20)),
        Order("SO2", ITEM_B, ITEM_B, 15, date(2025, 3, 21)),
    ])


def _wait(m, state, timeout=15.0):
    """Poll _optimize_status() until it reaches `state`. Loop-level tests use
    this instead of driving the loop's internals directly, so they exercise
    the real cloud_job() thread wired in api.main, not a re-implementation
    of it."""
    t0 = time.time()
    st = m._optimize_status()
    while time.time() - t0 < timeout:
        st = m._optimize_status()
        if st["state"] == state:
            return st
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for state={state}; last={st}")


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
    assert m._OPTIMIZE["state"] == "running", (
        "must NOT write a terminal state — that would make the collector's "
        "in-flight _finalize_from_shards a no-op and discard its shards")
    assert m._OPTIMIZE["error"] is None


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


def test_cancel_losing_the_claim_leaves_state_intact_for_the_real_finalize():
    """Regression for the race the mocks hide: if the collector already claimed the
    finalize (shards non-empty, in flight), a cancel arriving in that window must not
    write any terminal state — `_finalize_from_shards` checks `state == "running"` on
    entry, so a cancel that clobbered it here would turn the collector's real finalize
    into a silent no-op and discard every shard it was about to merge."""
    m = _api()
    _seed(m, shards={i: {"rows": [], "evals": 840} for i in range(20)},
          finalizing=True)

    m._cancel_cloud_job("job-1")

    # Untouched: the real _finalize_from_shards (not called here) would itself
    # return early unless state is still "running".
    with m._OPTIMIZE_LOCK:
        assert m._OPTIMIZE["state"] == "running", (
            "the real _finalize_from_shards returns early unless state is 'running'")


def test_cancel_ends_a_latched_no_winner_job_instead_of_orphaning_it():
    """_finalize_from_shards's no-winner branch leaves shards_finalizing set, clears
    shards, sets cloud_failed, and waits for a later watchdog poll to fall back to
    local. A cancel arriving in that window must END the job: the caller stops polling
    right after, so returning silently would leave it 'running' forever."""
    m = _api()
    _seed(m, shards={}, finalizing=True)
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE["cloud_failed"] = True      # the latched sentinel

    m._cancel_cloud_job("job-1")

    assert m._OPTIMIZE["state"] == "failed", "must not leave the job orphaned"
    assert m._OPTIMIZE["cloud_failed"] is False, "and must not trigger a local run"


# ---------------------------------------------------------------------------
# Loop-level tests: drive the REAL cloud_job() wait loop inside _start_optimize
# (not _cancel_cloud_job directly, which every test above already covers in
# isolation). These pin the WIRING — the `if was_cancelled:` trigger sitting
# before `if timed_out:`, and its `continue`-back-into-the-loop behaviour —
# so the wiring itself has coverage independent of the helper it calls.
# ---------------------------------------------------------------------------

def test_loop_cancel_only_reaches_terminal_promptly_and_never_goes_local(monkeypatch):
    """A Stop with the cloud never having answered (long timeout, so the
    deadline is nowhere close) must still end the run promptly, and `mode`
    must never become "local" — Stop must never start a fresh computation."""
    _cloud_env(monkeypatch, timeout_min="5")
    m = _api()
    _seed_book_for_contest()
    monkeypatch.setattr(m, "_dispatch_workflow", lambda cloud, job_id: True)

    st = m._start_optimize(budget_evals=15, label="deep", background=True)
    assert st["state"] == "running" and st["mode"] == "cloud"

    time.sleep(0.3)
    m._optimize_cancel()

    st = _wait(m, "failed", timeout=10.0)
    assert st["mode"] != "local", "cancel must never trigger a local search"
    assert "stopped" in (st["error"] or "").lower()


def test_loop_cancel_and_timeout_same_poll_prefers_cancel(monkeypatch):
    """The exact race Finding/§3 of the spec calls out: cancel and the deadline
    both true on the SAME poll must take the cancel path, not the timeout ->
    local-fallback path. A short timeout puts the deadline well behind the
    first 2s poll tick; cancel is set immediately, before that first tick."""
    _cloud_env(monkeypatch, timeout_min="0.005")   # ~0.3s, expires before poll 1
    m = _api()
    _seed_book_for_contest()
    monkeypatch.setattr(m, "_dispatch_workflow", lambda cloud, job_id: True)

    st = m._start_optimize(budget_evals=15, label="deep", background=True)
    assert st["state"] == "running" and st["mode"] == "cloud"
    m._optimize_cancel()          # set well before the first poll fires

    st = _wait(m, "failed", timeout=10.0)
    assert st["mode"] != "local", (
        "cancel racing the deadline in the same poll must never start a local run")
    assert "stopped" in (st["error"] or "").lower()


def test_loop_timeout_without_cancel_still_falls_back_to_local(monkeypatch):
    """Regression guard on the path this task must NOT change: a timeout with
    no cancel involved still falls back to a local recompute, exactly as
    before the wiring change."""
    _cloud_env(monkeypatch, timeout_min="0.005")   # ~0.3s
    m = _api()
    _seed_book_for_contest()
    monkeypatch.setattr(m, "_dispatch_workflow", lambda cloud, job_id: True)

    m._start_optimize(budget_evals=15, label="deep", background=True)
    st = _wait(m, "done", timeout=30.0)
    assert st["mode"] == "local" and st["best"]


def test_loop_cancel_while_in_flight_does_not_wedge_forever(monkeypatch):
    """Finding 1 regression guard. `_cancel_cloud_job` deliberately returns
    WITHOUT a terminal state when another party is genuinely mid-finalize
    (shards present + shards_finalizing already claimed) — it assumes that
    party's own finalize will end the job. `_finalize_from_shards`'s no-winner
    branch breaks that assumption: it clears `shards` and leaves the job
    waiting for a LATER poll to notice, rather than ending it itself. Before
    the fix, the wait loop `return`ed unconditionally after calling
    `_cancel_cloud_job`, so nothing was left polling and the job wedged in
    state="running" forever. This drives the REAL loop end-to-end: seed the
    in-flight state, cancel, let (at least) one poll pass untouched, then
    simulate the in-flight party finishing (mirroring the no-winner branch)
    and confirm the loop's own next poll — not a second thread — finishes
    the job."""
    _cloud_env(monkeypatch, timeout_min="5")   # deadline irrelevant here
    m = _api()
    _seed_book_for_contest()
    monkeypatch.setattr(m, "_dispatch_workflow", lambda cloud, job_id: True)

    st = m._start_optimize(budget_evals=15, label="deep", background=True)
    assert st["state"] == "running" and st["mode"] == "cloud"

    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(shards={0: {"rows": [], "evals": 840}},
                           shards_finalizing=True)
    m._optimize_cancel()

    # Let at least the first 2s poll see the in-flight state and (correctly)
    # decline to write a terminal state.
    time.sleep(3.0)
    with m._OPTIMIZE_LOCK:
        assert m._OPTIMIZE["state"] == "running", (
            "must not clobber an in-flight party's own finalize")

    # The in-flight party finishes (mirrors _finalize_from_shards's no-winner
    # branch): clears shards, sets cloud_failed, leaves shards_finalizing
    # latched — nothing but this wait loop's own next poll is watching now.
    with m._OPTIMIZE_LOCK:
        m._OPTIMIZE.update(shards={}, cloud_failed=True)

    st = _wait(m, "failed", timeout=10.0)
    assert st["mode"] != "local", "a stopped run must never start a local search"
    with m._OPTIMIZE_LOCK:
        assert m._OPTIMIZE["cloud_failed"] is False, (
            "must not leave cloud_failed set for a watchdog that no longer exists")
