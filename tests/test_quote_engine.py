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


# --------------------------------------------------------------------------- #
# Task 7: engine/quote.py
# --------------------------------------------------------------------------- #
from datetime import timedelta

from engine import optimizer, quote as quote_mod


def test_a_quote_returns_a_completion_date_for_each_new_line(loaded):
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    item = so_lines[0].item_code
    new = [quote_mod.QuoteLine("NEW-1", item, "x", 10)]
    res = quote_mod.quote(stage1, optimizer.expected_completion(stage1),
                          new, _CONF, masters)
    assert len(res.lines) == 1
    assert res.lines[0]["completion"] is not None
    assert res.lines[0]["error"] is None


def test_a_quote_moves_no_existing_order(loaded):
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    before = optimizer.expected_completion(stage1)
    item = so_lines[0].item_code
    new = [quote_mod.QuoteLine("NEW-1", item, "x", 50),
           quote_mod.QuoteLine("NEW-2", so_lines[-1].item_code, "y", 20)]
    res = quote_mod.quote(stage1, before, new, _CONF, masters)
    assert res.moved == []
    assert res.violations == []
    assert res.verified is True
    assert res.existing_count == len(before)


def test_a_new_order_never_finishes_before_the_plan_starts(loaded):
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    new = [quote_mod.QuoteLine("NEW-1", so_lines[0].item_code, "x", 10)]
    res = quote_mod.quote(stage1, optimizer.expected_completion(stage1),
                          new, _CONF, masters)
    assert res.lines[0]["completion"] >= _CONF.plan_start_date


def test_an_item_with_no_routing_is_reported_not_crashed(loaded):
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    new = [quote_mod.QuoteLine("NEW-1", "NO-SUCH-ITEM", "ghost", 5)]
    res = quote_mod.quote(stage1, optimizer.expected_completion(stage1),
                          new, _CONF, masters)
    assert res.lines[0]["completion"] is None
    assert "routing" in res.lines[0]["error"].lower()


def test_more_new_orders_never_pull_an_existing_order_earlier_or_later(loaded):
    """Stage 1 is literally the same calculation whatever stage 2 is asked to do."""
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    before = optimizer.expected_completion(stage1)
    item = so_lines[0].item_code
    for n in (1, 3, 6):
        new = [quote_mod.QuoteLine(f"NEW-{i}", item, "x", 25) for i in range(n)]
        res = quote_mod.quote(stage1, before, new, _CONF, masters)
        assert res.moved == [], f"{n} new orders disturbed the book"
        assert res.violations == [], f"{n} new orders disturbed the book (structural)"


def test_arrangement_counts_above_the_exhaustive_max():
    """Documents the actual arrangement counts tried once the fixture is too big
    for exhaustive permutations: rotations only, one per line, never more than
    _SAMPLED_ARRANGEMENTS."""
    def lines(n):
        return [quote_mod.QuoteLine(f"S{i}", "ITEM", "x", 1) for i in range(n)]

    assert len(quote_mod._arrangements(lines(5))) == 5
    assert len(quote_mod._arrangements(lines(10))) == 10
    assert len(quote_mod._arrangements(lines(20))) == 20


def test_moved_field_actually_catches_a_moved_order():
    """verify_unmoved must be able to fail: feed it a caller-claimed date that
    does not match what the same stage-1 entries actually produced."""
    real = {("SO1", "ITEM"): date(2025, 3, 10), ("SO2", "ITEM"): date(2025, 3, 12)}
    lied = dict(real)
    lied[("SO1", "ITEM")] = date(1999, 1, 1)
    moved = quote_mod.verify_unmoved(lied, real)
    assert len(moved) == 1
    assert moved[0]["so_no"] == "SO1"


def test_structural_check_catches_a_double_booked_machine(loaded):
    """Feeding a fabricated stage-2 entry that reuses a stage-1 machine interval
    exactly must trip the machine overlap check."""
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    machine_entry = next(e for e in stage1 if e.machine not in new_engine.OFF_LANES)
    fake_stage2 = [dataclasses.replace(machine_entry, so_refs=["NEW-CLASH"])]
    problems = quote_mod.structural_violations(stage1, fake_stage2, _CONF)
    assert any("double-booked" in p and machine_entry.machine in p for p in problems)


def test_structural_check_catches_a_double_booked_operator(loaded):
    """Same idea, on the operator dimension: same person, same time, a different
    (synthetic) machine so only the operator check can fire."""
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    entry_with_operator = next(e for e in stage1 if e.op_segments)
    fake_stage2 = [dataclasses.replace(entry_with_operator, machine="A-MACHINE-NOBODY-USES",
                                       so_refs=["NEW-CLASH"])]
    problems = quote_mod.structural_violations(stage1, fake_stage2, _CONF)
    assert any("double-booked" in p and "operator" in p for p in problems)


def test_structural_check_catches_a_reused_order_key(loaded):
    """A "new" schedule that reuses an existing order's (SO, item) key, placed a
    year later on a machine nobody else uses so time and resource cannot possibly
    collide. Only the order-key check can catch this."""
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    existing = stage1[0]
    fake_stage2 = [dataclasses.replace(
        existing, machine="A-MACHINE-NOBODY-USES", operator="", op_segments=[],
        start=existing.start + timedelta(days=365), end=existing.end + timedelta(days=365))]
    problems = quote_mod.structural_violations(stage1, fake_stage2, _CONF)
    assert any("appears in both" in p for p in problems)


def test_when_stage2_cannot_schedule_it_reports_a_clear_error(loaded, monkeypatch):
    """If every arrangement fails to schedule (here, forced by monkeypatching
    _stage2 to always raise), the result must still be a well-formed
    QuoteResult explaining why per line, never a raised exception."""
    so_lines, masters = loaded
    stage1 = _plan(so_lines, masters)
    before = optimizer.expected_completion(stage1)

    def _boom(*a, **k):
        raise RuntimeError("no machine free")

    monkeypatch.setattr(quote_mod, "_stage2", _boom)
    new = [quote_mod.QuoteLine("NEW-1", so_lines[0].item_code, "x", 10)]
    res = quote_mod.quote(stage1, before, new, _CONF, masters)
    assert res.lines[0]["completion"] is None
    assert res.lines[0]["error"] and "no machine free" in res.lines[0]["error"]
    assert res.existing_count == len(before)
