# tests/test_routing_rows.py
"""Both loaders read routings through ONE rows->routings function, whatever the
source (workbook sheet or the app's Item Process Master table)."""
import io

from ppc_engine.loaders import loader as ppc_loader
from ppc_engine.loaders.masters_loader import routing_rows_from_sheet
from ppc_engine.loaders.workbook import open_workbook
from tests.new_sample_workbook import build_new_sample_bytes
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def _ppc_rows(raw):
    wb = open_workbook(io.BytesIO(raw))
    try:
        return routing_rows_from_sheet(wb)
    finally:
        wb.close()


def test_ppc_rows_carry_raw_steps():
    rows = _ppc_rows(build_sample_bytes())
    by_code = {code: (desc, steps) for code, desc, steps in rows}
    desc, steps = by_code[ITEM_A]
    assert desc == "SAMPLE RING A"
    assert [s[0] for s in steps] == ["BANDSAW", "CNC OS", "INSP"]
    assert steps[1][1] == 5 and steps[1][3] == "CNC1/CNC2"
    assert [s[0] for s in by_code[ITEM_B][1]] == ["CNC", "WASHING"]


def test_ppc_load_all_from_rows_equals_from_sheet():
    for raw in (build_sample_bytes(), build_new_sample_bytes()):
        rows = _ppc_rows(raw)
        for flexible in (False, True):
            a = ppc_loader.load_all(io.BytesIO(raw), flexible_machines=flexible)
            b = ppc_loader.load_all(io.BytesIO(raw), flexible_machines=flexible,
                                    routing_rows=rows)
            assert a.masters.routings == b.masters.routings
            assert list(a.masters.routings) == list(b.masters.routings)
            assert a.masters.machines == b.masters.machines
            assert [(g.kind, g.ref) for g in a.report.gaps] == \
                   [(g.kind, g.ref) for g in b.report.gaps]


def test_ppc_rows_register_provisional_machines():
    """CNC9 is used only by ITEM_B's routing: the rows path must still register it."""
    rows = [(ITEM_B, "PIN", [("CNC", 4, None, "CNC9", None)])]
    res = ppc_loader.load_all(io.BytesIO(build_sample_bytes()), routing_rows=rows)
    assert "CNC9" in res.masters.machines
    assert list(res.masters.routings) == [ITEM_B]
