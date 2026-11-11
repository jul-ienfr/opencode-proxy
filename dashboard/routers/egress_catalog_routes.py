"""Read-only egress catalog routes.

Extracted from ``dashboard/api.py`` (slice S10).  Mutations (refresh, block,
source upsert) stay in the main module until their own slice.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI


def register_egress_catalog_routes(app: FastAPI, catalog_getter: Callable[[], Any]) -> None:
    @app.get("/api/egress/catalog")
    async def get_egress_catalog():
        runtime = catalog_getter()
        if runtime is None:
            return {"enabled": False, "providers": {}, "last_run": None}
        runs = runtime.store.list_runs(limit=1)
        return {
            "enabled": True,
            "providers": runtime.store.summary(),
            "rotation": runtime.rotation_plan() if hasattr(runtime, "rotation_plan") else {},
            "threat_intel": runtime.threat_intel.stats() if getattr(runtime, "threat_intel", None) else None,
            "last_run": runs[0] if runs else None,
        }

    @app.get("/api/egress/catalog/options")
    async def get_egress_catalog_options(
        provider: str = "",
        status: str = "",
        kind: str = "",
        limit: int = 100,
        offset: int = 0,
    ):
        runtime = catalog_getter()
        if runtime is None:
            return {"options": []}
        from dataclasses import asdict

        rows = runtime.store.list_options(
            provider=provider or None,
            status=status or None,
            kind=kind or None,
            limit=limit,
            offset=offset,
        )
        return {"options": [asdict(option) for option in rows]}

    @app.get("/api/egress/catalog/runs")
    async def get_egress_catalog_runs(provider: str = "", limit: int = 20):
        runtime = catalog_getter()
        if runtime is None:
            return {"runs": []}
        return {"runs": runtime.store.list_runs(provider=provider or None, limit=limit)}

    @app.get("/api/egress/catalog/observations")
    async def get_egress_catalog_observations(key: str = "", limit: int = 50):
        runtime = catalog_getter()
        if runtime is None:
            return {"observations": []}
        return {"observations": runtime.store.list_observations(key=key or None, limit=limit)}

    @app.get("/api/egress/catalog/sources")
    async def get_egress_catalog_sources():
        runtime = catalog_getter()
        if runtime is None:
            return {"sources": []}
        return {"sources": runtime.store.list_sources()}


__all__ = ["register_egress_catalog_routes"]
