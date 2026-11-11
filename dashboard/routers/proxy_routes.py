"""Proxy server control routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI


def register_proxy_routes(
    app: FastAPI,
    *,
    server_manager_getter: Callable[[], Any] | None,
    default_port: int,
) -> None:
    def _manager() -> Any:
        return server_manager_getter() if server_manager_getter else None

    def _refresh(manager: Any) -> None:
        """Réconcilie is_running avant toute décision start/stop/status.

        Un boot plus long que le délai d'attente de ServerManager.start()
        laissait le flag à False alors qu'uvicorn écoutait : le dashboard
        affichait « le proxy demarre encore » et un start/stop était décidé
        sur un état faux.
        """
        refresh = getattr(manager, "refresh_state", None)
        if callable(refresh):
            try:
                refresh()
            except Exception:
                pass

    @app.get("/api/proxy/status")
    async def proxy_status():
        manager = _manager()
        try:
            effective_port = (
                int(getattr(manager, "port", default_port))
                if manager
                else default_port
            )
        except (TypeError, ValueError):
            effective_port = default_port
        if manager:
            _refresh(manager)
            return {"running": manager.is_running, "port": effective_port}
        return {"running": True, "port": effective_port}

    @app.post("/api/proxy/start")
    async def proxy_start():
        manager = _manager()
        if not manager:
            return {
                "status": "ok",
                "message": "Aucun gestionnaire de serveur disponible",
            }
        _refresh(manager)
        if manager.is_running:
            return {"status": "ok", "message": "Déjà en cours"}
        await asyncio.to_thread(manager.start)
        return {"status": "ok", "message": "Proxy démarré"}

    @app.post("/api/proxy/stop")
    async def proxy_stop():
        manager = _manager()
        if not manager:
            return {
                "status": "ok",
                "message": "Aucun gestionnaire de serveur disponible",
            }
        _refresh(manager)
        if not manager.is_running:
            return {"status": "ok", "message": "Déjà arrêté"}
        await asyncio.to_thread(manager.stop)
        return {"status": "ok", "message": "Proxy arrêté"}

    @app.post("/api/proxy/restart")
    async def proxy_restart(full: bool = False):
        manager = _manager()
        if not manager:
            return {
                "status": "error",
                "message": "Aucun gestionnaire de serveur disponible",
            }
        _refresh(manager)
        target = manager.full_restart if full else manager.restart
        threading.Thread(
            target=target,
            name="proxy-restart",
            daemon=False,
        ).start()
        message = (
            "Redémarrage complet déclenché"
            if full
            else "Redémarrage déclenché"
        )
        return {"status": "ok", "message": message}


__all__ = ["register_proxy_routes"]
