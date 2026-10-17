"""The production analysis report as an Excel file, every figure a live formula.

Owner, 2026-10-04: "show each and every calculation, completely transparent".
So nothing calculated is typed into the file as a bare number. Every result
cell is an Excel formula over the cells it comes from: click it and Excel shows
exactly which cells it adds or divides. Each formula also carries the value the
app computed, so a viewer that does not recalculate (a phone preview) still
shows the right number, and the tests check the two agree.

Sheets, in reading order:
    Production analysis   the owner's own format (Format Production analysis,
                          Sheet1), copied as he drew it (2026-10-17, after he
                          rejected interleaved bold "Shift total" rows as
                          confusing): a flat table, ONE ROW PER DAILY ENTRY and
                          nothing else, plain rows of normal height. Overall
                          Productivity and Operator efficiency on every row are
                          that row's SHIFT figure (same operator, day, shift),
                          so every row of one shift shows the same percentage:
                          the floor types the whole shift on every line, so a
                          per-line percentage would be misleading (2026-10-04).
    Operator efficiency   the month per operator: SUMIFS / COUNTIFS over the
                          flat sheet, each shift's minutes counted ONCE (through
                          the "Shift minutes (first row of shift)" column)
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

DETAIL_SHEET = "Production analysis"
MONTH_SHEET = "Operator efficiency"
HOW_SHEET = "How it is calculated"

HEADER_ROW = 5          # 0-based: row 6 in Excel, as the owner's workbook has it
FIRST_COL = 1           # column B
FONT = "Times New Roman"  # the owner's sheet
_INPUT_FILL = "#D7E4BD"   # the owner's sheet: inputs green (accent 3, 60% lighter)
_RESULT_FILL = "#FCD5B4"  # results orange (accent 6, 60% lighter)
_WORK_FILL = "#FDE9D9"    # the working columns at the right: lighter orange

# The owner's format (Sheet1 B..S, then U..X; Rate (T) and Y onwards dropped),
# with Item code and Process after Item description, then the working columns.
FORMAT_COLUMNS = (
    "Date", "Machine", "Shift", "Operator", "Item description", "Item code", "Process",
    "Cycle time in Min", "Minutes available in shift", "Actual OK Qty", "Rejected qty",
    "Std. setting time in Min", "Actual setting time in Min", "No power (in Min)",
    "No operator (in Min)", "Machine Breakdown (in Min)", "Tool problem (in Min)",
    "Other work done (in Min)", "No Load (in Min)", "Remarks",
    "Planned qty", "Total Actual Qty", "Overall Productivity", "Operator efficiency",
)
FIRST_ROW_MINUTES = "Shift minutes (first row of shift)"
WORK_COLUMNS = (
    "Working minutes in shift (after break)", "Setting credit in Min",
    "Downtime credited in Min", "Standard minutes earned", "Operator minutes earned",
    "Shift minutes available", FIRST_ROW_MINUTES, "Counted in the month", "Note",
)
DETAIL_COLUMNS = FORMAT_COLUMNS + WORK_COLUMNS
_RESULT_HEADS = {"Planned qty", "Total Actual Qty", "Overall Productivity",
                 "Operator efficiency"}
_WHOLE_SHIFT = "for the whole shift"
_FROZEN_THROUGH = "Operator"     # Date..Operator stay in view scrolling right


def _safe_text(v):
    """A text cell can never start a formula (spreadsheet formula injection)."""
    s = "" if v is None else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") and s != "-" else s


class _Sheet:
    """One table on one worksheet, laid out as the owner's sheet: title on B1,
    "Month -year" on B3, header on row 6, data from row 7, from column B."""

    def __init__(self, wb, name, title, year, month, columns, head_fmt, fmt):
        self.ws = wb.add_worksheet(name)
        self.name, self.columns, self.fmt = name, list(columns), fmt
        self.ws.write_string(0, FIRST_COL, title, fmt["title"])
        self.ws.set_row(0, 36.75)
        self.ws.write_string(2, FIRST_COL, "Month -year", fmt["month_label"])
        self.ws.write_string(2, FIRST_COL + 1, f"{month:02d}-{year:04d}", fmt["month_value"])
        for j, c in enumerate(self.columns):
            self.ws.write_string(HEADER_ROW, FIRST_COL + j, c, fmt[head_fmt(c)])
            self.ws.set_column(FIRST_COL + j, FIRST_COL + j, 13)
        self.ws.set_row(HEADER_ROW, 73.5)
        # Item codes are text (61243661-01 is not a number): no green triangles.
        self.ws.ignore_errors({"number_stored_as_text": "A1:XFD1048576"})

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

    def width(self, widths):
        for name, w in widths:
            if name in self.columns:
                i = FIRST_COL + self.columns.index(name)
                self.ws.set_column(i, i, w)

    def put(self, row, name, value, formula=None):
        """Write one plain data cell: a formula (with its computed value) or a
        value. Never bold, never wrapped, never a row height of its own."""
        r, c = HEADER_ROW + 1 + row, FIRST_COL + self.columns.index(name)
        pct = name in pa.PCT_COLUMNS
        if formula is not None:
            cached = value
            if pct and isinstance(value, (int, float)):
                cached = value / 100.0
            if cached is None:
                cached = "-"
            self.ws.write_formula(r, c, formula, self.fmt["pct" if pct else "num"], cached)
        elif value is None or value == "":
            self.ws.write_blank(r, c, None, self.fmt["text"])
        elif isinstance(value, (int, float)):
            self.ws.write_number(r, c, value, self.fmt["num"])
        else:
            self.ws.write_string(r, c, _safe_text(value), self.fmt["text"])


def _line(sh, k, row, shift, a, b, default_setup):
    """The Daily Entry line at data row k, one of its shift's rows a..b: the
    inputs, the per-line formulas, and the shift's figures over rows a..b (the
    rows stay sorted date, shift, operator, so a shift is one block)."""
    n = HEADER_ROW + 2 + k
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

    # The shift's figures, the same on every row of the shift.
    blk = lambda name: sh.rng(name, a, b, absolute=True, other=False)
    sm, ok = sh.cell("Shift minutes available", k), sh.cell("Counted in the month", k)
    # The floor types the whole shift on every line: the largest is counted once,
    # never past the working minutes after the meal break.
    sh.put(k, "Shift minutes available", shift["Minutes available in shift"] or 0,
           f"=IF(ISNUMBER({w}{n}),MIN(MAX({blk('Minutes available in shift')}),{w}{n}),"
           f"MAX({blk('Minutes available in shift')}))")
    if k == a:
        sh.put(k, FIRST_ROW_MINUTES, shift["Minutes available in shift"] or 0, f"={sm}")
    else:
        sh.put(k, FIRST_ROW_MINUTES, None)
    sh.put(k, "Counted in the month", shift["Counted in the month"],
           f'=IF(AND(COUNT({blk("Cycle time in Min")})=COUNTA({blk("Date")}),{sm}>0),'
           f'"Yes","No")')
    sh.put(k, "Overall Productivity", shift["Overall Productivity"],
           f'=IF({ok}="Yes",SUM({blk("Standard minutes earned")})/{sm},"-")')
    sh.put(k, "Operator efficiency", shift["Operator efficiency"],
           f'=IF({ok}="Yes",SUM({blk("Operator minutes earned")})/{sm},"-")')
    sh.put(k, "Note", shift["Note"])


def _blocks(entries, shifts):
    """For each shift row, the (first, last) entry rows that make it up. The
    entries are sorted by date, shift, operator, so each shift is one block."""
    key = lambda r: (r["Date"], r["Shift"], r["Operator"])
    spans, start = {}, 0
    for k in range(1, len(entries) + 1):
        if k == len(entries) or key(entries[k]) != key(entries[start]):
            assert key(entries[start]) not in spans, "a shift's entries are not together"
            spans[key(entries[start])] = (start, k - 1)
            start = k
    out = [spans[key(s)] for s in shifts]
    assert sum(b - a + 1 for a, b in out) == len(entries), "an entry belongs to no shift"
    return out


def _detail(sh, entries, shifts, default_setup):
    """One row per entry, nothing else. Returns the number of data rows."""
    for shift, (a, b) in zip(shifts, _blocks(entries, shifts)):
        for k in range(a, b + 1):
            _line(sh, k, entries[k], shift, a, b, default_setup)
    n = len(entries)
    for name in pa.PCT_COLUMNS:
        sh.ws.write_comment(HEADER_ROW, FIRST_COL + sh.columns.index(name),
                            f"{name} {_WHOLE_SHIFT}: every row of one operator's shift "
                            f"(same date and shift) shows the same figure.")
    sh.ws.freeze_panes(HEADER_ROW + 1, FIRST_COL + sh.columns.index(_FROZEN_THROUGH) + 1)
    sh.ws.autofilter(HEADER_ROW, FIRST_COL, HEADER_ROW + max(n, 1),
                     FIRST_COL + len(sh.columns) - 1)
    sh.width((("Date", 12), ("Operator", 16), ("Item description", 22),
              ("Item code", 16), ("Process", 18), ("Remarks", 18), ("Note", 30)))
    return n


def _month(sh, rows, detail_sh, n_rows):
    last = max(n_rows - 1, 0)
    s = lambda name: detail_sh.rng(name, 0, last, absolute=True)
    yes = f'{s("Counted in the month")},"Yes"'
    # One per shift: the shift's minutes sit on its first row only.
    first = f'{s(FIRST_ROW_MINUTES)},">=0"'
    for k, row in enumerate(rows):
        c = lambda name: sh.cell(name, k)
        op = c("Operator")
        for name in ("Operator", "Note", "Working"):
            sh.put(k, name, row[name])
        sh.put(k, "Shifts worked", row["Shifts worked"],
               f"=COUNTIFS({s('Operator')},{op},{first})")
        sh.put(k, "Shifts counted", row["Shifts counted"],
               f"=COUNTIFS({s('Operator')},{op},{first},{yes})")
        sh.put(k, "Minutes available in shift", row["Minutes available in shift"] or 0,
               f"=SUMIFS({s(FIRST_ROW_MINUTES)},{s('Operator')},{op},{yes})")
        for name in ("Actual setting time in Min", "Setting credit in Min",
                     "Downtime credited in Min", "No operator (in Min)",
                     "Standard minutes earned", "Operator minutes earned"):
            sh.put(k, name, row[name] or 0, f"=SUMIFS({s(name)},{s('Operator')},{op},{yes})")
        sh.put(k, "Total Actual Qty", row["Total Actual Qty"],
               f"=SUMIFS({s('Total Actual Qty')},{s('Operator')},{op})")
        n, hm = c("Shifts counted"), c("Minutes available in shift")
        sh.put(k, "Overall Productivity", row["Overall Productivity"],
               f'=IF({n}>0,{c("Standard minutes earned")}/{hm},"-")')
        sh.put(k, "Operator efficiency", row["Operator efficiency"],
               f'=IF({n}>0,{c("Operator minutes earned")}/{hm},"-")')
    sh.ws.freeze_panes(HEADER_ROW + 1, FIRST_COL + 1)
    sh.width((("Operator", 18), ("Note", 30)))
    return sh


HOW_ROWS = (
    ("Where the numbers come from",
     "Every Daily Entry line typed from 03-10-2026 on (the day the form first had these "
     "fields). Outsourced steps are left out. Cycle time is the CURRENT Item's Process "
     "Master value (the value saved on the line only when the master has none)."),
    ("Production analysis: the layout",
     "The owner's format: one row per Daily Entry line and nothing else, sorted by date, "
     "shift and operator. Overall Productivity and Operator efficiency are the figures for "
     "the whole shift (that operator, that date, that shift), shown on every row of that "
     "shift, so every row of one shift shows the same percentage. A per-line percentage "
     "would be misleading: the floor types the whole shift's minutes on every line."),
    ("Each line: Total Actual Qty", "= Actual OK Qty + Rejected qty"),
    ("Each line: Setting credit",
     "= the Standard setting time, on a line where an Actual setting time was typed (a setup "
     "happened). Blank standard = the default 90. No actual setting typed (the job ran on in "
     "continuation) = 0. Faster or slower than standard, the operator earns the standard."),
    ("Each line: Downtime credited",
     "= No power + Machine breakdown + Tool problem + No load + Other work done. Not the "
     "operator's mistake (or other work he did), so credited to him."),
    ("Not credited",
     "No operator (his mistake) and any actual setting time beyond the standard. They count "
     "against the operator."),
    ("Each line: Standard minutes earned",
     "= Total Actual Qty x Cycle time. The minutes the pieces should take at standard pace."),
    ("Each line: Operator minutes earned",
     "= Standard minutes earned + Setting credit + Downtime credited"),
    ("Working minutes in shift (after break)",
     "1st shift 08:00-19:00 = 660 minutes less the 13:00-13:30 lunch = 630. 2nd shift "
     "19:00-05:00 = 600 minutes less the 22:00-22:30 dinner = 570. Nobody works in a break."),
    ("Each line: Planned qty", "= the smaller of Minutes available and Working minutes, "
                                 "divided by Cycle time"),
    ("Shift minutes available",
     "= the LARGEST minutes available typed on the shift's lines (the floor types the whole "
     "shift on every line, so it is counted once, never added up), but never more than the "
     "Working minutes in shift. A line typed before the break was taken out (660 / 600) "
     "counts as 630 / 570. Shown on every row of the shift."),
    ("Shift minutes (first row of shift)",
     "The Shift minutes available again, on the first row of each shift only, so the month "
     "sheet adds each shift's minutes once."),
    ("Counted in the month",
     "Yes when every line of the shift has a cycle time and minutes available were typed. "
     "A No shift has no percentage and is left out of the month."),
    ("Operator efficiency (shift)",
     "= the shift's Operator minutes earned added up over its rows / Shift minutes available"),
    ("Overall Productivity (shift)",
     "= the shift's Standard minutes earned added up over its rows / Shift minutes available. "
     "The real output of the shift, no credits."),
    ("Operator efficiency (month)",
     "= the counted shifts' Operator minutes earned added up, divided by their Shift minutes "
     "available added up (each shift once, from its first row). Percentages are never "
     "averaged."),
    ("Overall productivity (month)",
     "= the counted shifts' Standard minutes earned added up / their Shift minutes available "
     "added up"),
    ("Over 100%", "Possible when one operator runs two machines at once: both machines' work "
                  "counts against one shift."),
)


def build(year, month, tables) -> bytes:
    """The .xlsx bytes for one month. `tables` is api.main._production_tables."""
    buf = io.BytesIO()
    wb = Workbook(buf, {"in_memory": True})
    f = lambda **kw: wb.add_format({"font_name": FONT, "font_size": 11, **kw})
    head = dict(text_wrap=True, align="center", valign="top", border=1, top=2)
    cell = dict(border=1)
    fmt = {
        "title": f(bold=True, font_size=20),
        "month_label": f(bold=True, font_size=18, align="right"),
        "month_value": f(bold=True, font_size=18),
        "input_head": f(bg_color=_INPUT_FILL, **head),
        "result_head": f(bold=True, bg_color=_RESULT_FILL, **head),
        "work_head": f(bg_color=_WORK_FILL, **head),
        "pct": f(num_format="0.0%", **cell),
        "num": f(num_format="General", **cell),
        "text": f(**cell),
        "wrap": f(text_wrap=True, valign="top"),
    }
    entries = tables["entries"]["rows"]
    shifts = tables["shifts"]["rows"]
    detail_sh = _Sheet(wb, DETAIL_SHEET, "Monthly production analysis", year, month,
                       DETAIL_COLUMNS,
                       lambda c: ("result_head" if c in _RESULT_HEADS else
                                  "work_head" if c in WORK_COLUMNS else "input_head"), fmt)
    month_results = set(pa.MONTH_COLUMNS[1:-2])
    month_sh = _Sheet(wb, MONTH_SHEET, "Operator efficiency for the month", year, month,
                      pa.MONTH_COLUMNS,
                      lambda c: "result_head" if c in month_results else "input_head", fmt)
    n_rows = _detail(detail_sh, entries, shifts,
                     tables.get("default_setup", pa.DEFAULT_STD_SETUP_MIN))
    _month(month_sh, tables["operators"]["rows"], detail_sh, n_rows)
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
