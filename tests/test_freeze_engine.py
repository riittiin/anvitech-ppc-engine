import io
from datetime import date
import pytest

from engine.config import Config
from engine.new_engine import _orders_from_batches, _plan_config
from engine.rules import rule1_consolidate
from engine import book_store, loaders, orderbook
from ppc_engine.loaders import load_all as new_load
from ppc_engine.scheduler import decode, FrozenOp
from tests.new_sample_workbook import build_new_sample_bytes

_CONF = Config(scheduler="new", plan_start_date=date(2025, 3, 3), apply_operator_logic=True)

@pytest.fixture()
def ctx():
    wb = build_new_sample_bytes()
    book_store.save_masters_bytes(wb)
    nm = new_load(io.BytesIO(wb)).masters
    so_lines, _ = loaders.load_all(io.BytesIO(wb))
    batches = rule1_consolidate.run(so_lines, _CONF)
    orders, _ = _orders_from_batches(batches, nm)
    seq = [o.key for o in orders]
    return orders, seq, nm

def test_decode_frozen_none_is_byte_identical(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    a = decode(orders, seq, nm, cfg)
    b = decode(orders, seq, nm, cfg, frozen=None)
    c = decode(orders, seq, nm, cfg, frozen=[])
    assert a.segments == b.segments == c.segments
    assert a.completion == b.completion == c.completion


def _entry(segs, order_key, op_seq):
    got = [s for s in segs if s.order_key == order_key and s.op_seq == op_seq]
    return sorted(got, key=lambda s: s.start)


def test_single_frozen_op_pinned_to_machine_operator_no_setup(ctx):
    orders, seq, nm = ctx
    cfg = _plan_config(_CONF)
    # Pick the first order's first machining op; find a machine option it has.
    o0 = orders[0]
    routing = nm.routings[o0.item_code]
    mach_op = next(op for op in routing.operations if op.machine_options and op.cycle_min > 0)
    mid = mach_op.machine_options[0]
    # Freeze 5 pieces of it on that machine with operator "Alpha", starting at plan_start.
    fo = FrozenOp(order_key=o0.key, op_seq=mach_op.seq, machine_id=mid,
                  operator="Alpha", remaining_qty=5, prev_start=cfg.plan_start)
    sched = decode(orders, seq, nm, cfg, frozen=[fo])
    segs = _entry(sched.segments, o0.key, mach_op.seq)
    assert segs, "frozen op was not scheduled"
    assert all(s.machine_id == mid for s in segs), "frozen op left its pinned machine"
    assert segs[0].operator == "Alpha", "frozen op not run by the planned operator"
    assert segs[0].start == cfg.plan_start, "frozen op did not resume at plan start"
    # No setup: total minutes == 5 * cycle (machining setup would add setup_min).
    total_min = sum((s.end - s.start).total_seconds() for s in segs) / 60.0
    assert abs(total_min - 5 * mach_op.cycle_min) < 1e-6, "frozen op charged setup time"
