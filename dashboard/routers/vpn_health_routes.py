"""VPN health-check route extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request


def register_vpn_health_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    manager_getter: Callable[[], Any],
) -> None:
    @app.post("/api/vpn/health-check")
    async def vpn_health_check(request: Request):
        err = token_checker(request)
        if err:
            return err
        manager = manager_getter()
        if manager is None:
            return {"error": "gestionnaire VPN non initialisé"}
        return await manager.health_check()


__all__ = ["register_vpn_health_routes"]
