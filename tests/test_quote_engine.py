"""engine/quote.py and its adapter layer (Add New Orders, 2026-09-08 spec)."""
import dataclasses
import io
from datetime import date

import pytest

from engine import book_store, loaders, new_engine
from engine.config import Config
from engine.models import PlanRun
from engine.pipeline import run_forward
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3),
               apply_operator_logic=True)


@pytest.fixture()
def loaded():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    new_engine.set_masters_bytes(wb)
    so_lines, masters = loaders.load_all(io.BytesIO(wb))
    return so_lines, masters


def _plan(so_lines, masters, occupancy=None):
    pr = PlanRun(so_lines=list(so_lines))
    run_forward(pr, _CONF, masters, occupancy=occupancy)
    return pr.schedule


def test_off_lanes_have_one_definition():
    from engine import freeze
    assert freeze._OS_LANES is new_engine.OFF_LANES


def test_occupancy_lists_every_machine_block_and_skips_outsourcing(loaded):
    so_lines, masters = loaded
    entries = _plan(so_lines, masters)
    occ = new_engine.occupancy_from_entries(entries, _CONF)
    real = [e for e in entries if e.machine not in new_engine.OFF_LANES]
    assert sum(len(v) for v in occ["machine"].values()) == len(real)
    assert not (set(occ["machine"]) & new_engine.OFF_LANES)


def test_occupancy_lists_every_operator_segment(loaded):
    so_lines, masters = loaded
    entries = _plan(so_lines, masters)
    occ = new_engine.occupancy_from_entries(entries, _CONF)
    expected = sum(1 for e in entries for (_s, _e, name) in (e.op_segments or [])
                   if name)
    assert sum(len(v) for v in occ["operator"].values()) == expected


def test_planning_with_occupancy_keeps_new_work_off_occupied_machines(loaded):
    so_lines, masters = loaded
    half = max(1, len(so_lines) // 2)
    stage1 = _plan(so_lines[:half], masters)
    occ = new_engine.occupancy_from_entries(stage1, _CONF)
    stage2 = _plan(so_lines[half:], masters, occupancy=occ)

    busy = {}
    for e in stage1:
        if e.machine not in new_engine.OFF_LANES:
            busy.setdefault(e.machine, []).append((e.start, e.end))
    for e in stage2:
        for bs, be in busy.get(e.machine, ()):
            assert e.end <= bs or e.start >= be


def test_occupancy_actually_delays_a_step_the_routing_would_otherwise_allow_now(loaded):
    """The fixture used above has item B's second machining step (CNC SECOND SIDE)
    follow an outsourced (OS) first step, so it can never start before the OS block
    ends anyway — with OR without occupancy, both land on the same time. That made
    ``test_planning_with_occupancy_keeps_new_work_off_occupied_machines`` pass even
    with ``run``'s ``if occupancy:`` block deleted outright (mutation-tested,
    2026-09-08 task 6): the routing precedence alone happened to produce the same
    dates. This test removes that coincidence: two orders of the SAME item (its
    very FIRST step, no predecessor to delay it) compete for the same machine, so
    only occupancy — never routing order — can be what pushes the second one out."""
    so_lines, masters = loaded
    item_a_line = next(s for s in so_lines if s.item_code == "NEW-A-01")
    twin = dataclasses.replace(item_a_line, so_no="NSO-999")

    stage1 = _plan([item_a_line], masters)
    occ = new_engine.occupancy_from_entries(stage1, _CONF)
    stage2 = _plan([twin], masters, occupancy=occ)

    busy = {e.machine: (e.start, e.end) for e in stage1
            if e.machine not in new_engine.OFF_LANES}
    saw_a_shared_machine = False
    for e in stage2:
        if e.machine in busy:
            saw_a_shared_machine = True
            bs, be = busy[e.machine]
            assert e.end <= bs or e.start >= be
    assert saw_a_shared_machine, "fixture no longer shares a machine between the two stages"


def test_no_occupancy_plans_exactly_as_before(loaded):
    so_lines, masters = loaded
    a = _plan(so_lines, masters)
    b = _plan(so_lines, masters, occupancy=None)
    assert [(e.batch_id, e.process_seq, e.machine, e.operator, e.start, e.end)
            for e in a] == \
           [(e.batch_id, e.process_seq, e.machine, e.operator, e.start, e.end)
            for e in b]
