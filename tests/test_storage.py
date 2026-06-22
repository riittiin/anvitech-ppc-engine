"""Durable-store layer: off by default; backs actuals when configured."""
from datetime import date

from engine import storage
from engine.models import Actual
from engine.rules import rule8_capture_actuals


def test_get_kv_none_without_env(monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    assert storage.get_kv() is None


def test_get_kv_configured_with_env(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://example.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    kv = storage.get_kv()
    assert isinstance(kv, storage.UpstashKV)
    assert kv.url == "https://example.upstash.io"


class _FakeKV:
    def __init__(self):
        self.store = {}
    def get(self, k):
        return self.store.get(k)
    def set(self, k, v):
        self.store[k] = v


def test_actuals_round_trip_through_store(monkeypatch):
    fake = _FakeKV()
    monkeypatch.setattr(rule8_capture_actuals, "get_kv", lambda: fake)

    a = Actual(so_no="SO1", item_code="X", entry_date=date(2025, 8, 1),
               qty_produced=10, qty_rejected=2, no_operator_min=30)
    rule8_capture_actuals.run(a)                 # saves to the store, not a file
    assert rule8_capture_actuals.ACTUALS_KEY in fake.store

    reloaded = rule8_capture_actuals.load_actuals()
    assert len(reloaded) == 1
    assert reloaded[0].good_qty() == 8
    assert reloaded[0].no_operator_min == 30
