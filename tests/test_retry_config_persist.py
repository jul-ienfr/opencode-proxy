"""Persistence de la section `retry_until_success` (config.yaml top-level)."""

import yaml

import config.settings as st
import dashboard.api as api


def test_persist_retry_until_success_round_trip(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("server:\n  port: 4000\n", encoding="utf-8")
    monkeypatch.setattr(api, "__CONFIG_YAML_PATH", str(config_path))
    monkeypatch.setattr(api, "_config_yaml_mtime", lambda: 123.0)
    monkeypatch.setattr(api, "_config_yaml_known_mtime", 0.0, raising=False)

    missing = object()
    previous = st._yaml_data.get("retry_until_success", missing)
    try:
        err = api._persist_retry_until_success(
            {"enabled": True, "max_wait_s": 120, "base_delay_s": 0.5, "max_delay_s": 10}
        )
        assert err is None

        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        section = data["retry_until_success"]
        assert section["enabled"] is True
        assert section["max_wait_s"] == 120.0
        assert section["base_delay_s"] == 0.5
        assert section["max_delay_s"] == 10.0
        assert data["server"]["port"] == 4000, "les autres sections sont préservées"

        assert st._yaml_data["retry_until_success"]["enabled"] is True
        assert st._yaml_data["retry_until_success"]["max_wait_s"] == 120.0
    finally:
        if previous is missing:
            st._yaml_data.pop("retry_until_success", None)
        else:
            st._yaml_data["retry_until_success"] = previous


def test_persist_retry_until_success_clamps(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("server:\n  port: 4000\n", encoding="utf-8")
    monkeypatch.setattr(api, "__CONFIG_YAML_PATH", str(config_path))
    monkeypatch.setattr(api, "_config_yaml_mtime", lambda: 123.0)
    monkeypatch.setattr(api, "_config_yaml_known_mtime", 0.0, raising=False)

    missing = object()
    previous = st._yaml_data.get("retry_until_success", missing)
    try:
        err = api._persist_retry_until_success(
            {"max_wait_s": -5, "base_delay_s": 0, "max_delay_s": 1, "keepalive_interval_s": 0}
        )
        assert err is None
        section = yaml.safe_load(config_path.read_text(encoding="utf-8"))["retry_until_success"]
        assert section["max_wait_s"] == 0.0
        assert section["base_delay_s"] == 0.05
        assert section["max_delay_s"] == 1.0
        assert section["keepalive_interval_s"] == 1.0
    finally:
        if previous is missing:
            st._yaml_data.pop("retry_until_success", None)
        else:
            st._yaml_data["retry_until_success"] = previous
