"""Typed ``control_plane`` configuration.

Every leaf is read explicitly through :func:`config.settings.yaml_get` so the
config audit sees it as consumed.  The API key is never stored in the config
object: it is resolved from the environment at call time, which keeps it out of
``repr``, logs and telemetry.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from config.settings import yaml_get

from .errors import ConfigError

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"

VALID_MODES = ("legacy", "shadow", "active")
VALID_TRANSPORT_OWNERS = ("python", "fork")


@dataclass(frozen=True, slots=True)
class RetryConfig:
    max_attempts: int = 3
    backoff_s: float = 0.5
    max_backoff_s: float = 4.0

    def delay(self, attempt: int, retry_after_s: float = 0.0) -> float:
        if retry_after_s > 0:
            return min(retry_after_s, self.max_backoff_s)
        base = self.backoff_s * (2 ** max(0, attempt))
        return min(base, self.max_backoff_s)


@dataclass(frozen=True, slots=True)
class ShadowConfig:
    sample_rate: float = 1.0
    compare_window_s: float = 300.0


@dataclass(frozen=True, slots=True)
class EventsConfig:
    ingest: bool = True
    batch_size: int = 100
    poll_interval_s: float = 5.0


@dataclass(frozen=True, slots=True)
class ControlPlaneConfig:
    mode: str = "legacy"
    base_url: str = "http://vpn-gluetun-{station}:8000"
    api_key_env: str = "GLUETUN_CONTROL_API_KEY"
    timeout_s: float = 10.0
    switch_timeout_s: float = 60.0
    rotate_timeout_s: float = 180.0
    verify_timeout_s: float = 10.0
    idempotency_window_h: int = 24
    transport_owner: str = "python"
    retry: RetryConfig = field(default_factory=RetryConfig)
    shadow: ShadowConfig = field(default_factory=ShadowConfig)
    events: EventsConfig = field(default_factory=EventsConfig)

    def __post_init__(self) -> None:
        if self.mode not in VALID_MODES:
            raise ConfigError(f"control_plane.mode must be one of {VALID_MODES}, got {self.mode!r}")
        if self.transport_owner not in VALID_TRANSPORT_OWNERS:
            raise ConfigError(
                f"control_plane.transport_owner must be one of {VALID_TRANSPORT_OWNERS}, got {self.transport_owner!r}"
            )
        if not str(self.base_url).strip():
            raise ConfigError("control_plane.base_url must not be empty")
        if not str(self.api_key_env).strip():
            raise ConfigError("control_plane.api_key_env must not be empty")

    @property
    def is_active(self) -> bool:
        return self.mode == "active"

    @property
    def is_shadow(self) -> bool:
        return self.mode == "shadow"

    def base_url_for(self, station: int | str | None = None) -> str:
        station_text = "" if station is None else str(station).strip()
        if "{station}" in self.base_url and not station_text:
            raise ConfigError("control_plane.base_url needs a station to resolve {station}")
        return self.base_url.replace("{station}", station_text).strip().rstrip("/")

    def api_key(self, environ: Mapping[str, str] | None = None) -> str:
        source = environ if environ is not None else os.environ
        return str(source.get(self.api_key_env, "") or "").strip()

    def __repr__(self) -> str:
        return (
            "ControlPlaneConfig("
            f"mode={self.mode!r}, base_url={self.base_url!r}, "
            f"api_key_env={self.api_key_env!r}, "
            f"transport_owner={self.transport_owner!r})"
        )


def _defaults() -> dict[str, Any]:
    return {
        "mode": str(yaml_get("control_plane", "mode", "legacy") or "legacy"),
        "base_url": str(
            yaml_get(
                "control_plane",
                "base_url",
                "http://vpn-gluetun-{station}:8000",
            )
            or "http://vpn-gluetun-{station}:8000"
        ),
        "api_key_env": str(
            yaml_get("control_plane", "api_key_env", "GLUETUN_CONTROL_API_KEY") or "GLUETUN_CONTROL_API_KEY"
        ),
        "timeout_s": float(yaml_get("control_plane", "timeout_s", 10) or 10),
        "switch_timeout_s": float(yaml_get("control_plane", "switch_timeout_s", 60) or 60),
        "rotate_timeout_s": float(yaml_get("control_plane", "rotate_timeout_s", 180) or 180),
        "verify_timeout_s": float(yaml_get("control_plane", "verify_timeout_s", 10) or 10),
        "idempotency_window_h": int(yaml_get("control_plane", "idempotency_window_h", 24) or 24),
        "transport_owner": str(yaml_get("control_plane", "transport_owner", "python") or "python"),
        "retry": {
            "max_attempts": int(yaml_get("control_plane", "retry.max_attempts", 3) or 3),
            "backoff_s": float(yaml_get("control_plane", "retry.backoff_s", 0.5) or 0.5),
            "max_backoff_s": float(yaml_get("control_plane", "retry.max_backoff_s", 4) or 4),
        },
        "shadow": {
            "sample_rate": float(yaml_get("control_plane", "shadow.sample_rate", 1.0)),
            "compare_window_s": float(yaml_get("control_plane", "shadow.compare_window_s", 300) or 300),
        },
        "events": {
            "ingest": bool(yaml_get("control_plane", "events.ingest", True)),
            "batch_size": int(yaml_get("control_plane", "events.batch_size", 100) or 100),
            "poll_interval_s": float(yaml_get("control_plane", "events.poll_interval_s", 5) or 5),
        },
    }


def _to_config(values: Mapping[str, Any]) -> ControlPlaneConfig:
    retry = values.get("retry") if isinstance(values.get("retry"), dict) else {}
    shadow = values.get("shadow") if isinstance(values.get("shadow"), dict) else {}
    events = values.get("events") if isinstance(values.get("events"), dict) else {}
    return ControlPlaneConfig(
        mode=str(values.get("mode", "legacy")),
        base_url=str(values.get("base_url", "")),
        api_key_env=str(values.get("api_key_env", "")),
        timeout_s=float(values.get("timeout_s", 10) or 10),
        switch_timeout_s=float(values.get("switch_timeout_s", 60) or 60),
        rotate_timeout_s=float(values.get("rotate_timeout_s", 180) or 180),
        verify_timeout_s=float(values.get("verify_timeout_s", 10) or 10),
        idempotency_window_h=int(values.get("idempotency_window_h", 24) or 24),
        transport_owner=str(values.get("transport_owner", "python")),
        retry=RetryConfig(
            max_attempts=max(1, int(retry.get("max_attempts", 3) or 3)),
            backoff_s=max(0.0, float(retry.get("backoff_s", 0.5) or 0.0)),
            max_backoff_s=max(0.0, float(retry.get("max_backoff_s", 4) or 0.0)),
        ),
        shadow=ShadowConfig(
            sample_rate=min(1.0, max(0.0, float(shadow.get("sample_rate", 1.0)))),
            compare_window_s=max(0.0, float(shadow.get("compare_window_s", 300) or 0.0)),
        ),
        events=EventsConfig(
            ingest=bool(events.get("ingest", True)),
            batch_size=max(1, int(events.get("batch_size", 100) or 100)),
            poll_interval_s=max(0.1, float(events.get("poll_interval_s", 5) or 5)),
        ),
    )


def load_control_plane_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> ControlPlaneConfig:
    """Load and validate the ``control_plane`` section.

    ``yaml_get`` is used for the default file so the config audit can prove
    every leaf is consumed.  A custom ``path`` is merged over the same explicit
    defaults, mirroring ``free.ip_registry.config``.
    """

    del environ  # the key is resolved lazily through ControlPlaneConfig.api_key
    values = _defaults()
    if path is not None:
        try:
            target = Path(path)
            if target.resolve() != DEFAULT_CONFIG_PATH.resolve():
                data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
                section = data.get("control_plane") if isinstance(data, dict) else None
                if isinstance(section, dict):
                    for key, value in section.items():
                        if isinstance(value, dict) and isinstance(values.get(key), dict):
                            values[key].update(value)
                        else:
                            values[key] = value
        except (OSError, yaml.YAMLError):
            pass
    return _to_config(values)


__all__ = [
    "ControlPlaneConfig",
    "DEFAULT_CONFIG_PATH",
    "EventsConfig",
    "RetryConfig",
    "ShadowConfig",
    "VALID_MODES",
    "VALID_TRANSPORT_OWNERS",
    "load_control_plane_config",
]
