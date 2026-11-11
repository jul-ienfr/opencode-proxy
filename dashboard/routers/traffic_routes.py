"""Traffic capture routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def register_traffic_routes(
    app: FastAPI,
    *,
    token_checker: Callable[[Request], Any],
    capture: Any,
    mark_viewer: Callable[[], Any],
    apply_lazy: Callable[[], Any],
    user_enabled_getter: Callable[[], bool | None],
    user_enabled_setter: Callable[[bool], Any],
    debug: Callable[[str], Any],
) -> None:
    @app.get("/api/traffic/status")
    async def get_traffic_status(request: Request):
        err = token_checker(request)
        if err:
            return err
        mark_viewer()
        status = capture.status()
        if user_enabled_getter() is None:
            apply_lazy()
        status["user_enabled"] = bool(user_enabled_getter())
        return status

    @app.post("/api/traffic/config")
    async def set_traffic_config(request: Request):
        err = token_checker(request)
        if err:
            return err
        body = await request.json()
        if body.get("enabled") is not None:
            user_enabled_setter(bool(body.get("enabled")))
            apply_lazy()
        else:
            mark_viewer()
        capture.configure(
            max_frames=body.get("max_frames"),
            body_cap=body.get("body_cap"),
            max_bytes=body.get("max_bytes"),
        )
        debug(f"  [traffic] config updated -> {json.dumps(body)}")
        return {"ok": True, **(capture.status())}

    @app.get("/api/traffic/frames")
    async def get_traffic_frames(
        request: Request,
        limit: int = 200,
        offset: int = 0,
        method: str = None,
        path: str = None,
        status: int = None,
        aborted: bool = None,
        since: float = None,
    ):
        err = token_checker(request)
        if err:
            return err
        mark_viewer()
        try:
            limit = max(1, min(int(limit or 200), 200))
        except (TypeError, ValueError):
            limit = 200
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0
        frames = capture.frames(
            limit=limit,
            offset=offset,
            method=method,
            path=path,
            status=status,
            aborted=aborted,
            since=since,
        )
        return {
            "frames": frames,
            "total": len(frames),
            "status": capture.status(),
        }

    @app.get("/api/traffic/frames/{frame_id}")
    async def get_traffic_frame_detail(request: Request, frame_id: int):
        err = token_checker(request)
        if err:
            return err
        mark_viewer()
        frame = capture.frame_detail(frame_id)
        if frame is None:
            return JSONResponse(
                status_code=404,
                content={"error": "frame non trouvée"},
            )
        return frame

    @app.post("/api/traffic/clear")
    async def clear_traffic(request: Request):
        err = token_checker(request)
        if err:
            return err
        mark_viewer()
        count = capture.clear()
        debug(f"  [traffic] capture cleared ({count} frames)")
        return {"ok": True, "cleared": count}

    @app.get("/api/traffic/stats")
    async def get_traffic_stats(request: Request, window: float = 60.0):
        err = token_checker(request)
        if err:
            return err
        mark_viewer()
        return capture.stats(window=max(1.0, window))


__all__ = ["register_traffic_routes"]
