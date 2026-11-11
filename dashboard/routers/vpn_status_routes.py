"""Fleet VPN status projection extracted from ``dashboard/api.py``."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI


def register_vpn_status_routes(
    app: FastAPI,
    *,
    settings: Any,
    stats_cache: Any,
    ip_stats_db_getter: Callable[[], Any],
    proxy_mode_getter: Callable[[], str],
    socks5_payload: Callable[[Any], list[dict[str, Any]]],
    socks5_auto_rotate_state: Callable[[], bool],
    config_yaml_mtime: Callable[[], float],
    config_yaml_known_mtime_getter: Callable[[], float],
) -> None:
    @app.get("/api/vpn-status")
    async def get_vpn_status():
        import shared_state

        managers = getattr(shared_state, "vpn_managers", None) or []
        refreshed_at = datetime.now(UTC).isoformat()
        refresh_error = None
        stale = False
        status_age_s = None
        if managers:
            try:
                now_mono = time.monotonic()
                ages = [
                    None
                    if getattr(manager, "_last_status_refresh_at", None) is None
                    else now_mono
                    - float(getattr(manager, "_last_status_refresh_at", 0.0))
                    for manager in managers
                ]
                cache_s = max(
                    0.5,
                    min(
                        float(
                            getattr(manager, "_STATUS_CACHE_SECONDS", 5.0)
                        )
                        for manager in managers
                    ),
                )
                known_ages = [age for age in ages if age is not None]
                status_age_s = max(known_ages) if known_ages else None
                fresh_bound = max(120.0, cache_s * 6.0)
                stale = any(
                    age is None or age >= fresh_bound for age in ages
                )
                needs_refresh = any(
                    age is None or age >= cache_s for age in ages
                )
                if needs_refresh:
                    inflight = getattr(
                        shared_state,
                        "_refresh_inflight",
                        None,
                    )
                    if not inflight or getattr(
                        inflight,
                        "done",
                        lambda: True,
                    )():
                        shared_state._refresh_inflight = (
                            asyncio.ensure_future(
                                asyncio.gather(
                                    *(
                                        manager.refresh_status(force=False)
                                        for manager in managers
                                    ),
                                    return_exceptions=True,
                                )
                            )
                        )
                        shared_state._refresh_inflight.add_done_callback(
                            lambda _task: setattr(
                                shared_state,
                                "_refresh_inflight",
                                None,
                            )
                        )
            except Exception as error:
                refresh_error = f"{type(error).__name__}: {error}"
            refreshed_at = datetime.now(UTC).isoformat()
            if shared_state.free_ip_pool:
                data = shared_state.free_ip_pool.get_status()
            else:
                try:
                    all_statuses = [
                        manager.get_status() for manager in managers
                    ]
                    connected = sum(
                        1
                        for status in all_statuses
                        if status.get("status") in ("connected", "degraded")
                    )
                    total = len(all_statuses)
                    if any(
                        status.get("status") == "connected"
                        for status in all_statuses
                    ):
                        aggregate = "connected"
                    elif any(
                        status.get("status") == "degraded"
                        for status in all_statuses
                    ):
                        aggregate = "degraded"
                    elif all(
                        status.get("status") == "error"
                        for status in all_statuses
                    ):
                        aggregate = "error"
                    else:
                        aggregate = all_statuses[0].get(
                            "status",
                            "not_configured",
                        )
                    data = all_statuses[0].copy()
                    data["status"] = aggregate
                    data["vpn_status"] = aggregate
                    data["healthy"] = connected
                    data["total"] = total
                    data["stations"] = [
                        {
                            "station": status.get("station"),
                            "vpn_status": status.get("status"),
                            "current_ip": status.get("ip"),
                            "current_server": status.get("server"),
                            "vpn": status,
                        }
                        for status in all_statuses
                    ]
                except Exception:
                    try:
                        data = managers[0].get_status()
                    except Exception:
                        data = {
                            "enabled": False,
                            "status": "not_configured",
                        }
        else:
            data = {"enabled": False, "status": "not_configured"}

        if isinstance(data, dict):
            cached_ip = stats_cache.get("ip_stats")
            data["ip_stats"] = cached_ip if cached_ip is not None else {}
            try:
                ip_stats_db = ip_stats_db_getter()
                running = getattr(ip_stats_db, "_running_task", None)
                if running is None or running.done():
                    ip_stats_db._running_task = asyncio.create_task(
                        ip_stats_db(shared_state.vpn_manager)
                    )
            except Exception:
                pass
            rotation = getattr(shared_state, "shared_rotation", None)
            if rotation is not None:
                data["shared_rotation"] = rotation.get_status()
            watcher = getattr(shared_state, "docker_event_watcher", None)
            if watcher is not None:
                data["watch_events"] = watcher.get_status()
            if settings.ENV_DIVERGENCE:
                data["env_divergence"] = [
                    {"key": key, "file": path, "env": env}
                    for key, path, env in settings.ENV_DIVERGENCE
                ]
            try:
                expected_n = sum(
                    1
                    for manager in managers
                    if getattr(manager, "enabled", True)
                    and not getattr(manager, "_disabled", False)
                )
                up_n = sum(
                    1
                    for manager in managers
                    if str(
                        getattr(manager, "status", "disconnected")
                        or "disconnected"
                    )
                    in ("connected", "degraded")
                )
                missing = [
                    manager
                    for manager in managers
                    if getattr(manager, "enabled", True)
                    and not getattr(manager, "_disabled", False)
                    and str(
                        getattr(manager, "status", "disconnected")
                        or "disconnected"
                    )
                    not in ("connected", "degraded")
                ]
                only_transient = bool(missing) and all(
                    str(
                        getattr(manager, "status", "disconnected")
                        or "disconnected"
                    )
                    == "connecting"
                    and getattr(manager, "_rotation_task", None) is not None
                    and not getattr(
                        manager._rotation_task,
                        "done",
                        lambda: True,
                    )()
                    for manager in missing
                )
                enabled = bool(
                    (getattr(settings, "IP_ROTATION", {}) or {}).get(
                        "enabled",
                        False,
                    )
                )
                if enabled and up_n < expected_n and not only_transient:
                    shared_state.boot_error = (
                        f"boot {up_n}/{expected_n}: connected {up_n} — "
                        "reconcile/pool"
                    )
                    data["boot_error"] = shared_state.boot_error
                else:
                    shared_state.boot_error = None
                    data["boot_error"] = None
            except Exception:
                if getattr(shared_state, "boot_error", None):
                    data["boot_error"] = shared_state.boot_error
            try:
                from vpn.manager import docker_daemon_status

                data["docker_daemon"] = docker_daemon_status()
            except Exception:
                pass
            data["config_yaml_dirty"] = (
                config_yaml_mtime() != config_yaml_known_mtime_getter()
            )
            data["proxy_mode"] = proxy_mode_getter()
            try:
                from free.auto_egress import providers_from_mode

                selected = (
                    getattr(settings, "IP_ROTATION", None) or {}
                ).get("egress_providers")
                if not isinstance(selected, list) or not selected:
                    selected = providers_from_mode(str(data["proxy_mode"]))
                data["egress_providers"] = list(selected)
                rotation_config = getattr(settings, "IP_ROTATION", None) or {}
                data["egress_strategy"] = str(
                    rotation_config.get("egress_strategy") or "fallback"
                )
                weights = rotation_config.get("egress_weights")
                data["egress_weights"] = (
                    dict(weights) if isinstance(weights, dict) else {}
                )
            except Exception:
                data["egress_providers"] = []
            try:
                from latency_rotation import get_engine

                engine = (
                    getattr(shared_state, "latency_engine", None)
                    or get_engine()
                )
                latency = {}
                for (station_id, ip), tracker in list(
                    engine._trackers.items()
                )[:80]:
                    latency[f"{station_id}|{ip}"] = (
                        tracker.snapshot().__dict__
                    )
                data["latency"] = latency
                data["rotation_paused"] = bool(
                    getattr(engine, "paused", False)
                )
                data["latency_enabled"] = bool(
                    getattr(engine.cfg, "enabled", True)
                )
                data["global_degraded_remaining"] = round(
                    max(
                        0.0,
                        float(engine._global_paused_until)
                        - engine._now(),
                    ),
                    1,
                )
            except Exception:
                pass
            try:
                from free.quality import get_quality_registry

                data["quality"] = get_quality_registry().status()
            except Exception:
                pass
            data["socks5"] = {
                "proxies": socks5_payload(shared_state.free_ip_pool),
                "rotate": socks5_auto_rotate_state(),
            }
            try:
                import opencode as opencode_module

                durations = getattr(
                    opencode_module,
                    "_geo_pin_duration",
                    [],
                )
                average = sum(durations) / len(durations) if durations else 0
                has_opencode = True
            except Exception:
                durations, average = [], 0
                has_opencode = False
            data["geo"] = {
                "enabled": bool(
                    getattr(settings, "GEO_ENABLED", False)
                ),
                "version": int(
                    getattr(settings, "GEO_VERSION", 1) or 1
                ),
                "block_total": int(
                    getattr(opencode_module, "_geo_block_total", 0)
                )
                if has_opencode
                else 0,
                "pin_latency_avg_ms": round(average, 1),
                "pin_latency_p95_ms": round(
                    sorted(durations)[int(len(durations) * 0.95)]
                    if durations
                    else 0,
                    1,
                ),
                "queue_depth": len(
                    getattr(opencode_module, "_geo_breaker", {})
                )
                if has_opencode
                else 0,
                "allow_direct_when_compatible": bool(
                    getattr(
                        settings,
                        "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE",
                        True,
                    )
                ),
                "geo_allow_direct": bool(
                    getattr(
                        settings,
                        "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE",
                        True,
                    )
                ),
            }
            try:
                strict_union: set[str] = set()
                for route in settings.ROUTES.values():
                    try:
                        geo = settings.resolve_geo(route)
                        effective = geo.get("effective_allowed", set())
                        if isinstance(effective, set):
                            strict_union |= effective
                    except Exception:
                        pass
                data["geo_strict_union"] = sorted(strict_union)
            except Exception:
                data["geo_strict_union"] = []
            try:
                current_country = None
                manager_countries: list[str] = []
                for manager in managers:
                    if manager and getattr(
                        manager,
                        "_current_country",
                        None,
                    ):
                        manager_countries.append(manager._current_country)
                any_effective: set[str] = set()
                for route in settings.ROUTES.values():
                    try:
                        geo = settings.resolve_geo(route)
                        effective = geo.get("effective_allowed", set())
                        if isinstance(effective, set):
                            any_effective |= effective
                    except Exception:
                        pass
                for country in manager_countries:
                    if country in any_effective:
                        current_country = country
                        break
                if current_country is None and manager_countries:
                    current_country = manager_countries[0]
                data["current_country"] = current_country
                allowed_for = []
                for key, route in settings.ROUTES.items():
                    try:
                        geo = settings.resolve_geo(route)
                        effective = geo.get("effective_allowed", set())
                        if (
                            isinstance(effective, set)
                            and current_country
                            and current_country in effective
                        ):
                            allowed_for.append(key)
                    except Exception:
                        pass
                data["current_country_allowed_for"] = allowed_for
            except Exception:
                pass
            countries = {}
            for manager in managers:
                if manager is None:
                    continue
                status = manager.get_status()
                countries[manager._station] = {
                    "current_country": status.get("current_country"),
                    "next_country": status.get("next_country"),
                    "country_pinned_at": status.get("country_pinned_at"),
                    "country_rotation": status.get("country_rotation"),
                }
            if countries:
                data["countries"] = countries
            data["stale"] = stale
            data["refreshed_at"] = refreshed_at
            data["refresh_error"] = refresh_error
            data["status_age_s"] = status_age_s
            try:
                free_parallel = (
                    settings.FREE_PARALLEL
                    if hasattr(settings, "FREE_PARALLEL")
                    else {}
                )
                enabled_parallel = (
                    bool(free_parallel.get("enabled"))
                    if isinstance(free_parallel, dict)
                    else False
                )
                count = int(data.get("total") or len(managers) or 0)
                data["free_parallel_warning"] = (
                    "tout sur station 1 — activer free_parallel"
                    if count >= 2 and not enabled_parallel
                    else None
                )
            except Exception:
                pass
            try:
                last_401 = None
                eof_count = 0
                for manager in managers:
                    candidate = getattr(
                        manager,
                        "_control_last_401_at",
                        None,
                    )
                    if candidate and (
                        last_401 is None or candidate > last_401
                    ):
                        last_401 = candidate
                    eof_count = max(
                        eof_count,
                        int(
                            getattr(
                                manager,
                                "_socks5_eof_count",
                                0,
                            )
                            or 0
                        ),
                    )
                data["control_last_401_at"] = last_401
                data["socks5_eof_count"] = eof_count
                data["control_401_badge"] = bool(
                    last_401 and (time.time() - last_401) < 300
                )
            except Exception:
                pass
            try:
                import shared_state as supervisor_state

                supervisors = []
                for supervisor in (
                    getattr(
                        supervisor_state,
                        "station_supervisors",
                        None,
                    )
                    or []
                ):
                    readonly = getattr(
                        supervisor,
                        "breaker_open_readonly",
                        None,
                    )
                    supervisors.append(
                        {
                            "station": int(
                                getattr(supervisor, "station", -1)
                            ),
                            "breaker_open": bool(readonly())
                            if callable(readonly)
                            else bool(
                                getattr(
                                    supervisor,
                                    "breaker_open_until",
                                    0.0,
                                )
                            ),
                            "half_open_pending": bool(
                                getattr(
                                    supervisor,
                                    "half_open_pending",
                                    False,
                                )
                            ),
                            "consecutive_failures": int(
                                getattr(
                                    supervisor,
                                    "consecutive_failures",
                                    0,
                                )
                            ),
                            "restart_in_progress": bool(
                                getattr(
                                    supervisor,
                                    "restart_in_progress",
                                    False,
                                )
                            ),
                        }
                    )
                data["supervisors"] = supervisors
            except Exception:
                pass
        return data


__all__ = ["register_vpn_status_routes"]
