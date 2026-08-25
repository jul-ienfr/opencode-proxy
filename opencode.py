"""
Claude Code Proxy → opencode.ai
Convert Anthropic /v1/messages ↔ OpenAI chat/completions
"""

import asyncio
import contextvars
import copy
import datetime
import email.utils
import hmac
import ipaddress
import itertools
import json
import logging
import os
import random
import re
import re as _re_norm
import socket
import sqlite3
import threading
import time
import traceback
import urllib.parse
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager

import httpx
import yaml
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

import config.settings as _cfg_settings
from config import (
    API_BASE_FREE,
    API_KEY,
    API_KEY_ROUTING,
    API_KEYS,
    CUSTOM_ROUTES,
    DEBUG,
    DISABLE_MAPPING,
    FREE_MODEL_MAP,
    HOST,
    IP_ROTATION,
    MODELS,
    PORT,
    PROXY,
    ROUTES,
    WEB_PORT,
    get_model_config,
    maybe_reload_custom_routes,
    yaml_get,
)

# P2 geo: dynamic GEO_ENABLED via settings (hot-reload), base resolver alias

try:
    from vpn_manager import _normalize_country as _vpn_normalize_country
except ImportError:

    def _vpn_normalize_country(n):  # fallback never invents beyond title()
        return n.strip().replace("_", " ").strip().title()

try:
    from vpn_manager import get_socks5_proxy_url
except ImportError:
    try:
        from config import PROXY as _PROXY_FALLBACK
    except ImportError:
        _PROXY_FALLBACK = ""

    def get_socks5_proxy_url() -> str:
        return _PROXY_FALLBACK

# ── Web search/fetch shared primitives (v3.3) ──
_DDG_CACHE: OrderedDict = OrderedDict()  # kstr -> (expiry, formatted)
_DDG_LOCKS: dict[str, asyncio.Lock] = {}
_DDG_SEM = asyncio.Semaphore(3)
FETCH_SEM = asyncio.Semaphore(5)
_BLOCKED_NETS = [
    ipaddress.ip_network(c)
    for c in (
        "0.0.0.0/8",
        "169.254.0.0/16",
        "100.64.0.0/10",
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "::/128",
        "::1/128",
        "fe80::/10",
        "fc00::/7",
        "ff00::/8",
    )
]



# ── API key routing ──
# F-H3: threading.Lock kept (sync+async mix avoids asyncio.Lock deadlock from sync thread).
# itertools.cycle replaced by atomic index modulo under lock — no async-unsafe cycle.
_key_cycle_keys: list[str] = []
_key_cycle_index: int = 0
_key_failover_index = 0
_key_cycle_lock = threading.Lock()  # protects _key_cycle_keys/_key_cycle_index


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
        _debug(
            f"  [apikey] .env fallback key paused ({remaining:.0f}s) — raising AllKeysPausedError"
        )
        raise AllKeysPausedError(remaining if remaining > 0 else 1)
    return {"api_key": API_KEY}


def get_next_api_key() -> dict:
    global _key_cycle_keys, _key_failover_index
    if not API_KEYS:
        _debug("  [apikey] no API_KEYS configured, falling back to .env key")
        return _env_key_or_raise()
    enabled = _get_enabled_keys()
    if not enabled:
        _debug("  [apikey] no enabled keys, falling back to .env key")
        return _env_key_or_raise()

    # Filter out paused keys
    available = [k for k in enabled if not _key_pauser.is_paused(k.get("api_key", ""))]

    if not available:
        # All paused — raise with shortest wait time instead of reusing a paused key
        min_rem = min((_key_pauser.remaining(k.get("api_key", "")) for k in enabled), default=0)
        if min_rem > 0:
            _debug(
                f"  [apikey] ALL keys paused, min remaining={min_rem:.0f}s — raising AllKeysPausedError"
            )
            raise AllKeysPausedError(min_rem)
        _debug("  [apikey] no keys available, falling back to .env key")
        return _env_key_or_raise()

    if len(available) == 1:
        _debug(f"  [apikey] single available key: alias={available[0].get('alias', '?')}")
        return available[0]

    if API_KEY_ROUTING == "failover":
        for i in range(len(API_KEYS)):
            idx = (_key_failover_index + i) % len(API_KEYS)
            if API_KEYS[idx].get("enabled", True) and not _key_pauser.is_paused(
                API_KEYS[idx].get("api_key", "")
            ):
                _debug(
                    f"  [apikey] failover selected alias={API_KEYS[idx].get('alias', '?')} (idx={idx})"
                )
                return API_KEYS[idx]
        # Fallback to shortest-paused
        min_rem = min((_key_pauser.remaining(k.get("api_key", "")) for k in enabled), default=0)
        if min_rem > 0:
            raise AllKeysPausedError(min_rem)
        _debug("  [apikey] failover exhausted, falling back to .env key")
        return _env_key_or_raise()

    # Round-robin: atomic index modulo under threading.Lock (no itertools.cycle)
    global _key_cycle_index, _key_cycle_keys
    with _key_cycle_lock:
        current_ids = [k.get("api_key") for k in available]
        if _key_cycle_keys != current_ids:
            _key_cycle_keys = current_ids
            _key_cycle_index = 0
            _debug(
                f"  [apikey] round-robin index reset: {len(available)} available keys (filtered from {len(enabled)} enabled)"
            )
        idx = _key_cycle_index % len(available) if available else 0
        selected = available[idx]
        _key_cycle_index = (idx + 1) % len(available) if available else 0
        _debug(f"  [apikey] round-robin selected alias={selected.get('alias', '?')} idx={idx}")
        return selected


def _find_alternative_key(failed_key: str) -> dict | None:
    """Return the first enabled, non-paused key different from failed_key, or None."""
    for k in API_KEYS:
        if k.get("api_key") != failed_key and k.get("enabled", True):
            if not _key_pauser.is_paused(k.get("api_key", "")):
                _debug(f"  [apikey] alternative key found alias={k.get('alias', '?')}")
                return k
    _debug(f"  [apikey] no alternative key for {failed_key[:8]}...")
    return None


_key_alias_cache: dict[str, str] = {}


def _rebuild_key_cache():
    """Rebuild the API key → alias lookup dict."""
    global _key_alias_cache
    _key_alias_cache = {
        k["api_key"]: k.get("alias", "") or "" for k in API_KEYS if k.get("api_key")
    }


def _alias_for_key(api_key: str) -> str:
    """Look up the alias for a given API key. O(1) via dict cache."""
    return _key_alias_cache.get(api_key, "")


# ── Key pause tracker ─────────────────────────────────────────


# [plan v10 §14.1.15] même condition HTTP = même durée, stream ou non :
# 401 = clé temporairement indisponible (quota) — 1h des deux côtés.
KEY_PAUSE_401_SEC = 3600.0


class _KeyPauser:
    """Per-key rate limit pause tracker. Pauses a key when upstream returns 429.

    Persists pause state to logs/paused_keys.yaml so pauses survive reboots.
    """

    _PAUSED_FILE = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "logs", "paused_keys.yaml"
    )

    def __init__(self, max_pause: float = None):
        if max_pause is None:
            max_pause = float(yaml_get("key_pause", "max_pause", 600))
        self._paused: dict[str, float] = {}  # key_prefix -> monotonic expiry
        self._reasons: dict[str, str] = {}  # key_prefix -> reason string
        self._lock = threading.Lock()
        self._max_pause = max_pause

    @staticmethod
    def _prefix(api_key: str) -> str:
        """Slot stable par clé ENTIÈRE.

        [plan v10 Lot 0 filet — bug réel] l'ancien `api_key[:12]` fusionnait
        toutes les clés partageant le préfixe fournisseur (« sk-ant-api03 » =
        exactement 12 caractères) : mettre en pause UNE clé mettait en pause
        TOUTES les clés Anthropic, et la sémantique « seulement étendre »
        collait la plus longue pause à tout le monde. Hash tronqué = slot
        unique par clé, toujours non réversible pour les logs. Les entrées
        persistées sous l'ancien schéma deviennent orphelines et expirent
        naturellement (jamais re-matchées)."""
        if not api_key:
            return ""
        import hashlib

        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]

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
                # [plan v10 §9.1.2] tmp+fsync+replace — l'écriture directe
                # pouvait laisser un YAML tronqué sur crash/kill (les clés
                # pausées disparaissaient alors au prochain load).
                tmp = file_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    yaml.dump(payload, f, default_flow_style=False)
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except OSError:
                        pass
                os.replace(tmp, file_path)

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
            with open(self._PAUSED_FILE, encoding="utf-8") as f:
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
        _debug(
            f"  [keypauser] PAUSED alias={alias} prefix={prefix} for {duration:.0f}s reason={reason}"
        )
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
        from dashboard.quota import fetch_quotas

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
                _key_pauser.pause_key(
                    api_key,
                    reset_sec,
                    f"quota {window} {usage:.0f}% → réactivation {re_enable_str}",
                    quota_based=True,
                )
                _log(
                    f"  QUOTA {window.upper()} AT {usage:.0f}% — key {alias} paused until {re_enable_str} (in {reset_sec:.0f}s)"
                )
                return

        # No quota at 100% — use default pause
        _default_pause = float(yaml_get("key_pause", "default_pause", 60))
        _key_pauser.pause_key(api_key, _default_pause, "429 (no quota at 100%)")
    except Exception as e:
        _debug(f"  [quota] failed to fetch quotas for 429 pause: {e}")
        # Fallback to default pause
        _default_pause = float(yaml_get("key_pause", "default_pause", 60))
        _key_pauser.pause_key(api_key, _default_pause, "429 (quota fetch failed)")


def _key_from_headers(headers: dict | None, protocol: str) -> str:
    """Extract the API key from request headers."""
    if not headers:
        return ""
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
        return {
            "x-api-key": ak,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
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


from dashboard import (  # noqa: E402  # after _fast_id (uses itertools/time at runtime)
    register_dashboard,
    start_quota_fetcher,
)
from dashboard.display import (  # noqa: E402
    RichLogHandler,
    attach_module_logger,
    attach_panel_logger,
    run_terminal_loop,
    set_debug_log_file,
)
from dashboard.display import debug as _debug  # noqa: E402
from dashboard.display import log as _log  # noqa: E402
from dashboard.events import get_event_manager  # noqa: E402

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
        full = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        if len(full) <= max_chars:
            return full
        # Fell through: full was too big, build truncated version
    # Build truncated version (skip full serialization of large body)
    truncated = {k: v for k, v in body.items() if k != "messages"}
    if messages:
        truncated["messages"] = messages[:2] + [
            {"_truncated": True, "original_count": len(messages)}
        ]
    result = json.dumps(truncated, ensure_ascii=False, separators=(",", ":"))
    if len(result) > max_chars:
        result = result[:max_chars]
    return result


# SQLite setup — synchronous connection, all DB ops run in thread pool
_db_path = os.path.join(LOG_DIR, "requests.db")
_conn = sqlite3.connect(_db_path, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_conn.execute("PRAGMA journal_mode=WAL")
_conn.execute(f"PRAGMA busy_timeout={yaml_get('database', 'busy_timeout', 5000)}")
_conn.execute("PRAGMA synchronous=NORMAL")  # WAL+NORMAL: safe crash-resilient, 50-90% fewer fsyncs
_conn.execute(f"PRAGMA cache_size=-{yaml_get('database', 'cache_size', 64000)}")  # page cache
_conn.execute("PRAGMA temp_store=MEMORY")  # temp tables in RAM
_conn.execute(
    f"PRAGMA mmap_size={yaml_get('database', 'mmap_size', 268435456)}"
)  # memory-mapped I/O
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
for col, default in [
    ("protocol", "NULL"),
    ("is_stream", "0"),
    ("thinking", "NULL"),
    ("effort", "NULL"),
    ("client_ip", "NULL"),
    ("account_alias", "NULL"),
    ("tools", "NULL"),
    ("tools_used", "NULL"),
    ("request_body", "NULL"),
    ("response_body", "NULL"),
    ("client_user_agent", "NULL"),
    ("free_model_ip", "NULL"),
    ("identity", "NULL"),
    ("geo_country", "NULL"),
    ("geo_blocked", "0"),
    ("hedged", "0"),
    ("winner_station", "NULL"),
    ("geo_direct_country", "NULL"),
    ("geo_direct_ip", "NULL"),
    ("geo_via_vpn", "0"),
    ("geo_allowed", "NULL"),
]:
    try:
        _conn.execute(f"ALTER TABLE requests ADD COLUMN {col} TEXT DEFAULT {default}")
    except Exception:
        pass
# [plan v10 §4 Lot 4] colonne station INTEGER (filtres ?station= dashboard) +
# index composé station+timestamp (budget §7 : pas de scan à chaque filtre).
try:
    _conn.execute("ALTER TABLE requests ADD COLUMN station INTEGER DEFAULT NULL")
except Exception:
    pass
_conn.execute(
    "CREATE INDEX IF NOT EXISTS idx_requests_station_ts ON requests(station, timestamp)"
)
_conn.commit()
_debug(f"  [db] SQLite connection established: {_db_path}")

# [30] Canary: mixed naive/UTC timestamps break ORDER BY timestamp DESC (BINARY
# collation) — warn the operator that scripts/migrate_timestamps_utc.py is pending.
try:
    _naive = _conn.execute(
        "SELECT COUNT(*) FROM requests WHERE timestamp NOT LIKE '%Z'"
    ).fetchone()[0]
    if _naive:
        _log(
            f"  WARNING: {_naive} requests row(s) with naive timestamps (mixed local/UTC) — run scripts/migrate_timestamps_utc.py"
        )
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
_debug("  [db] free_model_usage table ready")


_db_pending_inserts = 0
_db_last_commit = time.monotonic()
_DB_COMMIT_INTERVAL = yaml_get("database", "commit_interval", 5)  # seconds between periodic commits
_DB_COMMIT_BATCH = yaml_get("database", "commit_batch", 10)  # force commit after N inserts
_db_commit_lock = threading.Lock()
# Ultra-fast async DB queue — single writer, no per-request to_thread
_db_queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
_db_writer_task: asyncio.Task | None = None

# Context variable to pass client user-agent from endpoint handlers to _save_request
# without threading it through every intermediate function call.
_current_user_agent: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_current_user_agent", default=None
)

# Context variable carrying the free-channel attempt (egress IP + identity profile)
# from the two places a free request actually leaves (_try_free_model_first,
# _open_free_stream) to the leaf _save_request — same pattern as _current_user_agent.
# Scoped to the asyncio task handling one client request; never leaks between requests.
# Value: {"ip": str, "identity": str} — identity = profile impersonate (fingerprint).
_current_free_attempt: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_current_free_attempt", default=None
)

# Geo direct → VPN fallback context (IP+pays direct, allowed, via flag)
# Set by _enforce_geo_gate per request, read by _save_request / _geo_headers / tray.
_current_geo: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "_current_geo", default=None
)


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
        cutoff_delete = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - delete_after_days * 86400)
        )
        cursor = _conn.execute("DELETE FROM requests WHERE timestamp < ?", (cutoff_delete,))
        deleted = cursor.rowcount
        cursor2 = _conn.execute(
            "DELETE FROM free_model_usage WHERE timestamp < ?", (cutoff_delete,)
        )
        deleted2 = cursor2.rowcount

        # Phase 2: Nullify bodies for 7-30 day old rows
        cutoff_null = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - retention_days * 86400)
        )
        cursor3 = _conn.execute(
            "UPDATE requests SET request_body = NULL, response_body = NULL "
            "WHERE timestamp < ? AND (request_body IS NOT NULL OR response_body IS NOT NULL)",
            (cutoff_null,),
        )
        cleaned = cursor3.rowcount

        total = deleted + deleted2 + cleaned
        if total > 0:
            _conn.commit()
            _debug(
                f"  [db] cleanup: deleted {deleted}+{deleted2} old rows, cleared bodies from {cleaned} requests"
            )
            _log(
                f"  DB CLEANUP: deleted {deleted + deleted2} old rows, cleared {cleaned} bodies (>{retention_days}d)"
            )
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
        return (
            datetime.datetime.fromisoformat(timestamp)
            .astimezone()
            .astimezone(datetime.UTC)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )
    except ValueError:
        return timestamp  # unparseable — store as-is rather than dropping the row


def _db_insert_sync(
    req_id,
    timestamp,
    model,
    original_model,
    duration_ms,
    tokens_input,
    tokens_output,
    tokens_cache,
    success,
    error,
    protocol,
    is_stream,
    thinking,
    effort,
    client_ip,
    account_alias,
    tools_json,
    tools_used_json,
    request_body_json=None,
    response_body_json=None,
    client_user_agent=None,
    free_model_ip=None,
    identity=None,
    geo_country=None,
    geo_blocked=None,
    geo_direct_country=None,
    geo_direct_ip=None,
    geo_via_vpn=None,
    geo_allowed=None,
    station=None,
):
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
        _conn.execute(
            """
            INSERT OR REPLACE INTO requests (id, timestamp, model, original_model, duration_ms,
                tokens_input, tokens_output, tokens_cache, success, error,
                protocol, is_stream, thinking, effort, client_ip, account_alias, tools, tools_used,
                request_body, response_body, client_user_agent, free_model_ip, identity, geo_country, geo_blocked,
                geo_direct_country, geo_direct_ip, geo_via_vpn, geo_allowed, station)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
            (
                req_id,
                timestamp,
                model,
                original_model,
                duration_ms,
                tokens_input,
                tokens_output,
                tokens_cache,
                1 if success else 0,
                error,
                protocol,
                1 if is_stream else 0,
                thinking,
                effort,
                client_ip,
                account_alias,
                tools_json,
                tools_used_json,
                request_body_json,
                response_body_json,
                client_user_agent,
                free_model_ip,
                identity,
                geo_country,
                geo_blocked,
                geo_direct_country,
                geo_direct_ip,
                1 if geo_via_vpn else 0,
                geo_allowed,
                station,
            ),
        )
        # Batch commit logic
        _db_pending_inserts += 1
        now = time.monotonic()
        elapsed = now - _db_last_commit
        if _db_pending_inserts >= _DB_COMMIT_BATCH or elapsed >= _DB_COMMIT_INTERVAL:
            try:
                _conn.commit()
                _debug(
                    f"  [db] _db_insert_sync: batch-committed {_db_pending_inserts} inserts ({elapsed:.1f}s) in {(time.monotonic() - t0) * 1000:.1f}ms"
                )
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
            _debug(
                f"  [db] _db_insert_sync: queued req_id={req_id} (pending={_db_pending_inserts}, {elapsed:.1f}s since last commit)"
            )


def _db_execute_batch_sync(batch: list[tuple]):
    """Execute a batch of DB inserts in a single transaction (called in thread pool)."""
    if not batch:
        return 0
    with _db_commit_lock:
        for item in batch:
            _conn.execute(
                """
                INSERT OR REPLACE INTO requests (id, timestamp, model, original_model, duration_ms,
                    tokens_input, tokens_output, tokens_cache, success, error,
                    protocol, is_stream, thinking, effort, client_ip, account_alias, tools, tools_used,
                    request_body, response_body, client_user_agent, free_model_ip, identity, geo_country, geo_blocked,
                    geo_direct_country, geo_direct_ip, geo_via_vpn, geo_allowed, station)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                item,
            )
        try:
            _conn.commit()
        except Exception as e:
            _debug(f"  [db] batch commit FAILED: {type(e).__name__}: {e}")
            try:
                _conn.rollback()
            except Exception:
                pass
            return 0
        return len(batch)


async def _db_writer_loop():
    """Single writer: batches DB inserts from queue, no per-request to_thread."""
    batch: list[tuple] = []
    while True:
        try:
            item = await _db_queue.get()
            batch.append(item)
            while len(batch) < 32:
                try:
                    nxt = await asyncio.wait_for(_db_queue.get(), timeout=0.05)
                    batch.append(nxt)
                except TimeoutError:
                    break
            # [plan v10 §14.3.15] si to_thread lève, l'ancien code laissait le
            # batch rempli → ré-exécution des mêmes items au prochain tour
            # (doublons) + task_done décalé. Vidange/tâches dans finally.
            try:
                await asyncio.to_thread(_db_execute_batch_sync, batch)
            finally:
                n_done = len(batch)
                batch = []
                for _ in range(n_done):
                    _db_queue.task_done()
            if n_done >= 32:
                _debug(f"  [db] writer batch {n_done} committed")
        except asyncio.CancelledError:
            if batch:
                try:
                    await asyncio.to_thread(_db_execute_batch_sync, batch)
                except Exception:
                    pass
            break
        except Exception as e:
            _debug(f"  [db] writer error: {type(e).__name__}: {e}")
            await asyncio.sleep(0.1)


async def _save_request(
    req_id,
    model,
    original_model,
    duration_ms,
    tokens_input,
    tokens_output,
    tokens_cache,
    success=True,
    error=None,
    protocol=None,
    is_stream=False,
    thinking=None,
    effort=None,
    client_ip=None,
    account_alias=None,
    tools=None,
    tools_used=None,
    request_body=None,
    response_body=None,
    free_model_ip=None,
    identity=None,
    geo_country=None,
    geo_blocked=None,
    geo_direct_country=None,
    geo_direct_ip=None,
    geo_via_vpn=None,
    geo_allowed=None,
    station=None,
):
    tools_json = json.dumps(tools) if tools else "[]"
    tools_used_json = json.dumps(list(dict.fromkeys(tools_used))) if tools_used else "[]"
    # B1: redact before DB INSERT (was only on _debug)
    request_body_json = _redact(_truncate_body_for_storage(request_body)) if request_body else None
    response_body_json = (
        _redact(_truncate_body_for_storage(response_body)) if response_body else None
    )
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
    # [plan v10 §4 Lot 4] n° de station pour les filtres ?station= dashboard :
    # lu depuis le contextvar free si non fourni explicitement.
    if station is None:
        try:
            _att_st = (_current_free_attempt.get() or {}).get("station")
            station = int(getattr(_att_st, "_station", 0) or 0) or None
        except Exception:
            station = None
    # [30] UTC everywhere — Z suffix
    timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    timestamp = _normalize_timestamp_utc(timestamp)
    # Geo direct extra — pull from ContextVar set by _enforce_geo_gate
    if geo_direct_country is None or geo_direct_ip is None or geo_via_vpn is None or geo_allowed is None:
        try:
            _g = _current_geo.get()
            if _g:
                if geo_direct_country is None:
                    geo_direct_country = _g.get("direct_country")
                if geo_direct_ip is None:
                    geo_direct_ip = _g.get("direct_ip")
                if geo_via_vpn is None:
                    geo_via_vpn = _g.get("via_vpn")
                if geo_allowed is None:
                    _al = _g.get("allowed")
                    if isinstance(_al, list):
                        geo_allowed = ",".join(_al)
                    elif isinstance(_al, str):
                        geo_allowed = _al
                if geo_country is None and _g.get("current_country"):
                    geo_country = _g.get("current_country")
                if geo_blocked is None and _g.get("blocked") is not None:
                    geo_blocked = _g.get("blocked")
        except Exception:
            pass
    # normalize geo_allowed list → string for DB
    if isinstance(geo_allowed, list):
        geo_allowed = ",".join(geo_allowed)
    item = (
        req_id,
        timestamp,
        model,
        original_model,
        duration_ms,
        tokens_input,
        tokens_output,
        tokens_cache,
        1 if success else 0,
        error,
        protocol,
        1 if is_stream else 0,
        thinking,
        effort,
        client_ip,
        account_alias,
        tools_json,
        tools_used_json,
        request_body_json,
        response_body_json,
        client_user_agent,
        free_model_ip,
        identity,
        geo_country,
        geo_blocked,
        geo_direct_country,
        geo_direct_ip,
        1 if geo_via_vpn else 0,
        geo_allowed,
        station,
    )
    try:
        _db_queue.put_nowait(item)
    except asyncio.QueueFull:
        _debug(
            f"  [db] queue full ({_db_queue.qsize()}), dropping req_id={req_id} — fallback to direct"
        )
        try:
            await asyncio.to_thread(
                _db_insert_sync,
                req_id,
                timestamp,
                model,
                original_model,
                duration_ms,
                tokens_input,
                tokens_output,
                tokens_cache,
                success,
                error,
                protocol,
                is_stream,
                thinking,
                effort,
                client_ip,
                account_alias,
                tools_json,
                tools_used_json,
                request_body_json,
                response_body_json,
                client_user_agent,
                free_model_ip,
                identity,
                geo_country,
                geo_blocked,
                geo_direct_country,
                geo_direct_ip,
                geo_via_vpn,
                geo_allowed,
            )
        except Exception as e:
            _debug(f"  [db] fallback insert failed: {e}")
    else:
        _debug(
            f"  [db] _save_request: queued req_id={req_id} model={model} success={success} qsize={_db_queue.qsize()}"
        )
    # Notify dashboard SSE clients about the update
    try:
        get_event_manager().publish("stats_updated", {"time": timestamp})
        # Also publish request detail via SSE — reuse original Python objects
        tools_used_deduped = list(dict.fromkeys(tools_used)) if tools_used else []
        get_event_manager().publish(
            "request_completed",
            {
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
                "geo_country": geo_country,
                "geo_blocked": geo_blocked,
                "geo_direct_country": geo_direct_country,
                "geo_direct_ip": geo_direct_ip,
                "geo_via_vpn": bool(geo_via_vpn),
                "geo_allowed": geo_allowed,
                "tools": tools or [],
                "tools_used": tools_used_deduped,
            },
        )
    except Exception as e:
        _debug(f"  ✗ SSE publish failed: {type(e).__name__}: {e}")


def _log_free_model_usage(
    paid_model: str,
    free_model: str,
    api_key: str,
    workspace_id: str,
    status: int,
    tokens_in: int = 0,
    tokens_out: int = 0,
    duration_ms: int = 0,
    ip: str = "",
):
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
                (
                    timestamp,
                    paid_model,
                    free_model,
                    api_key[:16] + "...",
                    workspace_id,
                    status,
                    tokens_in,
                    tokens_out,
                    duration_ms,
                    ip,
                ),
            )
            _conn.commit()
        _debug(
            f"  [free-usage] logged: {free_model} key={api_key[:8]}... ws={workspace_id[:12]}... "
            f"status={status} ip={ip} in={tokens_in} out={tokens_out}"
        )
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
        _debug(
            f"  [db] token counters restored: {len(rows)} models, total in={total_in} out={total_out} cache={total_cache}"
        )
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


# ── orjson fast-path (5-10x vs stdlib json on large bodies) ──
try:
    import orjson as _orjson  # type: ignore

    def _json_loads(b: bytes | str, **kw):
        if isinstance(b, str):
            b = b.encode()
        return _orjson.loads(b)

    def _json_dumps(obj, **kw) -> bytes:
        if kw.get("indent") is not None:
            return json.dumps(
                obj, ensure_ascii=False, indent=kw.get("indent"), default=str
            ).encode()
        return _orjson.dumps(obj)

    def _json_dumps_str(obj, **kw) -> str:
        if kw.get("indent") is not None:
            return json.dumps(obj, ensure_ascii=False, indent=kw.get("indent"), default=str)
        if kw:
            return _orjson.dumps(obj).decode()
        return _orjson.dumps(obj).decode()

    _JSON_LIB = "orjson"
except ImportError:

    def _json_loads(b: bytes | str, **kw):  # type: ignore[no-redef]
        if isinstance(b, bytes):
            b = b.decode()
        return json.loads(b, **kw)

    def _json_dumps(obj, **kw) -> bytes:  # type: ignore[no-redef]
        if "indent" in kw:
            return json.dumps(
                obj, ensure_ascii=False, indent=kw.get("indent"), default=str
            ).encode()
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode()

    def _json_dumps_str(obj, **kw) -> str:  # type: ignore[no-redef]
        if "indent" in kw:
            return json.dumps(obj, ensure_ascii=False, indent=kw.get("indent"), default=str)
        return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))

    _JSON_LIB = "json"


def _drop_orphan_tool_messages(messages: list[dict]) -> list[dict]:
    """Filter role:tool messages whose tool_call_id has no preceding tool_calls id."""
    _seen_ids: set[str] = set()
    filtered: list[dict] = []
    for m in messages:
        if m.get("tool_calls"):
            for tc in m["tool_calls"]:
                tid = tc.get("id")
                if tid:
                    _seen_ids.add(tid)
            filtered.append(m)
        elif m.get("role") == "tool":
            cid = m.get("tool_call_id", "")
            if cid in _seen_ids:
                filtered.append(m)
            else:
                _debug(
                    f"  [orphan] DROP tool output call_id={cid!r} — no preceding tool_call (compaction or empty-name skip)"
                )
        else:
            filtered.append(m)
    return filtered


def _drop_orphan_responses_input(inp: list[dict]) -> list[dict]:
    """Filter function_call_output items whose call_id has no preceding function_call."""
    _known: set[str] = set()
    _filt: list[dict] = []
    for it in inp:
        t = it.get("type")
        if t == "function_call":
            cid = it.get("call_id") or it.get("id") or ""
            if cid:
                _known.add(cid)
            _filt.append(it)
        elif t == "function_call_output":
            cid = it.get("call_id") or ""
            if cid in _known:
                _filt.append(it)
            else:
                _debug(
                    f"  [orphan] DROP function_call_output call_id={cid!r} — no preceding function_call"
                )
        else:
            _filt.append(it)
    return _filt


def _build_http_limits() -> httpx.Limits:
    """Tuned for extreme speed: 500 conns, 200 keepalive, 30s expiry."""
    return httpx.Limits(
        max_connections=int(yaml_get("upstream", "max_connections", 500)),
        max_keepalive_connections=int(yaml_get("upstream", "max_keepalive", 200)),
        keepalive_expiry=float(yaml_get("upstream", "keepalive_expiry", 30)),
    )


def _build_http_timeout() -> httpx.Timeout:
    t2 = (
        yaml_get("upstream", "timeout", {})
        if isinstance(yaml_get("upstream", "timeout", {}), dict)
        else {}
    )
    return httpx.Timeout(
        connect=float(t2.get("connect", 5) if isinstance(t2, dict) else 5),
        read=float(t2.get("read", 600) if isinstance(t2, dict) else 600),
        write=float(t2.get("write", 10) if isinstance(t2, dict) else 10),
        pool=float(t2.get("pool", 5) if isinstance(t2, dict) else 5),
    )


# Shared HTTP client (reused across requests) with connection pooling + HTTP/2 multiplex
_transport = (
    httpx.AsyncHTTPTransport(
        proxy=PROXY,
        limits=_build_http_limits(),
        http2=True,
        retries=0,
    )
    if PROXY
    else httpx.AsyncHTTPTransport(
        limits=_build_http_limits(),
        http2=True,
        retries=0,
    )
)
_client = httpx.AsyncClient(transport=_transport, timeout=_build_http_timeout())

# ── Curl TLS pool (M reusable AsyncSessions per proxy+impersonate) ──
# [A1 perf / audit vitesse §2] L'ancien schéma « 1 session + lock global par
# clé » sérialisait chaque POST non-streaming derrière le téléchargement
# complet (head-of-line blocking : toutes les requêtes d'une station en
# file invisible, read timeout 600 s). Un pool de M sessions avec
# checkout/checkin rend le parallélisme intra-station. Sûr quelle que soit
# la version curl_cffi : une session n'est JAMAIS partagée concurrentment
# (un seul emprunteur à la fois) ; les streams ne tiennent une session que
# jusqu'aux headers, comme avant.


class _CurlSessionSlot:
    __slots__ = ("sess", "busy")

    def __init__(self, sess):
        self.sess = sess
        self.busy = False


class _CurlSessionPool:
    """Pool FIFO de M sessions curl pour une clé (proxy, impersonate).

    checkout() réutilise une session libre, sinon crée dans la limite M,
    sinon attend une restitution (Condition — rare : concurrence > M).
    checkin() restitue ; evict() ferme une session fautive et libère sa
    place (l'éviction de la session fautive est conservée de l'ancien code).
    """

    __slots__ = ("slots", "_cond", "max_size")

    def __init__(self, max_size: int = 3):
        self.slots: list[_CurlSessionSlot] = []
        self._cond = asyncio.Condition()
        self.max_size = max(1, int(max_size))

    def _try_checkout(self) -> _CurlSessionSlot | None:
        for slot in self.slots:
            if not slot.busy:
                slot.busy = True
                return slot
        return None

    async def checkout(self, factory) -> _CurlSessionSlot:
        async with self._cond:
            slot = self._try_checkout()
            while slot is None:
                if len(self.slots) < self.max_size:
                    slot = _CurlSessionSlot(factory())
                    slot.busy = True
                    self.slots.append(slot)
                    return slot
                # M/M occupées → attendre une restitution (aucune session
                # jamais partagée concurrentment).
                await self._cond.wait()
                slot = self._try_checkout()
            return slot

    async def checkin(self, slot: _CurlSessionSlot) -> None:
        async with self._cond:
            if slot in self.slots:
                slot.busy = False
            self._cond.notify()

    async def evict(self, slot: _CurlSessionSlot) -> None:
        """Ferme une session fautive et la retire du pool."""
        async with self._cond:
            try:
                self.slots.remove(slot)
            except ValueError:
                pass
            try:
                await slot.sess.close()
            except Exception:
                pass
            self._cond.notify()

    def discard(self, slot: _CurlSessionSlot) -> None:
        """Retire sans fermer (chemin annulation : pas d'await possible)."""
        try:
            self.slots.remove(slot)
        except ValueError:
            pass

    async def close_all(self) -> None:
        for slot in list(self.slots):
            try:
                await slot.sess.close()
            except Exception:
                pass
        self.slots.clear()


def _curl_pool_size() -> int:
    """[A1] Taille M du pool par (proxy, impersonate), hot-reloadable."""
    try:
        return max(1, int(yaml_get("upstream", "curl_sessions_per_station", 3)))
    except Exception:
        return 3


_curl_pool: dict[str, _CurlSessionPool] = {}  # key -> pool (E2: get direct mono-thread)


def _evict_later(pool: "_CurlSessionPool", slot: "_CurlSessionSlot") -> None:
    """Fire-and-forget eviction — utilisable depuis un except CancelledError."""

    async def _do():
        try:
            await pool.evict(slot)
        except Exception:
            pass

    try:
        asyncio.get_running_loop().create_task(_do())
    except RuntimeError:
        pool.discard(slot)


async def _get_pooled_curl_session(proxy_url: str | None, impersonate: str):
    """Emprunte une session du pool pour (proxy, impersonate).

    Retourne (pool, slot) : l'appelant POSTe via ``slot.sess`` SANS tenir de
    verrou pendant le transfert, puis ``await pool.checkin(slot)`` (succès)
    ou éviction (session fautive). Les streams ne gardent l'emprunt que
    jusqu'aux headers.
    """
    key = f"{proxy_url or ''}|{impersonate}"
    # [E2 perf] dict.get mono-thread — pas de lock global ; seul l'état du
    # pool lui-même est sous Condition (dans checkout/checkin/evict).
    pool = _curl_pool.get(key)
    if pool is None:
        from curl_cffi.requests import AsyncSession

        pool = _CurlSessionPool(_curl_pool_size())
        _curl_pool[key] = pool
        slot = await pool.checkout(lambda: AsyncSession(impersonate=impersonate, proxy=_curl_proxy_url(proxy_url)))
        return pool, slot
    from curl_cffi.requests import AsyncSession

    slot = await pool.checkout(
        lambda: AsyncSession(impersonate=impersonate, proxy=_curl_proxy_url(proxy_url))
    )
    return pool, slot


async def _close_curl_pool():
    """Close all pooled curl sessions (lifespan shutdown)."""
    for pool in list(_curl_pool.values()):
        await pool.close_all()
    _curl_pool.clear()


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
        _transport = (
            httpx.AsyncHTTPTransport(
                proxy=PROXY,
                limits=_build_http_limits(),
                http2=True,
                retries=0,
            )
            if PROXY
            else httpx.AsyncHTTPTransport(
                limits=_build_http_limits(),
                http2=True,
                retries=0,
            )
        )
        _client = httpx.AsyncClient(transport=_transport, timeout=_build_http_timeout())
        _debug("[http] shared upstream client re-created (was closed)")
    return _client


# ── VPN / IP rotation (initialized in lifespan) ──────────────────
_vpn_manager = None
_free_ip_pool = None
# ── Geo gate isolation (P2) ──────────────────────────────────
_geo_breaker: dict = {}  # (country, station_id) -> consecutive failures
_geo_breaker_threshold: int = 3
_geo_pin_duration: list = []  # histogram buckets (ms)
_geo_pin_duration_max: int = 200
_geo_pinned_country: str | None = None
_geo_pinned_station = None
_geo_block_total = 0
_geo_forced_pool: set | None = None
# [plan 18/08 §4] Serializes POST /api/vpn-config station_count hot-reloads
# (upscale/downscale) — two interleaved POSTs must never race the registry
# or the pool's set_stations swap.
_apply_station_lock = asyncio.Lock()

# [plan v10 incident 25/08] Le reconcile ne doit tourner qu'UNE fois par
# PROCESS : un redémarrage in-process du server manager (tray stop/start)
# ré-exécute le lifespan, et son reconcile entrait alors en course avec les
# watchdogs/watchers du run précédent — supprimant des conteneurs légitimes
# (vpn-3 rm à 08:18 alors que son tunnel répondait). Les conteneurs d'un
# redémarrage in-process sont les NOTRES : pas des orphelins.
_RECONCILE_DONE_THIS_PROCESS = False


def _sync_station_supervisors(shared_state) -> None:
    """[plan v10 §4 Lot 1] Aligne shared_state.station_supervisors sur
    vpn_managers (crée manquants, conserve l'état isolé des persistantes).
    Fail-soft : jamais bloquer le hot-reload pour une erreur superviseur."""
    if not bool(yaml_get("supervisor", "enabled", True)):
        shared_state.station_supervisors = []
        return
    try:
        from station_supervisor import sync_supervisors as _sync_sup

        shared_state.station_supervisors = _sync_sup(
            list(getattr(shared_state, "station_supervisors", None) or []),
            list(getattr(shared_state, "vpn_managers", None) or []),
        )
    except Exception as e:
        _debug(f"  [vpn] supervisor sync failed (fail-soft): {e}")


async def _apply_station_count(new_n: int) -> None:
    """[plan 18/08 §4] Hot-reload the number of parallel VPN stations.

    GUI dropdown 1-10 → runtime start/stop of compose services, no proxy
    restart. Serialized by ``_apply_station_lock`` (POSTs may interleave).

    Upscale (new_n > old): create + start the missing managers
    (``start()`` = compose up by service name) and append. Downscale:
    ``stop()`` (state-only) then ``stop_container()`` (compose stop +
    ``docker rm -f`` — the retired container is DELETED [fix 19/08],
    its named volume survives, so an upscale recreates it) for each
    retired station, then drop it.

    Both directions converge on: pool.set_stations (fresh swap), watcher
    set_managers (atomic map swap), and ``_persist_vpn_config`` LAST — if
    compose fails mid-way, config.yaml still holds the old count, so the
    next boot is consistent with the pre-change state. Downscale also
    cancels the retired stations' in-flight rotations (cancel + await,
    [plan 18/08 §2.3]) so no rotation lands on a container that
    stop_container is deleting; the worker survives and stale queue
    entries remain no-ops.
    """
    new_n = max(1, min(10, new_n))
    async with _apply_station_lock:
        import shared_state

        # [plan v10 §4 Lot 1] Escape hatch : supervisor.enabled=false → aucun
        # superviseur, chemin legacy intact (rollback du refactor jusqu'au
        # jalon Train 1 vert).
        _sup_enabled = bool(yaml_get("supervisor", "enabled", True))
        managers = list(getattr(shared_state, "vpn_managers", None) or [])
        old_n = len(managers)
        if new_n == old_n:
            # [fix 2→3 idempotent] old==new but a container may be absent
            # (previous upscale failed fail-soft). Converge: create any
            # missing station numbers, restart any enabled but disconnected
            # station without bumping config.
            existing_sids = {m._station for m in managers}
            missing = [k for k in range(1, new_n + 1) if k not in existing_sids]
            if missing:
                from vpn_manager import VPNManager as _VM

                for k in missing:
                    mm = _VM(IP_ROTATION, station=k, shared=shared_state.shared_rotation)
                    mm.enabled = IP_ROTATION.get("enabled", False)
                    managers.append(mm)
                managers.sort(key=lambda m: m._station)
                shared_state.vpn_managers = managers
                _sync_station_supervisors(shared_state)
                pool = getattr(shared_state, "free_ip_pool", None)
                if pool is not None:
                    pool.set_stations(managers)
                watcher = getattr(shared_state, "docker_event_watcher", None)
                if watcher is not None:
                    watcher.set_managers({m._docker_container: m for m in managers})
                shared_state.vpn_manager = managers[0] if managers else None
                shared_state.vpn_manager_2 = managers[1] if len(managers) >= 2 else None
            # ensure enabled stations are started (idempotent)
            to_start = [m for m in managers if m.enabled and m.proxy_mode == "vpn" and m.status != "connected"]
            if to_start:
                try:
                    await asyncio.gather(*(m.start() for m in to_start))
                    # also ensure real managers are actually connected (blocking)
                    _real_connect = [m.connect() for m in to_start if hasattr(m, "_compose_up") and hasattr(m, "connect")]
                    if _real_connect:
                        await asyncio.gather(*_real_connect)
                except Exception as e:
                    _debug(f"  [vpn] idempotent start failed: {e}")
            return
        from vpn_manager import VPNManager

        if new_n > old_n:
            # snapshot for rollback on failure (config must not claim 3 when only 2 run)
            _snapshot = list(managers)
            try:
                for k in range(old_n + 1, new_n + 1):
                    m = VPNManager(IP_ROTATION, station=k, shared=shared_state.shared_rotation)
                    m.enabled = IP_ROTATION.get("enabled", False)
                    managers.append(m)
                # Registry + pool converge BEFORE any docker call: _apply_stack
                # reads the registry for the active set.
                shared_state.vpn_managers = managers
                _sync_station_supervisors(shared_state)
                pool = getattr(shared_state, "free_ip_pool", None)
                if pool is not None:
                    pool.set_stations(managers)
                # [fix 19/08] Sync the .env substitution keys FIRST: without
                # VPN_TYPE_STATION{1..N}, a new station boots on the compose
                # default ${VPN_TYPE_STATIONn:-openvpn} — OpenVPN under a
                # running WireGuard fleet (the 2-new-stations-on-openvpn bug).
                # No-op path writes the env, docker stays untouched.
                if managers:
                    await managers[0]._apply_stack(managers[0]._stack_effective)
                # [fix] Parallélise les compose up — 1→10 en // (gain 5-40s, 1 clic suffit)
                # Use connect() for new stations so the container is actually up (start() only schedules background task)
                if managers[old_n:]:
                    new_managers = managers[old_n:]
                    # start() for all to init watchdog/state, then connect() for new ones to block until healthy
                    await asyncio.gather(*(m.start() for m in new_managers))
                    # Now actually bring up the new tunnels (blocking) — real VPNManager has _compose_up, stub (tests) does not
                    connect_tasks = []
                    for m in new_managers:
                        if not getattr(m, "enabled", True):
                            continue
                        if getattr(m, "proxy_mode", "vpn") != "vpn":
                            continue
                        # real manager has _compose_up / connect, stub does not
                        if hasattr(m, "_compose_up") and hasattr(m, "connect"):
                            connect_tasks.append(m.connect())
                    if connect_tasks:
                        # Fail-soft: a new station may be unhealthy for 120s (WG UDP blocked,
                        # DNS timeout, MTU) — the watchdog will heal it (re-pin, protocol
                        # flip udp->tcp). Hot-reload must not rollback the station_count
                        # because of a transient tunnel health — it would leave config
                        # and runtime desynced and block scaling. Gather with
                        # return_exceptions and log, but keep the new registry.
                        results = await asyncio.gather(*connect_tasks, return_exceptions=True)
                        for _r in results:
                            if isinstance(_r, Exception):
                                _debug(f"  [vpn] upscale connect soft-failed (watchdog will heal): {_r}")
            except Exception as e:
                # rollback to old_n — config stays at old_n, runtime converges
                _debug(f"  [vpn] upscale {old_n}→{new_n} failed, rollback: {e}")
                shared_state.vpn_managers = _snapshot
                _sync_station_supervisors(shared_state)
                pool = getattr(shared_state, "free_ip_pool", None)
                if pool is not None:
                    try:
                        pool.set_stations(_snapshot)
                    except Exception:
                        pass
                watcher = getattr(shared_state, "docker_event_watcher", None)
                if watcher is not None:
                    try:
                        watcher.set_managers({m._docker_container: m for m in _snapshot})
                    except Exception:
                        pass
                shared_state.vpn_manager = _snapshot[0] if _snapshot else None
                shared_state.vpn_manager_2 = _snapshot[1] if len(_snapshot) >= 2 else None
                raise
        else:
            pool = getattr(shared_state, "free_ip_pool", None)
            if pool is not None:
                # [plan 18/08 §2.3] cancel + await (5 s cap) the retired
                # stations' in-flight rotations BEFORE their containers are
                # stopped/removed — a rotation must not land on a container
                # that stop_container is about to delete.
                await pool.cancel_rotations([m._station for m in managers[new_n:]])
            # [fix] Parallélise les stops (même gain en downscale) + garde-fou rm -f
            _retired = managers[new_n:]
            if _retired:
                # [plan v10 §14.1.2 P0] abandon COOPÉRATIF des rotations manager
                # encore en vol (shield connect_next) AVANT stop_container —
                # sinon l'implémentation détachée recréait le conteneur condamné.
                await asyncio.gather(
                    *(m.request_rotation_cancel(cap=5.0) for m in _retired),
                    return_exceptions=True,
                )
                await asyncio.gather(*(m.stop() for m in _retired))
                await asyncio.gather(*(m.stop_container() for m in reversed(_retired)))
            # P2 garde-fou: orphan rm -f fallback (compose stop peut laisser Exited)
            for m in _retired:
                try:
                    import subprocess as _sp

                    # [plan v10 §14.1.19] subprocess.run DIRECT dans ce handler
                    # async gelait l'event loop jusqu'à 10s × N stations — tous
                    # les flux LLM en cours stagnaient pendant le hot-reload.
                    await asyncio.to_thread(
                        _sp.run,
                        ["docker", "rm", "-f", m._docker_container],
                        capture_output=True,
                        timeout=10,
                        creationflags=0x08000000 if __import__("sys").platform == "win32" else 0,
                    )
                except Exception:
                    pass
                _debug(f"  [vpn] orphan garde-fou rm -f {m._docker_container}")
            managers = managers[:new_n]
            shared_state.vpn_managers = managers
            _sync_station_supervisors(shared_state)
            pool = getattr(shared_state, "free_ip_pool", None)
            if pool is not None:
                pool.set_stations(managers)
        watcher = getattr(shared_state, "docker_event_watcher", None)
        if watcher is not None:
            watcher.set_managers({m._docker_container: m for m in managers})
        # Retro-compat aliases converge too
        shared_state.vpn_manager = managers[0]
        shared_state.vpn_manager_2 = managers[1] if new_n >= 2 else None
        # [plan v2 auto-sync] derive AFTER set_stations+watcher, persist LAST
        # Strict: shared_state → pool.set_stations → watcher → derive → persist
        try:
            _eff = effective_free_max_attempts()
            IP_ROTATION["max_free_attempts"] = _eff
        except Exception:
            _eff = max(1, min(int(IP_ROTATION.get("max_free_attempts", 2) or 2), 5))
            IP_ROTATION["max_free_attempts"] = _eff
        from dashboard.api import _persist_vpn_config

        _res = _persist_vpn_config({"station_count": new_n, "max_free_attempts": _eff})
        if asyncio.iscoroutine(_res):
            await _res
        _debug(
            f"  [vpn] station_count hot-reload {old_n} → {new_n} "
            f"({len(managers)} active) effective_max={_eff}"
        )


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
        text = _json_dumps_str(body, indent=2, ensure_ascii=False, default=str)
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
    (
        re.compile(
            r'(?i)("?(?:x-api-key|authorization|cookie|set-cookie)"?\s*[:=]\s*"?)[^"\s,}]{4,}'
        ),
        r"\1***",
    ),
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
            _debug(
                f"  [cache] _evict: evicted {evicted} entries, store_size={len(self._store)}/{self._max_size}"
            )

    def get(self, key: str) -> tuple[bytes, dict] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, body, headers = entry
        if time.monotonic() - ts > self._ttl:
            _debug(
                f"  [cache] get: TTL expired (age={time.monotonic() - ts:.1f}s > ttl={self._ttl}s), evicting key={key[:16]}..."
            )
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
            _debug("  [cache] make_key: stream=True, returning None")
            return None
        # Don't cache requests with tool use (non-deterministic)
        messages = body.get("messages", [])
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        _debug("  [cache] make_key: tool_result found, returning None")
                        return None
        try:
            import hashlib

            if body_bytes:
                # Fast path: hash raw bytes directly (avoids json.dumps + sort_keys)
                key = hashlib.blake2b(body_bytes, digest_size=16).hexdigest()
            else:
                # Fallback: deterministic JSON serialization + blake2b
                key = hashlib.blake2b(
                    _json_dumps_str(body, separators=(",", ":"), default=str).encode(),
                    digest_size=16,
                ).hexdigest()
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

    # Toggle "Use Balance" on for all workspaces (non-bloquant, 6s max)
    try:
        from dashboard import toggle_use_balance_all

        try:
            balance_results = await asyncio.wait_for(toggle_use_balance_all(), timeout=6.0)
        except TimeoutError:
            _debug("  [lifespan] use-balance toggle timeout (6s) — non-bloquant")
            balance_results = {}
        if balance_results:
            ok = sum(1 for v in balance_results.values() if v)
            _debug(
                f"  [lifespan] use-balance toggle: {ok}/{len(balance_results)} workspaces enabled"
            )
    except Exception as e:
        _debug(f"  [lifespan] use-balance toggle failed: {e}")

    # Register recovery callback: unpause API keys when workspace returns to ok
    from dashboard.quota import set_on_workspace_recovered_callback

    set_on_workspace_recovered_callback(on_workspace_recovered)
    _debug("  [lifespan] workspace recovery callback registered")

    # ── Docker Desktop auto-launch (Windows) ───────────────────────────
    # User request: if Docker Desktop is not running after a reboot, the proxy
    # must launch it — otherwise 5 stations stay `disconnected`.
    try:
        from vpn_manager import ensure_docker_running

        ok = await asyncio.wait_for(ensure_docker_running(timeout=60), timeout=65)
        _debug(f"  [lifespan] docker daemon ready={ok}")
    except Exception as e:
        _debug(f"  [lifespan] docker ensure failed (fail-soft): {e}")

    # warm-avalanche: sync déterministe control_api_key → credentials.env avant tout compose up (Q3 C fail-closed)
    try:
        from scripts.make_credentials_env import _sync_control_api_key as _boot_sync_ck

        _boot_sync_ck()
        # fail-closed: si control_enabled mais clé divergente après sync, WARN (boot refuse si fichier manquant)
        try:
            import yaml as _yboot

            _ck_yaml = str((_yboot.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"), encoding="utf-8")) or {}).get("ip_rotation", {}).get("control_api_key") or "").strip()
            _ck_env = ""
            _creds_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "credentials.env")
            if os.path.exists(_creds_path):
                for _ln in open(_creds_path, encoding="utf-8"):
                    if _ln.strip().startswith("VPN_CONTROL_API_KEY="):
                        _ck_env = _ln.strip().split("=", 1)[1].strip()
                        break
            if IP_ROTATION.get("control_enabled", True) and _ck_yaml and _ck_yaml != _ck_env:
                _debug(f"  [lifespan] WARN control 401 risque: config.yaml key != credentials.env ({_ck_yaml[:4]}... vs {_ck_env[:4]}...) — resync forcé")
                _boot_sync_ck()
                # R3-Q2 fail-closed: si toujours divergent après resync, refuse VPN (pas de conteneur stale)
                try:
                    _ck_env2 = ""
                    if os.path.exists(_creds_path):
                        for _ln2 in open(_creds_path, encoding="utf-8"):
                            if _ln2.strip().startswith("VPN_CONTROL_API_KEY="):
                                _ck_env2 = _ln2.strip().split("=", 1)[1].strip()
                                break
                    if _ck_yaml != _ck_env2:
                        _debug("  [lifespan] ERROR fail-closed: clé toujours divergente après resync — VPN non démarré (corriger config.yaml/credentials.env)")
                        IP_ROTATION["_fail_closed"] = True
                except Exception:
                    pass
        except Exception as _e_boot:
            _debug(f"  [lifespan] boot control sync check failed: {_e_boot}")
    except Exception as _e_boot2:
        _debug(f"  [lifespan] boot control sync failed: {_e_boot2}")

    # ── VPN / IP rotation for free models ──
    import shared_state
    from free_ip_pool import FreeIPPool
    from shared_rotation import SharedRotationState
    from vpn_manager import VPNManager

    # Cross-station shared state: recent-IP registry + one absolute identity
    # cursor. Both stations read/write it so neither re-enters an IP the
    # other used recently and their live identities never collide.
    shared_state.shared_rotation = SharedRotationState(IP_ROTATION)
    # [plan 18/08 §4] N stations (GUI dropdown 1-10, hot-reload): boot with
    # resolved_station_count() managers — each owns ONE compose service
    # (vpn-gluetun-N, ports 1079+N/8887+N). Stations 2+ only exist when the
    # resolved count >= N; _apply_station_count() grows/shrinks this set at
    # runtime (start/stop_container) without a proxy restart.
    n = _cfg_settings.resolved_station_count(IP_ROTATION)
    _managers = [
        VPNManager(IP_ROTATION, station=k, shared=shared_state.shared_rotation)
        for k in range(1, n + 1)
    ]
    for m in _managers:
        m.enabled = False if IP_ROTATION.get("_fail_closed") else IP_ROTATION.get("enabled", False)
    if IP_ROTATION.get("_fail_closed"):
        _debug("  [lifespan] fail-closed: VPN désactivé (clé divergente)")
    # warm-avalanche Q7: hetero-boot opt-in (false par défaut) — S1 WG / S2 OV UDP en // au boot
    try:
        if not IP_ROTATION.get("_fail_closed") and IP_ROTATION.get("auto_hetero_boot", False) and len(_managers) >= 2 and IP_ROTATION.get("vpn_stack", "auto") == "auto":
            _wg_present = os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "vpn_configs", "wireguard.env"))
            if _wg_present:
                _managers[0]._stack = "auto"
                _managers[0]._stack_effective = "wireguard"
                _managers[0]._auto_hetero_boot = True
                _managers[1]._stack = "auto"
                _managers[1]._stack_effective = "openvpn"
                _managers[1]._ovpn_protocol = "udp"
                _managers[1]._ovpn_protocol_effective = "udp"
                _managers[1]._ovpn_endpoint_port = "1194"
                _managers[1]._ovpn_endpoint_port_effective = "1194"
                _managers[1]._auto_hetero_boot = True
                _debug("  [lifespan] hetero-boot: S1 WG / S2 OV udp:1194")
    except Exception as _e_hetero:
        _debug(f"  [lifespan] hetero-boot failed: {_e_hetero}")
    # Registry — SOURCE OF TRUTH for hot reload (1-indexed, [0] = station 1).
    # vpn_manager / vpn_manager_2 stay retro-compat aliases for the legacy
    # single/dual reads; all runtime readers use shared_state.vpn_managers.
    shared_state.vpn_managers = _managers
    _sync_station_supervisors(shared_state)
    # [plan v10 §3.6 Lot 3] moteur latence-adaptive : singleton partagé,
    # config canonique `ip_rotation.latency_rotation` (hot-reload via pool).
    try:
        from latency_rotation import get_engine as _get_leng

        shared_state.latency_engine = _get_leng()
        shared_state.latency_engine.update_config(dict(IP_ROTATION.get("latency_rotation") or {}))
    except Exception as _e_leng:
        _debug(f"  [lifespan] latency engine init failed (fail-soft): {_e_leng}")
    shared_state.vpn_manager = _managers[0]
    shared_state.vpn_manager_2 = _managers[1] if n >= 2 else None
    shared_state.free_ip_pool = FreeIPPool(_managers[0], _managers[1] if n >= 2 else None)
    shared_state.free_ip_pool.set_stations(_managers)
    # [plan] E: boot-time fan-out of the ip_rotation timings (connect retry,
    # bad TTL, rotation stagger) — the pool's built-in defaults are the
    # conservative legacy values.
    shared_state.free_ip_pool.update_config(IP_ROTATION)
    global _vpn_manager, _free_ip_pool
    _vpn_manager = _managers[0]
    _free_ip_pool = shared_state.free_ip_pool
    # [plan 18/08 §2.2] Boot reconcile: a crash (or a stack flip that died
    # mid-way) leaves docker containers that do not match the registry —
    # retired stations from a downscale that never got their `docker rm -f`,
    # or containers booted on the stale stack (the 19/08 case). Remove them
    # NOW so start() recreates the fleet exactly as configured; fail-soft
    # (a broken docker daemon must not block boot — start() handles it).
    try:
        global _RECONCILE_DONE_THIS_PROCESS
        from vpn_manager import reconcile_orphan_containers

        if _RECONCILE_DONE_THIS_PROCESS:
            _debug("  [lifespan] boot reconcile skipped (in-process restart — containers are ours)")
        else:
            _removed = await reconcile_orphan_containers(_managers)
            _RECONCILE_DONE_THIS_PROCESS = True
            if _removed:
                _debug(f"  [lifespan] boot reconcile removed: {_removed}")
    except Exception as e:
        _debug(f"  [lifespan] boot reconcile failed (fail-soft): {e}")
    # Start every enabled station in PARALLEL (multi-station would otherwise
    # serialize the cold-start: station N waits for station 1's full
    # compose-up). Each start() is fail-soft internally (docker down/logs a
    # warning) so gather() never raises.
    await asyncio.gather(*(m.start() for m in _managers if m.enabled))
    # [plan] C: docker event watcher — real-time container lifecycle → per-
    # station watchdog wake + SSE vpn_event. Fail-open: if docker is missing
    # or the stream dies, the watchdogs keep their interval pacing and the
    # dashboard its 10 s poll — never breaks a request.
    shared_state.docker_event_watcher = None
    if IP_ROTATION.get("docker_events", True) and _managers:
        from docker_events import DockerEventWatcher

        try:
            _watcher = DockerEventWatcher({m._docker_container: m for m in _managers})
            await _watcher.start()
            shared_state.docker_event_watcher = _watcher
            _debug(
                f"  [lifespan] docker event watcher started ({len(_watcher._managers)} containers)"
            )
        except Exception as e:
            _debug(f"  [lifespan] docker event watcher failed to start: {e}")
            shared_state.docker_event_watcher = None
    # [plan v2 auto-sync] boot derive — mirrors _apply_station_count strict order
    # shared_state → pool.set_stations already done; watcher started; now derive
    try:
        _eff_boot = effective_free_max_attempts()
        IP_ROTATION["max_free_attempts"] = _eff_boot
        _debug(
            f"  [lifespan] effective_max boot derived={_eff_boot} (auto={IP_ROTATION.get('auto_max_free_attempts', True)})"
        )
    except Exception as _e:
        _debug(f"  [lifespan] effective_max boot derive failed: {_e}")
    _debug(
        f"  [lifespan] VPN manager initialized (enabled={_managers[0].enabled}, "
        f"mode={_managers[0]._mode}, station_count={n})"
    )

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

    # [plan 18/08 §4.2] Periodic free-cooldown sweep: each IP rotation
    # leaves a dead (model, IP) key behind; the map must not grow forever.
    async def _periodic_cooldown_sweep():
        while True:
            await asyncio.sleep(yaml_get("background", "cooldown_sweep_interval", 30))
            try:
                _sweep_free_cooldowns()
            except Exception as e:
                _debug(f"  [cooldown] sweep error: {type(e).__name__}: {e}")

    cooldown_sweep_task = asyncio.create_task(_periodic_cooldown_sweep())

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

    async def _db_maintenance_loop():
        """[plan v10 §9.2.8] Dimanche 03h00 local : wal_checkpoint(TRUNCATE)
        + VACUUM (DB cible < 1 Go). Tolérant au busy, jamais fatal.
        Sleep par tranches d'1 h -> annuable proprement à l'arrêt."""

        def _next_run():
            import datetime as _dtm

            now = _dtm.datetime.now()
            days_ahead = (6 - now.weekday()) % 7  # 6 = dimanche
            target = (now + _dtm.timedelta(days=days_ahead)).replace(
                hour=3, minute=0, second=0, microsecond=0
            )
            if target <= now:
                target += _dtm.timedelta(weeks=1)
            return target

        while True:
            target = _next_run()
            import datetime as _dtm2

            while True:
                wait = (target - _dtm2.datetime.now()).total_seconds()
                if wait <= 0:
                    break
                await asyncio.sleep(min(3600.0, wait))
            try:
                t0 = time.monotonic()

                def _maint():
                    with _db_commit_lock:
                        _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                        _conn.execute("VACUUM")
                    return round(os.path.getsize(_db_path) / 1024 / 1024, 1)

                size_mo = await asyncio.to_thread(_maint)
                _debug(
                    f"  [db] maintenance hebdo OK en {time.monotonic() - t0:.1f}s "
                    f"(checkpoint+VACUUM, {_db_path} = {size_mo} Mo)"
                )
            except Exception as e:
                _debug(f"  [db] maintenance hebdo échouée (fail-soft): {type(e).__name__}: {e}")
            await asyncio.sleep(3600)  # re-programme l'année suivante au prochain calcul

    db_maintenance_task = asyncio.create_task(_db_maintenance_loop())

    _debug(
        "  [lifespan] background tasks created (WAL checkpoint, DB flush, key pause cleanup, DB body cleanup, quota fetcher)"
    )

    # [free-discovery] periodic refresh (±10% jitter, backoff after 3 failures)
    app.state._free_discovery_lock = asyncio.Lock()
    app.state._free_discovery_task = None
    app.state._free_refresh_last_minute: dict[str, float] = {}

    async def _free_discovery_loop():
        # Boot is already done synchronously via _ensure_free_models_sync() at import;
        # loop just schedules the next refreshes.
        while True:
            try:
                interval = int(
                    yaml_get(
                        "free_discovery",
                        "interval",
                        yaml_get(
                            "background",
                            "free_models_refresh_interval",
                            _cfg_settings.FREE_DISCOVERY_INTERVAL,
                        ),
                    )
                    or _cfg_settings.FREE_DISCOVERY_INTERVAL
                )
                if interval < 60:
                    interval = 60
                # jitter ±10%
                jitter = 0.9 + random.random() * 0.2
                failures = int(
                    _cfg_settings._FREE_DISCOVERY_STATE.get("consecutive_failures", 0) or 0
                )
                if failures >= 3:
                    sleep_s = min(7200, interval * 2) * jitter
                    _debug(
                        f"  [free-discovery] backoff active failures={failures} sleep={sleep_s:.0f}s"
                    )
                else:
                    sleep_s = interval * jitter
                # expose next_refresh for observability
                try:
                    import datetime as _dt

                    _cfg_settings._FREE_DISCOVERY_STATE["next_refresh"] = (
                        _dt.datetime.now(_dt.UTC) + _dt.timedelta(seconds=sleep_s)
                    ).isoformat()
                except Exception:
                    pass
                await asyncio.sleep(sleep_s)
                if not _cfg_settings.FREE_DISCOVERY_ENABLED:
                    continue
                # singleflight: one fetch at a time
                async with app.state._free_discovery_lock:
                    added = await asyncio.to_thread(_cfg_settings._ensure_free_models_sync)
                    if added:
                        _debug(f"  [free-discovery] loop added {added} new MODELS")
                    # publish event for SSE/dashboard if available
                    try:
                        from dashboard.events import get_event_manager

                        get_event_manager().publish(
                            "free_models_updated",
                            {
                                "detected": _cfg_settings._FREE_DISCOVERY_STATE.get("detected", []),
                                "source": _cfg_settings._FREE_DISCOVERY_STATE.get("source", "none"),
                            },
                        )
                    except Exception:
                        pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                _debug(f"  [free-discovery] loop error: {type(e).__name__}: {e}")
                await asyncio.sleep(60)

    if _cfg_settings.FREE_DISCOVERY_ENABLED:
        app.state._free_discovery_task = asyncio.create_task(_free_discovery_loop())
        _debug("  [lifespan] free-discovery loop started")

    # Ultra-fast DB writer (single writer, batch 32)
    global _db_writer_task
    _db_writer_task = asyncio.create_task(_db_writer_loop())
    _debug("  [lifespan] DB writer loop started (queue batch 32, 50ms)")

    yield

    _debug("  [lifespan] app shutting down")
    # Drain DB queue via writer before flush
    if _db_writer_task:
        _db_writer_task.cancel()
        try:
            await _db_writer_task
        except asyncio.CancelledError:
            pass
        _debug("  [lifespan] DB writer drained")
    # Flush any remaining (fallback path)
    await asyncio.to_thread(_db_flush)
    _debug("  [lifespan] final DB flush done")

    # Cancel background tasks
    checkpoint_task.cancel()
    db_flush_task.cancel()
    key_pause_cleanup_task.cancel()
    cooldown_sweep_task.cancel()
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
    try:
        await cooldown_sweep_task
    except asyncio.CancelledError:
        pass
    # [plan v10 §14.3.17] cancel() sans await = "task destroyed pending" +
    # commit en vol perdu à l'arrêt.
    try:
        await db_cleanup_task
    except asyncio.CancelledError:
        pass
    # [v10 §9.2.8] la tâche de maintenance hebdo aussi (checkpoint+VACUUM)
    try:
        await db_maintenance_task
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

    task = getattr(app.state, "_quota_task", None)
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        _debug("  [lifespan] quota fetcher task cancelled")
    fd_task = getattr(app.state, "_free_discovery_task", None)
    if fd_task:
        fd_task.cancel()
        try:
            await fd_task
        except asyncio.CancelledError:
            pass
        _debug("  [lifespan] free-discovery task cancelled")
    await _client.aclose()
    _debug("  [lifespan] HTTP client closed")
    try:
        await _close_curl_pool()
        _debug("  [lifespan] curl pool closed")
    except Exception as e:
        _debug(f"  [lifespan] curl pool close failed: {e}")
    # Save VPN state (containers stay up — compose-managed). Registry
    # loop: stop() is state-only (never downscales) — reversed order so
    # station 1's state is saved last like before.
    for _m in reversed(getattr(shared_state, "vpn_managers", None) or []):
        if _m is not None:
            await _m.stop()
    _debug(f"  [lifespan] VPN state saved ({len(shared_state.vpn_managers)} stations)")
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
    return JSONResponse(status_code=500, content={"error": "Erreur interne du serveur"})


# Server manager set later in __main__ for GUI mode; None means always-running
_server_manager = None

register_dashboard(
    app,
    STATIC_DIR,
    _conn,
    server_manager_getter=lambda: _server_manager,
    token_usage=_token_usage,
    token_lock=_token_lock,
    db_lock=_db_commit_lock,
)


# ── Rate Limiting (token bucket, per-IP) ────────────────────────

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
        body = _json_dumps({"error": "Limite de débit dépassée. Veuillez réessayer sous peu."})
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


app.add_middleware(RateLimitMiddleware)

# [plan v10 §9.1.5] Limite de taille APPLIQUÉE au niveau ASGI : l'ancien
# contrôle post-lecture (3 handlers) bufferisait d'abord tout le body en
# mémoire. Content-Length > MAX_BODY_SIZE -> 413 immédiat, sans lecture.
# Les bodies chunked sans Content-Length restent gérés par le 413 post-lecture.
from trust import ClientAuthMiddleware  # noqa: E402  # module local, import tardif conventionnel


class _RequestBodyLimitMiddleware:
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


app.add_middleware(_RequestBodyLimitMiddleware, limit_getter=lambda: MAX_BODY_SIZE)


def _build_metrics_text() -> str:
    """[v10 §12.2.7] Exposition Prometheus (format texte, zéro dépendance).

    Sources : moteur §3.6 (EWMA/p95/slow par station·ip), états stations,
    compteurs rotations, cooldowns actifs, mode maintenance.
    Fail-soft : toute source indisponible est sautée."""
    lines = []

    def gauge(name: str, help_txt: str, rows):
        if not rows:
            return
        lines.append(f"# HELP {name} {help_txt}")
        lines.append(f"# TYPE {name} gauge")
        for labels, value in rows:
            lines.append(f"{name}{{{labels}}} {value}")

    try:
        import shared_state as _ss

        eng = getattr(_ss, "latency_engine", None)
        mgrs = list(getattr(_ss, "vpn_managers", None) or [])

        st_rows = []
        for m in mgrs:
            status = str(getattr(m, "status", "") or "")
            st_rows.append((f'station="{m._station}"', 1 if status == "connected" else 0))
        gauge("vpn_station_connected", "1 si la station est connectée", st_rows)

        ewma_rows, p95_rows, slow_rows = [], [], []
        if eng is not None:
            for (_sid, _ip), tr in eng._trackers.items():
                snap = tr.snapshot()
                lbl = f'station="{_sid}",ip="{_ip}"'
                if snap.ewma_ms is not None:
                    ewma_rows.append((lbl, snap.ewma_ms))
                if snap.p95_ms is not None:
                    p95_rows.append((lbl, snap.p95_ms))
                slow_rows.append((lbl, snap.consecutive_slow))
        gauge("vpn_latency_ewma_ms", "EWMA par station·ip (ms)", ewma_rows)
        gauge("vpn_latency_p95_ms", "p95 glissant par station·ip (ms)", p95_rows)
        gauge(
            "vpn_latency_consecutive_slow",
            "requêtes lentes consécutives par station·ip",
            slow_rows,
        )

        if eng is not None:
            gauge(
                "vpn_rotations_total",
                "rotations déclenchées par type",
                [
                    ('kind="soft"', getattr(eng, "total_soft", 0)),
                    ('kind="hard"', getattr(eng, "total_hard", 0)),
                ],
            )
            cds = getattr(eng, "_cooldowns", {})
            now = time.monotonic()
            soft_n = sum(1 for _k, (kind, until) in cds.items() if kind == "soft" and until > now)
            hard_n = sum(1 for _k, (kind, until) in cds.items() if kind == "hard" and until > now)
            gauge(
                "vpn_cooldown_active",
                "cooldowns actifs par kind",
                [('kind="soft"', soft_n), ('kind="hard"', hard_n)],
            )
            gauge(
                "vpn_rotation_paused",
                "mode maintenance actif",
                [('paused="true"' if getattr(eng, "paused", False) else 'paused="false"', 1 if getattr(eng, "paused", False) else 0)],
            )
    except Exception as e:
        _debug(f"  [metrics] build échoué (partiel): {e}")

    return "\n".join(lines) + "\n"


from fastapi import Response as _FastResponse  # noqa: E402


@app.get("/metrics")
async def prometheus_metrics(request: Request):
    """[v10 §12.2.7] Métriques Prometheus. Clé dédiée optionnelle :
    VPN_METRICS_API_KEY (séparée du control plane) ; sinon posture LAN-trust v9."""
    metrics_key = os.environ.get("VPN_METRICS_API_KEY") or ""
    if metrics_key:
        provided = request.headers.get("X-API-Key") or ""
        if not hmac.compare_digest(provided, metrics_key):
            return _openai_error(401, "X-API-Key requis pour /metrics")
    body = _build_metrics_text()
    return _FastResponse(content=body, media_type="text/plain; version=0.0.4")


# [v10 §12.2.7] /metrics Prometheus + ClientAuthMiddleware ci-dessous
app.add_middleware(ClientAuthMiddleware)


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


app.add_middleware(AccessLogMiddleware)


class GeoWarningMiddleware:
    """Inject X-Geo-* warning headers when direct → VPN fallback is active.

    Reads _current_geo ContextVar set by _enforce_geo_gate. Runs as pure ASGI
    so it adds headers to both JSON and streaming responses without buffering.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Capture current geo at request start (may be None for non-geo routes)
        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                try:
                    _g = _current_geo.get()
                    if _g and _g.get("via_vpn"):
                        # Build headers from context (route not available here, use stored allowed)
                        h = {}
                        # Use stored values; also fallback to _geo_headers with dummy route
                        try:
                            _route = {}
                            # try to get allowed from context
                            allowed = _g.get("allowed")
                            if isinstance(allowed, list):
                                h["X-Geo-Allowed"] = ", ".join(allowed)
                            elif isinstance(allowed, str):
                                h["X-Geo-Allowed"] = allowed
                            if _g.get("direct_country"):
                                h["X-Geo-Direct-Country"] = str(_g["direct_country"])
                            if _g.get("direct_ip"):
                                h["X-Geo-Direct-Ip"] = str(_g["direct_ip"])
                            if _g.get("current_country"):
                                h["X-Geo-Current"] = str(_g["current_country"])
                            if _g.get("vpn_ip"):
                                h["X-Geo-Vpn-Ip"] = str(_g["vpn_ip"])
                            if _g.get("station"):
                                h["X-Geo-Station"] = str(_g["station"])
                            if _g.get("model"):
                                h["X-Geo-Model"] = str(_g["model"])
                            h["X-Geo-Warning"] = "direct incompatible — tunneled"
                            h["X-Geo-Pinned"] = "true"
                        except Exception:
                            pass
                        if h:
                            # Merge into existing headers (list of tuples)
                            existing = list(message.get("headers") or [])
                            # headers are bytes pairs; convert new ones
                            for k, v in h.items():
                                existing.append((k.lower().encode(), str(v).encode()))
                            message["headers"] = existing
                except Exception:
                    pass
            await send(message)

        await self.app(scope, receive, send_wrapper)


app.add_middleware(GeoWarningMiddleware)


# ── Traffic Capture (Wireshark-like raw request view) ──────────────
# Outermost middleware: records every client request — raw body bytes,
# headers, timing, tempo, abrupt disconnects (RST) — into a bounded
# ring buffer served by /api/traffic/*. Excludes /static, /health and
# itself. Configurable via the `traffic:` block in config.yaml.

from traffic_capture import TrafficCaptureMiddleware  # noqa: E402  # after middleware setup (app object required)
from traffic_capture import capture as _traffic_capture  # noqa: E402

_traffic_capture.configure(
    enabled=bool(yaml_get("traffic", "enabled", True)),
    max_frames=int(yaml_get("traffic", "max_frames", 500)),
    body_cap=int(yaml_get("traffic", "body_cap", 131072)),
    max_bytes=int(yaml_get("traffic", "max_bytes", 33554432)),
)
app.add_middleware(TrafficCaptureMiddleware, capture=_traffic_capture)


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


def _anthropic_error(status_code: int, message: str, error_type: str = "api_error") -> JSONResponse:
    """Return an error in Anthropic Messages API format."""
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {"type": error_type, "message": message},
        },
    )


def _openai_error(
    status_code: int, message: str, error_type: str = "invalid_request_error"
) -> JSONResponse:
    """Return an error in OpenAI API format."""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {"message": message, "type": error_type, "code": str(status_code)},
        },
    )


def _geo_i18n(key: str, lang: str | None = None) -> str:
    """Tiny i18n for geo warning messages — PROXY_LANG driven (en/fr)."""
    try:
        l = (lang or os.getenv("PROXY_LANG", "en") or "en").lower().strip()[:2]
    except Exception:
        l = "en"
    if l not in ("en", "fr"):
        l = "en"
    msgs = {
        "en": {
            "direct_via_vpn": "You are in Direct mode, but routed via VPN (station {station} {vpnCountry} {vpnIp}) because model {model} requires [{allowed}] and your direct egress {directIp} ({directCountry}) is outside that zone.",
            "direct_via_vpn_unknown": "You are in Direct mode, but routed via VPN (station {station} {vpnCountry} {vpnIp}) because model {model} requires [{allowed}] and your direct IP is unknown.",
            "direct_via_vpn_short": "Direct → VPN (geo fallback)",
        },
        "fr": {
            "direct_via_vpn": "Vous êtes en mode Direct, mais routé via VPN (station {station} {vpnCountry} {vpnIp}) car le modèle {model} exige [{allowed}] et votre IP directe {directIp} ({directCountry}) est hors zone.",
            "direct_via_vpn_unknown": "Vous êtes en mode Direct, mais routé via VPN (station {station} {vpnCountry} {vpnIp}) car le modèle {model} exige [{allowed}] et votre IP directe est indéterminée.",
            "direct_via_vpn_short": "Direct → VPN (repli géo)",
        },
    }
    return msgs.get(l, msgs["en"]).get(key, key)


_geo_tray_last: dict = {}  # model -> monotonic ts throttle
_GEO_TRAY_THROTTLE = 60.0  # sec per model


def _notify_geo_tray_throttled(msg: str, model: str | None = None):
    """Notify system tray (if GUI) + file fallback, throttled per model."""
    try:
        now = time.monotonic()
        key = model or "__global__"
        last = _geo_tray_last.get(key, 0)
        if now - last < _GEO_TRAY_THROTTLE:
            return
        _geo_tray_last[key] = now
        # Try GUI tray if running
        try:
            import gui.tray as _gt

            if hasattr(_gt, "notify_geo"):
                _gt.notify_geo(msg)  # type: ignore
                return
        except Exception:
            pass
        # Fallback: write notification file polled by tray / dashboard SSE
        try:
            import json as _js

            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "geo_notifications.json")
            os.makedirs(os.path.dirname(p), exist_ok=True)
            entry = {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "model": model or "", "message": msg}
            # keep last 20
            try:
                old = []
                if os.path.exists(p):
                    with open(p, encoding="utf-8") as _f:
                        old = _js.load(_f) or []
            except Exception:
                old = []
            old.append(entry)
            old = old[-20:]
            with open(p, "w", encoding="utf-8") as _f:
                _js.dump(old, _f, ensure_ascii=False, indent=2)
            # also publish SSE for dashboard toast
            try:
                from dashboard.events import get_event_manager

                get_event_manager().publish("geo_warning", entry)
            except Exception:
                pass
        except Exception:
            pass
    except Exception:
        pass


def _geo_headers(
    route: dict,
    pinned: bool = False,
    current_country: str | None = None,
    *,
    direct_country: str | None = None,
    direct_ip: str | None = None,
    via_vpn_while_direct: bool = False,
    allowed: list | None = None,
    vpn_ip: str | None = None,
    station: str | None = None,
    model: str | None = None,
) -> dict:
    """Centralized X-Geo-* headers (plan vivid-hinton §2).

    Minimized on success (X-Geo-Status/Mode/Pinned only); detailed
    X-Geo-Allowed/Current only on 403 or on direct→VPN fallback (caller merges).
    Auto-enriches from _current_geo ContextVar when via flag set there.
    """
    # Auto-enrich from ContextVar if caller didn't pass explicit via data
    if not via_vpn_while_direct:
        try:
            _cg = _current_geo.get()
            if _cg and _cg.get("via_vpn"):
                via_vpn_while_direct = True
                direct_country = direct_country or _cg.get("direct_country")
                direct_ip = direct_ip or _cg.get("direct_ip")
                allowed = allowed if allowed is not None else _cg.get("allowed")
                vpn_ip = vpn_ip or _cg.get("vpn_ip")
                station = station or _cg.get("station")
                model = model or _cg.get("model")
                # current_country fallback to context if not passed
                if not current_country:
                    current_country = _cg.get("current_country")
        except Exception:
            pass
    try:
        resolved = _cfg_settings.resolve_geo(route) if isinstance(route, dict) else {}
    except Exception:
        resolved = {}
    mode = str(resolved.get("mode", "strict")) if isinstance(resolved, dict) else "strict"
    status = str(resolved.get("geo_status", "ok")) if isinstance(resolved, dict) else "ok"
    ui_mode = "best_effort" if mode == "prefer" else mode
    h = {
        "X-Geo-Mode": ui_mode,
        "X-Geo-Status": status,
        "X-Geo-Pinned": "true" if pinned else "false",
    }
    if via_vpn_while_direct:
        # Verbose warning headers for direct incompatible case
        h["X-Geo-Warning"] = "direct incompatible — tunneled"
        if direct_country:
            h["X-Geo-Direct-Country"] = str(direct_country)
        if direct_ip:
            h["X-Geo-Direct-Ip"] = str(direct_ip)
        if current_country:
            h["X-Geo-Current"] = str(current_country)
        if vpn_ip:
            h["X-Geo-Vpn-Ip"] = str(vpn_ip)
        if allowed is not None:
            h["X-Geo-Allowed"] = ", ".join(allowed) if isinstance(allowed, list) else str(allowed)
        if station:
            h["X-Geo-Station"] = str(station)
        if model:
            h["X-Geo-Model"] = str(model)
    return h


def _geo_block_response(
    route: dict, current_country: str | None, protocol: str, passthrough_451: bool = False
) -> JSONResponse:
    """403 (Anthropic 400) + error.type=geo_blocked + {status:451} + X-Geo-* + Retry-After:0.

    Compat: never raw 451 unless X-Geo-Passthrough:1 (C2/S2). SSE caller
    should emit event:error {type:geo_blocked status:451} separately.
    """
    try:
        resolved = _cfg_settings.resolve_geo(route) if isinstance(route, dict) else {}
    except Exception:
        resolved = {}
    effective = resolved.get("effective_allowed", set()) if isinstance(resolved, dict) else set()
    allowed_list = sorted(effective) if isinstance(effective, set) else []
    body_allowed = ", ".join(allowed_list) if allowed_list else ""
    mode = str(resolved.get("mode", "strict")) if isinstance(resolved, dict) else "strict"
    ui_mode = "best_effort" if mode == "prefer" else mode
    status = str(resolved.get("geo_status", "ok")) if isinstance(resolved, dict) else "ok"
    headers = {
        "X-Geo-Blocked": "1",
        "X-Geo-Allowed": body_allowed,
        "X-Geo-Current": str(current_country or ""),
        "X-Geo-Mode": ui_mode,
        "X-Geo-Status": status,
        "X-Geo-Pinned": "false",
        "Retry-After": "0",
    }
    code = 451 if passthrough_451 else (400 if protocol == "anthropic" else 403)
    payload_anthropic = {
        "type": "error",
        "error": {
            "type": "geo_blocked",
            "message": f"Geographic restriction: model not available from {current_country or 'current egress'} — allowed: {body_allowed or 'none'}",
        },
        "status": 451,
        "code": "geo_unavailable",
        "allowed": allowed_list,
        "current": current_country,
    }
    payload_openai = {
        "error": {
            "message": payload_anthropic["error"]["message"],
            "type": "geo_blocked",
            "code": "geo_unavailable",
            "status": 451,
            "allowed": allowed_list,
            "current": current_country,
        },
    }
    content = payload_anthropic if protocol == "anthropic" else payload_openai
    # Passthrough 451 natif only if header present (C2)
    if passthrough_451:
        code = 451
    return JSONResponse(status_code=code, content=content, headers=headers)


def _geo_sse_error(route: dict, current_country: str | None) -> bytes:
    """SSE event:error {type:geo_blocked status:451} (does not clobber _sse_keepalive)."""
    try:
        resolved = _cfg_settings.resolve_geo(route) if isinstance(route, dict) else {}
    except Exception:
        resolved = {}
    effective = resolved.get("effective_allowed", set()) if isinstance(resolved, dict) else set()
    allowed_list = sorted(effective) if isinstance(effective, set) else []
    payload = {
        "type": "error",
        "error": {
            "type": "geo_blocked",
            "message": f"Geographic restriction — allowed: {', '.join(allowed_list) or 'none'}",
            "status": 451,
            "code": "geo_unavailable",
            "allowed": allowed_list,
            "current": current_country,
        },
    }
    return _sse("error", payload)


async def _enforce_geo_gate(route: dict, request: Request, *, is_stream: bool, protocol: str):
    """Gate unique fail-closed avant tout forward (I2/I3 adaptatif par modèle).

    Retourne Response de bloc ou None si pass. Centralise les 3 gates
    (/v1/messages, /v1/chat/completions, /v1/responses) + streaming + free.
    Appelé en tête de handler avant circuit-breaker/cache/auth/forward,
    et à chaque tentative free si rotation entre tentatives.
    """
    global _geo_block_total, _geo_forced_pool, _geo_pinned_country
    try:
        _geo_info = _cfg_settings.resolve_geo(route)
    except Exception:
        _geo_info = {
            "effective_allowed": set(),
            "mode": "strict",
            "require_vpn": False,
            "geo_status": "ok",
        }
    _geo_has = isinstance(route, dict) and isinstance(route.get("geo"), dict)
    _geo_enabled = bool(getattr(_cfg_settings, "GEO_ENABLED", False))
    if not (_geo_has and _geo_enabled):
        try:
            _current_geo.set(None)
        except Exception:
            pass
        return None
    _geo_mode = str(_geo_info.get("mode", "strict"))
    _geo_status = str(_geo_info.get("geo_status", "ok"))
    _geo_require = bool(_geo_info.get("require_vpn", False))
    _geo_effective = _geo_info.get("effective_allowed", set())
    _geo_passthrough = request.headers.get("X-Geo-Passthrough", "") == "1"
    if _geo_status == "misconfigured":
        _geo_block_total += 1
        if is_stream:

            async def _geo_err_stream():
                yield _geo_sse_error(route, None)

            return StreamingResponse(
                _geo_err_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Geo-Blocked": "1",
                    "X-Geo-Status": "misconfigured",
                },
            )
        return _geo_block_response(route, None, protocol, passthrough_451=_geo_passthrough)
    _vpn_on = (
        bool(getattr(_cfg_settings.IP_ROTATION, "get", lambda *a, **k: None)("enabled", False))
        if isinstance(getattr(_cfg_settings, "IP_ROTATION", None), dict)
        else False
    )
    try:
        import shared_state as _ss_geo

        _vpn_on = _vpn_on and bool(getattr(_ss_geo, "vpn_managers", None))
    except Exception:
        pass
    if _geo_require and not _vpn_on:
        _geo_block_total += 1
        if is_stream:

            async def _geo_req_stream():
                yield _geo_sse_error(route, None)

            return StreamingResponse(
                _geo_req_stream(),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Geo-Blocked": "1",
                    "X-Geo-Status": _geo_status,
                },
            )
        return _geo_block_response(route, None, protocol, passthrough_451=_geo_passthrough)
    if _geo_mode == "strict" and not _vpn_on and not _geo_require:
        if (
            _geo_effective is not None
            and isinstance(_geo_effective, set)
            and len(_geo_effective) > 0
        ):
            _geo_block_total += 1
            if is_stream:

                async def _geo_novpn_stream():
                    yield _geo_sse_error(route, None)

                return StreamingResponse(
                    _geo_novpn_stream(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Geo-Blocked": "1",
                    },
                )
            return _geo_block_response(route, None, protocol, passthrough_451=_geo_passthrough)
    # [Axe C] Read ALL managers, collect every _current_country, prefer a
    # station already in effective_allowed.  Older code broke at the first
    # manager — with N stations the first one may not be the pinned one.
    _geo_current = None
    _geo_all_countries: list[str] = []
    try:
        import shared_state as _ss_geo2

        for _m in getattr(_ss_geo2, "vpn_managers", None) or []:
            if _m and getattr(_m, "_current_country", None):
                _cc = _m._current_country
                _geo_all_countries.append(_cc)
                if _cc in _geo_effective:
                    _geo_current = _cc  # prefer station already in effective
        if _geo_current is None and _geo_all_countries:
            _geo_current = _geo_all_countries[0]  # single-station compat fallback
    except Exception:
        pass
    # ── Axe A: direct-compatibility check ─────────────────────
    # If allow_direct_when_compatible is True and the residential (direct)
    # IP is already in effective_allowed, the paid forward can go direct
    # without VPN tunnel — skip the entire pin logic below.
    # Otherwise we force VPN but keep direct IP/country for the i18n warning
    # "Direct → VPN (geo fallback)" (IP + pays affichés, tray + badge).
    _direct_country_val: str | None = None
    _direct_ip_val: str | None = None
    _allowed_list = sorted(_geo_effective) if isinstance(_geo_effective, set) else []
    _allowed_str = ", ".join(_allowed_list) if _allowed_list else ""
    if _geo_effective and isinstance(_geo_effective, set) and len(_geo_effective) > 0:
        _allow_direct = bool(getattr(_cfg_settings, "GEO_ALLOW_DIRECT_WHEN_COMPATIBLE", True))
        if _allow_direct:
            try:
                _direct_country_val = await _direct_country()
            except Exception:
                _direct_country_val = "unknown"
            # also fetch direct IP (cached, no extra I/O if country cache hit)
            try:
                _direct_ip_val = _direct_ip_cache.get("ip") or _direct_country_cache.get("ip") or await _get_direct_ip()
            except Exception:
                _direct_ip_val = "unknown"
            if not _direct_ip_val:
                _direct_ip_val = "unknown"
            if not _direct_country_val:
                _direct_country_val = "unknown"
            # store for downstream headers/DB/tray
            request.state._geo_direct_country = _direct_country_val  # type: ignore
            request.state._geo_direct_ip = _direct_ip_val  # type: ignore
            request.state._geo_allowed = _allowed_list  # type: ignore
            request.state._geo_model = route.get("model", "") if isinstance(route, dict) else ""  # type: ignore
            if _direct_country_val and _direct_country_val.lower() != "unknown" and _direct_country_val in _geo_effective:
                request.state._geo_force_tunnel = False  # type: ignore
                request.state._geo_via_vpn_while_direct = False  # type: ignore
                request.state._geo_pinned = False  # type: ignore
                request.state._geo_current = _direct_country_val  # type: ignore
                request.state._geo_mode = _geo_mode  # type: ignore
                request.state._geo_fallback = False  # type: ignore
                # ContextVar for DB/headers/tray
                try:
                    _current_geo.set(
                        {
                            "direct_country": _direct_country_val,
                            "direct_ip": _direct_ip_val,
                            "allowed": _allowed_list,
                            "via_vpn": False,
                            "current_country": _direct_country_val,
                            "model": route.get("model", "") if isinstance(route, dict) else "",
                        }
                    )
                except Exception:
                    pass
                _log(f"  geo: direct IP {_direct_country_val} ({_direct_ip_val}) ∈ [{_allowed_str}] → httpx direct allowed (no tunnel)")
                return None
            # direct incompatible → must tunnel, keep flag for warning
            request.state._geo_force_tunnel = True  # type: ignore
            request.state._geo_via_vpn_while_direct = True  # type: ignore
            # current will be filled after pin (or keep direct as placeholder)
            request.state._geo_current = _direct_country_val  # type: ignore
            try:
                _current_geo.set(
                    {
                        "direct_country": _direct_country_val,
                        "direct_ip": _direct_ip_val,
                        "allowed": _allowed_list,
                        "via_vpn": True,
                        "current_country": _direct_country_val,
                        "model": route.get("model", "") if isinstance(route, dict) else "",
                    }
                )
            except Exception:
                pass
        else:
            # allow_direct disabled → always tunnel, but still capture direct for warning
            try:
                _direct_country_val = await _direct_country()
            except Exception:
                _direct_country_val = "unknown"
            try:
                _direct_ip_val = _direct_ip_cache.get("ip") or _direct_country_cache.get("ip") or await _get_direct_ip()
            except Exception:
                _direct_ip_val = "unknown"
            request.state._geo_direct_country = _direct_country_val  # type: ignore
            request.state._geo_direct_ip = _direct_ip_val or "unknown"  # type: ignore
            request.state._geo_allowed = _allowed_list  # type: ignore
            request.state._geo_model = route.get("model", "") if isinstance(route, dict) else ""  # type: ignore
            request.state._geo_via_vpn_while_direct = True  # type: ignore
            request.state._geo_force_tunnel = True  # type: ignore
            try:
                _current_geo.set(
                    {
                        "direct_country": _direct_country_val,
                        "direct_ip": _direct_ip_val,
                        "allowed": _allowed_list,
                        "via_vpn": True,
                        "current_country": _direct_country_val,
                        "model": route.get("model", "") if isinstance(route, dict) else "",
                    }
                )
            except Exception:
                pass
    if _vpn_on and _geo_effective and isinstance(_geo_effective, set) and len(_geo_effective) > 0:
        if _geo_current and _geo_current in _geo_effective:
            request.state._geo_pinned = True  # type: ignore
            request.state._geo_current = _geo_current  # type: ignore
            request.state._geo_mode = _geo_mode  # type: ignore
            request.state._geo_fallback = False  # type: ignore
            _geo_forced_pool = set(_geo_effective)
            request.state._geo_forced_pool = _geo_forced_pool  # type: ignore
            # If we are here because direct was incompatible, emit warning context
            _via = bool(getattr(request.state, "_geo_via_vpn_while_direct", False))
            if _via:
                # enrich context var with final VPN country
                try:
                    _cur_g = _current_geo.get() or {}
                    _cur_g["current_country"] = _geo_current
                    _cur_g["via_vpn"] = True
                    # try to fetch VPN ip for this country
                    _vpn_ip = None
                    _station_n = None
                    try:
                        import shared_state as _ss_cur

                        for _mm in getattr(_ss_cur, "vpn_managers", None) or []:
                            if _mm and getattr(_mm, "_current_country", None) == _geo_current:
                                _vpn_ip = getattr(_mm, "current_ip", None)
                                _station_n = getattr(_mm, "_station", None)
                                break
                    except Exception:
                        pass
                    if _vpn_ip:
                        _cur_g["vpn_ip"] = _vpn_ip
                    if _station_n:
                        _cur_g["station"] = str(_station_n)
                    _current_geo.set(_cur_g)
                    # i18n log
                    _model = _cur_g.get("model", "") or route.get("model", "") if isinstance(route, dict) else ""
                    _allowed = _cur_g.get("allowed", _allowed_list)
                    _direct_c = _cur_g.get("direct_country", "unknown")
                    _direct_i = _cur_g.get("direct_ip", "unknown")
                    if _direct_c and _direct_c.lower() != "unknown":
                        _msg = _geo_i18n("direct_via_vpn").format(
                            station=_station_n or "?",
                            vpnCountry=_geo_current or "?",
                            vpnIp=_vpn_ip or "?",
                            model=_model or "?",
                            allowed=", ".join(_allowed) if isinstance(_allowed, list) else str(_allowed),
                            directIp=_direct_i or "unknown",
                            directCountry=_direct_c or "unknown",
                        )
                    else:
                        _msg = _geo_i18n("direct_via_vpn_unknown").format(
                            station=_station_n or "?",
                            vpnCountry=_geo_current or "?",
                            vpnIp=_vpn_ip or "?",
                            model=_model or "?",
                            allowed=", ".join(_allowed) if isinstance(_allowed, list) else str(_allowed),
                        )
                    _log(f"  geo fallback: {_msg}")
                    # tray notify (throttled)
                    try:
                        _notify_geo_tray_throttled(_msg, model=_model)
                    except Exception:
                        try:
                            import gui.tray as _gt  # type: ignore
                            if hasattr(_gt, "notify_geo"):
                                _gt.notify_geo(_msg)  # type: ignore
                        except Exception:
                            pass
                except Exception:
                    pass
            return None
        try:
            _probe_budget = (
                float(_cfg_settings.IP_ROTATION.get("ip_probe_budget", 8.0) or 8.0)
                if isinstance(getattr(_cfg_settings, "IP_ROTATION", None), dict)
                else 8.0
            )
        except Exception:
            _probe_budget = 8.0
        _geo_budget = min(8.0, _probe_budget)
        _geo_ok = False
        _geo_candidates = set(_geo_effective)
        try:
            _geo_candidates = {
                c for c in _geo_candidates if _geo_breaker.get((c, 0), 0) < _geo_breaker_threshold
            }
        except Exception:
            pass
        if not _geo_candidates:
            if _geo_mode == "strict":
                _geo_block_total += 1
                if is_stream:

                    async def _geo_no_cand_stream():
                        yield _geo_sse_error(route, _geo_current)

                    return StreamingResponse(
                        _geo_no_cand_stream(),
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control": "no-cache",
                            "Connection": "keep-alive",
                            "X-Geo-Blocked": "1",
                            "Retry-After": "0",
                        },
                    )
                return _geo_block_response(
                    route, _geo_current, protocol, passthrough_451=_geo_passthrough
                )
            request.state._geo_fallback = True  # type: ignore
            request.state._geo_pinned = False  # type: ignore
            request.state._geo_current = _geo_current  # type: ignore
            request.state._geo_mode = _geo_mode  # type: ignore
            return None
        try:
            import shared_state as _ss_pin

            _pin_mgr = None
            for _m in getattr(_ss_pin, "vpn_managers", None) or []:
                if _m and hasattr(_m, "ensure_geo_egress"):
                    _pin_mgr = _m
                    break
            if _pin_mgr is not None:
                _t0 = time.monotonic()
                _geo_ok = await asyncio.wait_for(
                    _pin_mgr.ensure_geo_egress(_geo_candidates, timeout=_geo_budget),
                    timeout=_geo_budget + 1.0,
                )
                _elapsed_ms = int((time.monotonic() - _t0) * 1000)
                _geo_pin_duration.append(_elapsed_ms)
                if len(_geo_pin_duration) > _geo_pin_duration_max:
                    _geo_pin_duration.pop(0)
                if _geo_ok:
                    if len(_geo_candidates) == 1:
                        _geo_current = next(iter(_geo_candidates))
                    # [Axe C] Post-pin verification: the station that will
                    # actually serve the request must have its country in
                    # effective_allowed.  Strict mode must not silently
                    # fallback to prefer — re-pin or block 403.
                    try:
                        import shared_state as _ss_verify

                        _v_station = None
                        if getattr(_ss_verify, "free_ip_pool", None):
                            _v_station = _ss_verify.free_ip_pool._best_station(set(_geo_effective))
                        if _v_station is None:
                            for _vm in getattr(_ss_verify, "vpn_managers", None) or []:
                                if _vm and getattr(_vm, "_current_country", None):
                                    _v_station = _vm
                                    break
                        if _v_station is not None:
                            _v_cc = getattr(_v_station, "_current_country", None)
                            if _v_cc and _v_cc not in _geo_effective:
                                if _geo_mode == "strict":
                                    _geo_block_total += 1
                                    if is_stream:

                                        async def _geo_verify_fail_stream():
                                            yield _geo_sse_error(route, _v_cc)

                                        return StreamingResponse(
                                            _geo_verify_fail_stream(),
                                            media_type="text/event-stream",
                                            headers={
                                                "Cache-Control": "no-cache",
                                                "Connection": "keep-alive",
                                                "X-Geo-Blocked": "1",
                                                "X-Geo-Mode": "strict",
                                                "X-Geo-Pinned": "false",
                                                "Retry-After": "0",
                                            },
                                        )
                                    return _geo_block_response(
                                        route, _v_cc, protocol, passthrough_451=_geo_passthrough
                                    )
                                _geo_ok = False  # prefer: fallback mode
                                request.state._geo_fallback = True  # type: ignore
                            else:
                                _geo_current = _v_cc or _geo_current
                    except Exception:
                        pass
                    _geo_pinned_country = _geo_current
                    _log(
                        f"  [geo] pin verified country={_geo_current} in effective_allowed={sorted(_geo_effective)}"
                    )
                    # If direct was incompatible, enrich context and emit warning/tray
                    try:
                        _via2 = bool(getattr(request.state, "_geo_via_vpn_while_direct", False))
                        if _via2:
                            _cur2 = _current_geo.get() or {}
                            _cur2["current_country"] = _geo_current
                            _cur2["via_vpn"] = True
                            # enrich with vpn ip/station
                            try:
                                import shared_state as _ss_cur2

                                for _mm2 in getattr(_ss_cur2, "vpn_managers", None) or []:
                                    if _mm2 and getattr(_mm2, "_current_country", None) == _geo_current:
                                        _cur2["vpn_ip"] = getattr(_mm2, "current_ip", None) or _cur2.get("vpn_ip")
                                        _cur2["station"] = str(getattr(_mm2, "_station", ""))
                                        break
                                # fallback: best station
                                if "vpn_ip" not in _cur2:
                                    _vst = None
                                    try:
                                        _vst = getattr(_ss_cur2, "free_ip_pool", None) and _ss_cur2.free_ip_pool._best_station(set(_geo_effective))
                                    except Exception:
                                        pass
                                    if _vst:
                                        _cur2["vpn_ip"] = getattr(_vst, "current_ip", None) or getattr(_vst, "pid", "")
                                        _cur2["station"] = str(getattr(_vst, "_station", ""))
                            except Exception:
                                pass
                            _current_geo.set(_cur2)
                            # also keep request.state in sync for response headers
                            request.state._geo_current = _geo_current  # type: ignore
                            # i18n log
                            _model2 = _cur2.get("model", "") or route.get("model", "") if isinstance(route, dict) else ""
                            _allowed2 = _cur2.get("allowed", _allowed_list)
                            _direct_c2 = _cur2.get("direct_country", "unknown")
                            _direct_i2 = _cur2.get("direct_ip", "unknown")
                            _vpn_ip2 = _cur2.get("vpn_ip", "?")
                            _st2 = _cur2.get("station", "?")
                            if _direct_c2 and _direct_c2.lower() != "unknown":
                                _msg2 = _geo_i18n("direct_via_vpn").format(
                                    station=_st2 or "?",
                                    vpnCountry=_geo_current or "?",
                                    vpnIp=_vpn_ip2 or "?",
                                    model=_model2 or "?",
                                    allowed=", ".join(_allowed2) if isinstance(_allowed2, list) else str(_allowed2),
                                    directIp=_direct_i2 or "unknown",
                                    directCountry=_direct_c2 or "unknown",
                                )
                            else:
                                _msg2 = _geo_i18n("direct_via_vpn_unknown").format(
                                    station=_st2 or "?",
                                    vpnCountry=_geo_current or "?",
                                    vpnIp=_vpn_ip2 or "?",
                                    model=_model2 or "?",
                                    allowed=", ".join(_allowed2) if isinstance(_allowed2, list) else str(_allowed2),
                                )
                            _log(f"  geo fallback: {_msg2}")
                            try:
                                _notify_geo_tray_throttled(_msg2, model=_model2)
                            except Exception:
                                try:
                                    import gui.tray as _gt2  # type: ignore

                                    if hasattr(_gt2, "notify_geo"):
                                        _gt2.notify_geo(_msg2)  # type: ignore
                                except Exception:
                                    pass
                    except Exception:
                        pass
                    for c in _geo_candidates:
                        _geo_breaker.pop((c, 0), None)
                else:
                    for c in _geo_candidates:
                        _geo_breaker[(c, 0)] = _geo_breaker.get((c, 0), 0) + 1
            else:
                _geo_ok = False
                for c in _geo_candidates:
                    _geo_breaker[(c, 0)] = _geo_breaker.get((c, 0), 0) + 1
        except TimeoutError:
            for c in _geo_candidates:
                _geo_breaker[(c, 0)] = _geo_breaker.get((c, 0), 0) + 1
        except RuntimeError as e:
            if "503" in str(e) or "saturated" in str(e).lower():
                for c in _geo_candidates:
                    _geo_breaker[(c, 0)] = _geo_breaker.get((c, 0), 0) + 1
                if _geo_mode == "strict":
                    _geo_block_total += 1
                    return JSONResponse(
                        status_code=503,
                        content={
                            "error": {
                                "message": "Geo pin queue saturated",
                                "type": "geo_unavailable",
                            }
                        },
                        headers={"X-Geo-Blocked": "1", "Retry-After": "0"},
                    )
                request.state._geo_fallback = True  # type: ignore
                request.state._geo_pinned = False  # type: ignore
                return None
            for c in _geo_candidates:
                _geo_breaker[(c, 0)] = _geo_breaker.get((c, 0), 0) + 1
        except Exception:
            for c in _geo_candidates:
                _geo_breaker[(c, 0)] = _geo_breaker.get((c, 0), 0) + 1
        if _geo_mode == "strict" and not _geo_ok:
            _geo_block_total += 1
            if is_stream:

                async def _geo_pin_fail_stream():
                    yield _geo_sse_error(route, _geo_current)

                return StreamingResponse(
                    _geo_pin_fail_stream(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Geo-Blocked": "1",
                        "X-Geo-Mode": "strict",
                        "X-Geo-Pinned": "false",
                        "Retry-After": "0",
                    },
                )
            return _geo_block_response(
                route, _geo_current, protocol, passthrough_451=_geo_passthrough
            )
        elif _geo_mode == "prefer" and not _geo_ok:
            request.state._geo_fallback = True  # type: ignore
        else:
            request.state._geo_fallback = False  # type: ignore
        request.state._geo_pinned = bool(_geo_ok)  # type: ignore
        request.state._geo_current = _geo_current  # type: ignore
        request.state._geo_mode = _geo_mode  # type: ignore
        _geo_forced_pool = set(_geo_effective) if _geo_ok else None
        request.state._geo_forced_pool = _geo_forced_pool  # type: ignore
    return None


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
        return (
            "Upstream access denied (403) — model/region may be restricted for "
            "this key. Try again later or check key permissions."
        )
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
from vpn_manager import _UA_BY_IMPERSONATE  # noqa: E402  # after identity docstring (lazy import avoids circular)


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
    mgr = (
        station
        if station is not None
        else (_free_ip_pool.active_station if _free_ip_pool else None) or _vpn_manager
    )
    if mgr:
        # [Axe 3.1] A static SOCKS5 proxy has no docker identity machinery
        # (no current_identity) — the chrome131 default below is correct
        # for it too: the proxy still needs a coherent browser fingerprint.
        identity = getattr(mgr, "current_identity", None)
        if identity:
            return identity
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
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in ("authorization", "x-api-key", "cookie", "x-request-id")
        and not k.lower().startswith("x-stainless-")
    }


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
    """[plan 18/08 §1a / Axe 1.5] Fire-and-forget pool signal for a real
    connection failure on ``station`` (SOCKS5 dead — invisible to the pool,
    which keeps the station "connected").

    [Axe 1.5] No more silent swallow: `except Exception: pass` hid a bug in
    the notification path (bad-mark dispatch, logging) and silently disabled
    the failover for every following request. A failure to NOTIFY is now
    logged with traceback and re-raised at the request level — the caller's
    own except-block already re-raises/falls back safely, so the request
    still never dies, but the defect becomes visible instead of mute.
    Only ``asyncio.CancelledError`` is exempt (a cancelled request must not
    fabricate a connection failure), and a legitimately ABSENT pool (VPN
    off, self-heal mode) stays a no-op — that absence is a valid state, not
    a defect to surface.
    """
    if not _free_ip_pool:
        return
    try:
        _free_ip_pool.notify_connection_failure(station)
    except asyncio.CancelledError:
        raise
    except Exception:
        sid = getattr(station, "_station", "?")
        _log(f"[free-ip] notify_connection_failure raised (station {sid})")
        logging.getLogger(__name__).exception(
            "[free-ip] notify_connection_failure raised (station %s):", sid
        )
        raise


async def _do_free_request_curl_cffi(
    body: dict,
    headers: dict,
    proxy_url: str | None = None,
    station=None,
    endpoint: str | None = None,
):
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
        from curl_cffi.requests import errors as _err
    except ImportError:
        raise RuntimeError("curl_cffi non installé : pip install curl_cffi")

    # Strip paid-account artifacts, stamp the identity (bundle UA wins)
    profile = _current_free_identity(station)
    req_headers = _apply_identity(_free_request_headers(headers), profile, use_curated_ua=False)
    req_headers["Content-Type"] = "application/json"

    # Pooled session — [A1 perf] checkout/checkin SANS verrou pendant le
    # transfert : le POST non-streaming ne sérialise plus la station.
    pool, slot = await _get_pooled_curl_session(proxy_url, profile["impersonate"])
    if endpoint:
        _ep = endpoint
    else:
        _ep = _cfg_settings._free_endpoint_for(body.get("model", ""))
        if (
            not _ep
            or _ep == _cfg_settings.API_BASE_FREE
            or _ep == _cfg_settings.API_BASE_FREE.replace("/chat/completions", "/v1/responses")
        ):
            _ep = API_BASE_FREE
        if not _ep:
            _ep = API_BASE_FREE
            _ep = API_BASE_FREE
        elif "/responses" in _ep and API_BASE_FREE != _cfg_settings.API_BASE_FREE:
            _ep = (
                API_BASE_FREE.replace("/chat/completions", "/responses").replace(
                    "/v1/chat/completions", "/v1/responses"
                )
                if "/chat/completions" in API_BASE_FREE
                else _ep
            )
    try:
        resp = await slot.sess.post(
            _ep,
            json=body,
            headers=req_headers,
            timeout=(10, 600),  # (connect, read) — read 600: long streams
        )
    except asyncio.CancelledError:
        # État transport inconnu → on jette l'emprunt sans await (pas de
        # close dans une tâche annulée) ; le shutdown fermera le reste.
        pool.discard(slot)
        raise
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
        # On pooled session error, evict it so next request gets fresh TLS
        # ([A1] éviction de la seule session fautive, le pool survit).
        await pool.evict(slot)
        raise
    except Exception:
        await pool.evict(slot)
        raise
    # Wrap in a compatible response object
    await pool.checkin(slot)
    return _CurlCffiResponse(resp)


def _free_parallel_should_hedge(body: dict, forced_pool=None) -> bool:
    """True when hedge is enabled, not streaming, and ≥2 candidates."""
    try:
        if body and body.get("stream"):
            return False
        if not _free_ip_pool or not _free_ip_pool.enabled:
            return False
        # pool attributes are hot-reloaded via update_config
        if not getattr(_free_ip_pool, "_free_parallel_enabled", False):
            return False
        if getattr(_free_ip_pool, "_free_parallel_mode", "load-balance") != "hedge":
            return False
        # need at least 2 usable candidates
        cands = _free_ip_pool.pick_candidates(forced_pool)
        return len(cands) >= 2
    except Exception:
        return False


async def _hedged_fetch(cands, body: dict, headers: dict, endpoint: str, forced_pool=None):
    """Hedge N stations: stagger + FIRST_COMPLETED, winner-only compta.

    cands: list of stations sorted by routing preference (pick_candidates).
    Limited to hedge_max+1 to avoid abuse. Stagger = hedge_delay_ms per station.
    Returns (resp, winner_station). Losers are cancelled (CancelledError silent,
    no bad-mark). Only winner increments quota via pool.note_hedge_winner().
    """
    if not cands or len(cands) < 2:
        raise ValueError("hedge needs ≥2 candidates")
    # bound to hedge_max+1 (default 2 fetches)
    try:
        hedge_max = int(getattr(_free_ip_pool, "_free_parallel_hedge_max", 1) or 1)
    except Exception:
        hedge_max = 1
    hedge_max = max(1, min(3, hedge_max))
    max_cands = min(len(cands), hedge_max + 1)
    cands = cands[:max_cands]
    try:
        stagger = float(getattr(_free_ip_pool, "_free_parallel_hedge_delay_ms", 300) or 300) / 1000.0
    except Exception:
        stagger = 0.3
    stagger = max(0.0, min(2.0, stagger))
    # [v10 §12.1.2/Lot 5] hedge_delay PER-MODEL optionnel + jitter ±20 % :
    # les modèles rapides haussent le seuil trop tard sinon.
    try:
        _pmap = getattr(_free_ip_pool, "_free_parallel_hedge_delay_ms_per_model", None)
        _model_name = str(body.get("model") or body.get("original_model") or "")
        if isinstance(_pmap, dict) and _pmap:
            _base_ms = float(
                _pmap.get(_model_name, _pmap.get("default", stagger * 1000.0))
            )
            stagger = max(0.0, min(2.0, _base_ms / 1000.0))
        stagger *= random.uniform(0.8, 1.2)  # jitter ±20 %
    except Exception:
        pass

    # helper to fetch one candidate
    async def _one(station):
        return await _do_free_request_curl_cffi(
            body, headers, station.socks5_url, station=station, endpoint=endpoint
        )

    tasks = {}
    # launch primary immediately
    primary = cands[0]
    t0 = asyncio.create_task(_one(primary))
    tasks[t0] = primary
    # hedge tasks will be launched with stagger if primary not done
    pending_stagger = cands[1:]

    winner_resp = None
    winner_station = None
    winner_task = None
    # [v10 §14.1.5] filet : la première ERREUR HTTP (>=400) est retenue au cas
    # où TOUS les candidats finissent en erreur — mais elle ne gagne JAMAIS
    # contre un <400 encore en vol (l'ancien code faisait gagner un 429 rapide
    # sur un 200 lent → cooldown + fallback paid injustifiés).
    err_resp = None
    err_station = None
    err_task = None
    # overall hedge timeout: generous (connect 10 + read 600, but hedge should not wait forever)
    # we wait until first success; if all fail, raise last error
    last_exc = None
    try:
        # stagger loop: sleep stagger, launch next if primary still pending
        while pending_stagger or tasks:
            # wait for any task with timeout = stagger if still have pending to launch
            timeout = stagger if pending_stagger else None
            done, pending = await asyncio.wait(tasks.keys(), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if done:
                # pick first SUCCESSFUL response (<400) ; une erreur HTTP est
                # mise en filet mais ne clôt pas la course (§14.1.5)
                for d in done:
                    try:
                        resp = d.result()
                    except asyncio.CancelledError:
                        continue
                    except Exception as e:
                        last_exc = e
                        continue
                    if getattr(resp, "status_code", 500) < 400:
                        winner_resp = resp
                        winner_station = tasks.get(d)
                        winner_task = d
                        break
                    if err_resp is None:
                        err_resp, err_station, err_task = resp, tasks.get(d), d
                if winner_resp is not None:
                    break
                # if no winner yet, continue waiting / launching next
                # remove done failed tasks from dict
                for d in list(done):
                    tasks.pop(d, None)
                    # pending set already empty for done
                # if no pending tasks and still have stagger candidates, launch next
                if pending_stagger and not tasks:
                    # all launched so far failed, launch next immediately
                    nxt = pending_stagger.pop(0)
                    nt = asyncio.create_task(_one(nxt))
                    tasks[nt] = nxt
                    continue
            else:
                # timeout: primary not done, launch next hedge
                if pending_stagger:
                    nxt = pending_stagger.pop(0)
                    nt = asyncio.create_task(_one(nxt))
                    tasks[nt] = nxt
                else:
                    # no more to launch, wait indefinitely for remaining
                    if not tasks:
                        break
                    # continue loop with indefinite wait
                    continue
            # also handle tasks dict sync: pending contains tasks not done
            # rebuild tasks dict to keep only pending
            new_tasks = {}
            for t in pending:
                if t in tasks:
                    new_tasks[t] = tasks[t]
            # add any stagger-launched tasks not in pending (just launched)
            for t, st in list(tasks.items()):
                if t not in new_tasks and t not in done:
                    new_tasks[t] = st
            tasks = new_tasks
            if not tasks and not pending_stagger:
                break
        # if winner found, cancel losers
        if winner_resp is not None:
            for t in list(tasks.keys()):
                if t is not winner_task:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
        # no winner <400 : filet = la première erreur HTTP reçue (comportement
        # historique préservé en dernier recours), sinon raise.
        if err_resp is not None:
            winner_resp, winner_station, winner_task = err_resp, err_station, err_task
        if winner_resp is not None:
            # [v10 §14.1.5] la compta hedge ne compte que les VRAIS wins :
            # une réponse >=400 ne consomme pas le quota du perdant-sans-le-savoir.
            if getattr(winner_resp, "status_code", 500) < 400:
                try:
                    if _free_ip_pool and hasattr(_free_ip_pool, "note_hedge_winner"):
                        _free_ip_pool.note_hedge_winner(winner_station, primary=primary)
                except Exception:
                    pass
            # need to ensure free_ip / identity context for downstream logging
            try:
                ip = getattr(winner_station, "current_ip", None) or getattr(winner_station, "pid", "") or ""
                prof = _current_free_identity(winner_station)
                _current_free_attempt.set({"ip": ip, "identity": prof.get("impersonate") or "", "station": winner_station})
            except Exception:
                pass
            return winner_resp, winner_station
        # no winner: all failed
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("échec de tous les candidats hedge")
    finally:
        # cleanup any remaining tasks on exception path
        for t in list(tasks.keys()):
            if t is not winner_task and not t.done():
                t.cancel()
                try:
                    await t
                except Exception:
                    pass


def _curl_proxy_url(proxy_url: str | None) -> str | None:
    """Fix curl_cffi (libcurl) SOCKS5 routing on Windows.

    With ``socks5://`` libcurl resolves the hostname locally, which times out
    against gluetun on Windows Docker Desktop (observed: 60 s curl 28).
    ``socks5h://`` resolves the hostname inside the tunnel (gluetun DNS), which
    answers in <1 s. httpx is untouched — it never sees this conversion.
    """
    if proxy_url and proxy_url.startswith("socks5://"):
        return "socks5h://" + proxy_url[len("socks5://") :]
    return proxy_url


class _CurlCffiResponse:
    """Wrapper to make curl_cffi response compatible with httpx response interface."""

    def __init__(self, resp):
        self._resp = resp
        self.status_code = resp.status_code
        self.headers = dict(resp.headers)

    @property
    def text(self) -> str:
        if hasattr(self, "_text_override"):
            return self._text_override
        if isinstance(self._resp.content, bytes):
            return self._resp.content.decode("utf-8", errors="replace")
        return str(self._resp.content)

    @text.setter
    def text(self, value: str):
        self._text_override = value

    @property
    def content(self) -> bytes:
        if hasattr(self, "_content_override"):
            return self._content_override
        return self._resp.content

    @content.setter
    def content(self, value: bytes):
        self._content_override = value

    def json(self):
        # [E3 perf] parse direct des bytes — évite le détour str(bytes)
        return _json_loads(self.content)


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
        # [v10 §3.6.1 Lot 5] TTFB streaming : contexte (station, t0, model)
        # posé par _open_free_stream ; la PREMIÈRE lecture du consommateur
        # déclenche le record moteur (warm-up/anti-flap inclus).
        self._ttfb_ctx = None
        self._ttfb_recorded = False

    def _record_ttfb_once(self):
        if self._ttfb_recorded or not self._ttfb_ctx:
            return
        self._ttfb_recorded = True
        try:
            import shared_state as _ss_lat

            eng = getattr(_ss_lat, "latency_engine", None)
            if eng is None:
                from latency_rotation import get_engine as _get_eng

                eng = _get_eng()
            if eng.cfg.stream_metric != "ttfb":
                return  # mode total : mesuré en fin de stream par les callers
            station, t0, model = self._ttfb_ctx
            ttfb_ms = (time.monotonic() - t0) * 1000.0
            sid = int(getattr(station, "_station", 0) or 0)
            ip = str((_current_free_attempt.get() or {}).get("ip") or "")
            eng.record_request(sid, ip, ttfb_ms, model, self.status_code)
        except Exception:
            pass

    async def aiter_lines(self):
        """Yield SSE lines as str, split manually from raw bytes ([41]).

        curl_cffi's own aiter_lines() yields bytes (its internal
        splitting also raises TypeError "startswith first arg must
        be bytes" in 0.14.0), while consumers expect httpx-style str
        lines — so iterate aiter_content() and split on \n here,
        buffering a partial line across chunks.
        """
        self._record_ttfb_once()
        buf = b""
        async for chunk in self._resp.aiter_content():
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.rstrip(b"\r").decode("utf-8", errors="replace")
        if buf:  # trailing line without final newline
            yield buf.rstrip(b"\r").decode("utf-8", errors="replace")

    async def aiter_bytes(self):
        self._record_ttfb_once()
        async for chunk in self._resp.aiter_content():
            yield chunk

    async def aread(self):
        self._record_ttfb_once()
        return await self._resp.acontent()

    async def aclose(self):
        await self._resp.aclose()


class _FreeTunnelFailure(Exception):
    """[plan 19/08 §2] typed tunnel failure re-raised by _open_free_stream.

    Free streams + station-first mode + remaining free budget: the caller's
    retry loop MUST NOT see the exception as a paid-path failure — it must
    try another station. The CM re-raises this instead of falling through to
    the direct residential fallback, and each stream loop catches it via a
    dedicated `except _FreeTunnelFailure:` clause placed BETWEEN
    asyncio.CancelledError and the broad except Exception (a broad clause
    first would route the failure into paid alt-key retries — the leak this
    type exists to prevent).
    """

    def __init__(self, station, cause):
        super().__init__(
            f"free tunnel failure on station {getattr(station, '_station', '?')}: {cause}"
        )
        self.station = station
        self.cause = cause


def effective_free_max_attempts(forced_pool=None) -> int:
    """[plan v2] effective free attempts — single source of truth.

    Auto mode (``auto_max_free_attempts`` true, default): derived from the
    number of *usable* egresses — ``len(_socks5_enabled_eps())`` in socks5
    mode, else ``resolved_station_count`` filtered by ``forced_pool`` when
    present (geo). Clamped to [1, 3]; direct is never counted.

    Manual mode (auto false): respects ``max_free_attempts`` clamped to
    [1, 3] + WARN if it exceeds the usable count (operator asked for more
    attempts than stations exist).
    """
    # Manual override — respect the stored value, but warn when incoherent
    if not IP_ROTATION.get("auto_max_free_attempts", True):
        try:
            v = int(IP_ROTATION.get("max_free_attempts", 2) or 2)
        except (TypeError, ValueError):
            v = 2
        manual = max(1, min(v, 5))
        # WARN when manual exceeds usable (only when we can compute usable)
        try:
            if _free_ip_pool is not None and getattr(_free_ip_pool, "socks5_mode", False):
                usable = len(_free_ip_pool._socks5_enabled_eps())
            else:
                usable = _cfg_settings.resolved_station_count(IP_ROTATION)
                if forced_pool is not None and _free_ip_pool is not None:
                    # count stations whose country is in forced_pool (via
                    # the same predicate _station_usable uses for geo)
                    cnt = 0
                    for _st in getattr(_free_ip_pool, "_stations", []) or []:
                        if _free_ip_pool._station_usable(
                            _st, exclude_approaching=False, forced_pool=forced_pool
                        ):
                            cnt += 1
                    # only narrow, never widen (None-country stations stay usable)
                    if cnt < usable:
                        usable = cnt
                usable = max(1, min(int(usable or 1), 5))
            if manual > usable:
                _debug(
                    f"  [free] WARN manual max_free_attempts={manual} > usable={usable} (auto=false) — clamping effective to {usable} would waste retries"
                )
        except Exception:
            pass
        return manual

    # Auto mode — derive from usable egresses
    try:
        if _free_ip_pool is not None and getattr(_free_ip_pool, "socks5_mode", False):
            # SOCKS5: number of enabled proxies (capped 3); forced_pool has
            # no country semantic for static proxies so it is ignored here
            try:
                n = len(_free_ip_pool._socks5_enabled_eps())
            except Exception:
                n = _cfg_settings.resolved_station_count(IP_ROTATION)
        else:
            n = _cfg_settings.resolved_station_count(IP_ROTATION)
            if forced_pool is not None and _free_ip_pool is not None:
                cnt = 0
                for _st in getattr(_free_ip_pool, "_stations", []) or []:
                    if _free_ip_pool._station_usable(
                        _st, exclude_approaching=False, forced_pool=forced_pool
                    ):
                        cnt += 1
                if cnt < n:
                    n = cnt
                # if pool has no stations yet (boot), keep resolved count
                if cnt == 0 and n == 0:
                    n = _cfg_settings.resolved_station_count(IP_ROTATION)
        return max(1, min(int(n or 1), 5))
    except Exception:
        try:
            return max(1, min(int(_cfg_settings.resolved_station_count(IP_ROTATION) or 1), 5))
        except Exception:
            return 2


def _free_max_attempts() -> int:
    """[plan 19/08 §1] alias — kept for compat, delegates to effective helper."""
    return effective_free_max_attempts()


def _free_exception_fallback_mode() -> str:
    """[plan 19/08 §2] tunnel-failure strategy (GUI: free_exception_fallback).

    "station-first" → a dead tunnel retries another station before the
    direct residential fallback; "direct" → legacy immediate direct
    fallback. Hot-reloaded per request like max_free_attempts.
    """
    mode = IP_ROTATION.get("free_exception_fallback", "station-first") or "station-first"
    return mode if mode in ("station-first", "direct") else "station-first"


def _free_attempts_active(forced_pool=None) -> bool:
    """Free re-strike budget is active (>1 attempt or station-first)."""
    return (
        effective_free_max_attempts(forced_pool) > 1
        or _free_exception_fallback_mode() == "station-first"
    )


@asynccontextmanager
async def _open_free_stream(
    endpoint,
    body,
    headers,
    use_free: bool,
    count_request: bool = True,
    fresh_station: bool = False,
    direct_fallback: bool = True,
    forced_pool=None,
):
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

    direct_fallback=False ([plan 19/08 §2] station-first mode): a tunnel
    exception RE-RAISES as _FreeTunnelFailure instead of falling through to
    the direct residential fallback — the caller's retry loop then tries
    another station (fresh IP = fresh quota). True = legacy behavior (the
    direct fallback is the existing semantics).
    """
    _debug(
        f"  [free-stream] _open_free_stream called: use_free={use_free} pool={_free_ip_pool is not None} pool_enabled={_free_ip_pool.enabled if _free_ip_pool else False}"
    )
    if use_free and _free_ip_pool and _free_ip_pool.enabled:
        _debug("  [free-stream] getting proxy from pool...")
        if count_request:
            try:
                proxy_url, station = await _free_ip_pool.on_request(forced_pool)
            except TypeError:
                proxy_url, station = await _free_ip_pool.on_request()
        elif fresh_station:
            _prev = _current_free_attempt.get() or {}
            try:
                proxy_url, station = await _free_ip_pool.on_disconnect_retry(
                    _prev.get("station"), forced_pool
                )
            except TypeError:
                proxy_url, station = await _free_ip_pool.on_disconnect_retry(_prev.get("station"))
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
        _debug(f"  [free-stream] pool result: proxy_url={proxy_url is not None}")
        if not proxy_url:
            _debug(
                "  [free-stream] ⚠️ no VPN proxy — free endpoint is geo-restricted, direct connection will likely 400"
            )
        if proxy_url:
            try:

                station = station or (_free_ip_pool.active_station if proxy_url else None)
                profile = _current_free_identity(station)
                _current_free_attempt.set(
                    {
                        "ip": _free_usage_ip(station),
                        "identity": profile.get("impersonate") or "",
                        "station": station,
                        "proxy_url": proxy_url,
                    }
                )
                req_headers = _apply_identity(
                    _free_request_headers(headers), profile, use_curated_ua=False
                )
                req_headers["Content-Type"] = "application/json"
                # [plan 18/08 §1a/am.21] connect timeout 30 → 10: in mono-station
                # the request IS the arming signal (bad-mark is C1-forbidden) —
                # the first failure reaches the manager in ≤10-15 s, not 30. Read
                # 600 unchanged (long streams). SOCKS5+TLS handshake ≈ 1-2 s on a
                # healthy tunnel — no legitimate request is impacted.
                _debug(
                    f"  [free-stream] creating curl_cffi session proxy={_curl_proxy_url(proxy_url)} (pooled)"
                )
                pool2, slot2 = await _get_pooled_curl_session(proxy_url, profile["impersonate"])
                # [A1 perf] emprunt SANS verrou pendant le POST : seuls les
                # headers transitent sous l'emprunt, le corps est consommé
                # après restitution — sémantique inchangée, parallélisme OK.
                try:
                    _debug(f"  [free-stream] posting to {endpoint} (pooled)")
                    _t0_ttfb = time.monotonic()
                    resp = await slot2.sess.post(
                        endpoint, json=body, headers=req_headers, stream=True
                    )
                except asyncio.CancelledError:
                    pool2.discard(slot2)
                    raise
                except BaseException:
                    _evict_later(pool2, slot2)
                    raise
                await pool2.checkin(slot2)
                _debug(f"  [free-stream] response status={resp.status_code}")
                wrapped = _CurlCffiStreamResponse(resp)
                # [v10 §3.6.1 Lot 5] TTFB streaming : la 1ʳᵉ lecture du
                # consommateur recordera la mesure au moteur.
                wrapped._ttfb_ctx = (
                    station,
                    _t0_ttfb,
                    str(body.get("model") or body.get("original_model") or ""),
                )
                # [plan 18/08 §am.22] register the in-flight stream so the
                # watchdog can cancel it the moment egress death is CONFIRMED
                # (egress_dead) — a client reading a dead tunnel must get the
                # error in ≤ ~10 s, not after the up-to-600 s read timeout.
                # asyncio.current_task() IS the request task (no task spawned
                # here). Unregistered in the finally below. The registry is
                # OPTIONAL pool protocol — a double without it (invariant
                # tests) must not break the request path: hasattr guard.
                if _free_ip_pool is not None and hasattr(_free_ip_pool, "register_stream"):
                    _free_ip_pool.register_stream(station, asyncio.current_task())
                try:
                    yield wrapped
                finally:
                    if _free_ip_pool is not None and hasattr(_free_ip_pool, "unregister_stream"):
                        _free_ip_pool.unregister_stream(station, asyncio.current_task())
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
                    # pooled session stays open (don't close)
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
                    # [fix 20/08][Axe 1.5] A notify-path failure must never
                    # pre-empt the station-first retry ordering below: Axe 1.5
                    # makes _signal_connection_failure log-and-RE-RAISE (with
                    # full traceback, inside itself), which would otherwise
                    # escape this generator untyped and bypass the caller's
                    # `except _FreeTunnelFailure` fresh-station retry (plan
                    # 19/08 §2). Guard the signal: the defect is already
                    # logged with traceback inside — swallow it here and
                    # continue to the raise below either way. The bad-mark
                    # usually already landed, so later requests stay
                    # protected and THIS request still fails over cleanly.
                    # (asyncio.CancelledError is BaseException-derived and
                    # passes through this guard untouched.)
                    try:
                        _signal_connection_failure(station)
                    except Exception:
                        _log(
                            "[free-ip] notify-step raised at the call site "
                            "(traceback above) — swallowed to preserve the "
                            "station-first retry"
                        )
                if not direct_fallback:
                    # [plan 19/08 §2] station-first mode with remaining free
                    # budget → re-raise so the caller's retry loop strikes
                    # ANOTHER station (fresh IP = fresh quota) before the
                    # residential-IP direct fallback is ever considered.
                    raise _FreeTunnelFailure(station, e) from e
                _debug(
                    f"  [stream] curl_cffi proxy stream failed: {e}, falling back to direct stream"
                )
                _log(
                    f"  FREE STREAM via VPN tunnel FAILED ({e}) → direct fallback (residential IP)"
                )
    if use_free:
        # Direct fallback to the free endpoint: never forward the paid key,
        # cookies or SDK identifiers (invariant A.0), stamp the identity UA.
        profile = _current_free_identity()
        # Preserve the station that just failed (the tunnel branch above set
        # it in the ContextVar before the accident): a disconnect retry must
        # switch AWAY from it on_disconnect_retry, not re-strike the same IP.
        _prev_attempt = _current_free_attempt.get() or {}
        _current_free_attempt.set(
            {
                "ip": _free_usage_ip(),
                "identity": profile.get("impersonate") or "",
                "station": _prev_attempt.get("station"),
                "proxy_url": None,
            }
        )
        free_headers = _apply_identity(_free_request_headers(headers), profile, use_curated_ua=True)
        free_headers["Content-Type"] = "application/json"
        async with _ensure_http_client().stream(
            "POST", endpoint, json=body, headers=free_headers
        ) as resp:
            yield resp
        return
    # [Axe A] Paid request with forced tunnel: route through VPN pool when
    # geo gate set _geo_force_tunnel=True (direct IP not in allowed set).
    # Reuses _open_via_pool which selects a station from forced_pool via
    # _free_ip_pool.on_request() and streams through curl_cffi + SOCKS5.
    if forced_pool and _free_ip_pool and _free_ip_pool.enabled:
        async with _open_via_pool(
            endpoint, body, headers, is_stream=True, forced_pool=forced_pool
        ) as resp:
            yield resp
        return
    async with _ensure_http_client().stream("POST", endpoint, json=body, headers=headers) as resp:
        yield resp


@asynccontextmanager
async def _open_via_pool(endpoint, body, headers, *, is_stream=False, forced_pool=None):
    """Context manager: paid geo-forwarding via SOCKS5 tunnel station.

    Used when _enforce_geo_gate sets _geo_force_tunnel=True — the paid
    request must route through a VPN station in effective_allowed, not
    via httpx direct.  Uses curl_cffi with SOCKS5 proxy (same transport
    as free streams) but keeps paid-request headers (no free-request
    transformation).  Falls back to vpn_manager.socks5_url if pool is
    unavailable.  Raises UpstreamError on connection/timeout failures.
    """
    # 1) Get station from pool, or fall back to vpn_manager
    # When forced_pool is set (geo fallback), we must pick a station
    # IGNORING proxy_mode=direct — direct mode still tunnels geo-restricted
    # models. Use _best_station directly (proxy_mode agnostic) before
    # falling back to on_request / single manager.
    proxy_url = None
    station = None
    if forced_pool is not None and _free_ip_pool is not None:
        try:
            # Geo path: pick best station whose country ∈ forced_pool, regardless of proxy_mode
            st = _free_ip_pool._best_station(forced_pool)
            if st is not None and getattr(st, "socks5_url", None):
                # ensure still usable (status check already in _best_station)
                proxy_url = st.socks5_url
                station = st
        except Exception:
            pass
    if not proxy_url and _free_ip_pool is not None:
        try:
            proxy_url, station = await _free_ip_pool.on_request(forced_pool)
        except Exception:
            pass
    if not proxy_url and _vpn_manager and getattr(_vpn_manager, "socks5_url", None):
        proxy_url = _vpn_manager.socks5_url
        station = _vpn_manager
    # Axe B: tag station with geo constraint so background rotations
    # (on_quota_exhausted / on_disconnect_retry) stay in effective_allowed
    if station and forced_pool:
        station._geo_forced_pool = forced_pool
    if not proxy_url:
        raise UpstreamError(
            "No VPN station available for geo-restricted request",
            status_code=503,
        )

    # 2) Build headers: apply identity profile for impersonation
    profile = _current_free_identity(station)
    req_headers = _apply_identity(dict(headers), profile, use_curated_ua=False)
    req_headers["Content-Type"] = "application/json"

    # 3) curl_cffi through SOCKS5 tunnel — POOL partagé avec le chemin free.
    # [v10 PLAN-commun 1.1] fini la AsyncSession jetable (handshake SOCKS5+TLS
    # par requête, ~300-1000 ms via tunnel). [A1 perf] checkout/checkin sans
    # verrou pendant le transfert : M sessions par (proxy, impersonate), le
    # POST geo non-streaming ne sérialise plus la station.
    resp = None
    pool = None
    slot = None
    try:
        pool, slot = await _get_pooled_curl_session(
            proxy_url, profile.get("impersonate", "chrome131")
        )
        try:
            raw = await slot.sess.post(
                endpoint, json=body, headers=req_headers, stream=True
            )
        except asyncio.CancelledError:
            pool.discard(slot)
            raise
        except BaseException:
            _evict_later(pool, slot)
            raise
        await pool.checkin(slot)
        resp = (
            _CurlCffiStreamResponse(raw)
            if is_stream
            else _CurlCffiResponse(raw)
        )
        yield resp
    except UpstreamError:
        raise
    except Exception as e:
        _signal_connection_failure(station)
        raise UpstreamError(
            f"Geo tunnel request failed: {e}",
            status_code=502,
            original=e,
        ) from e
    finally:
        if resp is not None:
            try:
                await resp.aclose()
            except Exception:
                pass


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
    expect_tunnel = bool(
        _vpn_manager and _vpn_manager.current_ip and getattr(_vpn_manager, "socks5_url", None)
    )
    now = time.monotonic()
    cached = _public_ip_cache
    if cached["ip"] and now - cached["ts"] < 60 and cached["via_tunnel"] == expect_tunnel:
        return cached["ip"]
    try:
        import httpx

        proxy = _vpn_manager.socks5_url if expect_tunnel else None
        async with httpx.AsyncClient(timeout=10 if expect_tunnel else 5, proxy=proxy) as client:
            resp = await client.get("https://api.ipify.org")
            ip = resp.text.strip()
    except Exception:
        # Serve the stale value only when it matches the current context.
        return cached["ip"] if cached["via_tunnel"] == expect_tunnel else "unknown"
    if ip:
        cached.update(ip=ip, ts=now, via_tunnel=expect_tunnel)
        return ip
    return cached["ip"] if cached["via_tunnel"] == expect_tunnel else "unknown"


_direct_country_cache = {"country": "", "ts": 0.0, "ip": ""}


async def _get_direct_ip() -> str:
    """Get the direct (non-tunnel) public IP. Always probes direct,
    bypassing any active VPN tunnel — used by _direct_country() to
    resolve the residential egress IP regardless of VPN state.
    """
    import httpx as _httpx_ip

    try:
        async with _httpx_ip.AsyncClient(timeout=5) as _client:
            resp = await _client.get("https://api.ipify.org")
            ip = resp.text.strip()
            if ip:
                return ip
    except Exception:
        pass
    return "unknown"


_direct_ip_cache = {"ip": "", "ts": 0.0}


async def _direct_country() -> str:
    """Resolve the direct (non-tunnel) egress country, cached 10 min.

    Calls _get_direct_ip (bypasses tunnel), then GeoIP lookup via
    ip-api.com with 5 s timeout. Normalizes via _vpn_normalize_country.
    Returns "unknown" on any failure — treated as NOT authorized by callers.

    Perf: cache-first — if country cached <600s ago, return instantly
    without any network I/O (saves 2 HTTP round-trips per geo-gated request).
    IP itself is cached 60s separately.
    """
    now = time.monotonic()
    cached = _direct_country_cache
    # Fast path: country still fresh → no network at all
    if cached["country"] and cached["ip"] and now - cached["ts"] < 600:
        return cached["country"]
    # IP cache 60s to avoid hammering ipify on every geo check
    ip_cached = _direct_ip_cache
    if ip_cached["ip"] and now - ip_cached["ts"] < 60:
        current_ip = ip_cached["ip"]
    else:
        try:
            current_ip = await _get_direct_ip()
        except Exception:
            current_ip = "unknown"
        if current_ip != "unknown" and current_ip:
            ip_cached["ip"] = current_ip
            ip_cached["ts"] = now
    if current_ip == "unknown" or not current_ip:
        return "unknown"
    if cached["ip"] == current_ip and cached["country"] and now - cached["ts"] < 600:
        return cached["country"]
    country = "unknown"
    try:
        import httpx as _httpx_geo

        # Direct lookup — never via tunnel (we want the residential egress)
        async with _httpx_geo.AsyncClient(timeout=5) as _client:
            # ip-api.com line format: country is plain text field
            resp = await _client.get(f"http://ip-api.com/line/{current_ip}?fields=country")
            raw = resp.text.strip()
            if raw and raw.lower() not in ("fail", "unknown"):
                country = _vpn_normalize_country(raw)
            else:
                # Fallback: ipinfo
                try:
                    r2 = await _client.get(f"https://ipinfo.io/{current_ip}/country", timeout=5)
                    raw2 = r2.text.strip()
                    if raw2 and len(raw2) <= 4:
                        # ipinfo returns 2-letter code — map via aliases if needed
                        country = _vpn_normalize_country(raw2)
                    elif raw2:
                        country = _vpn_normalize_country(raw2)
                except Exception:
                    pass
    except Exception:
        country = "unknown"
    if country and country.lower() != "unknown":
        cached.update(country=country, ts=now, ip=current_ip)
        return country
    return "unknown"


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
    if getattr(vpn, "pid", None):
        # [Axe 3.1] Static SOCKS5 proxy: no docker IP to key on — the
        # proxy's identity IS its host:port. Each proxy gets its own
        # bucket: a 429 on one must never cooldown the others, which
        # egress separate IPs.
        return f"{free_model}|socks5:{vpn.pid}"
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
                parsed = parsed.replace(tzinfo=datetime.UTC)  # HTTP-date is GMT
            v = (parsed - datetime.datetime.now(datetime.UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return _FREE_429_DEFAULT
    if 0 < v <= _FREE_COOLDOWN_MAX:
        return v
    return _FREE_429_DEFAULT


def _sweep_free_cooldowns() -> int:
    """[plan 18/08 §4.2] Drop expired (model, IP) cooldown entries.

    Without a periodic sweep the map grows without bound: each rotation
    leaves the previous IP's key behind (expiry is only cleaned lazily on
    that exact key's next lookup). Run on the lifespan background tick —
    the len()>32 guard in _set_free_cooldown remains a soft-limit for
    transient bursts between ticks.
    """
    now = time.monotonic()
    expired = [k for k, t in _free_model_cooldowns.items() if t <= now]
    for k in expired:
        del _free_model_cooldowns[k]
    if expired:
        _debug(f"  [cooldown] swept {len(expired)} expired free-cooldown entries")
    return len(expired)


def _set_free_cooldown(free_model: str, seconds: float, station=None) -> None:
    key = _free_cooldown_key(free_model, station)
    expiry = time.monotonic() + seconds
    # soft-limit ([plan 18/08 §4.2]): the periodic _sweep_free_cooldowns is
    # the real memory bound; this only avoids a burst between two ticks.
    if len(_free_model_cooldowns) > 32:
        _sweep_free_cooldowns()
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


def _is_watchdog_cancelled(station) -> bool:
    """[plan 18/08 §am.22/piège 19] was THIS task cancelled by the
    egress-death watchdog (pool.cancel_streams on a confirmed-dead tunnel)?

    The stream registry is OPTIONAL pool protocol: a pool double without it
    can never classify (False) — the request path must not depend on it.
    Sync and pure (id-marker lookups), safe to call mid-handler.
    """
    pool = _free_ip_pool
    if station is None or pool is None:
        return False
    return getattr(pool, "is_watchdog_cancelled", lambda *a, **k: False)(
        station, asyncio.current_task()
    )


def _free_stations_exhausted(free_model: str) -> bool:
    """True when NO station can serve a fresh free attempt for this model.

    A station is exhausted when its tunnel is down / marked bad by a
    recent 429, or when the (model, IP) cooldown key of its current IP
    is still active. Used by strict_free: only refuse the request when
    every station is truly exhausted — otherwise let the free attempt
    land on the usable station instead of paying or refusing.

    [Axe 1.4] A rotation in flight is about to land a fresh (model, IP)
    key on its station — the pool is NOT exhausted. Checking participation
    re-reads the same single-threaded state the pool mutates, so the gate
    is atomic with respect to rotations (no lock needed; see
    ``FreeIPPool.any_rotation_in_flight``).
    """
    if not _free_ip_pool:
        return True
    if getattr(_free_ip_pool, "socks5_mode", False):
        # [Axe 3.1] socks5 mode: the docker stations are inert — the usable
        # set is the enabled static proxies. No rotation can land a fresh
        # (model, IP) key here (no docker), so the rotation-in-flight
        # exemption below does not apply.
        for ep in _free_ip_pool._socks5_enabled_eps():
            if _free_ip_pool._socks5_usable(ep, exclude_approaching=False):
                if not _free_cooldown_active(free_model, ep):
                    return False
        return True
    if not _free_ip_pool._stations:
        return True
    if getattr(_free_ip_pool, "any_rotation_in_flight", lambda: False)():
        return False
    for st in _free_ip_pool._stations:
        if _free_ip_pool._station_usable(st, exclude_approaching=False):
            if not _free_cooldown_active(free_model, st):
                return False
    return True


def _on_free_429_stream(free_model: str, retry_after: str = "", forced_pool=None) -> bool:
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

    Axe B: ``forced_pool`` is propagated to the background rotation so
    the rotated station stays within geo-allowed countries.

    No-op when VPN rotation is off.
    """
    station = _free_attempt_station()
    _set_free_cooldown(free_model, _free_429_cooldown_seconds(retry_after), station)
    if _free_ip_pool:
        try:
            _free_ip_pool.on_quota_exhausted(station, forced_pool=forced_pool)
        except TypeError:
            _free_ip_pool.on_quota_exhausted(station)
    return bool(IP_ROTATION.get("strict_free", False)) and _free_stations_exhausted(free_model)


def _free_usage_ip(station=None) -> str:
    """Best-effort egress IP for free-model usage logging ([9]).

    Never does network I/O — prefers the live VPN IP of the station
    used (or station 1 as before), falls back to the cached ipify result
    from the last non-stream probe.
    """
    vpn = station or (_free_ip_pool.active_station if _free_ip_pool else None) or _vpn_manager
    if getattr(vpn, "pid", None):
        # [Axe 3.1] Static SOCKS5 proxy: no docker IP to report — its
        # host:port identity is the correct usage label.
        return f"socks5:{vpn.pid}"
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
        resp = _anthropic_error(
            429,
            f"Free quota exhausted on all VPN stations. Retry after {retry_after}s.",
            error_type="rate_limit_error",
        )
    else:
        resp = _openai_error(
            429,
            f"Free quota exhausted on all VPN stations. Retry after {retry_after}s.",
            error_type="rate_limit_error",
        )
    resp.headers["Retry-After"] = retry_after
    return resp


async def _try_free_model_first(body, headers, protocol, model_id, forced_pool=None):
    """Try the free model equivalent before falling back to paid.

    If the model has a free equivalent in FREE_MODEL_MAP, attempt the request
    via the free endpoint first. On 429 (quota exhausted) or any other
    non-200, returns None to signal the caller should use the paid model
    instead, and sets a per-(model, IP) cooldown ([4]).

    [plan 19/08 §1] max_free_attempts free strikes per request, each on a
    DIFFERENT station: a 429 or dead tunnel consumes one attempt and the
    next lands on a fresh station (fresh IP = fresh quota). Only after the
    budget is spent does the request fall back to paid (or to the
    residential-IP direct fallback on tunnel failure, station-first mode).

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

    free_endpoint = _cfg_settings._free_endpoint_for(free_model)
    if API_BASE_FREE != _cfg_settings.API_BASE_FREE:
        if "/responses" in free_endpoint:
            free_endpoint = (
                API_BASE_FREE.replace("/chat/completions", "/responses").replace(
                    "/v1/chat/completions", "/v1/responses"
                )
                if "/chat/completions" in API_BASE_FREE
                else free_endpoint
            )
        else:
            free_endpoint = API_BASE_FREE

    # [plan 19/08 §1] pre-flight: ANY station with a fresh (model, IP) key?
    # A hot key on the best station is not a dead end — each other station
    # carries a different IP = a fresh key. The legacy check (best station
    # hot → paid) leaked paid traffic whenever the fleet carried another
    # warm station; only a request whose EVERY station is hot/bad/down must
    # skip the free path. No pool (or VPN off) → legacy direct-mode attempt
    # (residential IP), no gating.
    #
    # [fix 20/08] Gate removed — the multi-attempt loop already handles
    # station exhaustion naturally (tries each station, falls back to paid
    # after budget). The pre-flight gate caused non-streaming to skip free
    # while streaming (no gate) worked fine.
    # if _free_ip_pool and _free_ip_pool.enabled and _free_stations_exhausted(free_model):
    #     _debug(f"  [free] skipping free model {free_model!r} (no station with a fresh key)")
    #     return None

    _debug(f"  [free] trying free model {free_model!r} instead of {model_id!r}")

    try:
        station = _free_ip_pool._best_station(forced_pool) if _free_ip_pool else None
    except TypeError:
        station = _free_ip_pool._best_station() if _free_ip_pool else None

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
    # [free_parallel hedge] skip pre-increment when hedge will race —
    # winner-only compta (loser not counted).
    _hedge_pre = False
    try:
        if _free_ip_pool and _free_ip_pool.enabled and _free_parallel_should_hedge(body, forced_pool):
            _hedge_pre = True
            _debug("  [free] hedge pre-check true — skipping on_request")
    except Exception:
        _hedge_pre = False
    if _free_ip_pool and _free_ip_pool.enabled and not _hedge_pre:
        try:
            _, station = await _free_ip_pool.on_request(forced_pool)
        except TypeError:
            _, station = await _free_ip_pool.on_request()
        vpn = station or _vpn_manager
        if vpn and vpn.current_ip:
            free_ip = vpn.current_ip

    # Free models don't need authentication — minimal headers + identity UA
    free_profile = _current_free_identity(station)
    _current_free_attempt.set(
        {"ip": free_ip, "identity": free_profile.get("impersonate") or "", "station": station}
    )
    free_headers = _apply_identity(
        {"Content-Type": "application/json"}, free_profile, use_curated_ua=True
    )
    free_api_key = "free (no auth)"
    free_workspace = "free (no auth)"

    _is_responses = "/responses" in free_endpoint
    if _is_responses:
        if "input" in body:
            free_body = dict(body)
            free_body["model"] = free_model
        elif protocol == "anthropic":
            free_body = _anthropic_to_responses_request({**body, "model": free_model})
        else:
            free_body = _chat_to_responses_request({**body, "model": free_model})
    else:
        free_body = dict(body)
        free_body["model"] = free_model
    free_body = ensure_min_tokens(free_body)
    _free_is_responses = _is_responses

    t0 = time.monotonic()

    # Determine routing based on proxy mode
    proxy_mode = "direct"
    if _vpn_manager:
        proxy_mode = _vpn_manager.proxy_mode

    if proxy_mode in ("vpn", "socks5") and _free_ip_pool and _free_ip_pool.enabled:
        # VPN/socks5 mode: use curl_cffi for TLS fingerprint evasion,
        # routed through the chosen station's tunnel or static SOCKS5
        # proxy for a fresh IP — station.socks5_url guarantees the
        # request egresses the same endpoint stamped in the contextvar.
        #
        # [plan 19/08 §1] multi-attempt loop: up to max_free_attempts free
        # strikes per request, each on a DIFFERENT station (cumulative
        # exclusion — never re-strike the same IP twice). A 429 or a dead
        # tunnel consumes one attempt and the loop continues on the next
        # station (fresh IP = fresh quota); only after the budget is spent
        # does the request fall back to paid (or to the residential-IP
        # direct fallback on tunnel failure — its bucket is long consumed,
        # so direct is reached only when every tunnel is down: paid
        # fallback is correct behaviour then).
        free_max = effective_free_max_attempts(forced_pool)
        station_first = _free_exception_fallback_mode() == "station-first"
        # [free_parallel hedge] N-wide race, first wins, winner-only compta, streaming OFF
        _hedge_done = False
        resp = resp_headers = None
        _hedge_cands = []
        try:
            if (not free_body.get("stream") and _free_ip_pool and not getattr(_free_ip_pool, "socks5_mode", False)
                and _free_parallel_should_hedge(free_body, forced_pool)):
                _hedge_cands = _free_ip_pool.pick_candidates(forced_pool)
                # bound already in _hedged_fetch via hedge_max, but we check length
                if len(_hedge_cands) >= 2:
                    _debug(f"  [free] hedge start N={len(_hedge_cands)} stagger={_free_ip_pool._free_parallel_hedge_delay_ms}ms")
                    try:
                        resp, winner = await _hedged_fetch(_hedge_cands, free_body, free_headers, free_endpoint, forced_pool)
                        # hedged_fetch already did note_hedge_winner + _current_free_attempt
                        station = winner
                        if winner and getattr(winner, "current_ip", None):
                            free_ip = winner.current_ip
                        elif winner and getattr(winner, "pid", None):
                            free_ip = winner.pid
                        # set headers for outer shared handling (resp_headers stays None for hedge)
                        _hedge_done = True
                        _debug(f"  [free] hedge winner station {getattr(winner,'_station','?')} status={resp.status_code if resp else '?'}")
                    except Exception as he:
                        _debug(f"  [free] hedge failed, fallback sequential: {he}")
                        resp = None
                        _hedge_done = False
        except Exception as he:
            _debug(f"  [free] hedge check failed: {he}")
        # if hedge succeeded (resp is HTTP response, even 429), skip sequential loop and go to shared handling
        # if hedge failed or not enabled, run sequential multi-attempt loop
        tried: set = set()
        if not _hedge_done:
            if station is not None:
                tried.add(station)  # on_request pick = first strike (dual-clutch)
            _free_used = 0
        else:
            # hedge consumed 1 logical attempt; shared handling below will treat resp
            _free_used = 1
            tried = set(_hedge_cands)  # mark all hedged candidates as tried to avoid re-strike in fallback
        while not _hedge_done and resp is None and _free_used < free_max:
            if _free_used == 0 and station is not None:
                attempt = station
            elif _free_ip_pool:
                if getattr(_free_ip_pool, "socks5_mode", False):
                    # [Axe 3.1] Static list — next round-robin proxy NOT in
                    # ``tried`` (never re-strike the same proxy twice). The
                    # two-pass walk admits an at-quota proxy on the second
                    # pass so a request still gets its free shot.
                    attempt = _free_ip_pool._socks5_next(excluded=tried)
                else:
                    attempt = (
                        _free_ip_pool._best_station_excluding_many(tried, forced_pool)
                        if tried
                        else _free_ip_pool._best_station(forced_pool)
                    )
                if attempt is not None:
                    tried.add(attempt)
            else:
                attempt = None
            if attempt is None:
                break  # no station left → direct fallback below
            _free_used += 1
            _free_elapsed = int((time.monotonic() - t0) * 1000)
            try:
                resp = await _do_free_request_curl_cffi(
                    free_body,
                    free_headers,
                    attempt.socks5_url,
                    station=attempt,
                    endpoint=free_endpoint,
                )
                if attempt.current_ip:
                    free_ip = attempt.current_ip
                elif getattr(attempt, "pid", None):
                    # [Axe 3.1] Static proxy: no docker IP — log its id so
                    # usage rows distinguish the proxies.
                    free_ip = attempt.pid
                _current_free_attempt.set(
                    {
                        "ip": free_ip,
                        "identity": _current_free_identity(attempt).get("impersonate") or "",
                        "station": attempt,
                    }
                )
                station = attempt  # cooldown key + on_quota_exhausted must target THIS IP
                if resp.status_code == 429 and _free_used < free_max:
                    # 429 = this station's bucket exhausted → cooldown
                    # (model, IP) + background rotation, then continue on a
                    # FRESH station while the budget lasts ([plan 19/08 §1]:
                    # each attempt a fresh IP; a same-IP retry would burn
                    # the budget on a guaranteed ✘).
                    _log_free_model_usage(
                        model_id,
                        free_model,
                        free_api_key,
                        free_workspace,
                        429,
                        0,
                        0,
                        _free_elapsed,
                        ip=free_ip,
                    )
                    _set_free_cooldown(
                        free_model,
                        _free_429_cooldown_seconds(resp.headers.get("retry-after", "") or ""),
                        attempt,
                    )
                    if _free_ip_pool:
                        try:
                            _free_ip_pool.on_quota_exhausted(attempt, forced_pool=forced_pool)
                        except TypeError:
                            _free_ip_pool.on_quota_exhausted(attempt)
                    _log(
                        f"  FREE {free_model!r} RATE LIMITED (429) on station {attempt._station} → "
                        f"retry station fraîche (essai {_free_used + 1}/{free_max})"
                    )
                    resp = None
                    continue
                break  # 200, final-429 or other status → shared handling below
            except Exception as e:
                _debug(f"  [free] curl_cffi error (station {attempt._station}): {e}")
                if station_first and _free_used < free_max:
                    # [plan 19/08 §2] station-first: a dead tunnel retries
                    # another station BEFORE the residential-IP direct
                    # fallback (whose bucket is long exhausted).
                    _log(
                        f"  FREE via station {attempt._station} tunnel FAILED ({e}) → "
                        f"retry station fraîche (essai {_free_used + 1}/{free_max})"
                    )
                    continue
                # station-first budget spent, or direct mode → legacy fallback
                _log(
                    f"  FREE via station {attempt._station} tunnel FAILED ({e}) → direct fallback (residential IP)"
                )
                resp = None
                break
        if resp is None:
            _log("  FREE via VPN tunnels FAILED → direct fallback (residential IP)")
            try:
                resp, resp_headers = await _do_request_with_retry(
                    free_endpoint, free_body, free_headers, protocol, retry_on_429=False
                )
            except UpstreamError:
                _log_free_model_usage(
                    model_id, free_model, free_api_key, free_workspace, 502, ip=free_ip
                )
                return None
    else:
        try:
            resp, resp_headers = await _do_request_with_retry(
                free_endpoint, free_body, free_headers, protocol, retry_on_429=False
            )
        except UpstreamError:
            _log_free_model_usage(
                model_id, free_model, free_api_key, free_workspace, 502, ip=free_ip
            )
            return None

    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if _free_is_responses and resp.status_code == 200:
        try:
            rdata = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            _debug(
                f"  [free] raw response keys: {list(rdata.keys()) if isinstance(rdata, dict) else 'not dict'}"
            )
            # Guard empty response (131x observed -> 0 tokens success should be treated as failure)
            if not isinstance(rdata, dict) or not rdata:
                _debug("  [free] empty JSON response — treating as failure, fallback to paid")
                _log_free_model_usage(model_id, free_model, free_api_key, free_workspace, 502, ip=free_ip)
                return None
            if isinstance(rdata, dict) and "output" in rdata:
                _debug(f"  [free] output items: {len(rdata.get('output', []))} items")
                for i, item in enumerate(rdata.get("output", [])[:3]):
                    if isinstance(item, dict):
                        itype = item.get("type", "?")
                        ikeys = list(item.keys())
                        # log reasoning summary lengths for debug (the user issue: incomplete thinking)
                        if itype == "reasoning":
                            summ = item.get("summary", [])
                            if isinstance(summ, list):
                                total_len = sum(len(s.get("text","")) for s in summ if isinstance(s, dict))
                                _debug(f"  [free] item {i}: type={itype}, keys={ikeys} summary_len={total_len} summary_parts={len(summ)} encrypted={bool(item.get('encrypted_content'))}")
                            else:
                                _debug(f"  [free] item {i}: type={itype}, keys={ikeys} summary_nolist encrypted={bool(item.get('encrypted_content'))}")
                        else:
                            _debug(f"  [free] item {i}: type={itype}, keys={ikeys}")
                        if itype == "message":
                            # also log text length
                            txt_len = 0
                            for blk in item.get("content", []) or []:
                                if isinstance(blk, dict) and blk.get("type") == "output_text":
                                    txt_len += len(blk.get("text",""))
                            if txt_len:
                                _debug(f"  [free] item {i} message text_len={txt_len}")
                if not rdata.get("output"):
                    _debug("  [free] empty output array — treating as failure, fallback to paid")
                    _log_free_model_usage(model_id, free_model, free_api_key, free_workspace, 502, ip=free_ip)
                    return None
            if protocol == "anthropic":
                cdata = _responses_to_anthropic_response(rdata, free_model)
                usage = cdata.get("usage", {})
                tokens_in = usage.get("input_tokens", 0)
                tokens_out = usage.get("output_tokens", 0)
            else:
                cdata = _responses_to_chat_response(rdata, free_model)
                usage = cdata.get("usage", {})
                tokens_in = usage.get("prompt_tokens", 0)
                tokens_out = usage.get("completion_tokens", 0)
            _log_free_model_usage(
                model_id,
                free_model,
                free_api_key,
                free_workspace,
                resp.status_code,
                tokens_in,
                tokens_out,
                elapsed_ms,
                ip=free_ip,
            )
            _debug(
                f"  [free] {free_model!r} succeeded ({tokens_in}+{tokens_out} tokens) via /responses"
            )
            wrapped = _CurlCffiResponse.__new__(_CurlCffiResponse)
            wrapped._resp = resp._resp if hasattr(resp, "_resp") else resp
            wrapped.status_code = 200
            wrapped.headers = dict(resp.headers)
            wrapped._converted = cdata
            orig_json = wrapped._resp.json if hasattr(wrapped._resp, "json") else (lambda: cdata)
            wrapped.json = lambda: cdata
            # [C3 perf] un seul dump : content (bytes) puis text = decode()
            _c3_bytes = _json_dumps(cdata)
            wrapped.content = _c3_bytes
            wrapped.text = _c3_bytes.decode()
            return wrapped, resp_headers, free_model, free_ip
        except Exception as e:
            _debug(f"  [free] /responses conversion failed: {e}")

    tokens_in = tokens_out = 0
    if resp.status_code == 200:
        try:
            data = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
            usage = data.get("usage", {})
            tokens_in = usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
            tokens_out = usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
        except Exception:
            pass

    _log_free_model_usage(
        model_id,
        free_model,
        free_api_key,
        free_workspace,
        resp.status_code,
        tokens_in,
        tokens_out,
        elapsed_ms,
        ip=free_ip,
    )

    # [plan v10 §3.6.1 Lot 3] mesure latence-adaptive par (station, ip) —
    # succès ET échec ; décision soft/hard + garde-fous (anti-flap, paused,
    # global_degraded) appliqués par le moteur/superviseur, fail-soft total.
    try:
        import shared_state as _ss_lat

        _eng = getattr(_ss_lat, "latency_engine", None)
        if _eng is None:
            from latency_rotation import get_engine as _get_eng

            _eng = _get_eng()
            _ss_lat.latency_engine = _eng
        if isinstance(station, object) and getattr(station, "_station", None) is not None:
            _sid = int(station._station)
            _dec = _eng.record_request(_sid, str(free_ip or "direct"), float(elapsed_ms), free_model, resp.status_code)
            # [v10 §12.1.3] alerte proactive SSE : une lente avant le seuil
            if _dec.get("action") == "warn":
                try:
                    from dashboard.events import get_event_manager as _gem

                    _gem().publish(
                        "vpn_event",
                        {
                            "reason": "slow_ip_warn",
                            "station": _sid,
                            "ip": str(free_ip or ""),
                            "ewma_ms": _dec.get("ewma"),
                            "consecutive_slow": 1,
                        },
                    )
                except Exception:
                    pass
            if _dec.get("action") in ("soft", "hard"):
                for _sup in getattr(_ss_lat, "station_supervisors", None) or []:
                    if _sup.station == _sid:
                        await _sup.react_to_decision(_dec, station_obj=station)
                        break
    except Exception as _e_lat:
        _debug(f"  [free] latency engine skip: {_e_lat}")

    if resp.status_code == 200:
        _debug(f"  [free] {free_model!r} succeeded ({tokens_in}+{tokens_out} tokens)")
        _log(f"  FREE {free_model!r} OK ({tokens_in}+{tokens_out} tokens, saved paid quota)")
        return resp, resp_headers, free_model, free_ip

    # 429 = free quota exhausted → fall back to paid silently
    if resp.status_code == 429:
        # Read and log the 429 response body for quota analysis
        try:
            body_429 = resp.text[:500] if hasattr(resp, "text") else ""
        except Exception:
            body_429 = ""
        # Extract retry-after header (seconds until quota resets)
        retry_after = resp.headers.get("retry-after", "")
        # Also log response headers (may contain X-RateLimit-*)
        headers_429 = {
            k: v
            for k, v in resp.headers.items()
            if k.lower()
            in (
                "retry-after",
                "x-ratelimit-limit",
                "x-ratelimit-remaining",
                "x-ratelimit-reset",
                "content-type",
            )
        }
        _debug(f"  [free] {free_model!r} 429 body={_redact(body_429)!r} headers={headers_429}")
        _log(
            f"  FREE {free_model!r} RATE LIMITED (429) retry-after={retry_after}s → falling back to paid {model_id!r}"
        )

        # Per-(model, IP) cooldown honoring the upstream retry-after ([4])
        # + background IP rotation ([0]/[42]): the key is (model, the
        # station's current IP), so the rotation makes the next free
        # attempt a fresh key. (Non-final VPN 429s already did this in the
        # multi-attempt loop — idempotent here.)
        _set_free_cooldown(free_model, _free_429_cooldown_seconds(retry_after), station)
        if _free_ip_pool:
            try:
                _free_ip_pool.on_quota_exhausted(station, forced_pool=forced_pool)
            except TypeError:
                _free_ip_pool.on_quota_exhausted(station)

        # strict_free (GUI): when EVERY station is exhausted, refuse
        # instead of paying — the caller answers 429/503 with Retry-After.
        if IP_ROTATION.get("strict_free", False) and _free_stations_exhausted(free_model):
            raise FreeQuotaExhausted(retry_after)

        return None

    # Other errors → cooldown (60 s) so the next request doesn't retry the
    # free model immediately, then fall back to paid
    try:
        _dbg_body = _truncate(free_body) if "free_body" in locals() else ""
        _dbg_resp = _redact(getattr(resp, "text", "")[:2000]) if hasattr(resp, "text") else ""
    except:
        _dbg_body, _dbg_resp = "", ""
    _debug(
        f"  [free] {free_model!r} returned {resp.status_code} body={_dbg_body[:2500]} resp={_dbg_resp[:2000]} → falling back to paid"
    )
    _set_free_cooldown(free_model, 60, station)
    return None


async def _do_request_with_retry(endpoint, body, headers, protocol, retry_on_429=True):
    """POST request with automatic 429/401 key failover and 5xx retry with backoff.

    Key failover (429/401) does NOT consume a retry attempt — it's a key change,
    not a retry. Only 5xx errors consume retries.

    Returns (response, final_headers) -- headers may differ after retry.
    Raises UpstreamError on connection/timeout/protocol failures.
    """
    # ── Orphan guard (defense in depth) ──
    if isinstance(body, dict):
        if "messages" in body:
            body["messages"] = _drop_orphan_tool_messages(body["messages"])
        elif "input" in body:
            body["input"] = _drop_orphan_responses_input(body["input"])
            # [Correctif parité multi-tours] marqueur interne jamais envoyé à
            # l'upstream — consommé par le retry-once du caller.
            body.pop("_has_synthetic_reasoning_items", None)
    # ── Orphan guard (defense in depth) ──
    if isinstance(body, dict):
        if "messages" in body:
            body["messages"] = _drop_orphan_tool_messages(body["messages"])
        elif "input" in body:
            body["input"] = _drop_orphan_responses_input(body["input"])
    _RETRYABLE_STATUSES = {500, 502, 503, 504, 499}
    max_retries = yaml_get("streaming", "retry_attempts", 2)
    attempt = 0

    while attempt < max_retries:
        if DEBUG:
            _debug(
                f"  → upstream POST {endpoint} attempt {attempt + 1}/{max_retries} headers={_sanitize_headers(headers)}"
            )
        t0 = time.monotonic()
        try:
            resp = await _ensure_http_client().post(endpoint, json=body, headers=headers)
        except httpx.ConnectError as e:
            _debug(f"  ✗ connect error after {(time.monotonic() - t0) * 1000:.0f}ms: {e}")
            _log(f"  UPSTREAM CONNECT ERROR: {type(e).__name__}: {e}")
            raise UpstreamError(
                f"Cannot connect to upstream: {e}", status_code=502, original=e
            ) from e
        except httpx.TimeoutException as e:
            _debug(f"  ✗ timeout after {(time.monotonic() - t0) * 1000:.0f}ms: {e}")
            _log(f"  UPSTREAM TIMEOUT: {type(e).__name__}: {e}")
            raise UpstreamError(
                f"Upstream request timed out: {e}", status_code=504, original=e
            ) from e
        except httpx.RequestError as e:
            _debug(f"  ✗ request error after {(time.monotonic() - t0) * 1000:.0f}ms: {e}")
            _log(f"  UPSTREAM REQUEST ERROR: {type(e).__name__}: {e}")
            raise UpstreamError(
                f"Upstream request failed: {type(e).__name__}: {e}", status_code=502, original=e
            ) from e

        elapsed_ms = (time.monotonic() - t0) * 1000
        _debug(
            f"  ← upstream {resp.status_code} in {elapsed_ms:.0f}ms | content-type={resp.headers.get('content-type', '?')}"
        )

        # Key failover on 429 — does NOT consume a retry attempt
        if retry_on_429 and resp.status_code == 429 and len(API_KEYS) > 1:
            failed_key = _key_from_headers(headers, protocol)
            # Fetch fresh quotas and pause key until exact reset time
            await _pause_key_for_quota_reset(failed_key)
            alt = _find_alternative_key(failed_key)
            if alt:
                _debug(f"  ⟳ 429 failover: {failed_key[:8]}… → alias={alt.get('alias', '?')}")
                _log("  429 on key, retrying with alternative key")
                headers = _get_auth_headers(protocol, entry=alt)
                continue  # retry immediately without incrementing attempt

        # Key failover on 401 — does NOT consume a retry attempt
        if resp.status_code == 401 and len(API_KEYS) > 1:
            failed_key = _key_from_headers(headers, protocol)
            # Log the 401 response body to help diagnose dead/revoked keys
            try:
                _err_body = resp.text[:500] if hasattr(resp, "text") else str(resp.content[:500])
            except Exception:
                _err_body = "(unable to read response body)"
            _debug(f"  [auth] 401 response body: {_redact(_err_body, 500)}")
            # 401 = key temporarily unavailable (quota exhausted) → pause 1h
            _key_pauser.pause_key(failed_key, KEY_PAUSE_401_SEC, "401 Unauthorized (temporary)")
            alt = _find_alternative_key(failed_key)
            if alt:
                _debug(f"  ⟳ 401 failover: {failed_key[:8]}… → alias={alt.get('alias', '?')}")
                _log("  401 on key (invalid/revoked), retrying with alternative key")
                headers = _get_auth_headers(protocol, entry=alt)
                continue  # retry immediately without incrementing attempt

        # Key failover on 403 — does NOT consume a retry attempt
        # 403 = region/model blocked for the key (e.g. RegionError). Pause the
        # key and try another account before surfacing anything to the client.
        if retry_on_429 and resp.status_code == 403 and len(API_KEYS) > 1:
            failed_key = _key_from_headers(headers, protocol)
            try:
                _err_body = resp.text[:500] if hasattr(resp, "text") else str(resp.content[:500])
            except Exception:
                _err_body = "(unable to read response body)"
            _debug(f"  [auth] 403 response body: {_redact(_err_body, 500)}")
            _key_pauser.pause_key(failed_key, 1800, "403 Forbidden (region/model not allowed)")
            alt = _find_alternative_key(failed_key)
            if alt:
                _debug(f"  ⟳ 403 failover: {failed_key[:8]}… → alias={alt.get('alias', '?')}")
                _log("  403 on key, retrying with alternative key")
                headers = _get_auth_headers(protocol, entry=alt)
                continue  # retry immediately without incrementing attempt

        # Retry on 502/503/504 with backoff — DOES consume a retry attempt
        if resp.status_code in _RETRYABLE_STATUSES and attempt < max_retries - 1:
            wait = 1.0 * (2**attempt)  # 1s, 2s
            _debug(f"  ⟳ retry {resp.status_code} in {wait:.1f}s")
            _log(
                f"  RETRY {resp.status_code} after {wait:.1f}s (attempt {attempt + 1}/{max_retries})"
            )
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
        _debug(
            f"  [tokens] _update_token_usage: model={model_id} +{inp} in +{out} out +{cache} cache | totals: {_token_usage[model_id]}"
        )
    except Exception as e:
        _debug(f"  ✗ _update_token_usage failed: {type(e).__name__}: {e}")
        _log(f"  WARN: _update_token_usage failed for {model_id!r}: {type(e).__name__}: {e}")


async def _save_and_log_request(
    req_id,
    model_id,
    original_model,
    start_time,
    inp,
    out,
    cache,
    protocol,
    is_stream,
    thinking_type,
    effort,
    client_ip,
    account_alias,
    tools,
    log_tag="",
    tools_used=None,
    request_body=None,
    response_body=None,
    free_model_ip=None,
    identity=None,
):
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
        await _save_request(
            req_id,
            model_id,
            original_model,
            _elapsed_ms(start_time),
            inp,
            out,
            cache,
            success=True,
            protocol=protocol,
            is_stream=is_stream,
            thinking=thinking_type,
            effort=effort,
            client_ip=client_ip,
            account_alias=account_alias,
            tools=tools,
            tools_used=tools_used,
            request_body=request_body,
            response_body=response_body,
            free_model_ip=free_model_ip,
            identity=identity,
        )
    except Exception as e:
        _debug(f"  ✗ save_request failed: {type(e).__name__}: {e}")
        _log(f"  WARN: save_request failed: {type(e).__name__}: {e}")


async def _log_and_save_error(
    req_id,
    model_id,
    original_model,
    start_time,
    status_code,
    resp_text,
    protocol,
    is_stream,
    thinking_type,
    effort,
    client_ip,
    account_alias,
    tools,
    tools_used=None,
    request_body=None,
    response_body=None,
):
    """Log error and save to DB with success=False."""
    _log(f"  ERROR {status_code}: {_redact(resp_text, 300)}")
    try:
        await _save_request(
            req_id,
            model_id,
            original_model,
            _elapsed_ms(start_time),
            0,
            0,
            0,
            success=False,
            error=f"HTTP {status_code}",
            protocol=protocol,
            is_stream=is_stream,
            thinking=thinking_type,
            effort=effort,
            client_ip=client_ip,
            account_alias=account_alias,
            tools=tools,
            tools_used=tools_used,
            request_body=request_body,
            response_body=response_body,
        )
    except Exception as e:
        _debug(f"  ✗ save_request (error path) failed: {type(e).__name__}: {e}")
        _log(f"  WARN: save_request (error path) failed: {type(e).__name__}: {e}")


# ── Streaming helpers ───────────────────────────────────────────


def _make_stream_retry_loop(protocol):
    """Return a retry function for streaming 429/401 handling.

    Returns (attempt_headers, should_retry) where should_retry=True means
    the caller should `continue` the outer loop.
    """

    async def _handle_429(headers, status_code, attempt, resp_headers=None, resp_text=None):
        # B4 idempotence: 400 never retries here (param=name would pause_key uselessly).
        # Credit 400 retry is handled explicitly in the non-stream caller after checking
        # "Insufficient balance" / "Monthly usage limit" in resp.text (see messages() P2).
        if attempt == 0 and len(API_KEYS) > 1 and status_code in (429, 401, 403):
            failed_key = _key_from_headers(headers, protocol)
            if status_code == 429:
                # Fetch fresh quotas and pause key until exact reset time
                await _pause_key_for_quota_reset(failed_key)
            elif status_code == 401:
                # [§14.1.15 v10] aligné sur le non-stream : même code HTTP =
                # même pause (l'ancien 24h créait une incohérence stream/non).
                _key_pauser.pause_key(failed_key, KEY_PAUSE_401_SEC, "401 Unauthorized (temporary)")
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


async def _stream_error_response(
    req_id,
    model_id,
    original_model,
    start_time,
    status_code,
    resp_body,
    protocol,
    thinking_type,
    effort,
    client_ip,
    account_alias,
    tools,
    error_payload,
    tools_used=None,
    request_body=None,
):
    """Handle streaming error: log, save DB, yield error SSE event. Returns the error event bytes."""
    _log(f"  ERROR {status_code}: {_redact(resp_body, 300)}")
    await _save_request(
        req_id,
        model_id,
        original_model,
        _elapsed_ms(start_time),
        0,
        0,
        0,
        success=False,
        error=f"HTTP {status_code}",
        protocol=protocol,
        is_stream=True,
        thinking=thinking_type,
        effort=effort,
        client_ip=client_ip,
        account_alias=account_alias,
        tools=tools,
        tools_used=tools_used,
        request_body=request_body,
    )
    return _sse("error", error_payload)


def _finalize_stream_tokens(
    model_id,
    est_input,
    stream_in,
    stream_out,
    stream_cache,
    actual_usage,
    _token_usage,
    _token_lock,
):
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
            _debug(
                f"  [tokens] _finalize_stream_tokens: model={model_id} fallback (no actual_usage) est_in={est_input} stream_out={stream_out}"
            )
    except Exception as e:
        _debug(f"  ✗ _finalize_stream_tokens failed: {type(e).__name__}: {e}")
        _log(f"  WARN: _finalize_stream_tokens failed for {model_id!r}: {type(e).__name__}: {e}")

    return final_in, final_out, final_cache, log_tag


def _sse(event: str, payload: dict) -> bytes:
    return f"event: {event}\ndata: {_json_dumps_str(payload, ensure_ascii=False)}\n\n".encode()


def _stream_has_yielded(started, open_blocks, stream_out, line_buf: str = "") -> bool:
    """True once any SSE byte has been flushed to the client — retry must not concat."""
    try:
        has_blocks = bool(open_blocks)
    except Exception:
        has_blocks = False
    try:
        has_out = int(stream_out or 0) > 0
    except Exception:
        has_out = bool(stream_out)
    return bool(
        started or has_blocks or has_out or (isinstance(line_buf, str) and bool(line_buf.strip()))
    )


async def _terminate_after_started(open_blocks, stream_out, thinking_idx=None, thinking_sig=""):
    """Graceful SSE termination after a mid-stream failure (avoids concat retry).

    [PLAN-raisonnement Phase C.2] si un bloc thinking est encore ouvert,
    son `signature_delta` local est émis AVANT le content_block_stop — même
    en fin de stream brutale, le client reçoit un bloc thinking complet
    (thinking_delta* → signature_delta → stop), sinon il l'abandonne."""
    for idx in list(open_blocks):
        if thinking_idx is not None and idx == thinking_idx and thinking_sig:
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "signature_delta", "signature": thinking_sig},
                },
            )
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "error"},
            "usage": {"output_tokens": stream_out},
        },
    )
    yield _sse("message_stop", {"type": "message_stop"})


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

    [B2 perf] recyclage des tâches : le timer de ping n'est plus annulé/
    recréé À CHAQUE chunk (~2 Tasks + un asyncio.wait par delta à 50-200
    deltas/s). Un ``asyncio.Event`` réarme la fenêtre d'idle en place ; le
    task timer n'est recréé qu'après un VRAI ping (silence réel), et la
    read-task reste la seule recréée par chunk (un seul anext à la fois).
    """
    read_task = None
    timer_task = None
    activity = asyncio.Event()

    async def _idle_timer():
        # Se TERMINE seulement après `interval` SANS activité — l'Event
        # réarmé par chaque chunk relance la fenêtre sans recycle.
        while True:
            activity.clear()
            try:
                await asyncio.wait_for(activity.wait(), timeout=interval)
            except TimeoutError:
                return

    try:
        while True:
            if read_task is None or read_task.done():
                read_task = asyncio.ensure_future(anext(stream_gen))
            if timer_task is None or timer_task.done():
                timer_task = asyncio.ensure_future(_idle_timer())
            done, _pending = await asyncio.wait(
                {read_task, timer_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if timer_task in done:
                # Recyclé UNIQUEMENT après un vrai ping (plus de churn/chunk)
                timer_task = None
                yield b": ping\n\n"
            if read_task in done:
                activity.set()  # [B2] réarme le timer sans le recréer
                try:
                    chunk = read_task.result()
                except StopAsyncIteration:
                    return
                except Exception as e:
                    # ClientDisconnect, ConnectionResetError, etc. — client gone, stop gracefully
                    # Log traceback for unexpected errors (like UnboundLocalError) to aid debugging
                    if isinstance(e, (ConnectionError, OSError)):
                        _debug(
                            f"  [stream] keepalive exiting (client gone): {type(e).__name__}: {e}"
                        )
                    else:
                        _debug(
                            f"  [stream] keepalive exiting: {type(e).__name__}: {e}\n{traceback.format_exc()}"
                        )
                    return
                yield chunk
    finally:
        if timer_task is not None and not timer_task.done():
            timer_task.cancel()
        if read_task is not None and not read_task.done():
            # Only reached on a client disconnect / generator teardown —
            # THERE the upstream abort is correct (client is gone).
            read_task.cancel()


_SSE_COALESCE_MAX_BYTES = 64 * 1024


async def _sse_coalesce(stream, max_group_bytes: int = _SSE_COALESCE_MAX_BYTES):
    """[B3 perf] Micro-batch SSE : regroupe les chunks DÉJÀ disponibles.

    Après chaque lecture, UN seul tick de scheduler laisse se terminer les
    lectures déjà prêtes (burst upstream bufferisé dans httpx/curl) ; tout
    ce qui est arrivé est émis en un seul bytes groupé → un send ASGI au
    lieu de N. Drain STRICTEMENT non-bloquant : aucun await d'attente, aucun
    timeout — latence inchangée (≤ 1 tick de boucle par groupe émis), juste
    moins de syscalls réseau par delta. Les frames SSE étant complètes et
    auto-délimitées (\\n\\n), la concaténation est transparente pour tout
    client SSE conforme.
    """
    it = stream.__aiter__()
    pending = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(it))
            done, _rest = await asyncio.wait(
                {pending}, return_when=asyncio.FIRST_COMPLETED
            )
            pending = None
            try:
                first = done.pop().result()
            except StopAsyncIteration:
                return
            except Exception:
                # mêmes sémantiques que _sse_keepalive : fin propre
                return
            if not isinstance(first, (bytes, bytearray)):
                yield first
                continue
            groups = [first]
            size = len(first)
            exhausted = False
            while size < max_group_bytes:
                nxt = asyncio.ensure_future(anext(it))
                await asyncio.sleep(0)  # LE drain non-bloquant (1 tick max)
                if not nxt.done():
                    pending = nxt  # rien de prêt : on rend la main
                    break
                try:
                    chunk = nxt.result()
                except StopAsyncIteration:
                    exhausted = True
                    break
                except Exception:
                    exhausted = True
                    break
                if isinstance(chunk, (bytes, bytearray)):
                    groups.append(chunk)
                    size += len(chunk)
                else:
                    yield b"".join(groups)
                    groups = []
                    yield chunk
                    break
            if groups:
                yield b"".join(groups)
            if exhausted:
                return
    finally:
        if pending is not None and not pending.done():
            pending.cancel()


# ── Routing fast cache (O(1) exact + substring) ──
_route_cache: dict[str, dict | None] = {}
_ROUTE_CACHE_MAX = 2048


def _route_for(model_name: str) -> dict | None:
    cache_key = model_name.lower().strip()
    if cache_key in _route_cache:
        return _route_cache[cache_key]
    maybe_reload_custom_routes()
    name = model_name.lower().strip()
    if not name:
        _route_cache[cache_key] = None
        return None
    # When DISABLE_MAPPING, only check custom routes (not auto-generated aliases)
    # but keep manual opus/sonnet/haiku aliases (they are defined in ROUTES even when auto-mapping is off)
    if DISABLE_MAPPING:
        for r in _cfg_settings.SORTED_CUSTOM_ROUTES:
            if any(m in name for m in r.get("match", [])):
                if len(_route_cache) >= _ROUTE_CACHE_MAX:
                    _route_cache.clear()
                _route_cache[cache_key] = r
                _debug(f"  [route] DISABLE_MAPPING custom match: '{name}' → {r.get('model')}")
                return r
        # Manual opus/sonnet/haiku routes must remain available even with DISABLE_MAPPING (Claude Code defaults)
        for r in _cfg_settings.SORTED_ROUTES:
            # Only the 3 manual aliases have match == opus/sonnet/haiku (checked via small set)
            mlist = [m.lower() for m in r.get("match", []) if isinstance(m, str)]
            if any(m in ("opus", "sonnet", "haiku") for m in mlist):
                if any(m in name for m in r.get("match", [])):
                    if len(_route_cache) >= _ROUTE_CACHE_MAX:
                        _route_cache.clear()
                    _route_cache[cache_key] = r
                    _debug(
                        f"  [route] DISABLE_MAPPING manual alias match: '{name}' → {r.get('model')}"
                    )
                    return r
        # No custom route matched — check if the model exists directly
        if name in MODELS:
            res = {"match": [name], "model": model_name}
            if len(_route_cache) >= _ROUTE_CACHE_MAX:
                _route_cache.clear()
            _route_cache[cache_key] = res
            _debug(f"  [route] DISABLE_MAPPING direct model: '{name}'")
            return res
        _debug(f"  [route] DISABLE_MAPPING no match for '{name}'")
        _route_cache[cache_key] = None
        return None
    # 0. Exact MODELS lookup first (fastest, O(1) — covers 90% of prod traffic)
    if name in MODELS:
        res = {"match": [name], "model": name}
        for r in _cfg_settings.SORTED_CUSTOM_ROUTES:
            if r.get("enabled") is False:
                continue
            if any(m == name for m in r.get("match", [])):
                res = r
                break
        if len(_route_cache) >= _ROUTE_CACHE_MAX:
            _route_cache.clear()
        _route_cache[cache_key] = res
        _debug(f"  [route] exact MODELS hit: '{name}' → {res.get('model')}")
        return res
    # 1. Model-based routing (sorted by longest match first)
    for r in _cfg_settings.SORTED_ROUTES:
        if r.get("enabled") is False:
            continue
        if any(m in name for m in r.get("match", [])):
            if len(_route_cache) >= _ROUTE_CACHE_MAX:
                _route_cache.clear()
            _route_cache[cache_key] = r
            _debug(f"  [route] model match: {r.get('model')} (pattern in '{name}')")
            return r
    # 3. Wildcard catch-all: if a custom route "*" (or legacy "") exists, use it
    wildcard = CUSTOM_ROUTES.get("*") or CUSTOM_ROUTES.get("")
    if (
        wildcard
        and isinstance(wildcard, dict)
        and wildcard.get("model")
        and wildcard.get("enabled") is not False
    ):
        if len(_route_cache) >= _ROUTE_CACHE_MAX:
            _route_cache.clear()
        _route_cache[cache_key] = wildcard
        _debug(f"  [route] wildcard catch-all: {wildcard.get('model')}")
        return wildcard
    # 5. No match found
    _debug(f"  [route] no match for '{name}'")
    if len(_route_cache) >= _ROUTE_CACHE_MAX:
        _route_cache.clear()
    _route_cache[cache_key] = None
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


# ── Web Search / Web Fetch Handler (v3.3) ───────────────────────────────


def _normalize_tool_name(t: dict) -> str:
    raw = t.get("name") or t.get("function", {}).get("name") or ""
    if not raw:
        tp = t.get("type", "")
        if tp.startswith("web_search"):
            raw = "web_search"
        elif tp.startswith("web_fetch"):
            raw = "web_fetch"
        else:
            return ""
    low = raw.strip().lower()
    low = _re_norm.sub(r"_20\d{2}_\d{2}_\d{2}$", "", low)
    if low in ("websearch", "web_search"):
        return "web_search"
    if low in ("webfetch", "web_fetch"):
        return "web_fetch"
    return low


def _has_tool(body: dict, name: str) -> bool:
    return any(_normalize_tool_name(t) == name for t in body.get("tools", []) if isinstance(t, dict))


def _is_tool_choice_auto(body: dict) -> bool:
    tc = body.get("tool_choice")
    if tc is None:
        return True
    if tc == "auto" or tc == "any":
        return True
    if isinstance(tc, dict) and tc.get("type") in ("auto", "any"):
        return True
    return False


def _is_web_search_forced(body: dict, protocol: str) -> bool:
    """Check if tool_choice is forced to web_search (legacy compat)."""
    tc = body.get("tool_choice")
    if not isinstance(tc, dict):
        return False
    # use normalized name
    name = tc.get("name", "") or tc.get("function", {}).get("name", "")
    if _normalize_tool_name({"name": name}) == "web_search":
        return tc.get("type") in ("tool", "function")
    # also handle type-based forced without name
    if tc.get("type") == "tool" and _normalize_tool_name({"type": tc.get("name", "")}) == "web_search":
        return True
    if protocol == "anthropic":
        return tc.get("type") == "tool" and _normalize_tool_name(tc) == "web_search"
    else:
        return tc.get("type") == "function" and _normalize_tool_name(tc) == "web_search"


def _extract_search_query(body: dict) -> str:
    """Extract the search query from the last user message. Q6B F6: takes last 500 chars, not first."""
    messages = body.get("messages", [])
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            text = ""
            if isinstance(content, str):
                text = content
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text = block.get("text", "")
                        break
            if not text:
                continue
            # Look for "Perform a web search for the query: ..." pattern
            if "web search" in text.lower():
                for line in text.split("\n"):
                    if "query:" in line.lower():
                        return line.split(":", 1)[1].strip()[-500:]
            # general: last 500 chars, not first
            return text.strip()[-500:] if len(text) > 500 else text.strip()
    return ""


def _normalize_query(q: str) -> str:
    return re.sub(r"\s+", " ", q.strip().lower())[:500]


def _format_ddg(results: list, query: str) -> str:
    if not results:
        return f"No results found for: {query}"
    lines = [f"Web search results for '{query}':\n"]
    for i, r in enumerate(results, 1):
        title = r.get("title", "")
        body_text = r.get("body", "")
        href = r.get("href", "")
        lines.append(f"{i}. **{title}**\n   {body_text}\n   {href}\n")
    return "\n".join(lines)


async def _is_safe_fetch_url(url: str) -> bool:
    """SSRF guard F1+R1+R4: async, fail-closed, budgeted via outer wait_for."""
    try:
        p = urllib.parse.urlparse(url)
        if p.scheme not in ("http", "https"):
            return False
        host = (p.hostname or "").lower().rstrip(".")
        if not host or host in ("localhost", "localhost."):
            return False
        # IP literal
        try:
            ip = ipaddress.ip_address(host)
            if any([ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_reserved, ip.is_multicast]) or any(ip in n for n in _BLOCKED_NETS):
                return False
            return True
        except ValueError:
            pass
        # DNS rebinding - to_thread, outer wait_for budgets it
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            for _, _, _, _, sa in infos:
                ip = ipaddress.ip_address(sa[0])
                if any([ip.is_private, ip.is_loopback, ip.is_link_local, ip.is_reserved, ip.is_multicast]) or any(ip in n for n in _BLOCKED_NETS):
                    return False
        except Exception:
            return False
        return True
    except Exception:
        return False


async def _execute_ddg_search(query: str, max_results: int = 5, timeout: int = 10, proxy=None) -> str:
    """Execute DDG with cache, semaphore, lock per key, wait_for budget unique."""
    # clamps Q8A
    try:
        timeout = max(5, min(30, int(timeout)))
    except Exception:
        timeout = 10
    try:
        max_results = max(1, min(10, int(max_results)))
    except Exception:
        max_results = 5
    qnorm = _normalize_query(query)
    kstr = f"{qnorm}:{max_results}"
    now = time.monotonic()
    # LRU hit
    if kstr in _DDG_CACHE:
        exp, val = _DDG_CACHE[kstr]
        if now < exp:
            _DDG_CACHE.move_to_end(kstr)
            return copy.deepcopy(val)
        else:
            try:
                del _DDG_CACHE[kstr]
            except KeyError:
                pass
    # lock per key (v3.3 R3: pop after lock released, finally)
    lock = _DDG_LOCKS.get(kstr)
    if lock is None:
        lock = asyncio.Lock()
        _DDG_LOCKS[kstr] = lock
    _hit_val = None
    _hit = False
    try:
        async with lock:
            # double-check
            if kstr in _DDG_CACHE:
                exp2, val2 = _DDG_CACHE[kstr]
                if time.monotonic() < exp2:
                    _DDG_CACHE.move_to_end(kstr)
                    _hit_val = copy.deepcopy(val2)
                    _hit = True
                else:
                    _hit = False
            else:
                _hit = False
            if not _hit:
                # semaphore + wait_for
                async with _DDG_SEM:

                    def _sync_ddg():
                        try:
                            from duckduckgo_search import DDGS

                            try:
                                ddgs = DDGS(timeout=timeout, proxy=proxy)
                            except TypeError:
                                ddgs = DDGS()
                            with ddgs:
                                return list(ddgs.text(qnorm, max_results=max_results))
                        except ImportError as e:
                            raise ImportError(f"duckduckgo-search not installed: {e}")

                    try:
                        results = await asyncio.wait_for(asyncio.to_thread(_sync_ddg), timeout + 2)
                    except TimeoutError:
                        _log(f"  WEB SEARCH: DDG timeout {timeout}s query='{qnorm[:60]}' queue={3 - _DDG_SEM._value}")
                        raise
                    except ImportError:
                        raise
                    except Exception as e:
                        # on_error strip - raise to let handler strip
                        raise RuntimeError(f"DDG error: {e}") from e
                formatted = _format_ddg(results, qnorm)
                _DDG_CACHE[kstr] = (now + 300, formatted)
                if len(_DDG_CACHE) > 512:
                    _DDG_CACHE.popitem(last=False)
                _hit_val = copy.deepcopy(formatted)
    finally:
        # outside lock: evict lock conditionnel (R3 sans race) - always
        try:
            if not lock.locked() and not getattr(lock, "_waiters", None):
                _DDG_LOCKS.pop(kstr, None)
            if len(_DDG_LOCKS) > 512:
                oldest = next(iter(_DDG_LOCKS))
                _DDG_LOCKS.pop(oldest, None)
        except Exception:
            try:
                _DDG_LOCKS.pop(kstr, None)
            except Exception:
                pass
    return _hit_val


async def _execute_web_fetch(url: str, prompt: str = "", timeout: int = 15, max_bytes: int = 12000, via_vpn: bool = False) -> str:
    """Fetch URL with SSRF guard, redirect re-validation, content guards, sem."""
    # clamps Q8A
    try:
        timeout = max(5, min(30, int(timeout)))
    except Exception:
        timeout = 15
    try:
        max_bytes = max(2000, min(50000, int(max_bytes)))
    except Exception:
        max_bytes = 12000
    # SSRF initial - budgeted via outer wait_for, no inner wait_for
    if not await _is_safe_fetch_url(url):
        raise ValueError(f"SSRF rejected: {url}")
    proxy = get_socks5_proxy_url() if via_vpn else None
    async with FETCH_SEM:
        async with httpx.AsyncClient(follow_redirects=False, timeout=timeout, proxy=proxy) as c:
            r = await c.get(url, headers={"User-Agent": "opencode-proxy/1.0"})
            for _ in range(3):
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("location", "")
                    nxt = urllib.parse.urljoin(url, loc)
                    if not loc or not await _is_safe_fetch_url(nxt):
                        raise ValueError(f"SSRF redirect rejected: {loc}")
                    url = nxt
                    r = await c.get(url, headers={"User-Agent": "opencode-proxy/1.0"})
                else:
                    break
            # R4 guards
            ct = r.headers.get("content-type", "").split(";")[0].strip().lower()
            if ct and not (ct.startswith("text/") or "json" in ct or "xml" in ct):
                raise ValueError(f"Rejected Content-Type: {ct}")
            if int(r.headers.get("content-length", "0") or 0) > 5_000_000 or len(r.content) > 5_000_000:
                raise ValueError("Content too large")
            r.raise_for_status()
            html = r.text[: max_bytes * 3]
    # extraction to_thread
    try:
        import trafilatura

        extracted = await asyncio.to_thread(trafilatura.extract, html) or ""
    except ImportError:
        extracted = ""
    if not extracted:
        try:
            from bs4 import BeautifulSoup

            extracted = await asyncio.to_thread(lambda: BeautifulSoup(html, "html.parser").get_text(separator="\n", strip=True))
        except ImportError:
            extracted = re.sub(r"<[^>]+>", " ", html)
    extracted = extracted[:max_bytes].strip()
    return f"Content of {url} (extracted {len(extracted)} chars):\n{extracted}"


def _inject_as_user_prefix(body: dict, content: str, protocol: str, tag: str):
    """Inject as user prefix split protocol R5: anthropic pos0, openai pos1 if system."""
    block = f"<{tag}>\n{content}\n</{tag}>"
    msgs = body.get("messages", [])
    if not isinstance(msgs, list):
        msgs = []
        body["messages"] = msgs
    if protocol == "anthropic":
        pos = 0
    else:
        pos = 1 if msgs and isinstance(msgs[0], dict) and msgs[0].get("role") == "system" else 0
    if protocol == "anthropic":
        msgs.insert(pos, {"role": "user", "content": [{"type": "text", "text": block}]})
    else:
        msgs.insert(pos, {"role": "user", "content": block})
    body["messages"] = msgs


def _strip_web_tool(body: dict, protocol: str, name: str):
    """Remove web_* tool and forced tool_choice."""
    if "tools" in body and isinstance(body["tools"], list):
        body["tools"] = [t for t in body["tools"] if _normalize_tool_name(t) != name]
        if not body["tools"]:
            try:
                del body["tools"]
            except KeyError:
                pass
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        # check if tc references the tool being stripped
        tc_name = tc.get("name", "") or tc.get("function", {}).get("name", "")
        # also check type containing web_*
        tc_type = tc.get("type", "")
        is_target = False
        if _normalize_tool_name({"name": tc_name}) == name:
            is_target = True
        elif isinstance(tc_type, str) and name in tc_type:
            is_target = True
        # empty orphan -> auto
        if not isinstance(tc_name, str) or not tc_name.strip():
            if tc_type in ("tool", "function") and not tc_name.strip():
                _debug("  [convert] _strip_web_tool: empty tool_choice name → auto")
                body["tool_choice"] = "auto"
                return
        if is_target and tc.get("type") in ("tool", "function"):
            try:
                del body["tool_choice"]
            except KeyError:
                pass
        # also strip type web_* without name
        if isinstance(tc_type, str) and tc_type.startswith(name):
            try:
                del body["tool_choice"]
            except KeyError:
                pass


def _strip_web_search_tool(body: dict, protocol: str):
    return _strip_web_tool(body, protocol, "web_search")


def _is_web_tool_native(model_id: str) -> bool:
    """Check if model natively supports web_search (capabilities + allowlist)."""
    # allowlist fallback
    try:
        from config.settings import WEB_SEARCH_NATIVE_MODELS as _ALLOW
    except ImportError:
        _ALLOW = ["muse-spark-1.2-contributor", "muse-spark-1.2-contributor-free"]
    if model_id in _ALLOW:
        return True
    # capabilities live
    try:
        cfg = get_model_config(model_id)
        caps = cfg.get("capabilities") if isinstance(cfg, dict) else None
        if isinstance(caps, dict) and caps.get("web-search"):
            return True
        # also check dashboard/quota capabilities if available
        try:
            from dashboard.quota import get_model_capabilities_for_all  # type: ignore

            all_caps = get_model_capabilities_for_all()
            if isinstance(all_caps, dict):
                mc = all_caps.get(model_id, {})
                if isinstance(mc, dict) and mc.get("web-search"):
                    return True
        except Exception:
            pass
    except Exception:
        pass
    return False


async def _handle_web_search(body: dict, model_id: str, protocol: str) -> bool:
    """Handle web_search tool (Q1A auto + passthrough)."""
    # R6 kill-switch
    if not yaml_get("web_search", "enabled", True):
        if _has_tool(body, "web_search"):
            _strip_web_tool(body, protocol, "web_search")
            _log("  WEB SEARCH: disabled via config → stripped")
        return False
    if not _has_tool(body, "web_search"):
        return False
    # [plan v10 §14.4.15] branches mortes purgées : is_forced/is_auto étaient
    # calculés mais jamais lus (le passthrough Q4A utilise mode/target_model).
    # passthrough if native
    mode = yaml_get("web_search", "mode", "duckduckgo")
    target_model = yaml_get("web_search", "target_model", None)
    max_results = yaml_get("web_search", "max_results", 5)
    timeout = yaml_get("web_search", "timeout", 10)
    via_vpn = bool(yaml_get("web_search", "via_vpn", False))
    # validate target_model
    if target_model and target_model not in MODELS:
        _log(f"  WEB SEARCH: target_model {target_model!r} not in MODELS → ignore")
        target_model = None
    if mode not in ("duckduckgo", "model", "ddg_then_model", "model_then_ddg"):
        _log(f"  WEB SEARCH: invalid mode {mode!r} → strip")
        _strip_web_tool(body, protocol, "web_search")
        return False
    # passthrough decision Q4A
    is_native = _is_web_tool_native(model_id)
    if is_native:
        _log(f"  WEB SEARCH: passthrough to native model {model_id}")
        return False
    # non-native → local or model routing
    # if mode == model → route
    if mode == "model":
        if target_model and not _is_web_tool_native(target_model):
            _log(f"  WEB SEARCH: target {target_model} also non-native but routing anyway")
        if target_model:
            body["model"] = target_model
            _log(f"  WEB SEARCH: routed to {target_model} for web_search")
        else:
            _strip_web_tool(body, protocol, "web_search")
            _log("  WEB SEARCH: stripped (model mode but no target)")
        return False
    if mode == "model_then_ddg":
        if target_model:
            body["model"] = target_model
        _log("  WEB SEARCH: model-first for web_search (fallback=DDG)")
        return False
    # duckduckgo and ddg_then_model → local execution
    query = _extract_search_query(body)
    if not query.strip():
        _log("  WEB SEARCH: empty query → stripped")
        _strip_web_tool(body, protocol, "web_search")
        return False
    _debug(f"  [web-search] mode={mode} model={model_id} query={query[:80]}...")
    # clamp
    try:
        max_results = max(1, min(10, int(max_results)))
    except Exception:
        max_results = 5
    try:
        timeout = max(5, min(30, int(timeout)))
    except Exception:
        timeout = 10
    proxy = get_socks5_proxy_url() if via_vpn else None
    # execute with retry 1x TimeoutError
    for attempt in range(2):
        try:
            results = await _execute_ddg_search(query, max_results, timeout, proxy)
            # success
            _inject_as_user_prefix(body, results, protocol, "local_search_results")
            _strip_web_tool(body, protocol, "web_search")
            _log(f"  WEB SEARCH: DuckDuckGo query='{query[:60]}' → injected results")
            return False
        except TimeoutError:
            if attempt == 0:
                await asyncio.sleep(0.3)
                continue
            _log(f"  WEB SEARCH: DDG TimeoutError query='{query[:60]}' → stripped (on_error: strip)")
            _strip_web_tool(body, protocol, "web_search")
            return False
        except ImportError as e:
            _log(f"  WEB SEARCH: ImportError {e} → strip/fallback")
            if target_model and mode == "ddg_then_model":
                body["model"] = target_model
                _log(f"  WEB SEARCH: DDG import failed, routed to {target_model}")
            else:
                _strip_web_tool(body, protocol, "web_search")
            return False
        except Exception as e:
            # check if ddg_then_model fallback
            if mode == "ddg_then_model" and target_model:
                body["model"] = target_model
                _log(f"  WEB SEARCH: DDG failed ({e}), routed to {target_model}")
                return False
            _log(f"  WEB SEARCH: DDG error {type(e).__name__}: {e} → stripped")
            _strip_web_tool(body, protocol, "web_search")
            return False
    _strip_web_tool(body, protocol, "web_search")
    return False


def _extract_fetch_url(body: dict) -> str:
    """Extract URL for web_fetch Q2A: tool_use.input.url priority, else last user http."""
    # check tool_use input
    for msg in body.get("messages", []) or []:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and _normalize_tool_name(block) == "web_fetch":
                    url = block.get("input", {}).get("url", "")
                    if isinstance(url, str) and url.strip().startswith("http"):
                        return url.strip()
        # also check assistant tool_calls
        if msg.get("tool_calls"):
            for tc in msg.get("tool_calls", []):
                fn = tc.get("function", {})
                if _normalize_tool_name(fn) == "web_fetch":
                    try:
                        args = json.loads(fn.get("arguments", "{}"))
                        url = args.get("url", "")
                        if isinstance(url, str) and url.strip().startswith("http"):
                            return url.strip()
                    except Exception:
                        pass
    # fallback: last user message http
    for msg in reversed(body.get("messages", []) or []):
        if msg.get("role") == "user":
            c = msg.get("content", "")
            txt = ""
            if isinstance(c, str):
                txt = c
            elif isinstance(c, list):
                for b in c:
                    if isinstance(b, dict) and b.get("type") == "text":
                        txt = b.get("text", "")
                        break
            if txt:
                m = re.search(r"https?://\S+", txt)
                if m:
                    return m.group(0).strip().rstrip(".,)\"'")
    return ""


async def _handle_web_fetch(body: dict, model_id: str, protocol: str) -> bool:
    """Handle web_fetch Q2A URL-gated."""
    if not yaml_get("web_fetch", "enabled", True):
        if _has_tool(body, "web_fetch"):
            _strip_web_tool(body, protocol, "web_fetch")
            _log("  WEB FETCH: disabled via config → stripped")
        return False
    if not _has_tool(body, "web_fetch"):
        return False
    # [plan v10 §14.4.15] branche morte purgée : is_forced calculé jamais lu.
    # is_auto, lui, EST consommé par la décision URL-gated Q2A ci-dessous.
    is_auto = _is_tool_choice_auto(body)
    # URL-gated for auto
    url = _extract_fetch_url(body)
    prompt = ""
    # extract prompt from tool_use input if present
    for msg in body.get("messages", []) or []:
        content = msg.get("content", "")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use" and _normalize_tool_name(block) == "web_fetch":
                    prompt = block.get("input", {}).get("prompt", "") or ""
    if not url and is_auto:
        # Q2A: no URL in last user → passthrough (strip)
        _log("  WEB FETCH: auto but no URL found → stripped (Q2A)")
        _strip_web_tool(body, protocol, "web_fetch")
        return False
    if not url:
        # try extract from last user anyway for forced
        url = _extract_fetch_url(body)
        if not url:
            _log("  WEB FETCH: no URL → stripped")
            _strip_web_tool(body, protocol, "web_fetch")
            return False
    mode = yaml_get("web_fetch", "mode", "direct")
    target_model = yaml_get("web_fetch", "target_model", None)
    timeout = yaml_get("web_fetch", "timeout", 15)
    max_bytes = yaml_get("web_fetch", "max_bytes", 12000)
    via_vpn = bool(yaml_get("web_fetch", "via_vpn", False))
    if target_model and target_model not in MODELS:
        _log(f"  WEB FETCH: target_model {target_model!r} not in MODELS → ignore")
        target_model = None
    if mode not in ("direct", "model", "direct_then_model"):
        _log(f"  WEB FETCH: invalid mode {mode!r} → strip")
        _strip_web_tool(body, protocol, "web_fetch")
        return False
    # passthrough if native? For fetch, assume not native unless allowlist
    is_native = _is_web_tool_native(model_id)  # reuse same
    if is_native and mode != "direct":
        _log(f"  WEB FETCH: passthrough to native model {model_id}")
        return False
    if mode == "model":
        if target_model:
            body["model"] = target_model
            _log(f"  WEB FETCH: routed to {target_model}")
        else:
            _strip_web_tool(body, protocol, "web_fetch")
        return False
    # direct and direct_then_model → local
    try:
        timeout = max(5, min(30, int(timeout)))
    except Exception:
        timeout = 15
    try:
        max_bytes = max(2000, min(50000, int(max_bytes)))
    except Exception:
        max_bytes = 12000
    # outer wait_for budget unique
    async def _inner():
        return await _execute_web_fetch(url, prompt, timeout, max_bytes, via_vpn)

    try:
        content = await asyncio.wait_for(_inner(), timeout + 2)
        _inject_as_user_prefix(body, content, protocol, "local_fetch_content")
        _strip_web_tool(body, protocol, "web_fetch")
        _log(f"  WEB FETCH: fetched {url[:60]} → injected {len(content)} chars")
        return False
    except TimeoutError:
        _log(f"  WEB FETCH: TimeoutError url={url[:60]} → stripped")
        _strip_web_tool(body, protocol, "web_fetch")
        return False
    except Exception as e:
        _log(f"  WEB FETCH: error {type(e).__name__}: {e} → stripped (on_error: strip)")
        if mode == "direct_then_model" and target_model:
            body["model"] = target_model
            _log(f"  WEB FETCH: direct failed, routed to {target_model}")
            return False
        _strip_web_tool(body, protocol, "web_fetch")
        return False


# ── Protocol mapping (single source: protocol_mapping.py) ──
# This block was deduplicated: all conversions live in protocol_mapping.py
# (Phase 2 of plan api-error-400-http-enchanted-creek). Imported here to
# preserve 'from opencode import ...' compatibility for tests.
from protocol_mapping import (  # noqa: E402  # re-export after function defs for compat
    THINKING_MODELS,
    _anthropic_to_responses_request,
    _chat_to_responses_request,
    _drop_orphan_responses_input,
    _drop_orphan_tool_messages,
    _extract_cache_tokens,
    _extract_text,
    _json_dumps,
    _json_dumps_str,
    _json_loads,
    _local_signature,
    _responses_sse_to_chat_deltas,
    _responses_to_anthropic_response,
    _responses_to_chat_response,
    anthropic_to_openai,
    anthropic_to_openai_response,
    anthropic_to_openai_responses,
    openai_chat_to_responses,
    openai_responses_to_anthropic,
    openai_to_anthropic,
    openai_to_anthropic_request,
    strip_synthetic_thinking,
)


async def _finalize_and_close_stream(
    started,
    open_blocks,
    text_block_idx,
    reasoning_block_idx,
    tool_block_idx,
    stream_out_tokens,
    actual_usage,
    model_id,
    stream_in_est,
    msg_id,
    original_model,
    _token_usage,
    _token_lock,
    _using_free,
    _paid_model_id,
    free_model,
    start_time,
    req_id,
    _req_model_id,
    protocol,
    thinking_type,
    effort,
    client_ip,
    hdrs,
    tool_names,
    used_tools,
    request_body,
    log_tag="",
    reasoning_signature="",
):
    """Yield closing SSE events for a stream and persist the request.

    Extracted so both the [DONE] path and the Responses-API
    response.completed path can share the same finalization logic.
    """
    final_in, final_out, final_cache, log_tag = _finalize_stream_tokens(
        model_id, stream_in_est, None, stream_out_tokens, 0, actual_usage, _token_usage, _token_lock
    )
    if not started:
        started = True
        yield _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": msg_id,
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": original_model,
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {
                        "input_tokens": final_in,
                        "output_tokens": 0,
                        "cache_read_input_tokens": final_cache,
                    },
                },
            },
        )
    has_tools = bool(tool_block_idx)
    _debug(
        f"  [stream-oai] summary: text={text_block_idx is not None} thinking={reasoning_block_idx is not None} tools={list(tool_block_idx.keys())} stop={'tool_use' if has_tools else 'end_turn'} out_tokens={final_out}"
    )
    if reasoning_block_idx is None and thinking_type != "none":
        _debug(
            f"  [stream-oai] WARNING: thinking requested (type={thinking_type}, effort={effort}) but upstream returned no reasoning_content"
        )
    for idx in open_blocks:
        if idx == reasoning_block_idx and reasoning_signature:
            # [PLAN-raisonnement Phase C.1] signature locale AVANT le stop du
            # bloc thinking : ordre contractuel thinking_delta* → signature_delta
            # → content_block_stop (sinon les clients Anthropic-compatibles
            # abandonnent le bloc en multi-tours).
            yield _sse(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "signature_delta", "signature": reasoning_signature},
                },
            )
        yield _sse("content_block_stop", {"type": "content_block_stop", "index": idx})
    yield _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use" if has_tools else "end_turn"},
            "usage": {"output_tokens": final_out, "cache_read_input_tokens": final_cache},
        },
    )
    yield _sse("message_stop", {"type": "message_stop"})
    ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
    if _using_free:
        _log_free_model_usage(
            _paid_model_id,
            free_model,
            "free (no auth)",
            "free (no auth)",
            200,
            final_in or 0,
            final_out or 0,
            _elapsed_ms(start_time),
            ip=_free_usage_ip(),
        )
    await _save_and_log_request(
        req_id,
        _req_model_id,
        original_model,
        start_time,
        final_in,
        final_out,
        final_cache,
        protocol,
        True,
        thinking_type,
        effort,
        client_ip,
        ak_h,
        tool_names,
        log_tag,
        tools_used=used_tools if used_tools else None,
        request_body=request_body,
    )


def ensure_min_tokens(body: dict, default: int = None) -> dict:
    if default is None:
        default = yaml_get("thinking", "default_min_tokens", 256)
    """Ajuste max_output_tokens pour les modèles thinking afin qu'il
    reste des tokens pour la réponse après le reasoning."""
    model = body.get("model", "")
    min_tokens = default
    _debug(
        f"  [thinking] ensure_min_tokens: model={model!r} THINKING_MODELS={THINKING_MODELS} keys={list(body.keys())}"
    )
    for prefix, tokens in THINKING_MODELS.items():
        if model.startswith(prefix) or model == prefix:
            min_tokens = max(min_tokens, tokens)
            break
    current = body.get("max_output_tokens") or body.get("max_tokens")
    _debug(
        f"  [thinking] ensure_min_tokens: current={current} min_tokens={min_tokens} will_bump={current is not None and current < min_tokens}"
    )
    if current is not None and current < min_tokens:
        body["max_output_tokens"] = min_tokens
        # Also bump max_tokens — anthropic_to_openai() reads max_tokens, not max_output_tokens
        if "max_tokens" in body:
            body["max_tokens"] = min_tokens
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
    return [
        tc["function"]["name"]
        for tc in (msg.get("tool_calls") or [])
        if isinstance(tc, dict) and isinstance(tc.get("function"), dict)
    ]


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
    _debug(
        f"  [body] read {len(body_bytes)} bytes in {(time.monotonic() - start_time) * 1000:.0f}ms"
    )
    if len(body_bytes) > MAX_BODY_SIZE:
        _debug(f"  413: body too large ({len(body_bytes)} bytes)")
        return _anthropic_error(
            413, f"Request body too large ({len(body_bytes)} bytes, max {MAX_BODY_SIZE})"
        )

    try:
        body = _json_loads(body_bytes)
    except Exception:
        _debug("  400: invalid JSON body")
        return _anthropic_error(400, "invalid json")

    original_model = body.get("model", "")
    tool_names = _extract_tool_names(body)
    request_body = body  # Capture original request before mutation
    _debug(f"[messages] req_id={req_id} model={original_model!r} tools={tool_names} ip={client_ip}")

    # [fix 20/08] Compact request detection + diagnostics
    _thinking = body.get("thinking")
    _is_compact = isinstance(_thinking, dict) and _thinking.get("type") == "compact"
    _msg_count = len(body.get("messages", []))
    if _is_compact or _msg_count > 50 or len(body_bytes) > 500000:
        _log(
            f"  [compact?] req_id={req_id} model={original_model!r} thinking={_thinking} msgs={_msg_count} body={len(body_bytes)}B"
        )
    if DEBUG:
        _debug(f"[messages] headers={_sanitize_headers(dict(request.headers))}")
        _debug(f"[messages] body=\n{_redact(_truncate(body))}")
    route = _route_for(original_model)
    if route is None:
        _debug(f"[messages] ✗ no route found for {original_model!r}")
        available = sorted(MODELS.keys())
        return Response(
            content=_json_dumps_str(
                {"error": f"Model not found: {original_model!r}", "available_models": available}
            ),
            status_code=404,
            media_type="application/json",
        )
    model_id = route["model"]
    cfg = get_model_config(model_id)
    endpoint = cfg["endpoint"]
    protocol = cfg["protocol"]
    _debug(f"[messages] route: {original_model!r} → {model_id} | {protocol} | endpoint={endpoint}")

    body = dict(body)
    body["model"] = model_id

    body = ensure_min_tokens(body)

    # Apply custom route overrides for thinking/effort
    thinking_override = route.get("thinking")
    if thinking_override and thinking_override != "auto":
        if not isinstance(body.get("thinking"), dict):
            body["thinking"] = {}
        body["thinking"]["type"] = thinking_override
    effort_override = route.get("effort")
    if effort_override and effort_override != "auto":
        body["effort"] = effort_override

    # Tool filtering removed — all tools are forwarded as-is

    # Handle web_search / web_fetch (v3.3)
    await _handle_web_search(body, model_id, protocol)
    await _handle_web_fetch(body, model_id, protocol)

    # Extract thinking for logging
    thinking = body.get("thinking", {})
    thinking_type = thinking.get("type", "none") if isinstance(thinking, dict) else "none"
    effort = (
        body.get("effort")
        or (thinking.get("effort") if isinstance(thinking, dict) else None)
        or (
            body.get("output_config", {}).get("effort")
            if isinstance(body.get("output_config"), dict)
            else None
        )
        or "none"
    )

    _log(
        f"→ {original_model!r} → {model_id} | {protocol} | stream={body.get('stream', False)} | thinking={thinking_type} | effort={effort} | ip={client_ip}"
    )

    _geo_gate = await _enforce_geo_gate(
        route, request, is_stream=bool(body.get("stream", False)), protocol=protocol
    )
    if _geo_gate is not None:
        return _geo_gate

    # Circuit breaker check
    if not _cb_should_allow(endpoint):
        _log(f"  CIRCUIT BREAKER OPEN — fast-failing request to {endpoint}")
        return _anthropic_error(503, "Service temporarily unavailable (circuit breaker open)")

    # ── Anthropic pass-through ──────────────────────────────────
    if protocol == "anthropic":
        # [PLAN-raisonnement Phase D + correctif parité multi-tours] Strip
        # sélectif RÉSERVÉ à Anthropic direct : les blocs thinking signés
        # LOCALEMENT par le proxy (synthétisés depuis reasoning_content) ne lui
        # sont jamais envoyés — il valide cryptographiquement les signatures et
        # on ne lui ment pas. Vers les upstreams openai-compatibles, le texte
        # voyage tel quel en reasoning_content (parité avec l'usage direct —
        # les signatures n'y voyagent de toute façon jamais).
        try:
            _stripped_thinking = strip_synthetic_thinking(body)
            if _stripped_thinking:
                _log(
                    f"  [thinking] {_stripped_thinking} bloc(s) thinking à signature locale strippé(s) de l'historique (upstream anthropic)"
                )
        except Exception as e:
            _debug(f"  [thinking] strip_synthetic_thinking failed: {type(e).__name__}: {e}")

        is_stream = body.get("stream", False)

        # ── Free model: try BEFORE auth (free models don't need API keys) ──
        if not is_stream and FREE_MODEL_MAP.get(model_id):
            try:
                free_result = await _try_free_model_first(
                    body,
                    {},
                    "anthropic",
                    model_id,
                    forced_pool=getattr(request.state, "_geo_forced_pool", None),
                )
                if free_result is not None:
                    resp, _, _actual_model, _actual_ip = free_result
                    data = (
                        resp.json()
                        if resp.headers.get("content-type", "").startswith("application/json")
                        else {}
                    )
                    usage = data.get("usage", {})
                    req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                    req_out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                    req_cache = usage.get("cache_read_input_tokens", 0)
                    _update_token_usage(_actual_model, req_in, req_out, req_cache)
                    used = [
                        b["name"] for b in data.get("content", []) if b.get("type") == "tool_use"
                    ]
                    await _save_and_log_request(
                        req_id,
                        _actual_model,
                        original_model,
                        start_time,
                        req_in,
                        req_out,
                        req_cache,
                        protocol,
                        False,
                        thinking_type,
                        effort,
                        client_ip,
                        "free (no auth)",
                        tool_names,
                        tools_used=used,
                        request_body=request_body,
                        response_body=data,
                        free_model_ip=_actual_ip,
                    )
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

                return StreamingResponse(
                    _sse_keepalive(anthropic_stream_free_fallback()),
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                )
            retry_after = int(e.retry_after) + 1
            return Response(
                content=_json_dumps_str(
                    {
                        "type": "error",
                        "error": {
                            "type": "api_error",
                            "message": f"All API keys exhausted. Retry after {retry_after}s.",
                        },
                    }
                ),
                status_code=503,
                media_type="application/json",
                headers={"Retry-After": str(retry_after)},
            )

        if not is_stream:
            # Check cache
            cache_key = _response_cache.make_key(body, body_bytes=body_bytes)
            cached = cache_key and _response_cache.get(cache_key)
            if cached:
                cached_body, cached_headers = cached
                _debug(f"  cache HIT key={cache_key[:16]}… size={len(cached_body)} bytes")
                _log(f"  ← {model_id} | cache HIT")
                return Response(
                    content=cached_body,
                    headers={**cached_headers, "X-Cache": "HIT"},
                    media_type="application/json",
                )
            _debug(f"  cache MISS key={cache_key[:16] if cache_key else 'none'}…")

            # Try free model first if available
            _geo_tunnel = getattr(request.state, "_geo_force_tunnel", False)
            try:
                free_result = await _try_free_model_first(
                    body,
                    a_headers,
                    "anthropic",
                    model_id,
                    forced_pool=getattr(request.state, "_geo_forced_pool", None),
                )
                if free_result is not None:
                    resp, a_headers, _actual_model, _actual_ip = free_result
                    model_id = _actual_model  # Log as free model
                elif _geo_tunnel:
                    # Axe A: geo-restricted paid → must route through tunnel station
                    async with _open_via_pool(
                        endpoint,
                        body,
                        a_headers,
                        is_stream=False,
                        forced_pool=getattr(request.state, "_geo_forced_pool", None),
                    ) as resp:
                        a_headers = dict(resp.headers)
                else:
                    resp, a_headers = await _do_request_with_retry(
                        endpoint, body, a_headers, "anthropic"
                    )
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
                await _log_and_save_error(
                    req_id,
                    model_id,
                    original_model,
                    start_time,
                    resp.status_code,
                    resp.text,
                    protocol,
                    is_stream,
                    thinking_type,
                    effort,
                    client_ip,
                    account_alias,
                    tool_names,
                    request_body=request_body,
                    response_body={"error": resp.text[:2000]},
                )
                # Pause key on credit/balance errors (400) and retry with alt key
                if resp.status_code == 400 and any(
                    x in resp.text for x in ("Insufficient balance", "Monthly usage limit")
                ):
                    failed_key = a_headers.get("x-api-key", "")
                    _key_pauser.pause_key(
                        failed_key,
                        _key_pauser._max_pause,
                        f"400 credit error: {_redact(resp.text, 80)}",
                    )
                    alt = _find_alternative_key(failed_key)
                    if alt:
                        _log("  400 credit error on key, retrying with alternative key")
                        headers = {
                            "x-api-key": alt.get("api_key", ""),
                            "Content-Type": "application/json",
                            "anthropic-version": "2023-06-01",
                        }
                        try:
                            resp, headers = await _do_request_with_retry(
                                endpoint, body, headers, "anthropic"
                            )
                            if resp.status_code == 200:
                                account_alias = _alias_for_key(headers.get("x-api-key", ""))
                            else:
                                return _anthropic_error(
                                    503, "All API keys exhausted. Check your billing."
                                )
                        except UpstreamError as e:
                            return _anthropic_error(e.status_code, str(e))
                if resp.status_code in (429, 401, 403):
                    return _anthropic_error(503, msg)
                if resp.status_code == 499:
                    return _anthropic_error(502, msg)
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type="application/json",
                )
            try:
                data = (
                    resp.json()
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else {}
                )
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
            await _save_and_log_request(
                req_id,
                model_id,
                original_model,
                start_time,
                req_in,
                req_out,
                req_cache,
                protocol,
                is_stream,
                thinking_type,
                effort,
                client_ip,
                account_alias,
                tool_names,
                tools_used=used,
                request_body=request_body,
                response_body=data,
            )
            if cache_key:
                _response_cache.put(cache_key, resp.content, {"Content-Type": "application/json"})
            # success headers (minimized X-Geo-*)
            _succ_geo = {}
            try:
                _pinned = bool(getattr(request.state, "_geo_pinned", False))
                _cur = getattr(request.state, "_geo_current", None)
                _geo_has = isinstance(route, dict) and isinstance(route.get("geo"), dict)
                _geo_enabled = bool(getattr(_cfg_settings, "GEO_ENABLED", False))
                if _geo_has and _geo_enabled:
                    _succ_geo = _geo_headers(route, pinned=_pinned, current_country=_cur)
            except Exception:
                pass
            return Response(
                content=resp.content,
                headers={"X-Cache": "MISS", **_succ_geo},
                media_type="application/json",
            )

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
                paid_endpoint = endpoint
                paid_body = dict(body)
                paid_body["model"] = model_id
                body = dict(body)
                body["model"] = free_model
                endpoint = _cfg_settings._free_endpoint_for(free_model)
                if API_BASE_FREE != _cfg_settings.API_BASE_FREE:
                    if "/responses" in endpoint:
                        endpoint = (
                            API_BASE_FREE.replace("/chat/completions", "/responses").replace(
                                "/v1/chat/completions", "/v1/responses"
                            )
                            if "/chat/completions" in API_BASE_FREE
                            else endpoint
                        )
                    else:
                        endpoint = API_BASE_FREE
                _using_free = True
                _track_model = free_model
            else:
                _using_free = False
            # Defer token estimation — [v10 PLAN-commun 1.3] TTFB : l'estimation
            # tiktoken tourne en TÂCHE concurrente avec la connexion upstream ;
            # le premier yield n'attend plus le comptage. Résolue paresseusement
            # au premier besoin réel (usage upstream / finalisation).
            est_input = None

            async def _ensure_est_input():
                nonlocal est_input
                if est_input is None:
                    est_input = await asyncio.to_thread(_estimate_input_tokens, body)
                    _debug(f"  [stream] est_input={est_input}")
                    _update_token_usage(_track_model, est_input, 0, 0)
                return est_input

            _est_task = asyncio.create_task(_ensure_est_input())

            stream_in = None
            stream_out = stream_cache = 0
            started = False
            open_blocks = []
            _yielded = False
            stop_reason = "end_turn"
            emitted_finish = False
            _handle_429 = _make_stream_retry_loop("anthropic")
            _geo_tunnel = getattr(request.state, "_geo_force_tunnel", False)
            _free_forced_pool = getattr(request.state, "_geo_forced_pool", None)
            _free_bound = yaml_get("streaming", "retry_attempts", 2)
            if _using_free and _free_attempts_active(_free_forced_pool):
                # [plan 19/08 §1] free multi-attempt: extra free strikes on
                # fresh stations; paid retry budget preserved
                # (max_free_attempts=1 ⇒ exact legacy behaviour).
                _free_bound += max(0, effective_free_max_attempts(_free_forced_pool) - 1)
            # [E1 perf] hoisté hors de la boucle par chunk (dict lookups/chunk sinon)
            _line_buf_max = yaml_get("streaming", "line_buffer_max", 1_000_000)
            for _attempt in range(_free_bound):
                used_tools = []  # Reset on each retry attempt
                _line_buf = ""  # fresh per attempt — avoids stale truncated data: re-parse
                _debug(f"  [stream] attempt {_attempt + 1}/{_free_bound}")
                try:
                    # Axe A: geo-restricted paid streaming → route through tunnel station
                    _stream_ctx = (
                        _open_via_pool(
                            endpoint, body, headers, is_stream=True, forced_pool=_free_forced_pool
                        )
                        if _geo_tunnel
                        else _open_free_stream(
                            endpoint,
                            body,
                            headers,
                            _using_free,
                            count_request=(_attempt == 0),
                            fresh_station=(_attempt > 0 and _using_free),
                            direct_fallback=(
                                _free_exception_fallback_mode() != "station-first"
                                or _attempt + 1 >= effective_free_max_attempts(_free_forced_pool)
                            ),
                            forced_pool=_free_forced_pool,
                        )
                    )
                    async with _stream_ctx as resp:
                        _debug(f"  [stream] connected status={resp.status_code}")
                        if resp.status_code != 200:
                            if _using_free:
                                # Free endpoint non-200 (429 quota, 5xx...) → cooldown
                                # the free model, fall back to paid. Never pauses PAID
                                # keys: a status from the free endpoint says nothing
                                # about the paid account (CRITIC(2)/CRITIC(3)).
                                if resp.status_code == 429:
                                    _refuse = _on_free_429_stream(
                                        free_model,
                                        resp.headers.get("retry-after", ""),
                                        forced_pool=_free_forced_pool,
                                    )
                                    if not _refuse and _attempt + 1 < effective_free_max_attempts(
                                        _free_forced_pool
                                    ):
                                        # [plan 19/08 §1] budget left → retry free on a
                                        # FRESH station (fresh IP = fresh quota); keep
                                        # _using_free so the next attempt re-enters free
                                        # with fresh_station=True. Cooldown + rotation
                                        # for the exhausted IP already done above.
                                        _log_free_model_usage(
                                            model_id,
                                            free_model,
                                            "free (no auth)",
                                            "free (no auth)",
                                            resp.status_code,
                                            ip=_free_usage_ip(),
                                        )
                                        _log(
                                            f"  FREE {free_model!r} RATE LIMITED (429) → retry station fraîche (essai {_attempt + 2}/{effective_free_max_attempts(_free_forced_pool)})"
                                        )
                                        if _stream_has_yielded(
                                            started, open_blocks, stream_out, _line_buf
                                        ):
                                            _cb_record_failure(endpoint)
                                            _log(
                                                f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                            )
                                            async for ev in _terminate_after_started(
                                                open_blocks, stream_out
                                            ):
                                                yield ev
                                            return
                                        continue
                                else:
                                    _set_free_cooldown(free_model, 60, _free_attempt_station())
                                    _refuse = False
                                body = paid_body
                                endpoint = paid_endpoint
                                _using_free = False
                                _track_model = model_id  # Revert tracking to paid model
                                _debug(
                                    f"  [stream] free model {resp.status_code} → falling back to paid {_track_model!r}"
                                )
                                _log(
                                    f"  FREE model {resp.status_code} → falling back to paid {_track_model!r}"
                                )
                                _log_free_model_usage(
                                    model_id,
                                    free_model,
                                    "free (no auth)",
                                    "free (no auth)",
                                    resp.status_code,
                                    ip=_free_usage_ip(),
                                )
                                if _refuse:
                                    # strict_free (GUI): every station exhausted
                                    # (bad/down + (model, IP) cooldown active) —
                                    # refuse instead of paying.
                                    _retry_after = resp.headers.get("retry-after", "") or "60"
                                    yield await _stream_error_response(
                                        req_id,
                                        free_model,
                                        original_model,
                                        start_time,
                                        429,
                                        await resp.aread(),
                                        protocol,
                                        thinking_type,
                                        effort,
                                        client_ip,
                                        "free (no auth)",
                                        tool_names,
                                        {
                                            "type": "error",
                                            "error": {
                                                "type": "rate_limit_error",
                                                "message": f"Free quota exhausted on all VPN stations. Retry after {_retry_after}s.",
                                            },
                                        },
                                        request_body=request_body,
                                    )
                                    return
                                continue
                            headers, should_retry = await _handle_429(
                                headers, resp.status_code, _attempt, resp.headers
                            )
                            if should_retry:
                                _debug("  [stream] 429 retry, key swapped")
                                if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                                    _cb_record_failure(endpoint)
                                    _log(
                                        f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                    )
                                    async for ev in _terminate_after_started(
                                        open_blocks, stream_out
                                    ):
                                        yield ev
                                    return
                                continue
                            if resp.status_code == 499:
                                wait = 1.0 * (2**_attempt)
                                _debug(
                                    f"  [stream] upstream 499, retrying in {wait:.1f}s (attempt {_attempt + 1})"
                                )
                                await asyncio.sleep(wait)
                                if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                                    _cb_record_failure(endpoint)
                                    _log(
                                        f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                    )
                                    async for ev in _terminate_after_started(
                                        open_blocks, stream_out
                                    ):
                                        yield ev
                                    return
                                continue
                            err = await resp.aread()
                            # Pause key on credit/balance errors (400)
                            if resp.status_code == 400 and any(
                                x in err.decode(errors="ignore")
                                for x in ("Insufficient balance", "Monthly usage limit")
                            ):
                                failed_key = headers.get("x-api-key", "")
                                _key_pauser.pause_key(
                                    failed_key, _key_pauser._max_pause, "400 credit error"
                                )
                                alt = _find_alternative_key(failed_key)
                                if alt:
                                    _log(
                                        "  400 credit error on key, retrying with alternative key"
                                    )
                                    headers = {
                                        "x-api-key": alt.get("api_key", ""),
                                        "Content-Type": "application/json",
                                        "anthropic-version": "2023-06-01",
                                    }
                                    if _stream_has_yielded(
                                        started, open_blocks, stream_out, _line_buf
                                    ):
                                        _cb_record_failure(endpoint)
                                        _log(
                                            f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                        )
                                        async for ev in _terminate_after_started(
                                            open_blocks, stream_out
                                        ):
                                            yield ev
                                        return
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
                            error_payload = {
                                "type": "error",
                                "error": {"type": "api_error", "message": err_msg},
                            }
                            # DB/console gardent le vrai statut upstream (resp.status_code)
                            yield await _stream_error_response(
                                req_id,
                                _track_model,
                                original_model,
                                start_time,
                                resp.status_code,
                                err,
                                protocol,
                                thinking_type,
                                effort,
                                client_ip,
                                ak,
                                tool_names,
                                error_payload,
                                request_body=request_body,
                            )
                            return
                        async for chunk in resp.aiter_bytes():
                            yield chunk
                            _line_buf += chunk.decode("utf-8", errors="replace")
                            if len(_line_buf) > _line_buf_max:
                                # Truncate on newline boundary to avoid splitting JSON (fix truncation mid-JSON)
                                _keep = 1000
                                _tail = _line_buf[-_keep:]
                                _nl = _tail.find("\n")
                                if _nl != -1:
                                    _line_buf = _tail[_nl + 1 :]
                                else:
                                    _debug(
                                        f"  [stream] line_buf truncated mid-JSON (no newline in last {_keep})"
                                    )
                                    _line_buf = _tail
                            while "\n" in _line_buf:
                                line, _line_buf = _line_buf.split("\n", 1)
                                line = line.strip()
                                if not line.startswith("data:"):
                                    continue
                                data_str = line[5:].strip()
                                if data_str == "[DONE]":
                                    _debug("  [stream] [DONE] received")
                                    continue
                                try:
                                    event = _json_loads(data_str)
                                except Exception:
                                    continue
                                etype = event.get("type", "")
                                if etype == "message_start":
                                    usage = event.get("message", {}).get("usage", {})
                                    stream_in = usage.get("input_tokens")
                                    _debug(
                                        f"  [stream] message_start: input_tokens={stream_in} cache_read={usage.get('cache_read_input_tokens', 0)}"
                                    )
                                    started = True
                                    stream_cache = usage.get("cache_read_input_tokens", 0)
                                    # Consolidated: single lock acquisition for input + cache update
                                    if stream_in is not None or stream_cache:
                                        try:
                                            # [E4 perf] l'await reste HORS du
                                            # threading.Lock — un await sous
                                            # verrou bloquant peut geler le loop.
                                            est_input = await _est_task
                                            with _token_lock:
                                                if stream_in is not None:
                                                    _token_usage[_track_model]["input"] += (
                                                        stream_in - est_input
                                                    )
                                                if stream_cache:
                                                    _token_usage[_track_model]["cache"] += (
                                                        stream_cache
                                                    )
                                        except Exception as e:
                                            _debug(
                                                f"  ✗ token rollback failed: {type(e).__name__}: {e}"
                                            )
                                elif etype == "content_block_start":
                                    block = event.get("content_block", {})
                                    _debug(
                                        f"  [stream] content_block_start: type={block.get('type')} name={block.get('name', '')} index={event.get('index')}"
                                    )
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
                                    stop_reason = event.get("delta", {}).get(
                                        "stop_reason", stop_reason
                                    )
                                    _debug(
                                        f"  [stream] message_delta: stop_reason={stop_reason} output_tokens={stream_out}"
                                    )
                                elif etype == "message_stop":
                                    emitted_finish = True
                                    _debug(
                                        "  [stream] message_stop received → emitted_finish=True"
                                    )
                        # After stream ends, handle truncated stream (EOF without message_stop)
                        # Check remaining buffer for a final event before synthesis
                        if _line_buf.strip() and not emitted_finish:
                            _rem = _line_buf.strip()
                            if _rem.startswith("data:"):
                                _rem = _rem[5:].strip()
                            try:
                                _ev = _json_loads(_rem)
                                if isinstance(_ev, dict) and _ev.get("type") in (
                                    "message_stop",
                                    "message_delta",
                                ):
                                    # If we have a buffered final event, consider it emitted
                                    if _ev.get("type") == "message_stop" or _ev.get(
                                        "delta", {}
                                    ).get("stop_reason"):
                                        emitted_finish = True
                            except Exception:
                                pass
                        # After stream ends, apply final output token count
                        if stream_out:
                            try:
                                with _token_lock:
                                    _token_usage[_track_model]["output"] += stream_out
                            except Exception:
                                pass
                        _cb_record_success(endpoint)  # Stream completed successfully
                except asyncio.CancelledError:
                    # [plan 18/08 §am.22/piège 19] a free stream cancelled by the
                    # watchdog (egress_dead CONFIRMED on its station) IS a network
                    # failure of that tunnel — redirect into the failover: the
                    # (_attempt>0) re-entry runs fresh_station=True →
                    # on_disconnect_retry → the bad-marked dead station is
                    # excluded, the retry lands on another station (or direct).
                    # Genuine client disconnects arrive the same way (uvicorn
                    # cancels the request task), but their tasks were never
                    # registered → re-raised unchanged. Attempts exhausted →
                    # honest error (client-side retry) instead of a dead re-strike.
                    st = _free_attempt_station()
                    if st is not None and _attempt == 0 and _is_watchdog_cancelled(st):
                        _debug(
                            f"  ⟳ stream watchdog-cancelled (dead tunnel, station {getattr(st, '_station', '?')}) — failover retry"
                        )
                        _log(
                            "  FREE STREAM on confirmed-dead tunnel cancelled → switching station"
                        )
                        if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                            _cb_record_failure(endpoint)
                            _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                            async for ev in _terminate_after_started(open_blocks, stream_out):
                                yield ev
                            return
                        continue
                    raise
                except _FreeTunnelFailure as ftf:
                    # [plan 19/08 §2] station-first: dead tunnel → retry free
                    # on a FRESH station (fresh IP = fresh quota) before any
                    # direct residential fallback. _using_free stays True →
                    # next attempt re-enters free with fresh_station=True
                    # (bad-mark → exclusion of the dead station).
                    _cb_record_failure(endpoint)  # Record failure for circuit breaker
                    _log(
                        f"  FREE STREAM via station {getattr(ftf.station, '_station', '?')} tunnel FAILED ({ftf.cause}) → retry station fraîche"
                    )
                    if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                        _cb_record_failure(endpoint)
                        _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                        async for ev in _terminate_after_started(open_blocks, stream_out):
                            yield ev
                        return
                    continue
                except Exception as e:
                    _cb_record_failure(endpoint)  # Record failure for circuit breaker
                    ak = _alias_for_key(headers.get("x-api-key", "")) if headers else ""
                    _debug(
                        f"  [stream] exception on attempt {_attempt + 1}: {type(e).__name__}: {e}"
                    )
                    _log(f"  ERROR stream (attempt {_attempt + 1}): {type(e).__name__}: {e}")
                    if _attempt == 0:
                        # Network errors (server disconnect, timeout) → retry with same key first
                        _is_network_error = (
                            isinstance(
                                e,
                                (
                                    httpx.RemoteProtocolError,
                                    httpx.ReadError,
                                    ConnectionError,
                                    OSError,
                                ),
                            )
                            or "disconnected" in str(e).lower()
                        )
                        if _is_network_error:
                            _debug("  ⟳ stream retry (network error, same key)")
                            _log("  Retrying stream (network error, same key)")
                            await asyncio.sleep(1.0)
                            if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                                _cb_record_failure(endpoint)
                                _log(
                                    f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                )
                                async for ev in _terminate_after_started(open_blocks, stream_out):
                                    yield ev
                                return
                            continue
                        # Auth/quota errors → try alternative key
                        failed_key = _key_from_headers(headers, "anthropic")
                        if not _key_pauser.is_paused(failed_key):
                            try:
                                await _pause_key_for_quota_reset(failed_key)
                            except Exception:
                                _default_pause = float(yaml_get("key_pause", "default_pause", 60))
                                _key_pauser.pause_key(
                                    failed_key, _default_pause, "stream exception"
                                )
                        alt = _find_alternative_key(failed_key)
                        if alt:
                            _debug(f"  ⟳ stream retry with alt key: alias={alt.get('alias', '?')}")
                            _log(f"  Retrying stream with alternative key: {alt.get('alias', '?')}")
                            headers = _get_auth_headers("anthropic", entry=alt)
                        if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                            _cb_record_failure(endpoint)
                            _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                            async for ev in _terminate_after_started(open_blocks, stream_out):
                                yield ev
                            return
                        continue
                    # Consolidated: single lock acquisition for error-path token adjustments
                    if stream_in is None or stream_out:
                        try:
                            with _token_lock:
                                if stream_in is None:
                                    est_input = await _est_task
                                    _token_usage[_track_model]["input"] -= est_input
                                if stream_out:
                                    _token_usage[_track_model]["output"] += stream_out
                        except Exception as e:
                            _debug(f"  ✗ token rollback failed: {type(e).__name__}: {e}")
                    est_input = await _est_task
                    await _save_request(
                        req_id,
                        _track_model,
                        original_model,
                        _elapsed_ms(start_time),
                        stream_in if stream_in is not None else est_input,
                        stream_out,
                        stream_cache,
                        success=False,
                        error=str(e),
                        protocol=protocol,
                        is_stream=True,
                        thinking=thinking_type,
                        effort=effort,
                        client_ip=client_ip,
                        account_alias=ak,
                        tools=tool_names,
                        tools_used=used_tools if used_tools else None,
                        request_body=request_body,
                    )
                    if started:
                        for idx in open_blocks:
                            yield _sse(
                                "content_block_stop", {"type": "content_block_stop", "index": idx}
                            )
                        yield _sse(
                            "message_delta",
                            {
                                "type": "message_delta",
                                "delta": {"stop_reason": "error"},
                                "usage": {"output_tokens": stream_out},
                            },
                        )
                        yield _sse("message_stop", {"type": "message_stop"})
                    return
                else:
                    # Only reached if no exception and no break (successful stream)
                    break
            else:
                # Exhausted retries without success → error already yielded
                return
            # Fix: guarantee finish_reason on truncated stream (EOF without message_stop)
            if started and not emitted_finish:
                _debug("  [stream] truncated without finish_reason → synthesizing stop")
                _log("  stream truncated without finish_reason → synthesizing stop")
                async for ev in _terminate_after_started(open_blocks, stream_out):
                    yield ev
                emitted_finish = True
            est_input = await _est_task
            logged_in = stream_in if stream_in is not None else est_input
            _debug(
                f"  [stream] done: in={logged_in} out={stream_out} cache={stream_cache} tools={used_tools}"
            )
            if _using_free:
                _log_free_model_usage(
                    model_id,
                    free_model,
                    "free (no auth)",
                    "free (no auth)",
                    200,
                    logged_in or 0,
                    stream_out or 0,
                    _elapsed_ms(start_time),
                    ip=_free_usage_ip(),
                )
            if stream_in is not None or stream_out:
                ak = _alias_for_key(headers.get("x-api-key", ""))
                await _save_and_log_request(
                    req_id,
                    _track_model,
                    original_model,
                    start_time,
                    logged_in,
                    stream_out,
                    stream_cache,
                    protocol,
                    True,
                    thinking_type,
                    effort,
                    client_ip,
                    ak,
                    tool_names,
                    tools_used=used_tools if used_tools else None,
                    request_body=request_body,
                )

        # For streaming: if free model exists, pass empty headers (free models don't need auth)
        _stream_headers = a_headers if a_headers.get("x-api-key") else {}
        return StreamingResponse(
            _sse_keepalive(anthropic_stream(_stream_headers)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # ── OpenAI-protocol ─────────────────────────────────────────
    try:
        # [C1 perf] raw bytes disponibles → clé de cache sans re-dumps
        oai_body = anthropic_to_openai(body, model_id, raw=body_bytes)
        # Convert to Responses API format if endpoint requires it (muse-spark)
        if "/responses" in endpoint:
            oai_body = _chat_to_responses_request(oai_body)
        _debug(f"[messages] converted to openai: {_redact(_truncate(oai_body, 2000))}")
        # ── Orphan guard (handler-level, plan api-error-400) ──
        if isinstance(oai_body, dict):
            if "messages" in oai_body:
                oai_body["messages"] = _drop_orphan_tool_messages(oai_body["messages"])
            elif "input" in oai_body:
                oai_body["input"] = _drop_orphan_responses_input(oai_body["input"])
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
                free_result = await _try_free_model_first(
                    oai_body,
                    {},
                    "openai",
                    model_id,
                    forced_pool=getattr(request.state, "_geo_forced_pool", None),
                )
                if free_result is not None:
                    resp, _, _actual_model, _actual_ip = free_result
                    data = (
                        resp.json()
                        if resp.headers.get("content-type", "").startswith("application/json")
                        else {}
                    )
                    usage = data.get("usage", {})
                    req_in = usage.get("prompt_tokens", 0)
                    req_out = usage.get("completion_tokens", 0)
                    cache = _extract_cache_tokens(usage)
                    _update_token_usage(_actual_model, req_in, req_out, cache)
                    used = _extract_usage_tool_names(data)
                    await _save_and_log_request(
                        req_id,
                        _actual_model,
                        original_model,
                        start_time,
                        req_in,
                        req_out,
                        cache,
                        protocol,
                        False,
                        thinking_type,
                        effort,
                        client_ip,
                        "free (no auth)",
                        tool_names,
                        tools_used=used,
                        request_body=request_body,
                        response_body=data,
                        free_model_ip=_actual_ip,
                    )
                    return Response(
                        content=_json_dumps_str(
                            openai_to_anthropic(data, original_model), ensure_ascii=False
                        ),
                        media_type="application/json",
                    )
            except FreeQuotaExhausted as e:
                return _free_quota_exhausted_response(e, "openai")
            except Exception as e:
                _debug(f"  [free] free model attempt failed: {e}")
        retry_after = int(e.retry_after) + 1
        return Response(
            content=_json_dumps_str(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"All API keys exhausted. Retry after {retry_after}s.",
                    },
                }
            ),
            status_code=503,
            media_type="application/json",
            headers={"Retry-After": str(retry_after)},
        )
    is_stream = oai_body["stream"]

    if not is_stream:
        # Cache check for OpenAI-protocol via /v1/messages (mirrors anthropic branch)
        cache_key = _response_cache.make_key(body, body_bytes=body_bytes)
        cached = cache_key and _response_cache.get(cache_key)
        if cached:
            cached_body, cached_headers = cached
            _debug(
                f"  cache HIT key={cache_key[:16]}… size={len(cached_body)} bytes (openai via messages)"
            )
            _log(f"  ← {model_id} | cache HIT")
            return Response(
                content=cached_body,
                headers={**cached_headers, "X-Cache": "HIT"},
                media_type="application/json",
            )
        _debug(f"  cache MISS key={cache_key[:16] if cache_key else 'none'}… (openai via messages)")
        # Try free model first if available
        _geo_tunnel = getattr(request.state, "_geo_force_tunnel", False)
        try:
            free_result = await _try_free_model_first(
                oai_body,
                headers,
                "openai",
                model_id,
                forced_pool=getattr(request.state, "_geo_forced_pool", None),
            )
            if free_result is not None:
                resp, headers, _actual_model, _actual_ip = free_result
                model_id = _actual_model
            elif _geo_tunnel:
                # Axe A: geo-restricted paid → must route through tunnel station
                async with _open_via_pool(
                    endpoint,
                    oai_body,
                    headers,
                    is_stream=False,
                    forced_pool=getattr(request.state, "_geo_forced_pool", None),
                ) as resp:
                    headers = dict(resp.headers)
            else:
                resp, headers = await _do_request_with_retry(endpoint, oai_body, headers, "openai")
        except FreeQuotaExhausted as e:
            return _free_quota_exhausted_response(e, "openai")
        except UpstreamError as e:
            _debug(f"  ✗ upstream error: {e}")
            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
        account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
        _debug(f"  response status={resp.status_code} size={len(resp.content)} bytes")
        _debug(f"  response status={resp.status_code} size={len(resp.content)} bytes")
        # [Correctif parité multi-tours] Retry-once défensif : si l'upstream
        # /responses rejette les items reasoning synthétiques (400/422),
        # retenter UNE fois sans eux plutôt que de casser le tour entier
        # (texte + tool calls) pour tous les clients routés sur ce endpoint.
        if (
            resp.status_code in (400, 422)
            and isinstance(oai_body, dict)
            and oai_body.get("input") is not None
            and oai_body.pop("_has_synthetic_reasoning_items", False)
        ):
            _pre = len(oai_body["input"])
            oai_body["input"] = [
                i for i in oai_body["input"] if not (isinstance(i, dict) and i.get("type") == "reasoning")
            ]
            _log(
                f"  [thinking] upstream {resp.status_code} avec items reasoning → retry sans ({_pre}→{len(oai_body['input'])} items)"
            )
            try:
                resp, headers = await _do_request_with_retry(endpoint, oai_body, headers, "openai")
            except UpstreamError as e:
                return JSONResponse(status_code=e.status_code, content={"error": str(e)})
            _debug(f"  [retry-no-reasoning] response status={resp.status_code}")
        if resp.status_code != 200:
            await _log_and_save_error(
                req_id,
                model_id,
                original_model,
                start_time,
                resp.status_code,
                resp.text,
                protocol,
                is_stream,
                thinking_type,
                effort,
                client_ip,
                account_alias,
                tool_names,
                request_body=request_body,
                response_body={"error": resp.text[:2000]},
            )
            # Pause key on credit/balance errors (400)
            if resp.status_code == 400 and any(
                x in resp.text for x in ("Insufficient balance", "Monthly usage limit")
            ):
                failed_key = _key_from_headers(headers, "openai")
                _key_pauser.pause_key(
                    failed_key, _key_pauser._max_pause, f"400 credit error: {resp.text[:80]}"
                )
                alt = _find_alternative_key(failed_key)
                if alt:
                    _log("  400 credit error on key, retrying with alternative key")
                    headers = _get_auth_headers("openai", entry=alt)
                    # Retry once with alt key
                    try:
                        resp, headers = await _do_request_with_retry(
                            endpoint, oai_body, headers, "openai"
                        )
                        if resp.status_code == 200:
                            account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
                        else:
                            return _anthropic_error(
                                503, "All API keys exhausted. Check your billing."
                            )
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
            anthro_err = _json_dumps_str(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"HTTP {resp.status_code}: {err_msg}",
                    },
                },
                ensure_ascii=False,
            )
            return Response(
                content=anthro_err, status_code=resp.status_code, media_type="application/json"
            )
        try:
            data = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
        except Exception:
            _debug(f"  ✗ non-JSON response from {endpoint}")
            _log(f"  UPSTREAM DECODE ERROR: non-JSON response from {endpoint}")
            return _anthropic_error(502, "Upstream returned non-JSON response")
        # Detect Responses API format (has "output" key) vs Chat Completions (has "choices" key)
        is_responses_format = "output" in data and "choices" not in data
        usage = data.get("usage", {})
        if is_responses_format:
            req_in = usage.get("input_tokens", 0)
            req_out = usage.get("output_tokens", 0)
            _inp_det = (
                usage.get("input_tokens_details")
                if isinstance(usage.get("input_tokens_details"), dict)
                else {}
            )
            cache = _inp_det.get("cached_tokens", 0)
        else:
            req_in = usage.get("prompt_tokens", 0)
            req_out = usage.get("completion_tokens", 0)
            cache = _extract_cache_tokens(usage)
        _debug(
            f"  usage: in={req_in} out={req_out} cache={cache} format={'responses' if is_responses_format else 'chat'}"
        )
        _update_token_usage(model_id, req_in, req_out, cache)
        if is_responses_format:
            _debug(
                f"  [non-stream] Responses API output items: {[(item.get('type'), list(item.keys())[:6]) for item in data.get('output', []) if isinstance(item, dict)]}"
            )
            used = [
                b["name"]
                for b in data.get("output", [])
                if isinstance(b, dict) and b.get("type") == "function_call"
            ]
            # Check for reasoning in Responses API format
            has_reasoning = any(
                isinstance(item, dict) and item.get("type") == "reasoning"
                for item in data.get("output", [])
            )
            if not has_reasoning and thinking_type != "none":
                _debug(
                    f"  [non-stream] WARNING: thinking requested (type={thinking_type}, effort={effort}) but upstream returned no reasoning in Responses API response"
                )
            anthro_resp = _responses_to_anthropic_response(data, original_model)
        else:
            used = _extract_usage_tool_names(data)
            msg_data = ((data.get("choices") or [{}])[0] or {}).get("message") or {}
            if (
                not (msg_data.get("reasoning_content") or msg_data.get("reasoning"))
                and thinking_type != "none"
            ):
                _debug(
                    f"  [non-stream] WARNING: thinking requested (type={thinking_type}, effort={effort}) but upstream returned no reasoning_content"
                )
            _debug(
                f"  [non-stream] blocks: text={bool(msg_data.get('content'))} thinking={bool(msg_data.get('reasoning_content') or msg_data.get('reasoning'))} tools={used}"
            )
            anthro_resp = openai_to_anthropic(data, original_model)
        await _save_and_log_request(
            req_id,
            model_id,
            original_model,
            start_time,
            req_in,
            req_out,
            cache,
            protocol,
            is_stream=False,
            thinking_type=thinking_type,
            effort=effort,
            client_ip=client_ip,
            account_alias=account_alias,
            tools=tool_names,
            tools_used=used if used else None,
            request_body=request_body,
            response_body=data,
        )
        anthro_bytes = _json_dumps_str(anthro_resp, ensure_ascii=False).encode()
        if cache_key:
            _response_cache.put(cache_key, anthro_bytes, {"Content-Type": "application/json"})
        return Response(
            content=anthro_bytes, headers={"X-Cache": "MISS"}, media_type="application/json"
        )

    # Streaming
    msg_id = _fast_id("msg")
    # stream_options is a Chat Completions API field; Responses API reports
    # usage via the response.completed SSE event instead.
    if "/responses" not in endpoint:
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
            endpoint = _cfg_settings._free_endpoint_for(free_model)
            # Convert body to Responses API format if endpoint requires it
            if "/responses" in endpoint:
                if protocol == "anthropic":
                    oai_body = _anthropic_to_responses_request({**body, "model": free_model})
                else:
                    # [fix 20/08] Use oai_body (OpenAI chat format), not body (Anthropic format)
                    oai_body = _chat_to_responses_request({**oai_body, "model": free_model})
                oai_body = ensure_min_tokens(oai_body)
                oai_body["stream"] = True
            _req_model_id = free_model
            _using_free = True
        else:
            _using_free = False
        # [B1 perf / v10 PLAN-commun 1.3] estimation tiktoken DIFFÉRÉE : elle
        # tourne en tâche concurrente avec la connexion upstream — le premier
        # yield n'attend plus le comptage (−20 à −150 ms de TTFB sur gros
        # contexte). Résolue paresseusement au premier besoin réel
        # (message_start / usage upstream / finalisation).
        stream_in_est = None

        async def _ensure_stream_in_est():
            nonlocal stream_in_est
            if stream_in_est is None:
                stream_in_est = await asyncio.to_thread(_estimate_input_tokens, body)
                if _cfg_settings.DEBUG:
                    _debug(f"  [stream-oai] est_input={stream_in_est}")
                _update_token_usage(_req_model_id, stream_in_est, 0, 0)
            return stream_in_est

        _est_task = asyncio.create_task(_ensure_stream_in_est())
        started = False
        open_blocks = []
        text_block_idx = None
        reasoning_block_idx = None
        reasoning_acc = ""
        next_block_idx = 0
        stream_out_tokens = 0
        actual_usage = None
        emitted_finish = False

        def _thinking_flush():
            """[PLAN-raisonnement Phase C] (idx, signature locale) du bloc
            thinking ouvert, ou (None, ""). La signature couvre le texte
            ACCUMULÉ complet — elle n'est calculée qu'au moment du flush."""
            if reasoning_block_idx is not None and reasoning_acc:
                return reasoning_block_idx, _local_signature(reasoning_acc)
            return None, ""

        _handle_429 = _make_stream_retry_loop("openai")
        _debug("  [stream-oai] _handle_429 ready, about to yaml_get")

        _free_bound = yaml_get("streaming", "retry_attempts", 2)
        _debug(f"  [stream-oai] _free_bound={_free_bound}")
        _geo_tunnel = getattr(request.state, "_geo_force_tunnel", False)
        _free_forced_pool = getattr(request.state, "_geo_forced_pool", None)
        _debug(f"  [stream-oai] _geo_tunnel={_geo_tunnel} _using_free={_using_free}")
        if _using_free and _free_attempts_active(_free_forced_pool):
            # [plan 19/08 §1] free multi-attempt: extra free strikes on
            # fresh stations; paid retry budget preserved
            # (max_free_attempts=1 ⇒ exact legacy behaviour).
            _free_bound += max(0, effective_free_max_attempts(_free_forced_pool) - 1)
        _debug(f"  [stream-oai] final _free_bound={_free_bound}, starting for loop")
        for _attempt in range(_free_bound):
            used_tools = []  # Reset on each retry attempt
            tool_block_idx = {}  # Reset tracking dict on each retry
            got_response_completed = False  # Responses API: set on response.completed
            _responses_stream_done = (
                False  # True after response.completed/incomplete → break inner loop
            )
            try:
                # Axe A: geo-restricted paid streaming → route through tunnel station
                _debug(
                    f"  [stream-oai] attempt {_attempt}/{_free_bound} _geo_tunnel={_geo_tunnel} _using_free={_using_free} endpoint={endpoint}"
                )
                _stream_ctx = (
                    _open_via_pool(
                        endpoint, oai_body, hdrs, is_stream=True, forced_pool=_free_forced_pool
                    )
                    if _geo_tunnel
                    else _open_free_stream(
                        endpoint,
                        oai_body,
                        hdrs,
                        _using_free,
                        count_request=(_attempt == 0),
                        fresh_station=(_attempt > 0 and _using_free),
                        direct_fallback=(
                            _free_exception_fallback_mode() != "station-first"
                            or _attempt + 1 >= effective_free_max_attempts(_free_forced_pool)
                        ),
                        forced_pool=_free_forced_pool,
                    )
                )
                _debug("  [stream-oai] entering stream context")
                async with _stream_ctx as resp:
                    _debug(f"  [stream-oai] stream context entered, status={resp.status_code}")
                    if resp.status_code != 200:
                        if _using_free:
                            # Free endpoint non-200 (429 quota, 5xx...) → cooldown
                            # the free model, fall back to paid. Never pauses PAID
                            # keys: a status from the free endpoint says nothing
                            # about the paid account (CRITIC(2)/CRITIC(3)).
                            if resp.status_code == 429:
                                _refuse = _on_free_429_stream(
                                    free_model,
                                    resp.headers.get("retry-after", ""),
                                    forced_pool=_free_forced_pool,
                                )
                                if not _refuse and _attempt + 1 < effective_free_max_attempts(
                                    _free_forced_pool
                                ):
                                    # [plan 19/08 §1] budget left → retry free on a
                                    # FRESH station (fresh IP = fresh quota); keep
                                    # _using_free so the next attempt re-enters free
                                    # with fresh_station=True. Cooldown + rotation
                                    # for the exhausted IP already done above.
                                    _log_free_model_usage(
                                        _paid_model_id,
                                        free_model,
                                        "free (no auth)",
                                        "free (no auth)",
                                        resp.status_code,
                                        ip=_free_usage_ip(),
                                    )
                                    _log(
                                        f"  FREE {free_model!r} RATE LIMITED (429) → retry station fraîche (essai {_attempt + 2}/{effective_free_max_attempts(_free_forced_pool)})"
                                    )
                                    if _stream_has_yielded(
                                        started, open_blocks, stream_out_tokens, ""
                                    ):
                                        _cb_record_failure(endpoint)
                                        _log(
                                            f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                        )
                                        _ti, _ts = _thinking_flush()
                                        async for ev in _terminate_after_started(
                                            open_blocks,
                                            stream_out_tokens,
                                            thinking_idx=_ti,
                                            thinking_sig=_ts,
                                        ):
                                            yield ev
                                        return
                                    continue
                            else:
                                _set_free_cooldown(free_model, 60, _free_attempt_station())
                                _refuse = False
                            _debug(
                                f"  [stream-oai] free model {resp.status_code} → falling back to paid {_paid_model_id!r}"
                            )
                            _log(
                                f"  FREE model {resp.status_code} → falling back to paid {_paid_model_id!r}"
                            )
                            _log_free_model_usage(
                                _paid_model_id,
                                free_model,
                                "free (no auth)",
                                "free (no auth)",
                                resp.status_code,
                                ip=_free_usage_ip(),
                            )
                            oai_body = paid_oai_body
                            endpoint = paid_endpoint
                            model_id = _paid_model_id
                            _req_model_id = _paid_model_id  # Also revert logging model (fixes is_free_model in history)
                            _using_free = False
                            if _refuse:
                                # strict_free (GUI): every station exhausted
                                # (bad/down + (model, IP) cooldown active) —
                                # refuse instead of paying.
                                _retry_after = resp.headers.get("retry-after", "") or "60"
                                yield await _stream_error_response(
                                    req_id,
                                    free_model,
                                    original_model,
                                    start_time,
                                    429,
                                    await resp.aread(),
                                    protocol,
                                    thinking_type,
                                    effort,
                                    client_ip,
                                    "free (no auth)",
                                    tool_names,
                                    {
                                        "type": "error",
                                        "error": {
                                            "type": "rate_limit_error",
                                            "message": f"Free quota exhausted on all VPN stations. Retry after {_retry_after}s.",
                                        },
                                    },
                                    request_body=request_body,
                                )
                                return
                            continue
                        hdrs, should_retry = await _handle_429(
                            hdrs, resp.status_code, _attempt, resp.headers
                        )
                        if should_retry:
                            if _stream_has_yielded(started, open_blocks, stream_out_tokens, ""):
                                _cb_record_failure(endpoint)
                                _log(
                                    f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                )
                                _ti, _ts = _thinking_flush()
                                async for ev in _terminate_after_started(
                                    open_blocks,
                                    stream_out_tokens,
                                    thinking_idx=_ti,
                                    thinking_sig=_ts,
                                ):
                                    yield ev
                                return
                            continue
                        if resp.status_code == 499:
                            wait = 1.0 * (2**_attempt)
                            _debug(
                                f"  [stream-oai] upstream 499, retrying in {wait:.1f}s (attempt {_attempt + 1})"
                            )
                            await asyncio.sleep(wait)
                            if _stream_has_yielded(started, open_blocks, stream_out_tokens, ""):
                                _cb_record_failure(endpoint)
                                _log(
                                    f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                )
                                _ti, _ts = _thinking_flush()
                                async for ev in _terminate_after_started(
                                    open_blocks,
                                    stream_out_tokens,
                                    thinking_idx=_ti,
                                    thinking_sig=_ts,
                                ):
                                    yield ev
                                return
                            continue
                        err = await resp.aread()
                        # Pause key on credit/balance errors (400)
                        if resp.status_code == 400 and any(
                            x in err.decode(errors="ignore")
                            for x in ("Insufficient balance", "Monthly usage limit")
                        ):
                            failed_key = _key_from_headers(hdrs, "openai")
                            _key_pauser.pause_key(
                                failed_key, _key_pauser._max_pause, "400 credit error"
                            )
                            alt = _find_alternative_key(failed_key)
                            if alt:
                                _log("  400 credit error on key, retrying with alternative key")
                                hdrs = _get_auth_headers("openai", entry=alt)
                                if _stream_has_yielded(
                                    started, open_blocks, stream_out_tokens, ""
                                ):
                                    _cb_record_failure(endpoint)
                                    _log(
                                        f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                    )
                                    _ti, _ts = _thinking_flush()
                                    async for ev in _terminate_after_started(
                                        open_blocks,
                                        stream_out_tokens,
                                        thinking_idx=_ti,
                                        thinking_sig=_ts,
                                    ):
                                        yield ev
                                    return
                                continue
                        ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))

                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()

                        if data == "[DONE]":
                            _ti, _ts = _thinking_flush()
                            async for ev in _finalize_and_close_stream(
                                started,
                                open_blocks,
                                text_block_idx,
                                reasoning_block_idx,
                                tool_block_idx,
                                stream_out_tokens,
                                actual_usage,
                                model_id,
                                stream_in_est,
                                msg_id,
                                original_model,
                                _token_usage,
                                _token_lock,
                                _using_free,
                                _paid_model_id,
                                free_model,
                                start_time,
                                req_id,
                                _req_model_id,
                                protocol,
                                thinking_type,
                                effort,
                                client_ip,
                                hdrs,
                                tool_names,
                                used_tools,
                                request_body,
                                reasoning_signature=_ts,
                            ):
                                yield ev
                            emitted_finish = True
                            break

                        try:
                            chunk = _json_loads(data)
                        except Exception:
                            continue
                        if chunk is None:
                            continue

                        chunk_usage = chunk.get("usage")
                        if chunk_usage and isinstance(chunk_usage, dict):
                            actual_usage = chunk_usage

                        choices = chunk.get("choices", [])
                        if not choices or not isinstance(choices, list):
                            # 可能是 Responses API format — try converting
                            if _cfg_settings.DEBUG:
                                _debug(
                                    f"  [stream-oai] no choices, trying responses_sse convert: {data[:200]!r}"
                                )
                            converted = _responses_sse_to_chat_deltas(data, parsed=chunk)
                            if converted is None:
                                continue
                            chunk = converted
                            choices = chunk.get("choices", [])
                            if not choices:
                                # response.completed or response.incomplete —
                                # Responses API never sends [DONE]; it sends
                                # response.completed (with usage) or
                                # response.incomplete (model didn't produce output).
                                # Both signal end-of-stream.
                                if isinstance(chunk, dict) and "usage" in chunk:
                                    got_response_completed = True
                                    _responses_stream_done = True
                                    actual_usage = chunk.get("usage") or actual_usage
                                    _debug(
                                        "  [stream-oai] response stream-end signal received — breaking inner loop"
                                    )
                                    break
                                continue

                        first_choice = choices[0] if choices else {}
                        delta = (
                            first_choice.get("delta", {}) if isinstance(first_choice, dict) else {}
                        )
                        if not delta or not isinstance(delta, dict):
                            delta = {}

                        if not started:
                            started = True
                            yield _sse(
                                "message_start",
                                {
                                    "type": "message_start",
                                    "message": {
                                        "id": msg_id,
                                        "type": "message",
                                        "role": "assistant",
                                        "content": [],
                                        "model": original_model,
                                        "stop_reason": None,
                                        "stop_sequence": None,
                                        "usage": {
                                            "input_tokens": stream_in_est,
                                            "output_tokens": 0,
                                            "cache_read_input_tokens": 0,
                                        },
                                    },
                                },
                            )

                        # Text
                        text = ""
                        c = delta.get("content")
                        if isinstance(c, str):
                            text = c
                        elif isinstance(c, list):
                            text = "".join(
                                p.get("text", "")
                                for p in c
                                if isinstance(p, dict) and p.get("type") == "text"
                            )

                        if text:
                            if text_block_idx is None:
                                text_block_idx = next_block_idx
                                next_block_idx += 1
                                yield _sse(
                                    "content_block_start",
                                    {
                                        "type": "content_block_start",
                                        "index": text_block_idx,
                                        "content_block": {"type": "text", "text": ""},
                                    },
                                )
                                open_blocks.append(text_block_idx)
                            stream_out_tokens += _estimate_tokens(text)
                            yield _sse(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": text_block_idx,
                                    "delta": {"type": "text_delta", "text": text},
                                },
                            )

                        # Reasoning content
                        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                        if isinstance(reasoning, str) and reasoning:
                            if reasoning_block_idx is None:
                                reasoning_block_idx = next_block_idx
                                next_block_idx += 1
                                _debug(
                                    f"  [stream-oai] reasoning_content block_start idx={reasoning_block_idx}"
                                )
                                yield _sse(
                                    "content_block_start",
                                    {
                                        "type": "content_block_start",
                                        "index": reasoning_block_idx,
                                        "content_block": {"type": "thinking", "thinking": ""},
                                    },
                                )
                                open_blocks.append(reasoning_block_idx)
                            stream_out_tokens += _estimate_tokens(reasoning)
                            reasoning_acc += reasoning
                            yield _sse(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": reasoning_block_idx,
                                    "delta": {"type": "thinking_delta", "thinking": reasoning},
                                },
                            )

                        # Tool calls
                        for tc in delta.get("tool_calls") or []:
                            api_idx = tc.get("index", 0)
                            if api_idx not in tool_block_idx:
                                block_idx = next_block_idx
                                next_block_idx += 1
                                tool_block_idx[api_idx] = block_idx
                                tc_id = tc.get("id", f"toolu_{uuid.uuid4().hex[:8]}")
                                tc_name = tc.get("function", {}).get("name", "")
                                if tc_name:
                                    used_tools.append(tc_name)
                                _debug(
                                    f"  [stream-oai] tool_call block_start idx={block_idx} name={tc_name!r} id={tc_id}"
                                )
                                yield _sse(
                                    "content_block_start",
                                    {
                                        "type": "content_block_start",
                                        "index": block_idx,
                                        "content_block": {
                                            "type": "tool_use",
                                            "id": tc_id,
                                            "name": tc_name,
                                            "input": {},
                                        },
                                    },
                                )
                                open_blocks.append(block_idx)
                            if args := tc.get("function", {}).get("arguments", ""):
                                stream_out_tokens += _estimate_tokens(args)
                                yield _sse(
                                    "content_block_delta",
                                    {
                                        "type": "content_block_delta",
                                        "index": tool_block_idx[api_idx],
                                        "delta": {"type": "input_json_delta", "partial_json": args},
                                    },
                                )

                    # Responses API: if response.completed/incomplete was
                    # received, the upstream may not close the connection.
                    # Break the outer retry loop to finalize the stream.
                    if _responses_stream_done:
                        break
            except asyncio.CancelledError:
                # [plan 18/08 §am.22/piège 19] — same watchdog-cancel
                # handling as the anthropic stream handler (see there).
                st = _free_attempt_station()
                if st is not None and _attempt == 0 and _is_watchdog_cancelled(st):
                    _debug(
                        f"  ⟳ stream watchdog-cancelled (dead tunnel, station {getattr(st, '_station', '?')}) — failover retry"
                    )
                    _log("  FREE STREAM on confirmed-dead tunnel cancelled → switching station")
                    if _stream_has_yielded(started, open_blocks, stream_out_tokens, ""):
                        _cb_record_failure(endpoint)
                        _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                        _ti, _ts = _thinking_flush()
                        async for ev in _terminate_after_started(
                            open_blocks, stream_out_tokens, thinking_idx=_ti, thinking_sig=_ts
                        ):
                            yield ev
                        return
                    continue
                raise
            except _FreeTunnelFailure as ftf:
                # [plan 19/08 §2] station-first: dead tunnel → retry free
                # on a FRESH station (fresh IP = fresh quota) before any
                # direct residential fallback. _using_free stays True →
                # next attempt re-enters free with fresh_station=True
                # (bad-mark → exclusion of the dead station).
                _log(
                    f"  FREE STREAM via station {getattr(ftf.station, '_station', '?')} tunnel FAILED ({ftf.cause}) → retry station fraîche"
                )
                if _stream_has_yielded(started, open_blocks, stream_out_tokens, ""):
                    _cb_record_failure(endpoint)
                    _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                    _ti, _ts = _thinking_flush()
                    async for ev in _terminate_after_started(
                        open_blocks, stream_out_tokens, thinking_idx=_ti, thinking_sig=_ts
                    ):
                        yield ev
                    return
                continue
            except Exception as e:
                _log(f"  ERROR stream (attempt {_attempt + 1}): {type(e).__name__}: {e}")
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
                    if _stream_has_yielded(started, open_blocks, stream_out_tokens, ""):
                        _cb_record_failure(endpoint)
                        _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                        _ti, _ts = _thinking_flush()
                        async for ev in _terminate_after_started(
                            open_blocks, stream_out_tokens, thinking_idx=_ti, thinking_sig=_ts
                        ):
                            yield ev
                        return
                    continue
                try:
                    # [B1] résout l'estimation différée avant le rollback
                    stream_in_est = await _ensure_stream_in_est()
                    with _token_lock:
                        _token_usage[_req_model_id]["input"] -= stream_in_est
                except Exception:
                    pass
                ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                await _save_request(
                    req_id,
                    _req_model_id,
                    original_model,
                    _elapsed_ms(start_time),
                    stream_in_est,
                    stream_out_tokens,
                    0,
                    success=False,
                    error=str(e),
                    protocol=protocol,
                    is_stream=True,
                    thinking=thinking_type,
                    effort=effort,
                    client_ip=client_ip,
                    account_alias=ak_h,
                    tools=tool_names,
                    tools_used=used_tools if used_tools else None,
                    request_body=request_body,
                )
                if started:
                    _ti, _ts = _thinking_flush()
                    for idx in open_blocks:
                        if _ti is not None and idx == _ti and _ts:
                            yield _sse(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": idx,
                                    "delta": {"type": "signature_delta", "signature": _ts},
                                },
                            )
                        yield _sse(
                            "content_block_stop", {"type": "content_block_stop", "index": idx}
                        )
                    yield _sse(
                        "message_delta",
                        {
                            "type": "message_delta",
                            "delta": {"stop_reason": "error"},
                            "usage": {"output_tokens": stream_out_tokens},
                        },
                    )
                    yield _sse("message_stop", {"type": "message_stop"})
                return
            else:
                break
        else:
            # Loop exhausted without [DONE] and without break.
            # If got_response_completed is True, finalize below (after the loop).
            pass

        # Responses API: response.completed / response.incomplete was received
        # but the upstream didn't close the connection (so the for loop
        # continued until the timeout). Finalize the stream properly.
        if got_response_completed and not emitted_finish:
            _debug("  [stream-oai] post-loop: finalizing Responses API stream")
            _ti, _ts = _thinking_flush()
            # [B1] résout l'estimation différée avant réconciliation
            stream_in_est = await _ensure_stream_in_est()
            async for ev in _finalize_and_close_stream(
                started,
                open_blocks,
                text_block_idx,
                reasoning_block_idx,
                tool_block_idx,
                stream_out_tokens,
                actual_usage,
                model_id,
                stream_in_est,
                msg_id,
                original_model,
                _token_usage,
                _token_lock,
                _using_free,
                _paid_model_id,
                free_model,
                start_time,
                req_id,
                _req_model_id,
                protocol,
                thinking_type,
                effort,
                client_ip,
                hdrs,
                tool_names,
                used_tools,
                request_body,
                reasoning_signature=_ts,
            ):
                yield ev
            emitted_finish = True
        # Fix: guarantee finish_reason on truncated stream (EOF without [DONE] / response.completed)
        if started and not emitted_finish:
            _debug("  [stream-oai] truncated without finish_reason → synthesizing stop")
            _log("  stream truncated without finish_reason → synthesizing stop")
            _ti, _ts = _thinking_flush()
            async for ev in _terminate_after_started(
                open_blocks, stream_out_tokens, thinking_idx=_ti, thinking_sig=_ts
            ):
                yield ev
            emitted_finish = True
        elif got_response_completed and not emitted_finish:
            # Incomplete Responses API without prior finalize (should be covered above, but keep for safety)
            _debug("  [stream-oai] truncated Responses without finalize → synthesizing")
            _ti, _ts = _thinking_flush()
            async for ev in _terminate_after_started(
                open_blocks, stream_out_tokens, thinking_idx=_ti, thinking_sig=_ts
            ):
                yield ev
            emitted_finish = True
        return

    return StreamingResponse(
        _sse_keepalive(stream_gen(headers)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


_health_cache: tuple[float, bool] | None = None  # (timestamp, upstream_ok)
_health_lock = asyncio.Lock()  # sérialise le check upstream caché (§14.3.16 garde le writer lock séparé)


@app.get("/health")
async def health():
    _debug("  [health] health check started")

    def _health_probe():
        # [v10 §14.3.16] SELECT 1 sous le MÊME lock que le batch writer :
        # un check concurrent au commit levait InterfaceError ponctuel.
        with _db_commit_lock:
            _conn.execute("SELECT 1")

    await asyncio.to_thread(_health_probe)
    _debug("  [health] DB connectivity OK")

    usage = {
        model: {"input": d["input"], "output": d["output"], "cache": d["cache"]}
        for model, d in _token_usage.items()
    }

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
        if _health_cache and (now - _health_cache[0]) < yaml_get(
            "background", "health_cache_ttl", 15
        ):
            upstream_ok = _health_cache[1]
            _debug(f"  [health] upstream check (cached): {'ok' if upstream_ok else 'unreachable'}")
        else:
            # Cache miss — perform actual check
            try:
                resp = await _ensure_http_client().get(
                    "https://opencode.ai",
                    timeout=float(yaml_get("background", "health_timeout", 5)),
                )
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
    return {
        "status": status,
        "usage": usage,
        "upstream": "ok" if upstream_ok else "unreachable",
        "circuit_breakers": cb_status,
        "key_pauses": key_pause_status,
    }


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
    for _endpoint, cb in _circuit_breakers.items():
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


@app.get("/api/free-models")
async def free_models():
    """Observability for auto free discovery (GET /api/free-models)."""
    st = _cfg_settings._FREE_DISCOVERY_STATE
    return {
        "detected": sorted(_cfg_settings.FREE_MODELS),
        "mapped": dict(_cfg_settings.FREE_MODEL_MAP),
        "pool": list(_cfg_settings.FREE_MODEL_POOL),
        "removed": list(st.get("removed", []) or []),
        "last_refresh": st.get("last_refresh"),
        "next_refresh": st.get("next_refresh"),
        "source": st.get("source", "none"),
        "consecutive_failures": int(st.get("consecutive_failures", 0) or 0),
    }


@app.post("/api/free-discovery/refresh")
async def free_discovery_refresh(request: Request):
    """Manual refresh of free discovery (POST /api/free-discovery/refresh).

    Guards (plan hardening):
    - DASHBOARD_TOKEN opt-in via X-Dashboard-Token (constant-time) → 401
    - control_enabled without control_api_key → 403
    - Rate-limit 1/min per IP → 429 with Retry-After
    - Singleflight via app.state._free_discovery_lock
    """
    # DASHBOARD_TOKEN guard (same contract as dashboard/api.py)
    _dash = os.getenv("DASHBOARD_TOKEN", "").strip()
    if _dash:
        provided = request.headers.get("X-Dashboard-Token", "")
        if not provided or not hmac.compare_digest(provided, _dash):
            return JSONResponse(
                status_code=401,
                content={
                    "error": "unauthorized",
                    "message": "Valid X-Dashboard-Token header required (DASHBOARD_TOKEN env).",
                },
            )
    # control_api guard (ip_rotation.control_enabled requires a key)
    _ctrl_enabled = bool(yaml_get("ip_rotation", "control_enabled", False))
    _ctrl_key = str(yaml_get("ip_rotation", "control_api_key", "") or "").strip()
    if _ctrl_enabled and not _ctrl_key:
        return JSONResponse(
            status_code=403,
            content={
                "error": "forbidden",
                "message": "control_enabled requires control_api_key — set ip_rotation.control_api_key",
            },
        )
    # Rate-limit 1/min per IP
    ip = request.client.host if request.client else "unknown"
    _rl = getattr(app.state, "_free_refresh_last_minute", None)
    if _rl is None:
        app.state._free_refresh_last_minute = {}
        _rl = app.state._free_refresh_last_minute
    now_m = time.monotonic()
    last = _rl.get(ip, 0)
    if last and (now_m - last) < 60:
        retry = int(60 - (now_m - last)) + 1
        return JSONResponse(
            status_code=429,
            content={"error": "rate_limited", "retry_after": retry},
            headers={"Retry-After": str(retry)},
        )
    # Singleflight
    lock = getattr(app.state, "_free_discovery_lock", None)
    if lock is None:
        app.state._free_discovery_lock = asyncio.Lock()
        lock = app.state._free_discovery_lock
    async with lock:
        # Re-check rate-limit inside lock (concurrent POSTs)
        now2 = time.monotonic()
        last2 = _rl.get(ip, 0)
        if last2 and (now2 - last2) < 60:
            retry = int(60 - (now2 - last2)) + 1
            return JSONResponse(
                status_code=429,
                content={"error": "rate_limited", "retry_after": retry},
                headers={"Retry-After": str(retry)},
            )
        added = await asyncio.to_thread(_cfg_settings._ensure_free_models_sync)
        _rl[ip] = time.monotonic()
        try:
            from dashboard.events import get_event_manager

            get_event_manager().publish(
                "free_models_updated",
                {
                    "detected": sorted(_cfg_settings.FREE_MODELS),
                    "source": _cfg_settings._FREE_DISCOVERY_STATE.get("source", "none"),
                },
            )
        except Exception:
            pass
    return {
        "refreshed": True,
        "added": int(added or 0),
        "detected": sorted(_cfg_settings.FREE_MODELS),
        "source": _cfg_settings._FREE_DISCOVERY_STATE.get("source", "none"),
    }


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
            data.append(
                {
                    "id": model_id,
                    "object": "model",
                    "created": now,
                    "owned_by": "opencode",
                    "status": cb_state,
                    "requests": {
                        "input": usage.get("input", 0),
                        "output": usage.get("output", 0),
                        "cache": usage.get("cache", 0),
                    },
                }
            )
            seen.add(model_id)
    for alias in ["gpt-5-codex", "gpt-5", "gpt-4o", "codex", "deepseek-chat"]:
        if alias not in seen:
            data.append({"id": alias, "object": "model", "created": now, "owned_by": "opencode"})
            seen.add(alias)
    return {"object": "list", "data": data, "cache": _response_cache.stats()}


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    try:
        body = _json_loads(await request.body())
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
    _debug(
        f"  [body] read {len(body_bytes)} bytes in {(time.monotonic() - start_time) * 1000:.0f}ms"
    )
    if len(body_bytes) > MAX_BODY_SIZE:
        _debug(f"  413: body too large ({len(body_bytes)} bytes)")
        return _openai_error(
            413, f"Request body too large ({len(body_bytes)} bytes, max {MAX_BODY_SIZE})"
        )

    try:
        body = _json_loads(body_bytes)
    except Exception:
        _debug("  400: invalid JSON body")
        return _openai_error(400, "invalid json")

    original_model = body.get("model", "")
    tool_names = _extract_tool_names(body)
    request_body = body  # Capture original request before mutation
    _debug(f"[chat] req_id={req_id} model={original_model!r} tools={tool_names} ip={client_ip}")
    if DEBUG:
        _debug(f"[chat] headers={_sanitize_headers(dict(request.headers))}")
        _debug(f"[chat] body=\n{_redact(_truncate(body))}")
    route = _route_for(original_model)
    if route is None:
        _debug(f"[chat] ✗ no route found for {original_model!r}")
        available = sorted(MODELS.keys())
        return JSONResponse(
            status_code=404,
            content={
                "error": f"Model not found: {original_model!r}",
                "available_models": available,
            },
        )
    model_id = route["model"]
    cfg = get_model_config(model_id)
    endpoint = cfg["endpoint"]
    protocol = cfg["protocol"]
    _debug(f"[chat] route: {original_model!r} → {model_id} | {protocol} | endpoint={endpoint}")

    body = dict(body)
    body["model"] = model_id
    is_stream = body.get("stream", False)

    thinking_raw = body.get("thinking") if isinstance(body.get("thinking"), dict) else {}
    thinking_type = (
        thinking_raw.get("type", "none")
        if isinstance(thinking_raw, dict) and thinking_raw
        else "none"
    )
    effort = (
        body.get("effort")
        or (thinking_raw.get("effort") if isinstance(thinking_raw, dict) else None)
        or (
            body.get("output_config", {}).get("effort")
            if isinstance(body.get("output_config"), dict)
            else None
        )
        or "none"
    )

    # Tool filtering removed — all tools are forwarded as-is

    # Normalize effort/thinking -> reasoning_effort for OpenAI direct clients (mimo/spark)
    # Claude Code via /v1/messages already maps via anthropic_to_openai, but
    # clients talking OpenAI directly (e.g. via /v1/chat/completions with effort)
    # need the same mapping so spark/mimo get correct reasoning level.
    if "reasoning_effort" not in body:
        _effort_level = body.get("effort")
        _thinking_param = body.get("thinking") if isinstance(body.get("thinking"), dict) else {}
        _ttype = _thinking_param.get("type", "") if isinstance(_thinking_param, dict) else ""
        _budget = (
            _thinking_param.get("budget_tokens", 0) if isinstance(_thinking_param, dict) else 0
        )
        _wants = False
        _mapped_effort = _effort_level
        if _effort_level and _effort_level != "none":
            _wants = True
        elif _ttype in ("enabled", "adaptive") or _budget:
            _wants = True
            if _budget and _budget < 4000:
                _mapped_effort = "low"
            elif _budget and _budget < 10000:
                _mapped_effort = "medium"
            elif _budget and _budget < 16000:
                _mapped_effort = "high"
            elif _ttype == "adaptive":
                _mapped_effort = "medium"
            elif _budget:
                _mapped_effort = "xhigh"
            else:
                _mapped_effort = "low" if _ttype == "enabled" else "medium"
        if _wants and _mapped_effort and _mapped_effort != "none":
            if model_id.startswith("glm-5"):
                body["reasoning_effort"] = (
                    "high"
                    if _mapped_effort in ("xhigh", "max", "high")
                    else ("medium" if _mapped_effort == "medium" else "low")
                )
            elif model_id.startswith("deepseek-v4"):
                body["reasoning_effort"] = "max" if _mapped_effort in ("xhigh", "max") else "high"
            else:
                body["reasoning_effort"] = (
                    "high"
                    if _mapped_effort in ("xhigh", "max", "high")
                    else ("medium" if _mapped_effort == "medium" else "low")
                )
            # Keep original effort for logging, but ensure reasoning_effort is set
            if effort == "none":
                effort = _mapped_effort

    # Handle web_search / web_fetch (v3.3)
    await _handle_web_search(body, model_id, protocol)
    await _handle_web_fetch(body, model_id, protocol)
    # ── Orphan guard (handler-level) ──
    if isinstance(body, dict):
        if "messages" in body:
            body["messages"] = _drop_orphan_tool_messages(body["messages"])
        elif "input" in body:
            body["input"] = _drop_orphan_responses_input(body["input"])

    _log(
        f"→ {original_model!r} → {model_id} | {protocol} | chat/completions | stream={is_stream} | thinking={thinking_type} | effort={effort} | ip={client_ip}"
    )

    _geo_gate = await _enforce_geo_gate(
        route, request, is_stream=bool(is_stream), protocol="openai"
    )
    if _geo_gate is not None:
        return _geo_gate

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
                    # Streaming with no API key: use free model stream directly (openai_stream defined later, use fallback 503 for now)
                    return _openai_error(503, "All API keys paused — free model will be tried by openai_stream on next attempt")
                else:
                    # Non-streaming: try free model
                    try:
                        free_result = await _try_free_model_first(
                            body,
                            {},
                            "openai",
                            model_id,
                            forced_pool=getattr(request.state, "_geo_forced_pool", None),
                        )
                        if free_result is not None:
                            resp, _, _actual_model, _actual_ip = free_result
                            data = (
                                resp.json()
                                if resp.headers.get("content-type", "").startswith(
                                    "application/json"
                                )
                                else {}
                            )
                            usage = data.get("usage", {})
                            req_in = usage.get("prompt_tokens", 0)
                            req_out = usage.get("completion_tokens", 0)
                            cache = _extract_cache_tokens(usage)
                            _update_token_usage(_actual_model, req_in, req_out, cache)
                            used = _extract_usage_tool_names(data)
                            await _save_and_log_request(
                                req_id,
                                _actual_model,
                                original_model,
                                start_time,
                                req_in,
                                req_out,
                                cache,
                                protocol,
                                False,
                                thinking_type,
                                effort,
                                client_ip,
                                "free (no auth)",
                                tool_names,
                                tools_used=used,
                                request_body=request_body,
                                response_body=data,
                                free_model_ip=_actual_ip,
                            )
                            return Response(
                                content=_json_dumps_str(data, ensure_ascii=False),
                                media_type="application/json",
                            )
                    except FreeQuotaExhausted as e:
                        return _free_quota_exhausted_response(e, "openai")
                    except Exception as e:
                        _debug(f"  [free] free model attempt failed: {e}")
            retry_after = int(e.retry_after) + 1
            return Response(
                content=_json_dumps_str(
                    {
                        "error": {
                            "message": f"All API keys exhausted. Retry after {retry_after}s.",
                            "type": "api_error",
                            "code": "503",
                        }
                    }
                ),
                status_code=503,
                media_type="application/json",
                headers={"Retry-After": str(retry_after)},
            )

        if not is_stream:
            # Response cache (mirrors the anthropic non-stream handler) [8]
            cache_key = _response_cache.make_key(body, body_bytes=body_bytes)
            cached = cache_key and _response_cache.get(cache_key)
            if cached:
                cached_body, cached_headers = cached
                _debug(f"  [chat] response cache HIT ({len(cached_body)} bytes)")
                return Response(
                    content=cached_body,
                    headers={**cached_headers, "X-Cache": "HIT"},
                    media_type="application/json",
                )
            # Try free model first if available
            _geo_tunnel = getattr(request.state, "_geo_force_tunnel", False)
            try:
                free_result = await _try_free_model_first(
                    body,
                    headers,
                    "openai",
                    model_id,
                    forced_pool=getattr(request.state, "_geo_forced_pool", None),
                )
                if free_result is not None:
                    resp, headers, _actual_model, _actual_ip = free_result
                    model_id = _actual_model
                else:
                    # Convert to Responses API format if endpoint requires it (muse-spark)
                    paid_body = (
                        _chat_to_responses_request(body) if "/responses" in endpoint else body
                    )
                    if _geo_tunnel:
                        # Axe A: geo-restricted paid → must route through tunnel station
                        async with _open_via_pool(
                            endpoint,
                            paid_body,
                            headers,
                            is_stream=False,
                            forced_pool=getattr(request.state, "_geo_forced_pool", None),
                        ) as resp:
                            headers = dict(resp.headers)
                    else:
                        resp, headers = await _do_request_with_retry(
                            endpoint, paid_body, headers, "openai"
                        )
            except FreeQuotaExhausted as e:
                return _free_quota_exhausted_response(e, "openai")
            except UpstreamError as e:
                return JSONResponse(status_code=e.status_code, content={"error": str(e)})
            account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
            if resp.status_code != 200:
                await _log_and_save_error(
                    req_id,
                    model_id,
                    original_model,
                    start_time,
                    resp.status_code,
                    resp.text,
                    protocol,
                    is_stream,
                    thinking_type,
                    effort,
                    client_ip,
                    account_alias,
                    tool_names,
                    request_body=request_body,
                    response_body={"error": resp.text[:2000]},
                )
                # Pause key on credit/balance errors (400) and retry with alt key
                if resp.status_code == 400 and any(
                    x in resp.text for x in ("Insufficient balance", "Monthly usage limit")
                ):
                    failed_key = _key_from_headers(headers, "openai")
                    _key_pauser.pause_key(
                        failed_key,
                        _key_pauser._max_pause,
                        f"400 credit error: {_redact(resp.text, 80)}",
                    )
                    alt = _find_alternative_key(failed_key)
                    if alt:
                        _log("  400 credit error on key, retrying with alternative key")
                        headers = _get_auth_headers("openai", entry=alt)
                        try:
                            resp, headers = await _do_request_with_retry(
                                endpoint, body, headers, "openai"
                            )
                            if resp.status_code == 200:
                                account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
                            else:
                                return _openai_error(
                                    503, "All API keys exhausted. Check your billing."
                                )
                        except UpstreamError as e:
                            return JSONResponse(
                                status_code=e.status_code, content={"error": str(e)}
                            )
                # Convert 429/401/403 → 503 to avoid Claude Code auth window
                if resp.status_code in (429, 401, 403):
                    return _openai_error(503, _auth_window_message(resp.status_code))
                if resp.status_code == 499:
                    return _openai_error(502, "Upstream disconnected (499). Retrying may help.")
                return Response(
                    content=resp.content,
                    status_code=resp.status_code,
                    media_type="application/json",
                )
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
            await _save_and_log_request(
                req_id,
                model_id,
                original_model,
                start_time,
                req_in,
                req_out,
                cache,
                protocol,
                is_stream,
                thinking_type,
                effort,
                client_ip,
                account_alias,
                tool_names,
                tools_used=used if used else None,
                request_body=request_body,
                response_body=data,
            )
            # [v10 §14.3.26] garde manquante sur cette branche : cache_key None
            # polluait le store LRU d'une entrée clé None.
            if cache_key:
                _response_cache.put(cache_key, resp.content, {"Content-Type": "application/json"})
            return Response(
                content=resp.content, headers={"X-Cache": "MISS"}, media_type="application/json"
            )

        # ── OpenAI streaming passthrough ──
        oai_body = dict(body)
        # stream_options is a Chat Completions API field; Responses API reports
        # usage via the response.completed SSE event instead.
        if "/responses" not in endpoint:
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
                paid_endpoint = endpoint
                paid_oai_body = dict(oai_body)
                oai_body = dict(oai_body)
                oai_body["model"] = free_model
                endpoint = _cfg_settings._free_endpoint_for(free_model)
                # Convert body to Responses API format if endpoint requires it
                if "/responses" in endpoint:
                    oai_body = _chat_to_responses_request(oai_body)
                    oai_body["stream"] = True
                _using_free = True
                _track_model = free_model
            else:
                _using_free = False
            # [B1 perf] estimation tiktoken DIFFÉRÉE (même motif que
            # anthropic_stream) : tâche concurrente avec la connexion upstream,
            # résolue paresseusement à la finalisation / rollback.
            est_input = None

            async def _ensure_est_input():
                nonlocal est_input
                if est_input is None:
                    est_input = await asyncio.to_thread(_estimate_input_tokens, body)
                    _update_token_usage(_track_model, est_input, 0, 0)
                    if _cfg_settings.DEBUG:
                        _debug(f"  [chat-stream] est_input={est_input}")
                return est_input

            _est_task = asyncio.create_task(_ensure_est_input())
            stream_out = 0
            _has_yielded = False
            _oai_has_yielded = False
            actual_usage = None
            emitted_finish = False
            _handle_429 = _make_stream_retry_loop("openai")
            _free_forced_pool = getattr(request.state, "_geo_forced_pool", None)
            _free_bound = yaml_get("streaming", "retry_attempts", 2)
            if _using_free and _free_attempts_active(_free_forced_pool):
                # [plan 19/08 §1] free multi-attempt: extra free strikes on
                # fresh stations; paid retry budget preserved
                # (max_free_attempts=1 ⇒ exact legacy behaviour).
                _free_bound += max(0, effective_free_max_attempts(_free_forced_pool) - 1)
            for _attempt in range(_free_bound):
                used_tools = []
                seen_tool_indices = set()  # dedup tool calls by index
                try:
                    async with _open_free_stream(
                        endpoint,
                        oai_body,
                        hdrs,
                        _using_free,
                        count_request=(_attempt == 0),
                        fresh_station=(_attempt > 0 and _using_free),
                        direct_fallback=(
                            _free_exception_fallback_mode() != "station-first"
                            or _attempt + 1 >= effective_free_max_attempts(_free_forced_pool)
                        ),
                        forced_pool=_free_forced_pool,
                    ) as resp:
                        if resp.status_code != 200:
                            if _using_free:
                                # Free endpoint non-200 (429 quota, 5xx...) → cooldown
                                # the free model, fall back to paid. Never pauses PAID
                                # keys: a status from the free endpoint says nothing
                                # about the paid account (CRITIC(2)/CRITIC(3)).
                                if resp.status_code == 429:
                                    _refuse = _on_free_429_stream(
                                        free_model,
                                        resp.headers.get("retry-after", ""),
                                        forced_pool=_free_forced_pool,
                                    )
                                    if not _refuse and _attempt + 1 < effective_free_max_attempts(
                                        _free_forced_pool
                                    ):
                                        # [plan 19/08 §1] budget left → retry free on a
                                        # FRESH station (fresh IP = fresh quota); keep
                                        # _using_free so the next attempt re-enters free
                                        # with fresh_station=True. Cooldown + rotation
                                        # for the exhausted IP already done above.
                                        _log_free_model_usage(
                                            model_id,
                                            free_model,
                                            "free (no auth)",
                                            "free (no auth)",
                                            resp.status_code,
                                            ip=_free_usage_ip(),
                                        )
                                        _log(
                                            f"  FREE {free_model!r} RATE LIMITED (429) → retry station fraîche (essai {_attempt + 2}/{effective_free_max_attempts(_free_forced_pool)})"
                                        )
                                        if _oai_has_yielded or stream_out > 0:
                                            _cb_record_failure(endpoint)
                                            _log(
                                                f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                            )
                                            yield (
                                                b"data: "
                                                + _json_dumps_str(
                                                    {"error": {"message": "stream interrupted"}},
                                                    ensure_ascii=False,
                                                ).encode()
                                                + b"\n\ndata: [DONE]\n\n"
                                            )
                                            return
                                        continue
                                else:
                                    _set_free_cooldown(free_model, 60, _free_attempt_station())
                                    _refuse = False
                                oai_body = paid_oai_body
                                endpoint = paid_endpoint
                                _using_free = False
                                _track_model = model_id  # Revert tracking to paid model
                                _debug(
                                    f"  [chat-stream] free model {resp.status_code} → falling back to paid {_track_model!r}"
                                )
                                _log(
                                    f"  FREE model {resp.status_code} → falling back to paid {_track_model!r}"
                                )
                                _log_free_model_usage(
                                    model_id,
                                    free_model,
                                    "free (no auth)",
                                    "free (no auth)",
                                    resp.status_code,
                                    ip=_free_usage_ip(),
                                )
                                if _refuse:
                                    # strict_free (GUI): every station exhausted
                                    # (bad/down + (model, IP) cooldown active) —
                                    # refuse instead of paying.
                                    _retry_after = resp.headers.get("retry-after", "") or "60"
                                    yield await _stream_error_response(
                                        req_id,
                                        free_model,
                                        original_model,
                                        start_time,
                                        429,
                                        await resp.aread(),
                                        protocol,
                                        thinking_type,
                                        effort,
                                        client_ip,
                                        "free (no auth)",
                                        tool_names,
                                        {
                                            "type": "error",
                                            "error": {
                                                "type": "rate_limit_error",
                                                "message": f"Free quota exhausted on all VPN stations. Retry after {_retry_after}s.",
                                            },
                                        },
                                        request_body=request_body,
                                    )
                                    return
                                continue
                            hdrs, should_retry = await _handle_429(
                                hdrs, resp.status_code, _attempt, resp.headers
                            )
                            if should_retry:
                                if _oai_has_yielded or stream_out > 0:
                                    _cb_record_failure(endpoint)
                                    _log(
                                        f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                    )
                                    yield (
                                        b"data: "
                                        + _json_dumps_str(
                                            {"error": {"message": "stream interrupted"}},
                                            ensure_ascii=False,
                                        ).encode()
                                        + b"\n\ndata: [DONE]\n\n"
                                    )
                                    return
                                continue
                            if resp.status_code == 499:
                                wait = 1.0 * (2**_attempt)
                                _debug(
                                    f"  [chat-stream] upstream 499, retrying in {wait:.1f}s (attempt {_attempt + 1})"
                                )
                                await asyncio.sleep(wait)
                                if _oai_has_yielded or stream_out > 0:
                                    _cb_record_failure(endpoint)
                                    _log(
                                        f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                    )
                                    yield (
                                        b"data: "
                                        + _json_dumps_str(
                                            {"error": {"message": "stream interrupted"}},
                                            ensure_ascii=False,
                                        ).encode()
                                        + b"\n\ndata: [DONE]\n\n"
                                    )
                                    return
                                continue
                            err = await resp.aread()
                            # Pause key on credit/balance errors (400)
                            if resp.status_code == 400 and any(
                                x in err.decode(errors="ignore")
                                for x in ("Insufficient balance", "Monthly usage limit")
                            ):
                                failed_key = _key_from_headers(hdrs, "openai")
                                _key_pauser.pause_key(
                                    failed_key, _key_pauser._max_pause, "400 credit error"
                                )
                                alt = _find_alternative_key(failed_key)
                                if alt:
                                    _log(
                                        "  400 credit error on key, retrying with alternative key"
                                    )
                                    hdrs = _get_auth_headers("openai", entry=alt)
                                    if _oai_has_yielded or stream_out > 0:
                                        _cb_record_failure(endpoint)
                                        _log(
                                            f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                        )
                                        yield (
                                            b"data: "
                                            + _json_dumps_str(
                                                {"error": {"message": "stream interrupted"}},
                                                ensure_ascii=False,
                                            ).encode()
                                            + b"\n\ndata: [DONE]\n\n"
                                        )
                                        return
                                    continue
                            ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                            # Log 401 body specifically for key diagnosis
                            if resp.status_code == 401:
                                _debug(f"  [auth] 401 response body: {_redact(err, 500)}")
                            # Convert 429/401/403 → 503 to avoid Claude Code auth window
                            if resp.status_code == 429:
                                err_msg = "All API keys exhausted (rate limited). Try again later."
                            elif resp.status_code in (401, 403):
                                err_msg = _auth_window_message(resp.status_code)
                            else:
                                err_msg = f"HTTP {resp.status_code}"
                            yield (
                                b"data: "
                                + _json_dumps_str(
                                    {"error": {"message": err_msg}}, ensure_ascii=False
                                ).encode()
                                + b"\n\ndata: [DONE]\n\n"
                            )
                            return

                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            if data_str == "[DONE]":
                                yield line.encode() + b"\n\n"
                                continue
                            try:
                                chunk = _json_loads(data_str)
                            except Exception:
                                yield line.encode() + b"\n\n"
                                continue
                            if chunk is None:
                                yield line.encode() + b"\n\n"
                                continue
                            chunk_usage = chunk.get("usage")
                            if isinstance(chunk_usage, dict):
                                actual_usage = chunk_usage
                            # Responses API stream (muse) : convertir avant de tester choices
                            if chunk.get("type", "").startswith("response."):
                                converted = _responses_sse_to_chat_deltas(data_str, parsed=chunk)
                                if converted is not None:
                                    chunk = converted
                                    # usage-only (completed/incomplete) → finaliser
                                    if (
                                        not chunk.get("choices")
                                        and isinstance(chunk, dict)
                                        and "usage" in chunk
                                    ):
                                        _debug(
                                            "  [oai-stream] response stream-end signal, breaking to finalize"
                                        )
                                        actual_usage = chunk.get("usage") or actual_usage
                                        break
                                    _oai_has_yielded = True
                                    yield f"data: {_json_dumps_str(chunk, ensure_ascii=False)}\n\n".encode()
                                    # reasoning_content / content compté plus bas via choices
                                    choices = chunk.get("choices", [])
                                    # ne pas re-tomber dans le test choices vide ci-dessous
                                    if not choices:
                                        continue
                            choices = chunk.get("choices", [])
                            if not choices or not isinstance(choices, list):
                                # 可能是 Responses API format — try converting (fallback legacy)
                                converted = _responses_sse_to_chat_deltas(data_str, parsed=chunk)
                                if converted is not None:
                                    chunk = converted
                                    choices = chunk.get("choices", [])
                                    if not choices and isinstance(chunk, dict) and "usage" in chunk:
                                        # response.completed or response.incomplete —
                                        # stream-end signal; don't yield raw Responses
                                        # API event, just break to finalize.
                                        _debug(
                                            "  [oai-stream] response stream-end signal, breaking to finalize"
                                        )
                                        actual_usage = chunk.get("usage") or actual_usage
                                        break
                                    # Yield converted chunk as chat/completions SSE
                                    _oai_has_yielded = True
                                    yield f"data: {_json_dumps_str(chunk, ensure_ascii=False)}\n\n".encode()
                                    continue
                            if choices and isinstance(choices, list) and len(choices) > 0:
                                delta = (
                                    choices[0].get("delta", {})
                                    if isinstance(choices[0], dict)
                                    else {}
                                )
                                if isinstance(delta, dict):
                                    c = delta.get("content")
                                    if isinstance(c, str):
                                        stream_out += _estimate_tokens(c)
                                    rc = delta.get("reasoning_content") or delta.get("reasoning")
                                    if isinstance(rc, str):
                                        stream_out += _estimate_tokens(rc)
                                    for tc in delta.get("tool_calls") or []:
                                        if isinstance(tc, dict) and "name" in tc.get(
                                            "function", {}
                                        ):
                                            tc_idx = tc.get("index", len(seen_tool_indices))
                                            if tc_idx not in seen_tool_indices:
                                                seen_tool_indices.add(tc_idx)
                                                used_tools.append(tc["function"]["name"])
                                # Track finish_reason for truncated-stream detection
                                _fr = (
                                    choices[0].get("finish_reason")
                                    if isinstance(choices[0], dict)
                                    else None
                                )
                                if _fr is not None:
                                    emitted_finish = True
                            _oai_has_yielded = True
                            yield line.encode() + b"\n\n"

                        # Fix: synthesize finish_reason if truncated (EOF without finish_reason)
                        if not emitted_finish:
                            if _oai_has_yielded or stream_out > 0:
                                _debug(
                                    "  [oai-stream] truncated without finish_reason → synthesizing stop"
                                )
                                _log(
                                    "  stream truncated without finish_reason → synthesizing stop"
                                )
                                _synth = {
                                    "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": original_model,
                                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                }
                                yield f"data: {_json_dumps_str(_synth, ensure_ascii=False)}\n\n".encode()
                                yield b"data: [DONE]\n\n"
                                emitted_finish = True
                            elif actual_usage is not None:
                                # Responses API completed but no finish yet — also synthesize
                                _debug(
                                    "  [oai-stream] synthesizing finish for Responses/empty stream"
                                )
                                _synth = {
                                    "id": f"chatcmpl-{uuid.uuid4().hex[:8]}",
                                    "object": "chat.completion.chunk",
                                    "created": int(time.time()),
                                    "model": original_model,
                                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                                }
                                yield f"data: {_json_dumps_str(_synth, ensure_ascii=False)}\n\n".encode()
                                yield b"data: [DONE]\n\n"
                                emitted_finish = True
                            else:
                                # Nothing yielded — emit error to avoid silent failure
                                _debug(
                                    "  [oai-stream] no content yielded, emitting error termination"
                                )
                                _err = {
                                    "error": {
                                        "message": "stream truncated without content",
                                        "type": "api_error",
                                    }
                                }
                                yield (
                                    b"data: "
                                    + _json_dumps_str(_err, ensure_ascii=False).encode()
                                    + b"\n\ndata: [DONE]\n\n"
                                )
                                emitted_finish = True
                        # Stream ended — finalize tracking
                        # [B1] résout l'estimation différée avant réconciliation
                        est_input = await _ensure_est_input()
                        final_in, final_out, final_cache, log_tag = _finalize_stream_tokens(
                            _track_model,
                            est_input,
                            None,
                            stream_out,
                            0,
                            actual_usage,
                            _token_usage,
                            _token_lock,
                        )
                        ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                        if _using_free:
                            _log_free_model_usage(
                                model_id,
                                free_model,
                                "free (no auth)",
                                "free (no auth)",
                                200,
                                final_in or 0,
                                final_out or 0,
                                _elapsed_ms(start_time),
                                ip=_free_usage_ip(),
                            )
                        await _save_and_log_request(
                            req_id,
                            _track_model,
                            original_model,
                            start_time,
                            final_in,
                            final_out,
                            final_cache,
                            protocol,
                            True,
                            thinking_type,
                            effort,
                            client_ip,
                            ak_h,
                            tool_names,
                            log_tag,
                            tools_used=used_tools if used_tools else None,
                            request_body=request_body,
                        )
                        _cb_record_success(endpoint)  # Stream completed successfully
                except asyncio.CancelledError:
                    # [plan 18/08 §am.22/piège 19] — same watchdog-cancel
                    # handling as the anthropic stream handler (see there).
                    st = _free_attempt_station()
                    if st is not None and _attempt == 0 and _is_watchdog_cancelled(st):
                        _debug(
                            f"  ⟳ stream watchdog-cancelled (dead tunnel, station {getattr(st, '_station', '?')}) — failover retry"
                        )
                        _log(
                            "  FREE STREAM on confirmed-dead tunnel cancelled → switching station"
                        )
                        if _oai_has_yielded or stream_out > 0:
                            _cb_record_failure(endpoint)
                            _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                            yield (
                                b"data: "
                                + _json_dumps_str(
                                    {"error": {"message": "stream interrupted"}}, ensure_ascii=False
                                ).encode()
                                + b"\n\ndata: [DONE]\n\n"
                            )
                            return
                        continue
                    raise
                except _FreeTunnelFailure as ftf:
                    # [plan 19/08 §2] station-first: dead tunnel → retry free
                    # on a FRESH station (fresh IP = fresh quota) before any
                    # direct residential fallback. _using_free stays True →
                    # next attempt re-enters free with fresh_station=True
                    # (bad-mark → exclusion of the dead station).
                    _cb_record_failure(endpoint)  # Record failure for circuit breaker
                    _log(
                        f"  FREE STREAM via station {getattr(ftf.station, '_station', '?')} tunnel FAILED ({ftf.cause}) → retry station fraîche"
                    )
                    if _oai_has_yielded or stream_out > 0:
                        _cb_record_failure(endpoint)
                        _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                        yield (
                            b"data: "
                            + _json_dumps_str(
                                {"error": {"message": "stream interrupted"}}, ensure_ascii=False
                            ).encode()
                            + b"\n\ndata: [DONE]\n\n"
                        )
                        return
                    continue
                except Exception as e:
                    _cb_record_failure(endpoint)  # Record failure for circuit breaker
                    _log(f"  ERROR stream (attempt {_attempt + 1}): {type(e).__name__}: {e}")
                    _debug(
                        f"  ✗ stream exception: {type(e).__name__}: {e}\n{traceback.format_exc()}"
                    )
                    if _attempt == 0:
                        # Network errors (server disconnect, timeout) → retry with same key first
                        _is_network_error = (
                            isinstance(
                                e,
                                (
                                    httpx.RemoteProtocolError,
                                    httpx.ReadError,
                                    ConnectionError,
                                    OSError,
                                ),
                            )
                            or "disconnected" in str(e).lower()
                        )
                        if _is_network_error:
                            _debug("  ⟳ stream retry (network error, same key)")
                            _log("  Retrying stream (network error, same key)")
                            await asyncio.sleep(1.0)
                            if _oai_has_yielded or stream_out > 0:
                                _cb_record_failure(endpoint)
                                _log(
                                    f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                )
                                yield (
                                    b"data: "
                                    + _json_dumps_str(
                                        {"error": {"message": "stream interrupted"}},
                                        ensure_ascii=False,
                                    ).encode()
                                    + b"\n\ndata: [DONE]\n\n"
                                )
                                return
                            continue
                        # Auth/quota errors → try alternative key
                        failed_key = _key_from_headers(hdrs, "openai")
                        if not _key_pauser.is_paused(failed_key):
                            try:
                                await _pause_key_for_quota_reset(failed_key)
                            except Exception:
                                _default_pause = float(yaml_get("key_pause", "default_pause", 60))
                                _key_pauser.pause_key(
                                    failed_key, _default_pause, "stream exception"
                                )
                        alt = _find_alternative_key(failed_key)
                        if alt:
                            _debug(f"  ⟳ stream retry with alt key: alias={alt.get('alias', '?')}")
                            _log(f"  Retrying stream with alternative key: {alt.get('alias', '?')}")
                            hdrs = _get_auth_headers("openai", entry=alt)
                        if _oai_has_yielded or stream_out > 0:
                            _cb_record_failure(endpoint)
                            _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                            yield (
                                b"data: "
                                + _json_dumps_str(
                                    {"error": {"message": "stream interrupted"}}, ensure_ascii=False
                                ).encode()
                                + b"\n\ndata: [DONE]\n\n"
                            )
                            return
                        continue
                    try:
                        # [B1] résout l'estimation différée avant le rollback
                        est_input = await _ensure_est_input()
                        with _token_lock:
                            _token_usage[_track_model]["input"] -= est_input
                    except Exception as e:
                        _debug(f"  ✗ token rollback failed: {type(e).__name__}: {e}")
                    ak_h = _alias_for_key(_key_from_headers(hdrs, "openai"))
                    await _log_and_save_error(
                        req_id,
                        _track_model,
                        original_model,
                        start_time,
                        0,
                        str(e),
                        protocol,
                        True,
                        thinking_type,
                        effort,
                        client_ip,
                        ak_h,
                        tool_names,
                        request_body=request_body,
                    )
                    return
                else:
                    break
            else:
                return

        return StreamingResponse(
            _sse_keepalive(openai_stream(headers)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

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
    effort = (
        anthro_body.get("effort")
        or (thinking.get("effort") if isinstance(thinking, dict) else None)
        or "none"
    )
    try:
        a_headers = _get_auth_headers("anthropic")
    except AllKeysPausedError as e:
        # If a free model exists, try it before giving up
        if FREE_MODEL_MAP.get(model_id):
            if is_stream:
                # Streaming with no API key: free model will be tried by _anthro_to_oai_stream on next normal attempt
                return _anthropic_error(503, "All API keys paused — free model will be tried on next attempt")
            else:
                # Non-streaming: try free model
                try:
                    free_result = await _try_free_model_first(
                        anthro_body,
                        {},
                        "anthropic",
                        model_id,
                        forced_pool=getattr(request.state, "_geo_forced_pool", None),
                    )
                    if free_result is not None:
                        resp, _, _actual_model, _actual_ip = free_result
                        data = (
                            resp.json()
                            if resp.headers.get("content-type", "").startswith("application/json")
                            else {}
                        )
                        usage = data.get("usage", {})
                        req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                        req_out = usage.get("output_tokens", 0) or usage.get("completion_tokens", 0)
                        req_cache = usage.get("cache_read_input_tokens", 0)
                        _update_token_usage(_actual_model, req_in, req_out, req_cache)
                        used = [
                            b["name"]
                            for b in data.get("content", [])
                            if b.get("type") == "tool_use"
                        ]
                        await _save_and_log_request(
                            req_id,
                            _actual_model,
                            original_model,
                            start_time,
                            req_in,
                            req_out,
                            req_cache,
                            protocol,
                            False,
                            thinking_type,
                            effort,
                            client_ip,
                            "free (no auth)",
                            tool_names,
                            tools_used=used,
                            request_body=request_body,
                            response_body=data,
                            free_model_ip=_actual_ip,
                        )
                        oai_response = anthropic_to_openai_response(data, original_model)
                        return Response(
                            content=_json_dumps_str(oai_response, ensure_ascii=False),
                            media_type="application/json",
                        )
                except FreeQuotaExhausted as e:
                    return _free_quota_exhausted_response(e, "anthropic")
                except Exception as e:
                    _debug(f"  [free] free model attempt failed: {e}")
        retry_after = int(e.retry_after) + 1
        return Response(
            content=_json_dumps_str(
                {
                    "error": {
                        "message": f"All API keys exhausted. Retry after {retry_after}s.",
                        "type": "api_error",
                    }
                }
            ),
            status_code=503,
            media_type="application/json",
            headers={"Retry-After": str(retry_after)},
        )

    if not is_stream:
        # Try free model first if available
        try:
            free_result = await _try_free_model_first(
                anthro_body,
                a_headers,
                "anthropic",
                model_id,
                forced_pool=getattr(request.state, "_geo_forced_pool", None),
            )
            if free_result is not None:
                resp, a_headers, _actual_model, _actual_ip = free_result
                model_id = _actual_model
            else:
                resp, a_headers = await _do_request_with_retry(
                    endpoint, anthro_body, a_headers, "anthropic"
                )
        except FreeQuotaExhausted as e:
            return _free_quota_exhausted_response(e, "anthropic")
        except UpstreamError as e:
            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
        account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
        if resp.status_code != 200:
            await _log_and_save_error(
                req_id,
                model_id,
                original_model,
                start_time,
                resp.status_code,
                resp.text,
                protocol,
                is_stream,
                thinking_type,
                effort,
                client_ip,
                account_alias,
                tool_names,
                request_body=request_body,
                response_body={"error": resp.text[:2000]},
            )
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
            oai_err = _json_dumps_str(
                {"error": {"message": err_msg, "type": "api_error"}}, ensure_ascii=False
            )
            return Response(
                content=oai_err, status_code=resp.status_code, media_type="application/json"
            )

        try:
            data = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
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
        await _save_and_log_request(
            req_id,
            model_id,
            original_model,
            start_time,
            req_in,
            req_out,
            req_cache,
            protocol,
            is_stream,
            thinking_type,
            effort,
            client_ip,
            account_alias,
            tool_names,
            tools_used=used if used else None,
            request_body=request_body,
            response_body=data,
        )

        oai_response = anthropic_to_openai_response(data, original_model)
        return Response(
            content=_json_dumps_str(oai_response, ensure_ascii=False), media_type="application/json"
        )

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
            endpoint = _cfg_settings._free_endpoint_for(free_model)
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
        total_input = 0
        cache_read = 0
        emitted_finish = False
        _handle_429 = _make_stream_retry_loop("anthropic")

        def _chunk(delta_override, finish):
            c = {
                "id": _id,
                "object": "chat.completion.chunk",
                "created": _created,
                "model": original_model,
                "choices": [{"index": 0, "delta": delta_override, "finish_reason": finish}],
            }
            return b"data: " + _json_dumps_str(c, ensure_ascii=False).encode() + b"\n\n"

        _geo_tunnel = getattr(request.state, "_geo_force_tunnel", False)
        _free_forced_pool = getattr(request.state, "_geo_forced_pool", None)
        _free_bound = yaml_get("streaming", "retry_attempts", 2)
        if _using_free and _free_attempts_active(_free_forced_pool):
            # [plan 19/08 §1] free multi-attempt: extra free strikes on
            # fresh stations; paid retry budget preserved
            # (max_free_attempts=1 ⇒ exact legacy behaviour).
            _free_bound += max(0, effective_free_max_attempts(_free_forced_pool) - 1)
        for _attempt in range(_free_bound):
            _line_buf = ""  # fresh per attempt — avoids stale truncated data: re-parse
            try:
                # Axe A: geo-restricted paid streaming → route through tunnel station
                _stream_ctx = (
                    _open_via_pool(
                        endpoint, anthro_body, hdrs, is_stream=True, forced_pool=_free_forced_pool
                    )
                    if _geo_tunnel
                    else _open_free_stream(
                        endpoint,
                        anthro_body,
                        hdrs,
                        _using_free,
                        count_request=(_attempt == 0),
                        fresh_station=(_attempt > 0 and _using_free),
                        direct_fallback=(
                            _free_exception_fallback_mode() != "station-first"
                            or _attempt + 1 >= effective_free_max_attempts(_free_forced_pool)
                        ),
                        forced_pool=_free_forced_pool,
                    )
                )
                async with _stream_ctx as resp:
                    if resp.status_code != 200:
                        if _using_free:
                            # Free endpoint non-200 (429 quota, 5xx...) → cooldown
                            # the free model, fall back to paid. Never pauses PAID
                            # keys: a status from the free endpoint says nothing
                            # about the paid account (CRITIC(2)/CRITIC(3)).
                            if resp.status_code == 429:
                                _refuse = _on_free_429_stream(
                                    free_model,
                                    resp.headers.get("retry-after", ""),
                                    forced_pool=_free_forced_pool,
                                )
                                if not _refuse and _attempt + 1 < effective_free_max_attempts(
                                    _free_forced_pool
                                ):
                                    # [plan 19/08 §1] budget left → retry free on a
                                    # FRESH station (fresh IP = fresh quota); keep
                                    # _using_free so the next attempt re-enters free
                                    # with fresh_station=True. Cooldown + rotation
                                    # for the exhausted IP already done above.
                                    _log_free_model_usage(
                                        model_id,
                                        free_model,
                                        "free (no auth)",
                                        "free (no auth)",
                                        resp.status_code,
                                        ip=_free_usage_ip(),
                                    )
                                    _log(
                                        f"  FREE {free_model!r} RATE LIMITED (429) → retry station fraîche (essai {_attempt + 2}/{effective_free_max_attempts(_free_forced_pool)})"
                                    )
                                    if _stream_has_yielded(
                                        started, open_blocks, stream_out, _line_buf
                                    ):
                                        _cb_record_failure(endpoint)
                                        _log(
                                            f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                        )
                                        yield _chunk({}, "stop")
                                        yield b"data: [DONE]\n\n"
                                        return
                                    continue
                            else:
                                _set_free_cooldown(free_model, 60, _free_attempt_station())
                                _refuse = False
                            anthro_body = paid_anthro_body
                            endpoint = paid_endpoint
                            _using_free = False
                            _track_model = model_id
                            _debug(
                                f"  [anthro-to-oai-stream] free model {resp.status_code} → falling back to paid {_track_model!r}"
                            )
                            _log(
                                f"  FREE model {resp.status_code} → falling back to paid {_track_model!r}"
                            )
                            _log_free_model_usage(
                                model_id,
                                free_model,
                                "free (no auth)",
                                "free (no auth)",
                                resp.status_code,
                                ip=_free_usage_ip(),
                            )
                            if _refuse:
                                # strict_free (GUI): every station exhausted
                                # (bad/down + (model, IP) cooldown active) —
                                # refuse instead of paying.
                                _retry_after = resp.headers.get("retry-after", "") or "60"
                                yield await _stream_error_response(
                                    req_id,
                                    free_model,
                                    original_model,
                                    start_time,
                                    429,
                                    await resp.aread(),
                                    protocol,
                                    thinking_type,
                                    effort,
                                    client_ip,
                                    "free (no auth)",
                                    tool_names,
                                    {
                                        "type": "error",
                                        "error": {
                                            "type": "rate_limit_error",
                                            "message": f"Free quota exhausted on all VPN stations. Retry after {_retry_after}s.",
                                        },
                                    },
                                    request_body=request_body,
                                )
                                return
                            continue
                        hdrs, should_retry = await _handle_429(
                            hdrs, resp.status_code, _attempt, resp.headers
                        )
                        if should_retry:
                            if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                                _cb_record_failure(endpoint)
                                _log(
                                    f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                )
                                yield _chunk({}, "stop")
                                yield b"data: [DONE]\n\n"
                                return
                            continue
                        if resp.status_code == 499:
                            wait = 1.0 * (2**_attempt)
                            _debug(
                                f"  [anthro-to-oai-stream] upstream 499, retrying in {wait:.1f}s (attempt {_attempt + 1})"
                            )
                            await asyncio.sleep(wait)
                            if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                                _cb_record_failure(endpoint)
                                _log(
                                    f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                )
                                yield _chunk({}, "stop")
                                yield b"data: [DONE]\n\n"
                                return
                            continue
                        err = await resp.aread()
                        # Pause key on credit/balance errors (400)
                        if resp.status_code == 400 and any(
                            x in err.decode(errors="ignore")
                            for x in ("Insufficient balance", "Monthly usage limit")
                        ):
                            failed_key = hdrs.get("x-api-key", "")
                            _key_pauser.pause_key(
                                failed_key, _key_pauser._max_pause, "400 credit error"
                            )
                            alt = _find_alternative_key(failed_key)
                            if alt:
                                _log("  400 credit error on key, retrying with alternative key")
                                hdrs = {
                                    "x-api-key": alt.get("api_key", ""),
                                    "Content-Type": "application/json",
                                    "anthropic-version": "2023-06-01",
                                }
                                if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                                    _cb_record_failure(endpoint)
                                    _log(
                                        f"  stream_retry_suppressed_after_started attempt={_attempt + 1}"
                                    )
                                    yield _chunk({}, "stop")
                                    yield b"data: [DONE]\n\n"
                                    return
                                continue
                        ak = _alias_for_key(hdrs.get("x-api-key", ""))
                        # Log 401 body specifically for key diagnosis
                        if resp.status_code == 401:
                            _debug(f"  [auth] 401 response body: {_redact(err, 500)}")
                        await _log_and_save_error(
                            req_id,
                            model_id,
                            original_model,
                            start_time,
                            resp.status_code,
                            str(resp.status_code),
                            protocol,
                            True,
                            thinking_type,
                            effort,
                            client_ip,
                            ak,
                            tool_names,
                            request_body=request_body,
                        )
                        # Convert 429/401/403 → 503 to avoid Claude Code auth window
                        if resp.status_code == 429:
                            err_msg = "All API keys exhausted (rate limited). Try again later."
                        elif resp.status_code in (401, 403):
                            err_msg = _auth_window_message(resp.status_code)
                        else:
                            err_msg = f"HTTP {resp.status_code}"
                        yield (
                            b"data: "
                            + _json_dumps_str(
                                {"error": {"message": err_msg}}, ensure_ascii=False
                            ).encode()
                            + b"\n\ndata: [DONE]\n\n"
                        )
                        return

                    async for raw in resp.aiter_bytes():
                        _line_buf += raw.decode("utf-8", errors="replace")
                        if len(_line_buf) > 1_000_000:
                            # Truncate on newline boundary to avoid splitting JSON
                            _keep = 1000
                            _tail = _line_buf[-_keep:]
                            _nl = _tail.find("\n")
                            if _nl != -1:
                                _line_buf = _tail[_nl + 1 :]
                            else:
                                _debug(
                                    f"  [_anthro_to_oai] line_buf truncated mid-JSON (no newline in last {_keep})"
                                )
                                _line_buf = _tail
                        while "\n" in _line_buf:
                            line, _line_buf = _line_buf.split("\n", 1)
                            line = line.strip()
                            if not line.startswith("data:"):
                                continue
                            data_str = line[5:].strip()
                            try:
                                ev = _json_loads(data_str)
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
                                    yield _chunk(
                                        {
                                            "tool_calls": [
                                                {
                                                    "index": idx,
                                                    "id": tool_data[idx]["id"],
                                                    "type": "function",
                                                    "function": {
                                                        "name": tool_data[idx]["name"],
                                                        "arguments": "",
                                                    },
                                                }
                                            ]
                                        },
                                        None,
                                    )

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
                                        yield _chunk(
                                            {
                                                "tool_calls": [
                                                    {
                                                        "index": idx,
                                                        "id": tool_data[idx]["id"],
                                                        "type": "function",
                                                        "function": {
                                                            "name": tool_data[idx]["name"],
                                                            "arguments": pj,
                                                        },
                                                    }
                                                ]
                                            },
                                            None,
                                        )

                            elif etype == "content_block_stop":
                                idx = ev.get("index")
                                open_blocks.discard(idx)

                            elif etype == "message_delta":
                                d = ev.get("delta", {})
                                u = ev.get("usage", {})
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
                                emitted_finish = True

                            elif etype == "message_stop":
                                _update_token_usage(
                                    _track_model, total_input, stream_out, cache_read
                                )
                                ak = _alias_for_key(hdrs.get("x-api-key", ""))
                                if _using_free:
                                    _log_free_model_usage(
                                        model_id,
                                        free_model,
                                        "free (no auth)",
                                        "free (no auth)",
                                        200,
                                        total_input or 0,
                                        stream_out or 0,
                                        _elapsed_ms(start_time),
                                        ip=_free_usage_ip(),
                                    )
                                used_tools = [
                                    v["name"] for v in tool_data.values() if v.get("name")
                                ]
                                await _save_and_log_request(
                                    req_id,
                                    _track_model,
                                    original_model,
                                    start_time,
                                    total_input,
                                    stream_out,
                                    cache_read,
                                    protocol,
                                    True,
                                    thinking_type,
                                    effort,
                                    client_ip,
                                    ak,
                                    tool_names,
                                    tools_used=used_tools if used_tools else None,
                                    request_body=request_body,
                                )
                                # Send final usage chunk for OpenAI streaming client
                                total = total_input + stream_out
                                usage_chunk = {
                                    "id": _id,
                                    "object": "chat.completion.chunk",
                                    "created": _created,
                                    "model": original_model,
                                    "choices": [],
                                    "usage": {
                                        "prompt_tokens": total_input,
                                        "completion_tokens": stream_out,
                                        "total_tokens": total,
                                    },
                                }
                                if cache_read:
                                    usage_chunk["usage"]["prompt_tokens_details"] = {
                                        "cached_tokens": cache_read
                                    }
                                yield (
                                    b"data: "
                                    + _json_dumps_str(usage_chunk, ensure_ascii=False).encode()
                                    + b"\n\n"
                                )
                                yield b"data: [DONE]\n\n"
                                _cb_record_success(endpoint)  # Stream completed successfully
                                return
            except asyncio.CancelledError:
                # [plan 18/08 §am.22/piège 19] — same watchdog-cancel
                # handling as the anthropic stream handler (see there).
                st = _free_attempt_station()
                if st is not None and _attempt == 0 and _is_watchdog_cancelled(st):
                    _debug(
                        f"  ⟳ stream watchdog-cancelled (dead tunnel, station {getattr(st, '_station', '?')}) — failover retry"
                    )
                    _log("  FREE STREAM on confirmed-dead tunnel cancelled → switching station")
                    if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                        _cb_record_failure(endpoint)
                        _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                        yield _chunk({}, "stop")
                        yield b"data: [DONE]\n\n"
                        return
                    continue
                raise
            except _FreeTunnelFailure as ftf:
                # [plan 19/08 §2] station-first: dead tunnel → retry free
                # on a FRESH station (fresh IP = fresh quota) before any
                # direct residential fallback. _using_free stays True →
                # next attempt re-enters free with fresh_station=True
                # (bad-mark → exclusion of the dead station).
                _cb_record_failure(endpoint)  # Record failure for circuit breaker
                _log(
                    f"  FREE STREAM via station {getattr(ftf.station, '_station', '?')} tunnel FAILED ({ftf.cause}) → retry station fraîche"
                )
                if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                    _cb_record_failure(endpoint)
                    _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                    yield _chunk({}, "stop")
                    yield b"data: [DONE]\n\n"
                    return
                continue
            except Exception as e:
                _cb_record_failure(endpoint)  # Record failure for circuit breaker
                _log(f"  ERROR stream (attempt {_attempt + 1}): {type(e).__name__}: {e}")
                _debug(f"  ✗ stream exception: {type(e).__name__}: {e}\n{traceback.format_exc()}")
                if _attempt == 0:
                    # Network errors (server disconnect, timeout) → retry with same key first
                    _is_network_error = (
                        isinstance(
                            e,
                            (httpx.RemoteProtocolError, httpx.ReadError, ConnectionError, OSError),
                        )
                        or "disconnected" in str(e).lower()
                    )
                    if _is_network_error:
                        _debug("  ⟳ stream retry (network error, same key)")
                        _log("  Retrying stream (network error, same key)")
                        await asyncio.sleep(1.0)
                        if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                            _cb_record_failure(endpoint)
                            _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                            yield _chunk({}, "stop")
                            yield b"data: [DONE]\n\n"
                            return
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
                    if _stream_has_yielded(started, open_blocks, stream_out, _line_buf):
                        _cb_record_failure(endpoint)
                        _log(f"  stream_retry_suppressed_after_started attempt={_attempt + 1}")
                        yield _chunk({}, "stop")
                        yield b"data: [DONE]\n\n"
                        return
                    continue
                ak = _alias_for_key(hdrs.get("x-api-key", ""))
                await _save_request(
                    req_id,
                    model_id,
                    original_model,
                    _elapsed_ms(start_time),
                    total_input or 0,
                    stream_out,
                    0,
                    success=False,
                    error=str(e),
                    protocol=protocol,
                    is_stream=True,
                    thinking=thinking_type,
                    effort=effort,
                    client_ip=client_ip,
                    account_alias=ak,
                    tools=tool_names,
                    request_body=request_body,
                )
                if total_input:
                    try:
                        with _token_lock:
                            _token_usage[model_id]["input"] += total_input
                    except Exception:
                        pass
                if started:
                    yield _chunk({}, "stop")
                    yield b"data: [DONE]\n\n"
                    emitted_finish = True
                return
            else:
                break
        else:
            return
        # Fix: guarantee finish_reason on truncated stream (EOF without message_stop)
        if started and not emitted_finish:
            # Check remaining buffer for a final event before synthesis
            if _line_buf.strip():
                _rem = _line_buf.strip()
                if _rem.startswith("data:"):
                    _rem = _rem[5:].strip()
                try:
                    _ev = _json_loads(_rem)
                    if isinstance(_ev, dict) and _ev.get("type") == "message_stop":
                        emitted_finish = True
                    elif (
                        isinstance(_ev, dict)
                        and _ev.get("type") == "message_delta"
                        and _ev.get("delta", {}).get("stop_reason")
                    ):
                        emitted_finish = True
                        yield _chunk({}, "stop")
                        yield b"data: [DONE]\n\n"
                        return
                except Exception:
                    pass
            if not emitted_finish:
                _debug("  [_anthro_to_oai] truncated without finish_reason → synthesizing stop")
                _log("  stream truncated without finish_reason → synthesizing stop")
                yield _chunk({}, "stop")
                yield b"data: [DONE]\n\n"
                emitted_finish = True

    return StreamingResponse(
        _sse_keepalive(_anthro_to_oai_stream(a_headers)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


@app.post("/v1/responses")
async def responses(request: Request):
    req_id = _fast_id("resp")
    start_time = time.monotonic()
    client_ip = _get_client_ip(request)
    _current_user_agent.set(request.headers.get("user-agent"))

    body_bytes = await request.body()
    _debug(
        f"  [body] read {len(body_bytes)} bytes in {(time.monotonic() - start_time) * 1000:.0f}ms"
    )
    if len(body_bytes) > MAX_BODY_SIZE:
        _debug(f"  413: body too large ({len(body_bytes)} bytes)")
        return _openai_error(
            413, f"Request body too large ({len(body_bytes)} bytes, max {MAX_BODY_SIZE})"
        )

    try:
        body = _json_loads(body_bytes)
    except Exception:
        _debug("  400: invalid JSON body")
        return _openai_error(400, "invalid json")

    body = ensure_min_tokens(body)

    original_model = body.get("model", "")
    tool_names = _extract_tool_names(body)
    request_body = body  # Capture original request before mutation
    route = _route_for(original_model)
    if route is None:
        available = sorted(MODELS.keys())
        return JSONResponse(
            status_code=404,
            content={
                "error": f"Model not found: {original_model!r}",
                "available_models": available,
            },
        )
    model_id = route["model"]
    cfg = get_model_config(model_id)
    endpoint = cfg["endpoint"]
    protocol = cfg["protocol"]
    is_stream = body.get("stream", False)

    _log(
        f"→ {original_model!r} → {model_id} | {protocol} | responses | stream={is_stream} | ip={client_ip}"
    )

    _geo_gate = await _enforce_geo_gate(
        route, request, is_stream=bool(is_stream), protocol=protocol
    )
    if _geo_gate is not None:
        return _geo_gate

    # Circuit breaker check
    if not _cb_should_allow(endpoint):
        _log(f"  CIRCUIT BREAKER OPEN — fast-failing request to {endpoint}")
        return _openai_error(503, "Service temporarily unavailable (circuit breaker open)")

    # Convert Responses API → Anthropic format
    anthro_body = openai_responses_to_anthropic(body)
    anthro_body["model"] = model_id

    # Tool filtering removed — all tools are forwarded as-is

    # Handle web_search / web_fetch (v3.3) — F3 protocol-correct
    if protocol == "anthropic":
        await _handle_web_search(anthro_body, model_id, "anthropic")
        await _handle_web_fetch(anthro_body, model_id, "anthropic")
    else:
        await _handle_web_search(body, model_id, "openai")
        await _handle_web_fetch(body, model_id, "openai")
        # resync anthro_body from mutated body (F3)
        anthro_body = openai_responses_to_anthropic(body)
        anthro_body["model"] = model_id
    # ── Orphan guard (handler-level) ──
    if isinstance(body, dict):
        if "messages" in body:
            body["messages"] = _drop_orphan_tool_messages(body["messages"])
        elif "input" in body:
            body["input"] = _drop_orphan_responses_input(body["input"])
    if isinstance(anthro_body, dict) and "messages" in anthro_body:
        # Anthropic body itself not filtered here (upstream is Anthropic), but keep for completeness
        pass

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
    effort = (
        anthro_body.get("effort")
        or (thinking.get("effort") if isinstance(thinking, dict) else None)
        or "none"
    )

    # ── Anthropic backend (passthrough) ─────────────────────
    if protocol == "anthropic":
        try:
            a_headers = _get_auth_headers("anthropic")
        except AllKeysPausedError as e:
            # If a free model exists, try it before giving up
            if FREE_MODEL_MAP.get(model_id):
                if is_stream:
                    # Streaming with no API key: free model will be tried on next normal attempt
                    return _anthropic_error(503, "All API keys paused — free model will be tried on next attempt")
                else:
                    # Non-streaming: try free model
                    try:
                        free_result = await _try_free_model_first(
                            anthro_body,
                            {},
                            "anthropic",
                            model_id,
                            forced_pool=getattr(request.state, "_geo_forced_pool", None),
                        )
                        if free_result is not None:
                            resp, _, _actual_model, _actual_ip = free_result
                            data = (
                                resp.json()
                                if resp.headers.get("content-type", "").startswith(
                                    "application/json"
                                )
                                else {}
                            )
                            usage = data.get("usage", {})
                            req_in = usage.get("input_tokens", 0) or usage.get("prompt_tokens", 0)
                            req_out = usage.get("output_tokens", 0) or usage.get(
                                "completion_tokens", 0
                            )
                            req_cache = usage.get("cache_read_input_tokens", 0)
                            _update_token_usage(_actual_model, req_in, req_out, req_cache)
                            used = [
                                b["name"]
                                for b in data.get("content", [])
                                if b.get("type") == "tool_use"
                            ]
                            await _save_and_log_request(
                                req_id,
                                _actual_model,
                                original_model,
                                start_time,
                                req_in,
                                req_out,
                                req_cache,
                                protocol,
                                False,
                                thinking_type,
                                effort,
                                client_ip,
                                "free (no auth)",
                                tool_names,
                                tools_used=used,
                                request_body=request_body,
                                response_body=data,
                                free_model_ip=_actual_ip,
                            )
                            oai_resp = anthropic_to_openai_responses(data, original_model)
                            payload = _json_dumps_str(
                                {"type": "response.completed", "response": oai_resp},
                                ensure_ascii=False,
                            )
                            sse_body = f"data: {payload}\n\ndata: [DONE]\n\n".encode()
                            return Response(
                                content=sse_body,
                                media_type="text/event-stream",
                                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                            )
                    except FreeQuotaExhausted as e:
                        return _free_quota_exhausted_response(e, "anthropic")
                    except Exception as e:
                        _debug(f"  [free] free model attempt failed: {e}")
            retry_after = int(e.retry_after) + 1
            return Response(
                content=_json_dumps_str(
                    {
                        "error": {
                            "message": f"All API keys exhausted. Retry after {retry_after}s.",
                            "type": "api_error",
                        }
                    }
                ),
                status_code=503,
                media_type="application/json",
                headers={"Retry-After": str(retry_after)},
            )
        if not is_stream:
            # Response cache (mirrors the anthropic/chat handlers)
            cache_key = _response_cache.make_key(body, body_bytes=body_bytes)
            cached = cache_key and _response_cache.get(cache_key)
            if cached:
                cached_body, cached_headers = cached
                _debug(f"  [responses] response cache HIT ({len(cached_body)} bytes)")
                return Response(
                    content=cached_body,
                    headers={**cached_headers, "X-Cache": "HIT"},
                    media_type="application/json",
                )
            # Try free model first if available
            _geo_tunnel = getattr(request.state, "_geo_force_tunnel", False)
            try:
                free_result = await _try_free_model_first(
                    anthro_body,
                    a_headers,
                    "anthropic",
                    model_id,
                    forced_pool=getattr(request.state, "_geo_forced_pool", None),
                )
                if free_result is not None:
                    resp, a_headers, _actual_model, _actual_ip = free_result
                    model_id = _actual_model
                elif _geo_tunnel:
                    # Axe A: geo-restricted paid → must route through tunnel station
                    async with _open_via_pool(
                        endpoint,
                        anthro_body,
                        a_headers,
                        is_stream=False,
                        forced_pool=getattr(request.state, "_geo_forced_pool", None),
                    ) as resp:
                        a_headers = dict(resp.headers)
                else:
                    resp, a_headers = await _do_request_with_retry(
                        endpoint, anthro_body, a_headers, "anthropic"
                    )
            except FreeQuotaExhausted as e:
                return _free_quota_exhausted_response(e, "anthropic")
            except UpstreamError as e:
                return JSONResponse(status_code=e.status_code, content={"error": str(e)})
            account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
            if resp.status_code != 200:
                await _log_and_save_error(
                    req_id,
                    model_id,
                    original_model,
                    start_time,
                    resp.status_code,
                    resp.text,
                    protocol,
                    is_stream,
                    thinking_type,
                    effort,
                    client_ip,
                    account_alias,
                    tool_names,
                    request_body=request_body,
                    response_body={"error": resp.text[:2000]},
                )
                # Pause key on credit/balance errors (400) and retry with alt key
                if resp.status_code == 400 and any(
                    x in resp.text for x in ("Insufficient balance", "Monthly usage limit")
                ):
                    failed_key = a_headers.get("x-api-key", "")
                    _key_pauser.pause_key(
                        failed_key,
                        _key_pauser._max_pause,
                        f"400 credit error: {_redact(resp.text, 80)}",
                    )
                    alt = _find_alternative_key(failed_key)
                    if alt:
                        _log("  400 credit error on key, retrying with alternative key")
                        a_headers = {
                            "x-api-key": alt.get("api_key", ""),
                            "Content-Type": "application/json",
                            "anthropic-version": "2023-06-01",
                        }
                        try:
                            resp, a_headers = await _do_request_with_retry(
                                endpoint, anthro_body, a_headers, "anthropic"
                            )
                            if resp.status_code == 200:
                                account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
                            else:
                                return Response(
                                    content=_json_dumps_str(
                                        {
                                            "error": {
                                                "message": "All API keys exhausted. Check your billing."
                                            }
                                        }
                                    ),
                                    status_code=503,
                                    media_type="application/json",
                                )
                        except UpstreamError as e:
                            return JSONResponse(
                                status_code=e.status_code, content={"error": str(e)}
                            )
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
                return Response(
                    content=_json_dumps_str({"error": {"message": err_msg}}),
                    status_code=resp.status_code,
                    media_type="application/json",
                )
            try:
                data = (
                    resp.json()
                    if resp.headers.get("content-type", "").startswith("application/json")
                    else {}
                )
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
            await _save_and_log_request(
                req_id,
                model_id,
                original_model,
                start_time,
                req_in,
                req_out,
                req_cache,
                protocol,
                is_stream,
                thinking_type,
                effort,
                client_ip,
                account_alias,
                tool_names,
                tools_used=used if used else None,
                request_body=request_body,
                response_body=data,
            )
            oai_resp = anthropic_to_openai_responses(data, original_model)
            _response_body = _json_dumps_str(oai_resp, ensure_ascii=False).encode()
            if cache_key:
                _response_cache.put(cache_key, _response_body, {"Content-Type": "application/json"})
            return Response(content=_response_body, media_type="application/json")
        # Anthropic streaming → collect, then emit SSE
        anthro_body["stream"] = False
        # Try free model first if available
        _geo_tunnel = getattr(request.state, "_geo_force_tunnel", False)
        try:
            free_result = await _try_free_model_first(
                anthro_body,
                a_headers,
                "anthropic",
                model_id,
                forced_pool=getattr(request.state, "_geo_forced_pool", None),
            )
            if free_result is not None:
                resp, a_headers, _actual_model, _actual_ip = free_result
                model_id = _actual_model
            elif _geo_tunnel:
                # Axe A: geo-restricted paid → must route through tunnel station
                async with _open_via_pool(
                    endpoint,
                    anthro_body,
                    a_headers,
                    is_stream=False,
                    forced_pool=getattr(request.state, "_geo_forced_pool", None),
                ) as resp:
                    a_headers = dict(resp.headers)
            else:
                resp, a_headers = await _do_request_with_retry(
                    endpoint, anthro_body, a_headers, "anthropic"
                )
        except FreeQuotaExhausted as e:
            return _free_quota_exhausted_response(e, "anthropic")
        except UpstreamError as e:
            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
        account_alias = _alias_for_key(a_headers.get("x-api-key", ""))
        if resp.status_code != 200:
            await _log_and_save_error(
                req_id,
                model_id,
                original_model,
                start_time,
                resp.status_code,
                resp.text,
                protocol,
                True,
                thinking_type,
                effort,
                client_ip,
                account_alias,
                tool_names,
                request_body=request_body,
                response_body={"error": resp.text[:2000]},
            )

            async def err_stream():
                yield b"data: [DONE]\n\n"

            return StreamingResponse(
                err_stream(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
            )
        try:
            data = (
                resp.json()
                if resp.headers.get("content-type", "").startswith("application/json")
                else {}
            )
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
        await _save_and_log_request(
            req_id,
            model_id,
            original_model,
            start_time,
            req_in,
            req_out,
            req_cache,
            protocol,
            True,
            thinking_type,
            effort,
            client_ip,
            account_alias,
            tool_names,
            tools_used=used if used else None,
            request_body=request_body,
            response_body=data,
        )
        oai_resp = anthropic_to_openai_responses(data, original_model)
        payload = _json_dumps_str(
            {"type": "response.completed", "response": oai_resp}, ensure_ascii=False
        )
        sse_body = f"data: {payload}\n\ndata: [DONE]\n\n".encode()
        return Response(
            content=sse_body,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # ── OpenAI backend (double conversion) ──────────────────
    # Convert Anthropic → Chat Completions for the backend
    try:
        # [C1 perf] raw bytes du body client (dérivation déterministe vers
        # anthro_body) → clé de cache sans re-dumps
        oai_body = anthropic_to_openai(anthro_body, model_id, raw=body_bytes)
        # Convert to Responses API format if endpoint requires it (muse-spark)
        if "/responses" in endpoint:
            oai_body = _chat_to_responses_request(oai_body)
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
                free_result = await _try_free_model_first(
                    oai_body,
                    {},
                    "openai",
                    model_id,
                    forced_pool=getattr(request.state, "_geo_forced_pool", None),
                )
                if free_result is not None:
                    resp, _, _actual_model, _actual_ip = free_result
                    data = (
                        resp.json()
                        if resp.headers.get("content-type", "").startswith("application/json")
                        else {}
                    )
                    usage = data.get("usage", {})
                    req_in = usage.get("prompt_tokens", 0)
                    req_out = usage.get("completion_tokens", 0)
                    cache = _extract_cache_tokens(usage)
                    _update_token_usage(_actual_model, req_in, req_out, cache)
                    used = _extract_usage_tool_names(data)
                    await _save_and_log_request(
                        req_id,
                        _actual_model,
                        original_model,
                        start_time,
                        req_in,
                        req_out,
                        cache,
                        protocol,
                        False,
                        thinking_type,
                        effort,
                        client_ip,
                        "free (no auth)",
                        tool_names,
                        tools_used=used,
                        request_body=request_body,
                        response_body=data,
                        free_model_ip=_actual_ip,
                    )
                    oai_resp = openai_chat_to_responses(data, original_model)
                    payload = _json_dumps_str(
                        {"type": "response.completed", "response": oai_resp}, ensure_ascii=False
                    )
                    sse_body = f"data: {payload}\n\ndata: [DONE]\n\n".encode()
                    return Response(
                        content=sse_body,
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                    )
            except FreeQuotaExhausted as e:
                return _free_quota_exhausted_response(e, "openai")
            except Exception as e:
                _debug(f"  [free] free model attempt failed: {e}")
        retry_after = int(e.retry_after) + 1
        return Response(
            content=_json_dumps_str(
                {
                    "error": {
                        "message": f"All API keys exhausted. Retry after {retry_after}s.",
                        "type": "api_error",
                        "code": "503",
                    }
                }
            ),
            status_code=503,
            media_type="application/json",
            headers={"Retry-After": str(retry_after)},
        )
    is_stream = oai_body["stream"]

    if not is_stream:
        # Response cache (mirrors the anthropic/chat handlers)
        cache_key = _response_cache.make_key(body, body_bytes=body_bytes)
        cached = cache_key and _response_cache.get(cache_key)
        if cached:
            cached_body, cached_headers = cached
            _debug(f"  [responses] response cache HIT ({len(cached_body)} bytes)")
            return Response(
                content=cached_body,
                headers={**cached_headers, "X-Cache": "HIT"},
                media_type="application/json",
            )
        # Try free model first if available
        _geo_tunnel = getattr(request.state, "_geo_force_tunnel", False)
        try:
            free_result = await _try_free_model_first(
                oai_body,
                headers,
                "openai",
                model_id,
                forced_pool=getattr(request.state, "_geo_forced_pool", None),
            )
            if free_result is not None:
                resp, headers, _actual_model, _actual_ip = free_result
                model_id = _actual_model
            elif _geo_tunnel:
                # Axe A: geo-restricted paid → must route through tunnel station
                async with _open_via_pool(
                    endpoint,
                    oai_body,
                    headers,
                    is_stream=False,
                    forced_pool=getattr(request.state, "_geo_forced_pool", None),
                ) as resp:
                    headers = dict(resp.headers)
            else:
                resp, headers = await _do_request_with_retry(endpoint, oai_body, headers, "openai")
        except FreeQuotaExhausted as e:
            return _free_quota_exhausted_response(e, "openai")
        except UpstreamError as e:
            return JSONResponse(status_code=e.status_code, content={"error": str(e)})
        account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
        if resp.status_code != 200:
            await _log_and_save_error(
                req_id,
                model_id,
                original_model,
                start_time,
                resp.status_code,
                resp.text,
                protocol,
                is_stream,
                thinking_type,
                effort,
                client_ip,
                account_alias,
                tool_names,
                request_body=request_body,
                response_body={"error": resp.text[:2000]},
            )
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
            return Response(
                content=_json_dumps_str({"error": {"message": err_msg}}),
                status_code=resp.status_code,
                media_type="application/json",
            )
        try:
            data = resp.json()
        except Exception:
            _debug(f"  ✗ non-JSON response from {endpoint}")
            _log(f"  UPSTREAM DECODE ERROR: non-JSON response from {endpoint}")
            return _openai_error(502, "Upstream returned non-JSON response")
        # Detect Responses API format (has "output" key) vs Chat Completions (has "choices" key)
        is_responses_format = "output" in data and "choices" not in data
        usage = data.get("usage", {})
        if is_responses_format:
            req_in = usage.get("input_tokens", 0)
            req_out = usage.get("output_tokens", 0)
            _inp_det = (
                usage.get("input_tokens_details")
                if isinstance(usage.get("input_tokens_details"), dict)
                else {}
            )
            cache = _inp_det.get("cached_tokens", 0)
        else:
            req_in = usage.get("prompt_tokens", 0)
            req_out = usage.get("completion_tokens", 0)
            cache = _extract_cache_tokens(usage)
        _update_token_usage(model_id, req_in, req_out, cache)
        if is_responses_format:
            used = [
                b["name"]
                for b in data.get("output", [])
                if isinstance(b, dict) and b.get("type") == "function_call"
            ]
        else:
            used = _extract_usage_tool_names(data)
        await _save_and_log_request(
            req_id,
            model_id,
            original_model,
            start_time,
            req_in,
            req_out,
            cache,
            protocol,
            is_stream,
            thinking_type,
            effort,
            client_ip,
            account_alias,
            tool_names,
            tools_used=used if used else None,
            request_body=request_body,
            response_body=data,
        )
        if is_responses_format:
            # Upstream already returned Responses API format — normalize and pass through
            oai_resp = data
            oai_resp["model"] = original_model
            if "id" not in oai_resp or not oai_resp["id"].startswith("resp_"):
                oai_resp["id"] = f"resp_{uuid.uuid4().hex[:24]}"
        else:
            # Chat Completions format — convert to Responses API
            oai_resp = openai_chat_to_responses(data, original_model)
        _response_body = _json_dumps_str(oai_resp, ensure_ascii=False).encode()
        if cache_key:
            _response_cache.put(cache_key, _response_body, {"Content-Type": "application/json"})
        return Response(content=_response_body, media_type="application/json")

    # ── Streaming (OpenAI backend) — collect stream, emit Responses SSE ──
    # Try free model first if available
    _geo_tunnel = getattr(request.state, "_geo_force_tunnel", False)
    try:
        free_result = await _try_free_model_first(
            oai_body,
            headers,
            "openai",
            model_id,
            forced_pool=getattr(request.state, "_geo_forced_pool", None),
        )
        if free_result is not None:
            resp, headers, _actual_model, _actual_ip = free_result
            model_id = _actual_model
        elif _geo_tunnel:
            # Axe A: geo-restricted paid → must route through tunnel station.
            # _open_via_pool closes the response on exit, so the entire
            # streaming collection must happen inside this block.
            async with _open_via_pool(
                endpoint,
                oai_body,
                headers,
                is_stream=True,
                forced_pool=getattr(request.state, "_geo_forced_pool", None),
            ) as resp:
                headers = dict(resp.headers)
                account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
                if resp.status_code != 200:
                    await _log_and_save_error(
                        req_id,
                        model_id,
                        original_model,
                        start_time,
                        resp.status_code,
                        resp.text,
                        protocol,
                        True,
                        thinking_type,
                        effort,
                        client_ip,
                        account_alias,
                        tool_names,
                        request_body=request_body,
                        response_body={"error": resp.text[:2000]},
                    )

                    async def err_stream():
                        yield b"data: [DONE]\n\n"

                    return StreamingResponse(
                        err_stream(),
                        media_type="text/event-stream",
                        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                    )
                collected_chunks = []
                collected_reasoning_chunks = []
                final_usage = None
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        continue
                    try:
                        chunk = _json_loads(data_str)
                    except Exception:
                        continue
                    if chunk is None:
                        continue
                    chunk_usage = chunk.get("usage")
                    if isinstance(chunk_usage, dict):
                        final_usage = chunk_usage
                    choices = chunk.get("choices", [])
                    if not choices or not isinstance(choices, list):
                        # 可能是 Responses API format — try converting
                        converted = _responses_sse_to_chat_deltas(data_str, parsed=chunk)
                        if converted is None:
                            continue
                        chunk = converted
                        choices = chunk.get("choices", [])
                        if not choices:
                            continue
                    if choices and isinstance(choices, list) and len(choices) > 0:
                        delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
                        if isinstance(delta, dict):
                            c = delta.get("content")
                            if isinstance(c, str) and c:
                                collected_chunks.append(c)
                            rc = delta.get("reasoning_content") or delta.get("reasoning")
                            if isinstance(rc, str) and rc:
                                collected_reasoning_chunks.append(rc)
                full_content = "".join(collected_chunks) or ""
                full_reasoning = "".join(collected_reasoning_chunks) or ""
                chat_resp = {
                    "choices": [
                        {
                            "message": {
                                "content": full_content,
                                "role": "assistant",
                                "reasoning_content": full_reasoning,
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": final_usage or {"prompt_tokens": 0, "completion_tokens": 0},
                }
                oai_resp = openai_chat_to_responses(chat_resp, original_model)
                payload = _json_dumps_str(
                    {"type": "response.completed", "response": oai_resp}, ensure_ascii=False
                )
                sse_body = f"data: {payload}\n\ndata: [DONE]\n\n".encode()
                return Response(
                    content=sse_body,
                    media_type="text/event-stream",
                    headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
                )
        else:
            resp, headers = await _do_request_with_retry(endpoint, oai_body, headers, "openai")
    except FreeQuotaExhausted as e:
        return _free_quota_exhausted_response(e, "openai")
    except UpstreamError as e:
        return JSONResponse(status_code=e.status_code, content={"error": str(e)})
    account_alias = _alias_for_key(_key_from_headers(headers, "openai"))
    if resp.status_code != 200:
        await _log_and_save_error(
            req_id,
            model_id,
            original_model,
            start_time,
            resp.status_code,
            resp.text,
            protocol,
            True,
            thinking_type,
            effort,
            client_ip,
            account_alias,
            tool_names,
            request_body=request_body,
            response_body={"error": resp.text[:2000]},
        )

        async def err_stream():
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            err_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )

    # Collect streaming response and convert to Responses API format
    collected_chunks = []
    collected_reasoning_chunks = []
    final_usage = None
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if data_str == "[DONE]":
            continue
        try:
            chunk = _json_loads(data_str)
        except Exception:
            continue
        if chunk is None:
            continue
        # Collect usage from final chunk
        chunk_usage = chunk.get("usage")
        if isinstance(chunk_usage, dict):
            final_usage = chunk_usage
        # Collect text content from delta
        choices = chunk.get("choices", [])
        if not choices or not isinstance(choices, list):
            # 可能是 Responses API format — try converting
            converted = _responses_sse_to_chat_deltas(data_str, parsed=chunk)
            if converted is None:
                continue
            chunk = converted
            choices = chunk.get("choices", [])
            if not choices:
                continue
        if choices and isinstance(choices, list) and len(choices) > 0:
            delta = choices[0].get("delta", {}) if isinstance(choices[0], dict) else {}
            if isinstance(delta, dict):
                c = delta.get("content")
                if isinstance(c, str) and c:
                    collected_chunks.append(c)
                # Collect reasoning content
                rc = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(rc, str) and rc:
                    collected_reasoning_chunks.append(rc)

    # Build final response
    full_content = "".join(collected_chunks)
    full_reasoning = "".join(collected_reasoning_chunks)
    if not full_content:
        full_content = ""  # Ensure non-None
    if not full_reasoning:
        full_reasoning = ""  # Ensure non-None

    # Create Chat Completions format response for conversion
    chat_resp = {
        "choices": [
            {
                "message": {
                    "content": full_content,
                    "role": "assistant",
                    "reasoning_content": full_reasoning,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": final_usage or {"prompt_tokens": 0, "completion_tokens": 0},
    }

    # Convert to Responses API format
    oai_resp = openai_chat_to_responses(chat_resp, original_model)
    # Return as single SSE block (data-only format)
    payload = _json_dumps_str(
        {"type": "response.completed", "response": oai_resp}, ensure_ascii=False
    )
    sse_body = f"data: {payload}\n\ndata: [DONE]\n\n".encode()
    return Response(
        content=sse_body,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )


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

            try:
                import uvloop  # noqa: F401

                _loop = "uvloop"
            except ImportError:
                _loop = "asyncio"
            config = Config(
                self.app,
                host=self.host,
                port=self.port,
                log_level="info",
                log_config=None,
                timeout_keep_alive=15,
                backlog=4096,
                limit_concurrency=2000,
                loop=_loop,
                http="httptools",
                ws="none",
                timeout_graceful_shutdown=5,
            )
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
                _debug("  [server] stop skipped — not running")
                return
            _debug(f"  [server] stopping on {self.host}:{self.port}...")
            if self._server:
                self._server.should_exit = True
            self.is_running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._server = None
        self._thread = None
        _debug("  [server] stopped")

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
        _log(
            f"WARNING: cannot create lock file {lock_path}: {e} — continuing without mono-instance guard"
        )
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
        print(
            f"FATAL: another opencode-proxy instance is already running (lock held: {lock_path})",
            file=sys.stderr,
            flush=True,
        )
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
    import argparse
    import signal
    import sys
    import traceback as _traceback

    parser = argparse.ArgumentParser(description="OpenCode Proxy")
    parser.add_argument(
        "--no-gui", action="store_true", help="Force terminal mode (no system tray)"
    )
    parser.add_argument("--gui", action="store_true", help="(default, no-op for backward compat)")
    parser.add_argument("--port", type=int, default=None, help=f"API port (default: {PORT})")
    _cli_args = parser.parse_args()
    if _cli_args.port is not None:
        PORT = _cli_args.port

    _acquire_instance_lock(
        # [v10 incident 25/08] chemin ANCRÉ au projet : un chemin relatif
        # dépendait du CWD du lanceur — deux lancements depuis des CWD
        # différents créaient deux locks distincts et deux instances vives.
        os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "logs", f"opencode-{PORT}.lock"
        )
    )

    # GUI by default (system tray + dashboard window); --no-gui forces terminal mode.
    use_gui = not _cli_args.no_gui

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
            print(
                "GUI dependencies not installed (pystray Pillow pywebview). Falling back to terminal mode."
            )
            print(
                "Install with: pip install pystray Pillow pywebview  (or use --no-gui to skip this check)"
            )
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