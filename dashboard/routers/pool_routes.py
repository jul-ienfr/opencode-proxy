"""Pool and latency-engine status routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse


def _default_latency_engine() -> Any:
    import shared_state
    from latency_rotation import get_engine

    return getattr(shared_state, "latency_engine", None) or get_engine()


def build_latency_status(engine: Any) -> dict[str, Any]:
    now = time.time()
    cfg = engine.cfg
    cooldowns = []
    for (sid, ip), (kind, until) in sorted(engine._cooldowns.items()):
        remaining = max(0.0, until - now)
        if remaining > 0:
            cooldowns.append(
                {
                    "station": sid,
                    "ip": ip,
                    "kind": kind,
                    "remaining_s": round(remaining, 1),
                }
            )
    soft_history = sorted(f"{sid}|{ip}" for sid, ip in engine._soft_history)
    trackers = []
    for (sid, ip), tracker in sorted(engine._trackers.items()):
        snapshot = tracker.snapshot()
        trackers.append(
            {
                "station": sid,
                "ip": ip,
                "count": snapshot.count,
                "ewma_ms": (
                    None if snapshot.ewma_ms is None else round(snapshot.ewma_ms, 1)
                ),
                "p95_ms": (
                    None if snapshot.p95_ms is None else round(snapshot.p95_ms, 1)
                ),
                "consecutive_slow": snapshot.consecutive_slow,
            }
        )
    return {
        "enabled": bool(cfg.enabled),
        "config": {
            "slow_threshold_ms": cfg.slow_threshold_ms,
            "ewma_threshold_ms": cfg.ewma_threshold_ms,
            "p95_threshold_ms": cfg.p95_threshold_ms,
            "floor_ms": cfg.floor_ms,
            "consecutive_slow": cfg.consecutive_slow,
            "soft_cooldown_sec": cfg.soft_cooldown_sec,
            "hard_cooldown_sec": cfg.hard_cooldown_sec,
            "slow_threshold_ms_per_model": dict(cfg.per_model),
            "stream_metric": cfg.stream_metric,
        },
        "cooldowns": cooldowns,
        "soft_history": soft_history,
        "trackers": trackers,
    }


def register_pool_routes(
    app: FastAPI,
    *,
    pool_getter: Callable[[], Any],
    latency_engine_getter: Callable[[], Any] | None = None,
) -> None:
    @app.get("/api/pool-status")
    async def get_pool_status():
        pool = pool_getter()
        if pool is None:
            return JSONResponse(
                status_code=503,
                content={"error": "free_ip_pool non initialisé", "proxy_mode": "none"},
            )
        return pool.get_status()

    @app.get("/api/latency-status")
    async def get_latency_status():
        try:
            engine = (latency_engine_getter or _default_latency_engine)()
        except Exception as exc:
            return {"enabled": False, "error": f"engine unavailable: {exc}"}
        return build_latency_status(engine)


__all__ = ["build_latency_status", "register_pool_routes"]
