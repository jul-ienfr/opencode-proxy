"""Small VPN control routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def shared_latency_engine() -> Any:
    import shared_state

    engine = getattr(shared_state, "latency_engine", None)
    if engine is None:
        from latency_rotation import get_engine

        engine = get_engine()
    return engine


def register_vpn_control_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    validate_payload: Callable[[Any], list[str]],
    engine_getter: Callable[[], Any],
    debug_logger: Callable[[str], Any] | None = None,
) -> None:
    @app.post("/api/vpn-config/validate")
    async def validate_vpn_config(request: Request):
        err = token_checker(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception as exc:
            return JSONResponse(
                status_code=400,
                content={"valid": False, "errors": [str(exc)]},
            )
        errors = validate_payload(body)
        return {"valid": not errors, "errors": errors}

    @app.post("/api/vpn/rotation-paused")
    async def set_rotation_paused(request: Request):
        err = token_checker(request)
        if err:
            return err
        try:
            body = await request.json()
        except Exception:
            body = {}
        paused = bool(body.get("paused"))
        engine_getter().paused = paused
        if debug_logger is not None:
            try:
                debug_logger(f"  [vpn] rotation_paused → {paused}")
            except Exception:
                pass
        return {"ok": True, "paused": paused}


__all__ = ["register_vpn_control_routes", "shared_latency_engine"]
