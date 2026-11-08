"""Typed, validated view of the rotation configuration.

The legacy `yaml_get` calls remain the source; this module is the typed
boundary the application layer will consume. Each leaf is read explicitly so
`config_audit.py` can prove the key is consumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from config.settings import yaml_get

TRANSPORTS = ("wireguard", "openvpn-udp", "openvpn-tcp")


@dataclass(frozen=True)
class ScoreWeights:
    success: float = 0.40
    latency: float = 0.25
    throughput: float = 0.20
    stability: float = 0.10
    freshness: float = 0.05

    def normalized(self) -> "ScoreWeights":
        values = {
            "success": max(0.0, float(self.success)),
            "latency": max(0.0, float(self.latency)),
            "throughput": max(0.0, float(self.throughput)),
            "stability": max(0.0, float(self.stability)),
            "freshness": max(0.0, float(self.freshness)),
        }
        total = sum(values.values())
        if total <= 0:
            return ScoreWeights()
        return ScoreWeights(**{key: value / total for key, value in values.items()})


@dataclass(frozen=True)
class SelectionConfig:
    unique_ip: bool = True
    allow_countries: tuple[str, ...] = ()
    deny_countries: tuple[str, ...] = ()
    warm_pool_size: int = 9
    warm_ttl_s: float = 21600.0
    weights: ScoreWeights = field(default_factory=ScoreWeights)


@dataclass(frozen=True)
class TransportConfig:
    mode: str = "auto"
    order: tuple[str, ...] = TRANSPORTS
    cycle_timeout_s: float = 45.0
    auth_backoff_s: float = 90.0


@dataclass(frozen=True)
class RepairConfig:
    max_parallel: int = 6
    shared_wireguard_key: bool = True


@dataclass(frozen=True)
class RotationConfig:
    selection: SelectionConfig
    transport: TransportConfig
    repair: RepairConfig


def _tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        value = value.split(",")
    if not isinstance(value, (list, tuple, set)):
        return ()
    return tuple(str(item).strip() for item in value if str(item).strip())


def load_rotation_config() -> RotationConfig:
    weights = ScoreWeights(
        success=float(yaml_get("ip_registry", "selection.score_weights.success", 0.40) or 0.40),
        latency=float(yaml_get("ip_registry", "selection.score_weights.latency", 0.25) or 0.25),
        throughput=float(yaml_get("ip_registry", "selection.score_weights.throughput", 0.20) or 0.20),
        stability=float(yaml_get("ip_registry", "selection.score_weights.stability", 0.10) or 0.10),
        freshness=float(yaml_get("ip_registry", "selection.score_weights.freshness", 0.05) or 0.05),
    ).normalized()
    selection = SelectionConfig(
        unique_ip=bool(yaml_get("ip_registry", "selection.unique_ip", True)),
        allow_countries=_tuple(yaml_get("ip_registry", "selection.allow_countries", [])),
        deny_countries=_tuple(yaml_get("ip_registry", "selection.deny_countries", [])),
        warm_pool_size=max(1, int(yaml_get("ip_registry", "selection.warm_pool_size", 9) or 9)),
        warm_ttl_s=max(60.0, float(yaml_get("ip_registry", "selection.warm_ttl_s", 21600) or 21600)),
        weights=weights,
    )
    order = _tuple(yaml_get("ip_rotation", "transport_order", list(TRANSPORTS)))
    order = tuple(item for item in order if item in TRANSPORTS) or TRANSPORTS
    mode = str(yaml_get("ip_rotation", "vpn_stack", "auto") or "auto").strip().lower()
    if mode not in ("auto",) + TRANSPORTS:
        mode = "auto"
    transport = TransportConfig(
        mode=mode,
        order=tuple(dict.fromkeys(order)),
        cycle_timeout_s=max(5.0, float(yaml_get("ip_rotation", "transport_cycle_timeout_s", 45) or 45)),
        auth_backoff_s=max(1.0, float(yaml_get("ip_rotation", "transport_auth_backoff_s", 90) or 90)),
    )
    repair = RepairConfig(
        max_parallel=max(1, int(yaml_get("ip_registry", "allocator.max_parallel_repairs", 6) or 6)),
        shared_wireguard_key=bool(yaml_get("ip_registry", "repair.shared_wireguard_key", True)),
    )
    return RotationConfig(selection=selection, transport=transport, repair=repair)
