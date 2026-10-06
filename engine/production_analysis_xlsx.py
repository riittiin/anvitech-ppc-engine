"""The production analysis report as an Excel file, every figure a live formula.

Owner, 2026-10-04: "show each and every calculation, completely transparent".
So nothing calculated is typed into the file as a bare number. Every result
cell is an Excel formula over the cells it comes from: click it and Excel shows
exactly which cells it adds or divides. Each formula also carries the value the
app computed, so a viewer that does not recalculate (a phone preview) still
shows the right number, and the tests check the two agree. Shift and month rows
add a "Working" column with the same arithmetic written out in words.

Sheets, in reading order:
    Operator efficiency   the month per operator (SUMIFS / COUNTIFS over Shift-wise)
    Shift-wise            one row per operator per shift (SUM / MAX over that
                          shift's block of rows on Every entry; the entries are
                          sorted so each operator's shift is one block)
    Every entry           one row per Daily Entry line, the sheet's inputs plus
                          its per-line formulas
    How it is calculated  every formula in words

Pure: takes the three tables production_analysis builds, returns bytes. Uses
XlsxWriter because it can store a formula together with its value; openpyxl
cannot.
"""
from __future__ import annotations

import io

from xlsxwriter import Workbook
from xlsxwriter.utility import xl_col_to_name

from . import production_analysis as pa

ENTRY_SHEET = "Every entry"
SHIFT_SHEET = "Shift-wise"
MONTH_SHEET = "Operator efficiency"
HOW_SHEET = "How it is calculated"

HEADER_ROW = 5          # 0-based: row 6 in Excel, as the owner's workbook has it
FIRST_COL = 1           # column B
_INPUT_FILL = "#D7E4BD"  # the owner's sheet: inputs green
_RESULT_FILL = "#FCD5B4"  # results orange


def _safe_text(v):
    """A text cell can never start a formula (spreadsheet formula injection)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") and s != "-" else s


class _Sheet:
    """One table on one worksheet: header on row 6, data from row 7, from column B."""

    def __init__(self, wb, name, title, year, month, columns, result_cols, fmt):
        self.ws = wb.add_worksheet(name)
        self.name, self.columns, self.fmt = name, list(columns), fmt
        self.ws.write_string(0, FIRST_COL, title, fmt["title"])
        self.ws.write_string(2, FIRST_COL, "Month - year")
        self.ws.write_string(2, FIRST_COL + 1, f"{month:02d}-{year:04d}")
        for j, c in enumerate(self.columns):
            self.ws.write_string(HEADER_ROW, FIRST_COL + j, c,
                          fmt["result_head"] if c in result_cols else fmt["input_head"])
            self.ws.set_column(FIRST_COL + j, FIRST_COL + j, 14)
        self.ws.set_row(HEADER_ROW, 45)
        self.ws.freeze_panes(HEADER_ROW + 1, 0)

    def col(self, name):
        """Excel column letter of a named column."""
        return xl_col_to_name(FIRST_COL + self.columns.index(name))

    def cell(self, name, row):
        """A1 reference to (column, 0-based data row index) on this sheet."""
        return f"{self.col(name)}{HEADER_ROW + 2 + row}"

    def rng(self, name, r1, r2, absolute=False, other=True):
        """A range down one column, data rows r1..r2, prefixed with the sheet name."""
        c = self.col(name)
        a, b = HEADER_ROW + 2 + r1, HEADER_ROW + 2 + r2
        ref = f"${c}${a}:${c}${b}" if absolute else f"{c}{a}:{c}{b}"
        return f"'{self.name}'!{ref}" if other else ref

    def put(self, row, name, value, formula=None):
        """Write one cell: a formula (with its computed value) or a plain value."""
        r, c = HEADER_ROW + 1 + row, FIRST_COL + self.columns.index(name)
        pct = name in pa.PCT_COLUMNS
        if formula is not None:
            cached = value
            if pct and isinstance(value, (int, float)):
                cached = value / 100.0
            if cached is None:
                cached = "-"
            self.ws.write_formula(r, c, formula, self.fmt["pct"] if pct else self.fmt["num"],
                                  cached)
        elif value is None or value == "":
            self.ws.write_blank(r, c, None)
        elif isinstance(value, (int, float)):
            self.ws.write_number(r, c, value, self.fmt["num"])
        else:
            self.ws.write_string(r, c, _safe_text(value), self.fmt["wrap"]
                                 if name in ("Working", "Note") else None)


def _entries(sh, rows, default_setup):
    g, h, i, j = (sh.col("Cycle time in Min"), sh.col("Minutes available in shift"),
                  sh.col("Actual OK Qty"), sh.col("Rejected qty"))
    w = sh.col("Working minutes in shift (after break)")
    std, act = sh.col("Std. setting time in Min"), sh.col("Actual setting time in Min")
    credited = [sh.col(c) for c in ("No power (in Min)", "Machine Breakdown (in Min)",
                                    "Tool problem (in Min)", "No Load (in Min)",
                                    "Other work done (in Min)")]
    v = sh.col("Total Actual Qty")
    cr, dc, ea = (sh.col("Setting credit in Min"), sh.col("Downtime credited in Min"),
                  sh.col("Standard minutes earned"))
    for k, row in enumerate(rows):
        n = HEADER_ROW + 2 + k
        for name in pa.INPUT_COLUMNS:
            sh.put(k, name, row[name])
        # Minutes available never count past the shift's working minutes (the meal
        # break), so the planned qty divides the smaller of the two.
        mins = f"IF(ISNUMBER({w}{n}),MIN({h}{n},{w}{n}),{h}{n})"
        sh.put(k, "Planned qty", row["Planned qty"],
               f'=IF(AND(ISNUMBER({g}{n}),{g}{n}>0),{mins}/{g}{n},"-")')
        sh.put(k, "Total Actual Qty", row["Total Actual Qty"], f"={i}{n}+{j}{n}")
        # A setup was typed: credit the standard (the default when it is blank).
        sh.put(k, "Setting credit in Min", row["Setting credit in Min"],
               f"=IF(N({act}{n})>0,IF(N({std}{n})>0,{std}{n},{default_setup:g}),0)")
        sh.put(k, "Downtime credited in Min", row["Downtime credited in Min"],
               "=" + "+".join(f"N({c}{n})" for c in credited))
        sh.put(k, "Standard minutes earned", row["Standard minutes earned"],
               f'=IF(ISNUMBER({g}{n}),{v}{n}*{g}{n},"-")')
        sh.put(k, "Operator minutes earned", row["Operator minutes earned"],
               f'=IF(ISNUMBER({ea}{n}),{ea}{n}+{cr}{n}+{dc}{n},"-")')
    sh.ws.set_column(FIRST_COL + pa.REPORT_COLUMNS.index("Remarks"),
                     FIRST_COL + pa.REPORT_COLUMNS.index("Remarks"), 24)
    return sh


def _blocks(entries, shifts):
    """For each shift row, the (first, last) entry rows that make it up. The
    entries are sorted by date, shift, operator, so each shift is one block."""
    key = lambda r: (r["Date"], r["Shift"], r["Operator"])
    spans, start = {}, 0
    for k in range(1, len(entries) + 1):
        if k == len(entries) or key(entries[k]) != key(entries[start]):
            spans[key(entries[start])] = (start, k - 1)
            start = k
    out = [spans[key(s)] for s in shifts]
    assert sum(b - a + 1 for a, b in out) == len(entries), "an entry belongs to no shift"
    return out


def _shifts(sh, rows, entry_sh, blocks):
    for k, (row, (a, b)) in enumerate(zip(rows, blocks)):
        e = lambda name: entry_sh.rng(name, a, b)
        c = lambda name: sh.cell(name, k)
        for name in ("Date", "Shift", "Operator", "Machines", "Items", "Note", "Working"):
            sh.put(k, name, row[name])
        sh.put(k, "Entries", row["Entries"], f"=COUNTA({e('Date')})")
        sh.put(k, "Minutes entered", row["Minutes entered"] or 0,
               f"=MAX({e('Minutes available in shift')})")
        sh.put(k, "Working minutes in shift (after break)", row["Working minutes in shift (after break)"], f"=MAX({e('Working minutes in shift (after break)')})"
               if row["Working minutes in shift (after break)"] is not None else None)
        en, wk = c("Minutes entered"), c("Working minutes in shift (after break)")
        sh.put(k, "Minutes available in shift", row["Minutes available in shift"] or 0,
               f"=IF(ISNUMBER({wk}),MIN({en},{wk}),{en})")
        for name in ("Actual setting time in Min", "Setting credit in Min",
                     "Downtime credited in Min", "No operator (in Min)",
                     "Standard minutes earned", "Operator minutes earned",
                     "Total Actual Qty"):
            sh.put(k, name, row[name], f"=SUM({e(name)})")
        hm = c("Minutes available in shift")
        ea, oe = c("Standard minutes earned"), c("Operator minutes earned")
        sh.put(k, "Counted in the month", row["Counted in the month"],
               f'=IF(AND(COUNT({e("Cycle time in Min")})={c("Entries")},{hm}>0),"Yes","No")')
        ok = c("Counted in the month")
        sh.put(k, "Overall Productivity", row["Overall Productivity"],
               f'=IF({ok}="Yes",{ea}/{hm},"-")')
        sh.put(k, "Operator efficiency", row["Operator efficiency"],
               f'=IF({ok}="Yes",{oe}/{hm},"-")')
    for name, w in (("Working", 70), ("Note", 30), ("Items", 22), ("Machines", 14)):
        i = FIRST_COL + pa.SHIFT_COLUMNS.index(name)
        sh.ws.set_column(i, i, w)
    return sh


def _month(sh, rows, shift_sh, n_shifts):
    last = max(n_shifts - 1, 0)
    s = lambda name: shift_sh.rng(name, 0, last, absolute=True)
    yes = f'{s("Counted in the month")},"Yes"'
    for k, row in enumerate(rows):
        c = lambda name: sh.cell(name, k)
        op = c("Operator")
        for name in ("Operator", "Note", "Working"):
            sh.put(k, name, row[name])
        sh.put(k, "Shifts worked", row["Shifts worked"], f"=COUNTIFS({s('Operator')},{op})")
        sh.put(k, "Shifts counted", row["Shifts counted"],
               f"=COUNTIFS({s('Operator')},{op},{yes})")
        for name in ("Minutes available in shift", "Actual setting time in Min",
                     "Setting credit in Min", "Downtime credited in Min",
                     "No operator (in Min)", "Standard minutes earned",
                     "Operator minutes earned"):
            sh.put(k, name, row[name] or 0, f"=SUMIFS({s(name)},{s('Operator')},{op},{yes})")
        sh.put(k, "Total Actual Qty", row["Total Actual Qty"],
               f"=SUMIFS({s('Total Actual Qty')},{s('Operator')},{op})")
        n, hm = c("Shifts counted"), c("Minutes available in shift")
        sh.put(k, "Overall Productivity", row["Overall Productivity"],
               f'=IF({n}>0,{c("Standard minutes earned")}/{hm},"-")')
        sh.put(k, "Operator efficiency", row["Operator efficiency"],
               f'=IF({n}>0,{c("Operator minutes earned")}/{hm},"-")')
    for name, w in (("Working", 70), ("Note", 30), ("Operator", 18)):
        i = FIRST_COL + pa.MONTH_COLUMNS.index(name)
        sh.ws.set_column(i, i, w)
    return sh


HOW_ROWS = (
    ("Where the numbers come from",
     "Every Daily Entry line typed from 03-10-2026 on (the day the form first had these "
     "fields). Outsourced steps are left out. Cycle time is the CURRENT Item's Process "
     "Master value (the value saved on the line only when the master has none)."),
    ("Every entry: Total Actual Qty", "= Actual OK Qty + Rejected qty"),
    ("Every entry: Setting credit",
     "= the Standard setting time, on a line where an Actual setting time was typed (a setup "
     "happened). Blank standard = the default 90. No actual setting typed (the job ran on in "
     "continuation) = 0. Faster or slower than standard, the operator earns the standard."),
    ("Every entry: Downtime credited",
     "= No power + Machine breakdown + Tool problem + No load + Other work done. Not the "
     "operator's mistake (or other work he did), so credited to him."),
    ("Not credited",
     "No operator (his mistake) and any actual setting time beyond the standard. They count "
     "against the operator."),
    ("Every entry: Standard minutes earned",
     "= Total Actual Qty x Cycle time. The minutes the pieces should take at standard pace."),
    ("Every entry: Operator minutes earned",
     "= Standard minutes earned + Setting credit + Downtime credited"),
    ("Shift-wise: one row", "One operator, one day, one shift: all of that operator's lines "
                            "for the shift together (a block of rows on Every entry)."),
    ("Working minutes in shift (after break)",
     "1st shift 08:00-19:00 = 660 minutes less the 13:00-13:30 lunch = 630. 2nd shift "
     "19:00-05:00 = 600 minutes less the 22:00-22:30 dinner = 570. Nobody works in a break."),
    ("Every entry: Planned qty", "= the smaller of Minutes available and Working minutes, "
                                 "divided by Cycle time"),
    ("Shift-wise: Minutes entered",
     "= the LARGEST minutes available typed on the shift's lines. The floor types the whole "
     "shift on every line, so it is counted once, never added up."),
    ("Shift-wise: Minutes available in shift",
     "= the smaller of Minutes entered and Working minutes in shift. A line typed before "
     "the break was taken out (660 / 600) counts as 630 / 570."),
    ("Shift-wise: every other minutes column", "= added up over the shift's lines"),
    ("Shift-wise: Counted in the month",
     "Yes when every line has a cycle time and minutes available were typed. A No shift has "
     "no percentage and is left out of the month."),
    ("Shift-wise: Operator efficiency", "= Operator minutes earned / Minutes available"),
    ("Shift-wise: Overall productivity",
     "= Standard minutes earned / Minutes available. The real output of the shift, no credits."),
    ("Operator efficiency (month)",
     "= the counted shifts' Operator minutes earned added up, divided by their Minutes "
     "available added up. Percentages are never averaged."),
    ("Overall productivity (month)",
     "= the counted shifts' Standard minutes earned added up / their Minutes available added up"),
    ("Over 100%", "Possible when one operator runs two machines at once: both machines' work "
                  "counts against one shift."),
)


def build(year, month, tables) -> bytes:
    """The .xlsx bytes for one month. `tables` is api.main._production_tables."""
    buf = io.BytesIO()
    wb = Workbook(buf, {"in_memory": True})
    fmt = {
        "title": wb.add_format({"bold": True, "font_size": 14}),
        "input_head": wb.add_format({"bold": True, "text_wrap": True, "valign": "top",
                                     "bg_color": _INPUT_FILL, "border": 1}),
        "result_head": wb.add_format({"bold": True, "text_wrap": True, "valign": "top",
                                      "bg_color": _RESULT_FILL, "border": 1}),
        "pct": wb.add_format({"num_format": "0.0%"}),
        "num": wb.add_format({"num_format": "General"}),
        "wrap": wb.add_format({"text_wrap": True, "valign": "top"}),
    }
    entries = tables["entries"]["rows"]
    shifts = tables["shifts"]["rows"]
    # Sheets in reading order; a formula may point at a sheet to its right.
    month_sh = _Sheet(wb, MONTH_SHEET, "Operator efficiency for the month", year, month,
                      pa.MONTH_COLUMNS, set(pa.MONTH_COLUMNS[1:-2]), fmt)
    shift_sh = _Sheet(wb, SHIFT_SHEET, "Operator efficiency per shift", year, month,
                      pa.SHIFT_COLUMNS, set(pa.SHIFT_COLUMNS[5:-2]), fmt)
    entry_sh = _Sheet(wb, ENTRY_SHEET, "Monthly production analysis: every Daily Entry line",
                      year, month, pa.REPORT_COLUMNS, pa.RESULT_COLUMNS, fmt)
    _entries(entry_sh, entries, tables.get("default_setup", pa.DEFAULT_STD_SETUP_MIN))
    _shifts(shift_sh, shifts, entry_sh, _blocks(entries, shifts))
    _month(month_sh, tables["operators"]["rows"], shift_sh, len(shifts))
    how = wb.add_worksheet(HOW_SHEET)
    how.write_string(0, 1, "How every figure is calculated", fmt["title"])
    how.set_column(1, 1, 38)
    how.set_column(2, 2, 110)
    for k, (a, b) in enumerate(HOW_ROWS):
        # write_string: these lines start with "=", and plain write() would turn
        # them into (broken) formulas.
        how.write_string(2 + k, 1, a, fmt["wrap"])
        how.write_string(2 + k, 2, b, fmt["wrap"])
    wb.close()
    return buf.getvalue()
