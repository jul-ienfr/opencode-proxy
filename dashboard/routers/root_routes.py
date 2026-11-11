"""Dashboard root/index route extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.responses import Response


def register_root_route(
    app: FastAPI,
    *,
    index_loader: Callable[[], bytes | str | None],
) -> None:
    @app.get("/")
    async def root():
        data = index_loader()
        if data is not None:
            return Response(
                content=data,
                media_type="text/html; charset=utf-8",
            )
        return Response(
            content=(
                "Dashboard UI introuvable : lancez `npm run build` dans "
                "dashboard-ui/ (sortie : static/dist/)."
            ),
            status_code=503,
            media_type="text/plain; charset=utf-8",
        )


__all__ = ["register_root_route"]
