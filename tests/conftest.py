"""Shared fixtures: build the generated sample workbook once per session (Test3
format — there is no bundled Test2 anymore), and isolate the durable store to a
fresh temp dir per test (so the order book / actuals don't leak between tests or
touch the real ./data/store)."""
import io

import pytest

from engine.loaders import load_all
from engine.config import Config
from tests.sample_workbook import build_sample_bytes


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("STORE_DIR", str(tmp_path / "store"))
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    # Clear in-process auth state (rate limiter + cached session secret) so each
    # test starts clean and resolves its secret from its own isolated store.
    try:
        from api import auth
        auth.reset_auth_state()
    except Exception:
        pass
    yield


@pytest.fixture(scope="session")
def sample_bytes():
    """The generated sample workbook as .xlsx bytes (for upload tests)."""
    return build_sample_bytes()


@pytest.fixture(scope="session")
def loaded(sample_bytes):
    so_lines, masters = load_all(io.BytesIO(sample_bytes))
    return so_lines, masters


@pytest.fixture
def config():
    return Config()
