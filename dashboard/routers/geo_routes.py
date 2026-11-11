"""Geo policy and notification routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_geo_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    settings: Any,
    config_yaml_mtime: Callable[[], float],
    config_yaml_known_mtime_setter: Callable[[float], Any],
    root: str,
) -> None:
    @app.get("/api/geo-policies")
    async def get_geo_policies(request: Request):
        err = token_checker(request)
        if err:
            return err
        return {
            "enabled": bool(getattr(settings, "GEO_ENABLED", False)),
            "version": int(getattr(settings, "GEO_VERSION", 1) or 1),
            "policies": dict(getattr(settings, "GEO_POLICIES", {}) or {}),
            "allow_direct_when_compatible": bool(
                getattr(settings, "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE", True)
            ),
        }

    @app.put("/api/geo-policies")
    async def put_geo_policies(request: Request):
        err = token_checker(request)
        if err:
            return err
        control_key = str(
            settings.yaml_get("ip_rotation", "control_api_key", "") or ""
        ).strip()
        provided = request.headers.get("X-API-Key", "")
        if control_key and provided != control_key:
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "X-API-Key valide requise",
                },
            )
        etag = str(int(config_yaml_mtime()))
        if_match = request.headers.get("If-Match", "")
        if if_match and if_match != etag and if_match != f'"{etag}"':
            return JSONResponse(
                status_code=412,
                content={
                    "error": "precondition_failed",
                    "message": "ETag obsolète — rechargez et réessayez",
                },
            )
        body = await request.json()
        if "enabled" in body:
            settings.GEO_ENABLED = bool(body["enabled"])
            settings._yaml_data.setdefault("geo", {})["enabled"] = bool(
                body["enabled"]
            )
        if "version" in body:
            try:
                settings.GEO_VERSION = int(body["version"])
                settings._yaml_data.setdefault("geo", {})["version"] = int(
                    body["version"]
                )
            except Exception:
                return JSONResponse(
                    status_code=400,
                    content={"error": "version doit être un entier"},
                )
        if "policies" in body:
            policies = body["policies"]
            if not isinstance(policies, dict):
                return JSONResponse(
                    status_code=400,
                    content={"error": "policies doit être un objet"},
                )
            settings.GEO_POLICIES.clear()
            settings.GEO_POLICIES.update(policies)
            settings.SORTED_GEO_POLICIES[:] = sorted(policies.items())
            settings._yaml_data.setdefault("geo", {})["policies"] = dict(policies)
        if "allow_direct_when_compatible" in body:
            value = bool(body["allow_direct_when_compatible"])
            settings.GEO_ALLOW_DIRECT_WHEN_COMPATIBLE = value
            settings._yaml_data.setdefault("geo", {})[
                "allow_direct_when_compatible"
            ] = value
        try:
            settings.save_yaml_config()
            config_yaml_known_mtime_setter(config_yaml_mtime())
        except Exception as error:
            return JSONResponse(status_code=500, content={"error": str(error)})
        return {
            "ok": True,
            "enabled": bool(settings.GEO_ENABLED),
            "version": int(settings.GEO_VERSION),
            "allow_direct_when_compatible": bool(
                settings.GEO_ALLOW_DIRECT_WHEN_COMPATIBLE
            ),
            "etag": str(int(config_yaml_mtime())),
        }

    @app.post("/api/geo-policies/rollback")
    async def geo_rollback(request: Request):
        err = token_checker(request)
        if err:
            return err
        settings.GEO_ENABLED = False
        settings._yaml_data.setdefault("geo", {})["enabled"] = False
        try:
            settings.save_yaml_config()
            config_yaml_known_mtime_setter(config_yaml_mtime())
        except Exception as error:
            return JSONResponse(status_code=500, content={"error": str(error)})
        return {"ok": True, "enabled": False}

    @app.get("/api/geo/notifications")
    async def get_geo_notifications(request: Request):
        try:
            path = os.path.join(root, "logs", "geo_notifications.json")
            if not os.path.exists(path):
                return {"notifications": []}
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle) or []
            try:
                from opencode import _direct_country_cache, _direct_ip_cache

                current = {
                    "direct_country": _direct_country_cache.get("country", ""),
                    "direct_ip": _direct_country_cache.get("ip", "")
                    or _direct_ip_cache.get("ip", ""),
                }
            except Exception:
                current = {}
            return {"notifications": data[-20:], "current": current}
        except Exception as error:
            return {"notifications": [], "error": str(error)}


__all__ = ["register_geo_routes"]
