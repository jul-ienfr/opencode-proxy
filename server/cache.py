"""server.cache — cache LRU des réponses non-streaming (Phase 2 refonte).

Déplacement PUR depuis ``opencode.py`` (``_ResponseCache`` + ``_response_cache``,
§ « Response Cache (non-streaming only) »). AUCUN import du projet : ``debug_fn``
et ``dumps_str_fn`` sont injectés (l'hôte passe ``dashboard.display.debug`` et
le fast-path orjson).
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from collections.abc import Callable


def _noop_debug(*args, **kwargs) -> None:
    return None


def _default_dumps_str(obj, **kw) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


class ResponseCache:
    """LRU cache for non-streaming API responses with TTL and size limit.

    Cache key: blake2b hash of raw request body bytes (excluding streaming and tool_use).
    Returns (body_bytes, headers_dict) or None on miss.
    Uses OrderedDict for O(1) LRU operations instead of list-based O(n).
    """

    def __init__(
        self,
        max_size: int = 1000,
        ttl: float = 300.0,
        *,
        debug_fn: Callable[..., None] = _noop_debug,
        dumps_str_fn: Callable[..., str] = _default_dumps_str,
    ):
        self._max_size = max_size
        self._ttl = ttl
        self._store: dict[str, tuple[float, bytes, dict]] = {}  # key -> (ts, body, headers)
        self._access_order: OrderedDict[str, None] = OrderedDict()  # O(1) LRU tracking
        self._debug_fn = debug_fn
        self._dumps_str_fn = dumps_str_fn

    def _evict(self):
        evicted = 0
        while len(self._store) > self._max_size:
            oldest, _ = self._access_order.popitem(last=False)  # O(1) pop oldest
            self._store.pop(oldest, None)
            evicted += 1
        if evicted > 0:
            self._debug_fn(
                f"  [cache] _evict: evicted {evicted} entries, store_size={len(self._store)}/{self._max_size}"
            )

    def get(self, key: str) -> tuple[bytes, dict] | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, body, headers = entry
        if time.monotonic() - ts > self._ttl:
            self._debug_fn(
                f"  [cache] get: TTL expired (age={time.monotonic() - ts:.1f}s > ttl={self._ttl}s), evicting key={key[:16]}..."
            )
            self._store.pop(key, None)
            self._access_order.pop(key, None)
            return None
        # Move to end of access order (most recently used) — O(1)
        self._access_order.move_to_end(key)
        self._debug_fn(f"  [cache] get: HIT key={key[:16]}... size={len(body)} bytes")
        return body, headers

    def put(self, key: str, body: bytes, headers: dict):
        if key in self._store:
            self._access_order.pop(key, None)
        self._store[key] = (time.monotonic(), body, dict(headers))
        self._access_order[key] = None  # append to end — O(1)
        self._evict()
        self._debug_fn(f"  [cache] put: key={key[:16]}... store_size={len(self._store)}/{self._max_size}")

    def make_key(self, body: dict, body_bytes: bytes | None = None) -> str | None:
        """Create cache key from request body. Returns None if not cacheable.

        If body_bytes is provided, hashes raw bytes directly (fast, no re-serialization).
        Falls back to json.dumps + blake2b if body_bytes is not provided.
        """
        if body.get("stream"):
            self._debug_fn("  [cache] make_key: stream=True, returning None")
            return None
        # Don't cache requests with tool use (non-deterministic)
        messages = body.get("messages", [])
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        self._debug_fn("  [cache] make_key: tool_result found, returning None")
                        return None
        try:
            if body_bytes:
                # Fast path: hash raw bytes directly (avoids json.dumps + sort_keys)
                key = hashlib.blake2b(body_bytes, digest_size=16).hexdigest()
            else:
                # Fallback: deterministic JSON serialization + blake2b
                key = hashlib.blake2b(
                    self._dumps_str_fn(body, separators=(",", ":"), default=str).encode(),
                    digest_size=16,
                ).hexdigest()
            self._debug_fn(f"  [cache] make_key: generated hash={key[:16]}...")
            return key
        except Exception:
            return None

    def stats(self) -> dict:
        return {"size": len(self._store), "max_size": self._max_size, "ttl": self._ttl}


# Alias historique (opencode._ResponseCache, tests) — PAS une sous-classe :
# isinstance / identité de classe préservés dans les deux sens.
_ResponseCache = ResponseCache

__all__ = ["ResponseCache", "_ResponseCache"]
