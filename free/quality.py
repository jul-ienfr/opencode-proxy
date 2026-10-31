"""Qualité mesurée d'une station/IP — brique de sélection.

Le pool historique choisissait par compteur de requêtes. Ce module fournit un
score reproductible par (station, IP) : latence observée, **débit de génération**
(tok/s), taux de succès, fraîcheur du quota, charge serveur et stabilité. Il ne
décide pas seul : le sélecteur (`free.selector`) et le pool appliquent les gates
d'éligibilité.

Aucune sonde réseau ici : `free.probe` alimente les probes, les requêtes réelles
alimentent `observe()` (TTFB/statut) et `observe_completion()` (durée totale,
tokens de sortie, tok/s, quarantaine slow lane).
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
    latency_weight: float = 0.30
    throughput_weight: float = 0.30
    success_weight: float = 0.15
    quota_weight: float = 0.10
    server_weight: float = 0.05
    stability_weight: float = 0.10
    latency_floor_ms: float = 1000.0
    latency_ceiling_ms: float = 20000.0
    # [P1 29/10] Un TTFB EWMA au-delà de ce seuil quarantaine l'IP même si
    # elle génère vite ensuite (ex. 15,9 s observé sur s6).
    ttfb_ceiling_ms: float = 8000.0
    # [P1 29/10] Least-in-flight : pénalise une station déjà chargée pour
    # répartir avant de saturer ses 8 sessions (s6 à 119 tok/s attirait tout).
    inflight_penalty: float = 0.08
    inflight_penalty_max: float = 0.60
    throughput_floor_tps: float = 20.0
    throughput_target_tps: float = 45.0
    slow_lane_min_samples: int = 3
    slow_lane_cooldown_s: float = 600.0
    slow_lane_score: float = 0.0
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
            latency_weight=f("latency_weight", 0.30),
            throughput_weight=f("throughput_weight", 0.30),
            success_weight=f("success_weight", 0.15),
            quota_weight=f("quota_weight", 0.10),
            server_weight=f("server_weight", 0.05),
            stability_weight=f("stability_weight", 0.10),
            latency_floor_ms=f("latency_floor_ms", 1000.0),
            latency_ceiling_ms=max(
                f("latency_floor_ms", 1000.0) + 1.0,
                f("latency_ceiling_ms", 20000.0),
            ),
            ttfb_ceiling_ms=max(
                f("latency_floor_ms", 1000.0) + 1.0,
                f("ttfb_ceiling_ms", 8000.0),
            ),
            inflight_penalty=f("inflight_penalty", 0.08),
            inflight_penalty_max=f("inflight_penalty_max", 0.60),
            throughput_floor_tps=max(1.0, f("throughput_floor_tps", 20.0)),
            throughput_target_tps=max(
                f("throughput_floor_tps", 20.0) + 1.0,
                f("throughput_target_tps", 45.0),
            ),
            slow_lane_min_samples=i("slow_lane_min_samples", 3),
            slow_lane_cooldown_s=max(0.0, f("slow_lane_cooldown_s", 600.0)),
            slow_lane_score=max(0.0, min(1.0, f("slow_lane_score", 0.0))),
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
    # Débit de génération mesuré en fin de flux (tok/s) : c'est la métrique
    # qui distingue une IP rapide au premier octet d'une IP qui génère vite.
    tps_ewma: float | None = None
    tps_n: int = 0
    tps_ring: deque[float] = field(default_factory=lambda: deque(maxlen=5))
    ttfb_n: int = 0
    total_ewma_ms: float | None = None
    last_stop_reason: str | None = None
    slow_lane_until: float = 0.0
    # Hostname du serveur VPN (ex. nl1050.nordvpn.com) : l'IP publique mesurée
    # n'est PAS l'IP d'entrée de l'API NordVPN ; le hostname est la clé qui
    # relie une mesure à un serveur sélectionnable.
    server_host: str | None = None

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
            self.ttfb_n += 1
            self.ring.append(duration)
            alpha = 0.3
            self.ewma_ms = duration if self.ewma_ms is None else alpha * duration + (1.0 - alpha) * self.ewma_ms
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

    def observe_completion(
        self,
        duration_ms: float,
        output_tokens: int,
        now: float,
        cfg: QualityConfig,
        *,
        success: bool = True,
        stop_reason: str | None = None,
        server_host: str | None = None,
    ) -> None:
        """Enregistre la fin de flux : durée totale, tokens, tok/s.

        Séparé de ``observe()`` : le TTFB est connu au premier octet, le débit
        seulement à la fin. Un flux tronqué (``success=False``) reste mesuré
        en tok/s : c'est précisément le signal qui révèle une IP à 1 tok/s.
        ``server_host`` relie la mesure au serveur VPN sélectionnable.
        """
        if server_host:
            self.server_host = str(server_host)
        duration = max(1.0, float(duration_ms or 1.0))
        alpha = 0.3
        self.total_ewma_ms = duration if self.total_ewma_ms is None else alpha * duration + (1.0 - alpha) * self.total_ewma_ms
        self.last_stop_reason = (str(stop_reason)[:64] if stop_reason else None)
        try:
            out = int(output_tokens or 0)
        except (TypeError, ValueError):
            out = 0
        # [audit 2026-10-30] A truncated/failed stream was already counted as
        # a success at first byte. Correct the counters at completion.
        if not success:
            try:
                if self.success > 0:
                    self.success -= 1
                self.errors += 1
            except Exception:
                pass
        if out > 0:
            tps = out / (duration / 1000.0)
            if tps > 0.0:
                self.tps_ewma = tps if self.tps_ewma is None else alpha * tps + (1.0 - alpha) * self.tps_ewma
                self.tps_n += 1
                self.tps_ring.append(tps)
        floor = max(0.1, float(cfg.throughput_floor_tps))
        _min_n = max(1, int(cfg.slow_lane_min_samples))
        _ttfb_ceil = max(1.0, float(getattr(cfg, "ttfb_ceiling_ms", 8000.0)))
        _ttfb_slow = (
            self.ttfb_n >= _min_n
            and self.ewma_ms is not None
            and self.ewma_ms > _ttfb_ceil
        )
        _tps_slow = self.tps_n >= _min_n and self.tps_ewma is not None and self.tps_ewma < floor
        if _tps_slow or _ttfb_slow:
            self.slow_lane_until = now + max(0.0, float(cfg.slow_lane_cooldown_s))
        elif (
            self.tps_ewma is not None
            and self.tps_ewma >= floor * 1.2
            and (self.ewma_ms is None or self.ewma_ms <= _ttfb_ceil * 0.8)
        ):
            # Récupération explicite : une IP qui remonte au-dessus du plancher
            # ET dont le TTFB est redescendu ne reste pas quarantinée.
            self.slow_lane_until = 0.0

    def is_slow_lane(self, now: float) -> bool:
        return bool(self.slow_lane_until and now < self.slow_lane_until)

    def tps_trend_pct(self, window: int = 5) -> float | None:
        """Baisse relative (%) entre le début et la fin de la fenêtre tok/s.

        Positif = dégradation (ex. 80 → 55 tok/s donne ~31 %). None si moins
        de 3 points ou base nulle.
        """
        pts = list(self.tps_ring)[-max(3, int(window)) :]
        if len(pts) < 3 or pts[0] <= 0:
            return None
        return max(0.0, (pts[0] - pts[-1]) / pts[0] * 100.0)

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
        if self.is_slow_lane(now):
            return float(cfg.slow_lane_score)

        latency = self.latency_ms()
        if latency is None:
            latency_score = 0.5
        else:
            span = max(1.0, cfg.latency_ceiling_ms - cfg.latency_floor_ms)
            latency_score = max(0.0, min(1.0, 1.0 - (latency - cfg.latency_floor_ms) / span))

        if self.tps_ewma is None:
            throughput_score = 0.5
        else:
            target = max(1.0, cfg.throughput_target_tps)
            throughput_score = max(0.0, min(1.0, self.tps_ewma / target))

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
            + cfg.throughput_weight * throughput_score
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
            "server_host": self.server_host,
            "count": self.count,
            "success": self.success,
            "errors": self.errors,
            "recent_429": self.recent_429,
            "ewma_ms": round(self.ewma_ms, 1) if self.ewma_ms is not None else None,
            "p95_ms": self.p95_ms(),
            "tps_ewma": round(self.tps_ewma, 2) if self.tps_ewma is not None else None,
            "tps_n": self.tps_n,
            "ttfb_n": self.ttfb_n,
            "total_ewma_ms": round(self.total_ewma_ms, 1) if self.total_ewma_ms is not None else None,
            "last_stop_reason": self.last_stop_reason,
            "slow_lane": self.is_slow_lane(now),
            "slow_lane_remaining_s": (
                round(max(0.0, self.slow_lane_until - now), 1) if self.is_slow_lane(now) else 0.0
            ),
            "probe_ms": round(self.last_probe_ms, 1) if self.last_probe_ms is not None else None,
            "probe_ok": self.last_probe_ok,
            "probe_age_s": (round(max(0.0, now - self.last_probe_at), 1) if self.last_probe_at else None),
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
        self.get(sid, ip).observe(duration_ms, status, time.monotonic(), latency=latency)

    def observe_probe(self, sid: int, ip: str, duration_ms: float, ok: bool) -> None:
        self.get(sid, ip).observe_probe(duration_ms, ok, time.monotonic())

    def observe_completion(
        self,
        sid: int,
        ip: str,
        duration_ms: float,
        output_tokens: int,
        *,
        success: bool = True,
        stop_reason: str | None = None,
        server_host: str | None = None,
    ) -> None:
        self.get(sid, ip).observe_completion(
            duration_ms,
            output_tokens,
            time.monotonic(),
            self.cfg,
            success=success,
            stop_reason=stop_reason,
            server_host=server_host,
        )

    def is_slow_lane(self, sid: int, ip: str, now: float | None = None) -> bool:
        item = self._items.get((int(sid), str(ip or "unknown")))
        if item is None:
            return False
        return item.is_slow_lane(float(now if now is not None else time.monotonic()))

    def is_degrading(self, sid: int, ip: str, pct: float = 25.0) -> bool:
        item = self._items.get((int(sid), str(ip or "unknown")))
        if item is None:
            return False
        trend = item.tps_trend_pct()
        return trend is not None and trend >= float(pct)

    def is_ttfb_high(self, sid: int, ip: str, ceiling_ms: float = 8000.0) -> bool:
        item = self._items.get((int(sid), str(ip or "unknown")))
        if item is None:
            return False
        return item.ttfb_n >= max(1, int(self.cfg.slow_lane_min_samples)) and (
            item.ewma_ms is not None and item.ewma_ms > float(ceiling_ms)
        )

    def host_quality(self, host: str, now: float | None = None) -> dict | None:
        """Qualité agrégée d'un hostname VPN (toutes stations/IP confondues).

        Le hostname est la clé de présélection : l'IP publique mesurée n'est
        pas l'IP d'entrée de l'API NordVPN. Retourne None si aucune mesure.
        """
        host = str(host or "").strip().lower()
        if not host:
            return None
        items = [i for i in self._items.values() if str(i.server_host or "").lower() == host]
        if not items:
            return None
        tps = [float(i.tps_ewma) for i in items if i.tps_ewma is not None]
        ttfb = [float(i.ewma_ms) for i in items if i.ewma_ms is not None]
        n = sum(int(i.tps_n) for i in items)
        now_ts = float(now if now is not None else time.monotonic())
        return {
            "host": host,
            "n": n,
            "tps": (sum(tps) / len(tps)) if tps else None,
            "ttfb": (sum(ttfb) / len(ttfb)) if ttfb else None,
            "slow": any(i.is_slow_lane(now_ts) for i in items),
            "ips": len(items),
        }

    def slow_station_count(self, now: float | None = None) -> int:
        """Nombre de stations dont au moins une IP est en slow lane."""
        now_ts = float(now if now is not None else time.monotonic())
        return len({i.sid for i in self._items.values() if i.is_slow_lane(now_ts)})

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
