"""Composition root for the qualification pipeline.

Reads ``egress_catalog.qualification`` once and builds the store, source and
sweep.  It never imports Docker or the manager: the station adapter is injected
by the caller.  Disabled config returns ``None``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .cutover import CutoverPorts, CutoverService
from .discovery import DiscoveryConfig, DiscoveryService
from .preflight import PreflightProberLike, PreflightService
from .scoring import PotentialWeights
from .source import DEFAULT_API_CACHE, NordVpnCatalogSource
from .store import QualificationStore
from .sweep import QualificationSweep, StationAdapter, SweepConfig
from .watchdog import WatchdogConfig, WatchdogService


def _section(config: dict[str, Any], name: str) -> dict[str, Any]:
    value = config.get(name) if isinstance(config, dict) else None
    return value if isinstance(value, dict) else {}


def _qualification_cfg(config: dict[str, Any]) -> dict[str, Any]:
    catalog = _section(config, "egress_catalog")
    value = catalog.get("qualification") if isinstance(catalog, dict) else None
    return value if isinstance(value, dict) else {}


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _weights(cfg: dict[str, Any]) -> PotentialWeights:
    raw_value = cfg.get("weights")
    raw: dict[str, Any] = raw_value if isinstance(raw_value, dict) else {}
    return PotentialWeights(
        reliability=_float(raw.get("reliability"), 0.30),
        latency=_float(raw.get("latency"), 0.30),
        quota=_float(raw.get("quota"), 0.20),
        load=_float(raw.get("load"), 0.15),
        freshness=_float(raw.get("freshness"), 0.05),
        latency_budget_ms=_float(cfg.get("latency_budget_ms"), 250.0),
        quota_estimate=_int(cfg.get("quota_estimate"), 500),
        freshness_ttl_h=_float(cfg.get("freshness_ttl_h"), 24.0),
    )


def _transport_order(cfg: dict[str, Any]) -> tuple[str, ...]:
    raw = cfg.get("transport_order")
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",")]
    if not isinstance(raw, (list, tuple)):
        return ("wireguard", "openvpn-udp", "openvpn-tcp")
    allowed = {"wireguard", "openvpn-udp", "openvpn-tcp"}
    order = tuple(str(item).strip().lower() for item in raw if str(item).strip().lower() in allowed)
    return order or ("wireguard", "openvpn-udp", "openvpn-tcp")


@dataclass
class QualificationRuntime:
    store: QualificationStore
    source: NordVpnCatalogSource
    sweep: QualificationSweep
    discovery: DiscoveryService
    watchdog: WatchdogService
    config: dict[str, Any]
    preflight: PreflightService | None = None

    def ingest(self, *, now: float | None = None) -> int:
        return self.source.ingest(self.store, now=now)

    def summary(self) -> dict[str, Any]:
        return self.store.summary()

    def close(self) -> None:
        self.store.close()


def build_qualification(
    config: dict[str, Any],
    *,
    db_path: str | Path | None = None,
    adapter: StationAdapter | None = None,
    watchdog_probe=None,
    watchdog_repair=None,
    preflight_prober: PreflightProberLike | None = None,
    clock=None,
) -> QualificationRuntime | None:
    cfg = _qualification_cfg(config)
    if not bool(cfg.get("enabled", False)):
        return None
    registry = _section(config, "ip_registry")
    target = Path(
        db_path
        or cfg.get("db_path")
        or registry.get("registry_db_path")
        or (Path(__file__).resolve().parent.parent.parent / "logs" / "registry.db")
    )
    store = QualificationStore(
        target,
        retention_days=_float(cfg.get("retention_days"), 7.0),
        **({"clock": clock} if clock is not None else {}),
    )
    denylist_value = _section(config, "egress_catalog").get("denylist")
    source = NordVpnCatalogSource(
        cfg.get("api_cache") or DEFAULT_API_CACHE,
        **({"clock": clock} if clock is not None else {}),
        weights=_weights(cfg),
        denylist=(
            denylist_value if isinstance(denylist_value, (list, tuple)) else None
        ),
    )
    sweep_cfg = SweepConfig(
        station=_int(cfg.get("station"), 1),
        max_hosts=_int(cfg.get("max_hosts"), 0),
        max_attempts_per_host=_int(cfg.get("max_attempts_per_host_per_epoch"), 3),
        infra_backoff_s=_float(cfg.get("infra_backoff_s"), 300.0),
        infra_backoff_max_s=_float(cfg.get("infra_backoff_max_s"), 3600.0),
        lease_s=_float(cfg.get("lease_s"), 300.0),
        strategy=str(cfg.get("transport_strategy") or "auto"),
        manual_transport=str(cfg.get("transport") or ""),
        transport_order=_transport_order(cfg),
        transport_rounds=_int(cfg.get("transport_retry_rounds"), 2),
        transport_retry_backoff_s=_float(cfg.get("transport_retry_backoff_s"), 60.0),
        transport_health_min_attempts=_int(cfg.get("transport_health_min_attempts"), 100),
        auth_backoff_s=_float(cfg.get("auth_backoff_s"), 3600.0),
        quota_retry_jitter_s=_float(cfg.get("quota_retry_jitter_s"), 60.0),
        shadow=bool(cfg.get("shadow_mode", True)),
        run_id=str(cfg.get("run_id") or ""),
    )
    sweep = QualificationSweep(
        store,
        adapter,
        config=sweep_cfg,
        **({"clock": clock} if clock is not None else {}),
    )
    discovery = DiscoveryService(
        store,
        source,
        config=DiscoveryConfig(
            interval_s=_float(cfg.get("discovery_interval_s"), 21600.0),
            stale_max_s=_float(cfg.get("discovery_stale_max_s"), 86400.0),
            source_id=source.source_id,
        ),
        **({"clock": clock} if clock is not None else {}),
    )
    raw_stations = cfg.get("watchdog_stations")
    stations = tuple(
        int(item)
        for item in (raw_stations if isinstance(raw_stations, list) else [1, 2, 3, 4, 5, 6])
        if str(item).strip()
    )
    watchdog = WatchdogService(
        store,
        watchdog_probe if bool(cfg.get("watchdog_enabled", True)) else None,
        watchdog_repair if bool(cfg.get("watchdog_enabled", True)) else None,
        config=WatchdogConfig(
            stations=stations or (1, 2, 3, 4, 5, 6),
            max_repair_attempts=_int(cfg.get("max_repair_attempts"), 3),
            repair_backoff_s=_float(cfg.get("repair_backoff_s"), 0.0),
            probe_interval_s=_float(cfg.get("watchdog_probe_interval_s"), 30.0),
        ),
        **({"clock": clock} if clock is not None else {}),
    )
    preflight = None
    if preflight_prober is not None:
        preflight = PreflightService(
            store,
            preflight_prober,
            weights=_weights(cfg),
            backoff_s=_float(cfg.get("infra_backoff_s"), 300.0),
            timeout_s=_float(cfg.get("preflight_timeout_s"), 5.0),
            **({"clock": clock} if clock is not None else {}),
        )
    return QualificationRuntime(
        store=store,
        source=source,
        sweep=sweep,
        discovery=discovery,
        watchdog=watchdog,
        config=cfg,
        preflight=preflight,
    )


def build_cutover_service(
    store: QualificationStore,
    ports: CutoverPorts,
    config: dict[str, Any],
    *,
    clock=None,
) -> CutoverService:
    """Build a cutover service bounded by ``cutover_timeout_ms``."""

    cfg = _qualification_cfg(config)
    timeout_s = max(
        0.1,
        _float(cfg.get("cutover_timeout_ms"), 5000.0) / 1000.0,
    )
    return CutoverService(
        store,
        ports,
        timeout_s=timeout_s,
        **({"clock": clock} if clock is not None else {}),
    )


def build_qualification_scheduler(
    runtime: QualificationRuntime,
    *,
    interval_s: float = 900.0,
    max_hosts: int | None = None,
    shadow: bool | None = None,
    run_preflight: bool | None = None,
    run_watchdog: bool | None = None,
    per_station_workers: bool | None = None,
    service_priority: bool = True,
    clock=None,
):
    """Assemble the periodic scheduler around an existing runtime."""

    from .scheduler import QualificationScheduler, QualificationSchedulerConfig

    cfg = runtime.config
    sweeps = None
    use_workers = (
        bool(cfg.get("per_station_workers", False))
        if per_station_workers is None
        else bool(per_station_workers)
    )
    if use_workers:
        raw_stations = cfg.get("watchdog_stations")
        stations = tuple(
            int(item)
            for item in (
                raw_stations
                if isinstance(raw_stations, (list, tuple))
                else (1, 2, 3, 4, 5, 6)
            )
            if str(item).strip()
        ) or (1,)
        base = runtime.sweep
        base_clock = getattr(base, "_clock", None) or time.time
        sweeps = [
            QualificationSweep(
                runtime.store,
                base.adapter,
                config=replace(base.config, station=int(station)),
                clock=clock or base_clock,
            )
            for station in stations
        ]
    return QualificationScheduler(
        runtime.sweep,
        sweeps=sweeps,
        discovery=runtime.discovery,
        watchdog=runtime.watchdog,
        preflight=runtime.preflight,
        config=QualificationSchedulerConfig(
            interval_s=max(60.0, float(interval_s)),
            max_hosts=(
                int(cfg.get("max_hosts") or 0)
                if max_hosts is None
                else max(0, int(max_hosts))
            ),
            shadow=(
                bool(cfg.get("shadow_mode", True)) if shadow is None else bool(shadow)
            ),
            run_preflight=(
                bool(runtime.preflight is not None)
                if run_preflight is None
                else bool(run_preflight)
            ),
            run_watchdog=(
                bool(getattr(runtime.watchdog, "probe", None) is not None)
                if run_watchdog is None
                else bool(run_watchdog)
            ),
            watchdog_interval_s=float(
                cfg.get("watchdog_probe_interval_s") or 30.0
            ),
            global_pause_on_429=bool(
                cfg.get("global_pause_on_429", False)
            ),
            service_priority=bool(service_priority),
        ),
        station_state_getter=lambda station: (
            runtime.store.station_runtime(int(station)) or {}
        ).get("state", ""),
        **({"clock": clock} if clock is not None else {}),
    )


__all__ = [
    "QualificationRuntime",
    "build_cutover_service",
    "build_qualification",
    "build_qualification_scheduler",
]
