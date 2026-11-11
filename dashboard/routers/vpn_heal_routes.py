"""Idempotent VPN heal route extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request


def register_vpn_heal_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    managers_getter: Callable[[], list[Any]],
    station_count_getter: Callable[[], int] | None,
    fleet_getter: Callable[[], Any],
    apply_station_count: Callable[[int], Any] | None = None,
) -> None:
    def _station_count() -> int:
        if station_count_getter is not None:
            return int(station_count_getter())
        from config.settings import IP_ROTATION, resolved_station_count

        return int(resolved_station_count(IP_ROTATION))

    async def _apply(count: int) -> Any:
        if apply_station_count is not None:
            return await apply_station_count(count)
        from opencode import _apply_station_count

        return await _apply_station_count(count)

    @app.post("/api/vpn/heal")
    async def heal_vpn(request: Request):
        err = token_checker(request)
        if err:
            return err
        try:
            count = int(_station_count())
            managers = list(managers_getter() or [])
            if len(managers) < count:
                await _apply(count)
                managers = list(managers_getter() or [])
            result = await fleet_getter().heal()
            return {
                "healed": bool(result.get("healed")),
                "n": count,
                "managers": len(managers),
                **result,
            }
        except Exception as exc:
            return {"error": str(exc), "healed": False}


__all__ = ["register_vpn_heal_routes"]
