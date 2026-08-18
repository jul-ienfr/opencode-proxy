"""
Claude Code Proxy → opencode.ai
Convert Anthropic /v1/messages ↔ OpenAI chat/completions
"""

import json
import uuid
import time
import logging
import os
import re
import sqlite3
import threading
import traceback
import asyncio
import contextvars
import yaml
from collections import OrderedDict
from contextlib import asynccontextmanager
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse as StarletteJSONResponse
from starlette.requests import ClientDisconnect

from config import API_KEY, PROXY, MODELS, ROUTES, get_model_config, HOST, PORT, WEB_PORT, DISABLE_MAPPING, API_KEYS, API_KEY_ROUTING, CUSTOM_ROUTES, maybe_reload_custom_routes, CACHE_MIN_PROMPT_SIZE, DEBUG, yaml_get, API_BASE_FREE, FREE_MODEL_MAP, IP_ROTATION
import config.settings as _cfg_settings

import itertools
import email.utils
import datetime

# ── API key routing ──
_key_cycle = None
_key_cycle_keys = []
_key_failover_index = 0
_key_cycle_lock = threading.Lock()

def _get_enabled_keys() -> list[dict]:
    return [k for k in API_KEYS if k.get("enabled", True)]

def _env_key_or_raise() -> dict:
    """Return the .env fallback key, or raise AllKeysPausedError if paused.

    [CRITIC(9)] The .env fallback must not be hammered while paused: when
    every routed key is paused AND the .env key itself is paused, raise so
    the caller surfaces a clean retry-after instead of sending a request
    with a known-dead key.
    """
    if _key_pauser.is_paused(API_KEY):
        remaining = _key_pauser.remaining(API_KEY)
        _debug(f"  [apikey] .env fallback key paused ({remaining:.0f}s) — raising AllKeysPausedError")
        raise AllKeysPausedError(remaining if remaining > 0 else 1)
    return {"api_key": API_KEY}

def get_next_api_key() -> dict:
    global _key_cycle, _key_cycle_keys, _key_failover_index
    if not API_KEYS:
        _debug(f"  [apikey] no API_KEYS configured, falling back to .env key")
        return _env_key_or_raise()
    enabled = _get_enabled_keys()
    if not enabled:
        _debug(f"  [apikey] no enabled keys, falling back to .env key")
        return _env_key_or_raise()

    # Filter out paused keys
    available = [k for k in enabled if not _key_pauser.is_paused(k.get("api_key", ""))]

    if not available:
        # All paused — raise with shortest wait time instead of reusing a paused key
        min_rem = min(
            (_key_pauser.remaining(k.get("api_key", "")) for k in enabled),
            default=0
        )
        if min_rem > 0:
            _debug(f"  [apikey] ALL keys paused, min remaining={min_rem:.0f}s — raising AllKeysPausedError")
            raise AllKeysPausedError(min_rem)
        _debug(f"  [apikey] no keys available, falling back to .env key")
        return _env_key_or_raise()

    if len(available) == 1:
        _debug(f"  [apikey] single available key: alias={available[0].get('alias','?')}")
        return available[0]

    if API_KEY_ROUTING == "failover":
        for i in range(len(API_KEYS)):
            idx = (_key_failover_index + i) % len(API_KEYS)
            if API_KEYS[idx].get("enabled", True) and not _key_pauser.is_paused(API_KEYS[idx].get("api_key", "")):
                _debug(f"  [apikey] failover selected alias={API_KEYS[idx].get('alias','?')} (idx={idx})")
                return API_KEYS[idx]
        # Fallback to shortest-paused
        min_rem = min(
            (_key_pauser.remaining(k.get("api_key", "")) for k in enabled),
            default=0
        )
        if min_rem > 0:
            raise AllKeysPausedError(min_rem)
        _debug(f"  [apikey] failover exhausted, falling back to .env key")
        return _env_key_or_raise()

    # Round-robin: rebuild cycle from available keys only
    with _key_cycle_lock:
        current_ids = [k.get("api_key") for k in available]
        if _key_cycle is None or _key_cycle_keys != current_ids:
            _key_cycle = itertools.cycle(available)
            _key_cycle_keys = current_ids
            _debug(f"  [apikey] round-robin cycle rebuilt: {len(available)} available keys (filtered from {len(enabled)} enabled)")
        selected = next(_key_cycle)
        _debug(f"  [apikey] round-robin selected alias={selected.get('alias','?')}")
        return selected

def _find_alternative_key(failed_key: str) -> dict | None:
    """Return the first enabled, non-paused key different from failed_key, or None."""
    for k in API_KEYS:
        if k.get("api_key") != failed_key and k.get("enabled", True):
            if not _key_pauser.is_paused(k.get("api_key", "")):
                _debug(f"  [apikey] alternative key found alias={k.get('alias','?')}")
                return k
    _debug(f"  [apikey] no alternative key for {failed_key[:8]}...")
    return None

_key_alias_cache: dict[str, str] = {}


def _rebuild_key_cache():
    """Rebuild the API key → alias lookup dict."""
    global _key_alias_cache
    _key_alias_cache = {k["api_key"]: k.get("alias", "") or "" for k in API_KEYS if k.get("api_key")}


def _alias_for_key(api_key: str) -> str:
    """Look up the alias for a given API key. O(1) via dict cache."""
    return _key_alias_cache.get(api_key, "")


# ── Key pause tracker ─────────────────────────────────────────

class _KeyPauser:
    """Per-key rate limit pause tracker. Pauses a key when upstream returns 429.

    Persists pause state to logs/paused_keys.yaml so pauses survive reboots.
    """

    _PAUSED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "paused_keys.yaml")

    def __init__(self, max_pause: float = None):
        if max_pause is None:
            max_pause = float(yaml_get("key_pause", "max_pause", 600))
        self._paused: dict[str, float] = {}   # key_prefix -> monotonic expiry
        self._reasons: dict[str, str] = {}    # key_prefix -> reason string
        self._lock = threading.Lock()
        self._max_pause = max_pause

    @staticmethod
    def _prefix(api_key: str) -> str:
        """Use first 12 chars as key identifier (safe for logging, unique enough)."""
        return api_key[:12] if len(api_key) >= 12 else api_key

    def _save(self):
        """Persist current pause state to YAML file (wall clock times).

        File I/O is offloaded to a thread pool so it doesn't block the event loop.
        Data serialization happens synchronously (fast, in-memory only).
        """
        try:
            data = {}
            for prefix, mono_expiry in self._paused.items():
                remaining = mono_expiry - time.monotonic()
                if remaining > 0:
                    wall_expiry = time.time() + remaining
                    data[prefix] = {
                        "expiry": wall_expiry,
                        "reason": self._reasons.get(prefix, ""),
                    }
            # Offload file I/O to thread pool (non-blocking)
            payload = {"paused_keys": data}
            file_path = self._PAUSED_FILE
            def _write_yaml():
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                with open(file_path, "w", encoding="utf-8") as f:
                    yaml.dump(payload, f, default_flow_style=False)
            try:
                loop = asyncio.get_running_loop()
                loop.run_in_executor(None, _write_yaml)
            except RuntimeError:
                _write_yaml()  # Fallback: sync if no event loop running
        except Exception as e:
            _debug(f"  [keypauser] save error: {e}")

    def load(self, api_keys: list):
        """Load persisted pause state from YAML (called once at startup).

        Converts wall clock expiry → monotonic expiry so is_paused() works.
        Expired entries are silently dropped.
        """
        try:
            if not os.path.exists(self._PAUSED_FILE):
                return
            with open(self._PAUSED_FILE, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            entries = raw.get("paused_keys", {})
            if not entries:
                return
            now_wall = time.time()
            now_mono = time.monotonic()
            loaded = 0
            for prefix, info in entries.items():
                wall_expiry = info.get("expiry", 0)
                if wall_expiry <= now_wall:
                    continue  # already expired
                remaining = wall_expiry - now_wall
                mono_expiry = now_mono + remaining
                with self._lock:
                    self._paused[prefix] = mono_expiry
                    self._reasons[prefix] = info.get("reason", "")
                loaded += 1
            if loaded:
                _debug(f"  [keypauser] loaded {loaded} persisted pauses from disk")
                _log(f"  KEY PAUSER: restored {loaded} pauses from disk")
        except Exception as e:
            _debug(f"  [keypauser] load error: {e}")

    def pause_key(self, api_key: str, duration: float, reason: str = "", quota_based: bool = False):
        """Pause a key for `duration` seconds from now.

        Only quota_based pauses (auto-computed reset times) are capped at
        max_pause — the quota estimate can be wrong (e.g. a free-endpoint
        429 misattributed to a paid key), and a wrong 24 h pause is worse
        than a short one. Explicit 401/403 durations (revoked/blocked keys)
        are honored in full: a revoked key never recovers, so capping its
        pause only creates churn.
        """
        prefix = self._prefix(api_key)
        if quota_based:
            duration = min(duration, self._max_pause)
        expiry = time.monotonic() + duration
        with self._lock:
            existing = self._paused.get(prefix, 0)
            if expiry > existing:  # only extend, never shorten
                self._paused[prefix] = expiry
                self._reasons[prefix] = reason
                self._save()
        alias = _alias_for_key(api_key)
        _debug(f"  [keypauser] PAUSED alias={alias} prefix={prefix} for {duration:.0f}s reason={reason}")
        _log(f"  KEY PAUSED: alias={alias} for {duration:.0f}s ({reason})")

    def is_paused(self, api_key: str) -> bool:
        """Check if a key is currently paused (and not yet expired)."""
        prefix = self._prefix(api_key)
        with self._lock:
            expiry = self._paused.get(prefix, 0)
            if expiry > 0 and time.monotonic() < expiry:
                return True
            if expiry > 0:
                del self._paused[prefix]
                self._reasons.pop(prefix, None)
        return False

    def remaining(self, api_key: str) -> float:
        """Return seconds remaining on pause, or 0 if not paused."""
        prefix = self._prefix(api_key)
        with self._lock:
            expiry = self._paused.get(prefix, 0)
            if expiry > 0:
                rem = expiry - time.monotonic()
                if rem > 0:
                    return rem
                del self._paused[prefix]
                self._reasons.pop(prefix, None)
        return 0.0

    def best_available(self, keys: list) -> dict | None:
        """Among keys, return the one with shortest remaining pause.

        Returns None if any key is fully available (meaning normal selection
        should proceed). Caller uses None to mean 'use normal selection'.
        """
        best = None
        best_remaining = float("inf")
        for k in keys:
            if not self.is_paused(k.get("api_key", "")):
                return None  # at least one key is available
            rem = self.remaining(k.get("api_key", ""))
            if rem < best_remaining:
                best_remaining = rem
                best = k
        return best

    def get_all_status(self) -> dict:
        """Return status of all paused keys (for dashboard/health endpoint)."""
        now = time.monotonic()
        with self._lock:
            status = {}
            expired = []
            for prefix, expiry in self._paused.items():
                remaining = expiry - now
                if remaining <= 0:
                    expired.append(prefix)
                    continue
                status[prefix] = {
                    "remaining_seconds": round(remaining, 1),
                    "reason": self._reasons.get(prefix, ""),
                }
            for prefix in expired:
                del self._paused[prefix]
                self._reasons.pop(prefix, None)
        return status

    def cleanup_expired(self):
        """Remove all expired entries. Called periodically."""
        now = time.monotonic()
        with self._lock:
            expired = [k for k, v in self._paused.items() if v <= now]
            for k in expired:
                del self._paused[k]
                self._reasons.pop(k, None)
            if expired:
                self._save()
        if expired:
            _debug(f"  [keypauser] cleanup: {len(expired)} expired pauses removed")

    def unpause_if_paused(self, api_key: str) -> bool:
        """Remove a pause for a key if it exists. Returns True if removed."""
        prefix = self._prefix(api_key)
        with self._lock:
            if prefix in self._paused:
                del self._paused[prefix]
                self._reasons.pop(prefix, None)
                self._save()
                alias = _alias_for_key(api_key)
                _debug(f"  [keypauser] UNPAUSED alias={alias} prefix={prefix} (recovered)")
                _log(f"  KEY UNPAUSED: alias={alias} (recovered)")
                return True
        return False


_key_pauser = _KeyPauser()


def on_workspace_recovered(workspace_id: str):
    """Called by quota fetcher when a workspace returns to healthy status.

    Unpauses the API key associated with this workspace so it can be reused.
    """
    for k in API_KEYS:
        if k.get("go_workspace_id") == workspace_id:
            _key_pauser.unpause_if_paused(k.get("api_key", ""))
            break


async def _pause_key_for_quota_reset(api_key: str):
    """On 429, fetch fresh quotas for this key's workspace and pause until reset.

    Finds which rolling quota is at 100%, calculates the exact reset time,
    and pauses the key for that duration. The key auto-re-enables at reset.
    """
    try:
        from dashboard.quota import fetch_quotas, get_configured_workspaces
        # Find workspace for this key
        ws_entry = None
        for k in API_KEYS:
            if k.get("api_key") == api_key and k.get("go_workspace_id") and k.get("go_auth_cookie"):
                ws_entry = k
                break
        if not ws_entry:
            return  # No workspace configured for this key, can't fetch quotas

        wid = ws_entry["go_workspace_id"]
        cookie = ws_entry["go_auth_cookie"]
        quotas = await fetch_quotas(wid, cookie)

        # Find which quota window is exhausted — prefer rolling (5h window)
        for window in ("rolling", "weekly", "monthly"):
            q = quotas.get(window, {})
            usage = q.get("usage_percent", 0)
            reset_sec = q.get("reset_in_sec", 0)
            if usage >= 95 and reset_sec > 0:
                alias = _alias_for_key(api_key)
                re_enable_at = time.time() + reset_sec
                re_enable_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(re_enable_at))
                _key_pauser.pause_key(api_key, reset_sec,
                                      f"quota {window} {usage:.0f}% → réactivation {re_enable_str}",
                                      quota_based=True)
                _log(f"  QUOTA {window.upper()} AT {usage:.0f}% — key {alias} paused until {re_enable_str} (in {reset_sec:.0f}s)")
                return

        # No quota at 100% — use default pause
        _default_pause = float(yaml_get("key_pause", "default_pause", 60))
        _key_pauser.pause_key(api_key, _default_pause, "429 (no quota at 100%)")
    except Exception as e:
        _debug(f"  [quota] failed to fetch quotas for 429 pause: {e}")
        # Fallback to default pause
        _default_pause = float(yaml_get("key_pause", "default_pause", 60))
        _key_pauser.pause_key(api_key, _default_pause, "429 (quota fetch failed)")


def _key_from_headers(headers: dict, protocol: str) -> str:
    """Extract the API key from request headers."""
    if protocol == "anthropic":
        return headers.get("x-api-key", "")
    return headers.get("Authorization", "").replace("Bearer ", "")

def _workspace_for_key(api_key: str) -> str:
    """Look up workspace ID for an API key."""
    for k in API_KEYS:
        if k.get("api_key") == api_key:
            return k.get("go_workspace_id", "unknown")
    return "unknown"

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
from dashboard.display import log as _log, debug as _debug, set_debug_log_file, attach_module_logger, attach_panel_logger, RichLogHandler, run_terminal_loop
from dashboard.events import get_event_manager

# Call after all imports are resolved (requires _debug for logging)
_rebuild_key_cache()
_debug(f"  [apikey] rebuilt alias cache: {len(_key_alias_cache)} keys")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")
LOG_DIR = os.path.join(BASE_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Debug log file setup
set_debug_log_file(os.path.join(LOG_DIR, "debug.log"))
if DEBUG:
    _debug("Debug mode enabled — full request/response logging active")
# [36] G7 — VPN rotation failures must never be silent: attach the
# vpn_manager / free_ip_pool loggers to debug.log even when DEBUG is off
# (they fail exactly when the tunnel is down — the moments the trace
# matters most). Handlers are rotation-aware (see dashboard.display).
attach_module_logger("vpn_manager")
attach_module_logger("free_ip_pool")

# Request body size limit (from YAML config, default 10 MB)
MAX_BODY_SIZE = yaml_get("upstream", "max_body_size", 10 * 1024 * 1024)
MAX_BODY_STORAGE = 100_000  # Max chars stored per request/response body in DB


def _truncate_body_for_storage(body: dict, max_chars: int = MAX_BODY_STORAGE) -> str | None:
    """Serialize body to JSON, truncating messages array if needed to stay under max_chars.

    Keeps model, tools, and a summary of messages to preserve context while
    avoiding the memory waste of serializing a 10MB body just to keep 100K.

    Optimized: builds truncated version first, only falls back to full
    serialization if the truncated version is small enough.
    """
    if not body:
        return None
    # Quick size estimate: sum of string lengths of non-messages fields
    # This avoids full json.dumps for large bodies
    estimate = sum(len(str(v)) for k, v in body.items() if k != "messages")
    messages = body.get("messages", [])
    if messages:
        # Estimate first 2 messages + truncation marker
        for msg in messages[:2]:
            estimate += len(str(msg))
        estimate += 80  # truncation marker overhead

    if estimate <= max_chars:
        # Likely fits — do full serialization (single pass)
        full = json.dumps(body, ensure_ascii=False, separators=(',', ':'))
        if len(full) <= max_chars:
            return full
        # Fell through: full was too big, build truncated version
    # Build truncated version (skip full serialization of large body)
    truncated = {k: v for k, v in body.items() if k != "messages"}
    if messages:
        truncated["messages"] = messages[:2] + [{"_truncated": True, "original_count": len(messages)}]
    result = json.dumps(truncated, ensure_ascii=False, separators=(',', ':'))
    if len(result) > max_chars:
        result = result[:max_chars]
    return result

# SQLite setup — synchronous connection, all DB ops run in thread pool
_db_path = os.path.join(LOG_DIR, "requests.db")
_conn = sqlite3.connect(_db_path, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute(f"PRAGMA busy_timeout={yaml_get('database', 'busy_timeout', 5000)}")
_conn.execute("PRAGMA synchronous=NORMAL")       # WAL+NORMAL: safe crash-resilient, 50-90% fewer fsyncs
_conn.execute(f"PRAGMA cache_size=-{yaml_get('database', 'cache_size', 64000)}")  # page cache
_conn.execute("PRAGMA temp_store=MEMORY")        # temp tables in RAM
_conn.execute(f"PRAGMA mmap_size={yaml_get('database', 'mmap_size', 268435456)}")  # memory-mapped I/O
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
_conn.execute("CREATE INDEX IF NOT EXISTS idx_free_ip ON requests(free_model_ip)")
for col, default in [("protocol", "NULL"), ("is_stream", "0"), ("thinking", "NULL"), ("effort", "NULL"), ("client_ip", "NULL"), ("account_alias", "NULL"), ("tools", "NULL"), ("tools_used", "NULL"), ("request_body", "NULL"), ("response_body", "NULL"), ("client_user_agent", "NULL"), ("free_model_ip", "NULL"), ("identity", "NULL")]:
    try:
        _conn.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT DEFAULT {default}")
    except Exception:
        pass
_conn.commit()
_debug(f"  [db] SQLite connection established: {_db_path}")

# [30] Canary: mixed naive/UTC timestamps break ORDER BY timestamp DESC (BINARY
# collation) — warn the operator that scripts/migrate_timestamps_utc.py is pending.
try:
    _naive = _conn.execute("SELECT COUNT(*) FROM requests WHERE timestamp NOT LIKE '%Z'").fetchone()[0]
    if _naive:
        _log(f"  WARNING: {_naive} requests row(s) with naive timestamps (mixed local/UTC) — run scripts/migrate_timestamps_utc.py")
except Exception:
    pass


# ── Free model usage tracking ──
_conn.execute("""
    CREATE TABLE IF NOT EXISTS free_model_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        paid_model TEXT NOT NULL,
        free_model TEXT NOT NULL,
        api_key TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        status INTEGER NOT NULL,
        tokens_input INTEGER DEFAULT 0,
        tokens_output INTEGER DEFAULT 0,
        duration_ms INTEGER DEFAULT 0,
        ip TEXT DEFAULT ''
    )
""")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_free_ts ON free_model_usage(timestamp)")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_free_model ON free_model_usage(free_model)")
_conn.execute("CREATE INDEX IF NOT EXISTS idx_free_key ON free_model_usage(api_key)")
try:
    _conn.execute("ALTER TABLE free_model_usage ADD COLUMN ip TEXT DEFAULT ''")
except Exception:
    pass  # Column already exists
_conn.commit()
_debug(f"  [db] free_model_usage table ready")


_db_pending_inserts = 0
_db_last_commit = time.monotonic()
_DB_COMMIT_INTERVAL = yaml_get("database", "commit_interval", 5)   # seconds between periodic commits
_DB_COMMIT_BATCH = yaml_get("database", "commit_batch", 10)       # force commit after N inserts
_db_commit_lock = threading.Lock()

# Context variable to pass client user-agent from endpoint handlers to _save_request
# without threading it through every intermediate function call.
_current_user_agent: contextvars.ContextVar[str] = contextvars.ContextVar('_current_user_agent', default=None)

# Context variable carrying the free-channel attempt (egress IP + identity profile)
# from the two places a free request actually leaves (_try_free_model_first,
# _open_free_stream) to the leaf _save_request — same pattern as _current_user_agent.
# Scoped to the asyncio task handling one client request; never leaks between requests.
# Value: {"ip": str, "identity": str} — identity = profile impersonate (fingerprint).
_current_free_attempt: contextvars.ContextVar[dict | None] = contextvars.ContextVar('_current_free_attempt', default=None)

def _db_flush():
    """Force a pending commit. Called periodically and before shutdown."""
    global _db_pending_inserts, _db_last_commit
    with _db_commit_lock:
        if _db_pending_inserts > 0:
            try:
                _conn.commit()
                _debug(f"  [db] _db_flush: committed {_db_pending_inserts} pending inserts")
            except Exception as e:
                _debug(f"  [db] _db_flush commit FAILED: {type(e).__name__}: {e}")
                # Try rollback to recover the connection for future operations
                try:
                    _conn.rollback()
                except Exception:
                    pass
            # Always reset counter to avoid stuck state — even if commit failed,
            # uncommitted rows will be lost but new inserts can proceed normally
            _db_pending_inserts = 0
            _db_last_commit = time.monotonic()


def _db_vacuum_if_needed(deleted_rows: int):
    """Reclaim disk space after cleanup deletes (Vague 4/(g)).

    SQLite keeps freed pages inside the file until VACUUM — without it the
    DB only grows. Runs at most daily (cleanup cadence), only when rows
    were actually deleted. VACUUM needs exclusive access, so it holds the
    same lock as inserts/checkpoints; it commits any pending transaction
    first and is itself transactional (safe on failure).
    """
    if deleted_rows <= 0:
        return
    with _db_commit_lock:
        try:
            # A batched-insert transaction may still be open; VACUUM refuses
            # to run inside one. Committing it early is harmless — the rows
            # were destined to commit within the batch window anyway.
            if _conn.in_transaction:
                _conn.commit()
            _conn.execute("VACUUM")
        except Exception as e:
            _debug(f"  [db] vacuum error: {type(e).__name__}: {e}")
            return
    _log(f"  DB VACUUM: reclaimed space after deleting {deleted_rows} rows")


def _db_cleanup_old_bodies(retention_days: int = 7, delete_after_days: int = 30):
    """Clean up old request data to prevent DB bloat.

    Two-phase cleanup:
    1. DELETE entire rows older than delete_after_days (30d default) — full removal
    2. NULLIFY bodies for rows between retention_days and delete_after_days — keep metadata

    Bodies account for ~95% of DB storage. This keeps recent bodies for debugging
    while preventing unbounded growth. Called periodically by background task.
    """
    try:
        # Phase 1: Delete old rows entirely
        cutoff_delete = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - delete_after_days * 86400))
        cursor = _conn.execute("DELETE FROM requests WHERE timestamp < ?", (cutoff_delete,))
        deleted = cursor.rowcount
        cursor2 = _conn.execute("DELETE FROM free_model_usage WHERE timestamp < ?", (cutoff_delete,))
        deleted2 = cursor2.rowcount

        # Phase 2: Nullify bodies for 7-30 day old rows
        cutoff_null = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - retention_days * 86400))
        cursor3 = _conn.execute(
            "UPDATE requests SET request_body = NULL, response_body = NULL "
            "WHERE timestamp < ? AND (request_body IS NOT NULL OR response_body IS NOT NULL)",
            (cutoff_null,)
        )
        cleaned = cursor3.rowcount

        total = deleted + deleted2 + cleaned
        if total > 0:
            _conn.commit()
            _debug(f"  [db] cleanup: deleted {deleted}+{deleted2} old rows, cleared bodies from {cleaned} requests")
            _log(f"  DB CLEANUP: deleted {deleted+deleted2} old rows, cleared {cleaned} bodies (>{retention_days}d)")
            _db_vacuum_if_needed(deleted + deleted2)
        return deleted + deleted2 + cleaned
    except Exception as e:
        _debug(f"  [db] cleanup error: {type(e).__name__}: {e}")
        return 0


def _normalize_timestamp_utc(timestamp: str) -> str:
    """Naive local wall time → UTC+Z ; les valeurs déjà en Z passent inchangées."""
    if timestamp.endswith("Z"):
        return timestamp
    try:
        return (datetime.datetime.fromisoformat(timestamp)
                .astimezone().astimezone(datetime.timezone.utc)
                .strftime("%Y-%m-%dT%H:%M:%SZ"))
    except ValueError:
        return timestamp  # unparseable — store as-is rather than dropping the row


def _db_insert_sync(req_id, timestamp, model, original_model, duration_ms,
                    tokens_input, tokens_output, tokens_cache, success, error,
                    protocol, is_stream, thinking, effort, client_ip, account_alias,
                    tools_json, tools_used_json, request_body_json=None, response_body_json=None,
                    client_user_agent=None, free_model_ip=None, identity=None):
    """Synchronous DB insert — called via asyncio.to_thread().

    Batches commits: accumulates INSERTs and commits every _DB_COMMIT_BATCH
    inserts or every _DB_COMMIT_INTERVAL seconds, whichever comes first.
    Reduces fsync overhead under load (50 req/s → ~1 commit/s instead of 50).
    """
    global _db_pending_inserts, _db_last_commit
    # [30] Normalize any timestamp that reaches the writer without a Z suffix:
    # naive strings are local wall time and silently mis-order ORDER BY timestamp DESC.
    timestamp = _normalize_timestamp_utc(timestamp)
    t0 = time.monotonic()
    # Lock the entire execute+commit block to prevent InterfaceError when
    # _db_flush or _wal_checkpoint runs concurrently on another thread.
    with _db_commit_lock:
        _conn.execute("""
            INSERT OR REPLACE INTO requests (id, timestamp, model, original_model, duration_ms,
                tokens_input, tokens_output, tokens_cache, success, error,
                protocol, is_stream, thinking, effort, client_ip, account_alias, tools, tools_used,
                request_body, response_body, client_user_agent, free_model_ip, identity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (req_id, timestamp, model, original_model, duration_ms,
              tokens_input, tokens_output, tokens_cache, 1 if success else 0, error,
              protocol, 1 if is_stream else 0, thinking, effort,
              client_ip, account_alias, tools_json, tools_used_json,
              request_body_json, response_body_json, client_user_agent, free_model_ip, identity))
        # Batch commit logic
        _db_pending_inserts += 1
        now = time.monotonic()
        elapsed = now - _db_last_commit
        if _db_pending_inserts >= _DB_COMMIT_BATCH or elapsed >= _DB_COMMIT_INTERVAL:
            try:
                _conn.commit()
                _debug(f"  [db] _db_insert_sync: batch-committed {_db_pending_inserts} inserts ({elapsed:.1f}s) in {(time.monotonic()-t0)*1000:.1f}ms")
            except Exception as e:
                _debug(f"  [db] _db_insert_sync commit FAILED: {type(e).__name__}: {e}")
                try:
                    _conn.rollback()
                except Exception:
                    pass
            # Always reset counter to avoid stuck state
            _db_pending_inserts = 0
            _db_last_commit = now
        else:
            _debug(f"  [db] _db_insert_sync: queued req_id={req_id} (pending={_db_pending_inserts}, {elapsed:.1f}s since last commit)")


async def _save_request(req_id, model, original_model, duration_ms,
	                  tokens_input, tokens_output, tokens_cache, success=True, error=None,
	                  protocol=None, is_stream=False, thinking=None, effort=None,
	                  client_ip=None, account_alias=None, tools=None, tools_used=None,
	                  request_body=None, response_body=None, free_model_ip=None,
	                  identity=None):
    tools_json = json.dumps(tools) if tools else "[]"
    tools_used_json = json.dumps(list(dict.fromkeys(tools_used))) if tools_used else "[]"
    request_body_json = _truncate_body_for_storage(request_body) if request_body else None
    response_body_json = _truncate_body_for_storage(response_body) if response_body else None
    client_user_agent = _current_user_agent.get()
    # Leaf reader for the free-channel stamp: the two writers set
    # _current_free_attempt (IP + identity profile) right before the free
    # request leaves; any save site in the same task picks it up — including
    # paid-fallback rows, which keep the free IP/identity of the attempt.
    if free_model_ip is None or identity is None:
        _attempt = _current_free_attempt.get()
        if _attempt:
            if free_model_ip is None:
                free_model_ip = _attempt.get("ip") or None
            if identity is None:
                identity = _attempt.get("identity") or None
    # [30] UTC everywhere — Z suffix so JS Date parsing and SQLite
    # string comparisons agree (naive strings were local wall time).
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    await asyncio.to_thread(
        _db_insert_sync, req_id, timestamp, model, original_model, duration_ms,
        tokens_input, tokens_output, tokens_cache, success, error,
        protocol, is_stream, thinking, effort, client_ip, account_alias,
        tools_json, tools_used_json, request_body_json, response_body_json,
        client_user_agent, free_model_ip, identity
    )
    # Fire-and-forget: commit in thread pool so dashboard sees the row quickly
    # without blocking the event loop. A blocked event loop freezes SSE streams
    # and triggers client-side 499 disconnects.
    if _db_pending_inserts > 0:
        asyncio.get_running_loop().run_in_executor(None, _db_flush)
    _debug(f"  [db] _save_request: saved req_id={req_id} model={model} success={success}")
    # Notify dashboard SSE clients about the update
    try:
        get_event_manager().publish("stats_updated", {"time": timestamp})
        # Also publish request detail via SSE — reuse original Python objects
        tools_used_deduped = list(dict.fromkeys(tools_used)) if tools_used else []
        get_event_manager().publish("request_completed", {
            "id": req_id,
            "timestamp": timestamp,
            "model": model,
            "original_model": original_model,
            "duration_ms": duration_ms,
            "tokens_input": tokens_input,
            "tokens_output": tokens_output,
            "tokens_cache": tokens_cache,
            "success": success,
            "error": error,
            "protocol": protocol,
            "is_stream": is_stream,
            "thinking": thinking,
            "effort": effort,
            "client_ip": client_ip,
            "account_alias": account_alias,
            "free_model_ip": free_model_ip,
            "identity": identity,
            "tools": tools or [],
            "tools_used": tools_used_deduped,
        })
    except Exception as e:
        _debug(f"  ✗ SSE publish failed: {type(e).__name__}: {e}")


def _log_free_model_usage(paid_model: str, free_model: str, api_key: str,
                          workspace_id: str, status: int, tokens_in: int = 0,
                          tokens_out: int = 0, duration_ms: int = 0, ip: str = ""):
    """Log a free model request to the database for quota analysis."""
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())  # [30] UTC
    timestamp = _normalize_timestamp_utc(timestamp)
    try:
        with _db_commit_lock:
            _conn.execute(
                "INSERT INTO free_model_usage "
                "(timestamp, paid_model, free_model, api_key, workspace_id, status, "
                " tokens_input, tokens_output, duration_ms, ip) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (timestamp, paid_model, free_model, api_key[:16] + "...", workspace_id,
                 status, tokens_in, tokens_out, duration_ms, ip)
            )
            _conn.commit()
        _debug(f"  [free-usage] logged: {free_model} key={api_key[:8]}... ws={workspace_id[:12]}... "
               f"status={status} ip={ip} in={tokens_in} out={tokens_out}")
    except Exception as e:
        _debug(f"  [free-usage] log failed: {e}")


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
        with _db_commit_lock:
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


def _ensure_http_client() -> httpx.AsyncClient:
    """Lazy self-heal for the shared upstream client.

    The lifespan shutdown calls ``await _client.aclose()`` (see below), so a
    request that re-enters a stream/POST path during a restart handoff races
    the now-closed client and httpx raises
    RuntimeError("Cannot send a request, as the client has been closed.").
    That error was surfacing as a confusing ✘ failure in dashboard history.
    Re-create the client on demand instead — same pattern as
    dashboard/quota.py::_get_http_client.
    """
    global _client, _transport
    # getattr: real httpx clients always expose .is_closed; test doubles that
    # omit it are treated as alive rather than closed.
    if _client is None or getattr(_client, "is_closed", False):
        _transport = httpx.AsyncHTTPTransport(
            proxy=PROXY,
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50, keepalive_expiry=120),
        ) if PROXY else httpx.AsyncHTTPTransport(
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=50, keepalive_expiry=120),
        )
        _client = httpx.AsyncClient(transport=_transport, timeout=httpx.Timeout(connect=30, read=600, write=30, pool=10))
        _debug("[http] shared upstream client re-created (was closed)")
    return _client


# ── VPN / IP rotation (initialized in lifespan) ──────────────────
_vpn_manager = None
_free_ip_pool = None


# ── Debug helpers ──────────────────────────────────────────────────

def _sanitize_headers(headers) -> dict:
    """Mask sensitive header values for debug logging."""
    safe = dict(headers)
    for key in ("x-api-key", "Authorization"):
        if key in safe:
            val = safe[key]
            safe[key] = val[:8] + "..." + val[-4:] if len(val) > 16 else "***"
    return safe


def _truncate(body, max_len=None) -> str:
    if max_len is None:
        max_len = yaml_get("debug", "truncate_max", 2000)
    """Pretty-print a body for debug logging, truncated to max_len chars."""
    try:
        text = json.dumps(body, indent=2, ensure_ascii=False, default=str)
    except Exception:
        text = str(body)
    if len(text) > max_len:
        return text[:max_len] + f"\n... [{len(text) - max_len} chars truncated]"
    return text


# Secret-masking patterns for log redaction (finding [40]).
# Applied to every logged response/request body so secrets echoed by
# upstream (429/401/403 bodies, error payloads) never reach debug.log.
_REDACT_PATTERNS = [
    # OpenAI-style API keys (sk-...)
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}\b"), "sk-***"),
    # Bearer tokens
    (re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._\-]{16,}"), r"\1***"),
    # Sensitive header values in logged text: "x-api-key": "v", Authorization: v, cookie: v
    (re.compile(r'(?i)("?(?:x-api-key|authorization|cookie|set-cookie)"?\s*[:=]\s*"?)[^"\s,}]{4,}'), r"\1***"),
    # JSON fields / query params: api_key=..., apikey: ...
    (re.compile(r'(?i)\b(api[_-]?key\s*[:=]\s*"?)[^"\s,}]{4,}'), r"\1***"),
]


def _redact(text, max_len=None) -> str:
    """Mask secrets (API keys, bearer tokens, sensitive header values) in log text."""
    if text is None:
        return ""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    elif not isinstance(text, str):
        text = str(text)
    for pat, repl in _REDACT_PATTERNS:
        text = pat.sub(repl, text)
    if max_len is not None and len(text) > max_len:
        return text[:max_len] + f"... [{len(text) - max_len} chars truncated]"
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
    # Restore persisted key pause state from disk
    _key_pauser.load(API_KEYS)

    # Start background quota fetcher (no-op if env vars not set)
    await start_quota_fetcher(app)

    # Toggle "Use Balance" on for all workspaces (so Go falls back to Zen balance)
    try:
        from dashboard import toggle_use_balance_all
        balance_results = await toggle_use_balance_all()
        if balance_results:
            ok = sum(1 for v in balance_results.values() if v)
            _debug(f"  [lifespan] use-balance toggle: {ok}/{len(balance_results)} workspaces enabled")
    except Exception as e:
        _debug(f"  [lifespan] use-balance toggle failed: {e}")

    # Register recovery callback: unpause API keys when workspace returns to ok
    from dashboard.quota import set_on_workspace_recovered_callback
    set_on_workspace_recovered_callback(on_workspace_recovered)
    _debug("  [lifespan] workspace recovery callback registered")

    # ── VPN / IP rotation for free models ──
    import shared_state
    from vpn_manager import VPNManager
    from shared_rotation import SharedRotationState
    from free_ip_pool import FreeIPPool
    # Cross-station shared state: recent-IP registry + one absolute identity
    # cursor. Both stations read/write it so neither re-enters an IP the
    # other used recently and their live identities never collide.
    shared_state.shared_rotation = SharedRotationState(IP_ROTATION)
    shared_state.vpn_manager = VPNManager(IP_ROTATION,
                                          shared=shared_state.shared_rotation)
    shared_state.vpn_manager.enabled = IP_ROTATION.get("enabled", False)
    # Dual station ("double embrayage"): a second gluetun tunnel runs in
    # parallel (station 2) so a free request always lands on a fresh
    # (model, IP) cooldown key while the other station rotates in the
    # background. Opt-in via ip_rotation.dual_station (GUI toggle).
    shared_state.vpn_manager_2 = None
    if IP_ROTATION.get("dual_station", False):
        shared_state.vpn_manager_2 = VPNManager(IP_ROTATION, station=2,
                                                shared=shared_state.shared_rotation)
        shared_state.vpn_manager_2.enabled = IP_ROTATION.get("enabled", False)
    shared_state.free_ip_pool = FreeIPPool(shared_state.vpn_manager,
                                           shared_state.vpn_manager_2)
    # [plan] E: boot-time fan-out of the ip_rotation timings (connect retry,
    # bad TTL, rotation stagger) — the pool's built-in defaults are the
    # conservative legacy values.
    shared_state.free_ip_pool.update_config(IP_ROTATION)
    global _vpn_manager, _free_ip_pool
    _vpn_manager = shared_state.vpn_manager
    _free_ip_pool = shared_state.free_ip_pool
    # Start every enabled station in PARALLEL (dual station would otherwise
    # halve the cold-start: station 2 waits for station 1's full compose-up).
    # Each start() is fail-soft internally (docker down/logs a warning) so
    # gather() never raises.
    _startup_managers = [m for m in (shared_state.vpn_manager,
                                     shared_state.vpn_manager_2)
                         if m is not None and m.enabled]
    if _startup_managers:
        await asyncio.gather(*(m.start() for m in _startup_managers))
    # [plan] C: docker event watcher — real-time container lifecycle → per-
    # station watchdog wake + SSE vpn_event. Fail-open: if docker is missing
    # or the stream dies, the watchdogs keep their interval pacing and the
    # dashboard its 10 s poll — never breaks a request.
    shared_state.docker_event_watcher = None
    if IP_ROTATION.get("docker_events", True) and _startup_managers:
        from docker_events import DockerEventWatcher
        try:
            _watcher = DockerEventWatcher(
                {m._docker_container: m for m in _startup_managers})
            await _watcher.start()
            shared_state.docker_event_watcher = _watcher
            _debug(f"  [lifespan] docker event watcher started "
                   f"({len(_watcher._managers)} containers)")
        except Exception as e:
            _debug(f"  [lifespan] docker event watcher failed to start: {e}")
            shared_state.docker_event_watcher = None
    _debug(f"  [lifespan] VPN manager initialized (enabled={shared_state.vpn_manager.enabled}, "
           f"mode={shared_state.vpn_manager._mode}, "
           f"dual_station={bool(shared_state.vpn_manager_2)})")

    # Periodic WAL checkpoint
    async def _periodic_checkpoint():
        while True:
            try:
                await asyncio.sleep(yaml_get("background", "wal_checkpoint_interval", 3600))
                await asyncio.to_thread(_wal_checkpoint)
            except Exception as e:
                _debug(f"  [db] _periodic_checkpoint error: {type(e).__name__}: {e}")
                await asyncio.sleep(60)  # retry after 60s on error

    # Periodic DB flush (every 5s) to commit any batched inserts
    async def _periodic_db_flush():
        while True:
            try:
                await asyncio.sleep(_DB_COMMIT_INTERVAL)
                await asyncio.to_thread(_db_flush)
            except Exception as e:
                _debug(f"  [db] _periodic_db_flush error: {type(e).__name__}: {e}")
                await asyncio.sleep(1)  # brief pause before retry

    checkpoint_task = asyncio.create_task(_periodic_checkpoint())
    db_flush_task = asyncio.create_task(_periodic_db_flush())

    # Periodic key pause cleanup to remove expired entries
    async def _periodic_key_pause_cleanup():
        while True:
            await asyncio.sleep(yaml_get("background", "key_pause_cleanup_interval", 30))
            _key_pauser.cleanup_expired()

    key_pause_cleanup_task = asyncio.create_task(_periodic_key_pause_cleanup())

    # Periodic DB body cleanup (daily) — removes old request/response bodies to save disk
    async def _periodic_db_cleanup():
        # Run cleanup once at startup, then every 24h
        await asyncio.sleep(60)  # wait 60s after startup before first cleanup
        while True:
            try:
                retention = yaml_get("database", "body_retention_days", 7)
                await asyncio.to_thread(_db_cleanup_old_bodies, retention)
                await asyncio.sleep(yaml_get("background", "db_cleanup_interval", 86400))
            except Exception as e:
                _debug(f"  [db] periodic cleanup error: {type(e).__name__}: {e}")
                await asyncio.sleep(3600)

    db_cleanup_task = asyncio.create_task(_periodic_db_cleanup())
    _debug("  [lifespan] background tasks created (WAL checkpoint, DB flush, key pause cleanup, DB body cleanup, quota fetcher)")

    yield

    _debug("  [lifespan] app shutting down")
    # Flush any pending DB writes before shutdown
    await asyncio.to_thread(_db_flush)
    _debug("  [lifespan] final DB flush done")

    # Cancel background tasks
    checkpoint_task.cancel()
    db_flush_task.cancel()
    key_pause_cleanup_task.cancel()
    db_cleanup_task.cancel()
    try:
        await checkpoint_task
    except asyncio.CancelledError:
        pass
    try:
        await db_flush_task
    except asyncio.CancelledError:
        pass
    try:
        await key_pause_cleanup_task
    except asyncio.CancelledError:
        pass
    _debug("  [lifespan] background tasks cancelled")

    # Stop the docker event watcher (kills the docker events subprocess)
    watcher = getattr(shared_state, "docker_event_watcher", None)
    if watcher is not None:
        try:
            await watcher.stop()
        except Exception as e:
            _debug(f"  [lifespan] docker event watcher stop failed: {e}")
        shared_state.docker_event_watcher = None

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
    # Save VPN state (container stays up — compose-managed)
    if _vpn_manager:
        await _vpn_manager.stop()
        _debug("  [lifespan] VPN state saved")
    if getattr(shared_state, "vpn_manager_2", None) is not None:
        await shared_state.vpn_manager_2.stop()
        _debug("  [lifespan] VPN station 2 state saved")
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

register_dashboard(app, STATIC_DIR, _conn, server_manager_getter=lambda: _server_manager, token_usage=_token_usage, token_lock=_token_lock, db_lock=_db_commit_lock)


# ── Rate Limiting (token bucket, per-IP) ────────────────────────

RATE_LIMIT_RPS = float(os.environ.get("RATE_LIMIT_RPS", str(yaml_get("rate_limit", "rps", 50))))
RATE_LIMIT_BURST = float(os.environ.get("RATE_LIMIT_BURST", str(yaml_get("rate_limit", "burst", 100))))
_STALE_BUCKET_TTL = yaml_get("rate_limit", "stale_ttl", 300)  # seconds — remove buckets inactive for 5 min


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

        # Identify client IP — socket peer only. X-Forwarded-For is
        # spoofable by any direct client, so it is never trusted (CRITIC 10).
        ip = request.client.host if request.client else "unknown"

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

        # Socket peer only — X-Forwarded-For is spoofable (CRITIC 10)
        client_ip = request.client.host if request.client else "?"

        start = time.monotonic()
        try:
            response = await call_next(request)
        except ClientDisconnect:
            elapsed_ms = (time.monotonic() - start) * 1000
            _debug(f"  [access] client disconnected after {elapsed_ms:.0f}ms: {request.method} {path} {client_ip}")
            _log(f"{request.method} {path} 499 {elapsed_ms:.0f}ms {client_ip}")
            return StarletteJSONResponse(status_code=499, content={"error": "Client disconnected"})
        elapsed_ms = (time.monotonic() - start) * 1000

        _log(f"{request.method} {path} {response.status_code} {elapsed_ms:.0f}ms {client_ip}")
        return response


app.add_middleware(AccessLogMiddleware)


# ── Traffic Capture (Wireshark-like raw request view) ──────────────
# Outermost middleware: records every client request — raw body bytes,
# headers, timing, tempo, abrupt disconnects (RST) — into a bounded
# ring buffer served by /api/traffic/*. Excludes /static, /health and
# itself. Configurable via the `traffic:` block in config.yaml.

from traffic_capture import capture as _traffic_capture, TrafficCaptureMiddleware

_traffic_capture.configure(
    enabled=bool(yaml_get("traffic", "enabled", True)),
    max_frames=int(yaml_get("traffic", "max_frames", 500)),
    body_cap=int(yaml_get("traffic", "body_cap", 131072)),
    max_bytes=int(yaml_get("traffic", "max_bytes", 33554432)),
)
app.add_middleware(TrafficCaptureMiddleware, capture=_traffic_capture)


# ── Circuit Breaker (per-endpoint) ──────────────────────────────

_CB_FAILURE_THRESHOLD = yaml_get("circuit_breaker", "failure_threshold", 5)     # trips open after N consecutive failures
_CB_RECOVERY_TIMEOUT = float(yaml_get("circuit_breaker", "recovery_timeout", 60))   # seconds before half-open test


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


class AllKeysPausedError(Exception):
    """Raised when all API keys are paused and no request can be made."""
    def __init__(self, retry_after: float):
        super().__init__(f"All API keys paused, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


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


# Claude Code traite 401/403 comme un échec d'authentification → il affiche sa
# page de login/blocage. Un 401/403 upstream signifie que la clé/région du proxy
# est bloquée — rien que l'utilisateur final puisse corriger en se reconnectant —
# donc on normalise ces statuts en 503 avant qu'ils atteignent le client.
_AUTH_WINDOW_CODES = {401, 403}


def _safe_client_status(upstream_status: int) -> int:
    """Statut à renvoyer au client pour éviter la fenêtre d'auth de Claude Code.

    401/403/429 → 503, 499 → 502, sinon inchangé.
    """
    if upstream_status in _AUTH_WINDOW_CODES or upstream_status == 429:
        return 503
    if upstream_status == 499:
        return 502
    return upstream_status


def _auth_window_message(status: int) -> str:
    """Message propre pour les statuts upstream réécrits en 503."""
    if status == 429:
        return "All API keys exhausted (rate limited). Try again later."
    if status == 401:
        return "All API keys exhausted (unauthorized). Check your API keys."
    if status == 403:
        return ("Upstream access denied (403) — model/region may be restricted for "
                "this key. Try again later or check key permissions.")
    return f"Upstream error (HTTP {status}). Try again later."


async def _forward_post(endpoint, json, headers):
    """POST with circuit breaker. Raises CircuitOpenError if circuit is open."""
    if not _cb_should_allow(endpoint):
        raise CircuitOpenError(f"Circuit breaker open for {endpoint}")
    try:
        resp = await _ensure_http_client().post(endpoint, json=json, headers=headers)
        _cb_record_success(endpoint)
        return resp
    except CircuitOpenError:
        raise
    except Exception:
        _cb_record_failure(endpoint)
        raise



# ── Shared helper functions for endpoint handlers ──────────────

# ── Identity rotation for free requests ─────────────────────────
# Each successful IP rotation advances an identity profile (curl_cffi
# impersonation target + User-Agent + optional extra headers) so the
# free endpoint sees a coherent browser "face" instead of the same
# fingerprint on every fresh IP. The profile list and index are owned
# by vpn_manager (config `ip_rotation.identity_profiles`).

# Curated real User-Agents for the default-profile targets live in
# vpn_manager (module-level _UA_BY_IMPERSONATE, derived by family from the
# full supported-impersonation list so the httpx fallback paths stay
# coherent with the curl_cffi bundles). Re-exported here: _apply_identity
# reads it, and tests reference oc._UA_BY_IMPERSONATE.
from vpn_manager import _UA_BY_IMPERSONATE


def _current_free_identity(station=None) -> dict:
    """Active identity profile for free requests (vpn_manager ownership).

    Station-aware: returns the identity of the station that will (or did)
    serve the request. With dual station the two tunnels MUST expose
    different identities — reading station 1 unconditionally would serve
    the same fingerprint from both stations. ``station`` defaults to the
    pool's last-picked station, then station 1. Falls back to the chrome131
    default when no VPN manager is present — identical behavior to before
    identity rotation existed.
    """
    mgr = (station if station is not None
           else (_free_ip_pool.active_station if _free_ip_pool else None)
           or _vpn_manager)
    if mgr:
        return mgr.current_identity
    return {"impersonate": "chrome131", "user_agent": None, "extra_headers": {}}


def _apply_identity(headers: dict, profile: dict, use_curated_ua: bool = True) -> dict:
    """Stamp a free-request header dict with the identity profile.

    The outgoing User-Agent ALWAYS comes from the identity, never from
    the paid client (invariant A.0):
      - explicit profile user_agent wins,
      - else curated UA map (httpx paths),
      - else (use_curated_ua=False, curl_cffi bundle paths) the incoming
        client UA is removed so the impersonation bundle injects its own
        coherent browser UA — curl_cffi only fills headers that are absent.
    extra_headers are applied last so they can override anything.
    """
    out = dict(headers)
    ua = profile.get("user_agent")
    if not ua and use_curated_ua:
        ua = _UA_BY_IMPERSONATE.get(profile.get("impersonate"))
    if ua:
        out["User-Agent"] = ua
    elif not use_curated_ua:
        out.pop("User-Agent", None)
        out.pop("user-agent", None)
    for k, v in (profile.get("extra_headers") or {}).items():
        out[k] = v
    return out


def _free_request_headers(headers: dict) -> dict:
    """Strip paid-account artifacts from a header dict for the free endpoint.

    Invariant A.0: no request sent to API_BASE_FREE may carry the paid
    key (Authorization / x-api-key), client cookies, x-request-id or
    x-stainless-* SDK identifiers. Protocol headers (anthropic-version)
    survive — they identify the API shape, not the account.
    """
    return {k: v for k, v in headers.items()
            if k.lower() not in ("authorization", "x-api-key", "cookie", "x-request-id")
            and not k.lower().startswith("x-stainless-")}


def _is_connect_error(e: Exception) -> bool:
    """[plan 18/08 §1a] True when ``e`` is a transport-level connection
    failure through the VPN tunnel (SOCKS5 dead) — NOT an HTTP answer.
    curl_cffi 0.14.0: RequestsError aliases RequestException (all
    subclasses); ``e.code`` is always set (IntEnum, 0 default); the
    ``errors`` module exports only CurlError/RequestsError/
    CookieConflict/SessionClosed — no subclass test possible. Classify by
    libcurl code: resolve/connect/partial-file/timeout/SSL/recv = dead
    tunnel. Fallback: the message names SOCKS/connect/proxy. Never fires
    on HTTP responses — 429/5xx are not exceptions on this path
    (raise_for_status is False).
    """
    import curl_cffi.requests.errors as _err
    if not isinstance(e, _err.RequestsError):
        return False
    try:
        code = int(getattr(e, "code", 0))
    except (TypeError, ValueError):
        code = 0
    if code in (5, 6, 7, 18, 28, 35, 56, 97):
        return True
    msg = str(e).lower()
    return any(k in msg for k in ("socks", "connect", "proxy"))


def _signal_connection_failure(station) -> None:
    """[plan 18/08 §1a] Fire-and-forget pool signal for a real connection
    failure on ``station`` (SOCKS5 dead — invisible to the pool, which keeps
    the station "connected"). The request path must NEVER suffer the signal:
    any bug inside notify (bad-mark dispatch, logging) must not turn a free
    request into an error — full swallow.
    """
    try:
        _free_ip_pool.notify_connection_failure(station)
    except Exception:
        pass


async def _do_free_request_curl_cffi(body: dict, headers: dict, proxy_url: str | None = None,
                                     station=None):
    """Make a free model request using curl_cffi for TLS fingerprint evasion.

    Uses the current identity profile's TLS fingerprint (default chrome131)
    and User-Agent to avoid detection.
    When proxy_url is provided (VPN mode), routes through the tunnel so the
    request exits with the VPN IP (fresh free quota per IP).
    station disambiguates the identity under dual station (the tunnel that
    egresses this request must be the one whose fingerprint is stamped).
    Returns an httpx-like response object for compatibility.
    """
    try:
        from curl_cffi.requests import AsyncSession
        from curl_cffi.requests import errors as _err
    except ImportError:
        raise RuntimeError("curl_cffi not installed: pip install curl_cffi")

    # Strip paid-account artifacts, stamp the identity (bundle UA wins)
    profile = _current_free_identity(station)
    req_headers = _apply_identity(_free_request_headers(headers), profile, use_curated_ua=False)
    req_headers["Content-Type"] = "application/json"

    async with AsyncSession(impersonate=profile["impersonate"],
                            proxy=_curl_proxy_url(proxy_url)) as session:
        # [plan 18/08 §1a/am.21] connect timeout 30 → 10: in mono-station the
        # request IS the arming signal (bad-mark is C1-forbidden) — the first
        # failure reaches the manager in ≤10-15 s, not 30. Read 600 unchanged
        # (long model streams). SOCKS5+TLS handshake ≈ 1-2 s on a healthy
        # tunnel — no legitimate request is impacted.
        try:
            resp = await session.post(
                API_BASE_FREE,
                json=body,
                headers=req_headers,
                timeout=(10, 600),  # (connect, read) — read 600: long streams
            )
        except _err.RequestsError as e:
            # [plan 18/08 §1a] a REAL connection failure (dead SOCKS5 tunnel)
            # is invisible to the pool — the station stays "connected" and
            # every request re-strikes it. Signal the pool+manager
            # fire-and-forget: bad-mark → instant failover (no request ever
            # re-strikes a known-dead tunnel), arm+wake → live tick repair.
            # Never on HTTP responses (429/5xx are not exceptions here —
            # raise_for_status is False).
            if station is not None and _is_connect_error(e):
                _signal_connection_failure(station)
            raise
        # Wrap in a compatible response object
        return _CurlCffiResponse(resp)


def _curl_proxy_url(proxy_url: str | None) -> str | None:
    """Fix curl_cffi (libcurl) SOCKS5 routing on Windows.

    With ``socks5://`` libcurl resolves the hostname locally, which times out
    against gluetun on Windows Docker Desktop (observed: 60 s curl 28).
    ``socks5h://`` resolves the hostname inside the tunnel (gluetun DNS), which
    answers in <1 s. httpx is untouched — it never sees this conversion.
    """
    if proxy_url and proxy_url.startswith("socks5://"):
        return "socks5h://" + proxy_url[len("socks5://"):]
    return proxy_url


class _CurlCffiResponse:
    """Wrapper to make curl_cffi response compatible with httpx response interface."""

    def __init__(self, resp):
        self._resp = resp
        self.status_code = resp.status_code
        self.headers = dict(resp.headers)

    @property
    def text(self) -> str:
        if isinstance(self._resp.content, bytes):
            return self._resp.content.decode("utf-8", errors="replace")
        return str(self._resp.content)

    @property
    def content(self) -> bytes:
        return self._resp.content

    def json(self):
        import json
        return json.loads(self.text)


class _CurlCffiStreamResponse:
    """Wrapper: curl_cffi streaming response → httpx-like interface.

    Exposes exactly what the streaming loops consume (status_code, headers,
    aiter_lines, aiter_bytes, aread) so the free-VPN path can reuse them
    unchanged. curl_cffi streams through the SOCKS5 tunnel when proxy set.
    """

    def __init__(self, resp):
        self._resp = resp
        self.status_code = resp.status_code
        self.headers = dict(resp.headers)

    async def aiter_lines(self):
        """Yield SSE lines as str, split manually from raw bytes ([41]).

        curl_cffi's own aiter_lines() yields bytes (its internal
        splitting also raises TypeError "startswith first arg must
        be bytes" in 0.14.0), while consumers expect httpx-style str
        lines — so iterate aiter_content() and split on \n here,
        buffering a partial line across chunks.
        """
        buf = b""
        async for chunk in self._resp.aiter_content():
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.rstrip(b"\r").decode("utf-8", errors="replace")
        if buf:  # trailing line without final newline
            yield buf.rstrip(b"\r").decode("utf-8", errors="replace")

    async def aiter_bytes(self):
        async for chunk in self._resp.aiter_content():
            yield chunk

    async def aread(self):
        return await self._resp.acontent()

    async def aclose(self):
        await self._resp.aclose()


@asynccontextmanager
async def _open_free_stream(endpoint, body, headers, use_free: bool, count_request: bool = True,
                            fresh_station: bool = False):
    """Context manager: upstream stream, routed through the VPN when free.

    use_free=True and VPN available → curl_cffi stream via the SOCKS5 tunnel
    (fresh IP = fresh free quota), impersonate the current identity profile.
    Direct fallback (VPN down) → httpx stream with paid-account artifacts
    stripped and the identity UA applied (invariant A.0). use_free=False →
    normal paid httpx stream, headers untouched. All paths yield the same
    response interface.

    count_request=False skips the quota counter on this attempt ([19]): the
    callers' retry loop re-enters this function on network errors, and each
    re-entry must not advance the per-IP request count (1 request = 1 count,
    whatever the number of attempts).

    fresh_station=True asks the pool for a DIFFERENT station than the one
    that just disconnected ([17/08 21:44] "Server disconnected without
    sending a response"): re-striking the same station = re-striking a dead
    IP, a guaranteed ✘ under the per-IP quota model. Uses the ContextVar's
    stored station as the failed one to exclude, then rotates it in the
    background.
    """
    if use_free and _free_ip_pool and _free_ip_pool.enabled:
        # on_request returns (proxy_url, station); the stream path used to
        # take only the URL, leaving a TUPLE in proxy_url — every free stream
        # then crashed into the direct fallback (BLOCKING, [plan] E).
        if count_request:
            proxy_url, station = await _free_ip_pool.on_request()
        elif fresh_station:
            # Disconnect retry: a different station = a different, likely-fresh
            # (model, IP) cooldown key. NO counter advance (a retry is not a
            # new request) — on_disconnect_retry is the count_request=False
            # sibling of on_request. The failed station is bad-marked + rotated
            # in the background by the pool.
            _prev = _current_free_attempt.get() or {}
            proxy_url, station = await _free_ip_pool.on_disconnect_retry(
                _prev.get("station"))
        else:
            # Retry after a network error: re-use the SAME station/proxy as
            # the original attempt. Reading the fresh ``proxy_url`` property
            # here would recompute ``_best_station()`` — a background 429
            # rotation could then pick the OTHER station, so the retry would
            # ship the retried request on a different IP than the one whose
            # (model, IP) cooldown key was counted (finding h). The original
            # attempt stored its proxy + station in the ContextVar.
            _attempt = _current_free_attempt.get() or {}
            proxy_url = _attempt.get("proxy_url")
            station = _attempt.get("station") if proxy_url else None
        if proxy_url:
            try:
                from curl_cffi.requests import AsyncSession
                station = station or (_free_ip_pool.active_station if proxy_url else None)
                profile = _current_free_identity(station)
                _current_free_attempt.set({"ip": _free_usage_ip(station),
                                           "identity": profile.get("impersonate") or "",
                                           "station": station,
                                           "proxy_url": proxy_url})
                req_headers = _apply_identity(_free_request_headers(headers),
                                              profile, use_curated_ua=False)
                req_headers["Content-Type"] = "application/json"
                # [plan 18/08 §1a/am.21] connect timeout 30 → 10: in mono-station
                # the request IS the arming signal (bad-mark is C1-forbidden) —
                # the first failure reaches the manager in ≤10-15 s, not 30. Read
                # 600 unchanged (long streams). SOCKS5+TLS handshake ≈ 1-2 s on a
                # healthy tunnel — no legitimate request is impacted.
                session = AsyncSession(impersonate=profile["impersonate"],
                                       proxy=_curl_proxy_url(proxy_url),
                                       timeout=(10, 600))  # connect 10 (am.21) / read 600
                resp = await session.post(endpoint, json=body, headers=req_headers, stream=True)
                wrapped = _CurlCffiStreamResponse(resp)
                try:
                    yield wrapped
                finally:
                    await resp.aclose()
                    await session.close()
                return
            except Exception as e:
                # [plan 18/08 §1a] a REAL connection failure (dead SOCKS5
                # tunnel) is invisible to the pool — the station stays
                # "connected" and every request re-strikes it. Signal
                # fire-and-forget BEFORE the direct fallback (bad-mark →
                # instant failover; arm+wake → live tick repair). No
                # re-raise: the direct fallback is the existing semantics.
                # Never on HTTP answers (429/5xx are not exceptions here).
                if station is not None and _is_connect_error(e):
                    _signal_connection_failure(station)
                _debug(f"  [stream] curl_cffi proxy stream failed: {e}, falling back to direct stream")
                _log(f"  FREE STREAM via VPN tunnel FAILED ({e}) → direct fallback (residential IP)")
    if use_free:
        # Direct fallback to the free endpoint: never forward the paid key,
        # cookies or SDK identifiers (invariant A.0), stamp the identity UA.
        profile = _current_free_identity()
        # Preserve the station that just failed (the tunnel branch above set
        # it in the ContextVar before the accident): a disconnect retry must
        # switch AWAY from it on_disconnect_retry, not re-strike the same IP.
        _prev_attempt = _current_free_attempt.get() or {}
        _current_free_attempt.set({"ip": _free_usage_ip(),
                                   "identity": profile.get("impersonate") or "",
                                   "station": _prev_attempt.get("station"),
                                   "proxy_url": None})
        free_headers = _apply_identity(_free_request_headers(headers), profile)
        async with _ensure_http_client().stream("POST", endpoint, json=body, headers=free_headers) as resp:
            yield resp
        return
    async with _ensure_http_client().stream("POST", endpoint, json=body, headers=headers) as resp:
        yield resp


# Cache for public IP to avoid flooding ipify.org. [47]: the probe must
# measure the REAL egress — through the tunnel when the VPN is up (that is
# the IP free requests actually present upstream), direct otherwise. The
# entry is tagged with the probe context so a tunnel IP is never served as
# the direct egress (or vice versa) across a VPN transition.
_public_ip_cache = {"ip": "", "ts": 0.0, "via_tunnel": False}

async def _get_cached_public_ip() -> str:
    """Get the real egress IP, cached for 60s to avoid flooding.

    [47] Probes THROUGH the SOCKS5 tunnel when the VPN is up — matching
    the egress the free requests present upstream, not the machine's
    residential IP. Falls back to the direct egress only when there is no
    tunnel. Never raises.
    """
    expect_tunnel = bool(_vpn_manager and _vpn_manager.current_ip
                         and getattr(_vpn_manager, "socks5_url", None))
    now = time.monotonic()
    cached = _public_ip_cache
    if cached["ip"] and now - cached["ts"] < 60 and cached["via_tunnel"] == expect_tunnel:
        return cached["ip"]
    try:
        import httpx
        proxy = _vpn_manager.socks5_url if expect_tunnel else None
        async with httpx.AsyncClient(timeout=10 if expect_tunnel else 5,
                                     proxy=proxy) as client:
            resp = await client.get("https://api.ipify.org")
            ip = resp.text.strip()
    except Exception:
        # Serve the stale value only when it matches the current context.
        return cached["ip"] if cached["via_tunnel"] == expect_tunnel else "unknown"
    if ip:
        cached.update(ip=ip, ts=now, via_tunnel=expect_tunnel)
        return ip
    return cached["ip"] if cached["via_tunnel"] == expect_tunnel else "unknown"


# Free model cooldown: after a 429 (or any free non-200), skip that free
# model for a duration derived from the upstream retry-after.
# Keyed per (model, current IP) ([4]) — one model/IP's quota exhaustion
# must not block the others, and a rotation to a fresh IP starts with a
# FRESH key (the 429 that triggered the rotation only blocks the
# exhausted IP, never the model on the new IP).
_free_model_cooldowns: dict[str, float] = {}  # "model|ip" -> monotonic expiry
_FREE_COOLDOWN_MAX = 86400  # hard ceiling: retry-after beyond 24 h → default below
# [incident 17/08 PAYANT] zen's free API 429 (FreeUsageLimitError) carries NO
# retry-after header. The old 3600 s default meant ONE 429 — even a transient
# tunnel-blip direct fallback — disabled every free attempt for a FULL HOUR
# (verified: `skipping free model (cooldown active)` every minute 19:32-20:31,
# a full hour of guaranteed PAID traffic). 120 s bounds the damage: after a
# rotation the (model, IP) key is fresh anyway, and 120 s ≈ worst-case rotation
# time + margin. Explicitly-pronounced retry-after values are still honored.
_FREE_429_DEFAULT = 120.0


def _free_cooldown_key(free_model: str, station=None) -> str:
    """Cooldown key = (free model, egress IP of the station used) ([4]).

    With dual station, the 429 must cooldown the IP of the station that
    actually served the request — not station 1's IP, which may be a
    different, still-fresh key. ``station=None`` → the active station
    (or station 1 as before). "direct" when no VPN IP is known (VPN
    down / direct mode) — in that case there is no fresh-IP path, so the
    key must stay stable.
    """
    vpn = station or (_free_ip_pool.active_station if _free_ip_pool else None) or _vpn_manager
    ip = vpn.current_ip if (vpn and vpn.current_ip) else "direct"
    return f"{free_model}|{ip}"


def _free_429_cooldown_seconds(retry_after: str = "") -> float:
    """Duration (seconds) to cooldown a free model after a 429.

    Accepts a seconds count ("120") or an RFC 9110 HTTP-date
    ("Wed, 21 Oct 2015 07:28:00 GMT") ([7]). Anything unparseable or out
    of (0, 86400] → _FREE_429_DEFAULT (120 s). An absent retry-after means
    we don't know the reset time — [incident 17/08] the OLD 3600 s default
    made one 429 block ALL free attempts for an hour (an hour of paid); the
    short default is safe because the key is (model, IP): the background
    rotation gives a fresh IP/key well within 120 s.
    """
    if not retry_after:
        return _FREE_429_DEFAULT
    v = 0.0
    try:
        v = float(retry_after)
    except (TypeError, ValueError):
        try:
            parsed = email.utils.parsedate_to_datetime(retry_after)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=datetime.timezone.utc)  # HTTP-date is GMT
            v = (parsed - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return _FREE_429_DEFAULT
    if 0 < v <= _FREE_COOLDOWN_MAX:
        return v
    return _FREE_429_DEFAULT


def _set_free_cooldown(free_model: str, seconds: float, station=None) -> None:
    key = _free_cooldown_key(free_model, station)
    expiry = time.monotonic() + seconds
    if len(_free_model_cooldowns) > 32:  # bound memory: drop expired entries
        expired = [m for m, t in _free_model_cooldowns.items() if t <= time.monotonic()]
        for m in expired:
            del _free_model_cooldowns[m]
    _free_model_cooldowns[key] = expiry
    _log(f"  FREE COOLDOWN: {key} for {seconds:.0f}s")


def _free_cooldown_active(free_model: str, station=None) -> bool:
    key = _free_cooldown_key(free_model, station)
    expiry = _free_model_cooldowns.get(key, 0.0)
    if expiry > 0 and time.monotonic() < expiry:
        return True
    if expiry > 0:
        del _free_model_cooldowns[key]
    return False


def _free_attempt_station():
    """The station that served the current free attempt (ContextVar).

    ``None`` when the attempt went direct or no free attempt is running
    — callers then fall back to the active/last-used station.
    """
    attempt = _current_free_attempt.get()
    return attempt.get("station") if attempt else None


def _free_stations_exhausted(free_model: str) -> bool:
    """True when NO station can serve a fresh free attempt for this model.

    A station is exhausted when its tunnel is down / marked bad by a
    recent 429, or when the (model, IP) cooldown key of its current IP
    is still active. Used by strict_free: only refuse the request when
    every station is truly exhausted — otherwise let the free attempt
    land on the usable station instead of paying or refusing.
    """
    if not _free_ip_pool or not _free_ip_pool._stations:
        return True
    for st in _free_ip_pool._stations:
        if _free_ip_pool._station_usable(st, exclude_approaching=False):
            if not _free_cooldown_active(free_model, st):
                return False
    return True


def _on_free_429_stream(free_model: str, retry_after: str = "") -> bool:
    """Free endpoint 429 during streaming: cooldown + paid fallback.

    Returns True when the request must be REFUSED (strict_free mode and
    both stations exhausted) — the caller then answers 429/503 to the
    client instead of falling back to paid. Returns False → paid
    fallback (default behavior).

    [0]/[42] restored: a 429 ALSO triggers an IP rotation in the
    background (single-flight via FreeIPPool.on_quota_exhausted; the
    calling request falls back to paid immediately). The cooldown is
    keyed per (model, IP) ([4]) so the fresh IP starts with a fresh key
    — the next free attempt on the new IP is NOT blocked by this 429.
    No-op when VPN rotation is off.
    """
    station = _free_attempt_station()
    _set_free_cooldown(free_model, _free_429_cooldown_seconds(retry_after), station)
    if _free_ip_pool:
        _free_ip_pool.on_quota_exhausted(station)
    return bool(IP_ROTATION.get("strict_free", False)) and _free_stations_exhausted(free_model)


def _free_usage_ip(station=None) -> str:
    """Best-effort egress IP for free-model usage logging ([9]).

    Never does network I/O — prefers the live VPN IP of the station
    used (or station 1 as before), falls back to the cached ipify result
    from the last non-stream probe.
    """
    vpn = station or (_free_ip_pool.active_station if _free_ip_pool else None) or _vpn_manager
    if vpn and vpn.current_ip:
        return vpn.current_ip
    return _public_ip_cache.get("ip", "") or ""


class FreeQuotaExhausted(Exception):
    """Raised by _try_free_model_first in strict_free mode when a 429
    leaves no usable station (all stations bad/down and their (model, IP)
    cooldown keys still active). The caller converts this into a 429/503
    to the client with Retry-After — never a paid fallback."""

    def __init__(self, retry_after: str = ""):
        super().__init__(f"free quota exhausted on all VPN stations (retry-after={retry_after!r})")
        self.retry_after = retry_after


def _free_quota_exhausted_response(exc: FreeQuotaExhausted, protocol: str):
    """HTTP refusal for strict_free exhaustion (non-stream requests)."""
    retry_after = exc.retry_after or "60"
    if protocol == "anthropic":
        resp = _anthropic_error(429, f"Free quota exhausted on all VPN stations. Retry after {retry_after}s.",
                                error_type="rate_limit_error")
    else:
        resp = _openai_error(429, f"Free quota exhausted on all VPN stations. Retry after {retry_after}s.",
                             error_type="rate_limit_error")
    resp.headers["Retry-After"] = retry_after
    return resp


async def _try_free_model_first(body, headers, protocol, model_id):
    """Try the free model equivalent before falling back to paid.

    If the model has a free equivalent in FREE_MODEL_MAP, attempt the request
    via the free endpoint first. On 429 (quota exhausted) or any other
    non-200, returns None to signal the caller should use the paid model
    instead, and sets a per-(model, IP) cooldown ([4]).

    When VPN rotation is enabled, free requests go through a VPN tunnel.
    [0]/[42] restored: a 429 answers with a background IP rotation
    (single-flight) so the next free attempt lands on a fresh IP with a
    fresh cooldown key — exactly the path that unblocks the model.

    Every attempt is logged to free_model_usage table for quota analysis.

    Returns (response, headers, actual_model_name, free_ip) if free model succeeded,
    or None if fallback needed.
    """
    global _free_ip_pool

    free_model = FREE_MODEL_MAP.get(model_id)
    if not free_model:
        return None  # No free equivalent, proceed with paid

    # Dual-clutch pre-flight: pick the best station BEFORE the cooldown
    # check. With dual station, a hot (model, IP) key on the best station
    # is not a dead end — the OTHER station carries a different IP = a
    # fresh key, so switch immediately instead of falling back to paid.
    station = _free_ip_pool._best_station() if _free_ip_pool else None
    if _free_cooldown_active(free_model, station):
        other = _free_ip_pool._best_station_excluding(station) if (station is not None and _free_ip_pool) else None
        if other is not None and not _free_cooldown_active(free_model, other):
            _debug(f"  [free] station {station._station} (model, IP) cooldown active — "
                   f"dual-clutch switch to station {other._station}")
            station = other
        else:
            _debug(f"  [free] skipping free model {free_model!r} (cooldown active)")
            return None

    _debug(f"  [free] trying free model {free_model!r} instead of {model_id!r}")

    # Get current public IP for logging (VPN or direct)
    free_ip = ""
    vpn = station or _vpn_manager
    if vpn and vpn.current_ip:
        free_ip = vpn.current_ip
    else:
        free_ip = await _get_cached_public_ip()

    # Ensure VPN is connected if rotation is enabled; on_request returns
    # the BEST station (may differ from the pre-flight pick — e.g. the
    # preferred one rotated while we were fetching the public IP).
    if _free_ip_pool and _free_ip_pool.enabled:
        _, station = await _free_ip_pool.on_request()
        vpn = station or _vpn_manager
        if vpn and vpn.current_ip:
            free_ip = vpn.current_ip

    # Free models don't need authentication — minimal headers + identity UA
    free_profile = _current_free_identity(station)
    _current_free_attempt.set({"ip": free_ip, "identity": free_profile.get("impersonate") or "",
                               "station": station})
    free_headers = _apply_identity({"Content-Type": "application/json"}, free_profile)
    free_api_key = "free (no auth)"
    free_workspace = "free (no auth)"

    # Build free request: swap model name and endpoint
    free_body = dict(body)
    free_body["model"] = free_model

    t0 = time.monotonic()

    # Determine routing based on proxy mode
    proxy_mode = "direct"
    if _vpn_manager:
        proxy_mode = _vpn_manager.proxy_mode

    if proxy_mode == "vpn" and _free_ip_pool and _free_ip_pool.enabled:
        # VPN mode: use curl_cffi for TLS fingerprint evasion,
        # routed through the chosen station's tunnel (SOCKS5 in docker
        # mode) for a fresh IP — station.socks5_url guarantees the
        # request egresses the same station stamped in the contextvar.
        #
        # [incident 17/08 PAYANT] If the preferred station's tunnel fails
        # (transient SOCKS5 blip), try the OTHER station's tunnel BEFORE
        # the direct httpx fallback: the other station carries a different,
        # likely-fresh egress IP whose free quota is untouched. The direct
        # fallback egresses the residential IP (quota long consumed) — its
        # immediate 429 is exactly what produced the hour-long cooldown
        # poison. Direct is now ONLY reached when both tunnels are down
        # (paid fallback is correct behaviour then).
        attempt_stations = [station] if station is not None else []
        if attempt_stations and _free_ip_pool:
            other = _free_ip_pool._best_station_excluding(attempt_stations[0])
            if other is not None:
                attempt_stations.append(other)
        resp = resp_headers = None
        for idx, attempt in enumerate(attempt_stations):
            if idx > 0:
                _log(f"  FREE via station {attempt_stations[0]._station} tunnel FAILED → "
                     f"dual-clutch to station {attempt._station} tunnel")
            try:
                resp = await _do_free_request_curl_cffi(free_body, free_headers,
                                                        attempt.socks5_url, station=attempt)
                resp_headers = resp.headers
                if attempt is not attempt_stations[0]:
                    station = attempt  # cooldown key + on_quota_exhausted must target THIS IP
                    if attempt.current_ip:
                        free_ip = attempt.current_ip
                    _current_free_attempt.set({"ip": free_ip,
                                               "identity": _current_free_identity(attempt).get("impersonate") or "",
                                               "station": attempt})
                break
            except Exception as e:
                _debug(f"  [free] curl_cffi error (station {attempt._station}): {e}")
        if resp is None:
            # Both tunnels down → last-resort direct fallback (residential IP).
            _log("  FREE via BOTH VPN tunnels FAILED → direct fallback (residential IP)")
            try:
                resp, resp_headers = await _do_request_with_retry(
                    API_BASE_FREE, free_body, free_headers, protocol, retry_on_429=False
                )
            except UpstreamError:
                _log_free_model_usage(model_id, free_model, free_api_key,
                                      free_workspace, 502, ip=free_ip)
                return None
    else:
        # Direct mode: no proxy
        try:
            resp, resp_headers = await _do_request_with_retry(
                API_BASE_FREE, free_body, free_headers, protocol, retry_on_429=False
            )
        except UpstreamError:
            _log_free_model_usage(model_id, free_model, free_api_key,
                                  free_workspace, 502, ip=free_ip)
            return None

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    # Extract token usage from response
    tokens_in = tokens_out = 0
    if resp.status_code == 200:
        try:
            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
            usage = data.get("usage", {})
            tokens_in = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
            tokens_out = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
        except Exception:
            pass

    # Log every attempt
    _log_free_model_usage(model_id, free_model, free_api_key,
                          free_workspace, resp.status_code,
                          tokens_in, tokens_out, elapsed_ms, ip=free_ip)

    if resp.status_code == 200:
        _debug(f"  [free] {free_model!r} succeeded ({tokens_in}+{tokens_out} tokens)")
        _log(f"  FREE {free_model!r} OK ({tokens_in}+{tokens_out} tokens, saved paid quota)")
        return resp, resp_headers, free_model, free_ip

    # 429 = free quota exhausted → fall back to paid silently
    if resp.status_code == 429:
        # Read and log the 429 response body for quota analysis
        try:
            body_429 = resp.text[:500] if hasattr(resp, 'text') else ''
        except Exception:
            body_429 = ''
        # Extract retry-after header (seconds until quota resets)
        retry_after = resp.headers.get('retry-after', '')
        # Also log response headers (may contain X-RateLimit-*)
        headers_429 = {k: v for k, v in resp.headers.items()
                       if k.lower() in ('retry-after', 'x-ratelimit-limit',
                                         'x-ratelimit-remaining', 'x-ratelimit-reset',
                                         'content-type')}
        _debug(f"  [free] {free_model!r} 429 body={_redact(body_429)!r} headers={headers_429}")
        _log(f"  FREE {free_model!r} RATE LIMITED (429) retry-after={retry_after}s → falling back to paid {model_id!r}")

        # Per-(model, IP) cooldown honoring the upstream retry-after ([4])
        # + background IP rotation ([0]/[42]): the key is (model, the
        # station's current IP), so the rotation makes the next free
        # attempt a fresh key.
        _set_free_cooldown(free_model, _free_429_cooldown_seconds(retry_after), station)
        if _free_ip_pool:
            _free_ip_pool.on_quota_exhausted(station)

        # strict_free (GUI): when EVERY station is exhausted, refuse
        # instead of paying — the caller answers 429/503 with Retry-After.
        if IP_ROTATION.get("strict_free", False) and _free_stations_exhausted(free_model):
            raise FreeQuotaExhausted(retry_after)

        return None

    # Other errors → cooldown (60 s) so the next request doesn't retry the
    # free model immediately, then fall back to paid
    _set_free_cooldown(free_model, 60, station)
    _debug(f"  [free] {free_model!r} returned {resp.status_code} → falling back to paid")
    return None


async def _do_request_with_retry(endpoint, body, headers, protocol, retry_on_429=True):
    """POST request with automatic 429/401 key failover and 5xx retry with backoff.

    Key failover (429/401) does NOT consume a retry attempt — it's a key change,
    not a retry. Only 5xx errors consume retries.

    Returns (response, final_headers) -- headers may differ after retry.
    Raises UpstreamError on connection/timeout/protocol failures.
    """
    _RETRYABLE_STATUSES = {502, 503, 504, 499}
    max_retries = yaml_get("streaming", "retry_attempts", 2)
    attempt = 0

    while attempt < max_retries:
        if DEBUG:
            _debug(f"  → upstream POST {endpoint} attempt {attempt+1}/{max_retries} headers={_sanitize_headers(headers)}")
        t0 = time.monotonic()
        try:
            resp = await _ensure_http_client().post(endpoint, json=body, headers=headers)
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

        # Key failover on 429 — does NOT consume a retry attempt
        if retry_on_429 and resp.status_code == 429 and len(API_KEYS) > 1:
            failed_key = _key_from_headers(headers, protocol)
            # Fetch fresh quotas and pause key until exact reset time
            await _pause_key_for_quota_reset(failed_key)
            alt = _find_alternative_key(failed_key)
            if alt:
                _debug(f"  ⟳ 429 failover: {failed_key[:8]}… → alias={alt.get('alias', '?')}")
                _log(f"  429 on key, retrying with alternative key")
                headers = _get_auth_headers(protocol, entry=alt)
                continue  # retry immediately without incrementing attempt

        # Key failover on 401 — does NOT consume a retry attempt
        if resp.status_code == 401 and len(API_KEYS) > 1:
            failed_key = _key_from_headers(headers, protocol)
            # Log the 401 response body to help diagnose dead/revoked keys
            try:
                _err_body = resp.text[:500] if hasattr(resp, 'text') else str(resp.content[:500])
            except Exception:
                _err_body = "(unable to read response body)"
            _debug(f"  [auth] 401 response body: {_redact(_err_body, 500)}")
            # 401 = key temporarily unavailable (quota exhausted) → pause 1h
            _key_pauser.pause_key(failed_key, 3600, "401 Unauthorized (temporary)")
            alt = _find_alternative_key(failed_key)
            if alt:
                _debug(f"  ⟳ 401 failover: {failed_key[:8]}… → alias={alt.get('alias', '?')}")
                _log(f"  401 on key (invalid/revoked), retrying with alternative key")
                headers = _get_auth_headers(protocol, entry=alt)
                continue  # retry immediately without incrementing attempt

        # Key failover on 403 — does NOT consume a retry attempt
        # 403 = region/model blocked for the key (e.g. RegionError). Pause the
        # key and try another account before surfacing anything to the client.
        if retry_on_429 and resp.status_code == 403 and len(API_KEYS) > 1:
            failed_key = _key_from_headers(headers, protocol)
            try:
                _err_body = resp.text[:500] if hasattr(resp, 'text') else str(resp.content[:500])
            except Exception:
                _err_body = "(unable to read response body)"
            _debug(f"  [auth] 403 response body: {_redact(_err_body, 500)}")
            _key_pauser.pause_key(failed_key, 1800, "403 Forbidden (region/model not allowed)")
            alt = _find_alternative_key(failed_key)
            if alt:
                _debug(f"  ⟳ 403 failover: {failed_key[:8]}… → alias={alt.get('alias', '?')}")
                _log(f"  403 on key, retrying with alternative key")
                headers = _get_auth_headers(protocol, entry=alt)
                continue  # retry immediately without incrementing attempt

        # Retry on 502/503/504 with backoff — DOES consume a retry attempt
        if resp.status_code in _RETRYABLE_STATUSES and attempt < max_retries - 1:
            wait = 1.0 * (2 ** attempt)  # 1s, 2s
            _debug(f"  ⟳ retry {resp.status_code} in {wait:.1f}s")
            _log(f"  RETRY {resp.status_code} after {wait:.1f}s (attempt {attempt+1}/{max_retries})")
            await asyncio.sleep(wait)
            attempt += 1
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
                                 tools_used=None, request_body=None, response_body=None,
                                 free_model_ip=None, identity=None):
    """Log success and save to DB with success=True."""
    # Free-channel stamp first: the IP/identity of the free attempt beats the
    # current IP (mid-stream 429 + background rotation must keep the pre-rotation
    # IP). Falls back to auto-detect for -free models when no stamp exists.
    if free_model_ip is None or identity is None:
        _attempt = _current_free_attempt.get()
        if _attempt:
            if free_model_ip is None:
                free_model_ip = _attempt.get("ip") or None
            if identity is None:
                identity = _attempt.get("identity") or None
    if free_model_ip is None and "-free" in (model_id or ""):
        # Station actually used by the free attempt (dual station) beats
        # station 1's IP — a mid-stream 429 + background rotation must
        # keep the pre-rotation station IP.
        _station = _free_ip_pool.active_station if _free_ip_pool else None
        vpn = _station or _vpn_manager
        if vpn and vpn.current_ip:
            free_model_ip = vpn.current_ip
        else:
            free_model_ip = await _get_cached_public_ip()

    alias_tag = f" | account={account_alias}" if account_alias else ""
    if free_model_ip:
        alias_tag = f" | vpn_ip={free_model_ip}"
    _log(f"  ← {model_id} | +{inp} in{log_tag} | +{out} out{log_tag} | +{cache} cache{alias_tag}")
    try:
        await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                     inp, out, cache, success=True,
                     protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                     client_ip=client_ip, account_alias=account_alias, tools=tools,
                     tools_used=tools_used, request_body=request_body, response_body=response_body,
                     free_model_ip=free_model_ip, identity=identity)
    except Exception as e:
        _debug(f"  ✗ save_request failed: {type(e).__name__}: {e}")
        _log(f"  WARN: save_request failed: {type(e).__name__}: {e}")


async def _log_and_save_error(req_id, model_id, original_model, start_time,
                               status_code, resp_text, protocol, is_stream, thinking_type,
                               effort, client_ip, account_alias, tools, tools_used=None,
                               request_body=None, response_body=None):
    """Log error and save to DB with success=False."""
    _log(f"  ERROR {status_code}: {_redact(resp_text, 300)}")
    try:
        await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                     0, 0, 0, success=False, error=f"HTTP {status_code}",
                     protocol=protocol, is_stream=is_stream, thinking=thinking_type, effort=effort,
                     client_ip=client_ip, account_alias=account_alias, tools=tools,
                     tools_used=tools_used, request_body=request_body, response_body=response_body)
    except Exception as e:
        _debug(f"  ✗ save_request (error path) failed: {type(e).__name__}: {e}")
        _log(f"  WARN: save_request (error path) failed: {type(e).__name__}: {e}")


# ── Streaming helpers ───────────────────────────────────────────

def _make_stream_retry_loop(protocol):
    """Return a retry function for streaming 429/401 handling.

    Returns (attempt_headers, should_retry) where should_retry=True means
    the caller should `continue` the outer loop.
    """
    async def _handle_429(headers, status_code, attempt, resp_headers=None):
        if attempt == 0 and len(API_KEYS) > 1 and status_code in (429, 401, 403, 400):
            failed_key = _key_from_headers(headers, protocol)
            if status_code == 429:
                # Fetch fresh quotas and pause key until exact reset time
                await _pause_key_for_quota_reset(failed_key)
            elif status_code == 401:
                # 401 = key permanently invalid/revoked → pause 24h
                _key_pauser.pause_key(failed_key, 86400, "401 Unauthorized (key likely revoked)")
            elif status_code == 403:
                # 403 = region/model blocked for key → pause 30min
                _key_pauser.pause_key(failed_key, 1800, "403 Forbidden (region/model not allowed)")
            elif status_code == 400:
                # Check for credit/balance errors in resp_headers or skip
                # We need to check the response body - handled in caller
                pass
            alt = _find_alternative_key(failed_key)
            if alt:
                _log(f"  {status_code} on key, retrying with alternative key")
                return _get_auth_headers(protocol, entry=alt), True
        return headers, False
    return _handle_429


async def _stream_error_response(req_id, model_id, original_model, start_time,
                                  status_code, resp_body, protocol, thinking_type,
                                  effort, client_ip, account_alias, tools,
                                  error_payload, tools_used=None, request_body=None):
    """Handle streaming error: log, save DB, yield error SSE event. Returns the error event bytes."""
    _log(f"  ERROR {status_code}: {_redact(resp_body, 300)}")
    await _save_request(req_id, model_id, original_model, _elapsed_ms(start_time),
                 0, 0, 0, success=False, error=f"HTTP {status_code}",
                 protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                 client_ip=client_ip, account_alias=account_alias, tools=tools,
                 tools_used=tools_used, request_body=request_body)
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
_SSE_KEEPALIVE_INTERVAL = yaml_get("streaming", "sse_keepalive_interval", 15)  # seconds


async def _sse_keepalive(stream_gen, interval: float = _SSE_KEEPALIVE_INTERVAL):
    """Wrap an async generator to inject SSE keepalive comments during idle periods.

    When no chunk is yielded for `interval` seconds, sends `: ping\\n\\n`
    (a comment line, ignored by SSE clients) to keep the connection alive.

    IMPORTANT: the ping timer RACES the upstream read and never cancels it.
    asyncio.wait_for(anext(...), timeout=interval) would CANCEL the pending
    anext on a long upstream silence — CancelledError (a BaseException that
    `except Exception` in the stream generators cannot catch) is thrown into
    the inner generator, killing the upstream connection, so a stall > 15s
    terminated the whole stream with EOF and no message_stop. Here the read
    task keeps waiting across pings: a slow first chunk or a stalled VPN
    tunnel no longer kills the stream, it just gets bridged by pings.
    """
    read_task = ping_task = None
    try:
        while True:
            if read_task is None or read_task.done():
                read_task = asyncio.ensure_future(anext(stream_gen))
            if ping_task is None or ping_task.done():
                ping_task = asyncio.ensure_future(asyncio.sleep(interval))
            done, _pending = await asyncio.wait(
                {read_task, ping_task}, return_when=asyncio.FIRST_COMPLETED)
            if read_task in done:
                ping_task.cancel()
                # NOTE: a cancelled task still reports done() == False until the
                # event loop delivers the cancellation, so the loop-top guard
                # would re-pass the STALE cancelled task to asyncio.wait → it
                # completes instantly → bogus ping at t=0. Recycle explicitly.
                ping_task = None
                try:
                    chunk = read_task.result()
                except StopAsyncIteration:
                    return
                except Exception as e:
                    # ClientDisconnect, ConnectionResetError, etc. — client gone, stop gracefully
                    # Log traceback for unexpected errors (like UnboundLocalError) to aid debugging
                    if isinstance(e, (ConnectionError, OSError)):
                        _debug(f"  [stream] keepalive exiting (client gone): {type(e).__name__}: {e}")
                    else:
                        _debug(f"  [stream] keepalive exiting: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                    return
                yield chunk
            else:
                # Ping fired while the upstream read is still pending — keep
                # the read alive (it stays pending in read_task) and let the
                # client know the proxy is still there.
                yield b": ping\n\n"
    finally:
        if ping_task is not None:
            ping_task.cancel()
        if read_task is not None and not read_task.done():
            # Only reached on a client disconnect / generator teardown —
            # THERE the upstream abort is correct (client is gone).
            read_task.cancel()


def _route_for(model_name: str, tool_names: list = None) -> dict | None:
    maybe_reload_custom_routes()
    name = model_name.lower().strip()
    if not name:
        return None
    # When DISABLE_MAPPING, only check custom routes (not auto-generated aliases)
    if DISABLE_MAPPING:
        for r in _cfg_settings.SORTED_CUSTOM_ROUTES:
            if any(m in name for m in r.get("match", [])):
                _debug(f"  [route] DISABLE_MAPPING custom match: '{name}' → {r.get('model')}")
                return r
        # No custom route matched — check if the model exists directly
        if name in MODELS:
            _debug(f"  [route] DISABLE_MAPPING direct model: '{name}'")
            return {"match": [name], "model": model_name}
        _debug(f"  [route] DISABLE_MAPPING no match for '{name}'")
        return None
    # 1. Tool-based routing (sorted by longest match first)
    tool_names_lower = [t.lower() for t in (tool_names or [])]
    for r in _cfg_settings.SORTED_ROUTES:
        if r.get("enabled") is False:
            continue
        if tool_names_lower and any(m in t for m in r.get("match", []) for t in tool_names_lower):
            _debug(f"  [route] tool-based match: {r.get('model')} (tool match)")
            return r
    # 2. Model-based routing (sorted by longest match first)
    for r in _cfg_settings.SORTED_ROUTES:
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


def _tool_name(tool: dict) -> str:
    """Extract tool name from either Anthropic or OpenAI format."""
    if not isinstance(tool, dict):
        return ""
    if "name" in tool and isinstance(tool["name"], str):
        return tool["name"]
    fn = tool.get("function")
    if isinstance(fn, dict) and "name" in fn and isinstance(fn["name"], str):
        return fn["name"]
    return ""


def _inject_system_hint(body: dict, hint: str):
    """Inject a system prompt hint into the request body.

    Works for both Anthropic format (body['system']) and OpenAI format (messages with system role).
    """
    if not hint:
        return
    # Anthropic format: body has "system" field
    if "system" in body:
        existing = body["system"]
        if isinstance(existing, str):
            body["system"] = (hint + "\n\n" + existing) if existing else hint
        elif isinstance(existing, list):
            body["system"] = [{"type": "text", "text": hint}] + existing
    # OpenAI format: messages array with system role
    elif "messages" in body:
        for msg in body["messages"]:
            if msg.get("role") in ("system", "developer"):
                msg["content"] = hint + "\n\n" + msg.get("content", "")
                return
        body["messages"].insert(0, {"role": "system", "content": hint})


def _filter_tools_for_model(body: dict, model_id: str) -> list:
    """Filter tools in request body based on model capabilities.

    Mutates body in place. Returns the list of tool names actually sent.

    Applies:
    1. Whitelist (supported_tools) if set
    2. Blacklist (unsupported_tools) if set
    3. System hint injection if configured
    """
    import config as _cfg

    # Fast path: no config at all
    if not _cfg.TOOL_CAPABILITIES:
        return _extract_tool_names(body)

    cfg = _cfg.get_tool_config(model_id)
    supported = cfg.get("supported_tools")
    unsupported = cfg.get("unsupported_tools") or []
    hint = cfg.get("system_hint")

    # No filtering needed
    if not supported and not unsupported and not hint:
        return _extract_tool_names(body)

    tools = body.get("tools")
    if not isinstance(tools, list) or not tools:
        # No tools in request — inject hint if configured and tools exist
        if hint and body.get("tools"):
            _inject_system_hint(body, hint)
        return []

    original_count = len(tools)

    if supported is not None:
        # Whitelist mode: keep only supported tools
        allowed = set(supported)
        tools = [t for t in tools if _tool_name(t) in allowed]
    elif unsupported:
        # Blacklist mode: remove unsupported tools
        blocked = set(unsupported)
        tools = [t for t in tools if _tool_name(t) not in blocked]

    body["tools"] = tools

    removed = original_count - len(tools)
    if removed > 0:
        _debug(f"  [tool-filter] {model_id}: filtered {removed}/{original_count} tools")

    # Inject system hint if tools are present
    if hint and tools:
        _inject_system_hint(body, hint)

    kept = [_tool_name(t) for t in tools]
    return kept


# ── Web Search Handler ───────────────────────────────────────────────

def _is_web_search_forced(body: dict, protocol: str) -> bool:
    """Check if tool_choice is forced to web_search."""
    tc = body.get("tool_choice")
    if not isinstance(tc, dict):
        return False
    if protocol == "anthropic":
        return tc.get("type") == "tool" and tc.get("name") == "web_search"
    else:
        return tc.get("type") == "function" and tc.get("function", {}).get("name") == "web_search"


def _extract_search_query(body: dict) -> str:
    """Extract the search query from the last user message."""
    messages = body.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                # Look for "Perform a web search for the query: ..." pattern
                if "web search" in content.lower():
                    for line in content.split("\n"):
                        if "query:" in line.lower():
                            return line.split(":", 1)[1].strip()
                return content[:500]
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        if "web search" in text.lower():
                            for line in text.split("\n"):
                                if "query:" in line.lower():
                                    return line.split(":", 1)[1].strip()
                        return text[:500]
    return ""


def _execute_ddg_search(query: str, max_results: int = 5) -> str:
    """Execute a DuckDuckGo search and return formatted results."""
    try:
        from duckduckgo_search import DDGS
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=max_results))
        if not results:
            return f"No results found for: {query}"
        lines = [f"Web search results for '{query}':\n"]
        for i, r in enumerate(results, 1):
            title = r.get("title", "")
            body_text = r.get("body", "")
            href = r.get("href", "")
            lines.append(f"{i}. **{title}**\n   {body_text}\n   {href}\n")
        return "\n".join(lines)
    except Exception as e:
        return f"Search error: {type(e).__name__}: {e}"


def _inject_search_results(body: dict, results: str, protocol: str):
    """Inject search results as a system message in the request body."""
    if protocol == "anthropic":
        existing = body.get("system", "")
        if isinstance(existing, str):
            body["system"] = results + "\n\n" + existing if existing else results
        elif isinstance(existing, list):
            body["system"] = [{"type": "text", "text": results}] + existing
    else:
        # OpenAI format — inject as system message at the beginning
        messages = body.get("messages", [])
        messages.insert(0, {"role": "system", "content": results})
        body["messages"] = messages


def _strip_web_search_tool(body: dict, protocol: str):
    """Remove web_search tool and forced tool_choice from the body."""
    # Remove web_search from tools
    if "tools" in body:
        if protocol == "anthropic":
            body["tools"] = [t for t in body["tools"] if t.get("name") != "web_search"]
        else:
            body["tools"] = [t for t in body["tools"]
                            if t.get("function", {}).get("name") != "web_search"]
        if not body["tools"]:
            del body["tools"]

    # Remove forced tool_choice
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        is_forced_web_search = (
            (tc.get("type") == "tool" and tc.get("name") == "web_search") or
            (tc.get("type") == "function" and tc.get("function", {}).get("name") == "web_search")
        )
        if is_forced_web_search:
            del body["tool_choice"]


async def _handle_web_search(body: dict, model_id: str, protocol: str) -> bool:
    """Handle web_search tool by executing locally or routing to a compatible model.

    Returns True if the request was handled locally (caller should return synthetic response).
    Returns False if the request should proceed normally to upstream.
    """
    if not _is_web_search_forced(body, protocol):
        return False

    mode = yaml_get("web_search", "mode", "duckduckgo")
    target_model = yaml_get("web_search", "target_model", None)
    max_results = yaml_get("web_search", "max_results", 5)
    query = _extract_search_query(body)

    _debug(f"  [web-search] mode={mode} model={model_id} query={query[:80]}...")

    if mode == "duckduckgo":
        # Execute search locally and inject results (offload to thread to avoid blocking event loop)
        results = await asyncio.to_thread(_execute_ddg_search, query, max_results)
        _inject_search_results(body, results, protocol)
        _strip_web_search_tool(body, protocol)
        _log(f"  WEB SEARCH: DuckDuckGo query='{query[:60]}' → injected results")
        return False  # Let upstream process the request with search context

    elif mode == "model" and target_model:
        # Replace model with one that supports web_search
        body["model"] = target_model
        _log(f"  WEB SEARCH: routed to {target_model} for web_search")
        return False  # Let upstream process with the compatible model

    elif mode == "ddg_then_model" and target_model:
        # Try DuckDuckGo first, fallback to model on failure (offload to thread)
        results = await asyncio.to_thread(_execute_ddg_search, query, max_results)
        if "error" in results.lower() or "no results" in results.lower():
            body["model"] = target_model
            _log(f"  WEB SEARCH: DDG failed, routed to {target_model}")
        else:
            _inject_search_results(body, results, protocol)
            _strip_web_search_tool(body, protocol)
            _log(f"  WEB SEARCH: DuckDuckGo query='{query[:60]}' → injected results")
        return False

    elif mode == "model_then_ddg":
        # Let upstream try first (will handle via 400 fallback)
        if target_model:
            body["model"] = target_model
        _log(f"  WEB SEARCH: model-first for web_search (fallback=DDG)")
        return False

    # Default: strip the tool and let upstream handle without it
    _strip_web_search_tool(body, protocol)
    _log(f"  WEB SEARCH: stripped web_search tool (mode={mode})")
    return False


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
CACHE_REWRITE_MODELS = set(yaml_get("cache_rewrite_models", default=["mimo-v2.5", "mimo-v2-pro", "mimo-v2-omni", "mimo-v2.5-pro"]))


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
    # GLM-5.x models don't support cache_control — skip it
    supports_cache_control = not model.startswith("glm-5")

    messages = []

    # System prompt — always add cache_control for prefix caching
    system_val = body.get("system", "")
    if isinstance(system_val, list):
        text = _extract_text(system_val)
        if text:
            text = _strip_billing_header(text)
            msg = {"role": "system", "content": text}
            if supports_cache_control:
                msg["cache_control"] = {"type": "ephemeral"}
            messages.append(msg)
    elif system_val:
        msg = {"role": "system", "content": _strip_billing_header(system_val)}
        # Always add cache_control to system messages for prefix caching
        if supports_cache_control:
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
            if last_cache_control and not is_asst and supports_cache_control:
                out["cache_control"] = last_cache_control
            messages.append(out)

    # Add cache_control to the last user message for optimal prefix caching
    # (Anthropic best practice: cache system + last user turn)
    if supports_cache_control:
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
        elif model.startswith("glm-5"):
            # GLM-5.x supports reasoning_effort like mimo-v2.5
            if effort_level in ("xhigh", "max"):
                oai["reasoning_effort"] = "high"
            elif effort_level in ("high",):
                oai["reasoning_effort"] = "high"
            elif effort_level in ("medium",):
                oai["reasoning_effort"] = "medium"
            else:
                oai["reasoning_effort"] = "low"
            _debug(f"  [thinking] {model}: reasoning_effort={oai['reasoning_effort']} (effort={effort_level})")
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
        elif model.startswith("glm-5"):
            # GLM-5.x supports reasoning_effort like mimo-v2.5
            if effort_level in ("xhigh", "max"):
                result["reasoning_effort"] = "high"
            elif effort_level in ("high",):
                result["reasoning_effort"] = "high"
            elif effort_level in ("medium",):
                result["reasoning_effort"] = "medium"
            else:
                result["reasoning_effort"] = "low"
            _debug(f"  [thinking] {model}: reasoning_effort={result['reasoning_effort']} (effort={effort_level})")
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
_thinking_cfg = yaml_get("thinking", "min_tokens", {})
THINKING_MODELS = {k: int(v) for k, v in _thinking_cfg.items()} if isinstance(_thinking_cfg, dict) else {
    "deepseek-v4-flash": 2048,
    "deepseek-v4-pro": 4096,
}

def ensure_min_tokens(body: dict, default: int = None) -> dict:
    if default is None:
        default = yaml_get("thinking", "default_min_tokens", 256)
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
    return int((time.monotonic() - start_time) * 1000)


def _extract_usage_tool_names(data: dict) -> list:
    """Extract tool call names from an OpenAI chat response, tolerating
    missing choices / message / tool_calls (upstream may return null).

    Distinct from _extract_tool_names() (request side): a duplicate
    definition here used to shadow the request-side helper at module
    scope, breaking request-side tool extraction ([43])."""
    choices = data.get("choices") or []
    if not choices or not isinstance(choices, list):
        return []
    msg = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not msg or not isinstance(msg, dict):
        return []
    return [tc["function"]["name"] for tc in (msg.get("tool_calls") or [])
            if isinstance(tc, dict) and isinstance(tc.get("function"), dict)]


# ── Shared helpers for endpoint handlers ──────────────────────────

def _get_client_ip(request: Request) -> str:
    """Extract client IP from the socket peer only.

    X-Forwarded-For is spoofable by any direct client and is never
    trusted (CRITIC 10). Trusted-proxy deployments can re-enable it
    behind a dedicated reverse proxy.
    """
    return request.client.host if request.client else "unknown"


@app.api_route("/v1/messages", methods=["POST"])
@app.api_route("/anthropic/v1/messages", methods=["POST"])
async def messages(request: Request):
    req_id = _fast_id("msg")
    start_time = time.monotonic()
    client_ip = _get_client_ip(request)
    _current_user_agent.set(request.headers.get("user-agent"))

    body_bytes = await request.body()
    _debug(f"  [body] read {len(body_bytes)} bytes in {(time.monotonic() - start_time) * 1000:.0f}ms")
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
    request_body = body  # Capture original request before mutation
    _debug(f"[messages] req_id={req_id} model={original_model!r} tools={tool_names} ip={client_ip}")
    if DEBUG:
        _debug(f"[messages] headers={_sanitize_headers(dict(request.headers))}")
        _debug(f"[messages] body=\n{_redact(_truncate(body))}")
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

    # Filter tools based on model capabilities
    tool_names = _filter_tools_for_model(body, model_id)

    # Handle web_search tool (DuckDuckGo or model routing)
    await _handle_web_search(body, model_id, protocol)

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
        is_stream = body.get("stream", False)

        # ── Free model: try BEFORE auth (free models don't need API keys) ──
        if not is_stream and FREE_MODEL_MAP.get(model_id):
            try:
                free_result = await _try_free_model_first(body, {}, "anthropic", model_id)
                if free_result is not None:
                    resp, _, _actual_model, _actual_ip = free_result
                    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                    usage = data.get("usage", {})
                    req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                    req_out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                    req_cache = usage.get("cache_read_input_tokens", 0)
                    _update_token_usage(_actual_model, req_in, req_out, req_cache)
                    used = [b["name"] for b in data.get("content", []) if b.get("type") == "tool_use"]
                    await _save_and_log_request(req_id, _actual_model, original_model, start_time,
                                 req_in, req_out, req_cache, protocol, False, thinking_type,
                                 effort, client_ip, "free (no auth)", tool_names, tools_used=used,
                                 request_body=request_body, response_body=data, free_model_ip=_actual_ip)
                    return Response(content=resp.content, media_type="application/json")
            except FreeQuotaExhausted as e:
                return _free_quota_exhausted_response(e, "anthropic")
            except Exception as e:
                _debug(f"  [free] free model attempt failed: {e}")

        try:
            a_headers = _get_auth_headers("anthropic")
        except AllKeysPausedError as e:
            # If a free model exists, try streaming with empty headers before giving up
            if is_stream and FREE_MODEL_MAP.get(model_id):
                async def anthropic_stream_free_fallback():
                    # Re-use the existing generator — it already handles free model swap
                    async for chunk in anthropic_stream({}):
                        yield chunk
                return StreamingResponse(_sse_keepalive(anthropic_stream_free_fallback()), media_type="text/event-stream",
                                         headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
            retry_after = int(e.retry_after) + 1
            return Response(
                content=json.dumps({"type": "error", "error": {"type": "api_error",
                    "message": f"All API keys exhausted. Retry after {retry_after}s."}}),
                status_code=503, media_type="application/json",
                headers={"Retry-After": str(retry_after)})

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

            # Try free model first if available
            try:
                free_result = await _try_free_model_first(body, a_headers, "anthropic", model_id)
                if free_result is not None:
                    resp, a_headers, _actual_model, _actual_ip = free_result
                    model_id = _actual_model  # Log as free model
                else:
                    resp, a_headers = await _do_request_with_retry(endpoint, body, a_headers, "anthropic")
            except FreeQuotaExhausted as e:
                return _free_quota_exhausted_response(e, "anthropic")
            except UpstreamError as e:
                _debug(f"  ✗ upstream error: {e}")
                return JSONResponse(status_code=e.status_code, content={"error": str(e)})
            account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
            if resp.status_code != 200:
                # Normalize 429/401/403 → 503 to avoid Claude Code auth window
                # Convert 499 → 502 (upstream disconnected, non-standard code)
                status = _safe_client_status(resp.status_code)
                if resp.status_code == 429:
                    msg = "All API keys exhausted (rate limited). Try again later."
                elif resp.status_code == 401:
                    msg = "All API keys exhausted (unauthorized). Check your API keys."
                elif resp.status_code == 403:
                    msg = _auth_window_message(403)
                elif resp.status_code == 499:
                    msg = "Upstream disconnected (499). Retrying may help."
                else:
                    msg = resp.text[:500]
                _debug(f"  ✗ upstream {resp.status_code} → client {status}: {_redact(msg, 300)}")
                await _log_and_save_error(req_id, model_id, original_model, start_time,
                             resp.status_code, resp.text, protocol, is_stream, thinking_type,
                             effort, client_ip, account_alias, tool_names,
                             request_body=request_body, response_body={"error": resp.text[:2000]})
                # Pause key on credit/balance errors (400) and retry with alt key
                if resp.status_code == 400 and any(x in resp.text for x in ("Insufficient balance", "Monthly usage limit")):
                    failed_key = headers.get("x-api-key", "")
                    _key_pauser.pause_key(failed_key, _key_pauser._max_pause, f"400 credit error: {_redact(resp.text, 80)}")
                    alt = _find_alternative_key(failed_key)
                    if alt:
                        _log(f"  400 credit error on key, retrying with alternative key")
                        headers = {"x-api-key": alt.get("api_key", ""), "Content-Type": "application/json",
                                   "anthropic-version": "2023-06-01"}
                        try:
                            resp, headers = await _do_request_with_retry(endpoint, body, headers, "anthropic")
                            if resp.status_code == 200:
                                account_alias = _alias_for_key(headers.get("x-api-key", ""))
                            else:
                                return _anthropic_error(503, "All API keys exhausted. Check your billing.")
                        except UpstreamError as e:
                            return _anthropic_error(e.status_code, str(e))
                if resp.status_code in (429, 401, 403):
                    return _anthropic_error(503, msg)
                if resp.status_code == 499:
                    return _anthropic_error(502, msg)
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
            _debug(f"  response=\n{_redact(_truncate(data))}")
            _update_token_usage(model_id, req_in, req_out, req_cache)
            used = [b["name"] for b in data.get("content", []) if b.get("type") == "tool_use"]
            await _save_and_log_request(req_id, model_id, original_model, start_time,
                         req_in, req_out, req_cache, protocol, is_stream, thinking_type,
                         effort, client_ip, account_alias, tool_names, tools_used=used,
                         request_body=request_body, response_body=data)
            if cache_key:
                _response_cache.put(cache_key, resp.content, {"Content-Type": "application/json"})
            return Response(content=resp.content, headers={"X-Cache": "MISS"}, media_type="application/json")


        async def anthropic_stream(headers):
            nonlocal endpoint, body, model_id
            # _track_model is the model used for logging/token tracking.
            # We use a local variable instead of reassigning model_id to avoid
            # UnboundLocalError in Python 3.12+ (nonlocal + assignment conflict).
            _track_model = model_id
            # Try free model for streaming: swap endpoint/model before starting stream
            free_model = FREE_MODEL_MAP.get(model_id)
            if free_model:
                _debug(f"  [stream] attempting free model {free_model!r} first")
                # Save paid config in case we need to fall back
                paid_endpoint = endpoint
                paid_body = dict(body)
                paid_body["model"] = model_id
                # Swap to free
                body = dict(body)
                body["model"] = free_model
                endpoint = API_BASE_FREE
                _using_free = True
                _track_model = free_model  # Log as free model
            else:
                _using_free = False
            # Defer token estimation to first chunk — reduces pre-response latency
            est_input = await asyncio.to_thread(_estimate_input_tokens, body)
            _debug(f"  [stream] est_input={est_input}")
            _update_token_usage(_track_model, est_input, 0, 0)
            stream_in = None
            stream_out = stream_cache = 0
            _line_buf = ""
            started = False
            open_blocks = []
            stop_reason = "end_turn"
            _handle_429 = _make_stream_retry_loop("anthropic")
            for _attempt in range(yaml_get("streaming", "retry_attempts", 2)):
                used_tools = []  # Reset on each retry attempt
                _debug(f"  [stream] attempt {_attempt+1}/2")
                try:
                    async with _open_free_stream(endpoint, body, headers, _using_free,
                                                 count_request=(_attempt == 0),
                                                 fresh_station=(_attempt > 0 and _using_free)) as resp:
                        _debug(f"  [stream] connected status={resp.status_code}")
                        if resp.status_code != 200:
                            if _using_free:
                                # Free endpoint non-200 (429 quota, 5xx...) → cooldown
                                # the free model, fall back to paid. Never pauses PAID
                                # keys: a status from the free endpoint says nothing
                                # about the paid account (CRITIC(2)/CRITIC(3)).
                                if resp.status_code == 429:
                                    _refuse = _on_free_429_stream(free_model, resp.headers.get('retry-after', ''))
                                else:
                                    _set_free_cooldown(free_model, 60, _free_attempt_station())
                                    _refuse = False
                                body = paid_body
                                endpoint = paid_endpoint
                                _using_free = False
                                _track_model = model_id  # Revert tracking to paid model
                                _debug(f"  [stream] free model {resp.status_code} → falling back to paid {_track_model!r}")
                                _log(f"  FREE model {resp.status_code} → falling back to paid {_track_model!r}")
                                _log_free_model_usage(model_id, free_model, "free (no auth)", "free (no auth)",
                                                      resp.status_code, ip=_free_usage_ip())
                                if _refuse:
                                    # strict_free (GUI): every station exhausted
                                    # (bad/down + (model, IP) cooldown active) —
                                    # refuse instead of paying.
                                    _retry_after = resp.headers.get('retry-after', '') or "60"
                                    yield await _stream_error_response(
                                        req_id, free_model, original_model, start_time,
                                        429, await resp.aread(), protocol, thinking_type,
                                        effort, client_ip, "free (no auth)", tool_names,
                                        {"type": "error",
                                         "error": {"type": "rate_limit_error",
                                                   "message": f"Free quota exhausted on all VPN stations. Retry after {_retry_after}s."}},
                                        request_body=request_body)
                                    return
                                continue
                            headers, should_retry = await _handle_429(headers, resp.status_code, _attempt, resp.headers)
                            if should_retry:
                                _debug(f"  [stream] 429 retry, key swapped")
                                continue
                            if resp.status_code == 499:
                                wait = 1.0 * (2 ** _attempt)
                                _debug(f"  [stream] upstream 499, retrying in {wait:.1f}s (attempt {_attempt+1})")
                                await asyncio.sleep(wait)
                                continue
                            err = await resp.aread()
                            # Pause key on credit/balance errors (400)
                            if resp.status_code == 400 and any(x in err.decode(errors='ignore') for x in ("Insufficient balance", "Monthly usage limit")):
                                failed_key = headers.get("x-api-key", "")
                                _key_pauser.pause_key(failed_key, _key_pauser._max_pause, f"400 credit error")
                                alt = _find_alternative_key(failed_key)
                                if alt:
                                    _log(f"  400 credit error on key, retrying with alternative key")
                                    headers = {"x-api-key": alt.get("api_key", ""), "Content-Type": "application/json",
                                               "anthropic-version": "2023-06-01"}
                                    continue
                            ak = _alias_for_key(headers.get("x-api-key", ""))
                            _debug(f"  [stream] error {resp.status_code}: {_redact(err, 300)}")
                            # Log 401 body specifically for key diagnosis
                            if resp.status_code == 401:
                                _debug(f"  [auth] 401 response body: {_redact(err, 500)}")
                            # Convert 429/401/403 → 503 to avoid Claude Code auth window
                            # (statut normalisé inutile ici : HTTP déjà 200 en streaming,
                            # seul err_msg atteint le client)
                            if resp.status_code == 429:
                                err_msg = "All API keys exhausted (rate limited). Try again later."
                            elif resp.status_code in (401, 403):
                                err_msg = _auth_window_message(resp.status_code)
                            else:
                                err_msg = f"HTTP {resp.status_code}: {err.decode('utf-8', errors='replace')[:200]}"
                            error_payload = {"type": "error", "error": {"type": "api_error",
                                           "message": err_msg}}
                            # DB/console gardent le vrai statut upstream (resp.status_code)
                            yield await _stream_error_response(req_id, _track_model, original_model, start_time,
                                         resp.status_code, err, protocol, thinking_type, effort,
                                         client_ip, ak, tool_names, error_payload,
                                         request_body=request_body)
                            return
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                            _line_buf += chunk.decode("utf-8", errors="replace")
                            if len(_line_buf) > yaml_get("streaming", "line_buffer_max", 1_000_000):
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
                                    stream_cache = usage.get("cache_read_input_tokens", 0)
                                    # Consolidated: single lock acquisition for input + cache update
                                    if stream_in is not None or stream_cache:
                                        try:
                                            with _token_lock:
                                                if stream_in is not None:
                                                    _token_usage[_track_model]["input"] += stream_in - est_input
                                                if stream_cache:
                                                    _token_usage[_track_model]["cache"] += stream_cache
                                        except Exception as e:
                                            _debug(f"  ✗ token rollback failed: {type(e).__name__}: {e}")
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
                                    _token_usage[_track_model]["output"] += stream_out
                            except Exception:
                                pass
                        _cb_record_success(endpoint)  # Stream completed successfully
                except Exception as e:
                    _cb_record_failure(endpoint)  # Record failure for circuit breaker
                    ak = _alias_for_key(headers.get("x-api-key", "")) if headers else ""
                    _debug(f"  [stream] exception on attempt {_attempt+1}: {type(e).__name__}: {e}")
                    _log(f"  ERROR stream (attempt {_attempt+1}): {type(e).__name__}: {e}")
                    if _attempt == 0:
                        # Network errors (server disconnect, timeout) → retry with same key first
                        _is_network_error = isinstance(e, (httpx.RemoteProtocolError, httpx.ReadError,
                                                           ConnectionError, OSError)) or "disconnected" in str(e).lower()
                        if _is_network_error:
                            _debug(f"  ⟳ stream retry (network error, same key)")
                            _log(f"  Retrying stream (network error, same key)")
                            await asyncio.sleep(1.0)
                            continue
                        # Auth/quota errors → try alternative key
                        failed_key = _key_from_headers(headers, "anthropic")
                        if not _key_pauser.is_paused(failed_key):
                            try:
                                await _pause_key_for_quota_reset(failed_key)
                            except Exception:
                                _default_pause = float(yaml_get("key_pause", "default_pause", 60))
                                _key_pauser.pause_key(failed_key, _default_pause, "stream exception")
                        alt = _find_alternative_key(failed_key)
                        if alt:
                            _debug(f"  ⟳ stream retry with alt key: alias={alt.get('alias', '?')}")
                            _log(f"  Retrying stream with alternative key: {alt.get('alias', '?')}")
                            headers = _get_auth_headers("anthropic", entry=alt)
                        continue
                    # Consolidated: single lock acquisition for error-path token adjustments
                    if stream_in is None or stream_out:
                        try:
                            with _token_lock:
                                if stream_in is None:
                                    _token_usage[_track_model]["input"] -= est_input
                                if stream_out:
                                    _token_usage[_track_model]["output"] += stream_out
                        except Exception as e:
                            _debug(f"  ✗ token rollback failed: {type(e).__name__}: {e}")
                    await _save_request(req_id, _track_model, original_model, _elapsed_ms(start_time),
                                 stream_in if stream_in is not None else est_input, stream_out, stream_cache, success=False, error=str(e),
                                 protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                                 client_ip=client_ip, account_alias=ak, tools=tool_names,
                                 tools_used=used_tools if used_tools else None,
                                 request_body=request_body)
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
            if _using_free:
                _log_free_model_usage(model_id, free_model, "free (no auth)", "free (no auth)",
                                      200, logged_in or 0, stream_out or 0,
                                      _elapsed_ms(start_time), ip=_free_usage_ip())
            if stream_in is not None or stream_out:
                ak = _alias_for_key(headers.get("x-api-key", ""))
                await _save_and_log_request(req_id, _track_model, original_model, start_time,
                             logged_in, stream_out, stream_cache, protocol, True, thinking_type,
                             effort, client_ip, ak, tool_names, tools_used=used_tools if used_tools else None,
                             request_body=request_body)

        # For streaming: if free model exists, pass empty headers (free models don't need auth)
        _stream_headers = a_headers if a_headers.get("x-api-key") else {}
        return StreamingResponse(_sse_keepalive(anthropic_stream(_stream_headers)), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})

    # ── OpenAI-protocol ─────────────────────────────────────────
    try:
        oai_body = anthropic_to_openai(body, model_id)
        _debug(f"[messages] converted to openai: {_redact(_truncate(oai_body, 2000))}")
    except Exception as e:
        _debug(f"[messages] ✗ conversion failed: {e}")
        _log(f"  CONVERSION ERROR: anthropic_to_openai failed: {type(e).__name__}: {e}")
        return _anthropic_error(400, f"Request conversion failed: {e}")
    try:
        headers = _get_auth_headers("openai")
    except AllKeysPausedError as e:
        # If a free model exists, try it before giving up
        if FREE_MODEL_MAP.get(model_id):
            try:
                free_result = await _try_free_model_first(oai_body, {}, "openai", model_id)
                if free_result is not None:
                    resp, _, _actual_model, _actual_ip = free_result
                    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                    usage = data.get("usage", {})
                    req_in = usage.get("prompt_tokens", 0)
                    req_out = usage.get("completion_tokens", 0)
                    cache = _extract_cache_tokens(usage)
                    _update_token_usage(_actual_model, req_in, req_out, cache)
                    used = _extract_usage_tool_names(data)
                    await _save_and_log_request(req_id, _actual_model, original_model, start_time,
                                 req_in, req_out, cache, protocol, False, thinking_type,
                                 effort, client_ip, "free (no auth)", tool_names, tools_used=used,
                                 request_body=request_body, response_body=data, free_model_ip=_actual_ip)
                    return Response(content=json.dumps(openai_to_anthropic(data, original_model), ensure_ascii=False),
                                    media_type="application/json")
            except FreeQuotaExhausted as e:
                return _free_quota_exhausted_response(e, "openai")
            except Exception as e:
                _debug(f"  [free] free model attempt failed: {e}")
        retry_after = int(e.retry_after) + 1
        return Response(
            content=json.dumps({"type": "error", "error": {"type": "api_error",
                "message": f"All API keys exhausted. Retry after {retry_after}s."}}),
            status_code=503, media_type="application/json",
            headers={"Retry-After": str(retry_after)})
    is_stream = oai_body["stream"]

    if not is_stream:
        # Try free model first if available
        try:
            free_result = await _try_free_model_first(oai_body, headers, "openai", model_id)
            if free_result is not None:
                resp, headers, _actual_model, _actual_ip = free_result
                model_id = _actual_model
            else:
                resp, headers = await _do_request_with_retry(endpoint, oai_body, headers, "openai")
        except FreeQuotaExhausted as e:
            return _free_quota_exhausted_response(e, "openai")
        except UpstreamError as e:
            _debug(f"  ✗ upstream error: {e}")
            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
        account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
        _debug(f"  response status={resp.status_code} size={len(resp.content)} bytes")
        if resp.status_code != 200:
            await _log_and_save_error(req_id, model_id, original_model, start_time,
                         resp.status_code, resp.text, protocol, is_stream, thinking_type,
                         effort, client_ip, account_alias, tool_names,
                         request_body=request_body, response_body={"error": resp.text[:2000]})
            # Pause key on credit/balance errors (400)
            if resp.status_code == 400 and any(x in resp.text for x in ("Insufficient balance", "Monthly usage limit")):
                failed_key = _key_from_headers(headers, "openai")
                _key_pauser.pause_key(failed_key, _key_pauser._max_pause, f"400 credit error: {resp.text[:80]}")
                alt = _find_alternative_key(failed_key)
                if alt:
                    _log(f"  400 credit error on key, retrying with alternative key")
                    headers = _get_auth_headers("openai", entry=alt)
                    # Retry once with alt key
                    try:
                        resp, headers = await _do_request_with_retry(endpoint, oai_body, headers, "openai")
                        if resp.status_code == 200:
                            account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
                        else:
                            return _anthropic_error(503, "All API keys exhausted. Check your billing.")
                    except UpstreamError as e:
                        return _anthropic_error(e.status_code, str(e))
            # Convert 429/401/403 → 503 to avoid Claude Code auth window
            if resp.status_code in (429, 401, 403):
                return _anthropic_error(503, _auth_window_message(resp.status_code))
            if resp.status_code == 499:
                return _anthropic_error(502, "Upstream disconnected (499). Retrying may help.")
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
        used = _extract_usage_tool_names(data)
        msg_data = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
        if not (msg_data.get("reasoning_content") or msg_data.get("reasoning")) and thinking_type != "none":
            _debug(f"  [non-stream] WARNING: thinking requested (type={thinking_type}, effort={effort}) but upstream returned no reasoning_content")
        _debug(f"  [non-stream] blocks: text={bool(msg_data.get('content'))} thinking={bool(msg_data.get('reasoning_content') or msg_data.get('reasoning'))} tools={used}")
        await _save_and_log_request(req_id, model_id, original_model, start_time,
                     req_in, req_out, cache, protocol, is_stream=False, thinking_type=thinking_type,
                     effort=effort, client_ip=client_ip, account_alias=account_alias, tools=tool_names,
                     tools_used=used if used else None,
                     request_body=request_body, response_body=data)
        return Response(content=json.dumps(openai_to_anthropic(data, original_model), ensure_ascii=False),
                        media_type="application/json")

    # Streaming
    msg_id = _fast_id("msg")
    oai_body["stream_options"] = {"include_usage": True}

    async def stream_gen(hdrs):
        nonlocal endpoint, oai_body, model_id
        # Use local variable to avoid sharing model_id across concurrent requests
        _req_model_id = model_id
        # Try free model for streaming
        _paid_model_id = _req_model_id  # Save original for fallback
        free_model = FREE_MODEL_MAP.get(_req_model_id)
        if free_model:
            _debug(f"  [stream-oai] attempting free model {free_model!r} first")
            paid_endpoint = endpoint
            paid_oai_body = dict(oai_body)
            oai_body = dict(oai_body)
            oai_body["model"] = free_model
            endpoint = API_BASE_FREE
            _req_model_id = free_model  # Log as free model
            _using_free = True
        else:
            _using_free = False
        stream_in_est = await asyncio.to_thread(_estimate_input_tokens, body)
        _debug(f"  [stream-oai] est_input={stream_in_est}")
        _update_token_usage(_req_model_id, stream_in_est, 0, 0)
        started = False
        open_blocks = []
        text_block_idx = None
        reasoning_block_idx = None
        next_block_idx = 0
        stream_out_tokens = 0
        actual_usage = None
        _handle_429 = _make_stream_retry_loop("openai")

        for _attempt in range(yaml_get("streaming", "retry_attempts", 2)):
            used_tools = []  # Reset on each retry attempt
            tool_block_idx = {}  # Reset tracking dict on each retry
            try:
                async with _open_free_stream(endpoint, oai_body, hdrs, _using_free,
                                             count_request=(_attempt == 0),
                                             fresh_station=(_attempt > 0 and _using_free)) as resp:
                    if resp.status_code != 200:
                        if _using_free:
                            # Free endpoint non-200 (429 quota, 5xx...) → cooldown
                            # the free model, fall back to paid. Never pauses PAID
                            # keys: a status from the free endpoint says nothing
                            # about the paid account (CRITIC(2)/CRITIC(3)).
                            if resp.status_code == 429:
                                _refuse = _on_free_429_stream(free_model, resp.headers.get('retry-after', ''))
                            else:
                                _set_free_cooldown(free_model, 60, _free_attempt_station())
                                _refuse = False
                            _debug(f"  [stream-oai] free model {resp.status_code} → falling back to paid {_paid_model_id!r}")
                            _log(f"  FREE model {resp.status_code} → falling back to paid {_paid_model_id!r}")
                            _log_free_model_usage(_paid_model_id, free_model, "free (no auth)", "free (no auth)",
                                                  resp.status_code, ip=_free_usage_ip())
                            oai_body = paid_oai_body
                            endpoint = paid_endpoint
                            model_id = _paid_model_id
                            _req_model_id = _paid_model_id  # Also revert logging model (fixes is_free_model in history)
                            _using_free = False
                            if _refuse:
                                # strict_free (GUI): every station exhausted
                                # (bad/down + (model, IP) cooldown active) —
                                # refuse instead of paying.
                                _retry_after = resp.headers.get('retry-after', '') or "60"
                                yield await _stream_error_response(
                                    req_id, free_model, original_model, start_time,
                                    429, await resp.aread(), protocol, thinking_type,
                                    effort, client_ip, "free (no auth)", tool_names,
                                    {"type": "error",
                                     "error": {"type": "rate_limit_error",
                                               "message": f"Free quota exhausted on all VPN stations. Retry after {_retry_after}s."}},
                                    request_body=request_body)
                                return
                            continue
                        hdrs, should_retry = await _handle_429(hdrs, resp.status_code, _attempt, resp.headers)
                        if should_retry:
                            continue
                        if resp.status_code == 499:
                            wait = 1.0 * (2 ** _attempt)
                            _debug(f"  [stream-oai] upstream 499, retrying in {wait:.1f}s (attempt {_attempt+1})")
                            await asyncio.sleep(wait)
                            continue
                        err = await resp.aread()
                        # Pause key on credit/balance errors (400)
                        if resp.status_code == 400 and any(x in err.decode(errors='ignore') for x in ("Insufficient balance", "Monthly usage limit")):
                            failed_key = _key_from_headers(hdrs, "openai")
                            _key_pauser.pause_key(failed_key, _key_pauser._max_pause, f"400 credit error")
                            alt = _find_alternative_key(failed_key)
                            if alt:
                                _log(f"  400 credit error on key, retrying with alternative key")
                                hdrs = _get_auth_headers("openai", entry=alt)
                                continue
                        ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))

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
                            if _using_free:
                                _log_free_model_usage(_paid_model_id, free_model, "free (no auth)", "free (no auth)",
                                                      200, final_in or 0, final_out or 0,
                                                      _elapsed_ms(start_time), ip=_free_usage_ip())
                            await _save_and_log_request(req_id, _req_model_id, original_model, start_time,
                                         final_in, final_out, final_cache, protocol, True, thinking_type,
                                         effort, client_ip, ak_h, tool_names, log_tag,
                                         tools_used=used_tools if used_tools else None,
                                         request_body=request_body)
                            break

                        try:
                            chunk = json.loads(data)
                        except Exception:
                            continue
                        if chunk is None:
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
                _debug(f"  ✗ stream exception: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                if _attempt == 0:
                    # Try alternative key on retry (handles rate-limit disguised as disconnect)
                    failed_key = _key_from_headers(hdrs, "openai")
                    if not _key_pauser.is_paused(failed_key):
                        try:
                            await _pause_key_for_quota_reset(failed_key)
                        except Exception:
                            _default_pause = float(yaml_get("key_pause", "default_pause", 60))
                            _key_pauser.pause_key(failed_key, _default_pause, "stream exception")
                    alt = _find_alternative_key(failed_key)
                    if alt:
                        _debug(f"  ⟳ stream retry with alt key: alias={alt.get('alias', '?')}")
                        _log(f"  Retrying stream with alternative key: {alt.get('alias', '?')}")
                        hdrs = _get_auth_headers("openai", entry=alt)
                    continue
                try:
                    with _token_lock:
                        _token_usage[_req_model_id]["input"] -= stream_in_est
                except Exception:
                    pass
                ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                await _save_request(req_id, _req_model_id, original_model, _elapsed_ms(start_time),
                             stream_in_est, stream_out_tokens, 0, success=False, error=str(e),
                             protocol=protocol, is_stream=True, thinking=thinking_type, effort=effort,
                             client_ip=client_ip, account_alias=ak_h, tools=tool_names,
                             tools_used=used_tools if used_tools else None,
                             request_body=request_body)
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
_health_lock = asyncio.Lock()

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
    async with _health_lock:
        if _health_cache and (now - _health_cache[0]) < yaml_get("background", "health_cache_ttl", 15):
            upstream_ok = _health_cache[1]
            _debug(f"  [health] upstream check (cached): {'ok' if upstream_ok else 'unreachable'}")
        else:
            # Cache miss — perform actual check
            try:
                resp = await _ensure_http_client().get("https://opencode.ai", timeout=float(yaml_get("background", "health_timeout", 5)))
                upstream_ok = resp.status_code < 500
            except Exception:
                upstream_ok = False
            _health_cache = (now, upstream_ok)
            _debug(f"  [health] upstream check (fresh): {'ok' if upstream_ok else 'unreachable'}")

    status = "ok"
    if not upstream_ok or any_open:
        status = "degraded"

    # Check key pause status
    key_pause_status = _key_pauser.get_all_status()
    any_paused = bool(key_pause_status)
    if any_paused:
        status = "degraded"
    _debug(f"  [health] key pauses: {len(key_pause_status)} active, status={status}")

    _debug(f"  [health] overall status={status}")
    return {"status": status, "usage": usage, "upstream": "ok" if upstream_ok else "unreachable",
            "circuit_breakers": cb_status, "key_pauses": key_pause_status}


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


@app.get("/api/key-pauses")
async def key_pauses():
    """Return status of all paused API keys."""
    return _key_pauser.get_all_status()


@app.post("/api/key-pauses/reset")
async def key_pauses_reset():
    """Unpause all paused keys immediately."""
    _key_pauser.cleanup_expired()
    with _key_pauser._lock:
        count = len(_key_pauser._paused)
        _key_pauser._paused.clear()
        _key_pauser._reasons.clear()
    _log(f"  KEY PAUSES RESET: {count} pauses cleared")
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
    tokens = await asyncio.to_thread(_estimate_input_tokens, body)
    return {"input_tokens": tokens}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    req_id = _fast_id("chatcmpl")
    start_time = time.monotonic()
    client_ip = _get_client_ip(request)
    _current_user_agent.set(request.headers.get("user-agent"))

    body_bytes = await request.body()
    _debug(f"  [body] read {len(body_bytes)} bytes in {(time.monotonic() - start_time) * 1000:.0f}ms")
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
    request_body = body  # Capture original request before mutation
    _debug(f"[chat] req_id={req_id} model={original_model!r} tools={tool_names} ip={client_ip}")
    if DEBUG:
        _debug(f"[chat] headers={_sanitize_headers(dict(request.headers))}")
        _debug(f"[chat] body=\n{_redact(_truncate(body))}")
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

    # Filter tools based on model capabilities
    tool_names = _filter_tools_for_model(body, model_id)

    # Handle web_search tool (DuckDuckGo or model routing)
    await _handle_web_search(body, model_id, protocol)

    _log(f"→ {original_model!r} → {model_id} | {protocol} | chat/completions | stream={is_stream} | thinking={thinking_type} | effort={effort} | ip={client_ip}")

    # Circuit breaker check
    if not _cb_should_allow(endpoint):
        _log(f"  CIRCUIT BREAKER OPEN — fast-failing request to {endpoint}")
        return _openai_error(503, "Service temporarily unavailable (circuit breaker open)")

    # ── OpenAI passthrough ─────────────────────────────────────
    if protocol == "openai":
        try:
            headers = _get_auth_headers("openai")
        except AllKeysPausedError as e:
            # If a free model exists, try it before giving up
            if FREE_MODEL_MAP.get(model_id):
                if is_stream:
                    # Streaming: pass empty headers — generator handles free model swap
                    return StreamingResponse(_sse_keepalive(openai_stream({})), media_type="text/event-stream",
                                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
                else:
                    # Non-streaming: try free model
                    try:
                        free_result = await _try_free_model_first(body, {}, "openai", model_id)
                        if free_result is not None:
                            resp, _, _actual_model, _actual_ip = free_result
                            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                            usage = data.get("usage", {})
                            req_in = usage.get("prompt_tokens", 0)
                            req_out = usage.get("completion_tokens", 0)
                            cache = _extract_cache_tokens(usage)
                            _update_token_usage(_actual_model, req_in, req_out, cache)
                            used = _extract_usage_tool_names(data)
                            await _save_and_log_request(req_id, _actual_model, original_model, start_time,
                                         req_in, req_out, cache, protocol, False, thinking_type,
                                         effort, client_ip, "free (no auth)", tool_names, tools_used=used,
                                         request_body=request_body, response_body=data, free_model_ip=_actual_ip)
                            return Response(content=json.dumps(data, ensure_ascii=False), media_type="application/json")
                    except FreeQuotaExhausted as e:
                        return _free_quota_exhausted_response(e, "openai")
                    except Exception as e:
                        _debug(f"  [free] free model attempt failed: {e}")
            retry_after = int(e.retry_after) + 1
            return Response(
                content=json.dumps({"error": {"message": f"All API keys exhausted. Retry after {retry_after}s.",
                    "type": "api_error", "code": "503"}}),
                status_code=503, media_type="application/json",
                headers={"Retry-After": str(retry_after)})

        if not is_stream:
            # Response cache (mirrors the anthropic non-stream handler) [8]
            cache_key = _response_cache.make_key(body, body_bytes=body_bytes)
            cached = cache_key and _response_cache.get(cache_key)
            if cached:
                cached_body, cached_headers = cached
                _debug(f"  [chat] response cache HIT ({len(cached_body)} bytes)")
                return Response(content=cached_body, headers={**cached_headers, "X-Cache": "HIT"},
                                media_type="application/json")
            # Try free model first if available
            try:
                free_result = await _try_free_model_first(body, headers, "openai", model_id)
                if free_result is not None:
                    resp, headers, _actual_model, _actual_ip = free_result
                    model_id = _actual_model
                else:
                    resp, headers = await _do_request_with_retry(endpoint, body, headers, "openai")
            except FreeQuotaExhausted as e:
                return _free_quota_exhausted_response(e, "openai")
            except UpstreamError as e:
                return JSONResponse(status_code=e.status_code, content={"error": str(e)})
            account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
            if resp.status_code != 200:
                await _log_and_save_error(req_id, model_id, original_model, start_time,
                             resp.status_code, resp.text, protocol, is_stream, thinking_type,
                             effort, client_ip, account_alias, tool_names,
                             request_body=request_body, response_body={"error": resp.text[:2000]})
                # Pause key on credit/balance errors (400) and retry with alt key
                if resp.status_code == 400 and any(x in resp.text for x in ("Insufficient balance", "Monthly usage limit")):
                    failed_key = _key_from_headers(headers, "openai")
                    _key_pauser.pause_key(failed_key, _key_pauser._max_pause, f"400 credit error: {_redact(resp.text, 80)}")
                    alt = _find_alternative_key(failed_key)
                    if alt:
                        _log(f"  400 credit error on key, retrying with alternative key")
                        headers = _get_auth_headers("openai", entry=alt)
                        try:
                            resp, headers = await _do_request_with_retry(endpoint, body, headers, "openai")
                            if resp.status_code == 200:
                                account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
                            else:
                                return _openai_error(503, "All API keys exhausted. Check your billing.")
                        except UpstreamError as e:
                            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
                # Convert 429/401/403 → 503 to avoid Claude Code auth window
                if resp.status_code in (429, 401, 403):
                    return _openai_error(503, _auth_window_message(resp.status_code))
                if resp.status_code == 499:
                    return _openai_error(502, "Upstream disconnected (499). Retrying may help.")
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
            used = _extract_usage_tool_names(data)
            await _save_and_log_request(req_id, model_id, original_model, start_time,
                         req_in, req_out, cache, protocol, is_stream, thinking_type,
                         effort, client_ip, account_alias, tool_names, tools_used=used if used else None,
                         request_body=request_body, response_body=data)
            _response_cache.put(cache_key, resp.content, {"Content-Type": "application/json"})
            return Response(content=resp.content, headers={"X-Cache": "MISS"}, media_type="application/json")

        # ── OpenAI streaming passthrough ──
        oai_body = dict(body)
        oai_body["stream_options"] = {"include_usage": True}

        async def openai_stream(hdrs):
            nonlocal endpoint, oai_body, model_id
            # _track_model is the model used for logging/token tracking.
            # We use a local variable instead of reassigning model_id to avoid
            # UnboundLocalError in Python 3.12+ (nonlocal + assignment conflict).
            _track_model = model_id
            # Try free model for streaming: swap endpoint/model before starting stream
            free_model = FREE_MODEL_MAP.get(model_id)
            if free_model:
                _debug(f"  [chat-stream] attempting free model {free_model!r} first")
                # Save paid config
                paid_endpoint = endpoint
                paid_oai_body = dict(oai_body)
                # Swap to free
                oai_body = dict(oai_body)
                oai_body["model"] = free_model
                endpoint = API_BASE_FREE
                _using_free = True
                _track_model = free_model  # Log as free model
            else:
                _using_free = False
            est_input = await asyncio.to_thread(_estimate_input_tokens, body)
            _update_token_usage(_track_model, est_input, 0, 0)
            _debug(f"  [chat-stream] est_input={est_input}")
            stream_out = 0
            actual_usage = None
            _handle_429 = _make_stream_retry_loop("openai")
            for _attempt in range(yaml_get("streaming", "retry_attempts", 2)):
                used_tools = []
                seen_tool_indices = set()  # dedup tool calls by index
                try:
                    async with _open_free_stream(endpoint, oai_body, hdrs, _using_free,
                                             count_request=(_attempt == 0),
                                             fresh_station=(_attempt > 0 and _using_free)) as resp:
                        if resp.status_code != 200:
                            if _using_free:
                                # Free endpoint non-200 (429 quota, 5xx...) → cooldown
                                # the free model, fall back to paid. Never pauses PAID
                                # keys: a status from the free endpoint says nothing
                                # about the paid account (CRITIC(2)/CRITIC(3)).
                                if resp.status_code == 429:
                                    _refuse = _on_free_429_stream(free_model, resp.headers.get('retry-after', ''))
                                else:
                                    _set_free_cooldown(free_model, 60, _free_attempt_station())
                                    _refuse = False
                                oai_body = paid_oai_body
                                endpoint = paid_endpoint
                                _using_free = False
                                _track_model = model_id  # Revert tracking to paid model
                                _debug(f"  [chat-stream] free model {resp.status_code} → falling back to paid {_track_model!r}")
                                _log(f"  FREE model {resp.status_code} → falling back to paid {_track_model!r}")
                                _log_free_model_usage(model_id, free_model, "free (no auth)", "free (no auth)",
                                                      resp.status_code, ip=_free_usage_ip())
                                if _refuse:
                                    # strict_free (GUI): every station exhausted
                                    # (bad/down + (model, IP) cooldown active) —
                                    # refuse instead of paying.
                                    _retry_after = resp.headers.get('retry-after', '') or "60"
                                    yield await _stream_error_response(
                                        req_id, free_model, original_model, start_time,
                                        429, await resp.aread(), protocol, thinking_type,
                                        effort, client_ip, "free (no auth)", tool_names,
                                        {"type": "error",
                                         "error": {"type": "rate_limit_error",
                                                   "message": f"Free quota exhausted on all VPN stations. Retry after {_retry_after}s."}},
                                        request_body=request_body)
                                    return
                                continue
                            hdrs, should_retry = await _handle_429(hdrs, resp.status_code, _attempt, resp.headers)
                            if should_retry:
                                continue
                            if resp.status_code == 499:
                                wait = 1.0 * (2 ** _attempt)
                                _debug(f"  [chat-stream] upstream 499, retrying in {wait:.1f}s (attempt {_attempt+1})")
                                await asyncio.sleep(wait)
                                continue
                            err = await resp.aread()
                            # Pause key on credit/balance errors (400)
                            if resp.status_code == 400 and any(x in err.decode(errors='ignore') for x in ("Insufficient balance", "Monthly usage limit")):
                                failed_key = _key_from_headers(hdrs, "openai")
                                _key_pauser.pause_key(failed_key, _key_pauser._max_pause, f"400 credit error")
                                alt = _find_alternative_key(failed_key)
                                if alt:
                                    _log(f"  400 credit error on key, retrying with alternative key")
                                    hdrs = _get_auth_headers("openai", entry=alt)
                                    continue
                            ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                            # Log 401 body specifically for key diagnosis
                            if resp.status_code == 401:
                                _debug(f"  [auth] 401 response body: {_redact(err, 500)}")
                            # Convert 429/401/403 → 503 to avoid Claude Code auth window
                            err_status = 503 if resp.status_code in (429, 401, 403) else resp.status_code
                            if resp.status_code == 429:
                                err_msg = "All API keys exhausted (rate limited). Try again later."
                            elif resp.status_code in (401, 403):
                                err_msg = _auth_window_message(resp.status_code)
                            else:
                                err_msg = f"HTTP {resp.status_code}"
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
                            if chunk is None:
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
                                    for tc in (delta.get("tool_calls") or []):
                                        if isinstance(tc, dict) and "name" in tc.get("function", {}):
                                            tc_idx = tc.get("index", len(seen_tool_indices))
                                            if tc_idx not in seen_tool_indices:
                                                seen_tool_indices.add(tc_idx)
                                                used_tools.append(tc["function"]["name"])
                            yield line.encode() + b"\n\n"

                        # Stream ended — finalize tracking
                        final_in, final_out, final_cache, log_tag = _finalize_stream_tokens(
                            _track_model, est_input, None, stream_out, 0,
                            actual_usage, _token_usage, _token_lock)
                        ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                        if _using_free:
                            _log_free_model_usage(model_id, free_model, "free (no auth)", "free (no auth)",
                                                  200, final_in or 0, final_out or 0,
                                                  _elapsed_ms(start_time), ip=_free_usage_ip())
                        await _save_and_log_request(req_id, _track_model, original_model, start_time,
                                     final_in, final_out, final_cache, protocol, True, thinking_type,
                                     effort, client_ip, ak_h, tool_names, log_tag,
                                     tools_used=used_tools if used_tools else None,
                                     request_body=request_body)
                        _cb_record_success(endpoint)  # Stream completed successfully
                except Exception as e:
                    _cb_record_failure(endpoint)  # Record failure for circuit breaker
                    _log(f"  ERROR stream (attempt {_attempt+1}): {type(e).__name__}: {e}")
                    _debug(f"  ✗ stream exception: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                    if _attempt == 0:
                        # Network errors (server disconnect, timeout) → retry with same key first
                        _is_network_error = isinstance(e, (httpx.RemoteProtocolError, httpx.ReadError,
                                                           ConnectionError, OSError)) or "disconnected" in str(e).lower()
                        if _is_network_error:
                            _debug(f"  ⟳ stream retry (network error, same key)")
                            _log(f"  Retrying stream (network error, same key)")
                            await asyncio.sleep(1.0)
                            continue
                        # Auth/quota errors → try alternative key
                        failed_key = _key_from_headers(hdrs, "openai")
                        if not _key_pauser.is_paused(failed_key):
                            try:
                                await _pause_key_for_quota_reset(failed_key)
                            except Exception:
                                _default_pause = float(yaml_get("key_pause", "default_pause", 60))
                                _key_pauser.pause_key(failed_key, _default_pause, "stream exception")
                        alt = _find_alternative_key(failed_key)
                        if alt:
                            _debug(f"  ⟳ stream retry with alt key: alias={alt.get('alias', '?')}")
                            _log(f"  Retrying stream with alternative key: {alt.get('alias', '?')}")
                            hdrs = _get_auth_headers("openai", entry=alt)
                        continue
                    try:
                        with _token_lock:
                            _token_usage[_track_model]["input"] -= est_input
                    except Exception as e:
                        _debug(f"  ✗ token rollback failed: {type(e).__name__}: {e}")
                    ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                    await _log_and_save_error(req_id, _track_model, original_model, start_time,
                                 0, str(e), protocol, True, thinking_type,
                                 effort, client_ip, ak_h, tool_names,
                                 request_body=request_body)
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
    try:
        a_headers = _get_auth_headers("anthropic")
    except AllKeysPausedError as e:
        # If a free model exists, try it before giving up
        if FREE_MODEL_MAP.get(model_id):
            if is_stream:
                # Streaming: pass empty headers — generator handles free model swap
                return StreamingResponse(_sse_keepalive(_anthro_to_oai_stream({})), media_type="text/event-stream",
                                         headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
            else:
                # Non-streaming: try free model
                try:
                    free_result = await _try_free_model_first(anthro_body, {}, "anthropic", model_id)
                    if free_result is not None:
                        resp, _, _actual_model, _actual_ip = free_result
                        data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                        usage = data.get("usage", {})
                        req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                        req_out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                        req_cache = usage.get("cache_read_input_tokens", 0)
                        _update_token_usage(_actual_model, req_in, req_out, req_cache)
                        used = [b["name"] for b in data.get("content", []) if b.get("type") == "tool_use"]
                        await _save_and_log_request(req_id, _actual_model, original_model, start_time,
                                     req_in, req_out, req_cache, protocol, False, thinking_type,
                                     effort, client_ip, "free (no auth)", tool_names, tools_used=used,
                                     request_body=request_body, response_body=data, free_model_ip=_actual_ip)
                        oai_response = anthropic_to_openai_response(data, original_model)
                        return Response(content=json.dumps(oai_response, ensure_ascii=False), media_type="application/json")
                except FreeQuotaExhausted as e:
                    return _free_quota_exhausted_response(e, "anthropic")
                except Exception as e:
                    _debug(f"  [free] free model attempt failed: {e}")
        retry_after = int(e.retry_after) + 1
        return Response(
            content=json.dumps({"error": {"message": f"All API keys exhausted. Retry after {retry_after}s.",
                "type": "api_error"}}),
            status_code=503, media_type="application/json",
            headers={"Retry-After": str(retry_after)})

    if not is_stream:
        # Try free model first if available
        try:
            free_result = await _try_free_model_first(anthro_body, a_headers, "anthropic", model_id)
            if free_result is not None:
                resp, a_headers, _actual_model, _actual_ip = free_result
                model_id = _actual_model
            else:
                resp, a_headers = await _do_request_with_retry(endpoint, anthro_body, a_headers, "anthropic")
        except FreeQuotaExhausted as e:
            return _free_quota_exhausted_response(e, "anthropic")
        except UpstreamError as e:
            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
        account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
        if resp.status_code != 200:
            await _log_and_save_error(req_id, model_id, original_model, start_time,
                         resp.status_code, resp.text, protocol, is_stream, thinking_type,
                         effort, client_ip, account_alias, tool_names,
                         request_body=request_body, response_body={"error": resp.text[:2000]})
            # Convert 429/401/403 → 503 to avoid Claude Code auth window
            if resp.status_code in (429, 401, 403):
                return _openai_error(503, _auth_window_message(resp.status_code))
            if resp.status_code == 499:
                return _openai_error(502, "Upstream disconnected (499). Retrying may help.")
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
                     effort, client_ip, account_alias, tool_names, tools_used=used if used else None,
                     request_body=request_body, response_body=data)

        oai_response = anthropic_to_openai_response(data, original_model)
        return Response(content=json.dumps(oai_response, ensure_ascii=False), media_type="application/json")

    # ── Streaming with Anthropic backend (true streaming) ──
    async def _anthro_to_oai_stream(hdrs):
        nonlocal endpoint, model_id
        # Try free model for streaming: swap endpoint/model before starting stream
        free_model = FREE_MODEL_MAP.get(model_id)
        if free_model:
            _debug(f"  [anthro-to-oai-stream] attempting free model {free_model!r} first")
            paid_endpoint = endpoint
            paid_anthro_body = dict(anthro_body)
            paid_anthro_body["model"] = model_id
            anthro_body = dict(anthro_body)
            anthro_body["model"] = free_model
            endpoint = API_BASE_FREE
            _using_free = True
            _track_model = free_model
        else:
            _using_free = False
            _track_model = model_id
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

        for _attempt in range(yaml_get("streaming", "retry_attempts", 2)):
            try:
                async with _open_free_stream(endpoint, anthro_body, hdrs, _using_free,
                                             count_request=(_attempt == 0),
                                             fresh_station=(_attempt > 0 and _using_free)) as resp:
                    if resp.status_code != 200:
                        if _using_free:
                            # Free endpoint non-200 (429 quota, 5xx...) → cooldown
                            # the free model, fall back to paid. Never pauses PAID
                            # keys: a status from the free endpoint says nothing
                            # about the paid account (CRITIC(2)/CRITIC(3)).
                            if resp.status_code == 429:
                                _refuse = _on_free_429_stream(free_model, resp.headers.get('retry-after', ''))
                            else:
                                _set_free_cooldown(free_model, 60, _free_attempt_station())
                                _refuse = False
                            anthro_body = paid_anthro_body
                            endpoint = paid_endpoint
                            _using_free = False
                            _track_model = model_id
                            _debug(f"  [anthro-to-oai-stream] free model {resp.status_code} → falling back to paid {_track_model!r}")
                            _log(f"  FREE model {resp.status_code} → falling back to paid {_track_model!r}")
                            _log_free_model_usage(model_id, free_model, "free (no auth)", "free (no auth)",
                                                  resp.status_code, ip=_free_usage_ip())
                            if _refuse:
                                # strict_free (GUI): every station exhausted
                                # (bad/down + (model, IP) cooldown active) —
                                # refuse instead of paying.
                                _retry_after = resp.headers.get('retry-after', '') or "60"
                                yield await _stream_error_response(
                                    req_id, free_model, original_model, start_time,
                                    429, await resp.aread(), protocol, thinking_type,
                                    effort, client_ip, "free (no auth)", tool_names,
                                    {"type": "error",
                                     "error": {"type": "rate_limit_error",
                                               "message": f"Free quota exhausted on all VPN stations. Retry after {_retry_after}s."}},
                                    request_body=request_body)
                                return
                            continue
                        hdrs, should_retry = await _handle_429(hdrs, resp.status_code, _attempt, resp.headers)
                        if should_retry:
                            continue
                        if resp.status_code == 499:
                            wait = 1.0 * (2 ** _attempt)
                            _debug(f"  [anthro-to-oai-stream] upstream 499, retrying in {wait:.1f}s (attempt {_attempt+1})")
                            await asyncio.sleep(wait)
                            continue
                        err = await resp.aread()
                        # Pause key on credit/balance errors (400)
                        if resp.status_code == 400 and any(x in err.decode(errors='ignore') for x in ("Insufficient balance", "Monthly usage limit")):
                            failed_key = hdrs.get("x-api-key", "")
                            _key_pauser.pause_key(failed_key, _key_pauser._max_pause, f"400 credit error")
                            alt = _find_alternative_key(failed_key)
                            if alt:
                                _log(f"  400 credit error on key, retrying with alternative key")
                                hdrs = {"x-api-key": alt.get("api_key", ""), "Content-Type": "application/json",
                                       "anthropic-version": "2023-06-01"}
                                continue
                        ak = _alias_for_key(hdrs.get("x-api-key", ""))
                        # Log 401 body specifically for key diagnosis
                        if resp.status_code == 401:
                            _debug(f"  [auth] 401 response body: {_redact(err, 500)}")
                        await _log_and_save_error(req_id, model_id, original_model, start_time,
                                     resp.status_code, str(resp.status_code), protocol, True, thinking_type,
                                     effort, client_ip, ak, tool_names,
                                     request_body=request_body)
                        # Convert 429/401/403 → 503 to avoid Claude Code auth window
                        err_status = 503 if resp.status_code in (429, 401, 403) else resp.status_code
                        if resp.status_code == 429:
                            err_msg = "All API keys exhausted (rate limited). Try again later."
                        elif resp.status_code in (401, 403):
                            err_msg = _auth_window_message(resp.status_code)
                        else:
                            err_msg = f"HTTP {resp.status_code}"
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
                                _update_token_usage(_track_model, total_input, stream_out, cache_read)
                                ak = _alias_for_key(hdrs.get("x-api-key", ""))
                                if _using_free:
                                    _log_free_model_usage(model_id, free_model, "free (no auth)", "free (no auth)",
                                                          200, total_input or 0, stream_out or 0,
                                                          _elapsed_ms(start_time), ip=_free_usage_ip())
                                used_tools = [v["name"] for v in tool_data.values() if v.get("name")]
                                await _save_and_log_request(req_id, _track_model, original_model, start_time,
                                             total_input, stream_out, cache_read, protocol, True, thinking_type,
                                             effort, client_ip, ak, tool_names, tools_used=used_tools if used_tools else None,
                                             request_body=request_body)
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
                                _cb_record_success(endpoint)  # Stream completed successfully
                                return
            except Exception as e:
                _cb_record_failure(endpoint)  # Record failure for circuit breaker
                _log(f"  ERROR stream (attempt {_attempt+1}): {type(e).__name__}: {e}")
                _debug(f"  ✗ stream exception: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                if _attempt == 0:
                    # Network errors (server disconnect, timeout) → retry with same key first
                    _is_network_error = isinstance(e, (httpx.RemoteProtocolError, httpx.ReadError,
                                                       ConnectionError, OSError)) or "disconnected" in str(e).lower()
                    if _is_network_error:
                        _debug(f"  ⟳ stream retry (network error, same key)")
                        _log(f"  Retrying stream (network error, same key)")
                        await asyncio.sleep(1.0)
                        continue
                    # Auth/quota errors → try alternative key
                    failed_key = _key_from_headers(hdrs, "anthropic")
                    if not _key_pauser.is_paused(failed_key):
                        try:
                            await _pause_key_for_quota_reset(failed_key)
                        except Exception:
                            _default_pause = float(yaml_get("key_pause", "default_pause", 60))
                            _key_pauser.pause_key(failed_key, _default_pause, "stream exception")
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
                             client_ip=client_ip, account_alias=ak, tools=tool_names,
                             request_body=request_body)
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
    start_time = time.monotonic()
    client_ip = _get_client_ip(request)
    _current_user_agent.set(request.headers.get("user-agent"))

    body_bytes = await request.body()
    _debug(f"  [body] read {len(body_bytes)} bytes in {(time.monotonic() - start_time) * 1000:.0f}ms")
    if len(body_bytes) > MAX_BODY_SIZE:
        _debug(f"  413: body too large ({len(body_bytes)} bytes)")
        return _openai_error(413, f"Request body too large ({len(body_bytes)} bytes, max {MAX_BODY_SIZE})")

    try:
        body = json.loads(body_bytes)
    except Exception:
        _debug(f"  400: invalid JSON body")
        return _openai_error(400, "invalid json")

    body = ensure_min_tokens(body)

    original_model = body.get("model", "")
    tool_names = _extract_tool_names(body)
    request_body = body  # Capture original request before mutation
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

    # Filter tools based on model capabilities
    tool_names = _filter_tools_for_model(anthro_body, model_id)

    # Handle web_search tool (DuckDuckGo or model routing)
    await _handle_web_search(anthro_body, model_id, "anthropic")

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
        try:
            a_headers = _get_auth_headers("anthropic")
        except AllKeysPausedError as e:
            # If a free model exists, try it before giving up
            if FREE_MODEL_MAP.get(model_id):
                if is_stream:
                    # Streaming: pass empty headers — generator handles free model swap
                    return StreamingResponse(_sse_keepalive(_anthro_to_oai_stream({})), media_type="text/event-stream",
                                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
                else:
                    # Non-streaming: try free model
                    try:
                        free_result = await _try_free_model_first(anthro_body, {}, "anthropic", model_id)
                        if free_result is not None:
                            resp, _, _actual_model, _actual_ip = free_result
                            data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                            usage = data.get("usage", {})
                            req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                            req_out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                            req_cache = usage.get("cache_read_input_tokens", 0)
                            _update_token_usage(_actual_model, req_in, req_out, req_cache)
                            used = [b["name"] for b in data.get("content", []) if b.get("type") == "tool_use"]
                            await _save_and_log_request(req_id, _actual_model, original_model, start_time,
                                         req_in, req_out, req_cache, protocol, False, thinking_type,
                                         effort, client_ip, "free (no auth)", tool_names, tools_used=used,
                                         request_body=request_body, response_body=data, free_model_ip=_actual_ip)
                            oai_resp = anthropic_to_openai_responses(data, original_model)
                            payload = json.dumps({"type": "response.completed", "response": oai_resp}, ensure_ascii=False)
                            sse_body = f"data: {payload}\n\ndata: [DONE]\n\n".encode()
                            return Response(content=sse_body, media_type="text/event-stream",
                                            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
                    except FreeQuotaExhausted as e:
                        return _free_quota_exhausted_response(e, "anthropic")
                    except Exception as e:
                        _debug(f"  [free] free model attempt failed: {e}")
            retry_after = int(e.retry_after) + 1
            return Response(
                content=json.dumps({"error": {"message": f"All API keys exhausted. Retry after {retry_after}s.",
                    "type": "api_error"}}),
                status_code=503, media_type="application/json",
                headers={"Retry-After": str(retry_after)})
        if not is_stream:
            # Try free model first if available
            try:
                free_result = await _try_free_model_first(anthro_body, a_headers, "anthropic", model_id)
                if free_result is not None:
                    resp, a_headers, _actual_model, _actual_ip = free_result
                    model_id = _actual_model
                else:
                    resp, a_headers = await _do_request_with_retry(endpoint, anthro_body, a_headers, "anthropic")
            except FreeQuotaExhausted as e:
                return _free_quota_exhausted_response(e, "anthropic")
            except UpstreamError as e:
                return JSONResponse(status_code=e.status_code, content={"error": str(e)})
            account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
            if resp.status_code != 200:
                await _log_and_save_error(req_id, model_id, original_model, start_time,
                             resp.status_code, resp.text, protocol, is_stream, thinking_type,
                             effort, client_ip, account_alias, tool_names,
                             request_body=request_body, response_body={"error": resp.text[:2000]})
                # Pause key on credit/balance errors (400) and retry with alt key
                if resp.status_code == 400 and any(x in resp.text for x in ("Insufficient balance", "Monthly usage limit")):
                    failed_key = a_headers.get("x-api-key", "")
                    _key_pauser.pause_key(failed_key, _key_pauser._max_pause, f"400 credit error: {_redact(resp.text, 80)}")
                    alt = _find_alternative_key(failed_key)
                    if alt:
                        _log(f"  400 credit error on key, retrying with alternative key")
                        a_headers = {"x-api-key": alt.get("api_key", ""), "Content-Type": "application/json",
                                     "anthropic-version": "2023-06-01"}
                        try:
                            resp, a_headers = await _do_request_with_retry(endpoint, anthro_body, a_headers, "anthropic")
                            if resp.status_code == 200:
                                account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
                            else:
                                return Response(content=json.dumps({"error": {"message": "All API keys exhausted. Check your billing."}}), status_code=503, media_type="application/json")
                        except UpstreamError as e:
                            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
                # Convert 429/401/403 → 503 to avoid Claude Code auth window
                if resp.status_code in (429, 401, 403):
                    return _openai_error(503, _auth_window_message(resp.status_code))
                if resp.status_code == 499:
                    return _openai_error(502, "Upstream disconnected (499). Retrying may help.")
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
                         effort, client_ip, account_alias, tool_names, tools_used=used if used else None,
                         request_body=request_body, response_body=data)
            oai_resp = anthropic_to_openai_responses(data, original_model)
            return Response(content=json.dumps(oai_resp, ensure_ascii=False), media_type="application/json")
        # Anthropic streaming → collect, then emit SSE
        anthro_body["stream"] = False
        # Try free model first if available
        try:
            free_result = await _try_free_model_first(anthro_body, a_headers, "anthropic", model_id)
            if free_result is not None:
                resp, a_headers, _actual_model, _actual_ip = free_result
                model_id = _actual_model
            else:
                resp, a_headers = await _do_request_with_retry(endpoint, anthro_body, a_headers, "anthropic")
        except FreeQuotaExhausted as e:
            return _free_quota_exhausted_response(e, "anthropic")
        except UpstreamError as e:
            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
        account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
        if resp.status_code != 200:
            await _log_and_save_error(req_id, model_id, original_model, start_time,
                         resp.status_code, resp.text, protocol, True, thinking_type,
                         effort, client_ip, account_alias, tool_names,
                         request_body=request_body, response_body={"error": resp.text[:2000]})
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
                     effort, client_ip, account_alias, tool_names, tools_used=used if used else None,
                     request_body=request_body, response_body=data)
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
    try:
        headers = _get_auth_headers("openai")
    except AllKeysPausedError as e:
        # If a free model exists, try it before giving up
        if FREE_MODEL_MAP.get(model_id):
            try:
                free_result = await _try_free_model_first(oai_body, {}, "openai", model_id)
                if free_result is not None:
                    resp, _, _actual_model, _actual_ip = free_result
                    data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
                    usage = data.get("usage", {})
                    req_in = usage.get("prompt_tokens", 0)
                    req_out = usage.get("completion_tokens", 0)
                    cache = _extract_cache_tokens(usage)
                    _update_token_usage(_actual_model, req_in, req_out, cache)
                    used = _extract_usage_tool_names(data)
                    await _save_and_log_request(req_id, _actual_model, original_model, start_time,
                                 req_in, req_out, cache, protocol, False, thinking_type,
                                 effort, client_ip, "free (no auth)", tool_names, tools_used=used,
                                 request_body=request_body, response_body=data, free_model_ip=_actual_ip)
                    oai_resp = openai_chat_to_responses(data, original_model)
                    payload = json.dumps({"type": "response.completed", "response": oai_resp}, ensure_ascii=False)
                    sse_body = f"data: {payload}\n\ndata: [DONE]\n\n".encode()
                    return Response(content=sse_body, media_type="text/event-stream",
                                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
            except FreeQuotaExhausted as e:
                return _free_quota_exhausted_response(e, "openai")
            except Exception as e:
                _debug(f"  [free] free model attempt failed: {e}")
        retry_after = int(e.retry_after) + 1
        return Response(
            content=json.dumps({"error": {"message": f"All API keys exhausted. Retry after {retry_after}s.",
                "type": "api_error", "code": "503"}}),
            status_code=503, media_type="application/json",
            headers={"Retry-After": str(retry_after)})
    is_stream = oai_body["stream"]

    if not is_stream:
        # Try free model first if available
        try:
            free_result = await _try_free_model_first(oai_body, headers, "openai", model_id)
            if free_result is not None:
                resp, headers, _actual_model, _actual_ip = free_result
                model_id = _actual_model
            else:
                resp, headers = await _do_request_with_retry(endpoint, oai_body, headers, "openai")
        except FreeQuotaExhausted as e:
            return _free_quota_exhausted_response(e, "openai")
        except UpstreamError as e:
            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
        account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
        if resp.status_code != 200:
            await _log_and_save_error(req_id, model_id, original_model, start_time,
                         resp.status_code, resp.text, protocol, is_stream, thinking_type,
                         effort, client_ip, account_alias, tool_names,
                         request_body=request_body, response_body={"error": resp.text[:2000]})
            # Convert 429/401/403 → 503 to avoid Claude Code auth window
            if resp.status_code in (429, 401, 403):
                return _openai_error(503, _auth_window_message(resp.status_code))
            if resp.status_code == 499:
                return _openai_error(502, "Upstream disconnected (499). Retrying may help.")
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
        used = _extract_usage_tool_names(data)
        await _save_and_log_request(req_id, model_id, original_model, start_time,
                     req_in, req_out, cache, protocol, is_stream, thinking_type,
                     effort, client_ip, account_alias, tool_names, tools_used=used if used else None,
                     request_body=request_body, response_body=data)
        # Direct conversion: Chat Completions → Responses (no intermediate Anthropic format)
        oai_resp = openai_chat_to_responses(data, original_model)
        return Response(content=json.dumps(oai_resp, ensure_ascii=False), media_type="application/json")

    # ── Streaming (OpenAI backend) — collect, then emit SSE ──
    oai_body["stream"] = False
    # Try free model first if available
    try:
        free_result = await _try_free_model_first(oai_body, headers, "openai", model_id)
        if free_result is not None:
            resp, headers, _actual_model, _actual_ip = free_result
            model_id = _actual_model
        else:
            resp, headers = await _do_request_with_retry(endpoint, oai_body, headers, "openai")
    except FreeQuotaExhausted as e:
        return _free_quota_exhausted_response(e, "openai")
    except UpstreamError as e:
        return JSONResponse(status_code=e.status_code, content={"error": str(e)})
    account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
    if resp.status_code != 200:
        await _log_and_save_error(req_id, model_id, original_model, start_time,
                     resp.status_code, resp.text, protocol, True, thinking_type,
                     effort, client_ip, account_alias, tool_names,
                     request_body=request_body, response_body={"error": resp.text[:2000]})
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
    used = _extract_usage_tool_names(data)
    await _save_and_log_request(req_id, model_id, original_model, start_time,
                 req_in, req_out, cache, protocol, True, thinking_type,
                 effort, client_ip, account_alias, tool_names, tools_used=used if used else None,
                 request_body=request_body, response_body=data)
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
            # [46] VPN logs were stderr-only (no handler attached) — route
            # them into the same rich log panel as the rest of the app.
            # Append, don't replace: attach_module_logger() at startup put a
            # debug.log FileHandler on these loggers, and replacing handlers
            # + propagate=False made [vpn]/[vpn-watchdog] lines invisible in
            # logs/debug.log exactly during AUTH_FAILED incidents.
            for name in ("vpn_manager", "free_ip_pool"):
                attach_panel_logger(name, h)

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


# ── Mono-instance lock [CRITIC(7)] ────────────────────────────────────
_INSTANCE_LOCK_FDS = []  # keep fds referenced: GC closing them would release the lock

def _acquire_instance_lock(lock_path: str = os.path.join("logs", "opencode.lock")) -> None:
    """Take a non-blocking exclusive file lock; exit if another instance holds it.

    [CRITIC(7)] Two proxy instances would fight over port 4000, the rotation
    machinery and the SQLite DB. The lock file is advisory — one lock per
    host — so a second `python opencode.py` exits immediately with a clear
    message instead of corrupting state.
    """
    import sys
    try:
        os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as e:
        _log(f"WARNING: cannot create lock file {lock_path}: {e} — continuing without mono-instance guard")
        return
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # stderr print: the Rich log panel is never built when we exit here,
        # so this is the only place the user sees why the instance refused to start.
        print(f"FATAL: another opencode-proxy instance is already running (lock held: {lock_path})",
              file=sys.stderr, flush=True)
        _log(f"FATAL: another opencode-proxy instance is already running (lock held: {lock_path})")
        try:
            os.close(fd)
        except OSError:
            pass
        sys.exit(1)
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"pid={os.getpid()}\n".encode("ascii"))
    except OSError:
        pass
    _INSTANCE_LOCK_FDS.append(fd)
    _debug(f"  [lock] instance lock acquired: {lock_path}")


if __name__ == "__main__":
    import sys
    import signal
    import traceback as _traceback

    _acquire_instance_lock()

    # GUI by default (system tray + dashboard window); --no-gui forces terminal mode.
    # --gui is still accepted for backward compatibility (no-op since it's the default).
    use_gui = "--no-gui" not in sys.argv

    mgr = ServerManager(app, HOST, PORT, WEB_PORT)
    _server_manager = mgr

    # Signal handler for clean shutdown on SIGINT/SIGTERM
    def _shutdown_handler(signum, frame):
        _log(f"Signal {signum} received, shutting down...")
        try:
            mgr.stop()
        except Exception:
            pass
        _db_flush()
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown_handler)
    try:
        signal.signal(signal.SIGTERM, _shutdown_handler)
    except (OSError, AttributeError):
        pass  # SIGTERM not available on Windows

    mgr.start()

    _log(f"API: http://localhost:{PORT}")

    if use_gui:
        try:
            from gui import run_gui
        except ImportError:
            # GUI is the default — fall back to terminal mode instead of exiting.
            print("GUI dependencies not installed (pystray Pillow pywebview). Falling back to terminal mode.")
            print("Install with: pip install pystray Pillow pywebview  (or use --no-gui to skip this check)")
            use_gui = False
    if use_gui:
        run_gui(mgr, HOST, PORT, WEB_PORT)
    else:
        try:
            run_terminal_loop(ROUTES, _token_usage, _token_lock)
        except KeyboardInterrupt:
            _log("Interrupted by user (Ctrl+C)")
        except Exception as e:
            _log(f"FATAL: terminal loop crashed: {type(e).__name__}: {e}")
            _debug(f"  [FATAL] traceback:\n{_traceback.format_exc()}")
        finally:
            _debug("  [main] cleaning up after terminal loop exit...")
            try:
                mgr.stop()
            except Exception:
                pass
            _db_flush()
