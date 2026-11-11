"""VPN fleet control routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request


def register_vpn_fleet_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    managers_getter: Callable[[], list[Any]],
    single_manager_getter: Callable[[], Any],
    fleet_getter: Callable[[], Any],
) -> None:
    def _managers() -> list[Any]:
        managers = list(managers_getter() or [])
        if not managers:
            manager = single_manager_getter()
            if manager is not None:
                managers = [manager]
        return managers

    @app.post("/api/vpn/toggle")
    async def toggle_vpn(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        enabled = body.get("enabled", True)
        managers = _managers()
        if managers:
            return await fleet_getter().set_enabled_all(enabled)
        return {"error": "gestionnaire VPN non initialisé"}

    @app.post("/api/vpn/connect")
    async def connect_vpn(request: Request):
        err = token_checker(request)
        if err:
            return err
        managers = _managers()
        if not managers:
            return {"error": "gestionnaire VPN non initialisé"}
        try:
            result = await fleet_getter().connect_all()
            first = managers[0]
            try:
                await first.refresh_status()
            except Exception:
                pass
            return {
                **result,
                "ip": getattr(first, "current_ip", None),
                "server": getattr(first, "current_server", None),
            }
        except Exception as exc:
            return {"error": str(exc)}

    @app.post("/api/vpn/disconnect")
    async def disconnect_vpn(request: Request):
        err = token_checker(request)
        if err:
            return err
        if not _managers():
            return {"error": "gestionnaire VPN non initialisé"}
        return await fleet_getter().disconnect_all(stop_containers=False)


__all__ = ["register_vpn_fleet_routes"]
