"""Server-sent events route extracted from ``dashboard/api.py``."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse


def register_event_routes(
    app: FastAPI,
    *,
    event_manager_getter: Callable[[], Any],
    debug_logger: Callable[[str], Any] | None = None,
) -> None:
    @app.get("/api/events")
    async def event_stream(request: Request):
        manager = event_manager_getter()
        try:
            manager.bind_loop(asyncio.get_running_loop())
        except Exception:
            pass
        try:
            queue = await manager.subscribe()
        except RuntimeError as exc:
            if "limit" in str(exc).lower():
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "too_many_subscribers",
                        "message": str(exc),
                    },
                )
            raise
        if debug_logger is not None:
            try:
                debug_logger("  [sse] new SSE subscriber")
            except Exception:
                pass

        async def event_generator():
            try:
                yield (
                    "event: connected\ndata: "
                    f"{json.dumps({'status': 'ok'})}\n\n"
                )
                while True:
                    try:
                        payload = await asyncio.wait_for(queue.get(), timeout=30)
                        yield payload
                    except TimeoutError:
                        if await request.is_disconnected():
                            break
                        yield "event: ping\ndata: {}\n\n"
            except (asyncio.CancelledError, RuntimeError):
                pass
            finally:
                await manager.unsubscribe(queue)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )


__all__ = ["register_event_routes"]
