"""server.throttle — rate-limit token bucket + garde taille corps (Phase 2 refonte).

Déplacement PUR depuis ``opencode.py`` (§ « Rate Limiting (token bucket,
per-IP) » et ``_RequestBodyLimitMiddleware``). AUCUN import du projet au
top-level :

* ``debug_fn`` / ``dumps_fn`` injectés (hôte : ``dashboard.display.debug``,
  fast-path orjson) ;
* ``rate`` / ``burst`` / ``stale_ttl`` passés par l'hôte (lus depuis
  ``RATE_LIMIT_*`` / ``config.yaml`` côté ``opencode.py``) ;
* ``trust.send_json`` importé en LAZY dans le hot path 413 (comme avant —
  ``trust`` est un module standalone sans cycle).
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable

# Défauts — l'hôte (opencode.py) passe TOUJOURS les valeurs lues depuis
# RATE_LIMIT_RPS / RATE_LIMIT_BURST / yaml rate_limit.* ; ces constantes ne
# servent qu'au standalone / tests directs.
DEFAULT_RATE = 50.0
DEFAULT_BURST = 100.0
DEFAULT_STALE_BUCKET_TTL = 300  # seconds — remove buckets inactive for 5 min


def _noop_debug(*args, **kwargs) -> None:
    return None


def _default_dumps(obj, **kw) -> bytes:
    return json.dumps(obj, ensure_ascii=False, default=str).encode()


class Bucket:
    """Token bucket for a single client IP — lock-free (single-threaded event loop)."""

    __slots__ = ("tokens", "last_refill", "max_tokens", "refill_rate", "last_access", "_debug_fn")

    def __init__(
        self,
        rate: float,
        burst: float,
        *,
        debug_fn: Callable[..., None] = _noop_debug,
    ):
        self.tokens = burst
        self.last_refill = time.monotonic()
        self.max_tokens = burst
        self.refill_rate = rate
        self.last_access = time.monotonic()
        self._debug_fn = debug_fn

    async def consume(self) -> tuple[bool, float]:
        """Try to consume one token. Returns (allowed, retry_after). Lock-free (no per-bucket lock)."""
        now = time.monotonic()
        self.last_access = now
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0
        wait = (1.0 - self.tokens) / self.refill_rate
        self._debug_fn(f"  [ratelimit] DENIED (tokens={self.tokens:.2f}, retry_after={wait:.2f}s)")
        return False, wait

    # Sync alias for pure-ASGI hot path (avoids await overhead)
    def consume_sync(self) -> tuple[bool, float]:
        now = time.monotonic()
        self.last_access = now
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True, 0.0
        wait = (1.0 - self.tokens) / self.refill_rate
        return False, wait


# Alias historique (opencode._Bucket, test_proxy.py) — PAS une sous-classe.
_Bucket = Bucket


class RateLimitMiddleware:
    """Pure-ASGI token bucket rate limiter — zero copy, streaming-safe.

    Replaces the old BaseHTTPMiddleware version which buffered response bodies
    and added ~8ms per request. This version is a raw ASGI middleware (like
    TrafficCaptureMiddleware) — no body copy, no BaseHTTPMiddleware overhead.
    """

    _SKIP_PREFIXES = ("/api/", "/static/", "/health")

    def __init__(
        self,
        app,
        rate: float = DEFAULT_RATE,
        burst: float = DEFAULT_BURST,
        *,
        stale_ttl: float = DEFAULT_STALE_BUCKET_TTL,
        debug_fn: Callable[..., None] = _noop_debug,
        dumps_fn: Callable[..., bytes] = _default_dumps,
    ):
        self.app = app
        self._rate = rate
        self._burst = burst
        self._stale_ttl = stale_ttl
        self._debug_fn = debug_fn
        self._dumps_fn = dumps_fn
        self._buckets: dict[str, Bucket] = {}
        self._cleanup_task: asyncio.Task | None = None

    def _new_bucket(self) -> Bucket:
        return Bucket(self._rate, self._burst, debug_fn=self._debug_fn)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path == "/health" or any(path.startswith(p) for p in self._SKIP_PREFIXES):
            await self.app(scope, receive, send)
            return
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        client = scope.get("client")
        ip = client[0] if client else "unknown"
        bucket = self._buckets.get(ip)
        if bucket is None:
            bucket = self._new_bucket()
            self._buckets[ip] = bucket
        allowed, retry_after = bucket.consume_sync()
        if allowed:
            await self.app(scope, receive, send)
            return
        retry_after_int = max(1, int(retry_after) + 1)
        body = self._dumps_fn({"error": "Limite de débit dépassée. Veuillez réessayer sous peu."})
        headers = [
            (b"content-type", b"application/json"),
            (b"retry-after", str(retry_after_int).encode()),
        ]
        await send({"type": "http.response.start", "status": 503, "headers": headers})
        await send({"type": "http.response.body", "body": body})

    async def _cleanup_loop(self):
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            stale = [ip for ip, b in self._buckets.items() if now - b.last_access > self._stale_ttl]
            for ip in stale:
                self._buckets.pop(ip, None)
            if stale:
                self._debug_fn(
                    f"  [ratelimit] cleanup: {len(stale)} stale buckets removed, {len(self._buckets)} active"
                )


class RequestBodyLimitMiddleware:
    """Pure ASGI — rejette 413 avant bufferisation si Content-Length dépasse
    la limite configurée (`upstream.max_body_size`, défaut 10 Mo)."""

    def __init__(self, app, limit_getter):
        self.app = app
        self._limit_getter = limit_getter

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http" and str(scope.get("method", "")).upper() in (
            "POST",
            "PUT",
            "PATCH",
        ):
            try:
                limit = int(self._limit_getter() or 0)
            except Exception:
                limit = 0
            if limit > 0:
                for raw_key, raw_val in scope.get("headers") or ():
                    if bytes(raw_key).lower() == b"content-length":
                        try:
                            if int(raw_val) > limit:
                                from trust import send_json as _sj

                                await _sj(
                                    send,
                                    413,
                                    {
                                        "error": "payload_too_large",
                                        "message": f"Body > {limit} octets (upstream.max_body_size).",
                                    },
                                )
                                return
                        except ValueError:
                            pass
                        break
        await self.app(scope, receive, send)


# Alias historique (opencode._RequestBodyLimitMiddleware).
_RequestBodyLimitMiddleware = RequestBodyLimitMiddleware

__all__ = [
    "DEFAULT_BURST",
    "DEFAULT_RATE",
    "DEFAULT_STALE_BUCKET_TTL",
    "Bucket",
    "RateLimitMiddleware",
    "RequestBodyLimitMiddleware",
    "_Bucket",
    "_RequestBodyLimitMiddleware",
]
