"""Egress mode and public-proxy routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request


def register_egress_mode_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    pool_getter: Callable[[], Any],
    managers_getter: Callable[[], list[Any]],
    settings: Any,
    persist_vpn_config: Callable[[dict[str, Any]], Any],
    egress_audit: Callable[[str, dict[str, Any]], Any],
    debug: Callable[[str], Any],
) -> None:
    @app.get("/api/vpn/public-proxies")
    async def get_vpn_public_proxies():
        pool = pool_getter()
        config = (getattr(settings, "IP_ROTATION", None) or {}).get(
            "public_proxies"
        ) or {}
        rows = list(getattr(pool, "_public_https_proxies", None) or [])
        worker = getattr(pool, "_public_worker", None)
        alive = False
        if worker is not None:
            probe = getattr(worker, "is_alive", None)
            if callable(probe):
                try:
                    alive = bool(await asyncio.to_thread(probe))
                except Exception:
                    alive = False
        endpoints = [
            {
                "host": str(row.get("host") or ""),
                "port": int(row.get("port") or 0),
                "egress_ip": str(row.get("egress_ip") or ""),
                "provider": str(row.get("provider") or "public"),
                "enabled": bool(row.get("enabled", True)),
                "transport": "worker",
                "last_status": int(row.get("last_status") or 0),
                "last_checked_at": float(row.get("last_checked_at") or 0),
            }
            for row in rows
            if isinstance(row, dict)
        ]
        return {
            "enabled": bool(config.get("enabled", False)),
            "isolation": str(config.get("isolation") or "process"),
            "active": len(endpoints),
            "worker": {
                "kind": type(worker).__name__ if worker is not None else "",
                "port": int(getattr(worker, "port", 0) or 0)
                if worker is not None
                else 0,
                "alive": alive,
            },
            "endpoints": endpoints,
        }

    @app.post("/api/vpn/public-proxies/refresh")
    async def refresh_vpn_public_proxies(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        pool = pool_getter()
        config = dict(
            (getattr(settings, "IP_ROTATION", None) or {}).get("public_proxies")
            or {}
        )
        for key in (
            "enabled",
            "feed_path",
            "max_active",
            "max_per_24",
            "isolation",
            "container_image",
        ):
            if key in body:
                config[key] = body[key]
        err = persist_vpn_config({"public_proxies": config})
        if err:
            return {"error": err}
        try:
            if pool is not None and hasattr(pool, "configure_public_proxies"):
                pool.configure_public_proxies(config)
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        active = len(getattr(pool, "_public_https_proxies", None) or [])
        if bool(config.get("enabled", False)) and active == 0:
            return {
                "error": "pool activé mais aucun endpoint chargé "
                "(feed ou worker indisponible)",
                "enabled": True,
                "isolation": str(config.get("isolation") or "process"),
                "active": 0,
            }
        return {
            "ok": True,
            "enabled": bool(config.get("enabled", False)),
            "isolation": str(config.get("isolation") or "process"),
            "active": active,
        }

    @app.post("/api/vpn/public-proxies/revalidate")
    async def revalidate_vpn_public_proxies(request: Request):
        err = token_checker(request)
        if err:
            return err
        pool = pool_getter()
        if pool is None or not hasattr(pool, "revalidate_public_proxies"):
            return {"error": "pool public indisponible"}
        before = len(getattr(pool, "_public_https_proxies", None) or [])
        try:
            active = await pool.revalidate_public_proxies()
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}
        return {"ok": True, "before": before, "active": int(active or 0)}

    @app.post("/api/vpn/proxy-mode")
    async def set_vpn_proxy_mode(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        try:
            from free.auto_egress import (
                derive_mode,
                equal_weights,
                normalize_providers,
                normalize_weights,
                providers_from_mode,
            )

            if "providers" in body:
                providers = normalize_providers(body.get("providers"))
                if not providers:
                    return {"error": "au moins un provider est requis"}
                mode = derive_mode(providers)
            else:
                mode = str(body.get("mode", "")).strip()
                if mode not in ("vpn", "socks5", "direct", "auto_egress"):
                    return {
                        "error": "mode doit être vpn|socks5|direct|auto_egress, "
                        f"reçu {mode!r}"
                    }
                providers = providers_from_mode(mode)
            rotation_config = getattr(settings, "IP_ROTATION", None) or {}
            strategy = str(body.get("strategy") or "").strip().lower()
            if strategy not in ("fallback", "balance"):
                strategy = str(
                    rotation_config.get("egress_strategy") or "fallback"
                ).strip().lower()
            if strategy not in ("fallback", "balance"):
                strategy = "fallback"
            raw_weights = body.get("weights")
            if isinstance(raw_weights, dict):
                weights = normalize_weights(raw_weights, providers)
            elif strategy == "balance":
                weights = equal_weights(providers)
            else:
                existing = rotation_config.get("egress_weights")
                weights = dict(existing) if isinstance(existing, dict) else {}
        except ValueError as exc:
            return {"error": str(exc)}
        managers = managers_getter()
        if not managers:
            return {"error": "gestionnaire VPN non initialisé"}
        runtime_mode = "vpn" if mode == "auto_egress" else mode
        for manager in managers:
            manager.proxy_mode = runtime_mode
        persist_vpn_config(
            {
                "proxy_mode": runtime_mode,
                "egress_providers": providers,
                "egress_strategy": strategy,
                "egress_weights": weights,
            }
        )
        egress_audit(
            "egress_mode",
            {
                "providers": list(providers),
                "strategy": strategy,
                "weights": dict(weights),
                "runtime_mode": runtime_mode,
            },
        )
        if mode == "auto_egress":
            persist_vpn_config({"socks5_fallback_enabled": True})
        try:
            pool = pool_getter()
            if pool is not None and hasattr(pool, "set_egress_config"):
                pool.set_egress_config(
                    providers=providers,
                    strategy=strategy,
                    weights=weights,
                )
            elif pool is not None:
                pool._egress_providers = list(providers)
        except Exception:
            pass
        if runtime_mode in ("vpn", "socks5"):
            try:
                try:
                    import config.settings as runtime_settings
                    from config.settings import (
                        resolved_station_count,
                    )

                    wanted = resolved_station_count(
                        getattr(runtime_settings, "IP_ROTATION", {}) or {}
                    )
                    if wanted > len(managers):
                        from opencode import _apply_station_count

                        await _apply_station_count(wanted)
                        managers = managers_getter()
                        for manager in managers:
                            manager.proxy_mode = runtime_mode
                except Exception as error:
                    debug(f"  [vpn] proxy-mode upscale check failed: {error}")
                starts = []
                for manager in managers:
                    if getattr(manager, "enabled", True) and str(
                        getattr(manager, "status", "disconnected")
                        or "disconnected"
                    ) not in ("connected", "degraded"):
                        starts.append(
                            asyncio.create_task(
                                manager.start(),
                                name=(
                                    "proxy-mode-start-st"
                                    f"{getattr(manager, '_station', '?')}"
                                ),
                            )
                        )
                if starts:
                    asyncio.create_task(_reap_proxy_mode_starts(starts, debug))
            except Exception as error:
                debug(f"  [vpn] proxy-mode resurrect failed: {error}")
        return {"ok": True, "mode": mode}


async def _reap_proxy_mode_starts(tasks: list[Any], debug: Callable[[str], Any]):
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results):
        if isinstance(result, Exception):
            debug(
                f"  [vpn] proxy-mode detached start failed "
                f"({task.get_name()}): {result}"
            )


__all__ = ["register_egress_mode_routes"]
