"""Monthly production analysis — pure, reporting-only (never touches the plan).

Reproduces the owner's "Format Production analysis" workbook, Sheet1: the green
input columns B..S (Rate, T, deliberately left out for now) and the four orange
result columns U..X. One row per Daily Entry punch, exactly as the sheet has one
row per line typed. The formulas are the sheet's own, with ONE owner-directed
change (2026-10-03): operator efficiency deducts the ACTUAL setting time (L),
not the standard one (K), so a setup that overruns its standard never counts
against the operator's production pace.

    G  cycle time (min/piece)       the CURRENT Item's Process Master (the punch's saved value only if the master has none)
    H  minutes available in shift   typed on Daily Entry (defaults to the shift length)
    I  actual OK qty                qty produced − qty rejected
    J  rejected qty
    L  actual setting time (min)
    M..R  no power / no operator / machine breakdown / tool problem /
          other work / no load (min)

    U  Planned qty            = H / G
    V  Total actual qty       = I + J
    W  Overall productivity   = (V x G) / H          the shift's real output
    X  Operator efficiency    = (V x G + setting credit + downtime credited) / H

OPERATOR EFFICIENCY CREDITS WHAT IS NOT HIS MISTAKE (owner, 2026-10-06). It
replaces the sheet's X, which took setting and every downtime out of the
minutes he was judged against:

    setting credit     the STANDARD setting time, on a line where an actual
                       setting time was typed (a setup really happened; a job
                       run on in continuation gets none). Faster or slower
                       than standard, he earns the standard. A blank standard
                       counts as the default (90 min).
    downtime credited  No power + Machine breakdown + Tool problem + No load +
                       Other work done (not his mistake, or other work he did)
    not credited       No operator (his mistake) and the actual setting time
                       beyond the standard: both count against him

Overall productivity stays the real output of the shift, no credits, so the
two figures say different things: efficiency judges the operator, productivity
shows what the shift actually made.

"Qty produced" on a punch is ALL pieces made — the order book has always
counted good = produced − rejected — so I = produced − rejected and V = produced.

A result whose formula has nothing to divide by (no cycle time or no minutes)
is None, shown as "-", never 0: a zero would read as "the operator produced
nothing".

SHIFT AND MONTH TOTALS (owner, 2026-10-04). The floor types the WHOLE shift as
"minutes available" on every line, so a per-line efficiency splits one shift's
work across several lines and reads 20-30% for an operator who was busy all
shift. Efficiency is therefore judged per OPERATOR per SHIFT per DAY, never per
line:

    standard minutes earned = sum over the shift's lines of  V x G
    operator minutes earned = standard minutes earned + setting credit
                              + downtime credited (all lines)
    minutes available       = H, counted ONCE (never past the working minutes)
    operator efficiency     = operator minutes earned / minutes available
    overall productivity    = standard minutes earned / minutes available

The MONTH figure per operator sums those minutes over every shift and divides
once; percentages are never averaged. A shift with a line that has no cycle
time, or with no minutes entered, has no figure ("-") and is left out of the
month, with a note saying so, rather than counted as idle.

Pure: everything comes from the parameters — no storage, no wall clock.
"""
from __future__ import annotations

from datetime import date

from .loaders import normalize_process_name

# The day the Daily Entry form first carried the report's fields (machine,
# minutes available, setting time). Entries before it cannot be analysed and
# are never shown (owner, 2026-10-04: "start from the 3rd of October").
REPORT_START = date(2026, 10, 3)

# The standard setting time credited when a setup was typed but the standard was
# left blank (owner, 2026-10-06: "the default, which is 90"). The API passes the
# plan's own setup time, which is this same 90.
DEFAULT_STD_SETUP_MIN = 90.0

# Downtime that is not the operator's mistake (or is other work he did): these
# minutes are CREDITED to him. No operator is deliberately absent: it is his.
CREDITED_DOWNTIME_FIELDS = ("no_power_min", "machine_breakdown_min", "tool_problem_min",
                            "no_load_min", "other_work_min")

# Sheet1 row-6 headers, in sheet order (B..S, then U..X). Column T (Rate) is left
# out on the owner's instruction. These are the contract for the preview table
# and the xlsx download.
INPUT_COLUMNS = (
    "Date", "Machine", "Shift", "Operator", "Item description", "Item code", "Process",
    "Cycle time in Min", "Minutes available in shift", "Working minutes in shift (after break)",
    "Actual OK Qty", "Rejected qty",
    "Std. setting time in Min", "Actual setting time in Min",
    "No power (in Min)", "No operator (in Min)", "Machine Breakdown (in Min)",
    "Tool problem (in Min)", "Other work done (in Min)", "No Load (in Min)",
    "Remarks",
)
# Per-line results. Productivity and efficiency are deliberately NOT here: the
# floor types the whole shift on every line, so a per-line percentage is
# misleading (owner, 2026-10-04). They live on the shift and month tables.
RESULT_COLUMNS = ("Planned qty", "Total Actual Qty", "Setting credit in Min",
                  "Downtime credited in Min", "Standard minutes earned",
                  "Operator minutes earned")
REPORT_COLUMNS = INPUT_COLUMNS + RESULT_COLUMNS

PCT_COLUMNS = ("Overall Productivity", "Operator efficiency")
SHIFT_COLUMNS = (
    "Date", "Shift", "Operator", "Machines", "Items", "Entries",
    "Minutes entered", "Working minutes in shift (after break)",
    "Minutes available in shift", "Actual setting time in Min", "Setting credit in Min",
    "Downtime credited in Min", "No operator (in Min)",
    "Standard minutes earned", "Operator minutes earned", "Total Actual Qty",
    "Counted in the month",
) + PCT_COLUMNS + ("Note", "Working")
MONTH_COLUMNS = (
    "Operator", "Shifts worked", "Shifts counted",
    "Minutes available in shift", "Actual setting time in Min", "Setting credit in Min",
    "Downtime credited in Min", "No operator (in Min)",
    "Standard minutes earned", "Operator minutes earned", "Total Actual Qty",
) + PCT_COLUMNS + ("Note", "Working")


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
            setting_credit, downtime_credited):
    """The four orange columns for one punch: (planned, total_actual,
    productivity, efficiency). Productivity and efficiency are FRACTIONS
    (0.636 = 63.6%), as the sheet's cells are. Any of them may be None."""
    g = cycle_time if cycle_time and cycle_time > 0 else None
    h = minutes_available or 0.0
    total_actual = (ok_qty or 0.0) + (rejected_qty or 0.0)          # V = I + J
    planned = _ratio(h, g) if g else None                           # U = H / G
    productivity = _ratio(total_actual, planned)                    # W = V x G / H
    earned = total_actual * g + (setting_credit or 0.0) + (downtime_credited or 0.0) \
        if g else None
    efficiency = _ratio(earned, h) if earned is not None else None  # X
    return planned, total_actual, productivity, efficiency


def setting_credit(a, default=DEFAULT_STD_SETUP_MIN):
    """The setting minutes credited to the operator for one line: the STANDARD
    setting time when an actual setting time was typed (a setup happened), the
    default when that standard is blank, and nothing when no setup was typed (the
    job ran on in continuation)."""
    if not (a.actual_setup_min or 0.0) > 0:
        return 0.0
    std = getattr(a, "std_setup_min", 0.0) or 0.0
    return float(std) if std > 0 else float(default)


def downtime_credited(a):
    """Minutes lost that are not the operator's mistake, credited to him:
    No power + Machine breakdown + Tool problem + No load + Other work done."""
    return sum(getattr(a, f, 0.0) or 0.0 for f in CREDITED_DOWNTIME_FIELDS)


def _cap(a, working):
    """The most this line's shift can count: its working minutes after the meal
    break (``working``, e.g. {"1st shift": 630, "2nd shift": 570}), or None."""
    return (working or {}).get((a.shift or "").strip())


def actual_metrics(a, masters=None, working=None, default_setup=DEFAULT_STD_SETUP_MIN):
    """`metrics` for a stored Actual. The cycle time is the CURRENT Process
    Master's (owner, 2026-10-04: one set of standards, the latest, so a master
    upload re-reads every month); the value snapshotted on the punch is used only
    when the master has none for that item and step (removed or renamed). Minutes
    available never count past the shift's working minutes (``working``): nothing
    is made in the meal break."""
    g = cycle_time_for(masters, a.item_code, a.process) if masters is not None else None
    if g is None:
        g = getattr(a, "cycle_time_min", None)
    ok = max((a.qty_produced or 0.0) - (a.qty_rejected or 0.0), 0.0)
    h = getattr(a, "shift_minutes", 0.0) or 0.0
    cap = _cap(a, working)
    if cap is not None:
        h = min(h, cap)
    return metrics(g, h, ok, a.qty_rejected, setting_credit(a, default_setup),
                   downtime_credited(a)), g


def _r(x, nd=2):
    return None if x is None else round(x, nd)


def report_row(a, masters=None, working=None, default_setup=DEFAULT_STD_SETUP_MIN):
    """One sheet row for one punch: its inputs, planned qty, total qty, the
    credits, the standard minutes it earned (total qty x cycle time) and the
    operator minutes (those plus the credits)."""
    (planned, total, prod, eff), g = actual_metrics(a, masters, working, default_setup)
    credit, down = setting_credit(a, default_setup), downtime_credited(a)
    ok = max((a.qty_produced or 0.0) - (a.qty_rejected or 0.0), 0.0)
    values = (
        a.entry_date.strftime("%d-%m-%Y"),
        getattr(a, "machine", "") or "",
        (a.shift or "").strip(),
        (a.operator or "").strip() or "Unattributed",
        a.item_name or a.item_code,
        a.item_code,
        a.process,
        g,
        getattr(a, "shift_minutes", 0.0) or None,
        _cap(a, working),
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
        credit,
        _r(down, 1),
        None if g is None else _r(total * g, 1),
        None if g is None else _r(total * g + credit + down, 1),
    )
    return dict(zip(REPORT_COLUMNS, values))


def _picked(actuals, masters, year, month):
    """Every punch dated in (year, month), from REPORT_START on, in the order
    date, shift, operator, machine. Outsourced (OS) steps are left out — they run off-site, with no
    machine, shift or operator to analyse."""
    from .orderbook import process_is_outsourced

    routings = getattr(masters, "routings", None) or {}
    picked = [a for a in actuals
              if a.entry_date.year == year and a.entry_date.month == month
              and a.entry_date >= REPORT_START
              and not process_is_outsourced(routings.get(a.item_code), a.process)]
    # One operator's lines for one shift sit together, so the Excel's shift
    # formulas can be plain SUM / MAX over a block of rows.
    picked.sort(key=lambda a: (a.entry_date, (a.shift or "").strip(),
                               (a.operator or "").strip() or "Unattributed",
                               getattr(a, "machine", "") or "", a.item_code, a.process))
    return picked


def monthly_rows(actuals, masters, year, month, working=None,
                 default_setup=DEFAULT_STD_SETUP_MIN):
    """Every punch dated in (year, month), one sheet row each."""
    return [report_row(a, masters, working, default_setup)
            for a in _picked(actuals, masters, year, month)]


def _pct(num, den):
    r = _ratio(num, den)
    return None if r is None else round(r * 100, 1)


def _shift_totals(lines, masters, working=None, default_setup=DEFAULT_STD_SETUP_MIN):
    """The totals for one operator's one shift on one day (see the module doc).
    Minutes available = the minutes entered (counted once), but never more than
    the shift's working minutes after its meal break."""
    minutes = [getattr(a, "shift_minutes", 0.0) or 0.0 for a in lines]
    entered = max(minutes)
    cap = _cap(lines[0], working)
    h = entered if cap is None else min(entered, cap)
    setting = sum(a.actual_setup_min or 0.0 for a in lines)
    credit = sum(setting_credit(a, default_setup) for a in lines)
    down = sum(downtime_credited(a) for a in lines)
    no_op = sum(a.no_operator_min or 0.0 for a in lines)
    earned, pieces, no_ct, terms = 0.0, 0.0, 0, []
    for a in lines:
        (_, total, _, _), g = actual_metrics(a, masters)
        pieces += total
        if g is None:
            no_ct += 1
            terms.append(f"{_n(total)} x (no cycle time)")
        else:
            earned += total * g
            terms.append(f"{_n(total)} x {_n(g)}")
    notes = []
    if no_ct:
        notes.append(f"{no_ct} entr{'y has' if no_ct == 1 else 'ies have'} no cycle time")
    if h <= 0:
        notes.append("minutes available not entered")
    if len({m for m in minutes if m > 0}) > 1:
        notes.append(f"entries give different minutes available; the largest ({entered:g}) is used")
    if cap is not None and entered > cap:
        notes.append(f"{entered:g} entered; the shift has {cap:g} working minutes after "
                     f"its meal break, so {cap:g} is used")
    complete = not no_ct and h > 0
    return {"h": h, "entered": entered, "cap": cap, "setting": setting,
            "credit": credit, "down": down, "no_op": no_op,
            "earned": earned, "op_earned": earned + credit + down,
            "pieces": pieces, "complete": complete, "notes": notes, "terms": terms}


def _n(x):
    """A number as the working text shows it: no trailing .0, thousands commas."""
    x = round(float(x), 2)
    return f"{x:,.0f}" if x == int(x) else f"{x:,.2f}".rstrip("0").rstrip(".")


def _working(earned_text, t, ok, avail_text=None):
    """The arithmetic behind a shift or month figure, written out in full."""
    parts = [f"Standard minutes earned = {earned_text} = {_n(t['earned'])}"]
    parts.append(f"Operator minutes earned = {_n(t['earned'])} earned + {_n(t['credit'])} "
                 f"setting credit + {_n(t['down'])} downtime credited = {_n(t['op_earned'])}")
    if avail_text:
        parts.append(avail_text)
    if ok:
        parts.append(f"Operator efficiency = {_n(t['op_earned'])} / {_n(t['h'])} = "
                     f"{t['op_earned'] / t['h'] * 100:.1f}%")
        parts.append(f"Overall productivity = {_n(t['earned'])} / {_n(t['h'])} = "
                     f"{t['earned'] / t['h'] * 100:.1f}%")
    else:
        parts.append("No efficiency: see the note")
    return ". ".join(parts) + "."


def _by_operator_shift(picked):
    groups = {}
    for a in picked:
        key = (a.entry_date, (a.shift or "").strip(),
               (a.operator or "").strip() or "Unattributed")
        groups.setdefault(key, []).append(a)
    return groups


def shift_rows(actuals, masters, year, month, working=None,
               default_setup=DEFAULT_STD_SETUP_MIN):
    """One row per operator per shift per day: everything the operator made in
    that shift, judged against the shift's minutes counted once."""
    out = []
    for (day, shift, op), lines in sorted(_by_operator_shift(
            _picked(actuals, masters, year, month)).items()):
        t = _shift_totals(lines, masters, working, default_setup)
        ok = t["complete"]
        out.append(dict(zip(SHIFT_COLUMNS, (
            day.strftime("%d-%m-%Y"), shift, op or "Unattributed",
            ", ".join(sorted({getattr(a, "machine", "") or "-" for a in lines})),
            ", ".join(sorted({a.item_code for a in lines})),
            len(lines),
            t["entered"] or None, t["cap"],
            t["h"] or None, t["setting"], t["credit"], round(t["down"], 1), t["no_op"],
            round(t["earned"], 1), round(t["op_earned"], 1), t["pieces"],
            "Yes" if ok else "No",
            _pct(t["earned"], t["h"]) if ok else None,
            _pct(t["op_earned"], t["h"]) if ok else None,
            "; ".join(t["notes"]),
            _working(" + ".join(t["terms"]), t, ok,
                     (f"Minutes available = smaller of {_n(t['entered'])} entered and "
                      f"{_n(t['cap'])} working minutes after the meal break = {_n(t['h'])}")
                     if t["cap"] is not None else None),
        ))))
    return out


def operator_month_rows(actuals, masters, year, month, working=None,
                        default_setup=DEFAULT_STD_SETUP_MIN):
    """One row per operator for the month. The minutes of every COUNTED shift are
    added up and divided once; a shift without a figure is left out and named in
    the note. Sorted by efficiency, highest first; no figure sorts last."""
    per_op = {}
    for (_day, _shift, op), lines in _by_operator_shift(
            _picked(actuals, masters, year, month)).items():
        per_op.setdefault(op, []).append(
            (_day, _shift, _shift_totals(lines, masters, working, default_setup)))
    out = []
    for op, shifts in per_op.items():
        counted = [t for _, _, t in shifts if t["complete"]]
        names = [f"{d.strftime('%d-%m')} {sh}" for d, sh, t in sorted(shifts, key=lambda x: (x[0], x[1]))
                 if t["complete"]]
        tot = {k: sum(t[k] for t in counted)
               for k in ("h", "setting", "credit", "down", "no_op", "earned", "op_earned")}
        left_out = len(shifts) - len(counted)
        note = (f"{left_out} shift{'s' if left_out != 1 else ''} left out "
                f"(missing cycle time or minutes; see the shift table)") if left_out else ""
        out.append(dict(zip(MONTH_COLUMNS, (
            op, len(shifts), len(counted),
            tot["h"] or None, tot["setting"], tot["credit"], round(tot["down"], 1),
            tot["no_op"], round(tot["earned"], 1), round(tot["op_earned"], 1),
            sum(t["pieces"] for _, _, t in shifts),
            _pct(tot["earned"], tot["h"]) if counted else None,
            _pct(tot["op_earned"], tot["h"]) if counted else None,
            note,
            _working("sum over the shifts counted (" + ", ".join(names) + ")" if counted
                     else "no shift counted", tot, bool(counted)),
        ))))
    out.sort(key=lambda r: (r["Operator efficiency"] is None,
                            -(r["Operator efficiency"] or 0), r["Operator"]))
    return out
