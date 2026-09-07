"""core.keys — pauser + sélection des clés API (Phase 5 refonte).

Déplacement PUR depuis ``opencode.py`` (§ « API key routing » + « Key pause
tracker »). Voir ``core/__init__.py`` pour le contrat DI (aucun import
projet ; état mutable possédé par l'hôte).
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import threading
import time
from collections.abc import Callable

import yaml

DEFAULT_MAX_PAUSE = 600.0  # = config.yaml key_pause.max_pause
DEFAULT_PREFIX_CACHE_MAX = 4096


def _noop_debug(*args, **kwargs) -> None:
    return None


def _default_alias(api_key: str) -> str:
    return ""


class AllKeysPausedError(Exception):
    """Raised when all API keys are paused and no request can be made."""

    def __init__(self, retry_after: float):
        super().__init__(f"All API keys paused, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class KeyPauser:
    """Per-key rate limit pause tracker. Pauses a key when upstream returns 429.

    Persists pause state to logs/paused_keys.yaml so pauses survive reboots.

    ``prefix_cache`` : mémo partagé {clé → slot} (possédé par l'hôte) ;
    ``alias_fn`` : alias lisible pour les logs (hôte : ``_alias_for_key``).
    """

    _PAUSED_FILE = ""  # surchargé par la sous-classe hôte (chemin ancré projet)

    def __init__(
        self,
        max_pause: float | None = None,
        *,
        prefix_cache: dict[str, str] | None = None,
        prefix_cache_max: int = DEFAULT_PREFIX_CACHE_MAX,
        alias_fn: Callable[[str], str] | None = None,
        debug_fn: Callable[..., None] = _noop_debug,
        log_fn: Callable[..., None] = _noop_debug,
    ):
        self._max_pause = float(max_pause) if max_pause is not None else DEFAULT_MAX_PAUSE
        self._paused: dict[str, float] = {}  # key_prefix -> monotonic expiry
        self._reasons: dict[str, str] = {}  # key_prefix -> reason string
        self._lock = threading.Lock()
        self._prefix_cache = prefix_cache if prefix_cache is not None else {}
        self._prefix_cache_max = prefix_cache_max
        self._alias_fn = alias_fn or _default_alias
        self._debug_fn = debug_fn
        self._log_fn = log_fn

    def _prefix(self, api_key: str) -> str:
        """Slot stable par clé ENTIÈRE.

        [plan v10 Lot 0 filet — bug réel] l'ancien `api_key[:12]` fusionnait
        toutes les clés partageant le préfixe fournisseur (« sk-ant-api03 » =
        exactement 12 caractères) : mettre en pause UNE clé mettait en pause
        TOUTES les clés Anthropic, et la sémantique « seulement étendre »
        collait la plus longue pause à tout le monde. Hash tronqué = slot
        unique par clé, toujours non réversible pour les logs. Les entrées
        persistées sous l'ancien schéma deviennent orphelines et expirent
        naturellement (jamais re-matchées)."""
        cached = self._prefix_cache.get(api_key)
        if cached is not None:
            return cached
        if not api_key:
            return ""
        prefix = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:12]
        if len(self._prefix_cache) < self._prefix_cache_max:
            self._prefix_cache[api_key] = prefix
        return prefix

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
            self._debug_fn(f"  [keypauser] save error: {e}")

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
                self._debug_fn(f"  [keypauser] loaded {loaded} persisted pauses from disk")
                self._log_fn(f"  KEY PAUSER: restored {loaded} pauses from disk")
        except Exception as e:
            self._debug_fn(f"  [keypauser] load error: {e}")

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
        alias = self._alias_fn(api_key)
        self._debug_fn(
            f"  [keypauser] PAUSED alias={alias} prefix={prefix} for {duration:.0f}s reason={reason}"
        )
        self._log_fn(f"  KEY PAUSED: alias={alias} for {duration:.0f}s ({reason})")

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
            self._debug_fn(f"  [keypauser] cleanup: {len(expired)} expired pauses removed")

    def unpause_if_paused(self, api_key: str) -> bool:
        """Remove a pause for a key if it exists. Returns True if removed."""
        prefix = self._prefix(api_key)
        with self._lock:
            if prefix in self._paused:
                del self._paused[prefix]
                self._reasons.pop(prefix, None)
                self._save()
                alias = self._alias_fn(api_key)
                self._debug_fn(f"  [keypauser] UNPAUSED alias={alias} prefix={prefix} (recovered)")
                self._log_fn(f"  KEY UNPAUSED: alias={alias} (recovered)")
                return True
        return False


# Alias historique (opencode._KeyPauser — la sous-classe hôte porte le même
# nom ; cet alias désigne la base pure).
_KeyPauser = KeyPauser


def enabled_keys(api_keys: list) -> list:
    """Clés activées (filtre ``enabled``, défaut True)."""
    return [k for k in api_keys if k.get("enabled", True)]


def env_key_or_raise(env_key: str, pauser: KeyPauser, debug_fn=_noop_debug) -> dict:
    """Return the .env fallback key, or raise AllKeysPausedError if paused.

    [CRITIC(9)] The .env fallback must not be hammered while paused: when
    every routed key is paused AND the .env key itself is paused, raise so
    the caller surfaces a clean retry-after instead of sending a request
    with a known-dead key.
    """
    if pauser.is_paused(env_key):
        remaining = pauser.remaining(env_key)
        debug_fn(f"  [apikey] .env fallback key paused ({remaining:.0f}s) — raising AllKeysPausedError")
        raise AllKeysPausedError(remaining if remaining > 0 else 1)
    return {"api_key": env_key}


def find_alternative_key(
    api_keys: list, pauser: KeyPauser, failed_key: str, debug_fn=_noop_debug
) -> dict | None:
    """Return the first enabled, non-paused key different from failed_key, or None."""
    for k in api_keys:
        if k.get("api_key") != failed_key and k.get("enabled", True):
            if not pauser.is_paused(k.get("api_key", "")):
                debug_fn(f"  [apikey] alternative key found alias={k.get('alias', '?')}")
                return k
    debug_fn(f"  [apikey] no alternative key for {failed_key[:8]}...")
    return None


def has_usable_paid_key(api_keys: list, env_key: str, pauser: KeyPauser) -> bool:
    """True iff ≥1 enabled, non-paused paid key exists (API_KEYS or .env).

    [Étape 2A — A1/A2] Garde no-valid-keys : toute jambe paid est condamnée
    au 401/403 sans clé utilisable. Chemin de chargement (config/settings.py
    load_api_keys) : api_keys.json → YAML → single-key .env → []. Le .env
    vide (API_KEY == "") ne compte PAS comme clé utilisable — un Bearer vide
    produit un 401 upstream systématique.
    """
    for k in api_keys:
        if not k.get("enabled", True):
            continue
        ak = k.get("api_key", "")
        if ak and not pauser.is_paused(ak):
            return True
    env_key = env_key or ""
    if env_key and not pauser.is_paused(env_key):
        return True
    return False


def build_alias_cache(api_keys: list) -> dict[str, str]:
    """Construit le lookup {api_key → alias} (l'hôte l'assigne + vide le
    mémo de préfixes, comme ``_rebuild_key_cache`` historique)."""
    return {k["api_key"]: k.get("alias", "") or "" for k in api_keys if k.get("api_key")}


def select_next_key(
    *,
    api_keys: list,
    env_key: str,
    routing: str,
    pauser: KeyPauser,
    failover_index: int,
    cycle_keys: list,
    cycle_index: int,
    cycle_lock,
    debug_fn=_noop_debug,
) -> tuple[dict, int, list, int]:
    """Sélectionne la prochaine clé API (failover sticky ou round-robin).

    Port exact de ``opencode.get_next_api_key`` : l'état mutable hôte est
    pris en paramètres et RETOURNÉ mis à jour
    ``(clé, failover_index, cycle_keys, cycle_index)`` — le wrapper hôte le
    réassigne à ses globaux (lus À L'APPEL, rebind par tests).

    F-H3: threading.Lock kept (sync+async mix avoids asyncio.Lock deadlock
    from sync thread). Pas de shortcut len(available)==1 [plan Lot 2/3] :
    la boucle failover gère uniformément tous les cas ET applique la
    sémantique sticky (avance sur pause).
    """
    if not api_keys:
        debug_fn("  [apikey] no API_KEYS configured, falling back to .env key")
        return env_key_or_raise(env_key, pauser, debug_fn), failover_index, cycle_keys, cycle_index
    enabled = enabled_keys(api_keys)
    if not enabled:
        debug_fn("  [apikey] no enabled keys, falling back to .env key")
        return env_key_or_raise(env_key, pauser, debug_fn), failover_index, cycle_keys, cycle_index

    # Filter out paused keys
    available = [k for k in enabled if not pauser.is_paused(k.get("api_key", ""))]

    if not available:
        # All paused — raise with shortest wait time instead of reusing a paused key
        min_rem = min((pauser.remaining(k.get("api_key", "")) for k in enabled), default=0)
        if min_rem > 0:
            debug_fn(f"  [apikey] ALL keys paused, min remaining={min_rem:.0f}s — raising AllKeysPausedError")
            raise AllKeysPausedError(min_rem)
        debug_fn("  [apikey] no keys available, falling back to .env key")
        return env_key_or_raise(env_key, pauser, debug_fn), failover_index, cycle_keys, cycle_index

    if routing == "failover":
        for i in range(len(api_keys)):
            idx = (failover_index + i) % len(api_keys)
            if api_keys[idx].get("enabled", True) and not pauser.is_paused(
                api_keys[idx].get("api_key", "")
            ):
                # [plan Lot 2] sticky + avance sur pause : quand la clé
                # d'index courant est pausée/sautée (i > 0), persister la
                # nouvelle position — pas de retour automatique à la clé 0
                # après récupération. Le failover intra-requête reste géré
                # par _do_request_with_retry (_find_alternative_key).
                if i > 0:
                    failover_index = idx
                    debug_fn(f"  [apikey] failover index avance → {idx} (clé précédente pausée)")
                debug_fn(
                    f"  [apikey] failover selected alias={api_keys[idx].get('alias', '?')} (idx={idx})"
                )
                return api_keys[idx], failover_index, cycle_keys, cycle_index
        # Fallback to shortest-paused
        min_rem = min((pauser.remaining(k.get("api_key", "")) for k in enabled), default=0)
        if min_rem > 0:
            raise AllKeysPausedError(min_rem)
        debug_fn("  [apikey] failover exhausted, falling back to .env key")
        return env_key_or_raise(env_key, pauser, debug_fn), failover_index, cycle_keys, cycle_index

    # Round-robin: atomic index modulo under threading.Lock (no itertools.cycle)
    with cycle_lock:
        current_ids = [k.get("api_key") for k in available]
        if cycle_keys != current_ids:
            cycle_keys = [str(k) for k in current_ids]
            cycle_index = 0
            debug_fn(
                f"  [apikey] round-robin index reset: {len(available)} available keys (filtered from {len(enabled)} enabled)"
            )
        idx = cycle_index % len(available) if available else 0
        selected = available[idx]
        cycle_index = (idx + 1) % len(available) if available else 0
        debug_fn(f"  [apikey] round-robin selected alias={selected.get('alias', '?')} idx={idx}")
        return selected, failover_index, cycle_keys, cycle_index


__all__ = [
    "DEFAULT_MAX_PAUSE",
    "DEFAULT_PREFIX_CACHE_MAX",
    "AllKeysPausedError",
    "KeyPauser",
    "build_alias_cache",
    "enabled_keys",
    "env_key_or_raise",
    "find_alternative_key",
    "has_usable_paid_key",
    "select_next_key",
    "_KeyPauser",
]
