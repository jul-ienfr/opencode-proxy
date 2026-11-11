"""Log, debug toggle and debug-log routes extracted from ``dashboard/api.py``."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request


def register_debug_routes(
    app: FastAPI,
    *,
    settings: Any,
    log_lines_getter: Callable[[], Any],
    save_env: Callable[[dict[str, Any]], Any],
    debug_log_path: str,
    set_debug_log_file: Callable[[str], Any],
    debug: Callable[[str], Any],
) -> None:
    @app.get("/api/logs")
    async def get_logs(limit: int = 100, offset: int = 0):
        try:
            limit = max(1, min(int(limit or 100), 500))
        except (TypeError, ValueError):
            limit = 100
        try:
            offset = max(0, int(offset or 0))
        except (TypeError, ValueError):
            offset = 0
        lines = list(log_lines_getter())
        return {
            "logs": lines[offset : offset + limit],
            "total": len(lines),
            "has_more": offset + limit < len(lines),
        }

    @app.get("/api/debug")
    async def get_debug():
        return {"enabled": settings.DEBUG}

    @app.post("/api/debug")
    async def set_debug(request: Request):
        body = await request.json()
        enabled = body.get("enabled", False)
        settings.DEBUG = bool(enabled)
        save_env({"OPENCODE_DEBUG": "1" if enabled else "0"})
        if enabled:
            os.makedirs(os.path.dirname(debug_log_path), exist_ok=True)
            set_debug_log_file(debug_log_path)
        debug(f"Debug mode {'ENABLED' if enabled else 'DISABLED'} via API")
        return {"enabled": settings.DEBUG}

    @app.get("/api/debug/logs")
    async def get_debug_logs(limit: int = 500, offset: int = 0):
        try:
            try:
                limit = max(1, min(int(limit or 500), 500))
            except (TypeError, ValueError):
                limit = 500
            try:
                offset = max(0, int(offset or 0))
            except (TypeError, ValueError):
                offset = 0
            if not os.path.exists(debug_log_path):
                return {"logs": [], "total": 0, "has_more": False}

            stat = os.stat(debug_log_path)
            file_size = stat.st_size
            if file_size == 0:
                return {"logs": [], "total": 0, "has_more": False}

            max_full_read = 10 * 1024 * 1024
            signature = (file_size, stat.st_mtime, limit, offset)
            cached = getattr(get_debug_logs, "_cache", None)
            if cached and cached[0] == signature:
                return cached[1]

            def _read_all():
                with open(
                    debug_log_path,
                    encoding="utf-8",
                    errors="replace",
                ) as handle:
                    all_lines = handle.readlines()
                total = len(all_lines)
                reversed_lines = [
                    line.rstrip("\n") for line in reversed(all_lines)
                ]
                page = reversed_lines[offset : offset + limit]
                return page, total

            def _read_tail():
                with open(debug_log_path, "rb") as handle:
                    handle.seek(0, 2)
                    size = handle.tell()
                    read_size = min(size, 10 * 1024 * 1024)
                    handle.seek(size - read_size)
                    data = handle.read().decode("utf-8", errors="replace")

                lines = data.split("\n")
                if size > read_size and len(lines) > 1:
                    lines = lines[1:]
                if lines and lines[-1] == "":
                    lines = lines[:-1]

                total = len(lines)
                reversed_lines = list(reversed(lines))
                page = reversed_lines[offset : offset + limit]
                return page, total

            if file_size <= max_full_read:
                page, total = await asyncio.to_thread(_read_all)
            else:
                page, total = await asyncio.to_thread(_read_tail)

            response = {
                "logs": page,
                "total": total,
                "has_more": offset + limit < total,
            }
            get_debug_logs._cache = (signature, response)
            return response
        except Exception as error:
            return {
                "logs": [],
                "total": 0,
                "has_more": False,
                "error": str(error),
            }

    @app.delete("/api/debug/logs")
    async def clear_debug_logs():
        try:
            if os.path.exists(debug_log_path):
                with open(debug_log_path, "w", encoding="utf-8") as handle:
                    handle.truncate(0)
            debug("Debug log cleared via API")
            return {"status": "ok", "message": "Debug log cleared."}
        except Exception as error:
            return {"status": "error", "message": str(error)}


__all__ = ["register_debug_routes"]
