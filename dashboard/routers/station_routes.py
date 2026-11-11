"""Station inventory and reconcile-plan routes.

Extracted from ``dashboard/api.py`` (slice S10).  The routes are read-only:
they report the consolidated station view and the desired-vs-actual reconcile
plan, and never start, stop, or rotate a container.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, Request


def build_stations_payload(
    *,
    pool_getter: Callable[[], Any],
    managers_getter: Callable[[], list[Any]],
    config_getter: Callable[[], dict[str, Any]],
    capacity_counts_getter: Callable[[], dict[str, dict[str, int]]],
) -> dict[str, Any]:
    from free.capacity import capacity_config
    from free.egress_reconcile import bucket_for_endpoint
    from free.quota_ledger import quota_block

    cfg = config_getter() or {}
    target, policy = capacity_config(cfg.get("egress_capacity"))
    pool = pool_getter()
    health: dict = {}
    if pool is not None and hasattr(pool, "_station_health"):
        try:
            health = pool._station_health.snapshot()
        except Exception:
            health = {}

    def _health_for(sid: int) -> dict:
        return health.get(sid) or health.get(str(sid)) or {}

    rows: list[dict] = []
    for manager in managers_getter():
        try:
            status = manager.get_status()
        except Exception:
            status = {}
        sid = int(getattr(manager, "_station", 0) or 0)
        station_health = _health_for(sid)
        rows.append(
            {
                "provider": "nordvpn",
                "station": sid,
                "container": status.get("container")
                or getattr(manager, "_docker_container", ""),
                "generation": int(status.get("generation", 0) or 0),
                "state": station_health.get("state") or status.get("status") or "unknown",
                "client_usable": bool(station_health.get("usable", False)),
                "egress_ip": status.get("ip") or status.get("current_ip"),
                "block": quota_block(status.get("ip") or status.get("current_ip")),
                "quota_state": "unknown",
                "reset_at": None,
                "last_evidence": None,
                "reason": station_health.get("last_error") or status.get("error"),
                "next_action": "none" if station_health.get("usable") else "repair",
                "provider_details": {
                    "stack": status.get("assigned_stack") or status.get("stack"),
                    "transport": status.get("ovpn_protocol_effective"),
                    "hostname": status.get("current_host"),
                },
            }
        )
    if pool is not None and hasattr(pool, "_socks5_enabled_eps"):
        try:
            endpoints = pool._socks5_enabled_eps()
        except Exception:
            endpoints = []
        for endpoint in endpoints:
            sid = int(getattr(endpoint, "_station", 0) or 0)
            station_health = _health_for(sid)
            egress = getattr(endpoint, "egress_ip", None)
            try:
                endpoint_usable = bool(pool._socks5_client_usable(endpoint))
            except Exception:
                endpoint_usable = bool(station_health.get("usable", False) and egress)
            rows.append(
                {
                    "provider": "warp",
                    "station": sid,
                    "container": getattr(endpoint, "container", None),
                    "generation": 0,
                    "state": "client_usable"
                    if endpoint_usable
                    else (station_health.get("state") or "degraded"),
                    "client_usable": endpoint_usable,
                    "egress_ip": egress,
                    "block": bucket_for_endpoint(endpoint),
                    "quota_state": "unknown",
                    "reset_at": getattr(endpoint, "reset_at", None),
                    "last_evidence": None,
                    "reason": station_health.get("last_error"),
                    "next_action": "none" if endpoint_usable else "repair",
                    "provider_details": {
                        "label": getattr(endpoint, "label", ""),
                        "ipv6": bool(getattr(endpoint, "ipv6", False)),
                        "managed": bool(getattr(endpoint, "managed", False)),
                    },
                }
            )
    duplicate_groups = (
        pool.duplicate_egress_groups()
        if pool is not None and hasattr(pool, "duplicate_egress_groups")
        else {}
    )
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "target_slots": target,
        "reserve_policy": policy,
        "providers": capacity_counts_getter(),
        "stations": rows,
        "duplicate_buckets": duplicate_groups,
        "warnings": [f"duplicate_block:{bucket}" for bucket in duplicate_groups],
    }


def build_reconcile_plan(
    *,
    pool_getter: Callable[[], Any],
    managers_getter: Callable[[], list[Any]],
) -> dict[str, Any]:
    from ops.providers.nordvpn import NordVpnProvider
    from ops.providers.warp import WarpProvider
    from ops.reconciler import ActualSlot, reconcile

    pool = pool_getter()
    managers = managers_getter()
    desired = list(NordVpnProvider(managers).specs())
    actual: list[ActualSlot] = []
    for manager in managers:
        try:
            status = manager.get_status()
        except Exception:
            status = {}
        sid = int(getattr(manager, "_station", 0) or 0)
        stack = str(status.get("assigned_stack") or status.get("stack") or "")
        proto = str(status.get("ovpn_protocol_effective") or "")
        transport = (
            "wireguard"
            if stack == "wireguard"
            else (f"openvpn-{proto}" if proto in ("tcp", "udp") else proto)
        )
        actual.append(
            ActualSlot(
                provider="nordvpn",
                slot_id=sid,
                container=str(status.get("container") or ""),
                generation=0,
                stack=stack,
                transport=transport,
                running=bool(status.get("ip") or status.get("current_ip")),
                health="healthy" if status.get("error") is None else "unhealthy",
                port=int(status.get("proxy_port") or 0),
            )
        )
    endpoints: list = []
    if pool is not None and hasattr(pool, "_socks5_enabled_eps"):
        try:
            endpoints = list(pool._socks5_enabled_eps())
        except Exception:
            endpoints = []
    desired.extend(WarpProvider(endpoints).specs())
    for endpoint in endpoints:
        actual.append(
            ActualSlot(
                provider="warp",
                slot_id=0,
                container=str(getattr(endpoint, "container", "") or ""),
                generation=0,
                stack="warp",
                transport="socks5",
                running=bool(getattr(endpoint, "egress_ip", None)),
                health="healthy" if getattr(endpoint, "egress_ip", None) else "unhealthy",
                port=int(getattr(endpoint, "port", 0) or 0),
                block=str(getattr(endpoint, "block_key", "") or "") or None,
            )
        )
    decisions = reconcile(desired, actual)
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "read_only": True,
        "decisions": [decision.as_dict() for decision in decisions],
    }


def register_station_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    pool_getter: Callable[[], Any],
    managers_getter: Callable[[], list[Any]],
    config_getter: Callable[[], dict[str, Any]],
    capacity_counts_getter: Callable[[], dict[str, dict[str, int]]],
) -> None:
    def _payload() -> dict[str, Any]:
        return build_stations_payload(
            pool_getter=pool_getter,
            managers_getter=managers_getter,
            config_getter=config_getter,
            capacity_counts_getter=capacity_counts_getter,
        )

    @app.get("/api/stations")
    async def get_stations(request: Request):
        err = token_checker(request)
        if err:
            return err
        return _payload()

    @app.get("/api/stations/reconcile-plan")
    async def get_stations_reconcile_plan(request: Request):
        err = token_checker(request)
        if err:
            return err
        return build_reconcile_plan(
            pool_getter=pool_getter,
            managers_getter=managers_getter,
        )

    @app.get("/api/stations/{provider}/{station}")
    async def get_station(provider: str, station: int, request: Request):
        err = token_checker(request)
        if err:
            return err
        payload = _payload()
        for row in payload.get("stations", []):
            if row.get("provider") == provider and int(row.get("station", 0)) == int(station):
                return row
        return {"error": "station inconnue", "provider": provider, "station": station}


__all__ = [
    "build_reconcile_plan",
    "build_stations_payload",
    "register_station_routes",
]
