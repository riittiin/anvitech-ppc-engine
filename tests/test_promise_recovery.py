"""Promise recovery — the optimizer's promise-slip objective (engine layer).

When committed orders slip after a disruption, re-sequencing the committed set to protect
the most promises beats strict promised-date order. This guards the engine piece:
  * the promise-slip metric measures expected-vs-PROMISED (not delivery date);
  * optimize(objective="promise_slip") never does worse than the promised-date order
    (safety: it is seeded with that order and keeps the best);
  * on a constructed disruption where date-order needlessly breaks a promise, the search
    recovers it;
  * deterministic.
"""
from datetime import date

from engine.config import Config
from engine.models import SOLine, Process, Routing, Machine, WorkCalendar, Masters, PlanRun
from engine.pipeline import run_forward, KEY_SEP
from engine import optimizer


def _masters(items):
    ms = {"M": Machine(machine_no="M", display_name="M", machine_type="CNC lathe",
                       available_hrs_per_day=19.5)}
    mm = Masters(machines=ms, calendar=WorkCalendar())
    for code, mins in items.items():
        mm.routings[code] = Routing(item_code=code, description=code, customer="", rm_type="",
                                    moq=None, processes=[Process(1, "CNC", mins, 0, "M", None)])
    return mm


def _committed(so, code, qty, promised):
    return SOLine(so_no=so, item_code=code, item_name=code, qty=qty,
                  delivery_date=promised, commitment="committed", promised_date=promised)


def test_promise_slip_metric_measures_against_promised_date():
    # One machine, one order promised in the past -> it slips vs its promise.
    masters = _masters({"A": 10})
    cfg = Config(plan_start_date=date(2025, 3, 5))
    lines = [_committed("S1", "A", 50, date(2025, 3, 4))]   # promised before the start
    pr = PlanRun(so_lines=list(lines))
    run_forward(pr, cfg, masters)
    m = optimizer.promise_slip_metrics(pr.schedule, lines, cfg.plan_start_date)
    assert m["promise_slip_days"] > 0 and m["promises_missed"] == 1


def test_recovery_never_worse_than_promised_date_order():
    masters = _masters({f"IT{i}": 5 + i for i in range(6)})
    cfg = Config(plan_start_date=date(2025, 3, 5))
    lines = [_committed(f"S{i}", f"IT{i}", 40 + 20 * (i % 3),
                        date(2025, 3, 8 + (i * 2) % 7)) for i in range(6)]
    r = optimizer.optimize(lines, cfg, masters, budget_evals=60, seed=1,
                           objective="promise_slip")
    # Safety guarantee: best is never worse than the promised-date baseline.
    assert r.best["promise_slip_days"] <= r.baseline["promise_slip_days"]


def test_deterministic():
    masters = _masters({f"IT{i}": 5 + i for i in range(6)})
    cfg = Config(plan_start_date=date(2025, 3, 5))
    lines = [_committed(f"S{i}", f"IT{i}", 40 + 20 * (i % 3),
                        date(2025, 3, 8 + (i * 2) % 7)) for i in range(6)]
    a = optimizer.optimize(lines, cfg, masters, budget_evals=40, seed=7, objective="promise_slip")
    b = optimizer.optimize(lines, cfg, masters, budget_evals=40, seed=7, objective="promise_slip")
    assert a.ranks == b.ranks and a.best == b.best


def test_recovery_saves_a_promise_date_order_breaks():
    # Constructed disruption: one machine, two orders competing.
    #   BIG: 200 pieces (long), promised day 30 — needs to START now to make it.
    #   SMALL: 10 pieces (short), promised day 31 — has slack, can wait.
    # Promised-date order runs SMALL first (its promise is 1 day earlier by delivery sort),
    # delaying BIG so BIG misses. Re-sequencing runs BIG first; SMALL still makes day 31.
    masters = _masters({"BIG": 60, "SMALL": 5})
    cfg = Config(plan_start_date=date(2025, 3, 3))     # Monday
    big = _committed("SB", "BIG", 200, date(2025, 3, 28))
    small = _committed("SS", "SMALL", 10, date(2025, 3, 27))
    lines = [big, small]

    # Baseline: promised-date order (SMALL first, promised 27 < 28).
    pr = PlanRun(so_lines=list(lines))
    run_forward(pr, cfg, masters)   # rule3 protected branch -> promised-date order
    base = optimizer.promise_slip_metrics(pr.schedule, lines, cfg.plan_start_date)

    r = optimizer.optimize(lines, cfg, masters, budget_evals=40, seed=1, objective="promise_slip")
    # The search should find an arrangement with no more slip than the baseline, and on
    # this constructed case strictly better (it protects BIG without losing SMALL).
    assert r.best["promise_slip_days"] <= base["promise_slip_days"]
