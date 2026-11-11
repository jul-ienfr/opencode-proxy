"""VPN image-update route extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request


def register_vpn_update_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    manager_getter: Callable[[], Any],
    managers_getter: Callable[[], list[Any]],
    apply_update: Callable[[list[Any], Any], Any] | None = None,
) -> None:
    async def _apply_update(managers: list[Any], primary: Any) -> dict[str, Any]:
        if apply_update is not None:
            return dict(await apply_update(managers, primary))
        from ops.updater import apply_fleet_update

        return dict(await apply_fleet_update(managers, primary))

    @app.post("/api/vpn/update")
    async def update_vpn(request: Request):
        err = token_checker(request)
        if err:
            return err
        manager = manager_getter()
        if manager is None:
            return {"error": "gestionnaire VPN non initialisé"}
        try:
            available = await manager.check_update()
            if not available:
                return {
                    "ok": False,
                    "error": "aucune mise à jour disponible",
                    "update": manager.get_status()["update"],
                }
            if not manager._update_opportune():
                return {"ok": False, "error": "pas opportun (trafic actif)"}
            result = await _apply_update(list(managers_getter() or []), manager)
            result["update"] = manager.get_status()["update"]
            return result
        except Exception as exc:
            return {"error": str(exc)}


__all__ = ["register_vpn_update_routes"]
