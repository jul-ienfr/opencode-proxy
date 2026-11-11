"""Next-server VPN route extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request


def shared_pool() -> Any:
    import shared_state

    return getattr(shared_state, "free_ip_pool", None)


def register_vpn_next_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    managers_getter: Callable[[], list[Any]],
    single_manager_getter: Callable[[], Any],
    pool_getter: Callable[[], Any],
) -> None:
    @app.post("/api/vpn/next")
    async def next_vpn(request: Request):
        err = token_checker(request)
        if err:
            return err
        managers = list(managers_getter() or [])
        if not managers:
            return {"error": "gestionnaire VPN non initialisé"}
        try:
            body = await request.json()
        except Exception:
            body = {}
        try:
            station = int(body.get("station", 0))
        except (TypeError, ValueError):
            station = 0
        if station:
            if not (1 <= station <= len(managers)):
                return {
                    "error": (
                        f"station {station} non configurée "
                        f"(station_count={len(managers)})"
                    )
                }
            manager = managers[station - 1]
        else:
            manager = None
        try:
            pool = pool_getter()
            if pool is not None:
                if manager is None:
                    manager = (
                        pool.active_station
                        or single_manager_getter()
                        or (managers[0] if managers else None)
                    )
                if manager is None:
                    return {"error": "aucun manager VPN disponible"}
                ip_before = manager.current_ip
                await pool.switch_ip(station=manager)
                ip_after = manager.current_ip
                server = manager.current_server
                station_out = manager._station
            else:
                manager = (
                    manager
                    or single_manager_getter()
                    or (managers[0] if managers else None)
                )
                if manager is None:
                    return {"error": "aucun manager VPN disponible"}
                ip_before = manager.current_ip
                await manager.connect_next()
                ip_after = manager.current_ip
                server = manager.current_server
                station_out = manager._station
            ok = bool(ip_after) and (
                ip_before is None or ip_after != ip_before
            )
            return {
                "ok": ok,
                "ip": ip_after,
                "server": server,
                "station": station_out,
            }
        except Exception as exc:
            return {"error": str(exc)}


__all__ = ["register_vpn_next_routes", "shared_pool"]
