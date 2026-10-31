"""Sélecteur unique de station pour les requêtes free.

Legacy (quality désactivée) : le pool garde ses règles round-robin /
least-loaded / failover. Quality activée : le sélecteur classe les stations
éligibles par score mesuré, avec exploration bornée et part minimale pour
éviter la starvation.

Le sélecteur ne fait AUCUN appel réseau et ne connaît pas Docker : le pool lui
passe les stations déjà filtrées par `_station_usable`.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from typing import Any

from free.quality import QualityConfig, QualityRegistry


class StationSelector:
    def __init__(
        self,
        quality: QualityRegistry,
        cfg: QualityConfig | None = None,
        rng: Callable[[], float] | None = None,
        stats_provider: Callable[[Any], tuple[int, int, float | None, float | None]] | None = None,
    ) -> None:
        self.quality = quality
        self.cfg = cfg or quality.cfg
        self._rng = rng or random.random
        self._stats_provider = stats_provider

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled)

    def _stats(self, station: Any) -> tuple[int, int, float | None, float | None]:
        try:
            if self._stats_provider is not None:
                return self._stats_provider(station)
            per = getattr(station, "_quality_stats", None)
            if callable(per):
                used, limit, load = per()
                return int(used), int(limit), load, None
        except Exception:
            pass
        try:
            used = int(getattr(station, "_requests_this_ip", 0) or 0)
        except Exception:
            used = 0
        try:
            limit = int(getattr(station, "_quota_per_ip", 0) or 0)
        except Exception:
            limit = 0
        try:
            load = getattr(station, "_server_load", None)
        except Exception:
            load = None
        return used, limit, load, None

    def score(self, station: Any, *, now: float | None = None) -> float:
        import time as _time

        sid = int(getattr(station, "_station", -1))
        ip = str(getattr(station, "current_ip", "") or "unknown")
        used, limit, load, inflight = self._stats(station)
        item = self.quality.get(sid, ip)
        base = item.score(
            self.cfg,
            quota_used=used,
            quota_limit=limit or 1,
            server_load=load,
            now=float(now if now is not None else _time.monotonic()),
        )
        try:
            n = int(inflight or 0)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            # [P1] Une station déjà chargée cède la place avant saturation de
            # ses sessions ; à 0 in-flight le score reste inchangé.
            base = max(
                0.0,
                base - min(float(self.cfg.inflight_penalty_max), float(self.cfg.inflight_penalty) * n),
            )
        return base

    def _item(self, station: Any):
        sid = int(getattr(station, "_station", -1))
        ip = str(getattr(station, "current_ip", "") or "unknown")
        return self.quality.get(sid, ip)

    def _has_observation(self, station: Any) -> bool:
        try:
            item = self._item(station)
            return item.count > 0 or bool(item.last_probe_at)
        except Exception:
            return False

    def _is_cold(self, station: Any, now: float) -> bool:
        """Station jamais mesurée en fin de flux (tok/s inconnu)."""
        try:
            item = self._item(station)
            return item.tps_n == 0 and not item.is_slow_lane(now)
        except Exception:
            return False

    def rank(self, stations: list[Any]) -> list[Any]:
        if not stations:
            return []
        # Cold start : aucune mesure encore -> conserver l'ordre legacy
        # (préférence station 1 / ordre d'éligibilité), pour ne pas changer
        # le routage avant la première probe.
        if not any(self._has_observation(st) for st in stations):
            return list(stations)
        scored = [(self.score(st), st) for st in stations]

        def _key(pair):
            score, st = pair
            try:
                used = int(getattr(st, "_requests_this_ip", 0) or 0)
            except Exception:
                used = 0
            # Score d'abord, puis la station la moins chargée pour départager.
            return (-score, used, int(getattr(st, "_station", 0)))

        scored.sort(key=_key)
        return [st for _, st in scored]

    def choose(self, stations: list[Any]) -> Any | None:
        if not stations:
            return None
        if not self.enabled:
            return None
        ranked = self.rank(stations)
        # Cold start : aucune observation → ordre legacy déterministe, sans
        # exploration. Sinon un simple tirage à 5 % pouvait choisir une autre
        # station avant la première probe (routage non reproductible au boot).
        if not any(self._has_observation(st) for st in stations):
            return ranked[0]
        # Exploration bornée : évite de coller à une station dont le score
        # est momentanément biaisé (probe froide, endpoint local).
        # [P1 29/10] Si des stations n'ont AUCUN échantillon tok/s, les tirer
        # au hasard parmi elles (pas seulement ranked[1]) : sinon elles
        # restent froides à vie et le fallback est inconnu.
        try:
            if len(ranked) > 1 and (self._rng() * 100.0) < self.cfg.exploration_pct:
                _now = time.monotonic()
                _cold = [st for st in ranked[1:] if self._is_cold(st, _now)]
                _pool = _cold or ranked[1:]
                return _pool[min(len(_pool) - 1, int(self._rng() * len(_pool)))]
        except Exception:
            pass
        # Si les deux meilleurs sont dans le bruit, choisir la moins chargée
        # pour conserver la répartition de quota.
        if len(ranked) >= 2:
            try:
                s0 = self.score(ranked[0])
                s1 = self.score(ranked[1])
                if abs(s0 - s1) < 0.03:

                    def _used(st):
                        try:
                            return int(getattr(st, "_requests_this_ip", 0) or 0)
                        except Exception:
                            return 0

                    return min((ranked[0], ranked[1]), key=_used)
            except Exception:
                pass
        return ranked[0]

    def should_rotate(self, station: Any) -> bool:
        """Vrai si le score reste sous le plancher ``consecutive_low`` fois."""
        if not self.enabled:
            return False
        score = self.score(station)
        try:
            below = score < self.cfg.rotate_floor
        except Exception:
            return False
        if not below:
            try:
                station._quality_low_count = 0
            except Exception:
                pass
            return False
        try:
            n = int(getattr(station, "_quality_low_count", 0) or 0) + 1
            station._quality_low_count = n
        except Exception:
            n = 1
        return n >= self.cfg.consecutive_low


__all__ = ["StationSelector"]
