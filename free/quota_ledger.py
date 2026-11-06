"""Journal des buckets de quota free — ``(IP, jour UTC, bucket)``.

Le quota free du provider est un compteur serveur :

* clé primaire ``(ip, interval)`` dans ``ip_rate_limit`` ;
* ``interval = YYYYMMDD`` pour les modèles « default » ;
* ``interval = YYYYMMDD + model_id[:2]`` pour les modèles à ``rateLimit``
  explicite ;
* reset à **00:00 UTC** (``getRetryAfterDay``), pas un timestamp par
  utilisateur.

Ce module est la couche *autoritaire* du bucket journalier. Il ne remplace pas
``free.ip_quota`` (latence, headroom estimé, quarantaine qualité) : il ajoute
la mémoire du bucket serveur, partagée entre redémarrages, avec une règle
conservatrice — tant que le mode ``model_prefix`` n'est pas prouvé, tous les
modèles free partagent le bucket ``default``.

Aucun réseau, aucun import projet : le pool pousse les résultats réels.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_PATH = os.path.join(ROOT, "logs", "quota_ledger.json")
_DAY_S = 86_400.0
_UNKNOWN_IP = "unknown"


def server_ip_key(raw: Any) -> str:
    """Reproduit la clé IP du handler.

    ``handler.ts`` fait exactement :

    ``rawIp.includes(":") ? rawIp.split(":").slice(0, 4).join(":") : rawIp``

    On applique la même transformation, sans canonicaliser l'IPv6 : le but est
    de produire la même clé que le serveur, pas une jolie adresse. Pour une
    valeur ``x-real-ip`` simple (cas Cloudflare), la clé est identique. Les
    valeurs multiples (``X-Forwarded-For``) prennent le premier élément et les
    crochets d'un ``[addr]:port`` sont retirés : normalisation défensive pour
    les IPs internes, sans effet sur le cas réel.
    """
    s = str(raw or "").strip().strip('"').strip("'")
    if "," in s:
        s = s.split(",", 1)[0].strip()
    if s.startswith("[") and "]" in s:
        s = s[1 : s.index("]")]
    if not s:
        return _UNKNOWN_IP
    if ":" in s:
        return ":".join(s.split(":")[:4]) or _UNKNOWN_IP
    return s


def is_logical_proxy_key(raw: Any) -> bool:
    """``socks5:<label>`` est un identifiant de station, pas une IP d'egress.

    Les anciennes versions du pool l'ont utilisé comme clé de ledger, créant
    des buckets ``socks5:warp-*`` qui restaient épuisés après une rotation
    vers une IP fraîche. On purge ces clés au chargement.
    """
    return str(raw or "").strip().startswith("socks5:")


def utc_day(now: float | None = None) -> str:
    """``YYYYMMDD`` UTC, comme ``buildYYYYMMDD`` côté serveur."""
    ts = float(now if now is not None else time.time())
    return time.strftime("%Y%m%d", time.gmtime(ts))


def next_utc_midnight(now: float | None = None) -> float:
    """Timestamp du prochain 00:00 UTC (formule exacte de ``getRetryAfterDay``)."""
    ts = float(now if now is not None else time.time())
    return ts - (ts % _DAY_S) + _DAY_S


def bucket_for(model: str | None, mode: str = "default") -> str:
    """Bucket serveur pour un modèle.

    ``default``      → ``"default"`` (tous les free sans rateLimit explicite) ;
    ``model_prefix`` → ``model_id[:2]`` (modèles à rateLimit explicite).
    """
    mid = str(model or "").strip().lower()
    if mode == "model_prefix" and mid:
        return mid[:2]
    return "default"


@dataclass
class BucketState:
    ip: str
    day: str
    bucket: str
    requests: int = 0
    successes: int = 0
    failures: int = 0
    transient_429: int = 0
    last_status: int = 0
    last_error: str = ""
    retry_after: float = 0.0
    reset_at: float = 0.0
    exhausted: bool = False
    first_seen: float = 0.0
    last_seen: float = 0.0
    source: str = "inferred"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "day": self.day,
            "bucket": self.bucket,
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "transient_429": self.transient_429,
            "last_status": self.last_status,
            "last_error": self.last_error,
            "retry_after": self.retry_after,
            "reset_at": self.reset_at,
            "exhausted": self.exhausted,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> BucketState | None:
        if not isinstance(raw, dict):
            return None
        try:
            ip = server_ip_key(raw.get("ip"))
            day = str(raw.get("day") or "").strip()
            bucket = str(raw.get("bucket") or "default").strip() or "default"
            if not day:
                return None
            return cls(
                ip=ip,
                day=day,
                bucket=bucket,
                requests=max(0, int(raw.get("requests", 0) or 0)),
                successes=max(0, int(raw.get("successes", 0) or 0)),
                failures=max(0, int(raw.get("failures", 0) or 0)),
                transient_429=max(0, int(raw.get("transient_429", 0) or 0)),
                last_status=max(0, int(raw.get("last_status", 0) or 0)),
                last_error=str(raw.get("last_error") or ""),
                retry_after=max(0.0, float(raw.get("retry_after", 0.0) or 0.0)),
                reset_at=max(0.0, float(raw.get("reset_at", 0.0) or 0.0)),
                exhausted=bool(raw.get("exhausted", False)),
                first_seen=max(0.0, float(raw.get("first_seen", 0.0) or 0.0)),
                last_seen=max(0.0, float(raw.get("last_seen", 0.0) or 0.0)),
                source=str(raw.get("source") or "inferred"),
            )
        except (TypeError, ValueError):
            return None


def _is_free_usage_error(error_type: str, body: Any) -> bool:
    et = str(error_type or "").strip().lower()
    if et in (
        "freeusagelimiterror",
        "free_usage_limit",
        "free-usage-limit",
        "daily-ip",
        "trial-exhausted",
    ):
        return True
    if not isinstance(body, str) or not body:
        return False
    low = body.lower()
    compact = low.replace(" ", "").replace("_", "").replace("-", "")
    return "freeusagelimiterror" in compact or "free usage limit" in low


@dataclass
class QuotaLedger:
    """Registre persisté des buckets ``(ip_norm, jour, bucket)``."""

    path: str = field(default=DEFAULT_PATH)
    bucket_mode: str = "default"
    daily_limit: int = 0
    reserve_pct: float = 0.10
    max_age_days: int = 3
    max_entries: int = 8192
    persist: bool = True
    save_interval_s: float = 2.0
    # [2026-11-06] Plafond optionnel du blocage effectif après un 429 quota.
    # Le ``Retry-After`` upstream est la distance au prochain 00:00 UTC, pas
    # la vraie recovery : afree-proxy a mesuré RA=20 h 46 et un 200 sur la
    # même IP 18 minutes plus tard. On garde la valeur brute dans
    # ``retry_after`` pour l'observabilité, et ``reset_at`` peut être plafonné
    # pour que l'IP redevienne candidate et soit re-testée. 0 = pas de
    # plafond (contrat historique : le ledger garde la vérité serveur).
    max_block_s: float = 0.0
    clock: Callable[[], float] = time.time
    _items: dict[tuple[str, str, str], BucketState] = field(default_factory=dict, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _save_lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _last_save: float = field(default=0.0, init=False)
    _dirty: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        self._path = str(self.path or DEFAULT_PATH)
        self._sqlite = self._path.lower().endswith(
            (".db", ".sqlite", ".sqlite3")
        )
        if self._sqlite:
            self._ensure_sqlite()
        # Les tests qui construisent un pool ne doivent pas écrire dans le
        # ledger de production. Un test de persistance passe un chemin
        # explicite (ou persist=True + path=tmp) et garde l'écriture.
        try:
            if os.getenv("PYTEST_CURRENT_TEST") and self._path == DEFAULT_PATH:
                self.persist = False
        except Exception:
            pass
        self._load()

    # ── horloge / clés ─────────────────────────────────────────────────────

    def _now(self, now: float | None = None) -> float:
        try:
            return float(now) if now is not None else float(self.clock())
        except (TypeError, ValueError):
            return float(time.time())

    def _key(self, ip: Any, model: str | None, now: float | None = None) -> tuple[str, str, str]:
        ts = self._now(now)
        return (server_ip_key(ip), utc_day(ts), bucket_for(model, self.bucket_mode))

    def _get(self, key: tuple[str, str, str], now: float, *, create: bool = True) -> BucketState | None:
        state = self._items.get(key)
        if state is None and create:
            state = BucketState(
                ip=key[0],
                day=key[1],
                bucket=key[2],
                first_seen=now,
                last_seen=now,
            )
            self._items[key] = state
            self._enforce_cap_locked()
        if state is not None:
            self._refresh(state, now)
        return state

    def _refresh(self, state: BucketState, now: float) -> None:
        if not state.exhausted:
            return
        if state.reset_at <= 0:
            state.reset_at = next_utc_midnight(now)
        if now >= state.reset_at:
            state.exhausted = False
            state.retry_after = 0.0
            state.reset_at = 0.0
            state.source = "expired"

    def _enforce_cap_locked(self) -> None:
        try:
            cap = max(1, int(self.max_entries))
        except (TypeError, ValueError):
            cap = 8192
        while len(self._items) > cap:
            oldest = min(self._items, key=lambda k: float(self._items[k].last_seen or 0.0))
            self._items.pop(oldest, None)

    # ── mutations ──────────────────────────────────────────────────────────

    def note_request(self, ip: Any, model: str | None, now: float | None = None) -> None:
        """Compte une tentative d'envoi (le serveur ``track()`` après réponse)."""
        ts = self._now(now)
        with self._lock:
            state = self._get(self._key(ip, model, ts), ts)
            if state is None:
                return
            state.requests += 1
            state.last_seen = ts
            self._dirty = True
        self._maybe_save()

    def note_result(
        self,
        ip: Any,
        model: str | None,
        status: int | None,
        *,
        error_type: str = "",
        retry_after: float | None = None,
        body: Any = None,
        now: float | None = None,
    ) -> None:
        """Enregistre un résultat réel (200, 429, autre)."""
        ts = self._now(now)
        code = int(status or 0)
        with self._lock:
            state = self._get(self._key(ip, model, ts), ts)
            if state is None:
                return
            state.last_status = code
            state.last_seen = ts
            if 200 <= code < 300:
                state.successes += 1
                # Un 200 observé est plus fort qu'un 429 antérieur : le bucket
                # est utilisable, on lève l'exhaustion.
                state.exhausted = False
                state.retry_after = 0.0
                state.reset_at = 0.0
                state.source = "observed-ok"
            elif code == 429:
                state.failures += 1
                if _is_free_usage_error(error_type, body):
                    state.exhausted = True
                    state.last_error = "FreeUsageLimitError"
                    state.source = "observed"
                    try:
                        ra = float(retry_after or 0.0)
                    except (TypeError, ValueError):
                        ra = 0.0
                    state.retry_after = max(0.0, ra)
                    if ra > 0:
                        # Le provider peut avoir un reset PAR IP (fenêtre
                        # glissante / jitter observés 00:01→00:53 UTC). Le
                        # Retry-After reste la source brute, mais il vaut
                        # « jusqu'à 00:00 UTC » : on plafonne le blocage
                        # effectif (cf. ``max_block_s``) pour re-tester l'IP
                        # au lieu de la parquer ~19 h sur une valeur qui n'est
                        # pas la vraie recovery.
                        try:
                            _cap = float(self.max_block_s or 0.0)
                        except (TypeError, ValueError):
                            _cap = 0.0
                        _effective = min(ra, _cap) if _cap > 0 else ra
                        state.reset_at = ts + _effective
                    else:
                        state.reset_at = next_utc_midnight(ts)
                else:
                    state.transient_429 += 1
                    state.last_error = str(error_type or "429")
            else:
                state.failures += 1
                state.last_error = str(error_type or code or "error")
            self._dirty = True
            _force = bool(state.exhausted)
        self._maybe_save(force=_force)

    def mark_exhausted(
        self,
        ip: Any,
        model: str | None,
        *,
        retry_after: float | None = None,
        error_type: str = "FreeUsageLimitError",
        now: float | None = None,
    ) -> None:
        self.note_result(
            ip,
            model,
            429,
            error_type=error_type,
            retry_after=retry_after,
            now=now,
        )

    # ── lectures ───────────────────────────────────────────────────────────

    def is_exhausted(self, ip: Any, model: str | None, now: float | None = None) -> bool:
        ts = self._now(now)
        with self._lock:
            state = self._get(self._key(ip, model, ts), ts, create=False)
            return bool(state and state.exhausted)

    def usable(self, ip: Any, model: str | None, now: float | None = None) -> tuple[bool, str | None]:
        ts = self._now(now)
        with self._lock:
            state = self._get(self._key(ip, model, ts), ts, create=False)
            if state is None:
                return True, None
            if state.exhausted:
                return False, "daily-bucket-exhausted"
            limit = max(0, int(self.daily_limit or 0))
            if limit > 0:
                reserve = max(1, int(round(limit * max(0.0, min(0.9, self.reserve_pct)))))
                if state.requests >= max(1, limit - reserve):
                    return False, "daily-bucket-reserve"
            return True, None

    def headroom(self, ip: Any, model: str | None, now: float | None = None) -> int:
        ts = self._now(now)
        with self._lock:
            state = self._get(self._key(ip, model, ts), ts, create=False)
            if state is None:
                return max(0, int(self.daily_limit or 0)) or 10**9
            if state.exhausted:
                return 0
            limit = max(0, int(self.daily_limit or 0))
            return max(0, limit - state.requests) if limit > 0 else 10**9

    def reset_at(self, ip: Any, model: str | None, now: float | None = None) -> float | None:
        ts = self._now(now)
        with self._lock:
            state = self._get(self._key(ip, model, ts), ts, create=False)
            if state is None or not state.exhausted or state.reset_at <= 0:
                return None
            return float(state.reset_at)

    def earliest_reset(self, now: float | None = None, *, bucket: str | None = None) -> float | None:
        ts = self._now(now)
        with self._lock:
            resets = []
            for state in self._items.values():
                self._refresh(state, ts)
                if bucket is not None and state.bucket != bucket:
                    continue
                if state.exhausted and state.reset_at > 0:
                    resets.append(float(state.reset_at))
            return min(resets) if resets else None

    def snapshot(self, limit: int = 80, now: float | None = None) -> list[dict[str, Any]]:
        ts = self._now(now)
        with self._lock:
            for state in self._items.values():
                self._refresh(state, ts)
            items = sorted(self._items.values(), key=lambda s: float(s.last_seen or 0.0), reverse=True)
            out = []
            for state in items[: max(1, int(limit))]:
                row = state.to_dict()
                row["retry_in_s"] = round(max(0.0, state.reset_at - ts), 1) if state.exhausted else 0.0
                row["reset_at_iso"] = (
                    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(state.reset_at))
                    if state.reset_at > 0
                    else None
                )
                out.append(row)
            return out

    # ── persistance ────────────────────────────────────────────────────────

    def _ensure_sqlite(self) -> None:
        try:
            from rotation.infrastructure.db.migrate import (
                apply_schema,
                connect,
            )

            conn = connect(self._path)
            try:
                apply_schema(conn)
            finally:
                conn.close()
        except Exception:
            pass

    def _load_sqlite(self) -> None:
        try:
            conn = sqlite3.connect(self._path, timeout=10.0)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT ip, day, bucket, used, successes, failures, "
                    "last_status, last_error, reset_at, source, "
                    "first_seen_at, last_seen_at FROM quota_buckets"
                ).fetchall()
            finally:
                conn.close()
        except Exception:
            return
        now = self._now(None)
        for row in rows:
            ip_key = server_ip_key(row["ip"])
            if is_logical_proxy_key(ip_key):
                continue
            status = int(row["last_status"] or 0)
            reset_at = float(row["reset_at"] or 0.0)
            state = BucketState(
                ip=ip_key,
                day=str(row["day"]),
                bucket=str(row["bucket"] or "default"),
                requests=max(0, int(row["used"] or 0)),
                successes=max(0, int(row["successes"] or 0)),
                failures=max(0, int(row["failures"] or 0)),
                last_status=max(0, status),
                last_error=str(row["last_error"] or ""),
                reset_at=max(0.0, reset_at),
                exhausted=status == 429 and reset_at > now,
                first_seen=max(0.0, float(row["first_seen_at"] or 0.0)),
                last_seen=max(0.0, float(row["last_seen_at"] or 0.0)),
                source=str(row["source"] or "observed"),
            )
            self._items[(state.ip, state.day, state.bucket)] = state
        self._enforce_cap_locked()
        self._prune_locked(now)
        # Purge durable des buckets hérités ``socks5:*`` (identifiants de
        # station utilisés à tort comme IP par les anciennes versions).
        try:
            conn = sqlite3.connect(self._path, timeout=10.0)
            try:
                conn.execute("DELETE FROM quota_buckets WHERE ip LIKE 'socks5:%'")
                conn.commit()
            finally:
                conn.close()
        except Exception:
            pass

    def _load(self) -> None:
        if not self.persist:
            return
        if self._sqlite:
            self._load_sqlite()
            return
        try:
            with open(self._path, encoding="utf-8") as f:
                raw = json.load(f) or {}
            items = raw.get("items") if isinstance(raw, dict) else raw
            if not isinstance(items, list):
                return
            for rec in items:
                state = BucketState.from_dict(rec)
                if state is None or is_logical_proxy_key(state.ip):
                    continue
                self._items[(state.ip, state.day, state.bucket)] = state
            self._enforce_cap_locked()
            self._prune_locked(self._now(None))
        except FileNotFoundError:
            return
        except Exception:
            return

    def _prune_locked(self, now: float) -> None:
        try:
            days = max(1, int(self.max_age_days))
        except (TypeError, ValueError):
            days = 3
        cutoff = utc_day(now - days * _DAY_S)
        for key in list(self._items):
            if key[1] < cutoff:
                self._items.pop(key, None)
        self._enforce_cap_locked()

    def prune(self, now: float | None = None) -> int:
        ts = self._now(now)
        with self._lock:
            before = len(self._items)
            self._prune_locked(ts)
            return before - len(self._items)

    def _save_sqlite(self) -> None:
        with self._lock:
            states = list(self._items.values())
            saved_at = self._now(None)
        try:
            conn = sqlite3.connect(self._path, timeout=10.0)
            try:
                for state in states:
                    conn.execute(
                        "INSERT OR IGNORE INTO egress_ips("
                        "ip, first_seen_at, last_seen_at, state"
                        ") VALUES(?,?,?,'fresh')",
                        (
                            state.ip,
                            state.first_seen or saved_at,
                            state.last_seen or saved_at,
                        ),
                    )
                    conn.execute(
                        "INSERT INTO quota_buckets("
                        "ip, model, day, bucket, used, successes, failures, "
                        "last_status, last_error, reset_at, source, "
                        "first_seen_at, last_seen_at"
                        ") VALUES(?,'default',?,?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(ip, model, day, bucket) DO UPDATE SET "
                        "used=excluded.used, successes=excluded.successes, "
                        "failures=excluded.failures, "
                        "last_status=excluded.last_status, "
                        "last_error=excluded.last_error, "
                        "reset_at=excluded.reset_at, source=excluded.source, "
                        "last_seen_at=excluded.last_seen_at",
                        (
                            state.ip,
                            state.day,
                            state.bucket,
                            int(state.requests),
                            int(state.successes),
                            int(state.failures),
                            int(state.last_status),
                            state.last_error,
                            state.reset_at or None,
                            state.source,
                            state.first_seen or saved_at,
                            state.last_seen or saved_at,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()
            self._last_save = saved_at
            self._dirty = False
        except Exception:
            return

    def _save(self) -> None:
        if not self.persist:
            return
        if self._sqlite:
            self._save_sqlite()
            return
        with self._lock:
            payload = {
                "version": 1,
                "saved_at": self._now(None),
                "items": [state.to_dict() for state in self._items.values()],
            }
        try:
            with self._save_lock:
                os.makedirs(os.path.dirname(self._path), exist_ok=True)
                tmp = self._path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
                os.replace(tmp, self._path)
            self._last_save = payload["saved_at"]
            self._dirty = False
        except Exception:
            return

    def _maybe_save(self, *, force: bool = False) -> None:
        if not self.persist:
            return
        now = self._now(None)
        try:
            interval = max(0.0, float(self.save_interval_s))
        except (TypeError, ValueError):
            interval = 2.0
        if not force and (now - self._last_save) < interval:
            return
        self._save()

    def flush(self) -> None:
        self._save()

    def export_state(self) -> list[dict[str, Any]]:
        with self._lock:
            return [state.to_dict() for state in self._items.values()]

    def restore_state(self, data: Any) -> None:
        if not isinstance(data, list):
            return
        with self._lock:
            for rec in data:
                state = BucketState.from_dict(rec)
                if state is None or is_logical_proxy_key(state.ip):
                    continue
                self._items[(state.ip, state.day, state.bucket)] = state
            self._enforce_cap_locked()

    def clear(self, ip: Any = None, model: str | None = None) -> int:
        """Efface des buckets (tous, une IP, ou un bucket modèle).

        Utile après une correction de corps : des 429 « faux quota » ont pu
        marquer des IPs saines. Le prochain passage les re-teste.
        """
        with self._lock:
            if ip is None and model is None:
                count = len(self._items)
                self._items.clear()
            else:
                key_ip = server_ip_key(ip) if ip is not None else None
                key_bucket = bucket_for(model, self.bucket_mode) if model is not None else None
                keys = [
                    key
                    for key in self._items
                    if (key_ip is None or key[0] == key_ip)
                    and (key_bucket is None or key[2] == key_bucket)
                ]
                count = len(keys)
                for key in keys:
                    self._items.pop(key, None)
            self._dirty = True
        self._maybe_save(force=True)
        return count


__all__ = [
    "BucketState",
    "QuotaLedger",
    "bucket_for",
    "next_utc_midnight",
    "server_ip_key",
    "utc_day",
]
