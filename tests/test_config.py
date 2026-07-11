"""Config knobs. The recorded-downtime→plan gate was removed (the feedback loop is
quantity-only), so the config no longer carries an apply_downtime_to_plan flag."""
from engine.config import Config


def test_downtime_flag_is_gone():
    assert not hasattr(Config(), "apply_downtime_to_plan")


def test_from_dict_ignores_removed_downtime_flag():
    # A persisted config that still carries the old key must not break.
    cfg = Config.from_dict({"apply_downtime_to_plan": True, "setup_time_min": 120})
    assert not hasattr(cfg, "apply_downtime_to_plan")
    assert cfg.setup_time_min == 120


def test_to_dict_round_trips_real_knobs():
    cfg = Config(setup_time_min=75, consolidation_window_days=7)
    d = cfg.to_dict()
    assert d["setup_time_min"] == 75 and d["consolidation_window_days"] == 7
    assert "apply_downtime_to_plan" not in d


def test_expedite_window_defaults_off_and_round_trips():
    # Off by default (current plans unchanged) and survives a to_dict/from_dict trip
    # so the admin's Settings tick mark persists with the saved plan config.
    assert Config().expedite_window_min == 0
    cfg = Config.from_dict({"expedite_window_min": 45})
    assert cfg.expedite_window_min == 45
    assert Config.from_dict(cfg.to_dict()).expedite_window_min == 45
