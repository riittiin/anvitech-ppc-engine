"""Rule 9 — Re-run MRP and regenerate the plan (the closed loop).

After actuals are entered, re-plan from actual-completed + balance-remaining:
  balance(SO line) = original SO qty - qty already produced (>= 0).

Crucially, Rule 9 DELEGATES to Rules 1-7 — it imports ``pipeline.run_forward``
and re-runs the same forward chain with the balance quantities. It never copies
rule logic, so any fix to Rules 1-7 automatically flows into the loop. With no
actuals, the balance equals the original demand, so the re-run reproduces the
original schedule (the reuse test).
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from ..models import PlanRun


def compute_balance_so_lines(original_so_lines, actuals):
    """Return SO lines with qty reduced by GOOD quantity (produced − rejected);
    drop completed lines. Rejected pieces don't fulfil the order, so they remain
    in the balance to be remade."""
    good = defaultdict(float)
    for a in (actuals or []):
        good[(a.so_no, a.item_code)] += a.good_qty()

    balance_lines = []
    for so in original_so_lines:
        done = good.get((so.so_no, so.item_code), 0.0)
        remaining = max(so.qty - done, 0.0)
        if remaining <= 0:
            continue  # fully produced (good) — nothing left to plan
        balance_lines.append(replace(so, qty=remaining))
    return balance_lines


def run(original_so_lines, config=None, notes=None, masters=None, actuals=None, **kw):
    """Re-plan from balance quantities. Returns a dict:
        {"so_lines": balance_lines, "trace": forward_trace}.
    """
    from ..pipeline import run_forward  # local import avoids any import cycle

    notes = notes if notes is not None else []
    balance_lines = compute_balance_so_lines(original_so_lines, actuals)
    notes.append(
        f"Re-planning {len(balance_lines)} SO line(s) from balance "
        f"(of {len(original_so_lines)} original) after {len(actuals or [])} actual(s)"
    )

    plan_run = PlanRun(so_lines=balance_lines)
    trace = run_forward(plan_run, config, masters)
    return {"so_lines": balance_lines, "trace": trace, "plan_run": plan_run}
