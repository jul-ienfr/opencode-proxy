"""[plan v10 §3.6.1 / Lot 1] Squelette IpLatencyTracker — mécanique neutre.

Lot 1 livre la STRUCTURE (ring, EWMA log-space, p95 glissant, compteurs) que
StationSupervisor porte par station. Les DÉCISIONS sont volontairement gelées
jusqu'au Lot 3 : ``should_soft_rotate()`` → False constant, persistance
désactivée. Lot 3 remplira les seuils (slow_threshold_ms_per_model,
consecutive_slow, min_requests_before_eval, global_degraded) SANS changer
l'API — contrat anti-inversion-de-dépendance v5.

Mesure streaming : Lot 3 branchera ``stream_metric: ttfb|total`` (§3.6.1 v5).
"""

from __future__ import annotations

import math
import statistics
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class IpLatencySnapshot:
    """Instantané sérialisable d'un tracker (dashboard / debug)."""

    count: int = 0
    ewma_ms: float | None = None
    p95_ms: float | None = None
    consecutive_slow: int = 0


@dataclass
class IpLatencyTracker:
    """Ring buffer par (station, ip) — mécanique uniquement (Lot 1).

    EWMA calculée en espace log (v6 §3.6.1) : latences LLM heavy-tail,
    l'EWMA brute est dominée par les outliers. Re-transformée à la lecture.
    """

    station: int
    ip: str
    window: int = 20
    alpha: float = 0.3
    _ring: deque[float] = field(default_factory=lambda: deque(maxlen=20))
    _ewma_log: float | None = None
    consecutive_slow: int = 0
    request_count: int = 0

    def __post_init__(self) -> None:
        if self._ring.maxlen != self.window:
            self._ring = deque(self._ring, maxlen=self.window)

    def record(self, duration_ms: float, slow_threshold_ms: float) -> None:
        """Enregistre une latence (succès comme échec — timeout = slow).

        Mécanique neutre Lot 1 : ring + EWMA log + p95 + compteurs.
        Aucune décision de rotation ici (gelée au Lot 3).
        """
        duration_ms = max(0.0, float(duration_ms))
        self._ring.append(duration_ms)
        self.request_count += 1
        ln = math.log(duration_ms) if duration_ms > 0 else math.log(1e-6)
        if self._ewma_log is None:
            self._ewma_log = ln
        else:
            self._ewma_log = self.alpha * ln + (1 - self.alpha) * self._ewma_log
        if slow_threshold_ms > 0 and duration_ms > slow_threshold_ms:
            self.consecutive_slow += 1
        else:
            self.consecutive_slow = 0

    def reset_consecutive_slow(self) -> None:
        """Appelé par StationSupervisor après finalize_ip (warm-up post-rotation,
        v6 §3.6.5) : la 1ʳᵉ requête sur une IP neuve paie le handshake."""
        self.consecutive_slow = 0

    def should_soft_rotate(self, threshold_for: float, p95_threshold: float) -> bool:
        """Gelé Lot 1 — toujours False. Lot 3 implémente :
        consecutive_slow>=3 ou (ewma>threshold_for et count>=5) ou
        (p95>p95_threshold et count>=5), min_requests_before_eval inclus."""
        del threshold_for, p95_threshold
        return False

    @property
    def ewma_ms(self) -> float | None:
        if self._ewma_log is None:
            return None
        return round(math.exp(self._ewma_log), 1)

    def p95_ms(self) -> float | None:
        if len(self._ring) < 2:
            return None
        ordered = sorted(self._ring)
        idx = max(0, math.ceil(0.95 * len(ordered)) - 1)
        return round(ordered[idx], 1)

    def latency_score(self, threshold_ms: float) -> int:
        """Score 0-100 dérivé du SEUIL CONFIGURÉ (correctif v5 — pas de
        diviseur magique : reste cohérent si ewma_threshold hot-reloadé).
        Server_scorer pattern, consommé par le dashboard Lot 3+."""
        ewma = self.ewma_ms
        if ewma is None or threshold_ms <= 0:
            return 0
        return min(100, int(ewma / threshold_ms * 100))

    def snapshot(self) -> IpLatencySnapshot:
        return IpLatencySnapshot(
            count=self.request_count,
            ewma_ms=self.ewma_ms,
            p95_ms=self.p95_ms(),
            consecutive_slow=self.consecutive_slow,
        )


def monotonic_now() -> float:
    """Point d'injection horloge (tests) — time.monotonic par défaut."""
    return time.monotonic()


def percentile(values: list[float], pct: float) -> float | None:
    """p-centile générique (tri) — partagé Lot 3 pour p95_threshold."""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
    return round(statistics.fmean([ordered[idx]]), 1)
