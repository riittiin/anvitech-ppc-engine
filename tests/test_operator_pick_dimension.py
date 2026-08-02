"""Operator-assignment (operator_pick) as a 4th Optimize dimension (2026-08-02)."""
import json

import pytest

from engine.config import Config


def test_operator_pick_defaults_to_scarce():
    assert Config().operator_pick == "scarce"


def test_operator_pick_round_trips():
    c = Config(operator_pick="balanced")
    assert Config.from_dict(c.to_dict()).operator_pick == "balanced"
    # to_dict must carry it (no special-casing needed — it's a plain str field).
    assert c.to_dict()["operator_pick"] == "balanced"


def test_operator_pick_blank_coerces_to_scarce():
    assert Config.from_dict({"operator_pick": ""}).operator_pick == "scarce"
    assert Config.from_dict({"operator_pick": None}).operator_pick == "scarce"


def test_operator_pick_invalid_is_rejected():
    with pytest.raises(ValueError):
        Config(operator_pick="nope").validate()
