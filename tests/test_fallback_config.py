"""FallbackConfig 007 : ordre gratuit d'abord, bornes et validation."""

from __future__ import annotations

from free.fallback import DEFAULT_ORDER, FallbackConfig


def test_defaults_are_free_first_and_fail_closed():
    cfg = FallbackConfig.from_mapping({})
    assert cfg.order == DEFAULT_ORDER
    assert cfg.order[0] == "warp"
    assert cfg.order[-1] == "vps_ipv6"
    assert cfg.max_fallback_stations == 2
    assert cfg.fail_closed is True
    assert cfg.max_block_s == 0


def test_order_filters_unknown_and_deduplicates():
    cfg = FallbackConfig.from_mapping(
        {"order": ["WARP", "unknown", "warp", "proton"]}
    )
    assert cfg.order == ("warp", "proton")


def test_order_empty_falls_back_to_default():
    assert FallbackConfig.from_mapping({"order": []}).order == DEFAULT_ORDER


def test_bounds_are_clamped():
    cfg = FallbackConfig.from_mapping(
        {
            "max_fallback_stations": 9,
            "probe_interval_s": 1,
            "recovery_successes": 0,
            "cooldown_s": 1,
            "max_block_s": 999999,
        }
    )
    assert cfg.max_fallback_stations == 2
    assert cfg.probe_interval_s == 60
    assert cfg.recovery_successes == 1
    assert cfg.cooldown_s == 300
    assert cfg.max_block_s == 86400


def test_bool_string_and_as_dict():
    cfg = FallbackConfig.from_mapping({"fail_closed": "false"})
    assert cfg.fail_closed is False
    assert cfg.as_dict()["order"][0] == "warp"


def test_config_settings_loader(monkeypatch):
    import config.settings as settings

    monkeypatch.setitem(
        settings._yaml_data,
        "ip_rotation",
        {"fallback": {"order": ["warp", "proton"], "max_fallback_stations": 1}},
    )
    loaded = settings.fallback_config()
    assert loaded["order"] == ["warp", "proton"]
    assert loaded["max_fallback_stations"] == 1
