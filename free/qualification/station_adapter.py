"""Adapter that binds a real station through the existing IP-registry layer.

This is the bridge between the pure qualification sweep and the production
Gluetun/Compose path.  The binder and prober are injected; the factory builds
them from the existing ``free.ip_registry`` adapters.  Nothing here opens a
tunnel at import time.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Protocol

from .domain import AttemptOutcome, Candidate

ROOT = Path(__file__).resolve().parent.parent.parent


class BinderLike(Protocol):
    async def assign(
        self,
        station: int,
        hostname: str,
        country: str | None = None,
        *,
        vpn_type: str | None = None,
        openvpn_protocol: str | None = None,
        dns_mode: str | None = None,
    ) -> Any: ...


class ProberLike(Protocol):
    async def probe(self, station: int, model: str) -> Any: ...


def transport_parts(transport: str) -> tuple[str, str | None]:
    value = str(transport or "wireguard").strip().lower()
    if value == "openvpn-tcp":
        return "openvpn", "tcp"
    if value == "openvpn-udp":
        return "openvpn", "udp"
    return "wireguard", None


class RegistryStationAdapter:
    """Bind a candidate on one station and probe the official free gate."""

    def __init__(
        self,
        binder: BinderLike,
        prober: ProberLike,
        *,
        model: str = "deepseek-v4.1-flash-free",
        models: tuple[str, ...] = (),
        clock=None,
    ) -> None:
        self.binder = binder
        self.prober = prober
        self.model = str(model or "deepseek-v4.1-flash-free")
        self.models = tuple(str(item) for item in (models or ()) if str(item).strip())
        self._clock = clock

    async def qualify(self, candidate: Candidate, transport: str, station: int) -> AttemptOutcome:
        vpn_type, openvpn_protocol = transport_parts(transport)
        try:
            bind = await self.binder.assign(
                int(station),
                candidate.hostname,
                candidate.country or None,
                vpn_type=vpn_type,
                openvpn_protocol=openvpn_protocol,
            )
        except Exception as exc:
            return AttemptOutcome(status=0, error=f"bind:{type(exc).__name__}: {exc}")
        if not bool(getattr(bind, "ok", False)):
            return AttemptOutcome(status=0, error=f"bind:{getattr(bind, 'error', 'unknown')}")
        egress_ip = str(getattr(bind, "egress_ip", "") or "")
        last: AttemptOutcome | None = None
        for model in self.models or (self.model,):
            try:
                result = await self.prober.probe(int(station), model)
            except Exception as exc:
                last = AttemptOutcome(status=0, egress_ip=egress_ip, error=f"probe:{type(exc).__name__}: {exc}")
                continue
            outcome = AttemptOutcome(
                status=int(getattr(result, "status", 0) or 0),
                egress_ip=egress_ip,
                latency_ms=float(getattr(result, "latency_ms", 0.0) or 0.0),
                retry_after_s=float(getattr(result, "retry_after_s", 0.0) or 0.0),
                error=str(getattr(result, "error", "") or ""),
                validation=str(getattr(result, "validation", "") or ""),
                valid=bool(getattr(result, "valid", True)),
            )
            last = outcome
            if outcome.status in (200, 429):
                return outcome
        return last or AttemptOutcome(status=0, egress_ip=egress_ip, error="probe:no-result")


def first_probe_model(config: dict[str, Any]) -> str:
    try:
        models = config["ip_registry"]["allocator"]["probe_models"]
        if isinstance(models, list) and models:
            return str(models[0])
    except Exception:
        pass
    return "deepseek-v4.1-flash-free"


def _control_key(config: dict[str, Any], rotation: dict[str, Any]) -> str:
    """Gluetun control key: process env first, then config mirror, then nothing.

    The control server is enabled on every station (``HTTP_CONTROL_SERVER=on``)
    and the key lives in ``.env`` as ``VPN_CONTROL_API_KEY``.  The old
    ``GLUETUN_CONTROL_SERVER_API_KEY`` name is kept as a fallback for
    deployments that already export it.
    """

    for name in ("VPN_CONTROL_API_KEY", "GLUETUN_CONTROL_SERVER_API_KEY"):
        value = str(os.environ.get(name) or "").strip()
        if value:
            return value
    for key in ("control_api_key", "VPN_CONTROL_API_KEY"):
        value = str(rotation.get(key) or "").strip()
        if value:
            return value
    return ""


def _container_name(station: int) -> str:
    return "opencode-vpn" if int(station) == 1 else f"opencode-vpn-{int(station)}"


def build_registry_station_adapter(
    config: dict[str, Any],
    *,
    root: str | Path | None = None,
    station: int = 1,
    probe_models: tuple[str, ...] | list[str] | None = None,
    probe_timeout_s: float | None = None,
    control_settle_s: float | None = None,
    control_egress_wait_s: float | None = None,
    control_egress_poll_s: float | None = None,
    force_control: bool = False,
) -> RegistryStationAdapter:
    """Build the production adapter from the existing IP-registry classes.

    The control path is used when a Gluetun control key is available (or when
    ``force_control`` is set: the binder can still read the key from the
    container).  Otherwise the Compose path is the fallback.  This factory is
    intentionally lazy so importing the qualification package never imports
    Docker/network code.
    """

    from free.ip_registry import (
        ComposeHostnameBinder,
        GluetunControlBinder,
        HybridBinder,
        StationEgress,
        StationProbe,
    )

    root_path = Path(root) if root else ROOT
    rotation_value = config.get("ip_rotation")
    rotation: dict[str, Any] = rotation_value if isinstance(rotation_value, dict) else {}
    registry_value = config.get("ip_registry")
    registry: dict[str, Any] = registry_value if isinstance(registry_value, dict) else {}
    allocator_value = registry.get("allocator")
    allocator: dict[str, Any] = allocator_value if isinstance(allocator_value, dict) else {}
    models_value = allocator.get("probe_models")
    configured_models: list[Any] = models_value if isinstance(models_value, list) else []
    if probe_models is None:
        models = tuple(str(item) for item in configured_models)
    else:
        models = tuple(str(item) for item in probe_models if str(item).strip())
    station_egress = StationEgress(timeout_s=float(rotation.get("egress_wait_s") or 8.0))
    compose = ComposeHostnameBinder(
        root_path,
        egress=station_egress,
        egress_wait_s=float(allocator.get("egress_wait_s") or 35.0),
        egress_poll_s=float(allocator.get("egress_poll_s") or 2.0),
        settle_s=float(allocator.get("settle_s") or 8.0),
    )
    binder: Any = compose
    control_key = _control_key(config, rotation)
    if control_key or force_control:
        control = GluetunControlBinder(
            container=_container_name(station),
            egress=station_egress.control_only,
            control_key=control_key,
            settle_s=float(
                control_settle_s
                if control_settle_s is not None
                else (allocator.get("settle_s") or 2.0)
            ),
            egress_wait_s=float(
                control_egress_wait_s
                if control_egress_wait_s is not None
                else (allocator.get("egress_wait_s") or 35.0)
            ),
            egress_poll_s=float(
                control_egress_poll_s
                if control_egress_poll_s is not None
                else (allocator.get("egress_poll_s") or 2.0)
            ),
        )
        binder = HybridBinder(control=control, compose=compose)
    prober = StationProbe(
        egress=station_egress,
        timeout_s=float(
            probe_timeout_s
            if probe_timeout_s is not None
            else (allocator.get("probe_timeout_s") or 60.0)
        ),
        validate_sse=bool(allocator.get("probe_validate_sse", True)),
    )
    return RegistryStationAdapter(
        binder,
        prober,
        model=models[0] if models else first_probe_model(config),
        models=models,
    )


__all__ = [
    "BinderLike",
    "ProberLike",
    "RegistryStationAdapter",
    "build_registry_station_adapter",
    "first_probe_model",
    "transport_parts",
]
