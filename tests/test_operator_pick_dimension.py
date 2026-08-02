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


def test_operator_pick_candidates_are_scarce_and_balanced():
    from engine.optimizer import OPERATOR_PICK_CANDIDATES
    assert OPERATOR_PICK_CANDIDATES == ("scarce", "balanced")


def test_operator_pick_contenders_put_current_first():
    from engine.optimizer import operator_pick_contenders
    assert operator_pick_contenders("balanced")[0] == "balanced"
    assert operator_pick_contenders("scarce") == ["scarce", "balanced"]
    # An off-list current policy still joins its own contest, first.
    assert operator_pick_contenders("flexible")[0] == "flexible"
    assert set(operator_pick_contenders("flexible")) == {"flexible", "scarce", "balanced"}


def test_sweepresult_defaults_operator_pick_scarce():
    from engine.optimizer import SweepResult
    assert SweepResult().operator_pick == "scarce"
