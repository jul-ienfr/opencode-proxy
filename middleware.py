"""
Middleware: rate limiting (token bucket) + circuit breaker.
Extracted from opencode.py (P3.10).
"""

import asyncio
import json
import os
import time

from config import yaml_get
from traffic_capture import (
    capture as _traffic_capture,  # noqa: E402  # configure after class defs, import kept top-level
)

try:
    from dashboard.display import debug as _debug
    from dashboard.display import log as _log
except ImportError:  # pragma: no cover - fallback for tests without dashboard

    def _debug(msg: str) -> None:  # type: ignore[no-redef]
        pass

    def _log(msg: str) -> None:  # type: ignore[no-redef]
        print(msg)


def _json_dumps(obj) -> bytes:
    try:
        import orjson as _orjson

        return _orjson.dumps(obj)
    except ImportError:
        return json.dumps(obj, ensure_ascii=False).encode()


def _json_loads(data: bytes | str):
    try:
        import orjson as _orjson

        if isinstance(data, str):
            data = data.encode()
        return _orjson.loads(data)
    except ImportError:
        if isinstance(data, bytes):
            data = data.decode()
        return json.loads(data)

RATE_LIMIT_RPS = float(os.environ.get("RATE_LIMIT_RPS", str(yaml_get("rate_limit", "rps", 50))))
RATE_LIMIT_BURST = float(
    os.environ.get("RATE_LIMIT_BURST", str(yaml_get("rate_limit", "burst", 100)))
)
_STALE_BUCKET_TTL = yaml_get(
    "rate_limit", "stale_ttl", 300
)  # seconds — remove buckets inactive for 5 min


class _Bucket:
    """Token bucket for a single client IP — lock-free (single-threaded event loop)."""

    __slots__ = ("tokens", "last_refill", "max_tokens", "refill_rate", "last_access")

    def __init__(self, rate: float, burst: float):
        self.tokens = burst
        self.last_refill = time.monotonic()
        self.max_tokens = burst
        self.refill_rate = rate
        self.last_access = time.monotonic()

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
        _debug(f"  [ratelimit] DENIED (tokens={self.tokens:.2f}, retry_after={wait:.2f}s)")
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


class RateLimitMiddleware:
    """Pure-ASGI token bucket rate limiter — zero copy, streaming-safe.

    Replaces the old BaseHTTPMiddleware version which buffered response bodies
    and added ~8ms per request. This version is a raw ASGI middleware (like
    TrafficCaptureMiddleware) — no body copy, no BaseHTTPMiddleware overhead.
    """

    _SKIP_PREFIXES = ("/api/", "/static/", "/health")

    def __init__(self, app, rate: float = RATE_LIMIT_RPS, burst: float = RATE_LIMIT_BURST):
        self.app = app
        self._rate = rate
        self._burst = burst
        self._buckets: dict[str, _Bucket] = {}
        self._cleanup_task: asyncio.Task | None = None

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
            bucket = _Bucket(self._rate, self._burst)
            self._buckets[ip] = bucket
        allowed, retry_after = bucket.consume_sync()
        if allowed:
            await self.app(scope, receive, send)
            return
        retry_after_int = max(1, int(retry_after) + 1)
        body = _json_dumps({"error": "Rate limit exceeded. Try again shortly."})
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
            stale = [
                ip for ip, b in self._buckets.items() if now - b.last_access > _STALE_BUCKET_TTL
            ]
            for ip in stale:
                self._buckets.pop(ip, None)
            if stale:
                _debug(
                    f"  [ratelimit] cleanup: {len(stale)} stale buckets removed, {len(self._buckets)} active"
                )


# ── Access Log Middleware ─────────────────────────────────────────


class AccessLogMiddleware:
    """Pure-ASGI access log — zero copy, streaming-safe, no BaseHTTPMiddleware buffering."""

    _SKIP_PREFIXES = ("/api/", "/static/", "/health")

    def __init__(self, app):
        self.app = app

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
            _log(f"{method} {path} 499 {elapsed_ms:.0f}ms {client_ip}")
            raise
        elapsed_ms = (time.monotonic() - start) * 1000
        status = status_holder.get("status", 0)
        _log(f"{method} {path} {status} {elapsed_ms:.0f}ms {client_ip}")


# ── Traffic Capture (Wireshark-like raw request view) ──────────────
# Outermost middleware: records every client request — raw body bytes,
# headers, timing, tempo, abrupt disconnects (RST) — into a bounded
# ring buffer served by /api/traffic/*. Excludes /static, /health and
# itself. Configurable via the `traffic:` block in config.yaml.

_traffic_capture.configure(
    enabled=bool(yaml_get("traffic", "enabled", True)),
    max_frames=int(yaml_get("traffic", "max_frames", 500)),
    body_cap=int(yaml_get("traffic", "body_cap", 131072)),
    max_bytes=int(yaml_get("traffic", "max_bytes", 33554432)),
)


# ── Circuit Breaker (per-endpoint) ──────────────────────────────

_CB_FAILURE_THRESHOLD = yaml_get(
    "circuit_breaker", "failure_threshold", 5
)  # trips open after N consecutive failures
_CB_RECOVERY_TIMEOUT = float(
    yaml_get("circuit_breaker", "recovery_timeout", 60)
)  # seconds before half-open test


class _CircuitBreaker:
    """Per-endpoint circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED."""

    __slots__ = (
        "failures",
        "state",
        "opened_at",
        "total_requests",
        "total_failures",
        "last_failure_time",
        "created_at",
    )

    def __init__(self):
        self.failures = 0
        self.state = "closed"  # closed | open | half_open
        self.opened_at = 0.0
        self.total_requests = 0
        self.total_failures = 0
        self.last_failure_time = 0.0
        self.created_at = time.monotonic()

    def record_success(self):
        old_state = self.state
        self.failures = 0
        self.total_requests += 1
        self.state = "closed"
        if old_state != "closed":
            _debug(f"  [cb] state {old_state} → closed (success #{self.total_requests})")

    def record_failure(self):
        old_state = self.state
        self.failures += 1
        self.total_failures += 1
        self.total_requests += 1
        self.last_failure_time = time.monotonic()
        if self.state == "half_open":
            # Failure during half-open test → immediately reopen
            self.state = "open"
            self.opened_at = time.monotonic()
            _debug("  [cb] half_open → open (test request failed)")
        elif self.failures >= _CB_FAILURE_THRESHOLD:
            self.state = "open"
            self.opened_at = time.monotonic()
            _debug(f"  [cb] {old_state} → open (failures={self.failures}/{_CB_FAILURE_THRESHOLD})")

    def should_allow(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.monotonic() - self.opened_at >= _CB_RECOVERY_TIMEOUT:
                self.state = "half_open"
                _debug("  [cb] open → half_open (cooldown expired)")
                return True  # allow one test request
            remaining = _CB_RECOVERY_TIMEOUT - (time.monotonic() - self.opened_at)
            _debug(f"  [cb] DENIED (state=open, cooldown={remaining:.0f}s remaining)")
            return False
        # half_open: allow one request through
        return True

    def get_status(self) -> dict:
        uptime = time.monotonic() - self.created_at
        return {
            "state": self.state,
            "failures": self.failures,
            "total_requests": self.total_requests,
            "total_failures": self.total_failures,
            "last_failure_time": self.last_failure_time,
            "uptime_seconds": round(uptime, 1),
        }


_circuit_breakers: dict[str, _CircuitBreaker] = {}


def _get_cb(endpoint: str) -> _CircuitBreaker:
    cb = _circuit_breakers.get(endpoint)
    if cb is None:
        cb = _CircuitBreaker()
        _circuit_breakers[endpoint] = cb
        _debug(f"  [cb] created circuit breaker for {endpoint}")
    return cb


def _cb_should_allow(endpoint: str) -> bool:
    """Check if the circuit breaker allows a request to this endpoint."""
    return _get_cb(endpoint).should_allow()


def _cb_record_success(endpoint: str):
    """Record a successful request to this endpoint."""
    _get_cb(endpoint).record_success()


def _cb_record_failure(endpoint: str):
    """Record a failed request to this endpoint."""
    _get_cb(endpoint).record_failure()


# ── HTTP helpers with circuit breaker ────────────────────────────


class CircuitOpenError(Exception):
    """Raised when the circuit breaker is open for an endpoint."""

    pass


class UpstreamError(Exception):
    """Raised when an upstream HTTP request fails (connection, timeout, etc.)."""

    def __init__(self, message: str, status_code: int = 502, original: Exception = None):
        super().__init__(message)
        self.status_code = status_code
        self.original = original


class AllKeysPausedError(Exception):
    """Raised when all API keys are paused and no request can be made."""

    def __init__(self, retry_after: float):
        super().__init__(f"All API keys paused, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


# ── Standardized error response helpers ──
