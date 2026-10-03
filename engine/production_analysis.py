"""Monthly production analysis — pure, reporting-only (never touches the plan).

Reproduces the owner's "Format Production analysis" workbook, Sheet1: the green
input columns B..S (Rate, T, deliberately left out for now) and the four orange
result columns U..X. One row per Daily Entry punch, exactly as the sheet has one
row per line typed. The formulas are the sheet's own, with ONE owner-directed
change (2026-10-03): operator efficiency deducts the ACTUAL setting time (L),
not the standard one (K), so a setup that overruns its standard never counts
against the operator's production pace.

    G  cycle time (min/piece)       Item's Process Master, snapshotted on the punch
    H  minutes available in shift   typed on Daily Entry (defaults to the shift length)
    I  actual OK qty                qty produced − qty rejected
    J  rejected qty
    L  actual setting time (min)
    M..R  no power / no operator / machine breakdown / tool problem /
          other work / no load (min)

    U  Planned qty            = H / G
    V  Total actual qty       = I + J
    W  Overall productivity   = V / U
    X  Operator efficiency    = V / ((H − L − M − N − O − P − Q − R) / G)

"Qty produced" on a punch is ALL pieces made — the order book has always
counted good = produced − rejected — so I = produced − rejected and V = produced.

A result whose formula has nothing to divide by (no cycle time, no minutes, or
no time left after setting and downtime) is None, shown as "-", never 0: a zero
would read as "the operator produced nothing".

Pure: everything comes from the parameters — no storage, no wall clock.
"""
from __future__ import annotations

from .loaders import normalize_process_name

# Sheet1 row-6 headers, in sheet order (B..S, then U..X). Column T (Rate) is left
# out on the owner's instruction. These are the contract for the preview table
# and the xlsx download.
INPUT_COLUMNS = (
    "Date", "Machine", "Shift", "Operator", "Item description",
    "Cycle time in Min", "Minutes available in shift",
    "Actual OK Qty", "Rejected qty",
    "Std. setting time in Min", "Actual setting time in Min",
    "No power (in Min)", "No operator (in Min)", "Machine Breakdown (in Min)",
    "Tool problem (in Min)", "Other work done (in Min)", "No Load (in Min)",
    "Remarks",
)
RESULT_COLUMNS = (
    "Planned qty", "Total Actual Qty",
    "Overall Productivity (Planned vs actual Qty)", "Operator efficiency",
)
REPORT_COLUMNS = INPUT_COLUMNS + RESULT_COLUMNS


def cycle_time_for(masters, item_code, process_name):
    """Standard cycle time (min/piece) for an (item, process) from the Item's
    Process Master, or None when there is none (no routing, no process of that
    name — matched NORMALIZED, the same key the order book uses — or a blank /
    non-numeric cycle time)."""
    routings = getattr(masters, "routings", None) or {}
    routing = routings.get(item_code)
    if routing is None:
        return None
    target = normalize_process_name(process_name)
    for p in routing.processes:
        if normalize_process_name(p.name) == target:
            ct = p.cycle_time
            return float(ct) if isinstance(ct, (int, float)) and ct > 0 else None
    return None


def _ratio(num, den):
    return num / den if den and den > 0 else None


def metrics(cycle_time, minutes_available, ok_qty, rejected_qty,
            actual_setup_min, downtime_min):
    """The four orange columns for one punch: (planned, total_actual,
    productivity, efficiency). Productivity and efficiency are FRACTIONS
    (0.636 = 63.6%), as the sheet's cells are. Any of them may be None."""
    g = cycle_time if cycle_time and cycle_time > 0 else None
    h = minutes_available or 0.0
    total_actual = (ok_qty or 0.0) + (rejected_qty or 0.0)          # V = I + J
    planned = _ratio(h, g) if g else None                           # U = H / G
    productivity = _ratio(total_actual, planned)                    # W = V / U
    available = h - (actual_setup_min or 0.0) - (downtime_min or 0.0)
    possible = _ratio(available, g) if g else None                  # (H − L − M..R) / G
    efficiency = _ratio(total_actual, possible)                     # X
    return planned, total_actual, productivity, efficiency


def actual_metrics(a, masters=None):
    """`metrics` for a stored Actual. The cycle time is the one snapshotted on
    the punch; a punch saved before snapshots existed falls back to the Process
    Master (when given)."""
    g = getattr(a, "cycle_time_min", None)
    if g is None and masters is not None:
        g = cycle_time_for(masters, a.item_code, a.process)
    ok = max((a.qty_produced or 0.0) - (a.qty_rejected or 0.0), 0.0)
    return metrics(g, getattr(a, "shift_minutes", 0.0), ok, a.qty_rejected,
                   a.actual_setup_min, a.total_downtime_min()), g


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def report_row(a, masters=None):
    """One sheet row for one punch. Productivity/efficiency are percentages
    (63.6, not 0.636) here — the xlsx writer converts back to fractions."""
    (planned, total, prod, eff), g = actual_metrics(a, masters)
    ok = max((a.qty_produced or 0.0) - (a.qty_rejected or 0.0), 0.0)
    values = (
        a.entry_date.strftime("%d-%m-%Y"),
        getattr(a, "machine", "") or "",
        a.shift,
        a.operator,
        a.item_name or a.item_code,
        g,
        getattr(a, "shift_minutes", 0.0) or None,
        ok,
        a.qty_rejected,
        getattr(a, "std_setup_min", 0.0),
        a.actual_setup_min,
        a.no_power_min,
        a.no_operator_min,
        a.machine_breakdown_min,
        a.tool_problem_min,
        a.other_work_min,
        a.no_load_min,
        a.remarks,
        _r(planned),
        total,
        _r(None if prod is None else prod * 100, 1),
        _r(None if eff is None else eff * 100, 1),
    )
    return dict(zip(REPORT_COLUMNS, values))


def monthly_rows(actuals, masters, year, month):
    """Every punch dated in (year, month), one sheet row each, in floor order:
    date, shift, machine, operator. Outsourced (OS) steps are left out — they
    run off-site, with no machine, shift or operator to analyse."""
    from .orderbook import process_is_outsourced

    routings = getattr(masters, "routings", None) or {}
    picked = [a for a in actuals
              if a.entry_date.year == year and a.entry_date.month == month
              and not process_is_outsourced(routings.get(a.item_code), a.process)]
    picked.sort(key=lambda a: (a.entry_date, a.shift, getattr(a, "machine", "") or "",
                               a.operator, a.item_code))
    return [report_row(a, masters) for a in picked]
