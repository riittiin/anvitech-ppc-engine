"""Machine maintenance: the plumbing that makes a break reach every plan —
the book signature, the cloud payload and the contest setup."""
import io
import json
from datetime import date

from engine import book_store, loaders, optimize_service as svc, orderbook
from engine.config import Config
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A


def _book():
    wb = build_sample_bytes()
    book_store.save_masters_bytes(wb)
    _so, masters = loaders.load_all(io.BytesIO(wb))
    book_store.add_orders([Order("SO1", ITEM_A, "A", 20, date(2025, 3, 20))])
    cfg = Config(plan_start_date=date(2025, 3, 1))
    return book_store.load_active_orders(), cfg, masters


def _lines():
    orders, _cfg, masters = _book()
    return orderbook.active_so_lines(orders, book_store.load_actuals(), masters)


DOWN = [{"machine": "CNC1", "from_date": "2025-03-05", "to_date": "2025-03-06"}]


def test_book_signature_moves_when_a_break_is_added():
    lines = _lines()
    assert svc.book_signature(lines) != svc.book_signature(lines, downtimes=DOWN)


def test_book_signature_is_byte_identical_for_the_empty_case():
    """No pre-existing caller's signature may move just because the parameter
    now exists (the same guard `frozen` carries)."""
    lines = _lines()
    base = svc.book_signature(lines)
    assert svc.book_signature(lines, downtimes=[]) == base
    assert svc.book_signature(lines, downtimes=None) == base
    assert svc.book_signature(lines, absences=[], frozen=[], downtimes=[]) == base


def test_book_signature_is_order_insensitive_and_restores():
    lines = _lines()
    two = DOWN + [{"machine": "VMC1", "from_date": "2025-03-09", "to_date": "2025-03-09"}]
    assert svc.book_signature(lines, downtimes=two) == \
           svc.book_signature(lines, downtimes=list(reversed(two)))
    a = svc.book_signature(lines, downtimes=two)
    assert svc.book_signature(lines, downtimes=DOWN) != a
    assert svc.book_signature(lines, downtimes=two) == a


def test_a_downtime_only_book_cannot_collide_with_a_frozen_only_book():
    """The appended element is tagged, so the two optional blocks are never
    structurally confusable."""
    lines = _lines()
    frozen = [{"so_no": "SO1", "item_code": ITEM_A, "op_seq": 2,
               "machine": "CNC1", "remaining_qty": 4}]
    assert svc.book_signature(lines, frozen=frozen) != \
           svc.book_signature(lines, downtimes=DOWN)


def test_payload_round_trips_machine_downtime():
    orders, cfg, _m = _book()
    payload = svc.build_payload(orders, [], build_sample_bytes(), cfg, seed=42,
                                machine_downtime=DOWN)
    assert payload["machine_downtime"] == DOWN
    payload = json.loads(json.dumps(payload))          # the network hop
    result = svc.parse_payload(payload)
    assert len(result) == 8
    assert result[-1] == DOWN


def test_an_older_payload_without_the_key_still_parses():
    orders, cfg, _m = _book()
    payload = svc.build_payload(orders, [], build_sample_bytes(), cfg, seed=42)
    payload.pop("machine_downtime")                    # a job dispatched pre-deploy
    assert svc.parse_payload(payload)[-1] == []


def test_prepare_contest_reserves_the_machine_alongside_the_operators():
    orders, cfg, masters = _book()
    setup = svc.prepare_contest(orders, [], masters, cfg, machine_downtime=DOWN)
    assert "CNC1" in setup.unavailable_reserved
    assert setup.machine_downtime == DOWN


def test_prepare_contest_with_no_breaks_reserves_nothing():
    orders, cfg, masters = _book()
    setup = svc.prepare_contest(orders, [], masters, cfg)
    assert setup.unavailable_reserved is None
    assert setup.machine_downtime == []
