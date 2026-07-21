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
  * ``reserved`` (blocked machine/operator intervals — today: operator absences)
    is passed through to every Rule 6 evaluation, so those windows stay clear.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from .pipeline import KEY_SEP
from .rules import rule1_consolidate, rule2_sort_by_date, rule3_tiebreak_process_time, \
    rule6_allocate

# Score = total_late_days + MAKESPAN_WEIGHT x makespan_days. Both owner goals in one
# number. 2026-07-19 (owner: minimize BOTH late days and completion days, after
# rejecting the 79-84 d plans): weight raised 10 -> 40, measured on the live
# 71-order book under the crew-smart scheduler — at 10 the search lands
# 78.4 d / 1327 late-days; at 40 it lands 72.7 d / 1528, which beats the
# previous live optimum (83.6 d / 1531) on BOTH axes; at 80 it trades real
# lateness for speed (69.7 d / 1701+). 40 is the dominance point, not a taste
# choice — re-measure before moving it.
MAKESPAN_WEIGHT = 40.0

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


def _work(batch, masters) -> float:
    r = masters.routings.get(batch.item_code)
    return (r.total_cycle_time() if r else 0.0) * batch.qty


def _atc_key(batch, masters, plan_start):
    """Apparent-Tardiness-Cost flavour: due-date pressure divided by work — the
    best single-pass dispatch rule found on the real book (a strong seed)."""
    slack_days = (batch.so_delivery_date - plan_start).days
    p = max(_work(batch, masters), 1.0)
    return -((1.0 / p) * math.exp(-max(slack_days, 0) / 14.0))


def ranks_for(seq) -> dict:
    """The persistable artifact: every member order of the i-th batch gets rank i+1
    (batch members share a rank; ``apply_priority_rank`` replays by min member rank)."""
    ranks: dict = {}
    for i, b in enumerate(seq):
        for so in (b.source_so_refs or []):
            ranks[f"{so}{KEY_SEP}{b.item_code}"] = i + 1
    return ranks


def optimize(so_lines, config, masters, *, reserved=None, budget_evals=150,
             seed=42, on_progress=None, should_cancel=None) -> OptimizeResult:
    """Search for a better batch sequence for THIS book (rolling: call it on
    whatever the order book holds today; the result is disposable and re-computable).

    "Better" = lower ``score`` (delivery lateness + makespan) — every active line
    competes in one pool (lanes are pure status labels, no scheduling effect).

    ``on_progress(evals_done, best_metrics)`` is called after every evaluation.
    ``should_cancel()`` (optional) is polled between evaluations; when it returns True the
    search stops early and returns the best plan found so far. Exceptions from Rule 6 are
    not caught: a book that cannot plan at all should fail loud exactly like Plan does.
    """
    # The new engine runs its own sequence search + objective; delegate there (so the cloud
    # contest, which calls this per overlap candidate, becomes the new engine's contest).
    if getattr(config, "scheduler", "classic") == "new":
        from engine import new_engine
        return new_engine.optimize_sequence(
            so_lines, config, masters, reserved=reserved, budget_evals=budget_evals,
            seed=seed, on_progress=on_progress, should_cancel=should_cancel)
    config.validate()

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

    from .pipeline import scheduler_for
    _sched_run = scheduler_for(config)

    def evaluate(seq) -> dict:
        nonlocal evals
        sched = _sched_run(list(seq), config=config, masters=masters,
                           reserved=reserved)
        evals += 1
        m = plan_metrics(sched, so_lines, config.plan_start_date)
        if on_progress:
            on_progress(evals, best_m if best_m and score(best_m) <= score(m) else m)
        return m

    best_seq, best_m = None, None      # GLOBAL best across every restart

    def consider(seq, m):
        nonlocal best_seq, best_m
        if best_m is None or score(m) < score(best_m):
            best_seq, best_m = list(seq), m

    # The Rule-3 order is the BASELINE (what we're trying to beat) — evaluate it first.
    baseline_m = evaluate(list(pri0))
    consider(pri0, baseline_m)  # baseline is always feasible -> best_m is set

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
            if score(m) < score(cur_m) or (score(m) == score(cur_m) and rng.random() < 0.5):
                if score(m) < score(cur_m):
                    since_improve = 0
                cur_seq, cur_m = cand, m
                consider(cand, m)
            else:
                since_improve += 1
                if since_improve > _RESTART_AFTER:
                    break            # basin exhausted → next restart from a fresh start
        restart += 1

    return OptimizeResult(
        ranks=ranks_for(best_seq),
        baseline=baseline_m,
        best=best_m,
        evals=evals,
        improved=score(best_m) < score(baseline_m),
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
# 2026-07-19 (crew-smart scheduler): re-measured on the live 71-order book --
# 85/88 dominate (makespan 73.5-73.7 d vs 77.4 at 80, 79+ at 50/60); 50/60
# lost every contest and were dropped, 90/92 measurably worse than 85/88.
OVERLAP_CANDIDATES = (70, 80, 85, 88)

# Flow-mode contest (2026-07-19 flow-scheduler spec): the overlap % has no
# meaning without Rule 6's pacing, so the settings sweep tunes the chunk count
# instead — how many transfer chunks a batch flows between steps in. Measured
# on the live book: 4 best (49.6 d), 6 close, 1 = no pipelining (56.6 d).
# Flow evals are ~1.5s each (5x classic), so a contest must be SHORT enough to
# fit the cloud's 20-min window on 2 vCPU: 2 candidates run in ONE parallel round
# (~13 min wall for 400 evals each), 3+ candidates need a second round and time
# out. Chunk 4 is the reliable winner (43.7 d at depth), 6 the close runner-up;
# 1/2/3 lost every measured contest. So the lineup is exactly (4, 6).
FLOW_CHUNK_CANDIDATES = (4, 6)

# Local-fallback budget when flow times out of the cloud: on Render's 0.1 CPU a
# flow eval is ~15s, so a full 1000-eval search would run for HOURS. Cap it hard
# — a short search still yields the ~50 d / far-fewer-late-days class, a big win
# over classic, without hanging the free instance.
FLOW_LOCAL_BUDGET = 100


def knob_for(config):
    """The ONE setting the sweep contest tunes for this scheduler mode:
    ('flow_chunks', FLOW_CHUNK_CANDIDATES) under the flow scheduler,
    ('overlap_percent', OVERLAP_CANDIDATES) under classic Rule 6."""
    if getattr(config, "scheduler", "classic") == "flow":
        return "flow_chunks", FLOW_CHUNK_CANDIDATES
    return "overlap_percent", OVERLAP_CANDIDATES


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
    """Winner of the (sequence, knob) search. ``result`` is the winning
    candidate's OptimizeResult (its ranks are the persistable artifact).
    ``overlap_percent`` carries the winning KNOB VALUE — the overlap % under
    classic Rule 6, the chunk count under the flow scheduler (``knob`` says
    which; the field keeps its historical name for wire compatibility)."""

    overlap_percent: int = 0            # the winning knob value (see docstring)
    knob: str = "overlap_percent"       # which config field the value belongs to
    result: OptimizeResult = field(default_factory=OptimizeResult)
    table: list = field(default_factory=list)   # per-candidate probe outcomes
    evals: int = 0
    cancelled: bool = False


def sweep_optimize(so_lines, config, masters, *, budget_evals=150, seed=42,
                   on_progress=None, should_cancel=None,
                   candidates=OVERLAP_CANDIDATES, base_reserved=None) -> SweepResult:
    """Search batch sequence AND overlap %. ``budget_evals`` is the TOTAL"""
    # The new engine owns its own search + objective; delegate there when it is the
    # selected scheduler (see engine/new_engine.py). The old sweep below runs only for the
    # retired classic/flow engines.
    if getattr(config, "scheduler", "classic") == "new":
        from engine import new_engine
        return new_engine.sweep_optimize(
            so_lines, config, masters, budget_evals=budget_evals, seed=seed,
            on_progress=on_progress, should_cancel=should_cancel, base_reserved=base_reserved)
    return _sweep_optimize_classic(
        so_lines, config, masters, budget_evals=budget_evals, seed=seed,
        on_progress=on_progress, should_cancel=should_cancel,
        candidates=candidates, base_reserved=base_reserved)


def _sweep_optimize_classic(so_lines, config, masters, *, budget_evals=150, seed=42,
                            on_progress=None, should_cancel=None,
                            candidates=OVERLAP_CANDIDATES, base_reserved=None) -> SweepResult:
    """The original classic/flow overlap-sweep search (see history). ``budget_evals`` is the TOTAL
    budget for the whole contest; it is split EQUALLY across the contenders
    (the current setting + ``candidates``), ``budget_evals // n`` plans each.

    The fair-contest contract (owner decisions, 2026-07-15): every contender
    gets the SAME search depth and the best-scoring plan wins — the current
    setting has no depth advantage. It runs FIRST (an early Stop still leaves
    the user's own setting fully searched) and wins exact ties (no Settings
    churn), nothing more.

    ``base_reserved`` (optional) is merged into EVERY candidate's reservations
    (operator absences — physical unavailability). ``None`` (default) is a no-op.
    """
    from dataclasses import replace
    from engine.optimize_service import merge_reservations

    knob, default_cands = knob_for(config)
    if candidates is OVERLAP_CANDIDATES:
        candidates = default_cands       # caller took the default: follow the mode
    cur = getattr(config, knob)
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
        """One seeded search under overlap ``ov``; returns None if cancelled."""
        nonlocal spent, cancelled
        if budget <= 0 or cancelled or (should_cancel and should_cancel()):
            cancelled = cancelled or bool(should_cancel and should_cancel())
            return None
        cfg = replace(config, **{knob: ov})
        reserved = merge_reservations(None, base_reserved) or None
        res = optimize(so_lines, cfg, masters, reserved=reserved,
                       budget_evals=budget, seed=seed,
                       on_progress=_offset(spent), should_cancel=should_cancel)
        spent += res.evals
        cancelled = cancelled or res.cancelled
        table.append({"overlap": ov, "eligible": True, "best": res.best,
                      "evals": res.evals})
        return res

    def _sc(res):
        """A comparable score, or None when the candidate never ran (cancelled:
        _run returned None)."""
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
        return SweepResult(overlap_percent=cur, knob=knob,
                           result=OptimizeResult(evals=spent, cancelled=cancelled,
                                                 best=None),
                           table=table, evals=spent, cancelled=cancelled)
    return SweepResult(overlap_percent=best_ov, knob=knob, result=best_res,
                       table=table, evals=spent, cancelled=cancelled)
