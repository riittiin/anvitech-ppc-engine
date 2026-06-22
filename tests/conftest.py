"""Shared fixtures: load the real Test2.xlsx once per session."""
import pytest

from engine.loaders import load_all
from engine.config import Config


@pytest.fixture(scope="session")
def loaded():
    so_lines, masters = load_all()
    return so_lines, masters


@pytest.fixture
def config():
    return Config()
