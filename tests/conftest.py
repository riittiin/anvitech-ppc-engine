"""Shared fixtures: load the real Test2.xlsx once per session, and isolate the
durable store to a fresh temp dir per test (so the order book / actuals don't
leak between tests or touch the real ./data/store)."""
import pytest

from engine.loaders import load_all
from engine.config import Config


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    yield


@pytest.fixture(scope="session")
def loaded():
    so_lines, masters = load_all()
    return so_lines, masters


@pytest.fixture
def config():
    return Config()
