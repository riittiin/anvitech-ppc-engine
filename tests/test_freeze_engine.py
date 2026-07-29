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
