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


#: [audit 2026-10-30] Seul ``prompt_cache_key`` est un hint de routage et
#: peut être exclu de la clé. ``previous_response_id`` et ``conversation``
#: désignent un historique côté serveur : les exclure servait la réponse
#: d'une AUTRE conversation à un client stateful (fuite inter-sessions).
_CHAINING_KEY_FIELDS = ("prompt_cache_key",)


def _is_error_payload(body: bytes) -> bool:
    """True only for a top-level ``{"error": ...}`` payload.

    A 200 carrying an upstream error was cached for 300 s and replayed as a
    valid answer. Never cache that shape; legitimate responses never have a
    top-level ``error`` key.
    """
    if not body:
        return False
    try:
        data = json.loads(body)
    except Exception:
        return False
    return isinstance(data, dict) and bool(data.get("error"))


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
        max_bytes: int = 128 * 1024 * 1024,
    ):
        self._max_size = max_size
        self._ttl = ttl
        # [audit-2026-10-24 Phase0] borne octets anti-10Go (1000 entrées x
        # max_body 10Mo). LRU par octets : éviction oldest-first jusqu'au cap.
        self._max_bytes = max(1 * 1024 * 1024, int(max_bytes))
        self._total_bytes = 0
        self._store: dict[str, tuple[float, bytes, dict]] = {}  # key -> (ts, body, headers)
        self._access_order: OrderedDict[str, None] = OrderedDict()  # O(1) LRU tracking
        self._debug_fn = debug_fn
        self._dumps_str_fn = dumps_str_fn
        self._hits = 0
        self._misses = 0
        self._skipped_errors = 0

    def _evict(self):
        evicted = 0
        while len(self._store) > self._max_size or self._total_bytes > self._max_bytes:
            oldest, _ = self._access_order.popitem(last=False)  # O(1) pop oldest
            entry = self._store.pop(oldest, None)
            if entry is not None:
                try:
                    self._total_bytes -= len(entry[1])
                except Exception:
                    pass
            evicted += 1
            if not self._store:
                break
        if evicted > 0:
            self._debug_fn(
                f"  [cache] _evict: evicted {evicted} entries, store_size={len(self._store)}/{self._max_size} bytes={self._total_bytes}/{self._max_bytes}"
            )

    def get(self, key: str) -> tuple[bytes, dict] | None:
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None
        ts, body, headers = entry
        if time.monotonic() - ts > self._ttl:
            self._debug_fn(
                f"  [cache] get: TTL expired (age={time.monotonic() - ts:.1f}s > ttl={self._ttl}s), evicting key={key[:16]}..."
            )
            self._store.pop(key, None)
            self._access_order.pop(key, None)
            try:
                self._total_bytes -= len(body)
            except Exception:
                pass
            self._misses += 1
            return None
        # Move to end of access order (most recently used) — O(1)
        self._access_order.move_to_end(key)
        self._hits += 1
        self._debug_fn(f"  [cache] get: HIT key={key[:16]}... size={len(body)} bytes")
        return body, headers

    def put(self, key: str, body: bytes, headers: dict):
        # [audit 2026-10-30] Never cache a 200 that is actually an upstream
        # error (``{"error": ...}``). It was replayed as a successful answer
        # for the whole TTL.
        if _is_error_payload(body):
            self._skipped_errors += 1
            self._debug_fn("  [cache] put: skip error-shaped payload")
            return
        if key in self._store:
            self._access_order.pop(key, None)
            try:
                self._total_bytes -= len(self._store[key][1])
            except Exception:
                pass
        self._store[key] = (time.monotonic(), body, dict(headers))
        try:
            self._total_bytes += len(body)
        except Exception:
            pass
        self._access_order[key] = None  # append to end — O(1)
        self._evict()
        self._debug_fn(
            f"  [cache] put: key={key[:16]}... store_size={len(self._store)}/{self._max_size} bytes={self._total_bytes}/{self._max_bytes}"
        )

    def make_key(
        self,
        body: dict,
        body_bytes: bytes | None = None,
        *,
        namespace: str | None = None,
    ) -> str | None:
        """Create cache key from request body. Returns None if not cacheable.

        If body_bytes is provided, hashes raw bytes directly (fast, no re-serialization).
        Falls back to json.dumps + blake2b if body_bytes is not provided.

        ``namespace`` MUST carry everything outside the body that changes the
        response shape: client endpoint/protocol, resolved model, egress leg,
        auth tenant and protocol-version headers. Without it, the same JSON
        posted to /v1/messages and /v1/chat/completions collided and one
        client received the other protocol's envelope.
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
            h = hashlib.blake2b(digest_size=16)
            if namespace:
                h.update(namespace.encode("utf-8", "replace"))
                h.update(b"\x00")
            if body_bytes and not any(k in body for k in _CHAINING_KEY_FIELDS):
                # Fast path: hash raw bytes directly (avoids json.dumps + sort_keys)
                h.update(body_bytes)
            else:
                scrubbed = {k: v for k, v in body.items() if k not in _CHAINING_KEY_FIELDS}
                # Fallback: deterministic JSON serialization + blake2b
                h.update(self._dumps_str_fn(scrubbed, separators=(",", ":"), default=str).encode())
            key = h.hexdigest()
            self._debug_fn(f"  [cache] make_key: generated hash={key[:16]}...")
            return key
        except Exception:
            return None

    def stats(self) -> dict:
        total = self._hits + self._misses
        return {
            "size": len(self._store),
            "max_size": self._max_size,
            "ttl": self._ttl,
            "bytes": self._total_bytes,
            "max_bytes": self._max_bytes,
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": (self._hits / total) if total else 0.0,
            "skipped_errors": self._skipped_errors,
        }


# Alias historique (opencode._ResponseCache, tests) — PAS une sous-classe :
# isinstance / identité de classe préservés dans les deux sens.
_ResponseCache = ResponseCache

__all__ = ["ResponseCache", "_ResponseCache"]
