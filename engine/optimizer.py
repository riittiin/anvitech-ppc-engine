"""The Optimize feature's sequence search (pure; no state, no UI, no I/O).

Rule 6 is a greedy, single-pass, non-delay scheduler: it builds exactly ONE plan,
and whichever operation is ready first claims the machine — so the same unlucky
orders can lose every near-race for weeks (measured on the real book: 723
order-days of queueing, 74% of it waiting for CNC/VMC). The batch sequence fed
into Rule 6 is worth days of makespan by itself, and one full plan evaluates in
well under a second — so instead of trusting one greedy pass, this module SEARCHES:
try a sequence, replay the unchanged Rule 6, score the plan, keep the best.

Design contract (see the 2026-07-13 optimize-plan spec):
  * Reuses the unchanged Rules 1→2→3 (once) and Rule 6 (per evaluation) — never
    duplicates rule logic.
  * Deterministic: same so_lines + config + masters + budget + seed → identical
    result, on any machine (budget counts EVALUATIONS, not wall-clock).
  * The result is a rank per composite order key "<SO No>\x1f<Item Code>";
    ``pipeline.apply_priority_rank`` replays it, and replaying reproduces exactly
    the metrics reported here (the "what you Apply is what you get" guarantee).
  * ``reserved`` (the two-pass Plan's committed-pass reservations) is passed
    through to every Rule 6 evaluation, so committed orders are never disturbed.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .pipeline import KEY_SEP
from .rules import rule1_consolidate, rule2_sort_by_date, rule3_tiebreak_process_time, \
    rule6_allocate

# Score = total_late_days + MAKESPAN_WEIGHT x makespan_days. Both owner goals in one
# number, delivery gaps dominant (a day of one order's lateness trades 1:10 against
# a day of everyone-finishes-earlier makespan).
MAKESPAN_WEIGHT = 10.0

# Local-search shape. Multi-start iterated local search: each restart hill-climbs a
# fresh random permutation until it has gone this many evaluations with no improvement,
# then a new restart explores a different basin. Smaller = more diverse restarts,
# larger = deeper per-basin refinement; tuned on the real book for the best final plan.
_RESTART_AFTER = 60        # non-improving evaluations before a fresh restart


@dataclass
class OptimizeResult:
    """What the search found. ``ranks`` is the persistable artifact."""

    ranks: dict = field(default_factory=dict)   # "<so>\x1f<item>" -> 1-based rank
    baseline: dict = field(default_factory=dict)  # metrics of the Rule-3 order
    best: dict = field(default_factory=dict)      # metrics of the best sequence
    evals: int = 0
    improved: bool = False
    cancelled: bool = False   # True when stopped early via should_cancel (best-so-far kept)


def score(metrics: dict) -> float:
    """Lower is better (open-order objective: delivery lateness + makespan)."""
    return metrics["total_late_days"] + MAKESPAN_WEIGHT * metrics["makespan_days"]


def promise_score(metrics: dict) -> float:
    """Lower is better (promise-recovery objective: promise-slip dominant, broken-promise
    count as the tiebreak — matches the disruption experiment)."""
    return metrics["promise_slip_days"] * 100 + metrics["promises_missed"]


def plan_metrics(schedule, so_lines, plan_start) -> dict:
    """Owner-facing quality of one plan: makespan + lateness vs SO delivery dates.

    Each order (SO#, item) is judged by its OWN delivery date — a consolidated
    batch's schedule entries carry every member's so_ref, so members due earlier
    are correctly measured against their earlier date.
    """
    from datetime import datetime
    due = {(l.so_no, l.item_code): l.delivery_date for l in so_lines}
    expected: dict = {}
    last_end = None
    for e in schedule:
        if last_end is None or e.end > last_end:
            last_end = e.end
        d = e.end.date()
        for ref in (e.so_refs or []):
            k = (ref, e.item_code)
            if k not in expected or d > expected[k]:
                expected[k] = d
    t0 = datetime.combine(plan_start, datetime.min.time())
    makespan = ((last_end - t0).total_seconds() / 86400.0) if last_end else 0.0
    gaps = [(expected[k] - due[k]).days for k in expected if k in due]
    late = [g for g in gaps if g > 0]
    return {
        "makespan_days": round(makespan, 2),
        "late_orders": len(late),
        "total_late_days": int(sum(late)),
        "max_late_days": int(max(late)) if late else 0,
        "orders": len(gaps),
    }


def promise_slip_metrics(schedule, so_lines, plan_start) -> dict:
    """Promise-recovery quality: how far committed orders finish PAST THEIR PROMISE
    (``SOLine.promised_date``), not their SO delivery date. Orders with no promise are
    ignored. Carries the standard fields too so results display consistently."""
    from datetime import datetime
    promised = {(l.so_no, l.item_code): l.promised_date
                for l in so_lines if getattr(l, "promised_date", None) is not None}
    expected: dict = {}
    last_end = None
    for e in schedule:
        if last_end is None or e.end > last_end:
            last_end = e.end
        d = e.end.date()
        for ref in (e.so_refs or []):
            k = (ref, e.item_code)
            if k not in expected or d > expected[k]:
                expected[k] = d
    t0 = datetime.combine(plan_start, datetime.min.time())
    makespan = ((last_end - t0).total_seconds() / 86400.0) if last_end else 0.0
    slips = [(expected[k] - promised[k]).days for k in promised if k in expected]
    missed = [s for s in slips if s > 0]
    return {
        "makespan_days": round(makespan, 2),
        "promise_slip_days": int(sum(missed)),
        "promises_missed": len(missed),
        "max_slip_days": int(max(missed)) if missed else 0,
        "promised_orders": len(slips),
        # standard aliases so downstream display code that reads late_orders/total_late_days
        # still works when it renders a promise-recovery result:
        "late_orders": len(missed),
        "total_late_days": int(sum(missed)),
        "max_late_days": int(max(missed)) if missed else 0,
        "orders": len(slips),
    }


def _work(batch, masters) -> float:
    r = masters.routings.get(batch.item_code)
    return (r.total_cycle_time() if r else 0.0) * batch.qty


def _atc_key(batch, masters, plan_start):
    """Apparent-Tardiness-Cost flavour: due-date pressure divided by work — the
    best single-pass dispatch rule found on the real book (a strong seed)."""
    slack_days = (batch.so_delivery_date - plan_start).days
    p = max(_work(batch, masters), 1.0)
    return -((1.0 / p) * math.exp(-max(slack_days, 0) / 14.0))


def promise_ceiling_ok(schedule, so_lines) -> bool:
    """The owner's law (spec 2026-07-15): no committed/urgent order may END
    after its promised date. Day-level: end.date() <= promised. Orders without
    a promise are never vetoed. FAIL CLOSED: a promised order with NO schedule
    entries at all (e.g. Rule 6 blocked its batch non-fatally) is a violation —
    an unschedulable committed order must never pass the veto as "on time"."""
    promised = {(l.so_no, l.item_code): l.promised_date for l in so_lines
                if getattr(l, "commitment", "open") in ("committed", "urgent")
                and getattr(l, "promised_date", None)}
    if not promised:
        return True
    ends = {}
    for e in schedule:
        for r in (e.so_refs or []):
            k = (r, e.item_code)
            if k in promised:
                d = e.end.date()
                if k not in ends or d > ends[k]:
                    ends[k] = d
    return all(k in ends and ends[k] <= promised[k] for k in promised)


def ranks_for(seq) -> dict:
    """The persistable artifact: every member order of the i-th batch gets rank i+1
    (batch members share a rank; ``apply_priority_rank`` replays by min member rank)."""
    ranks: dict = {}
    for i, b in enumerate(seq):
        for so in (b.source_so_refs or []):
            ranks[f"{so}{KEY_SEP}{b.item_code}"] = i + 1
    return ranks


def optimize(so_lines, config, masters, *, reserved=None, budget_evals=150,
             seed=42, on_progress=None, should_cancel=None,
             objective="lateness", feasible=None) -> OptimizeResult:
    """Search for a better batch sequence for THIS book (rolling: call it on
    whatever the order book holds today; the result is disposable and re-computable).

    ``objective`` selects what "better" means:
      * ``"lateness"`` (default) — open-order optimization: delivery lateness + makespan.
      * ``"promise_slip"`` — promise recovery: minimise how far COMMITTED orders finish
        past their ``promised_date`` (used after a disruption; see the promise-recovery
        design). Same search, different scorecard.

    ``feasible(schedule) -> bool`` (optional; default ``None`` = no gate, byte-identical
    to before this parameter existed) vetoes a candidate plan outright — its score
    becomes ``inf`` and it can never become (or stay) the search's best, regardless of
    its lateness/makespan numbers. If every candidate evaluated is infeasible, the
    search has no plan to offer: it returns an empty ``OptimizeResult`` with
    ``best=None`` and no ranks. See ``promise_ceiling_ok`` for the promise-protection use.

    ``on_progress(evals_done, best_metrics)`` is called after every evaluation.
    ``should_cancel()`` (optional) is polled between evaluations; when it returns True the
    search stops early and returns the best plan found so far. Exceptions from Rule 6 are
    not caught: a book that cannot plan at all should fail loud exactly like Plan does.
    """
    config.validate()
    _metrics = promise_slip_metrics if objective == "promise_slip" else plan_metrics
    _score = promise_score if objective == "promise_slip" else score

    batches = rule1_consolidate.run(list(so_lines), config=config, masters=masters)
    batches = rule2_sort_by_date.run(batches, config=config, masters=masters)
    pri0 = rule3_tiebreak_process_time.run(batches, config=config, masters=masters)
    n = len(pri0)
    if n == 0:
        return OptimizeResult()

    evals = 0
    cancelled = [False]

    def _stop() -> bool:
        if cancelled[0]:
            return True
        if should_cancel and should_cancel():
            cancelled[0] = True
        return cancelled[0]

    def _sc(m) -> float:
        """Score of a metrics dict, or inf for a vetoed (None) candidate — inf
        never beats a finite score, so an infeasible plan can never win."""
        return _score(m) if m is not None else float("inf")

    def evaluate(seq) -> dict:
        nonlocal evals
        sched = rule6_allocate.run(list(seq), config=config, masters=masters,
                                   reserved=reserved)
        evals += 1
        m = None if (feasible is not None and not feasible(sched)) \
            else _metrics(sched, so_lines, config.plan_start_date)
        if on_progress:
            on_progress(evals, best_m if best_m and _sc(best_m) <= _sc(m) else m)
        return m

    best_seq, best_m = None, None      # GLOBAL best across every restart (feasible only)

    def consider(seq, m):
        nonlocal best_seq, best_m
        if m is not None and (best_m is None or _score(m) < _score(best_m)):
            best_seq, best_m = list(seq), m

    # The Rule-3 order is the BASELINE (what we're trying to beat) — evaluate it first.
    baseline_m = evaluate(list(pri0))
    consider(pri0, baseline_m)

    # Restart starting points: the strong dispatch heuristics first (SPT, ATC), then an
    # unlimited stream of fresh random permutations. A single trajectory from one seed
    # gets stuck in whatever local optimum it first descends into; independent restarts
    # from DIVERSE starts explore different basins and keep the global best, which
    # reliably finds better plans than one long run. Deterministic: every RNG is seeded.
    def _starts():
        yield sorted(pri0, key=lambda b: _work(b, masters))                       # SPT
        yield sorted(pri0, key=lambda b: _atc_key(b, masters, config.plan_start_date))  # ATC
        k = 0
        while True:
            s = list(pri0)
            random.Random(seed * 1000 + k).shuffle(s)
            k += 1
            yield s

    gen = _starts()
    restart = 0
    while evals < budget_evals and n >= 2 and not _stop():
        rng = random.Random(seed + 100000 + restart)   # per-restart move RNG
        cur_seq = list(next(gen))
        cur_m = evaluate(cur_seq)
        consider(cur_seq, cur_m)
        since_improve = 0
        # Hill-climb this restart until it stalls (then a fresh restart escapes it).
        while evals < budget_evals and not _stop():
            r = rng.random()
            cand = list(cur_seq)
            if r < 0.5 or n < 4:                                    # insertion
                i, j = rng.randrange(n), rng.randrange(n)
                b = cand.pop(i)
                cand.insert(j, b)
            elif r < 0.8:                                           # swap
                i, j = rng.randrange(n), rng.randrange(n)
                cand[i], cand[j] = cand[j], cand[i]
            else:                                                   # 3-batch block move
                i, j = rng.randrange(n - 3), rng.randrange(n - 3)
                blk = cand[i:i + 3]
                del cand[i:i + 3]
                cand[j:j] = blk
            m = evaluate(cand)
            if _sc(m) < _sc(cur_m) or (_sc(m) == _sc(cur_m) and rng.random() < 0.5):
                if _sc(m) < _sc(cur_m):
                    since_improve = 0
                cur_seq, cur_m = cand, m
                consider(cand, m)
            else:
                since_improve += 1
                if since_improve > _RESTART_AFTER:
                    break            # basin exhausted → next restart from a fresh start
        restart += 1

    if best_m is None:      # every evaluated candidate was vetoed — no plan to offer
        return OptimizeResult(evals=evals, cancelled=cancelled[0], best=None)

    return OptimizeResult(
        ranks=ranks_for(best_seq),
        baseline=baseline_m if baseline_m is not None else {},
        best=best_m,
        evals=evals,
        improved=_score(best_m) < _sc(baseline_m),
        cancelled=cancelled[0],
    )


# --------------------------------------------------------------------------- #
# Settings sweep — auto-tune the overlap % inside the same Optimize click
# (2026-07-15 spec; contract rewritten the same day after a live regression
# and two owner decisions). The rule is the simplest fair one: EVERY overlap
# candidate gets the SAME search depth and the best plan wins, full stop. The
# current setting runs first (an early Stop still leaves it the most-searched)
# and wins exact ties (no Settings churn) — but it has no depth advantage.
#
# Budget (owner cap, 2026-07-15): ONE button, ~1,000 plans total (~40 min on
# the 0.1-CPU Render instance), split EQUALLY across the contenders. The
# candidate list holds only settings that have ever been competitive: overlap
# 90/100 ranked last or next-to-last in every measured contest on both real
# books (9 tables; matches the 2026-07-13 finding that high overlap adds
# nothing once the sequence is optimized), so they are out — but a current
# setting outside the list is always added as a contender, so no saved config
# is ever excluded from its own contest. Measured vs the 6×400 full contest
# (2,400 plans): identical winner+plan on Test6@15-07, same winner within 16
# late-days on Test5@15-07, and on Test6@11-07 it picks 713 late-d/39.7 d
# where full depth picks 717/36.8 — equal-or-near outcomes at 42% compute.
# --------------------------------------------------------------------------- #
OVERLAP_CANDIDATES = (50, 60, 70, 80)


def sweep_contenders(current_overlap=None, candidates=OVERLAP_CANDIDATES):
    """The contest lineup: the current setting first (Stop-safety + tie
    privilege), then the remaining candidates in ascending order."""
    vals = [v for v in sorted(dict.fromkeys(candidates)) if v != current_overlap]
    return ([current_overlap] if current_overlap is not None else []) + vals


def sweep_total_evals(budget_evals, current_overlap=None,
                      candidates=OVERLAP_CANDIDATES):
    """Total evaluations one ``sweep_optimize`` call spends: the budget split
    equally across the contenders (integer division — the remainder is left
    unspent). The API sizes the progress display with this."""
    n = len(sweep_contenders(current_overlap, candidates))
    return (budget_evals // n) * n if n else 0


@dataclass
class SweepResult:
    """Winner of the (sequence, overlap) search. ``result`` is the winning
    candidate's OptimizeResult (its ranks are the persistable artifact)."""

    overlap_percent: int = 0            # the winning overlap
    result: OptimizeResult = field(default_factory=OptimizeResult)
    table: list = field(default_factory=list)   # per-candidate probe outcomes
    evals: int = 0
    cancelled: bool = False


def sweep_optimize(so_lines, config, masters, *, budget_evals=150, seed=42,
                   on_progress=None, should_cancel=None,
                   candidate_setup=None, candidates=OVERLAP_CANDIDATES,
                   feasible=None, base_reserved=None) -> SweepResult:
    """Search batch sequence AND overlap %. ``budget_evals`` is the TOTAL
    budget for the whole contest; it is split EQUALLY across the contenders
    (the current setting + ``candidates``), ``budget_evals // n`` plans each.

    The fair-contest contract (owner decisions, 2026-07-15): every contender
    gets the SAME search depth and the best-scoring plan wins — the current
    setting has no depth advantage. It runs FIRST (an early Stop still leaves
    the user's own setting fully searched) and wins exact ties (no Settings
    churn), nothing more. (History: a probe-then-deepen shape searched the
    current setting at half depth and returned 753 late-days on the real book
    where the plain button found 713 — unequal depths misrank settings; equal
    depths cannot. A cheap-rank-then-deepen shape was measured and rejected
    too: a 100-eval ranking picks the wrong winner 2 times in 3.)

    ``candidate_setup(cfg) -> (reserved, eligible)`` is the API hook for promise
    protection: build the committed pass's reservations under ``cfg`` and say
    whether this overlap keeps every promise at least as well as the current one.
    ``None`` (all-open books, tests) → ``(None, True)`` for every candidate.

    ``feasible`` (optional) is forwarded unchanged to every ``optimize()`` call
    (the one-pool joint-search veto — see ``optimizer.promise_ceiling_ok``). A
    candidate where every evaluated plan is infeasible comes back with
    ``best=None``; such a candidate can never win (see the winner loop below).
    If EVERY candidate is infeasible, the sweep returns an empty result
    (``result.best is None``), same as "nothing eligible ran".

    ``base_reserved`` (optional) is merged into EVERY candidate's reservations
    (operator absences — physical unavailability, not a promise reservation,
    so it applies regardless of ``candidate_setup``/``feasible``). With
    ``candidate_setup=None`` (joint mode) this is simply ``base_reserved``.
    ``None`` (default, all existing callers) is a no-op merge.
    """
    from dataclasses import replace
    from engine.optimize_service import merge_reservations

    cur = config.overlap_percent
    lineup = sweep_contenders(cur, candidates)
    others = lineup[1:]
    each = budget_evals // len(lineup)

    spent = 0
    cancelled = False
    table = []

    def _offset(base):
        if on_progress is None:
            return None
        def cb(evals, best_m):
            on_progress(base + evals, best_m)
        return cb

    def _run(ov, budget):
        """One seeded search under overlap ``ov``; returns None if vetoed/cancelled."""
        nonlocal spent, cancelled
        if budget <= 0 or cancelled or (should_cancel and should_cancel()):
            cancelled = cancelled or bool(should_cancel and should_cancel())
            return None
        cfg = replace(config, overlap_percent=ov)
        reserved, eligible = candidate_setup(cfg) if candidate_setup else (None, True)
        if not eligible:
            table.append({"overlap": ov, "eligible": False})
            return None
        reserved = merge_reservations(reserved, base_reserved) or None
        res = optimize(so_lines, cfg, masters, reserved=reserved,
                       budget_evals=budget, seed=seed, feasible=feasible,
                       on_progress=_offset(spent), should_cancel=should_cancel)
        spent += res.evals
        cancelled = cancelled or res.cancelled
        table.append({"overlap": ov, "eligible": True, "best": res.best,
                      "evals": res.evals})
        return res

    def _sc(res):
        """A comparable score, or None for "no plan to offer" — either the
        candidate never ran (guard-rejected/cancelled: _run returned None) or
        every evaluated plan inside it was vetoed by ``feasible`` (best=None)."""
        return score(res.best) if (res is not None and res.best is not None) else None

    # The current setting runs FIRST (an early Stop still leaves the user's
    # own setting fully searched), then every other contender gets the SAME
    # depth — a fair contest, no depth advantage for anyone.
    best_ov, best_res = cur, _run(cur, each)
    best_score = _sc(best_res)
    for ov in others:
        r = _run(ov, each)
        r_score = _sc(r)
        if r_score is None:
            continue
        # Best plan wins; an exact tie keeps the current setting (no churn).
        if best_score is None or r_score < best_score:
            best_ov, best_res, best_score = ov, r, r_score

    if best_score is None:      # every contender vetoed/cancelled/infeasible
        return SweepResult(overlap_percent=cur,
                           result=OptimizeResult(evals=spent, cancelled=cancelled,
                                                 best=None),
                           table=table, evals=spent, cancelled=cancelled)
    return SweepResult(overlap_percent=best_ov, result=best_res, table=table,
                       evals=spent, cancelled=cancelled)
