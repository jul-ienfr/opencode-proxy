"""Contract tests for the ``control_plane`` configuration section."""

from __future__ import annotations

import pytest

from free.control_plane.config import (
    ControlPlaneConfig,
    EventsConfig,
    RetryConfig,
    ShadowConfig,
    load_control_plane_config,
)
from free.control_plane.errors import ConfigError


def test_defaults_are_legacy_and_resolve_station_urls():
    config = ControlPlaneConfig()

    assert config.mode == "legacy"
    assert not config.is_active
    assert config.transport_owner == "python"
    assert config.base_url_for(3) == "http://vpn-gluetun-3:8000"
    assert config.base_url_for("vpn-gluetun-6") == ("http://vpn-gluetun-vpn-gluetun-6:8000")


def test_repo_config_activates_the_fork_control_plane():
    config = load_control_plane_config()

    assert config.mode == "active"
    assert config.is_active
    assert config.transport_owner == "fork"
    assert config.base_url_for(6) == "http://127.0.0.1:8006"
    assert config.base_url_for(1) == "http://127.0.0.1:8001"


def test_station_placeholder_requires_a_station():
    config = ControlPlaneConfig()
    with pytest.raises(ConfigError):
        config.base_url_for(None)


def test_invalid_mode_is_rejected():
    with pytest.raises(ConfigError, match="control_plane.mode"):
        ControlPlaneConfig(mode="enabled")


def test_invalid_transport_owner_is_rejected():
    with pytest.raises(ConfigError, match="transport_owner"):
        ControlPlaneConfig(transport_owner="both")


def test_api_key_comes_from_the_environment_and_stays_out_of_repr():
    config = ControlPlaneConfig(api_key_env="MY_CONTROL_KEY")

    assert config.api_key({"MY_CONTROL_KEY": "top-secret"}) == "top-secret"
    assert config.api_key({}) == ""
    assert "top-secret" not in repr(config)
    assert "MY_CONTROL_KEY" in repr(config)


def test_custom_file_overrides_every_leaf(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text(
        """
control_plane:
  mode: active
  base_url: "http://127.0.0.1:9999"
  api_key_env: CUSTOM_KEY
  timeout_s: 3
  switch_timeout_s: 30
  rotate_timeout_s: 90
  verify_timeout_s: 4
  idempotency_window_h: 12
  transport_owner: fork
  retry:
    max_attempts: 2
    backoff_s: 0.25
    max_backoff_s: 1.5
  shadow:
    sample_rate: 0.5
    compare_window_s: 60
  events:
    ingest: false
    batch_size: 25
    poll_interval_s: 2
""",
        encoding="utf-8",
    )

    config = load_control_plane_config(target)

    assert config.mode == "active"
    assert config.is_active
    assert config.base_url_for(2) == "http://127.0.0.1:9999"
    assert config.api_key_env == "CUSTOM_KEY"
    assert config.timeout_s == 3
    assert config.switch_timeout_s == 30
    assert config.rotate_timeout_s == 90
    assert config.verify_timeout_s == 4
    assert config.idempotency_window_h == 12
    assert config.transport_owner == "fork"
    assert config.retry == RetryConfig(max_attempts=2, backoff_s=0.25, max_backoff_s=1.5)
    assert config.shadow == ShadowConfig(sample_rate=0.5, compare_window_s=60)
    assert config.events == EventsConfig(ingest=False, batch_size=25, poll_interval_s=2)


def test_invalid_mode_in_file_is_rejected(tmp_path):
    target = tmp_path / "config.yaml"
    target.write_text("control_plane:\n  mode: dry-run\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        load_control_plane_config(target)


def test_retry_delay_is_bounded_and_honours_retry_after():
    retry = RetryConfig(max_attempts=3, backoff_s=0.5, max_backoff_s=2.0)

    assert retry.delay(0) == 0.5
    assert retry.delay(1) == 1.0
    assert retry.delay(2) == 2.0
    assert retry.delay(3) == 2.0
    assert retry.delay(0, retry_after_s=10) == 2.0
