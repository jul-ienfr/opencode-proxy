"""Cost/savings route extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI

from dashboard.routers import read_models


def register_cost_routes(
    app: FastAPI,
    *,
    build_where: Callable[..., tuple[str, list[Any]]],
    db_read_sync: Callable[[Callable[[Any], Any]], Any],
    compute_costs: Callable[[list[dict[str, Any]], dict[str, Any]], dict[str, Any]],
    pricing_getter: Callable[[], dict[str, Any]],
) -> None:
    @app.get("/api/costs")
    async def get_costs(
        from_date: str = None,
        to_date: str = None,
        station: int = None,
    ):
        where, params = build_where(from_date, to_date, station=station)

        def _query(db):
            return read_models.cost_rows(db, where=where, params=params)

        rows = await db_read_sync(_query)
        result = compute_costs(rows, pricing_getter())
        result["window"] = {
            "from": from_date,
            "to": to_date,
            "station": station,
        }
        return result


__all__ = ["register_cost_routes"]
