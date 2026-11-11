"""Workspace quota snapshot route with stale-while-revalidate semantics."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse


def register_quota_routes(
    app: FastAPI,
    *,
    snapshot_getter: Callable[[], Any],
    cache_state_getter: Callable[[], tuple[str, float]],
    max_age_s: float,
    stale_s: float,
) -> None:
    @app.get("/api/quotas")
    async def get_quotas():
        data = await snapshot_getter()
        state, age = cache_state_getter()
        age_header = "unknown" if age == float("inf") else f"{age:.1f}"
        return JSONResponse(
            content=data,
            headers={
                "Cache-Control": (
                    f"private, max-age={int(max_age_s)}, "
                    f"stale-while-revalidate={int(stale_s)}"
                ),
                "X-Cache": {"fresh": "HIT", "stale": "STALE", "miss": "MISS"}[state],
                "X-Cache-Age": age_header,
            },
        )


__all__ = ["register_quota_routes"]
