"""Typed access to the ``ip_registry`` config section.

Every leaf is read explicitly through :func:`config.settings.yaml_get` so the
config audit can prove it is consumed.  A typo or a new key without a reader
must fail the audit instead of silently falling back to a default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from config.settings import yaml_get

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config.yaml"


def _model_list(value: Any) -> list[str]:
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple, set)):
        parts = list(value)
    else:
        parts = []
    return [str(part).strip() for part in parts if str(part).strip()]


def _defaults() -> dict[str, Any]:
    return {
        "enabled": bool(yaml_get("ip_registry", "enabled", False)),
        "db_path": str(yaml_get("ip_registry", "db_path", "logs/ip_registry.db")),
        "registry_db_path": str(
            yaml_get("ip_registry", "registry_db_path", "logs/registry.db")
        ),
        "catalog_refresh_s": float(
            yaml_get("ip_registry", "catalog_refresh_s", 21600) or 21600
        ),
        "test_timeout_s": float(yaml_get("ip_registry", "test_timeout_s", 60) or 60),
        "lease_ttl_s": float(yaml_get("ip_registry", "lease_ttl_s", 300) or 300),
        "retention_days": float(
            yaml_get("ip_registry", "retention_days", 7) or 7
        ),
        "score": {
            "availability_weight": float(
                yaml_get("ip_registry", "score.availability_weight", 0.55)
            ),
            "latency_weight": float(
                yaml_get("ip_registry", "score.latency_weight", 0.25)
            ),
            "freshness_weight": float(
                yaml_get("ip_registry", "score.freshness_weight", 0.20)
            ),
            "latency_budget_ms": float(
                yaml_get("ip_registry", "score.latency_budget_ms", 8000.0)
            ),
            "freshness_ttl_h": float(
                yaml_get("ip_registry", "score.freshness_ttl_h", 48.0)
            ),
            "min_score": float(yaml_get("ip_registry", "score.min_score", 20.0)),
        },
        "eligibility": {
            "error_cooldown_s": float(
                yaml_get("ip_registry", "eligibility.error_cooldown_s", 1800.0)
            ),
        },
        "allocator": {
            "mode": str(yaml_get("ip_registry", "allocator.mode", "off") or "off"),
            "max_attempts_per_need": int(
                yaml_get("ip_registry", "allocator.max_attempts_per_need", 20) or 20
            ),
            "max_parallel_repairs": int(
                yaml_get("ip_registry", "allocator.max_parallel_repairs", 6) or 6
            ),
            "rebind_limit_per_min": int(
                yaml_get("ip_registry", "allocator.rebind_limit_per_min", 4) or 4
            ),
            "winner_cooldown_s": float(
                yaml_get("ip_registry", "allocator.winner_cooldown_s", 120) or 120
            ),
            "winner_grace_s": float(
                yaml_get("ip_registry", "allocator.winner_grace_s", 20) or 20
            ),
            "failure_cooldown_s": float(
                yaml_get("ip_registry", "allocator.failure_cooldown_s", 300) or 300
            ),
            "max_candidates_per_chain": int(
                yaml_get("ip_registry", "allocator.max_candidates_per_chain", 20) or 20
            ),
            "probe_models": _model_list(
                yaml_get(
                    "ip_registry",
                    "allocator.probe_models",
                    [
                        "deepseek-v4.1-flash-free",
                        "glm-5.3-flash-free",
                        "mimo-v2.6-flash-free",
                    ],
                )
            ),
            "transports": list(
                yaml_get("ip_registry", "allocator.transports", []) or []
            ),
            "refresh_when_exhausted": bool(
                yaml_get(
                    "ip_registry", "allocator.refresh_when_exhausted", True
                )
            ),
            "refresh_cooldown_s": float(
                yaml_get(
                    "ip_registry", "allocator.refresh_cooldown_s", 900
                )
                or 900
            ),
            "refresh_min_available": int(
                yaml_get(
                    "ip_registry", "allocator.refresh_min_available", 3
                )
                or 3
            ),
            "global_429_window_s": float(
                yaml_get("ip_registry", "allocator.global_429_window_s", 600) or 600
            ),
            "global_429_threshold": int(
                yaml_get("ip_registry", "allocator.global_429_threshold", 20) or 20
            ),
            "global_429_pause_s": float(
                yaml_get("ip_registry", "allocator.global_429_pause_s", 900) or 900
            ),
            "exhausted_threshold": int(
                yaml_get("ip_registry", "allocator.exhausted_threshold", 40) or 40
            ),
            "exhausted_pause_s": float(
                yaml_get("ip_registry", "allocator.exhausted_pause_s", 1800) or 1800
            ),
            "poll_interval_s": float(
                yaml_get("ip_registry", "allocator.poll_interval_s", 20) or 20
            ),
        },
        "binder": {
            "mode": str(
                yaml_get("ip_registry", "binder.mode", "compose_hostname")
                or "compose_hostname"
            ),
        },
        "sources": {
            "manifest": str(
                yaml_get(
                    "ip_registry",
                    "sources.manifest",
                    "docs/nordvpn_sources.yaml",
                )
                or "docs/nordvpn_sources.yaml"
            ),
            "embedded_path": str(
                yaml_get(
                    "ip_registry",
                    "sources.embedded_path",
                    "logs/gluetun_nordvpn_embedded.json",
                )
                or "logs/gluetun_nordvpn_embedded.json"
            ),
            "cache_path": str(
                yaml_get(
                    "ip_registry",
                    "sources.cache_path",
                    "logs/nordvpn_api.json",
                )
                or "logs/nordvpn_api.json"
            ),
            "gluetun_manifest_path": str(
                yaml_get(
                    "ip_registry",
                    "sources.gluetun_manifest_path",
                    "logs/gluetun_manifest.json",
                )
                or "logs/gluetun_manifest.json"
            ),
        },
    }


def load_registry_config(path: str | Path | None = None) -> dict[str, Any]:
    """Return the ``ip_registry`` section with every documented leaf.

    ``path`` is kept for the scripts' ``--config`` option.  A custom file is
    merged over the explicit defaults; the default file goes through
    ``yaml_get`` so the audit sees each consumed key.
    """

    config = _defaults()
    if path is None:
        return config
    try:
        target = Path(path)
        if target.resolve() == DEFAULT_CONFIG_PATH.resolve():
            return config
        data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    except Exception:
        return config
    section = data.get("ip_registry") if isinstance(data, dict) else None
    if not isinstance(section, dict):
        return config
    for key, value in section.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


__all__ = ["DEFAULT_CONFIG_PATH", "load_registry_config"]
