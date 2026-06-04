"""
Claude Code Proxy → opencode.ai
Convert Anthropic /v1/messages ↔ OpenAI chat/completions
"""

import json
import uuid
import time
import logging
import os
import sqlite3
import threading
import traceback
import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse as StarletteJSONResponse
from starlette.requests import ClientDisconnect

from config import API_KEY, PROXY, MODELS, ROUTES, get_model_config, HOST, PORT, WEB_PORT, DISABLE_MAPPING, API_KEYS, API_KEY_ROUTING, CUSTOM_ROUTES, maybe_reload_custom_routes, CACHE_MIN_PROMPT_SIZE, DEBUG

import itertools

# ── API key routing ──
_key_cycle = None
_key_cycle_keys = []
_key_failover_index = 0
_key_cycle_lock = threading.Lock()

def _get_enabled_keys() -> list[dict]:
    return [k for k in API_KEYS if k.get("enabled", True)]

def get_next_api_key() -> dict:
    global _key_cycle, _key_cycle_keys, _key_failover_index
    if not API_KEYS:
        _debug(f"  [apikey] no API_KEYS configured, falling back to .env key")
        return {"api_key": API_KEY}
    enabled = _get_enabled_keys()
    if not enabled:
        _debug(f"  [apikey] no enabled keys, falling back to .env key")
        return {"api_key": API_KEY}
    if len(enabled) == 1:
        _debug(f"  [apikey] single key: alias={enabled[0].get('alias','?')}")
        return enabled[0]
    if API_KEY_ROUTING == "failover":
        for i in range(len(API_KEYS)):
            idx = (_key_failover_index + i) % len(API_KEYS)
            if API_KEYS[idx].get("enabled", True):
                _debug(f"  [apikey] failover selected alias={API_KEYS[idx].get('alias','?')} (idx={idx})")
                return API_KEYS[idx]
        _debug(f"  [apikey] failover exhausted, falling back to .env key")
        return {"api_key": API_KEY}
    with _key_cycle_lock:
        current_ids = [k.get("api_key") for k in enabled]
        if _key_cycle is None or _key_cycle_keys != current_ids:
            _key_cycle = itertools.cycle(enabled)
            _key_cycle_keys = current_ids
            _debug(f"  [apikey] round-robin cycle rebuilt: {len(enabled)} keys")
        selected = next(_key_cycle)
        _debug(f"  [apikey] round-robin selected alias={selected.get('alias','?')}")
        return selected

def _find_alternative_key(failed_key: str) -> dict | None:
    """Return the first enabled key different from failed_key, or None."""
    for k in API_KEYS:
        if k.get("api_key") != failed_key and k.get("enabled", True):
            _debug(f"  [apikey] alternative key found alias={k.get('alias','?')}")
            return k
    _debug(f"  [apikey] no alternative key for {failed_key[:8]}...")
    return None

def advance_failover():
    global _key_failover_index
    if API_KEYS and API_KEY_ROUTING == "failover":
        _key_failover_index = (_key_failover_index + 1) % len(API_KEYS)
        _debug(f"  [apikey] failover index advanced to {_key_failover_index}")

_key_alias_cache: dict[str, str] = {}


def _rebuild_key_cache():
    """Rebuild the API key → alias lookup dict."""
    global _key_alias_cache
    _key_alias_cache = {k["api_key"]: k.get("alias", "") or "" for k in API_KEYS if k.get("api_key")}


def _alias_for_key(api_key: str) -> str:
    """Look up the alias for a given API key. O(1) via dict cache."""
    return _key_alias_cache.get(api_key, "")

def _key_from_headers(headers: dict, protocol: str) -> str:
    """Extract the API key from request headers."""
    if protocol == "anthropic":
        return headers.get("x-api-key", "")
    return headers.get("Authorization", "").replace("Bearer ", "")

def _get_auth_headers(protocol: str, entry: dict | None = None) -> dict:
    if entry is None:
        entry = get_next_api_key()
    ak = entry.get("api_key", API_KEY)
    if protocol == "anthropic":
        return {"x-api-key": ak, "Content-Type": "application/json",
                "anthropic-version": "2023-06-01"}
    return {"Authorization": f"Bearer {ak}", "Content-Type": "application/json"}


try:
    import tiktoken
    _encoding = tiktoken.get_encoding("cl100k_base")
except Exception:
    _encoding = None

# Fast monotonic ID generator — avoids /dev/urandom syscall of uuid4()
# Used for request IDs (logging/DB keys only, not security-critical)
_id_counter = itertools.count()
def _fast_id(prefix: str = "id") -> str:
    """Generate a fast, unique-enough ID within this process. ~0.01ms vs ~0.5ms for uuid4."""
    return f"{prefix}_{time.monotonic_ns():x}-{next(_id_counter):x}"

from dashboard import register_dashboard
from dashboard import start_quota_fetcher
from dashboard.display import log as _log, debug as _debug, set_debug_log_file, RichLogHandler, run_terminal_loop
from dashboard.events import get_event_manager

# Call after all imports are resolved (requires _debug for logging)
_rebuild_key_cache()
_debug(f"  [apikey] rebuilt alias cache: {len(_key_alias_cache)} keys")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Debug log file setup
if DEBUG:
    set_debug_log_file(os.path.join(LOG_DIR, "debug.log"))
    _debug("Debug mode enabled — full request/response logging active")

# Request body size limit (10 MB)
MAX_BODY_SIZE = 10 * 1024 * 1024

# SQLite setup — synchronous connection, all DB ops run in thread pool
_db_path = os.path.join(LOG_DIR, "requests.db")
_conn = sqlite3.connect(_db_path, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute("PRAGMA busy_timeout=5000")
_conn.execute("PRAGMA synchronous=NORMAL")       # WAL+NORMAL: safe crash-resilient, 50-90% fewer fsyncs
_conn.execute("PRAGMA cache_size=-64000")        # 64MB page cache (default: 2MB)
_conn.execute("PRAGMA temp_store=MEMORY")        # temp tables in RAM
_conn.execute("PRAGMA mmap_size=268435456")      # memory-mapped I/O for 256MB
_conn.execute("""
    CREATE TABLE IF NOT EXISTS requests (
        id TEXT PRIMARY KEY,
        timestamp TEXT NOT NULL,
        model TEXT NOT NULL,
        original_model TEXT,
        duration_ms INTEGER,
        tokens_input INTEGER,
        tokens_output INTEGER,
        tokens_cache INTEGER,
        success INTEGER,
        error TEXT,
        protocol TEXT,
        is_stream INTEGER,
        thinking TEXT,
        effort TEXT
    )
""")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON requests(timestamp)")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_model ON requests(model)")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_success ON requests(success)")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_account ON requests(account_alias)")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_ts_model ON requests(timestamp, model)")
for col, default in [("protocol", "NULL"), ("is_stream", "0"), ("thinking", "NULL"), ("effort", "NULL"), ("client_ip", "NULL"), ("account_alias", "NULL"), ("tools", "NULL"), ("tools_used", "NULL")]:
    try:
        _conn.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT DEFAULT {default}")
    except Exception:
        pass
_conn.commit()
_debug(f"  [db] SQLite connection established: {_db_path}")


_db_pending_inserts = 0
_db_last_commit = time.monotonic()
_DB_COMMIT_INTERVAL = 5.0   # seconds between periodic commits
_DB_COMMIT_BATCH = 10       # force commit after N inserts
_db_commit_lock = threading.Lock()

def _db_flush():
    """Force a pending commit. Called periodically and before shutdown."""
    global _db_pending_inserts, _db_last_commit
    with _db_commit_lock:
        if _db_pending_inserts > 0:
            _conn.commit()
            _debug(f"  [db] _db_flush: committed {_db_pending_inserts} pending inserts")
            _db_pending_inserts = 0
            _db_last_commit = time.monotonic()


def _db_insert_sync(req_id, timestamp, model, original_model, duration_ms,
                    tokens_input, tokens_output, tokens_cache, success, error,
                    protocol, is_stream, thinking, effort, client_ip, account_alias,
                    tools_json, tools_used_json):
    """Synchronous DB insert — called via asyncio.to_thread().

    Batches commits: accumulates INSERTs and commits every _DB_COMMIT_BATCH
    inserts or every _DB_COMMIT_INTERVAL seconds, whichever comes first.
    Reduces fsync overhead under load (50 req/s → ~1 commit/s instead of 50).
    """
    global _db_pending_inserts, _db_last_commit
    t0 = time.monotonic()
    _conn.execute("""
        INSERT OR REPLACE INTO requests (id, timestamp, model, original_model, duration_ms,
            tokens_input, tokens_output, tokens_cache, success, error,
            protocol, is_stream, thinking, effort, client_ip, account_alias, tools, tools_used)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (req_id, timestamp, model, original_model, duration_ms,
          tokens_input, tokens_output, tokens_cache, 1 if success else 0, error,
          protocol, 1 if is_stream else 0, thinking, effort,
          client_ip, account_alias, tools_json, tools_used_json))
    # Batch commit logic
    with _db_commit_lock:
        _db_pending_inserts += 1
        now = time.monotonic()
        elapsed = now - _db_last_commit
        if _db_pending_inserts >= _DB_COMMIT_BATCH or elapsed >= _DB_COMMIT_INTERVAL:
            _conn.commit()
            _debug(f"  [db] _db_insert_sync: batch-committed {_db_pending_inserts} inserts ({elapsed:.1f}s) in {(time.monotonic()-t0)*1000:.1f}ms")
            _db_pending_inserts = 0
            _db_last_commit = now
        else:
            _debug(f"  [db] _db_insert_sync: queued req_id={req_id} (pending={_db_pending_inserts}, {elapsed:.1f}s since last commit)")


async def _save_request(req_id, model, original_model, duration_ms,
	                  tokens_input, tokens_output, tokens_cache, success=True, error=None,
	                  protocol=None, is_stream=False, thinking=None, effort=None,
	                  client_ip=None, account_alias=None, tools=None, tools_used=None):
    tools_json = json.dumps(tools) if tools else "[]"
    tools_used_json = json.dumps(tools_used) if tools_used else "[]"
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    await asyncio.to_thread(
        _db_insert_sync, req_id, timestamp, model, original_model, duration_ms,
        tokens_input, tokens_output, tokens_cache, success, error,
        protocol, is_stream, thinking, effort, client_ip, account_alias,
        tools_json, tools_used_json
    )
    _debug(f"  [db] _save_request: saved req_id={req_id} model={model} success={success}")
    # Notify dashboard SSE clients about the update
    try:
        get_event_manager().publish("stats_updated", {"time": timestamp})
    except Exception:
        pass


# Token usage tracking (in-memory, restored from SQLite on startup)
_token_usage = {model: {"input": 0, "output": 0, "cache": 0} for model in MODELS}
_token_lock = threading.Lock()

def _restore_token_counters():
    """Restore in-memory token counters from SQLite on startup."""
    try:
        rows = _conn.execute(
            "SELECT model, COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0),"
            "       COALESCE(SUM(tokens_cache), 0)"
            " FROM requests GROUP BY model"
        ).fetchall()
        total_in, total_out, total_cache = 0, 0, 0
        for row in rows:
            model = row["model"]
            if model in _token_usage:
                _token_usage[model]["input"] = row[1]
                _token_usage[model]["output"] = row[2]
                _token_usage[model]["cache"] = row[3]
                _debug(f"  [db] restored {model}: in={row[1]} out={row[2]} cache={row[3]}")
                total_in += row[1]
                total_out += row[2]
                total_cache += row[3]
        _debug(f"  [db] token counters restored: {len(rows)} models, total in={total_in} out={total_out} cache={total_cache}")
        _log(f"  Restored token counters for {len(rows)} models from database")
    except Exception as e:
        _debug(f"  [db] restore token counters FAILED: {type(e).__name__}: {e}")
        _log(f"  Warning: Could not restore token counters: {e}")

_restore_token_counters()


def _wal_checkpoint():
    """Run WAL checkpoint to prevent WAL file growth."""
    try:
        _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        _debug("  [db] WAL checkpoint completed successfully")
    except Exception as e:
        _debug(f"  [db] WAL checkpoint FAILED: {type(e).__name__}: {e}")

# Shared HTTP client (reused across requests) with connection pooling
_transport = httpx.AsyncHTTPTransport(
    proxy=PROXY,
    limits=httpx.Limits(max_connections=200, max_keepalive_connections=50, keepalive_expiry=120),
) if PROXY else httpx.AsyncHTTPTransport(
    limits=httpx.Limits(max_connections=200, max_keepalive_connections=50, keepalive_expiry=120),
)
_client = httpx.AsyncClient(transport=_transport, timeout=httpx.Timeout(connect=30, read=600, write=30, pool=10))


# ── Debug helpers ──────────────────────────────────────────────────

def _sanitize_headers(headers) -> dict:
    """Mask sensitive header values for debug logging."""
    safe = dict(headers)
    for key in ("x-api-key", "Authorization"):
        if key in safe:
            val = safe[key]
            safe[key] = val[:8] + "..." + val[-4:] if len(val) > 16 else "***"
    return safe


def _truncate(body, max_len=10240) -> str:
    """Pretty-print a body for debug logging, truncated to max_len chars."""
    try:
        text = json.dumps(body, indent=2, ensure_ascii=False, default=str)
    except Exception:
        text = str(body)
    if len(text) > max_len:
        return text[:max_len] + f"\n... [{len(text) - max_len} chars truncated]"
    return text


# ── Response Cache (non-streaming only) ──────────────────────────

class _ResponseCache:
    """LRU cache for non-streaming API responses with TTL and size limit.

    Cache key: blake2b hash of raw request body bytes (excluding streaming and tool_use).
    Returns (body_bytes, headers_dict) or None on miss.
    Uses OrderedDict for O(1) LRU operations instead of list-based O(n).
    """
    def __init__(self, max_size: int = 1000, ttl: float = 300.0):
        self._max_size = max_size
        self._ttl = ttl
        self._store: dict[str, tuple[float, bytes, dict]] = {}  # key -> (ts, body, headers)
        self._access_order: OrderedDict[str, None] = OrderedDict()  # O(1) LRU tracking

    def _evict(self):
        evicted = 0
        while len(self._store) > self._max_size:
            oldest, _ = self._access_order.popitem(last=False)  # O(1) pop oldest
            self._store.pop(oldest, None)
            evicted += 1
        if evicted > 0:
            _debug(f"  [cache] _evict: evicted {evicted} entries, store_size={len(self._store)}/{self._max_size}")

    def get(self, key: str) -> tuple[bytes, dict] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, body, headers = entry
        if time.monotonic() - ts > self._ttl:
            _debug(f"  [cache] get: TTL expired (age={time.monotonic()-ts:.1f}s > ttl={self._ttl}s), evicting key={key[:16]}...")
            self._store.pop(key, None)
            self._access_order.pop(key, None)
            return None
        # Move to end of access order (most recently used) — O(1)
        self._access_order.move_to_end(key)
        _debug(f"  [cache] get: HIT key={key[:16]}... size={len(body)} bytes")
        return body, headers

    def put(self, key: str, body: bytes, headers: dict):
        if key in self._store:
            self._access_order.pop(key, None)
        self._store[key] = (time.monotonic(), body, dict(headers))
        self._access_order[key] = None  # append to end — O(1)
        self._evict()
        _debug(f"  [cache] put: key={key[:16]}... store_size={len(self._store)}/{self._max_size}")

    def make_key(self, body: dict, body_bytes: bytes | None = None) -> str | None:
        """Create cache key from request body. Returns None if not cacheable.

        If body_bytes is provided, hashes raw bytes directly (fast, no re-serialization).
        Falls back to json.dumps + blake2b if body_bytes is not provided.
        """
        if body.get("stream"):
            _debug(f"  [cache] make_key: stream=True, returning None")
            return None
        # Don't cache requests with tool use (non-deterministic)
        messages = body.get("messages", [])
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        _debug(f"  [cache] make_key: tool_result found, returning None")
                        return None
        try:
            import hashlib
            if body_bytes:
                # Fast path: hash raw bytes directly (avoids json.dumps + sort_keys)
                key = hashlib.blake2b(body_bytes, digest_size=16).hexdigest()
            else:
                # Fallback: deterministic JSON serialization + blake2b
                key = hashlib.blake2b(json.dumps(body, separators=(',', ':'), default=str).encode(), digest_size=16).hexdigest()
            _debug(f"  [cache] make_key: generated hash={key[:16]}...")
            return key
        except Exception:
            return None

    def stats(self) -> dict:
        return {"size": len(self._store), "max_size": self._max_size, "ttl": self._ttl}


_response_cache = _ResponseCache()


@asynccontextmanager
async def lifespan(app):
    _debug("  [lifespan] app starting")
    # Start background quota fetcher (no-op if env vars not set)
    await start_quota_fetcher(app)

    # Periodic WAL checkpoint (every hour)
    async def _periodic_checkpoint():
        while True:
            await asyncio.sleep(3600)
            await asyncio.to_thread(_wal_checkpoint)

    # Periodic DB flush (every 5s) to commit any batched inserts
    async def _periodic_db_flush():
        while True:
            await asyncio.sleep(_DB_COMMIT_INTERVAL)
            await asyncio.to_thread(_db_flush)

    checkpoint_task = asyncio.create_task(_periodic_checkpoint())
    db_flush_task = asyncio.create_task(_periodic_db_flush())
    _debug("  [lifespan] background tasks created (WAL checkpoint, DB flush, quota fetcher)")

    yield

    _debug("  [lifespan] app shutting down")
    # Flush any pending DB writes before shutdown
    await asyncio.to_thread(_db_flush)
    _debug("  [lifespan] final DB flush done")

    # Cancel background tasks
    checkpoint_task.cancel()
    db_flush_task.cancel()
    try:
        await checkpoint_task
    except asyncio.CancelledError:
        pass
    try:
        await db_flush_task
    except asyncio.CancelledError:
        pass
    _debug("  [lifespan] background tasks cancelled")

    task = getattr(app.state, '_quota_task', None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        _debug("  [lifespan] quota fetcher task cancelled")
    await _client.aclose()
    _debug("  [lifespan] HTTP client closed")
    # Close quota fetcher shared client
    from dashboard.quota import _http_client as _quota_client
    if _quota_client and not _quota_client.is_closed:
        await _quota_client.aclose()
        _debug("  [lifespan] quota fetcher HTTP client closed")

app = FastAPI(lifespan=lifespan)

@app.exception_handler(Exception)
async def debug_exception(request: Request, exc: Exception):
    tb = traceback.format_exc()
    client_ip = _get_client_ip(request)
    _log(f"ERROR {request.method} {request.url.path} from {client_ip}: {type(exc).__name__}: {exc}")
    _debug(f"Traceback (500):\n{tb}")
    # Don't expose tracebacks to clients — log them server-side only
    return JSONResponse(status_code=500, content={"error": "Internal server error"})

# Server manager set later in __main__ for GUI mode; None means always-running
_server_manager = None

register_dashboard(app, STATIC_DIR, _conn, server_manager_getter=lambda: _server_manager, token_usage=_token_usage, token_lock=_token_lock)


# ── Rate Limiting (token bucket, per-IP) ────────────────────────

RATE_LIMIT_RPS = float(os.environ.get("RATE_LIMIT_RPS", "50"))
RATE_LIMIT_BURST = float(os.environ.get("RATE_LIMIT_BURST", "100"))
_STALE_BUCKET_TTL = 300  # seconds — remove buckets inactive for 5 min


class _Bucket:
    """Token bucket for a single client IP."""
    __slots__ = ("tokens", "last_refill", "max_tokens", "refill_rate", "lock", "last_access")

    def __init__(self, rate: float, burst: float):
        self.tokens = burst
        self.last_refill = time.monotonic()
        self.max_tokens = burst
        self.refill_rate = rate  # tokens per second
        self.lock = asyncio.Lock()
        self.last_access = time.monotonic()

    async def consume(self) -> tuple[bool, float]:
        """Try to consume one token. Returns (allowed, retry_after_seconds)."""
        async with self.lock:
            now = time.monotonic()
            self.last_access = now
            elapsed = now - self.last_refill
            # Refill tokens
            self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
            self.last_refill = now
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True, 0.0
            # Calculate wait time for next token
            wait = (1.0 - self.tokens) / self.refill_rate
            _debug(f"  [ratelimit] DENIED (tokens={self.tokens:.2f}, retry_after={wait:.2f}s)")
            return False, wait


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP token bucket rate limiter with periodic stale bucket cleanup."""

    # Paths that bypass rate limiting
    _SKIP_PREFIXES = ("/api/", "/static/", "/health")

    def __init__(self, app, rate: float = RATE_LIMIT_RPS, burst: float = RATE_LIMIT_BURST):
        super().__init__(app)
        self._rate = rate
        self._burst = burst
        self._buckets: dict[str, _Bucket] = {}
        self._cleanup_task: asyncio.Task | None = None

    async def dispatch(self, request: Request, call_next):
        path = request.url.path

        # Skip rate limiting for excluded paths
        if path == "/health" or any(path.startswith(p) for p in self._SKIP_PREFIXES):
            try:
                return await call_next(request)
            except ClientDisconnect:
                return StarletteJSONResponse(status_code=499, content={"error": "Client disconnected"})

        # Start cleanup task lazily on first request
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

        # Identify client IP
        ip = "unknown"
        if request.client:
            ip = request.client.host
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            ip = forwarded.split(",")[0].strip()

        # Get or create bucket for this IP
        bucket = self._buckets.get(ip)
        if bucket is None:
            bucket = _Bucket(self._rate, self._burst)
            self._buckets[ip] = bucket
            _debug(f"  [ratelimit] new bucket for {ip} (rate={self._rate}, burst={self._burst})")

        allowed, retry_after = await bucket.consume()
        if allowed:
            try:
                return await call_next(request)
            except ClientDisconnect:
                return StarletteJSONResponse(status_code=499, content={"error": "Client disconnected"})

        # Rate limited — return 503 (not 429) to avoid Claude Code auth window
        retry_after_int = max(1, int(retry_after) + 1)
        return StarletteJSONResponse(
            status_code=503,
            content={"error": "Rate limit exceeded. Try again shortly."},
            headers={"Retry-After": str(retry_after_int)},
        )

    async def _cleanup_loop(self):
        """Periodically remove buckets with no recent activity."""
        while True:
            await asyncio.sleep(60)
            now = time.monotonic()
            stale = [ip for ip, b in self._buckets.items()
                     if now - b.last_access > _STALE_BUCKET_TTL]
            for ip in stale:
                self._buckets.pop(ip, None)
            if stale:
                _debug(f"  [ratelimit] cleanup: {len(stale)} stale buckets removed, {len(self._buckets)} active")


app.add_middleware(RateLimitMiddleware)


# ── Access Log Middleware ─────────────────────────────────────────

class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log every request with method, path, status code, duration, and client IP."""

    _SKIP_PREFIXES = ("/api/", "/static/", "/health")

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/health" or any(path.startswith(p) for p in self._SKIP_PREFIXES):
            return await call_next(request)

        client_ip = request.client.host if request.client else "?"
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()

        start = time.monotonic()
        try:
            response = await call_next(request)
        except ClientDisconnect:
            _debug(f"  [access] client disconnected: {request.method} {path} {client_ip}")
            return StarletteJSONResponse(status_code=499, content={"error": "Client disconnected"})
        elapsed_ms = (time.monotonic() - start) * 1000

        _log(f"{request.method} {path} {response.status_code} {elapsed_ms:.0f}ms {client_ip}")
        return response


app.add_middleware(AccessLogMiddleware)


# ── Circuit Breaker (per-endpoint) ──────────────────────────────

_CB_FAILURE_THRESHOLD = 5     # trips open after N consecutive failures
_CB_RECOVERY_TIMEOUT = 60.0   # seconds before half-open test


class _CircuitBreaker:
    """Per-endpoint circuit breaker: CLOSED → OPEN → HALF_OPEN → CLOSED."""
    __slots__ = ("failures", "state", "opened_at", "total_requests", "total_failures", "last_failure_time", "created_at")

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
            _debug(f"  [cb] half_open → open (test request failed)")
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
                _debug(f"  [cb] open → half_open (cooldown expired)")
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


# ── Standardized error response helpers ──

def _anthropic_error(status_code: int, message: str, error_type: str = "api_error") -> JSONResponse:
    """Return an error in Anthropic Messages API format."""
    return JSONResponse(status_code=status_code, content={
        "type": "error",
        "error": {"type": error_type, "message": message},
    })

def _openai_error(status_code: int, message: str, error_type: str = "invalid_request_error") -> JSONResponse:
    """Return an error in OpenAI API format."""
    return JSONResponse(status_code=status_code, content={
        "error": {"message": message, "type": error_type, "code": str(status_code)},
    })


async def _forward_post(endpoint, json, headers):
    """POST with circuit breaker. Raises CircuitOpenError if circuit is open."""
    if not _cb_should_allow(endpoint):
        raise CircuitOpenError(f"Circuit breaker open for {endpoint}")
    try:
        resp = await _client.post(endpoint, json=json, headers=headers)
        _cb_record_success(endpoint)
        return resp
    except CircuitOpenError:
        raise
    except Exception:
        _cb_record_failure(endpoint)
        raise


async def _forward_stream(method, endpoint, json, headers):
    """Stream with circuit breaker. Raises CircuitOpenError if circuit is open."""
    if not _cb_should_allow(endpoint):
        raise CircuitOpenError(f"Circuit breaker open for {endpoint}")
    try:
        cm = _client.stream(method, endpoint, json=json, headers=headers)
        resp = await cm.__aenter__()
        if resp.status_code >= 500:
            _cb_record_failure(endpoint)
        else:
            _cb_record_success(endpoint)
        return cm, resp
    except CircuitOpenError:
        raise
    except Exception:
        _cb_record_failure(endpoint)
        raise


# ── Shared helper functions for endpoint handlers ──────────────

async def _do_request_with_retry(endpoint, body, headers, protocol, retry_on_429=True):
    """POST request with automatic 429 key failover and 5xx retry with backoff.

    Returns (response, final_headers) -- headers may differ after retry.
    Raises UpstreamError on connection/timeout/protocol failures.
    """
    _RETRYABLE_STATUSES = {502, 503, 504}
    max_retries = 2

    for attempt in range(max_retries):
        _debug(f"  → upstream POST {endpoint} attempt {attempt+1}/{max_retries} headers={_sanitize_headers(headers)}")
        t0 = time.monotonic()
        try:
            resp = await _client.post(endpoint, json=body, headers=headers)
        except httpx.ConnectError as e:
            _debug(f"  ✗ connect error after {(time.monotonic()-t0)*1000:.0f}ms: {e}")
            _log(f"  UPSTREAM CONNECT ERROR: {type(e).__name__}: {e}")
            raise UpstreamError(f"Cannot connect to upstream: {e}", status_code=502, original=e) from e
        except httpx.TimeoutException as e:
            _debug(f"  ✗ timeout after {(time.monotonic()-t0)*1000:.0f}ms: {e}")
            _log(f"  UPSTREAM TIMEOUT: {type(e).__name__}: {e}")
            raise UpstreamError(f"Upstream request timed out: {e}", status_code=504, original=e) from e
        except httpx.RequestError as e:
            _debug(f"  ✗ request error after {(time.monotonic()-t0)*1000:.0f}ms: {e}")
            _log(f"  UPSTREAM REQUEST ERROR: {type(e).__name__}: {e}")
            raise UpstreamError(f"Upstream request failed: {type(e).__name__}: {e}", status_code=502, original=e) from e

        elapsed_ms = (time.monotonic() - t0) * 1000
        _debug(f"  ← upstream {resp.status_code} in {elapsed_ms:.0f}ms | content-type={resp.headers.get('content-type', '?')}")

        # Retry on 429 with key failover
        if retry_on_429 and resp.status_code == 429 and len(API_KEYS) > 1:
            failed_key = _key_from_headers(headers, protocol)
            alt = _find_alternative_key(failed_key)
            if alt:
                _debug(f"  ⟳ 429 failover: {failed_key[:8]}… → alias={alt.get('alias', '?')}")
                _log(f"  429 on key, retrying with alternative key")
                headers = _get_auth_headers(protocol, entry=alt)
                continue

        # Retry on 502/503/504 with backoff
        if resp.status_code in _RETRYABLE_STATUSES and attempt < max_retries - 1:
            wait = 1.0 * (2 ** attempt)  # 1s, 2s
            _debug(f"  ⟳ retry {resp.status_code} in {wait:.1f}s")
            _log(f"  RETRY {resp.status_code} after {wait:.1f}s (attempt {attempt+1}/{max_retries})")
            await asyncio.sleep(wait)
            continue

        return resp, headers

    # Should not reach here, but safety fallback
    return resp, headers


def _update_token_usage(model_id, inp, out, cache):
    """Thread-safe update of in-memory token counters."""
    try:
        with _token_lock:
            if model_id not in _token_usage:
                _token_usage[model_id] = {"input": 0, "output": 0, "cache": 0}
            _token_usage[model_id]["input"] += inp
            _token_usage[model_id]["output"] += out
            _token_usage[model_id]["cache"] += cache
        _debug(f"  [tokens] _update_token_usage: model={model_id} +{inp} in +{out} out +{cache} cache | totals: {_token_usage[model_id]}")
    except Exception as e:
        _debug(f"  ✗ _update_token_usage failed: {type(e).__name__}: {e}")
        _log(f"  WARN: _update_token_usage failed for {model_id!r}: {type(e).__name__}: {e}")


async def _save_and_log_request(req_id, model_id, original_model, start_time,
                                 inp, out, cache, protocol, is_stream, thinking_type,
                                 effort, client_ip, account_alias, tools, log_tag="",
                                 tools_used=None):
    """Log success and save to DB with success=True."""
    alias_tag = f" | account={account_alias}" if account_alias else ""
    _log(f"  ← {model_id} | +{inp} in{log_tag} | +{out} out{log_tag} | +{cache} cache{alias_tag}")
    try:
        await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                     inp, out, cache, success=True,
                     protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                     client_ip=client_ip, account_alias=account_alias, tools=tools,
                     tools_used=tools_used)
    except Exception as e:
        _debug(f"  ✗ save_request failed: {type(e).__name__}: {e}")
        _log(f"  WARN: save_request failed: {type(e).__name__}: {e}")


async def _log_and_save_error(req_id, model_id, original_model, start_time,
                               status_code, resp_text, protocol, is_stream, thinking_type,
                               effort, client_ip, account_alias, tools, tools_used=None):
    """Log error and save to DB with success=False."""
    _log(f"  ERROR {status_code}: {resp_text[:300]}")
    try:
        await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                     0, 0, 0, success=False, error=f"HTTP {status_code}",
                     protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                     client_ip=client_ip, account_alias=account_alias, tools=tools,
                     tools_used=tools_used)
    except Exception as e:
        _debug(f"  ✗ save_request (error path) failed: {type(e).__name__}: {e}")
        _log(f"  WARN: save_request (error path) failed: {type(e).__name__}: {e}")


# ── Streaming helpers ───────────────────────────────────────────

def _make_stream_retry_loop(protocol):
    """Return a retry function for streaming 429 handling.

    Returns (attempt_headers, should_retry) where should_retry=True means
    the caller should `continue` the outer loop.
    """
    def _handle_429(headers, status_code, attempt):
        if status_code == 429 and attempt == 0 and len(API_KEYS) > 1:
            failed_key = _key_from_headers(headers, protocol)
            alt = _find_alternative_key(failed_key)
            if alt:
                _log(f"  429 on key, retrying with alternative key")
                return _get_auth_headers(protocol, entry=alt), True
        return headers, False
    return _handle_429


async def _stream_error_response(req_id, model_id, original_model, start_time,
                                  status_code, resp_body, protocol, thinking_type,
                                  effort, client_ip, account_alias, tools,
                                  error_payload, tools_used=None):
    """Handle streaming error: log, save DB, yield error SSE event. Returns the error event bytes."""
    _log(f"  ERROR {status_code}: {resp_body[:300] if isinstance(resp_body, str) else resp_body[:300]}")
    await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                 0, 0, 0, success=False, error=f"HTTP {status_code}",
                 protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                 client_ip=client_ip, account_alias=account_alias, tools=tools,
                 tools_used=tools_used)
    return _sse("error", error_payload)


def _finalize_stream_tokens(model_id, est_input, stream_in, stream_out, stream_cache,
                             actual_usage, _token_usage, _token_lock):
    """Reconcile estimated vs actual token counts after stream ends.

    Returns (final_in, final_out, final_cache, log_tag).
    """
    final_in = stream_in if stream_in is not None else est_input
    final_out = stream_out
    final_cache = stream_cache
    log_tag = ""

    try:
        if actual_usage:
            final_in = actual_usage.get("prompt_tokens") or final_in
            final_out = actual_usage.get("completion_tokens")
            if final_out is None:
                total = actual_usage.get("total_tokens")
                prompt = actual_usage.get("prompt_tokens")
                if total is not None and prompt is not None:
                    final_out = total - prompt
            final_out = final_out or stream_out
            final_cache = _extract_cache_tokens(actual_usage)
            log_tag = ""
            with _token_lock:
                _token_usage[model_id]["input"] -= est_input
                _token_usage[model_id]["input"] += final_in
                _token_usage[model_id]["output"] += final_out
                if final_cache:
                    _token_usage[model_id]["cache"] += final_cache
        else:
            log_tag = " (est)"
            with _token_lock:
                _token_usage[model_id]["output"] += stream_out
            _debug(f"  [tokens] _finalize_stream_tokens: model={model_id} fallback (no actual_usage) est_in={est_input} stream_out={stream_out}")
    except Exception as e:
        _debug(f"  ✗ _finalize_stream_tokens failed: {type(e).__name__}: {e}")
        _log(f"  WARN: _finalize_stream_tokens failed for {model_id!r}: {type(e).__name__}: {e}")

    return final_in, final_out, final_cache, log_tag


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()


# Keepalive comment that is harmless to clients: every 15s during idle periods
_SSE_KEEPALIVE_INTERVAL = 15  # seconds


async def _sse_keepalive(stream_gen, interval: float = _SSE_KEEPALIVE_INTERVAL):
    """Wrap an async generator to inject SSE keepalive comments during idle periods.

    When no chunk is yielded for `interval` seconds, sends `: ping\\n\\n`
    (a comment line, ignored by SSE clients) to keep the connection alive.
    """
    while True:
        try:
            chunk = await asyncio.wait_for(anext(stream_gen), timeout=interval)
            yield chunk
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            yield b": ping\n\n"
        except Exception as e:
            # ClientDisconnect, ConnectionResetError, etc. — client gone, stop gracefully
            _debug(f"  [stream] keepalive exiting: {type(e).__name__}: {e}")
            return


def _route_for(model_name: str, tool_names: list = None) -> dict | None:
    maybe_reload_custom_routes()
    name = model_name.lower().strip()
    if not name:
        return None
    # When DISABLE_MAPPING, only check custom routes (not auto-generated aliases)
    if DISABLE_MAPPING:
        for r in CUSTOM_ROUTES.values():
            if any(m in name for m in r.get("match", [])):
                _debug(f"  [route] DISABLE_MAPPING custom match: '{name}' → {r.get('model')}")
                return r
        # No custom route matched — check if the model exists directly
        if name in MODELS:
            _debug(f"  [route] DISABLE_MAPPING direct model: '{name}'")
            return {"match": [name], "model": model_name}
        _debug(f"  [route] DISABLE_MAPPING no match for '{name}'")
        return None
    # 1. Tool-based routing (optional, additive)
    tool_names_lower = [t.lower() for t in (tool_names or [])]
    for r in ROUTES.values():
        if r.get("enabled") is False:
            continue
        if tool_names_lower and any(m in t for m in r.get("match", []) for t in tool_names_lower):
            _debug(f"  [route] tool-based match: {r.get('model')} (tool match)")
            return r
    # 2. Model-based routing (alias matching)
    for r in ROUTES.values():
        if r.get("enabled") is False:
            continue
        if any(m in name for m in r.get("match", [])):
            _debug(f"  [route] model match: {r.get('model')} (pattern in '{name}')")
            return r
    # 3. Direct lookup in MODELS (exact model name)
    if name in MODELS:
        _debug(f"  [route] direct MODELS lookup: '{name}'")
        return {"match": [name], "model": name}
    # 4. Wildcard catch-all: if a custom route "*" (or legacy "") exists, use it
    wildcard = CUSTOM_ROUTES.get("*") or CUSTOM_ROUTES.get("")
    if wildcard and isinstance(wildcard, dict) and wildcard.get("model") and wildcard.get("enabled") is not False:
        _debug(f"  [route] wildcard catch-all: {wildcard.get('model')}")
        return wildcard
    # 5. No match found
    _debug(f"  [route] no match for '{name}'")
    return None


def _extract_tool_names(body: dict) -> list:
    """Extract tool names from request body (Anthropic or OpenAI format)."""
    tools = body.get("tools", [])
    if not isinstance(tools, list):
        return []
    names = []
    for t in tools:
        if isinstance(t, dict):
            if "name" in t and isinstance(t["name"], str):
                names.append(t["name"])
            elif "function" in t and isinstance(t["function"], dict):
                fn = t["function"]
                if "name" in fn and isinstance(fn["name"], str):
                    names.append(fn["name"])
    _debug(f"  [tools] extracted {len(names)} tool names: {names}")
    return names


def _extract_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for i in content:
            if isinstance(i, str):
                parts.append(i)
            elif isinstance(i, dict):
                if i.get("type") == "text":
                    parts.append(i.get("text", ""))
                elif i.get("type") == "thinking":
                    parts.append(i.get("thinking", ""))
                elif i.get("type") == "image":
                    parts.append(f"[image:{i.get('source', {}).get('type', 'unknown')}]")
                else:
                    parts.append(i.get("text", str(i)))
        return "\n".join(parts)
    return str(content) if content else ""


# ── Cache restructuration for models without semantic caching ──
CACHE_REWRITE_MODELS = {"mimo-v2.5", "mimo-v2-pro", "mimo-v2-omni", "mimo-v2.5-pro"}


def _find_split_point(text: str) -> int:
    """Find the best split point between static instructions and dynamic content.

    Looks for the last double-newline in the first 8000 chars to split cleanly.
    """
    search_limit = min(8000, len(text) // 2)
    last_double = text.rfind("\n\n", 0, search_limit)
    if last_double > 500:
        return last_double
    last_newline = text.rfind("\n", 0, search_limit)
    if last_newline > 500:
        return last_newline
    return 0


def _restructure_for_cache(oai_body: dict, model_id: str) -> dict:
    """For models without semantic caching, split the system prompt.

    Keeps the static part (instructions + tools) as the system message with
    cache_control, and moves the dynamic part (conversation history) into
    the messages array so the prefix stays stable across requests.
    """
    if model_id not in CACHE_REWRITE_MODELS:
        return oai_body

    messages = oai_body.get("messages", [])
    if not messages:
        return oai_body

    # Find the system message
    sys_idx = None
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            sys_idx = i
            break

    if sys_idx is None:
        return oai_body

    sys_content = messages[sys_idx].get("content", "")
    if not isinstance(sys_content, str) or len(sys_content) < CACHE_MIN_PROMPT_SIZE:
        _debug(f"  [cache-restructure] skipped: sys_content len={len(sys_content) if isinstance(sys_content, str) else 0} < min={CACHE_MIN_PROMPT_SIZE}")
        return oai_body  # Small prompt, no need to restructure

    split_point = _find_split_point(sys_content)
    if split_point <= 0:
        _debug(f"  [cache-restructure] skipped: no valid split point found in {len(sys_content)} chars")
        return oai_body

    static_part = sys_content[:split_point].strip()
    dynamic_part = sys_content[split_point:].strip()

    # Rebuild: static system message with cache_control + dynamic as user message
    new_messages = [{
        "role": "system",
        "content": static_part,
        "cache_control": {"type": "ephemeral"},
    }]

    if dynamic_part:
        new_messages.append({"role": "user", "content": dynamic_part})

    # Append original messages (skip the old system message)
    for i, m in enumerate(messages):
        if i != sys_idx:
            new_messages.append(m)

    oai_body["messages"] = new_messages
    _debug(f"  [cache-restructure] split at point={split_point}: static={len(static_part)} dynamic={len(dynamic_part)} chars")
    _log(f"  [cache] split system prompt: static={len(static_part)} dynamic={len(dynamic_part)} chars")
    return oai_body


def _strip_billing_header(text: str) -> str:
    """Remove x-anthropic-billing-header from system prompt.

    Claude Code injects a billing header with a changing hash (cch=...) that
    breaks prompt caching by modifying the prefix on every request.
    """
    if not text.startswith("x-anthropic-billing-header:"):
        return text
    # Strip the first line (the header) and any trailing blank line
    first_nl = text.find("\n")
    if first_nl == -1:
        return text
    rest = text[first_nl + 1:]
    if rest.startswith("\n"):
        rest = rest[1:]
    return rest


def anthropic_to_openai(body: dict, model: str) -> dict:
    thinking = isinstance(body.get("thinking"), dict) and body["thinking"].get("type") in ("enabled", "adaptive")

    messages = []

    # System prompt — always add cache_control for prefix caching
    system_val = body.get("system", "")
    if isinstance(system_val, list):
        text = _extract_text(system_val)
        if text:
            text = _strip_billing_header(text)
            msg = {"role": "system", "content": text, "cache_control": {"type": "ephemeral"}}
            messages.append(msg)
    elif system_val:
        msg = {"role": "system", "content": _strip_billing_header(system_val)}
        # Always add cache_control to system messages for prefix caching
        msg["cache_control"] = {"type": "ephemeral"}
        messages.append(msg)

    for msg in body.get("messages", []):
        role, content = msg["role"], msg.get("content", "")
        is_asst = role == "assistant"

        # Simple string content
        if isinstance(content, str):
            out = {"role": role, "content": content}
            if thinking and is_asst:
                out["reasoning_content"] = " "
            messages.append(out)
            continue

        if not isinstance(content, list):
            continue

        text_parts, tool_calls, thinking_parts, tool_results = [], [], [], []
        last_cache_control = None

        for block in content:
            if isinstance(block, str):
                text_parts.append(block)
                continue
            if not isinstance(block, dict):
                continue

            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", ""))
                if "cache_control" in block:
                    last_cache_control = block["cache_control"]
            elif btype == "thinking":
                thinking_parts.append(block.get("thinking", ""))
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
            elif btype == "tool_result":
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": _extract_text(block.get("content", "")),
                })
                if "cache_control" in block:
                    last_cache_control = block["cache_control"]

        # Emit tool_result messages first (must immediately follow assistant's tool_calls)
        messages.extend(tool_results)

        # Then emit the main message (text + tool_calls + thinking)
        joined_thinking = "\n".join(thinking_parts) if thinking_parts else ""
        if tool_calls:
            out = {
                "role": role,
                "content": "\n".join(text_parts) if text_parts else "",
                "tool_calls": tool_calls,
            }
            if joined_thinking:
                out["reasoning_content"] = joined_thinking
            elif thinking and is_asst:
                out["reasoning_content"] = " "
            if last_cache_control and not is_asst:
                out["cache_control"] = last_cache_control
            messages.append(out)
        elif text_parts or thinking_parts or (thinking and is_asst):
            out = {"role": role, "content": "\n".join(text_parts) if text_parts else ""}
            if joined_thinking:
                out["reasoning_content"] = joined_thinking
            elif thinking and is_asst:
                out["reasoning_content"] = " "
            if last_cache_control and not is_asst:
                out["cache_control"] = last_cache_control
            messages.append(out)

    # Add cache_control to the last user message for optimal prefix caching
    # (Anthropic best practice: cache system + last user turn)
    for i in range(len(messages) - 1, -1, -1):
        if messages[i].get("role") == "user":
            messages[i]["cache_control"] = {"type": "ephemeral"}
            break

    # Build request
    oai = {"model": model, "messages": messages,
           "max_tokens": body.get("max_tokens", 16384),
           "stream": body.get("stream", False)}

    for key, oai_key in [("temperature", "temperature"), ("top_p", "top_p"), ("stop_sequences", "stop")]:
        if key in body:
            oai[oai_key] = body[key]

    if "tools" in body:
        # Support both Anthropic format (name at top level) and OpenAI format (function.name)
        oai_tools = []
        for t in body["tools"]:
            if "name" in t:
                # Anthropic format: {"name": "...", "description": "...", "input_schema": {...}}
                oai_tools.append({"type": "function", "function": {
                    "name": t["name"], "description": t.get("description", ""),
                    "parameters": t.get("input_schema", {}),
                }})
            elif "function" in t:
                # OpenAI format: {"type": "function", "function": {"name": "...", ...}}
                fn = t["function"]
                oai_tools.append({"type": "function", "function": {
                    "name": fn.get("name", ""), "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {}),
                }})
            else:
                # Unknown format, try best effort
                oai_tools.append({"type": "function", "function": {
                    "name": t.get("name", ""), "description": t.get("description", ""),
                    "parameters": t.get("input_schema", t.get("parameters", {})),
                }})
        oai["tools"] = oai_tools
        tc = body.get("tool_choice", "auto")
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "tool":
                oai["tool_choice"] = {"type": "function", "function": {"name": tc.get("name", "")}}
            elif tc_type == "any":
                oai["tool_choice"] = "required"
            else:
                oai["tool_choice"] = "auto"
        else:
            oai["tool_choice"] = tc

    # Convert Anthropic thinking/effort → OpenAI reasoning parameters
    # Claude Code sends: thinking: {type: "adaptive"} OR effort: "low"/"medium"/"high"/"xhigh"/"max"
    effort_level = body.get("effort")
    thinking_param = body.get("thinking") if isinstance(body.get("thinking"), dict) else {}
    ttype = thinking_param.get("type", "")
    budget = thinking_param.get("budget_tokens", 0)

    if effort_level and effort_level != "none":
        wants_thinking = True
    elif ttype in ("enabled", "adaptive") or budget > 0:
        wants_thinking = True
        if budget >= 16000 or budget == 0:
            effort_level = "xhigh"
        elif budget >= 10000:
            effort_level = "high"
        elif budget >= 4000:
            effort_level = "medium"
        elif ttype == "adaptive":
            effort_level = "medium"
        else:
            effort_level = "low"
    else:
        wants_thinking = False

    if wants_thinking:
        if model.startswith("kimi-k2.6"):
            # Kimi K2.6 uses reasoning: true (not reasoning_effort)
            oai["reasoning"] = True
            _debug(f"  [thinking] {model}: reasoning=True (effort={effort_level})")
        elif model.startswith("deepseek-v4"):
            # DeepSeek V4 only supports high and max
            if effort_level in ("xhigh", "max"):
                oai["reasoning_effort"] = "max"
            else:
                oai["reasoning_effort"] = "high"
            _debug(f"  [thinking] {model}: reasoning_effort={oai['reasoning_effort']} (effort={effort_level})")
        else:
            # MiMo V2.5 etc. supports low/medium/high
            if effort_level in ("xhigh", "max"):
                oai["reasoning_effort"] = "high"
            elif effort_level in ("high",):
                oai["reasoning_effort"] = "high"
            elif effort_level in ("medium",):
                oai["reasoning_effort"] = "medium"
            else:
                oai["reasoning_effort"] = "low"
            _debug(f"  [thinking] {model}: reasoning_effort={oai['reasoning_effort']} (effort={effort_level})")

    # Restructure system prompt for models without semantic caching
    oai = _restructure_for_cache(oai, model)
    return oai


def openai_to_anthropic(resp: dict, model: str) -> dict:
    choice = resp.get("choices", [{}])[0]
    msg = choice.get("message", {})
    usage = resp.get("usage", {})

    blocks = []
    if reasoning := msg.get("reasoning_content") or msg.get("reasoning"):
        blocks.append({"type": "thinking", "thinking": reasoning})
    if msg.get("content"):
        blocks.append({"type": "text", "text": msg["content"]})
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function", {})
        try:
            inp = json.loads(fn.get("arguments", "{}"))
        except Exception:
            inp = {}
        blocks.append({"type": "tool_use", "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                        "name": fn.get("name", ""), "input": inp})

    if not blocks:
        blocks.append({"type": "text", "text": ""})

    stop = "tool_use" if msg.get("tool_calls") else "end_turn"
    if choice.get("finish_reason") == "length":
        stop = "max_tokens"

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
        "content": blocks, "model": model, "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0),
                "output_tokens": usage.get("completion_tokens", 0),
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)},
    }


def openai_to_anthropic_request(oai_body: dict) -> dict:
    """Convert OpenAI Chat Completions request → Anthropic Messages format."""
    system_text = ""
    pending_tool_results = []
    anthro_messages = []

    for msg in oai_body.get("messages", []):
        role = msg.get("role", "")

        if role == "system":
            system_text = _extract_text(msg.get("content", ""))
            continue

        if role == "tool":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": msg.get("tool_call_id", ""),
                "content": _extract_text(msg.get("content", "")),
            })
            continue

        if role not in ("user", "assistant"):
            continue

        blocks = []

        # Prepend pending tool_results to the next user message
        if role == "user" and pending_tool_results:
            blocks.extend(pending_tool_results)
            pending_tool_results = []

        # Convert content
        content = msg.get("content", "")
        if isinstance(content, str):
            if content:
                blocks.append({"type": "text", "text": content})
        elif isinstance(content, list):
            for block in content:
                t = block.get("type", "")
                if t == "text":
                    blocks.append({"type": "text", "text": block.get("text", "")})

        # Convert tool_calls (assistant only)
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            try:
                inp = json.loads(fn.get("arguments", "{}"))
            except Exception:
                inp = {}
            blocks.append({
                "type": "tool_use",
                "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                "name": fn.get("name", ""),
                "input": inp,
            })

        # Convert reasoning_content → thinking block (assistant only)
        if role == "assistant":
            reasoning = msg.get("reasoning_content") or msg.get("reasoning")
            if isinstance(reasoning, str) and reasoning.strip():
                blocks.insert(0, {"type": "thinking", "thinking": reasoning})

        # Ensure at least one block
        if not blocks:
            blocks.append({"type": "text", "text": ""})

        anthro_messages.append({"role": role, "content": blocks})

    # Trailing tool_results (edge case)
    if pending_tool_results:
        anthro_messages.append({"role": "user", "content": pending_tool_results})

    result = {
        "model": oai_body.get("model", ""),
        "messages": anthro_messages,
        "max_tokens": oai_body.get("max_tokens", 16384),
        "stream": oai_body.get("stream", False),
    }

    if system_text:
        result["system"] = system_text

    # Map simple params
    for key, anthro_key in [("temperature", "temperature"), ("top_p", "top_p"),
                             ("stop", "stop_sequences")]:
        if key in oai_body:
            result[anthro_key] = oai_body[key]

    # Convert tools
    if "tools" in oai_body:
        result["tools"] = [{
            "name": t["function"]["name"],
            "description": t["function"].get("description", ""),
            "input_schema": t["function"].get("parameters", {}),
        } for t in oai_body["tools"] if t.get("type") == "function"]

        # Convert tool_choice
        tc = oai_body.get("tool_choice", "auto")
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "function":
                result["tool_choice"] = {"type": "tool", "name": tc.get("function", {}).get("name", "")}
            elif tc_type == "any":
                result["tool_choice"] = {"type": "any"}
            else:
                result["tool_choice"] = tc_type
        else:
            result["tool_choice"] = tc

    return result


def anthropic_to_openai_response(anthro: dict, model: str) -> dict:
    """Convert Anthropic Messages response → OpenAI Chat Completions format."""
    content_blocks = anthro.get("content", [])
    text_parts = []
    reasoning_text = ""
    tool_calls = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        t = block.get("type", "")
        if t == "text":
            text_parts.append(block.get("text", ""))
        elif t == "thinking":
            reasoning_text = block.get("thinking", "")
        elif t == "tool_use":
            tool_calls.append({
                "id": block.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                },
            })

    # Determine finish_reason
    sr = anthro.get("stop_reason", "")
    if sr == "max_tokens":
        finish = "length"
    elif sr == "tool_use":
        finish = "tool_calls"
    else:
        finish = "stop"

    # Usage mapping
    usage = anthro.get("usage", {})
    prompt_tokens = usage.get("input_tokens", 0)
    completion_tokens = usage.get("output_tokens", 0)
    oai_usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    cache_read = usage.get("cache_read_input_tokens", 0)
    if cache_read:
        oai_usage["prompt_tokens_details"] = {"cached_tokens": cache_read}

    message = {"role": "assistant"}
    if text_parts:
        message["content"] = "\n".join(text_parts)
    else:
        message["content"] = ""
    if reasoning_text:
        message["reasoning_content"] = reasoning_text
    if tool_calls:
        message["tool_calls"] = tool_calls

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish,
        }],
        "usage": oai_usage,
    }


def openai_responses_to_anthropic(body: dict) -> dict:
    """Convert OpenAI Responses API request → Anthropic Messages format."""
    system_text = ""
    pending_tool_results = []
    anthro_messages = []

    for item in body.get("input", []):
        if not isinstance(item, dict):
            continue
        role = item.get("role", item.get("type", "user"))

        if role in ("system", "developer"):
            for block in (item.get("content") or []):
                if isinstance(block, dict) and block.get("type") == "input_text":
                    system_text += block.get("text", "")
            continue

        if item.get("type") == "function_call_output":
            pending_tool_results.append({
                "type": "tool_result",
                "tool_use_id": item.get("call_id", item.get("id", "")),
                "content": item.get("output", ""),
            })
            continue

        # Convert previous-turn function_call items to assistant tool_use blocks
        if item.get("type") == "function_call":
            try:
                inp = json.loads(item.get("arguments", "{}"))
            except Exception:
                inp = {}
            anthro_messages.append({
                "role": "assistant",
                "content": [{
                    "type": "tool_use",
                    "id": item.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
                    "name": item.get("name", ""),
                    "input": inp,
                }]
            })
            continue

        if role not in ("user", "assistant"):
            continue

        blocks = []
        if role == "user" and pending_tool_results:
            blocks.extend(pending_tool_results)
            pending_tool_results = []

        for block in (item.get("content") or []):
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")
            if btype in ("input_text", "text"):
                blocks.append({"type": "text", "text": block.get("text", "")})
            elif btype == "reasoning":
                summary = block.get("summary") or []
                text = "".join(s.get("text", "") for s in summary if isinstance(s, dict))
                if text:
                    blocks.insert(0, {"type": "thinking", "thinking": text})

        if not blocks:
            blocks.append({"type": "text", "text": ""})
        anthro_messages.append({"role": role, "content": blocks})

    if pending_tool_results:
        anthro_messages.append({"role": "user", "content": pending_tool_results})

    result = {
        "model": body.get("model", ""),
        "messages": anthro_messages,
        "max_tokens": body.get("max_output_tokens", 16384),
        "stream": body.get("stream", False),
    }
    if system_text:
        result["system"] = system_text
    if "temperature" in body:
        result["temperature"] = body["temperature"]
    if "top_p" in body:
        result["top_p"] = body["top_p"]

    # Convert tools (strip "type": "function" wrapper)
    if "tools" in body:
        result["tools"] = []
        for t in body["tools"]:
            if isinstance(t, dict) and t.get("type") == "function":
                tool = {"name": t["name"], "description": t.get("description", "")}
                tool["input_schema"] = t.get("input_schema") or t.get("parameters") or {}
                result["tools"].append(tool)
        tc = body.get("tool_choice", "auto")
        if isinstance(tc, dict):
            tc_type = tc.get("type", "auto")
            if tc_type == "function":
                result["tool_choice"] = {"type": "tool", "name": tc.get("name", "")}
            else:
                result["tool_choice"] = tc_type
        else:
            result["tool_choice"] = tc

    # Convert Anthropic thinking/effort -> model-specific reasoning parameter
    # Claude Code sends: thinking: {type: "adaptive"} OR effort: "low"/"medium"/"high"/"xhigh"/"max"
    effort_level = body.get("effort")
    thinking = body.get("thinking") if isinstance(body.get("thinking"), dict) else {}
    ttype = thinking.get("type", "")
    budget = thinking.get("budget_tokens", 0)

    # Determine desired effort from effort param or thinking param
    if effort_level and effort_level != "none":
        wants_thinking = True
    elif ttype in ("enabled", "adaptive") or budget > 0:
        wants_thinking = True
        # Map deprecated budget_tokens -> effort level
        if budget >= 16000 or budget == 0:
            effort_level = "xhigh"
        elif budget >= 10000:
            effort_level = "high"
        elif budget >= 4000:
            effort_level = "medium"
        elif ttype == "adaptive":
            effort_level = "medium"
        else:
            effort_level = "low"
    else:
        wants_thinking = False

    if wants_thinking:
        if model.startswith("kimi-k2.6"):
            # Kimi K2.6 uses reasoning: true (not reasoning_effort)
            result["reasoning"] = True
            _debug(f"  [thinking] {model}: reasoning=True (effort={effort_level})")
        elif model.startswith("deepseek-v4"):
            # DeepSeek V4 only supports high and max
            if effort_level in ("xhigh", "max"):
                result["reasoning_effort"] = "max"
            else:
                result["reasoning_effort"] = "high"
            _debug(f"  [thinking] {model}: reasoning_effort={result['reasoning_effort']} (effort={effort_level})")
        else:
            # MiMo V2.5 etc. supports low/medium/high
            if effort_level in ("xhigh", "max"):
                result["reasoning_effort"] = "high"
            elif effort_level in ("high",):
                result["reasoning_effort"] = "high"
            elif effort_level in ("medium",):
                result["reasoning_effort"] = "medium"
            else:
                result["reasoning_effort"] = "low"
            _debug(f"  [thinking] {model}: reasoning_effort={result['reasoning_effort']} (effort={effort_level})")

    return result


def anthropic_to_openai_responses(anthro: dict, model: str) -> dict:
    """Convert Anthropic Messages response → OpenAI Responses API format."""
    content_blocks = anthro.get("content", [])
    output_items = []
    text_content = []
    function_calls = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type", "")
        if btype == "text":
            text_content.append({"type": "output_text", "text": block.get("text", "")})
        elif btype == "thinking":
            output_items.insert(0, {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": block.get("thinking", "")}],
            })
        elif btype == "tool_use":
            function_calls.append({
                "type": "function_call",
                "id": block.get("id", f"call_{uuid.uuid4().hex[:12]}"),
                "name": block.get("name", ""),
                "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                "status": "completed",
            })

    if text_content:
        output_items.append({"type": "message", "role": "assistant", "content": text_content})
    output_items.extend(function_calls)

    # Status mapping
    sr = anthro.get("stop_reason", "")
    if sr == "max_tokens":
        status = "incomplete"
    else:
        status = "completed"

    # Usage mapping
    usage = anthro.get("usage", {})
    in_t = usage.get("input_tokens", 0)
    out_t = usage.get("output_tokens", 0)
    oai_usage = {
        "input_tokens": in_t,
        "output_tokens": out_t,
        "total_tokens": in_t + out_t,
        "output_tokens_details": {"reasoning_tokens": 0, "cached_tokens": usage.get("cache_read_input_tokens", 0)},
    }

    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "status": status,
        "model": model,
        "output": output_items,
        "usage": oai_usage,
    }


def openai_chat_to_responses(chat_resp: dict, model: str) -> dict:
    """Convert OpenAI Chat Completions response directly to OpenAI Responses API format.

    Bypasses the intermediate Anthropic format to avoid data loss and unnecessary conversion.
    """
    choice = chat_resp.get("choices", [{}])[0]
    msg = choice.get("message", {})
    usage = chat_resp.get("usage", {})

    output_items = []

    # Reasoning content -> reasoning item
    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if reasoning:
        output_items.insert(0, {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })

    # Text content -> message item with output_text
    content = msg.get("content", "")
    if content:
        output_items.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": content}],
        })

    # Tool calls -> function_call items
    for tc in (msg.get("tool_calls") or []):
        fn = tc.get("function", {})
        output_items.append({
            "type": "function_call",
            "id": tc.get("id", f"call_{uuid.uuid4().hex[:12]}"),
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments", "{}"),
            "status": "completed",
        })

    # Status mapping
    finish = choice.get("finish_reason", "")
    if finish == "length":
        status = "incomplete"
    else:
        status = "completed"

    # Usage mapping
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)
    cached = _extract_cache_tokens(usage)
    oai_usage = {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "output_tokens_details": {
            "reasoning_tokens": 0,
            "cached_tokens": cached,
        },
    }

    return {
        "id": f"resp_{uuid.uuid4().hex[:24]}",
        "object": "response",
        "status": status,
        "model": model,
        "output": output_items,
        "usage": oai_usage,
    }


# ── Thinking models token guard ────────────────────────────
THINKING_MODELS = {
    "deepseek-v4-flash": 2048,
    "deepseek-v4-pro": 4096,
}

def ensure_min_tokens(body: dict, default: int = 256) -> dict:
    """Ajuste max_output_tokens pour les modèles thinking afin qu'il
    reste des tokens pour la réponse après le reasoning."""
    model = body.get("model", "")
    min_tokens = default
    for prefix, tokens in THINKING_MODELS.items():
        if model.startswith(prefix) or model == prefix:
            min_tokens = max(min_tokens, tokens)
            break
    current = body.get("max_output_tokens") or body.get("max_tokens")
    if current is not None and current < min_tokens:
        body["max_output_tokens"] = min_tokens
        _log(f"  ⚠️ {model}: max_tokens ajusté {current} → {min_tokens}")
    return body


def _estimate_tokens(text: str) -> int:
    """Fast token estimation — uses char-length for small strings, tiktoken for large."""
    if len(text) < 200:
        # Fast path: ~4 chars per token for English, ~2 for CJK — use 3 as compromise
        return max(1, len(text) // 3)
    if _encoding:
        return len(_encoding.encode(text))
    return max(1, len(text) // 3)


def _estimate_input_tokens(body: dict) -> int:
    """Estimate input tokens from message content, tools, and tool_results."""
    try:
        chunks = []

        # System prompt
        system = body.get("system", "")
        if isinstance(system, str):
            chunks.append(system)
        elif isinstance(system, list):
            for s in system:
                if isinstance(s, str):
                    chunks.append(s)
                elif isinstance(s, dict):
                    chunks.append(s.get("text", ""))

        # Tools definitions
        for tool in body.get("tools", []):
            chunks.append(tool.get("name", ""))
            chunks.append(tool.get("description", ""))
            chunks.append(str(tool.get("input_schema", {})))

        # Messages
        for msg in body.get("messages", []):
            content = msg.get("content", "")
            if isinstance(content, str):
                chunks.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, str):
                        chunks.append(block)
                    elif isinstance(block, dict):
                        btype = block.get("type", "")
                        if btype == "tool_result":
                            chunks.append(_extract_text(block.get("content", "")))
                        elif btype == "thinking":
                            chunks.append(block.get("thinking", ""))
                        else:
                            chunks.append(block.get("text", ""))
                            chunks.append(str(block.get("input", "")))

        combined = "\n".join(chunks)
        if _encoding:
            return len(_encoding.encode(combined))
        return max(1, len(combined) // 3)
    except Exception as e:
        _debug(f"  ✗ token estimation failed: {type(e).__name__}: {e}")
        _log(f"  WARN: token estimation failed: {type(e).__name__}: {e}")
        return 0


def _extract_cache_tokens(usage: dict) -> int:
    details = usage.get("prompt_tokens_details") or {}
    if "cached_tokens" in details:
        return details["cached_tokens"]
    if "cached_tokens" in usage:
        return usage["cached_tokens"]
    if "cache_read_input_tokens" in usage:
        return usage["cache_read_input_tokens"]
    return 0


def _elapsed_ms(start_time: float) -> int:
    return int((time.time() - start_time) * 1000)


# ── Shared helpers for endpoint handlers ──────────────────────────

def _get_client_ip(request: Request) -> str:
    """Extract client IP from request, respecting X-Forwarded-For."""
    ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    return ip


async def _try_failover_key(hdrs: dict, err_status: int) -> dict:
    """If 429 and multiple keys configured, return headers for an alternative key. Else return None."""
    if err_status != 429 or len(API_KEYS) <= 1:
        return None
    failed = hdrs.get("Authorization", "").replace("Bearer ", "")
    alt = _find_alternative_key(failed)
    if alt:
        _log(f"  429 on key, retrying with alternative key")
        return _get_auth_headers("openai", entry=alt)
    return None


@app.api_route("/v1/messages", methods=["POST"])
@app.api_route("/anthropic/v1/messages", methods=["POST"])
async def messages(request: Request):
    req_id = _fast_id("msg")
    start_time = time.time()
    client_ip = _get_client_ip(request)

    body_bytes = await request.body()
    if len(body_bytes) > MAX_BODY_SIZE:
        _debug(f"  413: body too large ({len(body_bytes)} bytes)")
        return _anthropic_error(413, f"Request body too large ({len(body_bytes)} bytes, max {MAX_BODY_SIZE})")

    try:
        body = json.loads(body_bytes)
    except Exception:
        _debug(f"  400: invalid JSON body")
        return _anthropic_error(400, "invalid json")

    original_model = body.get("model", "")
    tool_names = _extract_tool_names(body)
    _debug(f"[messages] req_id={req_id} model={original_model!r} tools={tool_names} ip={client_ip}")
    _debug(f"[messages] headers={_sanitize_headers(dict(request.headers))}")
    _debug(f"[messages] body=\n{_truncate(body)}")
    route = _route_for(original_model, tool_names)
    if route is None:
        _debug(f"[messages] ✗ no route found for {original_model!r}")
        available = sorted(MODELS.keys())
        return Response(
            content=json.dumps({"error": f"Model not found: {original_model!r}", "available_models": available}),
            status_code=404, media_type="application/json",
        )
    model_id = route["model"]
    cfg = get_model_config(model_id)
    endpoint = cfg["endpoint"]
    protocol = cfg["protocol"]
    _debug(f"[messages] route: {original_model!r} → {model_id} | {protocol} | endpoint={endpoint}")

    body = dict(body)
    body["model"] = model_id

    # Apply custom route overrides for thinking/effort
    thinking_override = route.get("thinking")
    if thinking_override and thinking_override != "auto":
        if not isinstance(body.get("thinking"), dict):
            body["thinking"] = {}
        body["thinking"]["type"] = thinking_override
    effort_override = route.get("effort")
    if effort_override and effort_override != "auto":
        body["effort"] = effort_override

    # Extract thinking for logging
    thinking = body.get("thinking", {})
    thinking_type = thinking.get("type", "none") if isinstance(thinking, dict) else "none"
    effort = (body.get("effort")
              or (thinking.get("effort") if isinstance(thinking, dict) else None)
              or (body.get("output_config", {}).get("effort") if isinstance(body.get("output_config"), dict) else None)
              or "none")

    _log(f"→ {original_model!r} → {model_id} | {protocol} | stream={body.get('stream', False)} | thinking={thinking_type} | effort={effort} | ip={client_ip}")

    # Circuit breaker check
    if not _cb_should_allow(endpoint):
        _log(f"  CIRCUIT BREAKER OPEN — fast-failing request to {endpoint}")
        return _anthropic_error(503, "Service temporarily unavailable (circuit breaker open)")

    # ── Anthropic pass-through ──────────────────────────────────
    if protocol == "anthropic":
        a_headers = _get_auth_headers("anthropic")
        is_stream = body.get("stream", False)

        if not is_stream:
            # Check cache
            cache_key = _response_cache.make_key(body, body_bytes=body_bytes)
            cached = cache_key and _response_cache.get(cache_key)
            if cached:
                cached_body, cached_headers = cached
                _debug(f"  cache HIT key={cache_key[:16]}… size={len(cached_body)} bytes")
                _log(f"  ← {model_id} | cache HIT")
                return Response(content=cached_body, headers={**cached_headers, "X-Cache": "HIT"}, media_type="application/json")
            _debug(f"  cache MISS key={cache_key[:16] if cache_key else 'none'}…")

            try:
                resp, a_headers = await _do_request_with_retry(endpoint, body, a_headers, "anthropic")
            except UpstreamError as e:
                _debug(f"  ✗ upstream error: {e}")
                return JSONResponse(status_code=e.status_code, content={"error": str(e)})
            account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
            if resp.status_code != 200:
                # Convert 429 → 503 to avoid Claude Code auth window on quota exhaustion
                status = 503 if resp.status_code == 429 else resp.status_code
                msg = "All API keys exhausted (rate limited). Try again later." if resp.status_code == 429 else resp.text[:500]
                _debug(f"  ✗ upstream {resp.status_code} → client {status}: {msg[:300]}")
                await _log_and_save_error(req_id, model_id, original_model, start_time,
                             resp.status_code, resp.text, protocol, is_stream, thinking_type,
                             effort, client_ip, account_alias, tool_names)
                if resp.status_code == 429:
                    return _anthropic_error(503, msg)
                return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
            try:
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            except Exception:
                _debug(f"  ✗ non-JSON response from {endpoint}")
                _log(f"  UPSTREAM DECODE ERROR: non-JSON response from {endpoint}")
                return _anthropic_error(502, "Upstream returned non-JSON response")
            usage = data.get("usage", {})
            req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
            req_out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
            req_cache = usage.get("cache_read_input_tokens", 0)
            _debug(f"  usage: in={req_in} out={req_out} cache={req_cache}")
            _debug(f"  response=\n{_truncate(data)}")
            _update_token_usage(model_id, req_in, req_out, req_cache)
            used = [b["name"] for b in data.get("content", []) if b.get("type") == "tool_use"]
            await _save_and_log_request(req_id, model_id, original_model, start_time,
                         req_in, req_out, req_cache, protocol, is_stream, thinking_type,
                         effort, client_ip, account_alias, tool_names, tools_used=used)
            if cache_key:
                _response_cache.put(cache_key, resp.content, {"Content-Type": "application/json"})
            return Response(content=resp.content, headers={"X-Cache": "MISS"}, media_type="application/json")


        # Estimate input tokens for Anthropic streaming
        est_input = _estimate_input_tokens(body)
        _debug(f"  [stream] est_input={est_input}")
        _update_token_usage(model_id, est_input, 0, 0)

        async def anthropic_stream(headers):
            stream_in = None
            stream_out = stream_cache = 0
            _line_buf = ""
            started = False
            open_blocks = []
            used_tools = []
            stop_reason = "end_turn"
            _handle_429 = _make_stream_retry_loop("anthropic")
            for _attempt in range(2):  # retry once on 429
                _debug(f"  [stream] attempt {_attempt+1}/2")
                try:
                    async with _client.stream("POST", endpoint, json=body, headers=headers) as resp:
                        _debug(f"  [stream] connected status={resp.status_code}")
                        if resp.status_code != 200:
                            headers, should_retry = _handle_429(headers, resp.status_code, _attempt)
                            if should_retry:
                                _debug(f"  [stream] 429 retry, key swapped")
                                continue
                            err = await resp.aread()
                            ak = _alias_for_key(headers.get("x-api-key", ""))
                            _debug(f"  [stream] error {resp.status_code}: {err[:300]}")
                            # Convert 429 → 503 to avoid Claude Code auth window on quota exhaustion
                            err_status = 503 if resp.status_code == 429 else resp.status_code
                            err_msg = "All API keys exhausted (rate limited). Try again later." if resp.status_code == 429 else f"HTTP {resp.status_code}: {err.decode('utf-8', errors='replace')[:200]}"
                            error_payload = {"type": "error", "error": {"type": "api_error",
                                           "message": err_msg}}
                            yield await _stream_error_response(req_id, model_id, original_model, start_time,
                                         err_status, err, protocol, thinking_type, effort,
                                         client_ip, ak, tool_names, error_payload)
                            return
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                            _line_buf += chunk.decode("utf-8", errors="replace")
                            if len(_line_buf) > 1_000_000:
                                _line_buf = _line_buf[-1000:]
                            while "\n" in _line_buf:
                                line, _line_buf = _line_buf.split("\n", 1)
                                line = line.strip()
                                if not line.startswith("data:"):
                                    continue
                                data_str = line[5:].strip()
                                if data_str == "[DONE]":
                                    _debug(f"  [stream] [DONE] received")
                                    continue
                                try:
                                    event = json.loads(data_str)
                                except Exception:
                                    continue
                                etype = event.get("type", "")
                                if etype == "message_start":
                                    usage = event.get("message", {}).get("usage", {})
                                    stream_in = usage.get("input_tokens")
                                    _debug(f"  [stream] message_start: input_tokens={stream_in} cache_read={usage.get('cache_read_input_tokens', 0)}")
                                    started = True
                                    if stream_in is not None:
                                        try:
                                            with _token_lock:
                                                _token_usage[model_id]["input"] -= est_input
                                                _token_usage[model_id]["input"] += stream_in
                                        except Exception:
                                            pass
                                    stream_cache = usage.get("cache_read_input_tokens", 0)
                                    if stream_cache:
                                        try:
                                            with _token_lock:
                                                _token_usage[model_id]["cache"] += stream_cache
                                        except Exception:
                                            pass
                                elif etype == "content_block_start":
                                    block = event.get("content_block", {})
                                    _debug(f"  [stream] content_block_start: type={block.get('type')} name={block.get('name', '')} index={event.get('index')}")
                                    if block.get("type") == "tool_use" and block.get("name"):
                                        used_tools.append(block["name"])
                                    open_blocks.append(event.get("index"))
                                elif etype == "content_block_stop":
                                    idx = event.get("index")
                                    _debug(f"  [stream] content_block_stop: index={idx}")
                                    if idx in open_blocks:
                                        open_blocks.remove(idx)
                                elif etype == "message_delta":
                                    usage = event.get("usage", {})
                                    stream_out = usage.get("output_tokens", 0)
                                    stop_reason = event.get("delta", {}).get("stop_reason", stop_reason)
                                    _debug(f"  [stream] message_delta: stop_reason={stop_reason} output_tokens={stream_out}")
                        # After stream ends, apply final output token count
                        if stream_out:
                            try:
                                with _token_lock:
                                    _token_usage[model_id]["output"] += stream_out
                            except Exception:
                                pass
                except Exception as e:
                    ak = _alias_for_key(headers.get("x-api-key", "")) if headers else ""
                    _debug(f"  [stream] exception on attempt {_attempt+1}: {type(e).__name__}: {e}")
                    _log(f"  ERROR stream (attempt {_attempt+1}): {type(e).__name__}: {e}")
                    if _attempt == 0:
                        # Try alternative key on retry
                        failed_key = _key_from_headers(headers, "anthropic")
                        alt = _find_alternative_key(failed_key)
                        if alt:
                            _debug(f"  ⟳ stream retry with alt key: alias={alt.get('alias', '?')}")
                            _log(f"  Retrying stream with alternative key: {alt.get('alias', '?')}")
                            headers = _get_auth_headers("anthropic", entry=alt)
                        continue
                    if stream_in is None:
                        try:
                            with _token_lock:
                                _token_usage[model_id]["input"] -= est_input
                        except Exception:
                            pass
                    if stream_out:
                        try:
                            with _token_lock:
                                _token_usage[model_id]["output"] += stream_out
                        except Exception:
                            pass
                    await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                                 stream_in if stream_in is not None else est_input, stream_out, stream_cache, success=False, error=str(e),
                                 protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                 client_ip=client_ip, account_alias=ak, tools=tool_names,
                                 tools_used=used_tools if used_tools else None)
                    if started:
                        for idx in open_blocks:
                            yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
                        yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "error"}, "usage": {"output_tokens": stream_out}})
                        yield _sse("message_stop", {"type": "message_stop"})
                    return
                else:
                    # Only reached if no exception and no break (successful stream)
                    break
            else:
                # Exhausted retries without success → error already yielded
                return
            logged_in = stream_in if stream_in is not None else est_input
            _debug(f"  [stream] done: in={logged_in} out={stream_out} cache={stream_cache} tools={used_tools}")
            if stream_in is not None or stream_out:
                ak = _alias_for_key(headers.get("x-api-key", ""))
                await _save_and_log_request(req_id, model_id, original_model, start_time,
                             logged_in, stream_out, stream_cache, protocol, True, thinking_type,
                             effort, client_ip, ak, tool_names, tools_used=used_tools if used_tools else None)

        return StreamingResponse(_sse_keepalive(anthropic_stream(a_headers)), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # ── OpenAI-protocol ─────────────────────────────────────────
    try:
        oai_body = anthropic_to_openai(body, model_id)
        _debug(f"[messages] converted to openai: {_truncate(oai_body, 5000)}")
    except Exception as e:
        _debug(f"[messages] ✗ conversion failed: {e}")
        _log(f"  CONVERSION ERROR: anthropic_to_openai failed: {type(e).__name__}: {e}")
        return _anthropic_error(400, f"Request conversion failed: {e}")
    headers = _get_auth_headers("openai")
    is_stream = oai_body["stream"]

    if not is_stream:
        try:
            resp, headers = await _do_request_with_retry(endpoint, oai_body, headers, "openai")
        except UpstreamError as e:
            _debug(f"  ✗ upstream error: {e}")
            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
        account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
        _debug(f"  response status={resp.status_code} size={len(resp.content)} bytes")
        if resp.status_code != 200:
            await _log_and_save_error(req_id, model_id, original_model, start_time,
                         resp.status_code, resp.text, protocol, is_stream, thinking_type,
                         effort, client_ip, account_alias, tool_names)
            # Convert 429 → 503 to avoid Claude Code auth window on quota exhaustion
            if resp.status_code == 429:
                return _anthropic_error(503, "All API keys exhausted (rate limited). Try again later.")
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {})
                if isinstance(err_msg, dict):
                    err_msg = err_msg.get("message", resp.text[:200])
            except Exception:
                err_msg = resp.text[:200]
            anthro_err = json.dumps({"type": "error", "error": {"type": "api_error", "message": f"HTTP {resp.status_code}: {err_msg}"}},
                                    ensure_ascii=False)
            return Response(content=anthro_err, status_code=resp.status_code, media_type="application/json")
        try:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        except Exception:
            _debug(f"  ✗ non-JSON response from {endpoint}")
            _log(f"  UPSTREAM DECODE ERROR: non-JSON response from {endpoint}")
            return _anthropic_error(502, "Upstream returned non-JSON response")
        usage = data.get("usage", {})
        req_in = usage.get("prompt_tokens", 0)
        req_out = usage.get("completion_tokens", 0)
        cache = _extract_cache_tokens(usage)
        _debug(f"  usage: in={req_in} out={req_out} cache={cache}")
        _update_token_usage(model_id, req_in, req_out, cache)
        used = [tc["function"]["name"] for tc in data.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])]
        msg_data = data.get("choices", [{}])[0].get("message", {})
        if not (msg_data.get("reasoning_content") or msg_data.get("reasoning")) and thinking_type != "none":
            _debug(f"  [non-stream] WARNING: thinking requested (type={thinking_type}, effort={effort}) but upstream returned no reasoning_content")
        _debug(f"  [non-stream] blocks: text={bool(msg_data.get('content'))} thinking={bool(msg_data.get('reasoning_content') or msg_data.get('reasoning'))} tools={used}")
        await _save_and_log_request(req_id, model_id, original_model, start_time,
                     req_in, req_out, cache, protocol, is_stream=False, thinking_type=thinking_type,
                     effort=effort, client_ip=client_ip, account_alias=account_alias, tools=tool_names,
                     tools_used=used if used else None)
        return Response(content=json.dumps(openai_to_anthropic(data, original_model), ensure_ascii=False),
                        media_type="application/json")

    # Streaming
    msg_id = _fast_id("msg")
    oai_body["stream_options"] = {"include_usage": True}

    stream_in_est = _estimate_input_tokens(body)
    _debug(f"  [stream-oai] est_input={stream_in_est}")
    _update_token_usage(model_id, stream_in_est, 0, 0)

    async def stream_gen(hdrs):
        started = False
        open_blocks = []
        text_block_idx = None
        reasoning_block_idx = None
        tool_block_idx = {}
        next_block_idx = 0
        stream_out_tokens = 0
        actual_usage = None
        used_tools = []
        _handle_429 = _make_stream_retry_loop("openai")

        for _attempt in range(2):
            try:
                async with _client.stream("POST", endpoint, json=oai_body, headers=hdrs) as resp:
                    if resp.status_code != 200:
                        hdrs, should_retry = _handle_429(hdrs, resp.status_code, _attempt)
                        if should_retry:
                            continue
                        err = await resp.aread()
                        ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                        # Convert 429 → 503 to avoid Claude Code auth window on quota exhaustion
                        err_status = 503 if resp.status_code == 429 else resp.status_code
                        err_msg = "All API keys exhausted (rate limited). Try again later." if resp.status_code == 429 else f"HTTP {resp.status_code}: {err.decode('utf-8', errors='replace')[:200]}"
                        error_payload = {"type": "error", "error": {"type": "api_error",
                                       "message": err_msg}}
                        yield await _stream_error_response(req_id, model_id, original_model, start_time,
                                     err_status, err, protocol, thinking_type, effort,
                                     client_ip, ak_h, tool_names, error_payload)
                        return

                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()

                        if data == "[DONE]":
                            final_in, final_out, final_cache, log_tag = _finalize_stream_tokens(
                                model_id, stream_in_est, None, stream_out_tokens, 0,
                                actual_usage, _token_usage, _token_lock)
                            if not started:
                                started = True
                                yield _sse("message_start", {"type": "message_start", "message": {
                                    "id": msg_id, "type": "message", "role": "assistant", "content": [],
                                    "model": original_model, "stop_reason": None, "stop_sequence": None,
                                    "usage": {"input_tokens": final_in, "output_tokens": 0, "cache_read_input_tokens": final_cache}}})
                            for idx in open_blocks:
                                yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
                            has_tools = bool(tool_block_idx)
                            _debug(f"  [stream-oai] summary: text={text_block_idx is not None} thinking={reasoning_block_idx is not None} tools={list(tool_block_idx.keys())} stop={'tool_use' if has_tools else 'end_turn'} out_tokens={final_out}")
                            if reasoning_block_idx is None and thinking_type != "none":
                                _debug(f"  [stream-oai] WARNING: thinking requested (type={thinking_type}, effort={effort}) but upstream returned no reasoning_content")
                            yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use" if has_tools else "end_turn"}, "usage": {"output_tokens": final_out, "cache_read_input_tokens": final_cache}})
                            yield _sse("message_stop", {"type": "message_stop"})
                            ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                            await _save_and_log_request(req_id, model_id, original_model, start_time,
                                         final_in, final_out, final_cache, protocol, True, thinking_type,
                                         effort, client_ip, ak_h, tool_names, log_tag,
                                         tools_used=used_tools if used_tools else None)
                            break

                        try:
                            chunk = json.loads(data)
                        except Exception:
                            continue

                        chunk_usage = chunk.get("usage")
                        if chunk_usage and isinstance(chunk_usage, dict):
                            actual_usage = chunk_usage

                        choices = chunk.get("choices", [])
                        if not choices or not isinstance(choices, list):
                            continue
                        first_choice = choices[0] if choices else {}
                        delta = first_choice.get("delta", {}) if isinstance(first_choice, dict) else {}
                        if not delta or not isinstance(delta, dict):
                            delta = {}

                        if not started:
                            started = True
                            yield _sse("message_start", {"type": "message_start", "message": {
                                "id": msg_id, "type": "message", "role": "assistant", "content": [],
                                "model": original_model, "stop_reason": None, "stop_sequence": None,
                                "usage": {"input_tokens": stream_in_est, "output_tokens": 0, "cache_read_input_tokens": 0}}})

                        # Text
                        text = ""
                        c = delta.get("content")
                        if isinstance(c, str):
                            text = c
                        elif isinstance(c, list):
                            text = "".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")

                        if text:
                            if text_block_idx is None:
                                text_block_idx = next_block_idx
                                next_block_idx += 1
                                yield _sse("content_block_start", {"type": "content_block_start", "index": text_block_idx,
                                           "content_block": {"type": "text", "text": ""}})
                                open_blocks.append(text_block_idx)
                            stream_out_tokens += _estimate_tokens(text)
                            yield _sse("content_block_delta", {"type": "content_block_delta", "index": text_block_idx,
                                       "delta": {"type": "text_delta", "text": text}})

                        # Reasoning content
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            if reasoning_block_idx is None:
                                reasoning_block_idx = next_block_idx
                                next_block_idx += 1
                                _debug(f"  [stream-oai] reasoning_content block_start idx={reasoning_block_idx}")
                                yield _sse("content_block_start", {"type": "content_block_start", "index": reasoning_block_idx,
                                           "content_block": {"type": "thinking", "thinking": ""}})
                                open_blocks.append(reasoning_block_idx)
                            stream_out_tokens += _estimate_tokens(reasoning)
                            yield _sse("content_block_delta", {"type": "content_block_delta", "index": reasoning_block_idx,
                                       "delta": {"type": "thinking_delta", "thinking": reasoning}})

                        # Tool calls
                        for tc in (delta.get("tool_calls") or []):
                            api_idx = tc.get("index", 0)
                            if api_idx not in tool_block_idx:
                                block_idx = next_block_idx
                                next_block_idx += 1
                                tool_block_idx[api_idx] = block_idx
                                tc_id = tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}")
                                tc_name = tc.get("function", {}).get("name", "")
                                if tc_name:
                                    used_tools.append(tc_name)
                                _debug(f"  [stream-oai] tool_call block_start idx={block_idx} name={tc_name!r} id={tc_id}")
                                yield _sse("content_block_start", {"type": "content_block_start", "index": block_idx,
                                           "content_block": {"type": "tool_use", "id": tc_id,
                                           "name": tc_name, "input": {}}})
                                open_blocks.append(block_idx)
                            if args := tc.get("function", {}).get("arguments", ""):
                                stream_out_tokens += _estimate_tokens(args)
                                yield _sse("content_block_delta", {"type": "content_block_delta", "index": tool_block_idx[api_idx],
                                           "delta": {"type": "input_json_delta", "partial_json": args}})
            except Exception as e:
                _log(f"  ERROR stream (attempt {_attempt+1}): {type(e).__name__}: {e}")
                _debug(f"  ✗ stream exception: {type(e).__name__}: {e}")
                if _attempt == 0:
                    # Try alternative key on retry (handles rate-limit disguised as disconnect)
                    failed_key = _key_from_headers(hdrs, "openai")
                    alt = _find_alternative_key(failed_key)
                    if alt:
                        _debug(f"  ⟳ stream retry with alt key: alias={alt.get('alias', '?')}")
                        _log(f"  Retrying stream with alternative key: {alt.get('alias', '?')}")
                        hdrs = _get_auth_headers("openai", entry=alt)
                    continue
                try:
                    with _token_lock:
                        _token_usage[model_id]["input"] -= stream_in_est
                except Exception:
                    pass
                ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                             stream_in_est, stream_out_tokens, 0, success=False, error=str(e),
                             protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                             client_ip=client_ip, account_alias=ak_h, tools=tool_names,
                             tools_used=used_tools if used_tools else None)
                if started:
                    for idx in open_blocks:
                        yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
                    yield _sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "error"}, "usage": {"output_tokens": stream_out_tokens}})
                    yield _sse("message_stop", {"type": "message_stop"})
                return
            else:
                break
        else:
            return

    return StreamingResponse(_sse_keepalive(stream_gen(headers)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


_health_cache: tuple[float, bool] | None = None  # (timestamp, upstream_ok)
_health_lock = threading.Lock()

@app.get("/health")
async def health():
    _debug("  [health] health check started")
    await asyncio.to_thread(_conn.execute, "SELECT 1")
    _debug("  [health] DB connectivity OK")

    usage = {model: {"input": d["input"], "output": d["output"], "cache": d["cache"]}
             for model, d in _token_usage.items()}

    # Check circuit breaker status
    cb_status = {}
    any_open = False
    for endpoint, cb in _circuit_breakers.items():
        cb_status[endpoint] = cb.get_status()
        if cb.state == "open":
            any_open = True
    _debug(f"  [health] circuit breakers: {len(_circuit_breakers)} total, any_open={any_open}")

    # Quick upstream connectivity check (5s timeout, cached for 15s)
    global _health_cache
    upstream_ok = True
    now = time.monotonic()
    with _health_lock:
        if _health_cache and (now - _health_cache[0]) < 15.0:
            upstream_ok = _health_cache[1]
            _debug(f"  [health] upstream check (cached): {'ok' if upstream_ok else 'unreachable'}")
        else:
            # Cache miss — perform actual check
            try:
                resp = await _client.get("https://opencode.ai", timeout=5.0)
                upstream_ok = resp.status_code < 500
            except Exception:
                upstream_ok = False
            _health_cache = (now, upstream_ok)
            _debug(f"  [health] upstream check (fresh): {'ok' if upstream_ok else 'unreachable'}")

    status = "ok"
    if not upstream_ok or any_open:
        status = "degraded"

    _debug(f"  [health] overall status={status}")
    return {"status": status, "usage": usage, "upstream": "ok" if upstream_ok else "unreachable",
            "circuit_breakers": cb_status}


@app.get("/api/circuit-breakers")
async def circuit_breakers():
    """Return status of all circuit breakers."""
    return {endpoint: cb.get_status() for endpoint, cb in _circuit_breakers.items()}


@app.post("/api/circuit-breakers/reset")
async def circuit_breakers_reset(request: Request):
    """Reset one or all circuit breakers to closed state.

    Body (JSON, optional):
      - {"endpoint": "https://..."} → reset one
      - {} or no body → reset all
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    target = body.get("endpoint")
    if target:
        cb = _circuit_breakers.get(target)
        if cb is None:
            return _anthropic_error(404, f"No circuit breaker for {target}")
        cb.record_success()
        _log(f"  CIRCUIT BREAKER RESET: {target}")
        return {"reset": target, "status": cb.get_status()}
    # Reset all
    count = 0
    for endpoint, cb in _circuit_breakers.items():
        cb.record_success()
        count += 1
    _log(f"  CIRCUIT BREAKERS RESET: all ({count} endpoints)")
    return {"reset": "all", "count": count}


@app.get("/v1/models")
async def list_models():
    now = int(time.time())
    seen = set()
    data = []
    for model_id in MODELS:
        if model_id not in seen:
            cfg = get_model_config(model_id)
            endpoint = cfg.get("endpoint", "")
            cb = _circuit_breakers.get(endpoint)
            cb_state = cb.state if cb else "unknown"
            usage = _token_usage.get(model_id, {})
            data.append({
                "id": model_id, "object": "model", "created": now, "owned_by": "opencode",
                "status": cb_state,
                "requests": {"input": usage.get("input", 0), "output": usage.get("output", 0), "cache": usage.get("cache", 0)},
            })
            seen.add(model_id)
    for alias in ["gpt-5-codex", "gpt-5", "gpt-4o", "codex", "deepseek-chat"]:
        if alias not in seen:
            data.append({"id": alias, "object": "model", "created": now, "owned_by": "opencode"})
            seen.add(alias)
    return {"object": "list", "data": data, "cache": _response_cache.stats()}


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    try:
        body = json.loads(await request.body())
    except Exception:
        return _anthropic_error(400, "invalid json")
    tokens = _estimate_input_tokens(body)
    return {"input_tokens": tokens}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    req_id = _fast_id("chatcmpl")
    start_time = time.time()
    client_ip = _get_client_ip(request)

    body_bytes = await request.body()
    if len(body_bytes) > MAX_BODY_SIZE:
        _debug(f"  413: body too large ({len(body_bytes)} bytes)")
        return _openai_error(413, f"Request body too large ({len(body_bytes)} bytes, max {MAX_BODY_SIZE})")

    try:
        body = json.loads(body_bytes)
    except Exception:
        _debug(f"  400: invalid JSON body")
        return _openai_error(400, "invalid json")

    original_model = body.get("model", "")
    tool_names = _extract_tool_names(body)
    _debug(f"[chat] req_id={req_id} model={original_model!r} tools={tool_names} ip={client_ip}")
    _debug(f"[chat] headers={_sanitize_headers(dict(request.headers))}")
    _debug(f"[chat] body=\n{_truncate(body)}")
    route = _route_for(original_model, tool_names)
    if route is None:
        _debug(f"[chat] ✗ no route found for {original_model!r}")
        available = sorted(MODELS.keys())
        return JSONResponse(status_code=404, content={
            "error": f"Model not found: {original_model!r}",
            "available_models": available,
        })
    model_id = route["model"]
    cfg = get_model_config(model_id)
    endpoint = cfg["endpoint"]
    protocol = cfg["protocol"]
    _debug(f"[chat] route: {original_model!r} → {model_id} | {protocol} | endpoint={endpoint}")

    body = dict(body)
    body["model"] = model_id
    is_stream = body.get("stream", False)

    thinking_raw = body.get("thinking") if isinstance(body.get("thinking"), dict) else {}
    thinking_type = thinking_raw.get("type", "none") if isinstance(thinking_raw, dict) and thinking_raw else "none"
    effort = (body.get("effort")
              or (thinking_raw.get("effort") if isinstance(thinking_raw, dict) else None)
              or (body.get("output_config", {}).get("effort") if isinstance(body.get("output_config"), dict) else None)
              or "none")

    _log(f"→ {original_model!r} → {model_id} | {protocol} | chat/completions | stream={is_stream} | thinking={thinking_type} | effort={effort} | ip={client_ip}")

    # Circuit breaker check
    if not _cb_should_allow(endpoint):
        _log(f"  CIRCUIT BREAKER OPEN — fast-failing request to {endpoint}")
        return _openai_error(503, "Service temporarily unavailable (circuit breaker open)")

    # ── OpenAI passthrough ─────────────────────────────────────
    if protocol == "openai":
        headers = _get_auth_headers("openai")

        if not is_stream:
            try:
                resp, headers = await _do_request_with_retry(endpoint, body, headers, "openai")
            except UpstreamError as e:
                return JSONResponse(status_code=e.status_code, content={"error": str(e)})
            account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
            if resp.status_code != 200:
                await _log_and_save_error(req_id, model_id, original_model, start_time,
                             resp.status_code, resp.text, protocol, is_stream, thinking_type,
                             effort, client_ip, account_alias, tool_names)
                return Response(content=resp.content, status_code=resp.status_code, media_type="application/json")
            try:
                data = resp.json()
            except Exception:
                _debug(f"  ✗ non-JSON response from {endpoint}")
                _log(f"  UPSTREAM DECODE ERROR: non-JSON response from {endpoint}")
                return _openai_error(502, "Upstream returned non-JSON response")
            usage = data.get("usage", {})
            req_in = usage.get("prompt_tokens", 0)
            req_out = usage.get("completion_tokens", 0)
            cache = _extract_cache_tokens(usage)
            _debug(f"  usage: in={req_in} out={req_out} cache={cache}")
            _update_token_usage(model_id, req_in, req_out, cache)
            used = [tc["function"]["name"] for tc in data.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])]
            await _save_and_log_request(req_id, model_id, original_model, start_time,
                         req_in, req_out, cache, protocol, is_stream, thinking_type,
                         effort, client_ip, account_alias, tool_names, tools_used=used if used else None)
            return Response(content=json.dumps(data, ensure_ascii=False), media_type="application/json")

        # ── OpenAI streaming passthrough ──
        oai_body = dict(body)
        oai_body["stream_options"] = {"include_usage": True}

        est_input = _estimate_input_tokens(body)
        _update_token_usage(model_id, est_input, 0, 0)
        _debug(f"  [chat-stream] est_input={est_input}")

        async def openai_stream(hdrs):
            stream_out = 0
            actual_usage = None
            used_tools = []
            _handle_429 = _make_stream_retry_loop("openai")
            for _attempt in range(2):
                try:
                    async with _client.stream("POST", endpoint, json=oai_body, headers=hdrs) as resp:
                        if resp.status_code != 200:
                            hdrs, should_retry = _handle_429(hdrs, resp.status_code, _attempt)
                            if should_retry:
                                continue
                            err = await resp.aread()
                            ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                            await _log_and_save_error(req_id, model_id, original_model, start_time,
                                         resp.status_code, err, protocol, True, thinking_type,
                                         effort, client_ip, ak_h, tool_names)
                            # Convert 429 → 503 to avoid Claude Code auth window on quota exhaustion
                            err_status = 503 if resp.status_code == 429 else resp.status_code
                            err_msg = "All API keys exhausted (rate limited). Try again later." if resp.status_code == 429 else f"HTTP {resp.status_code}"
                            yield b"data: " + json.dumps({"error": {"message": err_msg}}, ensure_ascii=False).encode() + b"\n\ndata: [DONE]\n\n"
                            return

                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                yield line.encode() + b"\n\n"
                                continue
                            try:
                                chunk = json.loads(data_str)
                            except Exception:
                                yield line.encode() + b"\n\n"
                                continue
                            chunk_usage = chunk.get("usage")
                            if isinstance(chunk_usage, dict):
                                actual_usage = chunk_usage
                            choices = chunk.get("choices", [])
                            if choices and isinstance(choices, list) and len(choices) > 0:
                                delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
                                if isinstance(delta, dict):
                                    c = delta.get("content")
                                    if isinstance(c, str):
                                        stream_out += _estimate_tokens(c)
                                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                                    if isinstance(rc, str):
                                        stream_out += _estimate_tokens(rc)
                                    for tc in delta.get("tool_calls", []):
                                        if isinstance(tc, dict) and "name" in tc.get("function", {}):
                                            used_tools.append(tc["function"]["name"])
                            yield line.encode() + b"\n\n"

                        # Stream ended — finalize tracking
                        final_in, final_out, final_cache, log_tag = _finalize_stream_tokens(
                            model_id, est_input, None, stream_out, 0,
                            actual_usage, _token_usage, _token_lock)
                        ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                        await _save_and_log_request(req_id, model_id, original_model, start_time,
                                     final_in, final_out, final_cache, protocol, True, thinking_type,
                                     effort, client_ip, ak_h, tool_names, log_tag,
                                     tools_used=used_tools if used_tools else None)
                except Exception as e:
                    _log(f"  ERROR stream (attempt {_attempt+1}): {type(e).__name__}: {e}")
                    _debug(f"  ✗ stream exception: {type(e).__name__}: {e}")
                    if _attempt == 0:
                        # Try alternative key on retry
                        failed_key = _key_from_headers(hdrs, "openai")
                        alt = _find_alternative_key(failed_key)
                        if alt:
                            _debug(f"  ⟳ stream retry with alt key: alias={alt.get('alias', '?')}")
                            _log(f"  Retrying stream with alternative key: {alt.get('alias', '?')}")
                            hdrs = _get_auth_headers("openai", entry=alt)
                        continue
                    try:
                        with _token_lock:
                            _token_usage[model_id]["input"] -= est_input
                    except Exception:
                        pass
                    ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                    await _log_and_save_error(req_id, model_id, original_model, start_time,
                                 0, str(e), protocol, True, thinking_type,
                                 effort, client_ip, ak_h, tool_names)
                    return
                else:
                    break
            else:
                return

        return StreamingResponse(_sse_keepalive(openai_stream(headers)), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # ── Anthropic protocol (double conversion) ──────────────────
    anthro_body = openai_to_anthropic_request(body)

    # Apply thinking/effort overrides from route
    thinking_override = route.get("thinking")
    if thinking_override and thinking_override != "auto":
        if not isinstance(anthro_body.get("thinking"), dict):
            anthro_body["thinking"] = {}
        anthro_body["thinking"]["type"] = thinking_override
    effort_override = route.get("effort")
    if effort_override and effort_override != "auto":
        anthro_body["effort"] = effort_override

    thinking = anthro_body.get("thinking", {})
    thinking_type = thinking.get("type", "none") if isinstance(thinking, dict) else "none"
    effort = (anthro_body.get("effort")
              or (thinking.get("effort") if isinstance(thinking, dict) else None)
              or "none")
    a_headers = _get_auth_headers("anthropic")

    if not is_stream:
        try:
            resp, a_headers = await _do_request_with_retry(endpoint, anthro_body, a_headers, "anthropic")
        except UpstreamError as e:
            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
        account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
        if resp.status_code != 200:
            await _log_and_save_error(req_id, model_id, original_model, start_time,
                         resp.status_code, resp.text, protocol, is_stream, thinking_type,
                         effort, client_ip, account_alias, tool_names)
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {}).get("message", resp.text[:200])
            except Exception:
                err_msg = resp.text[:200]
            oai_err = json.dumps({"error": {"message": err_msg, "type": "api_error"}}, ensure_ascii=False)
            return Response(content=oai_err, status_code=resp.status_code, media_type="application/json")

        try:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        except Exception:
            _debug(f"  ✗ non-JSON response from {endpoint}")
            _log(f"  UPSTREAM DECODE ERROR: non-JSON response from {endpoint}")
            return _openai_error(502, "Upstream returned non-JSON response")
        usage = data.get("usage", {})
        req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
        req_out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
        req_cache = usage.get("cache_read_input_tokens", 0)
        _update_token_usage(model_id, req_in, req_out, req_cache)
        used = [b["name"] for b in data.get("content", []) if b.get("type") == "tool_use"]
        await _save_and_log_request(req_id, model_id, original_model, start_time,
                     req_in, req_out, req_cache, protocol, is_stream, thinking_type,
                     effort, client_ip, account_alias, tool_names, tools_used=used if used else None)

        oai_response = anthropic_to_openai_response(data, original_model)
        return Response(content=json.dumps(oai_response, ensure_ascii=False), media_type="application/json")

    # ── Streaming with Anthropic backend (true streaming) ──
    async def _anthro_to_oai_stream(hdrs):
        _id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
        _created = int(time.time())
        started = False
        content_types = {}
        text_data = {}
        thinking_data = {}
        tool_data = {}
        open_blocks = set()
        stream_out = 0
        actual_usage = None
        total_input = 0
        cache_read = 0
        _line_buf = ""
        _handle_429 = _make_stream_retry_loop("anthropic")

        def _chunk(delta_override, finish):
            c = {
                "id": _id, "object": "chat.completion.chunk", "created": _created,
                "model": original_model,
                "choices": [{"index": 0, "delta": delta_override, "finish_reason": finish}],
            }
            return b"data: " + json.dumps(c, ensure_ascii=False).encode() + b"\n\n"

        for _attempt in range(2):
            try:
                async with _client.stream("POST", endpoint, json=anthro_body, headers=hdrs) as resp:
                    if resp.status_code != 200:
                        hdrs, should_retry = _handle_429(hdrs, resp.status_code, _attempt)
                        if should_retry:
                            continue
                        ak = _alias_for_key(hdrs.get("x-api-key", ""))
                        await _log_and_save_error(req_id, model_id, original_model, start_time,
                                     resp.status_code, str(resp.status_code), protocol, True, thinking_type,
                                     effort, client_ip, ak, tool_names)
                        # Convert 429 → 503 to avoid Claude Code auth window on quota exhaustion
                        err_status = 503 if resp.status_code == 429 else resp.status_code
                        err_msg = "All API keys exhausted (rate limited). Try again later." if resp.status_code == 429 else f"HTTP {resp.status_code}"
                        yield b"data: " + json.dumps({"error": {"message": err_msg}}, ensure_ascii=False).encode() + b"\n\ndata: [DONE]\n\n"
                        return

                    async for raw in resp.aiter_bytes():
                        _line_buf += raw.decode("utf-8", errors="replace")
                        if len(_line_buf) > 1_000_000:
                            _line_buf = _line_buf[-1000:]
                        while "\n" in _line_buf:
                            line, _line_buf = _line_buf.split("\n", 1)
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            try:
                                ev = json.loads(data_str)
                            except Exception:
                                continue
                            etype = ev.get("type", "")

                            if etype == "message_start":
                                msg = ev.get("message", {})
                                usage = msg.get("usage", {})
                                total_input = usage.get("input_tokens", 0)
                                cache_read = usage.get("cache_read_input_tokens", 0)
                                started = True
                                yield _chunk({"role": "assistant", "content": ""}, None)

                            elif etype == "content_block_start":
                                idx = ev.get("index")
                                block = ev.get("content_block", {})
                                btype = block.get("type")
                                content_types[idx] = btype
                                open_blocks.add(idx)
                                if btype == "tool_use":
                                    tool_data[idx] = {
                                        "id": block.get("id", f"toolu_{uuid.uuid4().hex[:8]}"),
                                        "name": block.get("name", ""),
                                        "args": "",
                                    }
                                    yield _chunk({
                                        "tool_calls": [{
                                            "index": idx, "id": tool_data[idx]["id"],
                                            "type": "function",
                                            "function": {"name": tool_data[idx]["name"], "arguments": ""},
                                        }]
                                    }, None)

                            elif etype == "content_block_delta":
                                idx = ev.get("index")
                                delta = ev.get("delta", {})
                                dtype = delta.get("type")
                                if dtype == "text_delta":
                                    txt = delta.get("text", "")
                                    text_data[idx] = text_data.get(idx, "") + txt
                                    stream_out += _estimate_tokens(txt)
                                    yield _chunk({"content": txt}, None)
                                elif dtype == "thinking_delta":
                                    th = delta.get("thinking", "")
                                    thinking_data[idx] = thinking_data.get(idx, "") + th
                                    stream_out += _estimate_tokens(th)
                                    yield _chunk({"reasoning_content": th}, None)
                                elif dtype == "input_json_delta":
                                    pj = delta.get("partial_json", "")
                                    if idx in tool_data:
                                        tool_data[idx]["args"] += pj
                                        stream_out += _estimate_tokens(pj)
                                        yield _chunk({
                                            "tool_calls": [{
                                                "index": idx, "id": tool_data[idx]["id"],
                                                "type": "function",
                                                "function": {"name": tool_data[idx]["name"], "arguments": pj},
                                            }]
                                        }, None)

                            elif etype == "content_block_stop":
                                idx = ev.get("index")
                                open_blocks.discard(idx)

                            elif etype == "message_delta":
                                d = ev.get("delta", {})
                                u = ev.get("usage", {})
                                actual_usage = u
                                if u.get("output_tokens"):
                                    stream_out = u["output_tokens"]
                                sr = d.get("stop_reason", "")
                                if sr == "end_turn":
                                    finish = "stop"
                                elif sr == "max_tokens":
                                    finish = "length"
                                elif sr == "tool_use":
                                    finish = "tool_calls"
                                else:
                                    finish = "stop"
                                yield _chunk({}, finish)

                            elif etype == "message_stop":
                                _update_token_usage(model_id, total_input, stream_out, cache_read)
                                ak = _alias_for_key(hdrs.get("x-api-key", ""))
                                used_tools = [v["name"] for v in tool_data.values() if v.get("name")]
                                await _save_and_log_request(req_id, model_id, original_model, start_time,
                                             total_input, stream_out, cache_read, protocol, True, thinking_type,
                                             effort, client_ip, ak, tool_names, tools_used=used_tools if used_tools else None)
                                # Send final usage chunk for OpenAI streaming client
                                total = total_input + stream_out
                                usage_chunk = {
                                    "id": _id, "object": "chat.completion.chunk", "created": _created,
                                    "model": original_model,
                                    "choices": [],
                                    "usage": {
                                        "prompt_tokens": total_input,
                                        "completion_tokens": stream_out,
                                        "total_tokens": total,
                                    }
                                }
                                if cache_read:
                                    usage_chunk["usage"]["prompt_tokens_details"] = {"cached_tokens": cache_read}
                                yield b"data: " + json.dumps(usage_chunk, ensure_ascii=False).encode() + b"\n\n"
                                yield b"data: [DONE]\n\n"
                                return
            except Exception as e:
                _log(f"  ERROR stream (attempt {_attempt+1}): {type(e).__name__}: {e}")
                _debug(f"  ✗ stream exception: {type(e).__name__}: {e}")
                if _attempt == 0:
                    # Try alternative key on retry
                    failed_key = _key_from_headers(hdrs, "anthropic")
                    alt = _find_alternative_key(failed_key)
                    if alt:
                        _debug(f"  ⟳ stream retry with alt key: alias={alt.get('alias', '?')}")
                        _log(f"  Retrying stream with alternative key: {alt.get('alias', '?')}")
                        hdrs = _get_auth_headers("anthropic", entry=alt)
                    continue
                ak = _alias_for_key(hdrs.get("x-api-key", ""))
                await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                             total_input or 0, stream_out, 0, success=False, error=str(e),
                             protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                             client_ip=client_ip, account_alias=ak, tools=tool_names)
                if total_input:
                    try:
                        with _token_lock:
                            _token_usage[model_id]["input"] += total_input
                    except Exception:
                        pass
                if started:
                    yield _chunk({}, "stop")
                    yield b"data: [DONE]\n\n"
                return
            else:
                break
        else:
            return

    return StreamingResponse(_sse_keepalive(_anthro_to_oai_stream(a_headers)), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


@app.post("/v1/responses")
async def responses(request: Request):
    req_id = _fast_id("resp")
    start_time = time.time()
    client_ip = _get_client_ip(request)

    body_bytes = await request.body()
    if len(body_bytes) > MAX_BODY_SIZE:
        _debug(f"  413: body too large ({len(body_bytes)} bytes)")
        return _openai_error(413, f"Request body too large ({len(body_bytes)} bytes, max {MAX_BODY_SIZE})")

    try:
        body = json.loads(body_bytes)
    except Exception:
        _debug(f"  400: invalid JSON body")
        return _openai_error(400, "invalid json")

    body = ensure_min_tokens(body)

    # ── DeepSeek optimizations ────────────────────────────────
    if "deepseek-v4" in body.get("model", ""):
        # Filter tools: keep only those DeepSeek handles well
        basic_tools = {"Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebSearch"}
        if "tools" in body:
            body["tools"] = [t for t in body["tools"] if isinstance(t, dict) and t.get("name") in basic_tools]

    original_model = body.get("model", "")
    tool_names = _extract_tool_names(body)
    route = _route_for(original_model, tool_names)
    if route is None:
        available = sorted(MODELS.keys())
        return JSONResponse(status_code=404, content={
            "error": f"Model not found: {original_model!r}",
            "available_models": available,
        })
    model_id = route["model"]
    cfg = get_model_config(model_id)
    endpoint = cfg["endpoint"]
    protocol = cfg["protocol"]
    is_stream = body.get("stream", False)

    _log(f"→ {original_model!r} → {model_id} | {protocol} | responses | stream={is_stream} | ip={client_ip}")

    # Circuit breaker check
    if not _cb_should_allow(endpoint):
        _log(f"  CIRCUIT BREAKER OPEN — fast-failing request to {endpoint}")
        return _openai_error(503, "Service temporarily unavailable (circuit breaker open)")

    # Convert Responses API → Anthropic format
    anthro_body = openai_responses_to_anthropic(body)
    anthro_body["model"] = model_id

    # Inject system prompt for deepseek to use tools
    if "deepseek-v4" in model_id:
        tool_hint = (
            "IMPORTANT: You are a coding agent with file system access. "
            "When asked to create or modify files, always use the Write tool. "
            "When asked to read files, use Read. For shell commands, use Bash. "
            "DO NOT describe what you will do — just call the appropriate function."
        )
        existing = anthro_body.get("system", "")
        anthro_body["system"] = (tool_hint + "\n\n" + existing) if existing else tool_hint

    # Apply route overrides
    thinking_override = route.get("thinking")
    if thinking_override and thinking_override != "auto":
        if not isinstance(anthro_body.get("thinking"), dict):
            anthro_body["thinking"] = {}
        anthro_body["thinking"]["type"] = thinking_override
    effort_override = route.get("effort")
    if effort_override and effort_override != "auto":
        anthro_body["effort"] = effort_override

    thinking = anthro_body.get("thinking", {})
    thinking_type = thinking.get("type", "none") if isinstance(thinking, dict) else "none"
    effort = (anthro_body.get("effort")
              or (thinking.get("effort") if isinstance(thinking, dict) else None)
              or "none")

    # ── Anthropic backend (passthrough) ─────────────────────
    if protocol == "anthropic":
        a_headers = _get_auth_headers("anthropic")
        if not is_stream:
            try:
                resp, a_headers = await _do_request_with_retry(endpoint, anthro_body, a_headers, "anthropic")
            except UpstreamError as e:
                return JSONResponse(status_code=e.status_code, content={"error": str(e)})
            account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
            if resp.status_code != 200:
                await _log_and_save_error(req_id, model_id, original_model, start_time,
                             resp.status_code, resp.text, protocol, is_stream, thinking_type,
                             effort, client_ip, account_alias, tool_names)
                try:
                    err_data = resp.json()
                    err_msg = err_data.get("error", {}).get("message", resp.text[:200])
                except Exception:
                    err_msg = resp.text[:200]
                return Response(content=json.dumps({"error": {"message": err_msg}}), status_code=resp.status_code, media_type="application/json")
            try:
                data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            except Exception:
                _debug(f"  ✗ non-JSON response from {endpoint}")
                _log(f"  UPSTREAM DECODE ERROR: non-JSON response from {endpoint}")
                return _openai_error(502, "Upstream returned non-JSON response")
            usage = data.get("usage", {})
            req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
            req_out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
            req_cache = usage.get("cache_read_input_tokens", 0)
            _update_token_usage(model_id, req_in, req_out, req_cache)
            used = [b["name"] for b in data.get("content", []) if b.get("type") == "tool_use"]
            await _save_and_log_request(req_id, model_id, original_model, start_time,
                         req_in, req_out, req_cache, protocol, is_stream, thinking_type,
                         effort, client_ip, account_alias, tool_names, tools_used=used if used else None)
            oai_resp = anthropic_to_openai_responses(data, original_model)
            return Response(content=json.dumps(oai_resp, ensure_ascii=False), media_type="application/json")
        # Anthropic streaming → collect, then emit SSE
        anthro_body["stream"] = False
        try:
            resp, a_headers = await _do_request_with_retry(endpoint, anthro_body, a_headers, "anthropic")
        except UpstreamError as e:
            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
        account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
        if resp.status_code != 200:
            await _log_and_save_error(req_id, model_id, original_model, start_time,
                         resp.status_code, resp.text, protocol, True, thinking_type,
                         effort, client_ip, account_alias, tool_names)
            async def err_stream():
                yield b"data: [DONE]\n\n"
            return StreamingResponse(err_stream(), media_type="text/event-stream",
                                     headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
        try:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        except Exception:
            _debug(f"  ✗ non-JSON response from {endpoint}")
            _log(f"  UPSTREAM DECODE ERROR: non-JSON response from {endpoint}")
            return _openai_error(502, "Upstream returned non-JSON response")
        usage = data.get("usage", {})
        req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
        req_out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
        req_cache = usage.get("cache_read_input_tokens", 0)
        _update_token_usage(model_id, req_in, req_out, req_cache)
        used = [b["name"] for b in data.get("content", []) if b.get("type") == "tool_use"]
        await _save_and_log_request(req_id, model_id, original_model, start_time,
                     req_in, req_out, req_cache, protocol, True, thinking_type,
                     effort, client_ip, account_alias, tool_names, tools_used=used if used else None)
        oai_resp = anthropic_to_openai_responses(data, original_model)
        payload = json.dumps({"type": "response.completed", "response": oai_resp}, ensure_ascii=False)
        sse_body = f"data: {payload}\n\ndata: [DONE]\n\n".encode()
        return Response(content=sse_body, media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # ── OpenAI backend (double conversion) ──────────────────
    # Convert Anthropic → Chat Completions for the backend
    try:
        oai_body = anthropic_to_openai(anthro_body, model_id)
    except Exception as e:
        _debug(f"[responses] ✗ conversion failed: {e}")
        _log(f"  CONVERSION ERROR: anthropic_to_openai failed: {type(e).__name__}: {e}")
        return _openai_error(400, f"Request conversion failed: {e}")
    headers = _get_auth_headers("openai")
    is_stream = oai_body["stream"]

    if not is_stream:
        try:
            resp, headers = await _do_request_with_retry(endpoint, oai_body, headers, "openai")
        except UpstreamError as e:
            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
        account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
        if resp.status_code != 200:
            await _log_and_save_error(req_id, model_id, original_model, start_time,
                         resp.status_code, resp.text, protocol, is_stream, thinking_type,
                         effort, client_ip, account_alias, tool_names)
            try:
                err_data = resp.json()
                err_msg = err_data.get("error", {}).get("message", resp.text[:200])
            except Exception:
                err_msg = resp.text[:200]
            return Response(content=json.dumps({"error": {"message": err_msg}}), status_code=resp.status_code, media_type="application/json")
        try:
            data = resp.json()
        except Exception:
            _debug(f"  ✗ non-JSON response from {endpoint}")
            _log(f"  UPSTREAM DECODE ERROR: non-JSON response from {endpoint}")
            return _openai_error(502, "Upstream returned non-JSON response")
        usage = data.get("usage", {})
        req_in = usage.get("prompt_tokens", 0)
        req_out = usage.get("completion_tokens", 0)
        cache = _extract_cache_tokens(usage)
        _update_token_usage(model_id, req_in, req_out, cache)
        used = [tc["function"]["name"] for tc in data.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])]
        await _save_and_log_request(req_id, model_id, original_model, start_time,
                     req_in, req_out, cache, protocol, is_stream, thinking_type,
                     effort, client_ip, account_alias, tool_names, tools_used=used if used else None)
        # Direct conversion: Chat Completions → Responses (no intermediate Anthropic format)
        oai_resp = openai_chat_to_responses(data, original_model)
        return Response(content=json.dumps(oai_resp, ensure_ascii=False), media_type="application/json")

    # ── Streaming (OpenAI backend) — collect, then emit SSE ──
    oai_body["stream"] = False
    try:
        resp, headers = await _do_request_with_retry(endpoint, oai_body, headers, "openai")
    except UpstreamError as e:
        return JSONResponse(status_code=e.status_code, content={"error": str(e)})
    account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
    if resp.status_code != 200:
        await _log_and_save_error(req_id, model_id, original_model, start_time,
                     resp.status_code, resp.text, protocol, True, thinking_type,
                     effort, client_ip, account_alias, tool_names)
        async def err_stream():
            yield b"data: [DONE]\n\n"
        return StreamingResponse(err_stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
    try:
        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    except Exception:
        _debug(f"  ✗ non-JSON response from {endpoint}")
        _log(f"  UPSTREAM DECODE ERROR: non-JSON response from {endpoint}")
        return _openai_error(502, "Upstream returned non-JSON response")
    usage = data.get("usage", {})
    req_in = usage.get("prompt_tokens", 0)
    req_out = usage.get("completion_tokens", 0)
    cache = _extract_cache_tokens(usage)
    _update_token_usage(model_id, req_in, req_out, cache)
    used = [tc["function"]["name"] for tc in data.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])]
    await _save_and_log_request(req_id, model_id, original_model, start_time,
                 req_in, req_out, cache, protocol, True, thinking_type,
                 effort, client_ip, account_alias, tool_names, tools_used=used if used else None)
    # Direct conversion: Chat Completions → Responses (no intermediate Anthropic format)
    oai_resp = openai_chat_to_responses(data, original_model)
    # Return as single SSE block (data-only format)
    payload = json.dumps({"type": "response.completed", "response": oai_resp}, ensure_ascii=False)
    sse_body = f"data: {payload}\n\ndata: [DONE]\n\n".encode()
    return Response(content=sse_body, media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


class ServerManager:
    """Manages uvicorn server lifecycle for start/stop/restart."""

    def __init__(self, app, host, port, web_port):
        self.app = app
        self.host = host
        self.port = port
        self.web_port = web_port
        self._server = None
        self._thread = None
        self._lock = threading.Lock()
        self.is_running = False

    def start(self):
        from uvicorn import Config, Server
        with self._lock:
            if self.is_running:
                _debug(f"  [server] start skipped — already running on {self.host}:{self.port}")
                return
            h = RichLogHandler()
            for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
                lg = logging.getLogger(name)
                lg.handlers = [h]
                lg.propagate = False

            config = Config(self.app, host=self.host, port=self.port, log_level="info", log_config=None,
                            timeout_keep_alive=300)
            self._server = Server(config)
            self._thread = threading.Thread(target=self._server.run, daemon=True)
            self._thread.start()

            # Wait for server to actually start listening
            for _ in range(50):
                time.sleep(0.1)
                if self._server.started:
                    break
            self.is_running = True
            _debug(f"  [server] started on {self.host}:{self.port} (web_port={self.web_port})")

    def stop(self, timeout=10):
        """Graceful stop: signal uvicorn to stop, then wait for in-flight requests."""
        with self._lock:
            if not self.is_running:
                _debug(f"  [server] stop skipped — not running")
                return
            _debug(f"  [server] stopping on {self.host}:{self.port}...")
            if self._server:
                self._server.should_exit = True
            self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._server = None
        self._thread = None
        _debug(f"  [server] stopped")

    def restart(self, port=None, web_port=None, host=None):
        """Hot-restart: graceful stop + update host/ports + start."""
        _debug(f"  [server] restart requested: host={host} port={port} web_port={web_port}")
        self.stop()
        if host is not None:
            self.host = host
        if port is not None:
            self.port = int(port)
        if web_port is not None:
            self.web_port = int(web_port)
        self.start()
        _debug(f"  [server] restarted on {self.host}:{self.port}")

    def full_restart(self):
        """Full process restart — re-executes Python to reload all code.

        Uses os.execv() to replace the current process in-place,
        avoiding zombie child processes from subprocess.Popen + os._exit.
        """
        import sys
        _log("Full restart: replacing process...")
        # Flush all output before exec
        for stream in (sys.stdout, sys.stderr):
            stream.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)


if __name__ == "__main__":
    import sys

    use_gui = "--gui" in sys.argv

    mgr = ServerManager(app, HOST, PORT, WEB_PORT)
    _server_manager = mgr
    mgr.start()

    _log(f"API: http://localhost:{PORT}")

    if use_gui:
        try:
            from gui import run_gui
        except ImportError:
            print("GUI dependencies not installed. Run: pip install pystray Pillow pywebview")
            sys.exit(1)
        run_gui(mgr, HOST, PORT, WEB_PORT)
    else:
        run_terminal_loop(ROUTES, _token_usage, _token_lock)
