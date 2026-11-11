"""Route table projection extracted from ``dashboard/api.py``."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request


def register_route_table_routes(
    app: FastAPI,
    *,
    settings: Any,
) -> None:
    @app.get("/api/routes")
    async def get_routes(
        request: Request,
        q: str = None,
        geo_status: str = None,
        limit: int = 50,
        offset: int = 0,
    ):
        limit = max(1, min(200, int(limit or 50)))
        offset = max(0, int(offset or 0))
        items = []
        for key, route in settings.ROUTES.items():
            try:
                geo = settings.resolve_geo(route)
            except Exception:
                geo = {
                    "effective_allowed": set(),
                    "mode": "strict",
                    "require_vpn": False,
                    "geo_status": "ok",
                }
            status = str(geo.get("geo_status", "ok"))
            if geo_status and status != geo_status:
                continue
            if (
                q
                and q.lower() not in key.lower()
                and q.lower() not in str(route.get("model", "")).lower()
            ):
                continue
            effective = geo.get("effective_allowed", set())
            items.append(
                {
                    "key": key,
                    "match": route.get("match", []),
                    "model": route.get("model", ""),
                    "thinking": route.get("thinking"),
                    "geo": route.get("geo"),
                    "geo_status": status,
                    "effective_allowed": sorted(effective)
                    if isinstance(effective, set)
                    else [],
                    "mode": geo.get("mode", "strict"),
                    "require_vpn": bool(geo.get("require_vpn", False)),
                }
            )
        total = len(items)
        page = items[offset : offset + limit]
        return {
            "routes": page,
            "total": total,
            "limit": limit,
            "offset": offset,
            "has_more": offset + limit < total,
        }


__all__ = ["register_route_table_routes"]
