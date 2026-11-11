"""Free-leg registry and allocator status routes.

Extracted from ``dashboard/api.py`` (slice S10).  Both endpoints are read-only
views over ``free.ip_registry.status.registry_status`` and tolerate a disabled
registry or a missing database.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request


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


__all__ = ["register_free_status_routes"]
