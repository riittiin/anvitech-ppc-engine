"""Machines and holidays reach both loaders through ONE rows rule each, whatever the
source (workbook sheet or the app's tables), and with all three tables no workbook
is opened."""
import io
from datetime import date

import pytest

from ppc_engine.loaders import loader as ppc_loader
from ppc_engine.loaders.masters_loader import (
    calendar_from_holiday_rows, holiday_rows_from_sheet, machine_rows_from_sheet,
    routing_rows_from_sheet)
from ppc_engine.loaders.workbook import open_workbook
from tests.new_sample_workbook import build_new_sample_bytes
from tests.sample_workbook import build_sample_bytes


def _rows(raw):
    wb = open_workbook(io.BytesIO(raw))
    try:
        return (routing_rows_from_sheet(wb), machine_rows_from_sheet(wb),
                holiday_rows_from_sheet(wb))
    finally:
        wb.close()


def test_ppc_machine_rows_are_raw_cells():
    _r, machines, _h = _rows(build_sample_bytes())
    assert machines and all(len(m) == 3 for m in machines)


def test_ppc_tables_only_equals_workbook_masters():
    for raw in (build_sample_bytes(), build_new_sample_bytes()):
        routing, machines, holidays = _rows(raw)
        for flexible in (False, True):
            a = ppc_loader.load_all(io.BytesIO(raw), flexible_machines=flexible,
                                    routing_rows=routing)
            b = ppc_loader.load_all(None, flexible_machines=flexible, routing_rows=routing,
                                    machine_rows=machines, holiday_rows=holidays)
            assert a.masters.machines == b.masters.machines
            assert list(a.masters.machines) == list(b.masters.machines)
            assert a.masters.routings == b.masters.routings
            assert a.masters.calendar.holidays == b.masters.calendar.holidays
            assert a.masters.calendar.weekly_off_weekday == b.masters.calendar.weekly_off_weekday
            assert b.masters.calendar.leaves == {}
            assert b.orders == [] and b.masters.operators == ()


def test_ppc_tables_only_never_opens_a_workbook(monkeypatch):
    routing, machines, holidays = _rows(build_sample_bytes())
    def boom(*a, **k):
        raise AssertionError("a workbook was opened")
    monkeypatch.setattr(ppc_loader, "open_workbook", boom)
    ppc_loader.load_all(None, routing_rows=routing, machine_rows=machines, holiday_rows=holidays)


def test_ppc_partial_tables_without_a_workbook_is_an_error():
    routing, machines, _h = _rows(build_sample_bytes())
    with pytest.raises(ValueError):
        ppc_loader.load_all(None, routing_rows=routing, machine_rows=machines)


def test_calendar_from_holiday_rows_is_thursday_without_leaves():
    cal = calendar_from_holiday_rows([(date(2026, 8, 15), "Independence Day")])
    assert cal.weekly_off_weekday == 3
    assert cal.holidays == frozenset({date(2026, 8, 15)})
    assert cal.leaves == {}
