"""Rule 9 — rerun MRP. The key guarantee: it DELEGATES to Rules 1-7.

Reuse test: re-running with no actuals (balance == original demand) reproduces
the original forward schedule exactly. This proves Rule 9 calls Rules 1-7 rather
than duplicating them.
"""
from datetime import date

from engine.config import Config
from engine.models import PlanRun, Actual
from engine.pipeline import run_forward, to_table
from engine.rules import rule9_rerun_mrp


def test_reuse_zero_actuals_reproduces_original(loaded):
    so_lines, masters = loaded
    cfg = Config()

    original = run_forward(PlanRun(so_lines=so_lines), cfg, masters)
    result = rule9_rerun_mrp.run(so_lines, config=cfg, masters=masters, actuals=[])
    rerun = result["trace"]

    # Same schedule table => Rule 9 reused Rules 1-7.
    assert original["rule6"]["output"] == rerun["rule6"]["output"]
    assert original["rule3"]["output"] == rerun["rule3"]["output"]


def test_balance_reduces_quantity(loaded):
    so_lines, masters = loaded
    # Produce 4 of SO214 (item 61240807-01, original qty 10) -> balance 6.
    actuals = [Actual("24-25SO214", "61240807-01", date(2025, 3, 7), qty_produced=4)]
    balance = rule9_rerun_mrp.compute_balance_so_lines(so_lines, actuals)
    so214 = next(s for s in balance if s.so_no == "24-25SO214")
    assert so214.qty == 6


def test_completed_line_is_dropped(loaded):
    so_lines, masters = loaded
    actuals = [Actual("24-25SO214", "61240807-01", date(2025, 3, 7), qty_produced=10)]
    balance = rule9_rerun_mrp.compute_balance_so_lines(so_lines, actuals)
    assert not any(s.so_no == "24-25SO214" for s in balance)


def test_rejected_pieces_stay_in_balance(loaded):
    # Produce 10 but reject 3 -> only 7 good -> balance keeps the line at qty 3.
    so_lines, masters = loaded
    actuals = [Actual("24-25SO214", "61240807-01", date(2025, 3, 7),
                      qty_produced=10, qty_rejected=3)]
    balance = rule9_rerun_mrp.compute_balance_so_lines(so_lines, actuals)
    so214 = next(s for s in balance if s.so_no == "24-25SO214")
    assert so214.qty == 3  # 10 SO qty − 7 good
