"""Qualité mesurée d'une station/IP — brique de sélection.

Le pool historique choisissait par compteur de requêtes. Ce module fournit un
score reproductible par (station, IP) : latence observée, taux de succès,
fraîcheur du quota, charge serveur et stabilité. Il ne décide pas seul : le
sélecteur (`free.selector`) et le pool appliquent les gates d'éligibilité.

Aucune sonde réseau ici : `free.probe` alimente les probes, les requêtes
réelles alimentent `observe()`.
"""

from __future__ import annotations

import math
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from typing import Any

_MAX_ENTRIES = 300
_RING = 30


def _yaml_get(section: str, key: str, default: Any = None) -> Any:
    try:
        from config.settings import yaml_get

        return yaml_get(section, key, default)
    except Exception:
        return default


@dataclass
class QualityConfig:
    enabled: bool = False
    probe_interval_s: float = 30.0
    probe_timeout_s: float = 5.0
    min_samples: int = 5
    exploration_pct: float = 5.0
    latency_weight: float = 0.40
    success_weight: float = 0.20
    quota_weight: float = 0.15
    server_weight: float = 0.15
    stability_weight: float = 0.10
    latency_floor_ms: float = 1000.0
    latency_ceiling_ms: float = 20000.0
    rotate_floor: float = 0.35
    consecutive_low: int = 3

    @classmethod
    def from_yaml(cls) -> QualityConfig:
        raw = _yaml_get("stations", "quality", {}) or {}
        if not isinstance(raw, dict):
            raw = {}

        def f(name: str, default: float) -> float:
            try:
                return max(0.0, float(raw.get(name, default)))
            except (TypeError, ValueError):
                return default

        def i(name: str, default: int) -> int:
            try:
                return max(1, int(raw.get(name, default)))
            except (TypeError, ValueError):
                return default

        return cls(
            enabled=bool(raw.get("enabled", False)),
            probe_interval_s=max(5.0, f("probe_interval_s", 30.0)),
            probe_timeout_s=max(1.0, f("probe_timeout_s", 5.0)),
            min_samples=i("min_samples", 5),
            exploration_pct=max(0.0, min(50.0, f("exploration_pct", 5.0))),
            latency_weight=f("latency_weight", 0.40),
            success_weight=f("success_weight", 0.20),
            quota_weight=f("quota_weight", 0.15),
            server_weight=f("server_weight", 0.15),
            stability_weight=f("stability_weight", 0.10),
            latency_floor_ms=f("latency_floor_ms", 1000.0),
            latency_ceiling_ms=max(
                f("latency_floor_ms", 1000.0) + 1.0,
                f("latency_ceiling_ms", 20000.0),
            ),
            rotate_floor=max(0.0, min(1.0, f("rotate_floor", 0.35))),
            consecutive_low=i("consecutive_low", 3),
        )


@dataclass
class IpQuality:
    sid: int
    ip: str
    ring: deque[float] = field(default_factory=lambda: deque(maxlen=_RING))
    ewma_ms: float | None = None
    success: int = 0
    errors: int = 0
    recent_429: int = 0
    last_429_at: float = 0.0
    last_ok_at: float = 0.0
    last_probe_ms: float | None = None
    last_probe_at: float = 0.0
    last_probe_ok: bool | None = None
    consecutive_low: int = 0

    @property
    def count(self) -> int:
        return self.success + self.errors

    def observe(
        self,
        duration_ms: float,
        status: int | None,
        now: float,
        *,
        latency: bool = True,
    ) -> None:
        """Enregistre une requête. ``latency=False`` pour les réponses
        non-streaming : ``duration_ms`` est alors la durée TOTALE du
        completion, pas un TTFB — l'injecter dans l'EWMA ferait passer une
        réponse longue (mais tunnel rapide) pour une station lente. La
        latence reste pilotée par les TTFB streaming et la probe active.
        """
        duration = max(1.0, float(duration_ms or 1.0))
        if latency:
            self.ring.append(duration)
            alpha = 0.3
            self.ewma_ms = (
                duration
                if self.ewma_ms is None
                else alpha * duration + (1.0 - alpha) * self.ewma_ms
            )
        code = int(status) if status is not None else 0
        if code == 200 or (200 <= code < 300):
            self.success += 1
            self.last_ok_at = now
        else:
            self.errors += 1
            if code == 429:
                self.recent_429 += 1
                self.last_429_at = now

    def observe_probe(self, duration_ms: float, ok: bool, now: float) -> None:
        self.last_probe_ms = max(1.0, float(duration_ms or 1.0))
        self.last_probe_at = now
        self.last_probe_ok = bool(ok)
        if ok:
            self.last_ok_at = now

    def p95_ms(self) -> float | None:
        if len(self.ring) < 2:
            return None
        ordered = sorted(self.ring)
        idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
        return round(ordered[idx], 1)

    def latency_ms(self) -> float | None:
        # La probe est la meilleure source à froid ; l'EWMA des vraies
        # requêtes prend le relais dès qu'elle existe.
        if self.ewma_ms is not None and self.count > 0:
            return self.ewma_ms
        return self.last_probe_ms

    def score(
        self,
        cfg: QualityConfig,
        *,
        quota_used: int,
        quota_limit: int,
        server_load: float | None,
        now: float,
    ) -> float:
        latency = self.latency_ms()
        if latency is None:
            latency_score = 0.5
        else:
            span = max(1.0, cfg.latency_ceiling_ms - cfg.latency_floor_ms)
            latency_score = max(
                0.0, min(1.0, 1.0 - (latency - cfg.latency_floor_ms) / span)
            )

        total = self.success + self.errors
        success_score = 0.5 if total == 0 else self.success / max(1, total)

        try:
            limit = max(1, int(quota_limit or 1))
            used = max(0, int(quota_used or 0))
            quota_score = max(0.0, min(1.0, 1.0 - used / limit))
        except Exception:
            quota_score = 0.5

        if server_load is None:
            server_score = 0.5
        else:
            try:
                server_score = max(0.0, min(1.0, 1.0 - float(server_load) / 100.0))
            except (TypeError, ValueError):
                server_score = 0.5

        # Stabilité : pénalise les 429 récents et les erreurs, sans écraser
        # une IP qui vient de démarrer.
        age_429 = max(0.0, now - self.last_429_at) if self.last_429_at else 1e9
        recent_penalty = 0.0 if age_429 > 600.0 else max(0.0, 1.0 - age_429 / 600.0)
        stability_score = max(
            0.0,
            min(1.0, 1.0 - 0.5 * recent_penalty - (0.25 if self.last_probe_ok is False else 0.0)),
        )

        raw = (
            cfg.latency_weight * latency_score
            + cfg.success_weight * success_score
            + cfg.quota_weight * quota_score
            + cfg.server_weight * server_score
            + cfg.stability_weight * stability_score
        )
        # Confiance : un score inconnu reste neutre au lieu d'être artificiellement
        # excellent ou mauvais.
        confidence = min(1.0, total / max(1, cfg.min_samples))
        if self.last_probe_at:
            confidence = max(confidence, 0.5)
        return round(0.5 * (1.0 - confidence) + raw * confidence, 4)

    def snapshot(self, cfg: QualityConfig, now: float, **kw) -> dict:
        return {
            "sid": self.sid,
            "ip": self.ip,
            "count": self.count,
            "success": self.success,
            "errors": self.errors,
            "recent_429": self.recent_429,
            "ewma_ms": round(self.ewma_ms, 1) if self.ewma_ms is not None else None,
            "p95_ms": self.p95_ms(),
            "probe_ms": round(self.last_probe_ms, 1) if self.last_probe_ms is not None else None,
            "probe_ok": self.last_probe_ok,
            "probe_age_s": (
                round(max(0.0, now - self.last_probe_at), 1) if self.last_probe_at else None
            ),
            "score": self.score(cfg, now=now, **kw),
        }


class QualityRegistry:
    """Registre borné (LRU) des qualités par (station, IP)."""

    def __init__(self, cfg: QualityConfig | None = None) -> None:
        self.cfg = cfg or QualityConfig.from_yaml()
        self._items: OrderedDict[tuple[int, str], IpQuality] = OrderedDict()

    def get(self, sid: int, ip: str) -> IpQuality:
        key = (int(sid), str(ip or "unknown"))
        item = self._items.get(key)
        if item is None:
            item = IpQuality(sid=key[0], ip=key[1])
            self._items[key] = item
            while len(self._items) > _MAX_ENTRIES:
                self._items.popitem(last=False)
        else:
            self._items.move_to_end(key)
        return item

    def observe(
        self,
        sid: int,
        ip: str,
        duration_ms: float,
        status: int | None,
        *,
        latency: bool = True,
    ) -> None:
        self.get(sid, ip).observe(
            duration_ms, status, time.monotonic(), latency=latency
        )

    def observe_probe(self, sid: int, ip: str, duration_ms: float, ok: bool) -> None:
        self.get(sid, ip).observe_probe(duration_ms, ok, time.monotonic())

    def prune_sids(self, max_sid: int) -> int:
        try:
            max_sid = int(max_sid)
        except (TypeError, ValueError):
            return 0
        drop = [k for k in self._items if int(k[0]) > max_sid]
        for k in drop:
            self._items.pop(k, None)
        return len(drop)

    def status(self) -> dict:
        return {
            "enabled": bool(self.cfg.enabled),
            "entries": len(self._items),
            "stations": {
                str(sid): {
                    "ip": ip,
                    **item.snapshot(
                        self.cfg,
                        time.monotonic(),
                        quota_used=0,
                        quota_limit=1,
                        server_load=None,
                    ),
                }
                for (sid, ip), item in list(self._items.items())[-40:]
            },
        }


_REGISTRY: QualityRegistry | None = None


def get_quality_registry() -> QualityRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = QualityRegistry()
    return _REGISTRY


def reset_quality_registry() -> None:
    global _REGISTRY
    _REGISTRY = None


__all__ = [
    "IpQuality",
    "QualityConfig",
    "QualityRegistry",
    "get_quality_registry",
    "reset_quality_registry",
]
