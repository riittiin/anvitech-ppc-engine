"""Storage layer: local file backend kv/hash/list round-trips + backend selection."""
from engine import storage


def test_kv_round_trip():
    s = storage.get_store()
    assert s.kv_get("missing") is None
    s.kv_set("k", "hello")
    assert s.kv_get("k") == "hello"


def test_hash_ops():
    s = storage.get_store()
    assert s.hgetall("orders") == {}
    s.hset("orders", "SO1", "a")
    s.hset("orders", "SO2", "b")
    assert s.hgetall("orders") == {"SO1": "a", "SO2": "b"}
    s.hdel("orders", "SO1")
    assert s.hgetall("orders") == {"SO2": "b"}


def test_list_append_is_additive():
    s = storage.get_store()
    assert s.list_all("acts") == []
    s.list_append("acts", "one")
    s.list_append("acts", "two")
    assert s.list_all("acts") == ["one", "two"]


def test_local_store_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    assert isinstance(storage.get_store(), storage.LocalStore)


def test_upstash_store_when_configured(monkeypatch):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://x.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "tok")
    assert isinstance(storage.get_store(), storage.UpstashStore)
