"""VPN credentials/state/export routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_vpn_state_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    manager_getter: Callable[[], Any],
    credentials_path: str,
    write_credentials: Callable[[str, str], Any],
) -> None:
    @app.get("/api/vpn/credentials")
    async def get_vpn_credentials(request: Request):
        err = token_checker(request)
        if err:
            return err
        exists = (
            os.path.exists(credentials_path)
            and os.path.getsize(credentials_path) > 0
        )
        username_saved = ""
        if exists:
            try:
                with open(credentials_path, encoding="utf-8") as handle:
                    for line in handle:
                        if line.startswith("OPENVPN_USER="):
                            username_saved = line.split("=", 1)[1].strip()
                            break
            except Exception:
                pass
        return {
            "exists": exists,
            "username_preview": (
                username_saved[:4] + "****" if username_saved else ""
            ),
        }

    @app.post("/api/vpn/credentials")
    async def save_vpn_credentials(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        username = body.get("username", "")
        password = body.get("password", "")
        if not username or not password:
            return JSONResponse(
                status_code=400,
                content={"error": "Nom d'utilisateur et mot de passe requis"},
            )
        try:
            write_credentials(username, password)
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": str(exc)})
        return {
            "ok": True,
            "note": (
                "restart the VPN container (docker compose up -d) for gluetun "
                "to pick up new credentials"
            ),
        }

    @app.post("/api/vpn/save-state")
    async def save_vpn_state(request: Request):
        err = token_checker(request)
        if err:
            return err
        manager = manager_getter()
        if manager is None:
            return {"error": "gestionnaire VPN non initialisé"}
        manager.save_state()
        return {"ok": True}

    @app.get("/api/vpn/export")
    async def export_vpn_config(request: Request):
        err = token_checker(request)
        if err:
            return err
        manager = manager_getter()
        if manager is None:
            return {"error": "gestionnaire VPN non initialisé"}
        config = manager.get_config()
        state = {
            "ip_history": manager._ip_history,
            "total_switches": manager._total_switches,
        }
        return {
            "config": config,
            "state": state,
            "exported_at": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ",
                time.gmtime(),
            ),
            "version": "1.0",
        }


__all__ = ["register_vpn_state_routes"]
