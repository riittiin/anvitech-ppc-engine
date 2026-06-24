"""Config knobs — focus on the apply_downtime_to_plan flag (downtime loop-back gate)."""
import pytest

from engine.config import Config


def test_apply_downtime_defaults_off():
    # Engine default is OFF so existing plans / the golden trace are unaffected.
    assert Config().apply_downtime_to_plan is False


def test_from_dict_sets_flag():
    cfg = Config.from_dict({"apply_downtime_to_plan": True})
    assert cfg.apply_downtime_to_plan is True


def test_to_dict_round_trips_flag():
    assert Config(apply_downtime_to_plan=True).to_dict()["apply_downtime_to_plan"] is True


def test_non_bool_flag_rejected():
    with pytest.raises(ValueError):
        Config(apply_downtime_to_plan="yes").validate()
