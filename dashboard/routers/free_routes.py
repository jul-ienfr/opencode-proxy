"""Free-leg registry and allocator status routes.

Extracted from ``dashboard/api.py`` (slice S10).  Both endpoints are read-only
views over ``free.ip_registry.status.registry_status`` and tolerate a disabled
registry or a missing database.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_free_status_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    registry_status_getter: Callable[[], dict[str, Any]] | None = None,
) -> None:
    def _registry_status() -> dict[str, Any]:
        if registry_status_getter is not None:
            return registry_status_getter()
        from free.ip_registry.status import registry_status

        return registry_status()

    @app.get("/api/free/allocator/status")
    async def get_allocator_status(request: Request):
        err = token_checker(request)
        if err:
            return err
        status = await asyncio.to_thread(_registry_status)
        db = status.get("db") or {}
        return {
            "generated_at": status.get("generated_at"),
            "enabled": status.get("enabled"),
            "allocator": status.get("allocator"),
            "db": {
                "assignments_open": db.get("assignments_open", 0),
                "open_assignments": db.get("open_assignments", []),
                "ip_state": db.get("ip_state", {}),
            },
        }

    @app.get("/api/free/ip-registry/status")
    async def get_ip_registry_status(request: Request):
        err = token_checker(request)
        if err:
            return err
        return await asyncio.to_thread(_registry_status)

    def _reserve_view() -> dict[str, Any]:
        from free.client_reserve import (
            count,
            probe_budget_remaining,
            reserved_ips,
        )
        from free.reserve_settings import DEFAULTS, ENV_NAMES, all_settings

        settings = all_settings()
        overrides = {
            name: venue
            for name, venue in ENV_NAMES.items()
            if str(os.getenv(venue, "") or "").strip()
        }
        return {
            "settings": settings,
            "defaults": dict(DEFAULTS),
            "env_overrides": overrides,
            "ips": sorted(reserved_ips()),
            "count": count(),
            "probe_budget_remaining": probe_budget_remaining(),
        }

    @app.get("/api/free/client-reserve")
    async def get_client_reserve(request: Request):
        err = token_checker(request)
        if err:
            return err
        return await asyncio.to_thread(_reserve_view)

    @app.post("/api/free/client-reserve")
    async def update_client_reserve(request: Request):
        err = token_checker(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                status_code=400,
                content={"error": "JSON invalide"},
            )
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "objet JSON attendu"},
            )
        from free.reserve_settings import set_many

        try:
            await asyncio.to_thread(set_many, body)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        return await asyncio.to_thread(_reserve_view)


__all__ = ["register_free_status_routes"]
