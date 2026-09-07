"""server.accesslog — middleware access log pur ASGI (Phase 2 refonte).

Déplacement PUR depuis ``opencode.py`` (§ « Access Log Middleware »). AUCUN
import du projet : ``log_fn`` injecté (hôte : ``dashboard.display.log``).
"""

from __future__ import annotations

import time
from collections.abc import Callable


def _noop_log(*args, **kwargs) -> None:
    return None


class AccessLogMiddleware:
    """Pure-ASGI access log — zero copy, streaming-safe, no BaseHTTPMiddleware buffering."""

    _SKIP_PREFIXES = ("/api/", "/static/", "/health")

    def __init__(self, app, *, log_fn: Callable[..., None] = _noop_log):
        self.app = app
        self._log_fn = log_fn

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health" or any(path.startswith(p) for p in self._SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        client_ip = client[0] if client else "?"
        method = scope.get("method", "?")
        start = time.monotonic()
        status_holder = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["status"] = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception:
            elapsed_ms = (time.monotonic() - start) * 1000
            self._log_fn(f"{method} {path} 499 {elapsed_ms:.0f}ms {client_ip}")
            raise
        elapsed_ms = (time.monotonic() - start) * 1000
        status = status_holder.get("status", 0)
        self._log_fn(f"{method} {path} {status} {elapsed_ms:.0f}ms {client_ip}")


__all__ = ["AccessLogMiddleware"]
