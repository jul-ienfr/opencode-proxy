"""[plan v10 §4 Lot 1] StationSupervisor — un objet par station, cycle de vie
isolé. Wrapper par COMPOSITION (pas de fork du VPNManager de 4770 lignes) :
chaque méthode délègue au manager existant et porte l'état d'isolation
(breaker local léger, warm-up post-rotation v6, tracker de latence).

Contrat v5/v9 :
- ``shared_state.vpn_managers`` reste la liste des managers NUS — zéro impact
  sur les consumers existants ; les superviseurs vivent dans
  ``shared_state.station_supervisors`` (aligné 1:1 par numéro de station).
- Escape hatch : ``supervisor.enabled: false`` dans config.yaml → aucun
  superviseur créé, chemin legacy intact (rollback du refactor jusqu'au
  jalon Train 1 vert).
- Lot 2 branchera watchdog/restart ici ; Lot 3 branchera soft_rotate via
  IpLatencyTracker.should_soft_rotate (aujourd'hui gelé à False).

Typé pour ``mypy --strict`` (charte §3.7 nouveaux modules).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from ip_latency import IpLatencySnapshot, IpLatencyTracker

logger = logging.getLogger(__name__)

# Warm-up post-rotation (v6 §3.6.5) : la 1ʳᵉ requête sur une IP neuve paie le
# handshake TLS/SOCKS et ne doit pas compter comme "slow".
# [plan 30/08 — constante → config] clé ``supervisor.warmup_excluded_requests``
# (défaut 1 = comportement historique, bornes 0–1000 appliquées à la lecture).
# La valeur se relit à chaque usage via config mirror → hot-reload OK.
WARMUP_EXCLUDED_REQUESTS: int = 1


def warmup_excluded_requests() -> int:
    """[plan 30/08] Valeur effective du warm-up (clé supervisor.*, clampée)."""
    default = WARMUP_EXCLUDED_REQUESTS
    try:
        from config.settings import _yaml_data

        raw = (_yaml_data.get("supervisor") or {}).get(
            "warmup_excluded_requests", default
        )
        val = int(raw)
    except Exception:
        return default
    if val < 0 or val > 1000:
        clamped = max(0, min(1000, val))
        logger.warning(
            "[supervisor-config] warmup_excluded_requests=%r hors bornes [0, 1000]"
            " — clamp à %s",
            raw,
            clamped,
        )
        return clamped
    return val


@dataclass
class StationSupervisor:
    """État isolé par station + délégation au VPNManager existant."""

    station: int
    manager: Any  # vpn_manager.VPNManager (non importé pour éviter le cycle)
    ip_latency: dict[str, IpLatencyTracker] = field(default_factory=dict)
    # Breaker local léger (Lot 3 remplacera par CircuitBreaker per-station §3.4)
    consecutive_failures: int = 0
    breaker_open_until: float = 0.0
    breaker_threshold: int = 3
    breaker_cooldown_sec: float = 60.0
    last_probe_mono: float = 0.0
    restart_in_progress: bool = False

    # ── cycle de vie (délégation pure) ────────────────────────────────

    async def start(self) -> None:
        await self.manager.start()

    async def stop(self) -> None:
        await self.manager.stop()

    async def restart(self, reason: str = "") -> None:
        """restart sérialisé : un second appel pendant un restart est no-op
        (garde anti-thundering, exploité par le watchdog Lot 2)."""
        if self.restart_in_progress:
            logger.info(
                "[supervisor st%s] restart déjà en cours — no-op (%s)",
                self.station,
                reason,
            )
            return
        self.restart_in_progress = True
        try:
            await self.manager.restart()
            logger.info("[supervisor st%s] restart ok (%s)", self.station, reason)
        finally:
            self.restart_in_progress = False

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.breaker_open_until = 0.0

    def record_failure(self) -> bool:
        """Incrémente le breaker local. Retourne True si le breaker VIENT
        de s'ouvrir (pour déclencher une rotation au Lot 3)."""
        self.consecutive_failures += 1
        if (
            self.consecutive_failures >= self.breaker_threshold
            and self.breaker_open_until == 0.0
        ):
            self.breaker_open_until = time.monotonic() + self.breaker_cooldown_sec
            logger.warning(
                "[supervisor st%s] breaker OPEN %ss après %s échecs",
                self.station,
                self.breaker_cooldown_sec,
                self.consecutive_failures,
            )
            return True
        return False

    def breaker_open(self) -> bool:
        if self.breaker_open_until == 0.0:
            return False
        if time.monotonic() >= self.breaker_open_until:
            # half-open immédiat : une seule sonde passera (Lot 3 posera le
            # garde single-probe §14.1.11 côté appelant)
            self.breaker_open_until = 0.0
            self.consecutive_failures = self.breaker_threshold - 1
            return False
        return True

    # ── latence / rotation (Lot 3 : délègue au moteur partagé) ────────

    def _engine(self) -> Any:
        from latency_rotation import get_engine

        return get_engine()

    def tracker_for(self, ip: str) -> IpLatencyTracker:
        """Tracker (station, ip) — objet PARTAGÉ avec le moteur (une seule
        source de vérité : la vitrine locale et les décisions lisent la même
        instance)."""
        tr: IpLatencyTracker
        try:
            tr = self._engine().tracker_for(self.station, ip)
            return tr
        except Exception:
            cached = self.ip_latency.get(ip)
            if cached is not None:
                return cached
            tr = IpLatencyTracker(station=self.station, ip=ip)
            self.ip_latency[ip] = tr
            return tr

    def on_ip_finalized(self, new_ip: str) -> None:
        """finalize_ip hook (v6 §3.6.5) : reset consecutive_slow — la requête
        de warm-up sur l'IP neuve ne doit pas déclencher de soft_rotate.
        Lot 3 : les trackers vivent dans le moteur → délégation."""
        self.consecutive_failures = 0
        for tr in self.ip_latency.values():
            tr.reset_consecutive_slow()
        try:
            self._engine().on_rotation_done(self.station, new_ip)
        except Exception:
            pass
        self.tracker_for(new_ip)  # pré-crée le slot de la nouvelle IP

    def should_soft_rotate(self, model: str, thresholds: dict[str, float]) -> bool:
        """Lot 3 : délègue au tracker partagé (seuils per-model du moteur)."""
        engine = self._engine()
        threshold_for, p95_threshold = engine.threshold_for(model)
        active_ip = str(getattr(self.manager, "current_ip", "") or "")
        tr = self.tracker_for(active_ip)
        result: bool = bool(tr.should_soft_rotate(threshold_for, p95_threshold))
        return result

    def record_request(
        self,
        ip: str,
        duration_ms: float,
        model: str,
        status_code: int | None = None,
    ) -> dict[str, Any]:
        """Mesure §3.6.1 — délègue au moteur (cooldowns/décisions inclus)."""
        decision: dict[str, Any] = self._engine().record_request(
            self.station, ip, duration_ms, model, status_code
        )
        return decision

    async def react_to_decision(
        self, decision: dict[str, Any], station_obj: Any = None
    ) -> dict[str, Any]:
        """Exécute la décision du moteur : soft → rotation pool existante
        (_launch_rotation), hard → restart superviseur + rotation. Garde-fous
        (anti-flap/paused/global_degraded) appliqués AVANT toute action."""
        import shared_state

        action = decision.get("action")
        if action not in ("soft", "hard"):
            return {"executed": False, "why": decision.get("reason", "none")}
        total = len(getattr(shared_state, "vpn_managers", None) or []) or 1
        ok, why = self._engine().can_soft_rotate(self.station, total)
        if not ok:
            logger.info("[supervisor st%s] rotation ignorée (%s)", self.station, why)
            return {"executed": False, "why": why}
        self._engine()._note_rotation(self.station)
        pool = getattr(shared_state, "free_ip_pool", None)
        if action == "hard":
            await self.restart(f"latency_hard {decision.get('reason', '')}")
        if station_obj is None and pool is not None:
            for m in getattr(shared_state, "vpn_managers", None) or []:
                if m._station == self.station:
                    station_obj = m
                    break
        launcher = getattr(pool, "_launch_rotation", None) if pool is not None else None
        if callable(launcher) and station_obj is not None:
            try:
                launcher(station_obj)
                return {"executed": True, "action": action}
            except Exception as exc:
                logger.warning("[supervisor st%s] launch rotation failed: %s", self.station, exc)
                return {"executed": False, "why": f"launch_failed: {exc}"}
        return {"executed": False, "why": "no_pool_launcher"}

    # ── observabilité ─────────────────────────────────────────────────

    def status(self) -> dict[str, Any]:
        mgr_status: Any
        try:
            get_status = getattr(self.manager, "get_status", None)
            mgr_status = get_status() if callable(get_status) else {}
        except Exception as exc:  # fail-soft : le dashboard ne doit jamais 500
            mgr_status = {"error": str(exc)}
        latency = {
            ip: snap.__dict__
            for ip, tr in list(self.ip_latency.items())[:10]
            for snap in [tr.snapshot()]
        }
        return {
            "station": self.station,
            "manager": mgr_status,
            "consecutive_failures": self.consecutive_failures,
            "breaker_open": self.breaker_open(),
            "restart_in_progress": self.restart_in_progress,
            "latency": latency,
        }

    def latency_snapshot(self, ip: str) -> IpLatencySnapshot:
        return self.tracker_for(ip).snapshot()


def build_supervisors(managers: list[Any]) -> list[StationSupervisor]:
    """Construit les superviseurs alignés 1:1 avec la registry de managers."""
    supervisors = [
        StationSupervisor(station=m._station, manager=m)
        for m in sorted(managers, key=lambda m: m._station)
    ]
    return supervisors


def sync_supervisors(current: list[StationSupervisor], managers: list[Any]) -> list[StationSupervisor]:
    """Aligne la liste des superviseurs sur la registry de managers :
    crée les manquants (upscale), retire les partis (downscale), conserve
    l'état isolé des stations qui persistent. L'état est lié au SID (la
    station et son volume persistent), pas à l'identité d'objet du manager
    (reconstruit possible au boot-reconcile) — re-liaison systématique."""
    by_sid = {s.station: s for s in current}
    out: list[StationSupervisor] = []
    for m in sorted(managers, key=lambda m: m._station):
        sup = by_sid.get(m._station)
        if sup is None:
            sup = StationSupervisor(station=m._station, manager=m)
        else:
            sup.manager = m
        out.append(sup)
    return out
