"""Ship gate for the symmetric on-time objective (spec 2026-08-06 §6).

Judged on the OWNER'S measures, not on score — the two objectives are not
comparable by score, so only outcomes count:

    orders inside +/-4 days   must NOT fall
    worst single order        must NOT get worse
    at least one              must strictly improve

    python3 scripts/measure_ontime.py Test9.xlsx --budget 400
"""
import argparse
import dataclasses
import sys
from datetime import date

sys.path.insert(0, ".")

from engine import loaders, new_engine, optimizer
from engine.config import Config
from engine.models import PlanRun
from engine.pipeline import run_forward

BAND = 4
SEEDS = (42, 7, 2026)

# PlanConfig is a FROZEN dataclass whose defaults are baked into __init__ at class
# creation, so assigning the class attribute is silently ignored. Override at the one
# construction site in the plan path instead.
_ORIG_PLAN_CONFIG = new_engine._plan_config
_OVERRIDE = {}


def _patched_plan_config(config):
    pc = _ORIG_PLAN_CONFIG(config)
    return dataclasses.replace(pc, **_OVERRIDE) if _OVERRIDE else pc


def _install_patch():
    """From main() only — installing at import would redirect any importing process."""
    new_engine._plan_config = _patched_plan_config


def _use_old_objective():
    """Approximate the pre-2026-08-06 objective by switching the on-time term off and
    restoring the old makespan weight, so the baseline is measured on the same code."""
    optimizer.ONTIME_WEIGHT = 0.0
    optimizer.MAKESPAN_WEIGHT = 40.0
    _OVERRIDE.clear()
    _OVERRIDE.update(ontime_weight=0.0, makespan_weight=40.0)


def _use_new_objective():
    optimizer.ONTIME_WEIGHT = 1.0
    optimizer.MAKESPAN_WEIGHT = 0.1
    _OVERRIDE.clear()
    _OVERRIDE.update(ontime_weight=1.0, makespan_weight=0.1)


def _self_check():
    """Prove the knob reaches the search before any number is trusted."""
    _use_new_objective()
    pc = new_engine._plan_config(Config(plan_start_date=date(2026, 8, 6)))
    assert pc.ontime_weight == 1.0, f"ppc knob is dead: {pc.ontime_weight}"
    _use_old_objective()
    pc = new_engine._plan_config(Config(plan_start_date=date(2026, 8, 6)))
    assert pc.ontime_weight == 0.0, f"ppc knob is dead: {pc.ontime_weight}"
    print("self-check OK: the objective switch reaches the search\n")


def _outcomes(so_lines, masters, cfg, budget, seed):
    res = optimizer.optimize(so_lines, cfg, masters, budget_evals=budget, seed=seed)
    pr = PlanRun(so_lines=so_lines)
    run_forward(pr, cfg, masters, priority_rank=res.ranks)
    m = optimizer.plan_metrics(pr.schedule, so_lines, cfg.plan_start_date)

    due = {(l.so_no, l.item_code): l.delivery_date for l in so_lines}
    expected = {}
    for e in pr.schedule:
        for ref in (e.so_refs or []):
            k = (ref, e.item_code)
            d = e.end.date()
            if k not in expected or d > expected[k]:
                expected[k] = d
    gaps = [(expected[k] - due[k]).days for k in expected if k in due]
    return {
        "on_time": sum(1 for g in gaps if abs(g) <= BAND),
        "late_beyond": sum(1 for g in gaps if g > BAND),
        "early_beyond": sum(1 for g in gaps if g < -BAND),
        "worst": max((g for g in gaps if g > 0), default=0),
        "total_late": m["total_late_days"],
        "makespan": m["makespan_days"],
        "orders": len(gaps),
    }


def _best_of_seeds(so_lines, masters, cfg, budget, label):
    runs = [_outcomes(so_lines, masters, cfg, budget, s) for s in SEEDS]
    for s, r in zip(SEEDS, runs):
        print(f"    seed {s:5d}: on-time {r['on_time']:3d}  worst {r['worst']:3d}  "
              f"late>{BAND} {r['late_beyond']:3d}  early>{BAND} {r['early_beyond']:3d}")
    # "best" = most on time, worst order breaking ties
    best = max(runs, key=lambda r: (r["on_time"], -r["worst"]))
    print(f"  {label:22s} BEST-OF-3: on-time {best['on_time']}/{best['orders']}  "
          f"worst {best['worst']}d  late>{BAND} {best['late_beyond']}  "
          f"early>{BAND} {best['early_beyond']}  (total late-days {best['total_late']}, "
          f"makespan {best['makespan']:.1f})\n")
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("workbook")
    ap.add_argument("--budget", type=int, default=400)
    ap.add_argument("--start", default="2026-08-06")
    args = ap.parse_args()

    _install_patch()
    with open(args.workbook, "rb") as fh:
        new_engine.set_masters_bytes(fh.read())
    so_lines, masters = loaders.load_all(args.workbook)
    cfg = Config(plan_start_date=date.fromisoformat(args.start), scheduler="new",
                 overlap_percent=84, flexible_machines=True,
                 apply_operator_logic=True, consolidation_window_days=10)
    _self_check()
    print(f"{len(so_lines)} SO lines | budget {args.budget} | seeds {SEEDS}\n")

    print("OLD objective (on-time term off, makespan 40)")
    _use_old_objective()
    old = _best_of_seeds(so_lines, masters, cfg, args.budget, "OLD")

    print("NEW objective (symmetric on-time, makespan 0.1 tie-break)")
    _use_new_objective()
    new = _best_of_seeds(so_lines, masters, cfg, args.budget, "NEW")

    print("=" * 70)
    print("SHIP GATE")
    print("=" * 70)
    on_time_ok = new["on_time"] >= old["on_time"]
    worst_ok = new["worst"] <= old["worst"]
    improved = new["on_time"] > old["on_time"] or new["worst"] < old["worst"]
    print(f"  orders inside +/-{BAND} days : {old['on_time']:3d} -> {new['on_time']:3d}   "
          f"{'OK' if on_time_ok else 'FAIL (fell)'}")
    print(f"  worst single order        : {old['worst']:3d} -> {new['worst']:3d}   "
          f"{'OK' if worst_ok else 'FAIL (worse)'}")
    print(f"  at least one improved     : {'OK' if improved else 'FAIL (no change)'}")
    print(f"\n  VERDICT: {'SHIP' if (on_time_ok and worst_ok and improved) else 'DO NOT SHIP'}")
    print(f"\n  (reported, not gated: late>{BAND} {old['late_beyond']} -> {new['late_beyond']}, "
          f"early>{BAND} {old['early_beyond']} -> {new['early_beyond']}, "
          f"makespan {old['makespan']:.1f} -> {new['makespan']:.1f})")


if __name__ == "__main__":
    main()
