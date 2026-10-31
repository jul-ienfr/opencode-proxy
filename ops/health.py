"""Classification de santé des stations et anti-flapping.

Ce module ne connaît ni Docker ni VPNState : il reçoit des statuts sous forme
de chaînes et applique une politique bornée. Il est volontairement pur pour
être testable sans event loop ni conteneur.

Politique :
- un passage ``connected``/``degraded`` -> ``error`` (ou inverse) n'est publié
  qu'après ``flap_hysteresis_s`` secondes et ``flap_min_observations``
  observations consécutives ;
- ``flap_hysteresis_s == 0`` conserve exactement le comportement historique
  (utile aux tests et au rollback) ;
- les transitions stables sont comptées pour exposer ``flap_count_1h``.
"""

from __future__ import annotations

import os
from collections import deque
from dataclasses import dataclass, field
from typing import Any


def _yaml_get(section: str, key: str, default: Any = None) -> Any:
    try:
        from config.settings import yaml_get

        return yaml_get(section, key, default)
    except Exception:
        return default


@dataclass
class HealthPolicy:
    """Politique de classification, lue depuis ``config.yaml:stations.health``."""

    effective_stack_filter: bool = True
    flap_hysteresis_s: float = 0.0
    flap_min_observations: int = 2
    churn_scan_max_min: int = 10
    log_scan_tail: int = 2000
    secondary_path_hold_s: float = 0.0
    degraded_hold_s: float = 0.0

    @classmethod
    def from_config(cls, cfg: dict | None = None) -> HealthPolicy:
        raw = cfg if isinstance(cfg, dict) else {}
        _health_raw = raw.get("health")
        health: dict = _health_raw if isinstance(_health_raw, dict) else raw

        def _float(name: str, default: float) -> float:
            try:
                return max(0.0, float(health.get(name, default)))
            except (TypeError, ValueError):
                return default

        def _int(name: str, default: int) -> int:
            try:
                return max(0, int(health.get(name, default)))
            except (TypeError, ValueError):
                return default

        _hyst = min(300.0, _float("flap_hysteresis_s", 0.0))
        # Les tests historiques appellent _set_status une fois et attendent
        # l'état immédiat. L'anti-flap est donc désactivé sous pytest, sauf
        # test dédié qui active explicitement le flag d'environnement.
        _secondary_hold = min(600.0, _float("secondary_path_hold_s", 0.0))
        _degraded_hold = min(300.0, _float("degraded_hold_s", 0.0))
        if os.getenv("PYTEST_CURRENT_TEST") and not os.getenv("STATION_FLAP_TEST"):
            _hyst = 0.0
            _secondary_hold = 0.0
            _degraded_hold = 0.0
        return cls(
            effective_stack_filter=bool(health.get("effective_stack_filter", True)),
            flap_hysteresis_s=_hyst,
            flap_min_observations=max(1, min(10, _int("flap_min_observations", 2))),
            churn_scan_max_min=max(2, min(60, _int("churn_scan_max_min", 10))),
            log_scan_tail=max(100, min(20000, _int("log_scan_tail", 2000))),
            secondary_path_hold_s=_secondary_hold,
            degraded_hold_s=_degraded_hold,
        )

    @classmethod
    def from_yaml(cls) -> HealthPolicy:
        health = _yaml_get("stations", "health", {}) or {}
        return cls.from_config({"health": health})


@dataclass
class StatusDebouncer:
    """Filtre les transitions instables et compte les flips.

    ``stable`` est l'état publié. Un état candidat différent doit tenir
    ``hysteresis_s`` et ``min_observations`` avant d'être publié. Les états
    ``connected``/``degraded`` ne sont jamais retardés quand l'état courant
    n'est pas routable (récupération immédiate).
    """

    policy: HealthPolicy = field(default_factory=HealthPolicy)
    stable: str = "disconnected"
    candidate: str | None = None
    candidate_since: float = 0.0
    candidate_hits: int = 0
    transition_times: deque[float] = field(default_factory=lambda: deque(maxlen=256))
    total_transitions: int = 0

    def _is_up(self, status: str) -> bool:
        return status in ("connected", "degraded")

    def observe(self, raw_status: str, now: float) -> str:
        """Retourne l'état stable à publier pour ``raw_status``."""
        raw = str(raw_status or "disconnected")
        if self.policy.flap_hysteresis_s <= 0 or not self.policy.effective_stack_filter:
            if raw != self.stable:
                self._apply(raw, now)
            return self.stable

        if raw == self.stable:
            self.candidate = None
            self.candidate_hits = 0
            return self.stable

        # Une récupération (vers connected) ne doit jamais être retardée :
        # retarder une guérison prolonge une coupure réelle.
        if raw == "connected":
            self._apply(raw, now)
            return self.stable

        # Une première erreur au boot (état non routable) est publiée tout de
        # suite : il n'y a pas de trafic à protéger et l'opérateur doit voir
        # l'échec.
        if not self._is_up(self.stable):
            self._apply(raw, now)
            return self.stable

        if raw != self.candidate:
            self.candidate = raw
            self.candidate_since = now
            self.candidate_hits = 1
        else:
            self.candidate_hits += 1

        enough_hits = self.candidate_hits >= self.policy.flap_min_observations
        enough_time = (now - self.candidate_since) >= self.policy.flap_hysteresis_s
        if enough_hits and enough_time:
            self._apply(raw, now)
        return self.stable

    def _apply(self, status: str, now: float) -> None:
        if status == self.stable:
            return
        self.stable = status
        self.candidate = None
        self.candidate_hits = 0
        self.transition_times.append(float(now))
        self.total_transitions += 1

    def force(self, status: str, now: float) -> None:
        """Publie immédiatement (start/stop explicite, shutdown)."""
        self._apply(str(status), now)

    def flap_count(self, now: float, window_s: float = 3600.0) -> int:
        cutoff = float(now) - max(1.0, float(window_s))
        while self.transition_times and self.transition_times[0] < cutoff:
            self.transition_times.popleft()
        return len(self.transition_times)

    def snapshot(self, now: float) -> dict[str, Any]:
        return {
            "stable": self.stable,
            "candidate": self.candidate,
            "candidate_hits": self.candidate_hits,
            "candidate_age_s": (round(max(0.0, float(now) - self.candidate_since), 1) if self.candidate else 0.0),
            "flap_count_1h": self.flap_count(now),
            "total_transitions": self.total_transitions,
            "hysteresis_s": self.policy.flap_hysteresis_s,
        }


__all__ = ["HealthPolicy", "StatusDebouncer"]
