"""Transparent-retry configuration routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

RETRY_KEYS = (
    "enabled",
    "max_wait_s",
    "base_delay_s",
    "max_delay_s",
    "keepalive_interval_s",
    "buffer_max_bytes",
)


def register_retry_routes(
    app: FastAPI,
    *,
    config_getter: Callable[[], dict[str, Any]],
    config_persister: Callable[[dict[str, Any]], str | None],
    token_checker: Callable[[Request], Any],
) -> None:
    @app.get("/api/retry-config")
    async def get_retry_config():
        return config_getter()

    @app.post("/api/retry-config")
    async def update_retry_config(request: Request):
        err = token_checker(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"error": "JSON invalide"})
        if not isinstance(body, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "objet JSON attendu"},
            )
        updates: dict[str, Any] = {}
        for key in RETRY_KEYS:
            if key in body:
                updates[key] = body[key]
        if "enabled" in updates:
            raw = updates["enabled"]
            if isinstance(raw, str):
                updates["enabled"] = raw.strip().lower() in (
                    "1",
                    "true",
                    "yes",
                    "on",
                )
            else:
                updates["enabled"] = bool(raw)
        if not updates:
            return config_getter()
        persist_err = config_persister(updates)
        if persist_err:
            return JSONResponse(status_code=400, content={"error": persist_err})
        return config_getter()


__all__ = ["RETRY_KEYS", "register_retry_routes"]
