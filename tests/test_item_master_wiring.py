"""The Item Process Master reaches every planner: store, api masters, new engine,
cloud payload."""
import io
import json
from datetime import date

import pytest

from engine import book_store, item_master as im
from engine.models import Order
from tests.sample_workbook import build_sample_bytes, ITEM_A, ITEM_B


def test_store_round_trip():
    assert book_store.load_item_master() is None
    doc = im.seed_doc(build_sample_bytes(), "2026-10-05T10:00:00")
    book_store.save_item_master(doc)
    assert book_store.load_item_master() == doc
    assert list(book_store.load_item_master()["items"]) == [ITEM_A, ITEM_B]
