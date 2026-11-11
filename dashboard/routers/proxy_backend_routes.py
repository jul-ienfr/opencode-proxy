"""SOCKS5 and HTTPS proxy backend routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request


def register_proxy_backend_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    pool_getter: Callable[[], Any],
    socks5_payload: Callable[[Any], list[dict[str, Any]]],
    socks5_auto_rotate_state: Callable[[], bool],
    socks5_fallback_state: Callable[[], bool],
    apply_socks5_rows: Callable[..., Any],
    as_index: Callable[[Any], int],
    stored_proxy_credentials: Callable[..., tuple[Any, Any]],
    socks5_probe: Callable[..., Any],
    https_fallback_state: Callable[[], bool],
    https_payload: Callable[[Any], list[dict[str, Any]]],
    https_manual_rows: Callable[[Any], list[dict[str, Any]]],
    apply_https_rows: Callable[[list[dict[str, Any]]], Any],
    https_probe: Callable[..., Any],
    persist_vpn_config: Callable[[dict[str, Any]], Any],
) -> None:
    @app.get("/api/vpn/socks5")
    async def get_vpn_socks5():
        pool = pool_getter()
        return {
            "proxies": socks5_payload(pool),
            "rotate": socks5_auto_rotate_state(),
            "fallback": socks5_fallback_state(),
            "mode": getattr(pool, "proxy_mode", ""),
            "enabled": bool(getattr(pool, "enabled", False)),
        }

    @app.post("/api/vpn/socks5/refresh-egress")
    async def refresh_vpn_socks5_egress(request: Request):
        err = token_checker(request)
        if err:
            return err
        pool = pool_getter()
        if pool is None or not hasattr(pool, "refresh_socks5_egress"):
            return {"ok": False, "error": "pool_unavailable"}
        try:
            results = await asyncio.to_thread(pool.refresh_socks5_egress, force=True)
            duplicates = (
                pool.duplicate_egress_groups()
                if hasattr(pool, "duplicate_egress_groups")
                else {}
            )
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "results": results, "duplicates": duplicates}

    @app.post("/api/vpn/socks5/rotate-egress")
    async def rotate_vpn_socks5_egress(request: Request):
        err = token_checker(request)
        if err:
            return err
        pool = pool_getter()
        if pool is None or not hasattr(pool, "rotate_exhausted_managed"):
            return {"ok": False, "error": "pool_unavailable"}
        try:
            started = int(pool.rotate_exhausted_managed())
            scan = pool.rotation_scan() if hasattr(pool, "rotation_scan") else []
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "started": started, "scan": scan}

    @app.post("/api/vpn/socks5")
    async def add_vpn_socks5(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        pool = pool_getter()
        try:
            port = int(body.get("port") or 1080)
        except (TypeError, ValueError):
            return {"error": f"port invalide: {body.get('port')!r}"}
        rows = list(getattr(pool, "_socks5_proxies", None) or [])
        rows.append(
            {
                "host": str(body.get("host", "") or ""),
                "port": port,
                "enabled": True,
                "username": body.get("username"),
                "password": body.get("password"),
            }
        )
        err = apply_socks5_rows(rows, action="socks5_add")
        if err:
            return err
        return {"ok": True, "proxies": socks5_payload(pool)}

    @app.post("/api/vpn/socks5/remove")
    async def remove_vpn_socks5(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        pool = pool_getter()
        rows = list(getattr(pool, "_socks5_proxies", None) or [])
        index = as_index(body.get("index"))
        if not (0 <= index < len(rows)):
            return {"error": f"proxy index hors bornes: {index!r}"}
        del rows[index]
        err = apply_socks5_rows(rows, action="socks5_remove")
        if err:
            return err
        return {"ok": True, "proxies": socks5_payload(pool)}

    @app.post("/api/vpn/socks5/toggle")
    async def toggle_vpn_socks5(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        pool = pool_getter()
        rows = list(getattr(pool, "_socks5_proxies", None) or [])
        index = as_index(body.get("index"))
        if not (0 <= index < len(rows)):
            return {"error": f"proxy index hors bornes: {index!r}"}
        rows[index] = dict(rows[index])
        rows[index]["enabled"] = bool(body.get("enabled", True))
        err = apply_socks5_rows(rows, action="socks5_toggle")
        if err:
            return err
        return {"ok": True}

    @app.post("/api/vpn/socks5/test")
    async def test_vpn_socks5(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        host = body.get("host", "")
        port = body.get("port", 0)
        label = str(body.get("label") or "").strip()
        ipv6 = False
        username = password = None
        pool = pool_getter()
        row = None
        if label and pool is not None:
            row = next(
                (
                    item
                    for item in (getattr(pool, "_socks5_proxies", None) or [])
                    if isinstance(item, dict)
                    and str(item.get("label") or "") == label
                ),
                None,
            )
        if row is not None:
            host = row.get("host", host)
            port = row.get("port", port)
            username = row.get("username")
            password = row.get("password")
            ipv6 = bool(row.get("ipv6", False))
        else:
            username, password = stored_proxy_credentials(
                pool,
                "_socks5_proxies",
                host,
                port,
            )
        result = await socks5_probe(
            host,
            port,
            username=username,
            password=password,
            ipv6=ipv6,
        )
        if result.get("opencode_ok") and pool is not None:
            try:
                endpoint = next(
                    (
                        ep
                        for ep in pool._socks5_enabled_eps()
                        if str(getattr(ep, "label", "")) == label
                        or int(getattr(ep, "port", 0)) == int(port)
                    ),
                    None,
                )
                if endpoint is not None:
                    station = int(getattr(endpoint, "_station", 0))
                    health = pool._station_health.get(station)
                    if health is not None:
                        health.repair_succeeded()
                    pool._station_health.observe(station, True)
                    pool._station_health.observe(station, True)
            except Exception:
                pass
        return result

    @app.post("/api/vpn/socks5/rotate")
    async def rotate_vpn_socks5(request: Request):
        err = token_checker(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        if "rotate" in body:
            value = bool(body.get("rotate"))
            persist_vpn_config({"socks5_auto_rotate": value})
            pool = pool_getter()
            if pool is not None and hasattr(pool, "_socks5_auto_rotate"):
                pool._socks5_auto_rotate = value
            return {"ok": True, "rotate": value}
        pool = pool_getter()
        if pool is None or not hasattr(pool, "rotate_socks5_now"):
            return {"error": "pool socks5 non disponible"}
        next_proxy = pool.rotate_socks5_now()
        return {
            "ok": next_proxy is not None,
            "next": getattr(next_proxy, "pid", None)
            if next_proxy is not None
            else None,
        }

    @app.post("/api/vpn/socks5/fallback")
    async def set_vpn_socks5_fallback(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        enabled = bool(body.get("enabled"))
        persist_vpn_config({"socks5_fallback_enabled": enabled})
        pool = pool_getter()
        if pool is not None and hasattr(pool, "_socks5_fallback_enabled"):
            pool._socks5_fallback_enabled = enabled
        return {"ok": True, "enabled": enabled}

    @app.get("/api/vpn/https-proxies")
    async def get_vpn_https_proxies():
        pool = pool_getter()
        return {
            "enabled": https_fallback_state(),
            "proxies": https_payload(pool),
        }

    @app.post("/api/vpn/https-proxies")
    async def add_vpn_https_proxy(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        pool = pool_getter()
        try:
            port = int(body.get("port") or 89)
        except (TypeError, ValueError):
            return {"error": f"port invalide: {body.get('port')!r}"}
        rows = https_manual_rows(pool)
        rows.append(
            {
                "host": str(body.get("host", "") or ""),
                "port": port,
                "enabled": True,
                "username": body.get("username"),
                "password": body.get("password"),
            }
        )
        err = apply_https_rows(rows)
        if err:
            return err
        return {"ok": True, "proxies": https_payload(pool)}

    @app.post("/api/vpn/https-proxies/remove")
    async def remove_vpn_https_proxy(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        pool = pool_getter()
        rows = https_manual_rows(pool)
        index = as_index(body.get("index"))
        if not (0 <= index < len(rows)):
            return {"error": f"proxy index hors bornes: {index!r}"}
        del rows[index]
        err = apply_https_rows(rows)
        if err:
            return err
        return {"ok": True, "proxies": https_payload(pool)}

    @app.post("/api/vpn/https-proxies/toggle")
    async def toggle_vpn_https_proxy(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        pool = pool_getter()
        rows = https_manual_rows(pool)
        index = as_index(body.get("index"))
        if not (0 <= index < len(rows)):
            return {"error": f"proxy index hors bornes: {index!r}"}
        rows[index] = dict(rows[index])
        rows[index]["enabled"] = bool(body.get("enabled", True))
        err = apply_https_rows(rows)
        if err:
            return err
        return {"ok": True}

    @app.post("/api/vpn/https-proxies/fallback")
    async def set_vpn_https_fallback(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        enabled = bool(body.get("enabled"))
        persist_vpn_config({"https_fallback_enabled": enabled})
        pool = pool_getter()
        if pool is not None and hasattr(pool, "_https_fallback_enabled"):
            pool._https_fallback_enabled = enabled
        return {"ok": True, "enabled": enabled}

    @app.post("/api/vpn/https-proxies/test")
    async def test_vpn_https_proxy(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        host = body.get("host", "")
        port = body.get("port", 0)
        pool = pool_getter()
        username, password = stored_proxy_credentials(
            pool,
            "_https_proxies",
            host,
            port,
        )
        return await https_probe(
            host,
            port,
            username=username,
            password=password,
        )


__all__ = ["register_proxy_backend_routes"]
