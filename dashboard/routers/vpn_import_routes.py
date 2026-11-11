"""VPN configuration import route extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_vpn_import_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    managers_getter: Callable[[], list[Any]],
    single_manager_getter: Callable[[], Any],
    apply_import: Callable[..., Any],
) -> None:
    @app.post("/api/vpn/import")
    async def import_vpn_config(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        response, status = await apply_import(
            list(managers_getter() or []),
            body,
            fallback_manager=single_manager_getter(),
        )
        if status != 200:
            return JSONResponse(status_code=status, content=response)
        return response


__all__ = ["register_vpn_import_routes"]
