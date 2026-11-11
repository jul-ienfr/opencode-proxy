"""NordVPN status/country routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import FastAPI


def nordvpn_status_snapshot(managers_getter: Callable[[], list[Any]]) -> dict[str, Any]:
    state = {"connected": False, "country": None, "city": None, "ip": None}
    for manager in managers_getter():
        try:
            status = manager.get_status()
        except Exception:
            continue
        if state["country"] is None:
            state["country"] = status.get("current_country") or None
        if getattr(manager, "status", None) in ("connected", "degraded") or status.get("status") in (
            "connected",
            "degraded",
        ):
            state["connected"] = True
        if state["ip"] is None:
            state["ip"] = getattr(manager, "current_ip", None) or status.get("ip") or None
        if state["city"] is None:
            state["city"] = status.get("city") or None
    return state


def register_nordvpn_routes(
    app: FastAPI,
    *,
    managers_getter: Callable[[], list[Any]],
    countries_getter: Callable[[bool], Awaitable[list[str]]],
    config_getter: Callable[[], dict[str, Any]],
    credentials_path: str,
) -> None:
    @app.get("/api/vpn/nordvpn-available")
    async def nordvpn_available():
        has = os.path.exists(credentials_path) and os.path.getsize(credentials_path) > 0
        cfg = config_getter() or {}
        provider = str(cfg.get("server_provider") or "nordvpn").lower()
        return {"available": has and provider == "nordvpn"}

    @app.get("/api/vpn/nordvpn-status")
    async def nordvpn_status():
        return nordvpn_status_snapshot(managers_getter)

    @app.get("/api/vpn/nordvpn-countries")
    async def nordvpn_countries_ep():
        cfg = config_getter() or {}
        return {"countries": await countries_getter(bool(cfg.get("use_nordvpn_api")))}

    @app.get("/api/vpn/countries")
    async def vpn_countries_ep():
        cfg = config_getter() or {}
        return {"countries": await countries_getter(bool(cfg.get("use_nordvpn_api")))}


__all__ = ["nordvpn_status_snapshot", "register_nordvpn_routes"]
