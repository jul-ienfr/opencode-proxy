"""Politique de rotation et de libération — 023 (T029/T034/T035).

Une IP prouvée n'est libérée que sur preuve dure : quota avec ``reset_at``,
burn de format avec ``wire_fp``, ou mismatch appuyé par deux lecteurs
concordants. Un 429 ambigu, un timeout ou un sentinel ne libèrent jamais.

Le garde de rotation borne les rotations par station et par heure, et
coalesce les demandes identiques.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

RELEASE_QUOTA = "quota"
RELEASE_BURN = "burned"
RELEASE_MISMATCH = "mismatch"

_RELEASABLE = frozenset({RELEASE_QUOTA, RELEASE_BURN, RELEASE_MISMATCH})


@dataclass(frozen=True)
class ReleaseEvidence:
    kind: str
    reason: str = ""
    reset_at: float | None = None
    wire_fp: str = ""
    readers: tuple[str, ...] = ()
    readers_agree: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "reason": self.reason,
            "reset_at": self.reset_at,
            "wire_fp": self.wire_fp,
            "readers": list(self.readers),
            "readers_agree": self.readers_agree,
        }


def should_release(
    evidence: ReleaseEvidence,
    *,
    proof_fresh: bool = False,
) -> tuple[bool, str]:
    """Décide d'une libération et retourne ``(ok, raison)``."""
    if evidence.kind not in _RELEASABLE:
        return False, f"not a hard evidence ({evidence.kind or 'none'})"
    if evidence.kind == RELEASE_QUOTA:
        if not evidence.reset_at:
            return False, "quota without reset_at"
        return True, "quota with provider reset_at"
    if evidence.kind == RELEASE_BURN:
        if not evidence.wire_fp:
            return False, "burned without wire_fp"
        return True, "format burn with wire_fp"
    if evidence.kind == RELEASE_MISMATCH:
        if not evidence.readers_agree or len(set(evidence.readers)) < 2:
            return False, "mismatch without two agreeing readers"
        return True, "mismatch confirmed by two readers"
    return False, "unhandled evidence"


@dataclass
class StationRate:
    last_at: float = 0.0
    timestamps: list[float] = field(default_factory=list)
    coalesced: int = 0


class RotationGuard:
    """Token bucket par ``(provider, station)`` + plafond horaire global."""

    def __init__(
        self,
        *,
        min_interval_s: float = 600.0,
        max_per_hour: int = 6,
        clock=time.monotonic,
    ) -> None:
        self.min_interval_s = max(0.0, float(min_interval_s))
        self.max_per_hour = max(0, int(max_per_hour))
        self._clock = clock
        self._lock = threading.RLock()
        self._stations: dict[tuple[str, int], StationRate] = {}
        self._global: list[float] = []

    def _prune(self, now: float) -> None:
        horizon = now - 3600.0
        self._global = [t for t in self._global if t >= horizon]
        for rate in self._stations.values():
            rate.timestamps = [t for t in rate.timestamps if t >= horizon]

    def allow(self, provider: str, station: int, *, hard: bool = False) -> tuple[bool, str]:
        """``hard=True`` pour un burn/mismatch prouvé (ignore l'intervalle)."""
        now = self._clock()
        key = (str(provider), int(station))
        with self._lock:
            self._prune(now)
            rate = self._stations.setdefault(key, StationRate())
            if len(self._global) >= self.max_per_hour and not hard:
                rate.coalesced += 1
                return False, "hourly rotation cap reached"
            if not hard and rate.last_at and now - rate.last_at < self.min_interval_s:
                rate.coalesced += 1
                return False, "station rotation interval not elapsed"
            return True, "allowed"

    def record(self, provider: str, station: int) -> None:
        now = self._clock()
        key = (str(provider), int(station))
        with self._lock:
            rate = self._stations.setdefault(key, StationRate())
            rate.last_at = now
            rate.timestamps.append(now)
            self._global.append(now)

    def coalesced(self, provider: str, station: int) -> int:
        with self._lock:
            return self._stations.get((str(provider), int(station)), StationRate()).coalesced

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "stations": {
                    f"{p}:{s}": {
                        "last_at": r.last_at,
                        "count_hour": len(r.timestamps),
                        "coalesced": r.coalesced,
                    }
                    for (p, s), r in self._stations.items()
                },
                "global_hour": len(self._global),
                "max_per_hour": self.max_per_hour,
                "min_interval_s": self.min_interval_s,
            }
