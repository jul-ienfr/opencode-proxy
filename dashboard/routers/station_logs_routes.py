"""Per-station Docker log tail extracted from ``dashboard/api.py``."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse


def register_station_logs_routes(
    app: FastAPI,
    *,
    managers_getter: Callable[[], list[Any]],
    docker_runner: Callable[..., Any] | None = None,
    platform_name: str | None = None,
) -> None:
    runner = docker_runner or subprocess.run
    platform = sys.platform if platform_name is None else platform_name

    @app.get("/api/vpn/station/{station_id}/logs")
    async def get_station_logs(station_id: int, lines: int = 50):
        manager = next(
            (
                item
                for item in managers_getter()
                if getattr(item, "_station", None) == station_id
            ),
            None,
        )
        if manager is None:
            return JSONResponse(
                status_code=404,
                content={"error": f"station {station_id} introuvable"},
            )
        tail = max(10, min(int(lines or 50), 300))
        try:
            proc = await asyncio.to_thread(
                runner,
                ["docker", "logs", "--tail", str(tail), manager._docker_container],
                capture_output=True,
                timeout=8,
                creationflags=0x08000000 if platform == "win32" else 0,
            )
            text = (
                (proc.stdout or b"") + (proc.stderr or b"")
            ).decode("utf-8", errors="replace")[-20000:]
            return {"station": station_id, "tail": tail, "logs": text}
        except Exception as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "error": f"{type(exc).__name__}: {exc}",
                    "station": station_id,
                },
            )


__all__ = ["register_station_logs_routes"]
