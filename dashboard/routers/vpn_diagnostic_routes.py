"""VPN discovery, upload and diagnostic routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request


def register_vpn_diagnostic_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    managers_getter: Callable[[], list[Any]],
    proxy_mode_getter: Callable[[], str],
    servers_by_country: Callable[..., Any],
    countries_getter: Callable[..., Any],
    settings: Any,
    persist_vpn_config: Callable[[dict[str, Any]], Any],
    docker_diag: Callable[[], dict[str, Any]],
    compose_config: Callable[[], list[str]],
    config_yaml_mtime: Callable[[], float],
    config_yaml_known_mtime_getter: Callable[[], float],
    root: str,
    no_window_flag: int = 0,
) -> None:
    @app.post("/api/vpn/discover-and-add")
    async def vpn_discover_and_add(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        code = str(body.get("country") or "").strip().upper()
        try:
            limit = max(1, min(50, int(body.get("limit") or 5)))
        except (TypeError, ValueError):
            limit = 5
        if not code:
            return {"error": "country requis (code, ex: DE)"}
        servers = await servers_by_country(code, limit=limit)
        if not servers:
            return {"error": f"aucun serveur trouvé pour {code}"}
        country_name = code
        for country in await countries_getter(True):
            if str(country.get("code", "")).upper() == code:
                country_name = country.get("name") or code
                break
        config = getattr(settings, "IP_ROTATION", None) or {}
        current = str(config.get("server_countries") or "")
        names = [item.strip() for item in current.split(",") if item.strip()]
        if country_name not in names:
            names.append(country_name)
        persist_vpn_config({"server_countries": ", ".join(names)})
        return {"count": len(servers), "country": country_name}

    @app.post("/api/vpn/upload-config")
    async def vpn_upload_config(request: Request):
        err = token_checker(request)
        if err:
            return err
        form = await request.form()
        name = str(form.get("name") or "").strip() or "custom"
        name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
        if not name.lower().endswith(".ovpn"):
            name += ".ovpn"
        upload = form.get("config")
        content = (
            await upload.read()
            if upload is not None and not isinstance(upload, str)
            else None
        )
        if not content:
            return {
                "error": "fichier config manquant (champ FormData 'config')"
            }
        custom_dir = os.path.join(root, "vpn_configs", "custom")
        os.makedirs(custom_dir, exist_ok=True)
        path = os.path.join(custom_dir, name)
        with open(path, "wb") as handle:
            handle.write(content)
        relative = os.path.join("vpn_configs", "custom", name)
        persist_vpn_config(
            {"custom_ovpn_file": relative.replace(os.sep, "/")}
        )
        return {"ok": True, "path": relative.replace(os.sep, "/")}

    @app.get("/api/vpn/diagnostic")
    async def vpn_diagnostic():
        managers = managers_getter()
        mode = proxy_mode_getter()
        status, ip = "not_configured", None
        if managers:
            connected = [
                manager
                for manager in managers
                if getattr(manager, "status", None)
                in ("connected", "degraded")
            ]
            if connected:
                status = (
                    "connected"
                    if any(
                        getattr(manager, "status", None) == "connected"
                        for manager in managers
                    )
                    else "degraded"
                )
            elif any(
                getattr(manager, "status", None)
                in ("connecting", "rotating")
                for manager in managers
            ):
                status = "connecting"
            else:
                status = "not_connected"
            ip = next(
                (
                    manager.current_ip
                    for manager in managers
                    if manager.current_ip
                ),
                None,
            )
        nordvpn = {"available": False, "exe": None, "status": None}
        if status in ("connected", "degraded"):
            from dashboard.routers.nordvpn_routes import (
                nordvpn_status_snapshot,
            )

            nordvpn = {
                "available": True,
                "exe": None,
                "status": nordvpn_status_snapshot(managers_getter),
            }
        docker = await asyncio.to_thread(docker_diag)
        wsl2 = {"available": False}
        try:
            result = await asyncio.to_thread(
                lambda: subprocess.run(
                    ["wsl", "--status"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    creationflags=no_window_flag,
                )
            )
            wsl2["available"] = result.returncode == 0
        except Exception:
            pass
        openvpn = {"available": False, "path": None}
        ovpn_path = shutil.which("openvpn")
        if ovpn_path:
            openvpn = {"available": True, "path": ovpn_path}
        elif os.path.exists("/usr/sbin/openvpn"):
            openvpn = {"available": True, "path": "/usr/sbin/openvpn"}
        docker_ok = bool(docker.get("running"))
        if mode == "vpn":
            if status == "connected":
                recommendation = (
                    "Connexion VPN active — rotation IP opérationnelle"
                )
            elif status == "degraded":
                recommendation = (
                    "Connexion VPN dégradée mais routable (reset récent / "
                    "cooldown / SOCKS) — voir stations"
                )
            elif docker_ok:
                recommendation = (
                    "Docker OK — VPN non connecté (voir statut ci-dessus)"
                )
            else:
                recommendation = (
                    "Docker absent ou arrêté — installer/démarrer Docker "
                    "puis reconnecter"
                )
        else:
            recommendation = (
                f"Mode {mode} actif (pas de tunnel docker requis)"
            )
        mtime = config_yaml_mtime()
        known = config_yaml_known_mtime_getter()
        return {
            "current_mode": mode,
            "status": status,
            "ip": ip,
            "nordvpn_app": nordvpn,
            "docker": docker,
            "wsl2": wsl2,
            "openvpn": openvpn,
            "recommendation": recommendation,
            "config_yaml_dirty": mtime != known,
            "config_yaml_mtime": mtime,
            "config_yaml_known": known,
            "compose_config": await asyncio.to_thread(compose_config),
        }


__all__ = ["register_vpn_diagnostic_routes"]
