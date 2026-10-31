"""Planificateur de rotation proactive (drain, spare, une station à la fois).

Il ne lance aucune rotation : il décide *quand* et *laquelle*, à partir des
leases IP/quota. Le pool applique la décision via ``_launch_rotation``.

Règles :

- jamais deux rotations planifiées simultanément ;
- jamais la dernière station active ;
- avec un spare qualifié : promotion du spare avant drain (zéro trou) ;
- sans spare : une seule station à la fois, capacité N-1 visible.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from free.ip_quota import DRAINING, EXHAUSTED, IpQuotaLease, QuotaProfile


@dataclass
class RotationDecision:
    station: int
    action: str
    reason: str
    model: str
    ip: str
    headroom: int
    spare_used: bool = False
    state: str = DRAINING

    def snapshot(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "action": self.action,
            "reason": self.reason,
            "model": self.model,
            "ip": self.ip,
            "headroom": self.headroom,
            "spare_used": self.spare_used,
            "state": self.state,
        }


@dataclass
class RotationPlanner:
    enabled: bool = True
    max_concurrent: int = 1
    min_active: int = 1
    profile: QuotaProfile = field(default_factory=QuotaProfile)
    in_flight: int = 0
    rotations: deque[float] = field(default_factory=lambda: deque(maxlen=512))
    last_decision: RotationDecision | None = None
    last_defer_reason: str | None = None

    def _active_rotations(self, now: float) -> int:
        return max(0, int(self.in_flight))

    def note_started(self, now: float | None = None) -> None:
        self.in_flight = max(0, int(self.in_flight)) + 1
        self.rotations.append(float(now if now is not None else time.monotonic()))

    def note_finished(self) -> None:
        self.in_flight = max(0, int(self.in_flight) - 1)

    def rotations_last_hour(self, now: float | None = None) -> int:
        ts = float(now if now is not None else time.monotonic())
        while self.rotations and ts - self.rotations[0] > 3600.0:
            self.rotations.popleft()
        return len(self.rotations)

    def should_rotate(self, lease: IpQuotaLease) -> bool:
        if lease.state in (DRAINING, EXHAUSTED):
            return True
        if lease.recent_429 >= 3:
            return True
        threshold = self.profile.rotation_headroom_for(lease.model)
        return lease.headroom <= threshold

    def plan(
        self,
        leases: Iterable[IpQuotaLease],
        *,
        active_count: int,
        qualified_spares: int = 0,
        rotation_in_flight: bool = False,
        now: float | None = None,
    ) -> RotationDecision | None:
        if not self.enabled:
            self.last_defer_reason = "disabled"
            return None
        ts = float(now if now is not None else time.monotonic())
        if rotation_in_flight or self._active_rotations(ts) >= max(1, int(self.max_concurrent)):
            self.last_defer_reason = "rotation-in-flight"
            return None
        if int(active_count) <= max(1, int(self.min_active)):
            self.last_defer_reason = "last-active"
            return None
        candidates = [lease for lease in leases if self.should_rotate(lease)]
        if not candidates:
            self.last_defer_reason = "no-draining"
            return None
        # La pire IP d'abord : moins de headroom, plus de 429 récents, puis
        # l'identifiant de station pour un ordre déterministe.
        candidates.sort(key=lambda l: (l.headroom, -l.recent_429, l.station, l.model))
        target = candidates[0]
        spare = int(qualified_spares) > 0
        if not spare and int(active_count) <= 1:
            self.last_defer_reason = "no-qualified-spare"
            return None
        decision = RotationDecision(
            station=int(target.station),
            action="promote_spare" if spare else "rotate_one",
            reason="quota-draining" if target.state == DRAINING else "quota-threshold",
            model=target.model,
            ip=target.ip,
            headroom=target.headroom,
            spare_used=spare,
            state=target.state,
        )
        self.last_decision = decision
        self.last_defer_reason = None
        return decision

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "in_flight": self.in_flight,
            "max_concurrent": self.max_concurrent,
            "min_active": self.min_active,
            "rotations_last_hour": self.rotations_last_hour(now),
            "last_decision": self.last_decision.snapshot() if self.last_decision else None,
            "last_defer_reason": self.last_defer_reason,
        }


__all__ = ["RotationDecision", "RotationPlanner"]
