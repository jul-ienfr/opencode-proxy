"""[plan v10 §3.6 Lot 3] Moteur de rotation latence-adaptive.

Décide soft_rotate/hard_rotate par (station, ip) d'après IpLatencyTracker,
applique cooldowns soft(600s)/hard(1800s), anti-flapping (6/h/station),
garde-fou global_degraded (≥50% stations tournées → pause 300s), toggle
maintenance ``rotation_paused``, et GARANTIE LRU : jamais zéro candidat —
au pire le moins mauvais est servi.

Config canonique : ``ip_rotation.latency_rotation`` dans config.yaml
(bloc §3.6.2 v5/v6), hot-reloadable via ``update_config()``.
Typé pour ``mypy --strict`` (charte §3.7).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ip_latency import IpLatencyTracker

logger = logging.getLogger(__name__)

COOLDOWN_SOFT: str = "soft"
COOLDOWN_HARD: str = "hard"


@dataclass
class EngineConfig:
    enabled: bool = True
    slow_threshold_ms: float = 8000.0
    ewma_threshold_ms: float = 6000.0
    p95_threshold_ms: float = 9000.0
    consecutive_slow: int = 3
    min_requests_before_eval: int = 5
    soft_cooldown_sec: float = 600.0
    hard_cooldown_sec: float = 1800.0
    ewma_alpha: float = 0.3
    window: int = 20
    per_model: dict[str, float] = field(
        default_factory=lambda: {"default": 8000.0}
    )
    floor_ms: float = 3000.0
    global_degraded_threshold: float = 0.5
    global_degraded_cooldown_sec: float = 300.0
    max_soft_rotates_per_hour: int = 6
    stream_metric: str = "ttfb"  # ttfb | total (v5 §3.6.1)
    prewarm_after_rotate: bool = True

    @classmethod
    def from_cfg(cls, cfg: dict[str, Any]) -> EngineConfig:
        c = cls()
        if not isinstance(cfg, dict):
            return c
        g = cfg.get
        c.enabled = bool(g("enabled", True))
        c.slow_threshold_ms = float(g("slow_threshold_ms", c.slow_threshold_ms))
        c.ewma_threshold_ms = float(g("ewma_threshold_ms", c.ewma_threshold_ms))
        c.p95_threshold_ms = float(g("p95_threshold_ms", c.p95_threshold_ms))
        c.consecutive_slow = int(g("consecutive_slow", c.consecutive_slow))
        c.min_requests_before_eval = int(
            g("min_requests_before_eval", c.min_requests_before_eval)
        )
        c.soft_cooldown_sec = float(g("soft_cooldown_sec", c.soft_cooldown_sec))
        c.hard_cooldown_sec = float(g("hard_cooldown_sec", c.hard_cooldown_sec))
        c.ewma_alpha = float(g("ewma_alpha", c.ewma_alpha))
        c.window = int(g("window", c.window))
        pm_raw = g("slow_threshold_ms_per_model", None)
        if isinstance(pm_raw, dict) and pm_raw:
            c.per_model = {str(k): float(v) for k, v in pm_raw.items()}
        c.floor_ms = float(g("floor_ms", c.floor_ms))
        c.global_degraded_threshold = float(
            g("global_degraded_threshold", c.global_degraded_threshold)
        )
        c.global_degraded_cooldown_sec = float(
            g("global_degraded_cooldown_sec", c.global_degraded_cooldown_sec)
        )
        c.max_soft_rotates_per_hour = int(
            g("max_soft_rotates_per_hour", c.max_soft_rotates_per_hour)
        )
        sm = str(g("stream_metric", c.stream_metric)).lower()
        c.stream_metric = sm if sm in ("ttfb", "total") else "ttfb"
        c.prewarm_after_rotate = bool(g("prewarm_after_rotate", True))
        return c

    def threshold_for(self, model: str) -> tuple[float, float]:
        """(seuil lent, seuil p95) pour un modèle — per-model sinon default."""
        base = self.per_model.get(model, self.per_model.get("default", self.slow_threshold_ms))
        return base, self.p95_threshold_ms


class LatencyRotationEngine:
    """État global du système de rotation latence-adaptive (singleton)."""

    def __init__(self) -> None:
        self.cfg = EngineConfig()
        self._trackers: dict[tuple[int, str], IpLatencyTracker] = {}
        # (sid, ip) -> (kind, until_monotonic)
        self._cooldowns: dict[tuple[int, str], tuple[str, float]] = {}
        # couples ayant DÉJÀ subi un soft — re-slow après expiration = hard
        self._soft_history: set[tuple[int, str]] = set()
        # anti-flap : timestamps (monotonic) des rotations déclenchées par sid
        self._soft_log: dict[int, deque[float]] = {}
        # fenêtre global_degraded : (mono, sid) des dernières rotations
        self._global_log: deque[tuple[float, int]] = deque(maxlen=64)
        self._global_paused_until: float = 0.0
        self.paused: bool = False
        self._now: Any = time.monotonic  # point d'injection tests

    # ── config ────────────────────────────────────────────────────────

    def update_config(self, cfg: dict[str, Any]) -> None:
        old = (
            self.cfg.window,
            self.cfg.ewma_alpha,
            self.cfg.min_requests_before_eval,
            self.cfg.consecutive_slow,
        )
        self.cfg = EngineConfig.from_cfg(cfg)
        new = (
            self.cfg.window,
            self.cfg.ewma_alpha,
            self.cfg.min_requests_before_eval,
            self.cfg.consecutive_slow,
        )
        if old != new:
            for tr in self._trackers.values():
                tr.window = self.cfg.window
                tr.alpha = self.cfg.ewma_alpha
                tr.min_requests_before_eval = self.cfg.min_requests_before_eval
                tr.consecutive_slow_limit = self.cfg.consecutive_slow

    def on_rotation_done(self, sid: int, new_ip: str) -> None:
        """Rotation réussie : warm-up v6 — reset consecutive_slow de tous les
        trackers de la station (la nouvelle IP démarre vierge, et les anciennes
        aussi pour éviter un faux signal au retour par fallback LRU)."""
        for (s, _ip), tr in self._trackers.items():
            if s == int(sid):
                tr.reset_consecutive_slow()
        self.tracker_for(int(sid), str(new_ip))

    def threshold_for(self, model: str) -> tuple[float, float]:
        return self.cfg.threshold_for(model)

    # ── trackers ──────────────────────────────────────────────────────

    def tracker_for(self, sid: int, ip: str) -> IpLatencyTracker:
        key = (int(sid), str(ip))
        tr = self._trackers.get(key)
        if tr is None:
            tr = IpLatencyTracker(
                station=int(sid),
                ip=str(ip),
                window=self.cfg.window,
                alpha=self.cfg.ewma_alpha,
                min_requests_before_eval=self.cfg.min_requests_before_eval,
                consecutive_slow_limit=self.cfg.consecutive_slow,
            )
            self._trackers[key] = tr
            while len(self._trackers) > 300:  # borne mémoire globale
                self._trackers.pop(next(iter(self._trackers)))
        return tr

    # ── cooldowns ─────────────────────────────────────────────────────

    def mark(self, sid: int, ip: str, kind: str) -> None:
        dur = self.cfg.soft_cooldown_sec if kind == COOLDOWN_SOFT else self.cfg.hard_cooldown_sec
        self._cooldowns[(int(sid), str(ip))] = (kind, self._now() + dur)
        logger.info("[latency] %s cooldown st%s ip=%s %.0fs", kind, sid, ip, dur)

    def cooldown_kind(self, sid: int, ip: str) -> str | None:
        entry = self._cooldowns.get((int(sid), str(ip)))
        if entry is None:
            return None
        kind, until = entry
        if self._now() >= until:
            del self._cooldowns[(int(sid), str(ip))]
            return None
        return kind

    def ip_hard_cooled(self, sid: int, ip: str) -> bool:
        return self.cooldown_kind(sid, ip) == COOLDOWN_HARD

    def lru_pick(self, candidates: list[tuple[int, str]]) -> tuple[int, str] | None:
        """GARANTIE LRU §3.6.5 : jamais zéro candidat — un non-refroidi gagne,
        sinon le plus proche d'expiration (least-recently-cooled)."""
        if not candidates:
            return None
        for cand in candidates:
            if self.cooldown_kind(int(cand[0]), str(cand[1])) is None:
                return cand
        best: tuple[int, str] | None = None
        best_until = float("inf")
        for cand in candidates:
            entry = self._cooldowns.get((int(cand[0]), str(cand[1])))
            until = entry[1] if entry else float("-inf")
            if until < best_until:
                best, best_until = cand, until
        return best

    # ── garde-fous ────────────────────────────────────────────────────

    def _note_rotation(self, sid: int) -> None:
        now = self._now()
        dq = self._soft_log.setdefault(int(sid), deque())
        dq.append(now)
        hour_ago = now - 3600.0
        while dq and dq[0] < hour_ago:
            dq.popleft()
        self._global_log.append((now, int(sid)))

    def can_soft_rotate(self, sid: int, total_stations: int) -> tuple[bool, str]:
        """Anti-flap + paused + global_degraded (§3.6.4/§3.6.5)."""
        if not self.cfg.enabled:
            return False, "disabled"
        if self.paused:
            return False, "rotation_paused"
        now = self._now()
        hour_ago = now - 3600.0
        dq = self._soft_log.get(int(sid))
        if dq is not None:
            # [fix v10] purge ICI : sinon len(dq) compte des notes sorties de
            # la fenêtre et le cap ne se lève jamais.
            while dq and dq[0] < hour_ago:
                dq.popleft()
        if dq and len(dq) >= self.cfg.max_soft_rotates_per_hour:
            return False, "anti_flapping"
        if now < self._global_paused_until:
            return False, "global_degraded_pause"
        if total_stations > 0 and self._global_log:
            recent_sids = {sid_t for ts, sid_t in self._global_log if ts >= now - 600.0}
            if len(recent_sids) / total_stations >= self.cfg.global_degraded_threshold:
                self._global_paused_until = self._now() + self.cfg.global_degraded_cooldown_sec
                logger.warning(
                    "[latency] global_degraded: %d/%d stations tournées <10min → pause %.0fs",
                    len(recent_sids),
                    total_stations,
                    self.cfg.global_degraded_cooldown_sec,
                )
                return False, "global_degraded"
        return True, ""

    # ── point d'entrée requête ────────────────────────────────────────

    def record_request(
        self,
        sid: int,
        ip: str,
        duration_ms: float,
        model: str,
        status_code: int | None = None,
    ) -> dict[str, Any]:
        """Mesure §3.6.1 : succès ET échec. Retourne la décision prise.

        Anti-re-mark : une fois un cooldown posé sur (sid, ip), les requêtes
        suivantes NE ré-escaladent pas tant que la rotation n'a pas changé
        l'IP (on_rotation_done) — sinon chaque requête lente post-mark
        escaladerait soft→hard dans la même rafale. Escalade hard réservée
        au cas : soft déjà subi par ce couple, expiré, et re-détection lente
        (l'IP est revenue en service via fallback LRU et reste mauvaise)."""
        if not self.cfg.enabled:
            return {"action": "none", "reason": "disabled"}
        key = (int(sid), str(ip))
        prev_kind = self.cooldown_kind(sid, ip)
        threshold_for, p95_threshold = self.threshold_for(model)
        effective_slow = max(threshold_for, self.cfg.floor_ms)
        tr = self.tracker_for(sid, ip)
        warmup_skip = tr.request_count == 0
        tr.record(duration_ms, effective_slow)
        action, reason = "none", ""
        if status_code is not None and status_code != 200:
            reason = f"http_{status_code}"
        elif warmup_skip:
            reason = "warmup_excluded"
        elif not tr.should_soft_rotate(threshold_for, p95_threshold):
            pass  # seuils non atteints
        elif prev_kind is not None:
            # déjà sous cooldown ACTIF → rien à faire, attendre la rotation
            # (sinon chaque requête lente post-mark ré-escaladerait)
            action, reason = "none", f"{prev_kind}_active"
        elif key in self._soft_history:
            # soft déjà subi par ce couple puis expiré et re-slow (l'IP est
            # revenue en service via fallback LRU et reste mauvaise) → hard
            self.mark(sid, ip, COOLDOWN_HARD)
            action, reason = "hard", "repeated_slow_after_soft"
        else:
            self._soft_history.add(key)
            self.mark(sid, ip, COOLDOWN_SOFT)
            action, reason = "soft", "latency_thresholds"
        return {
            "action": action,
            "reason": reason,
            "ewma": tr.ewma_ms,
            "p95": tr.p95_ms(),
            "count": tr.request_count,
        }


_ENGINE: LatencyRotationEngine | None = None


def get_engine() -> LatencyRotationEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = LatencyRotationEngine()
        try:
            from config.settings import IP_ROTATION

            _ENGINE.update_config(dict(IP_ROTATION.get("latency_rotation") or {}))
        except Exception:
            pass
    return _ENGINE
