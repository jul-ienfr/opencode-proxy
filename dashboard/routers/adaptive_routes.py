"""Adaptive egress weight route extracted from ``dashboard/api.py``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI


def register_adaptive_routes(
    app: FastAPI,
    *,
    pool_getter: Callable[[], Any],
) -> None:
    @app.get("/api/egress/adaptive")
    async def get_egress_adaptive():
        pool = pool_getter()
        config = (
            dict(getattr(pool, "_egress_adaptive", None) or {})
            if pool is not None
            else {}
        )
        available: list[str] = []
        if pool is not None and hasattr(pool, "_available_egress_providers"):
            model = getattr(pool, "_last_model", None)
            try:
                available = list(pool._available_egress_providers(model=model))
            except TypeError:
                try:
                    available = list(pool._available_egress_providers())
                except Exception:
                    available = []
            except Exception:
                available = []
        if not available:
            return {
                "enabled": bool(config.get("enabled")),
                "available": [],
                "base": {},
                "health": {},
                "effective": {},
            }
        from free.adaptive_weights import adaptive_weights
        from free.auto_egress import normalize_weights

        base = normalize_weights(
            getattr(pool, "_egress_weights", None) or {},
            available,
        )
        health = {}
        if hasattr(pool, "_adaptive_health"):
            try:
                health = pool._adaptive_health(available)
            except Exception:
                health = {}
        effective = base
        if config.get("enabled"):
            effective = adaptive_weights(
                base,
                health,
                min_samples=int(config.get("min_samples", 3) or 3),
                recent_429_penalty=float(
                    config.get("recent_429_penalty", 0.25) or 0.25
                ),
                min_weight_pct=int(config.get("min_weight_pct", 2) or 0),
            )
        return {
            "enabled": bool(config.get("enabled")),
            "available": available,
            "base": base,
            "health": health,
            "effective": effective,
        }


__all__ = ["register_adaptive_routes"]
