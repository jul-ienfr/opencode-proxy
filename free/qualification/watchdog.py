"""Per-station watchdog.

The watchdog probes each station independently, persists ``station_state``,
opens one incident per station/cause, and asks the injected repair adapter to
fix only the affected station.  A healthy serving station is never restarted.
A repair failure is bounded; after ``max_repair_attempts`` the incident is
escalated instead of looping forever.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from .store import QualificationStore

_LOG = logging.getLogger(__name__)

HEALTHY = "healthy"
TUNNEL_DOWN = "tunnel_down"
DNS_BROKEN = "dns_broken"
NO_EGRESS = "no_egress"
AUTH_FAILED = "auth_failed"
CLIENT_UNUSABLE = "client_unusable"
QUOTA = "quota"
PROBE_ERROR = "probe_error"

_QUOTA_REASONS = frozenset(
    {
        "daily-bucket-exhausted",
        "daily-bucket-reserve",
        "draining",
        "exhausted",
        "quota-blocked",
        "reserve",
        "warming",
        "bad-ip",
        "no-lease",
    }
)


@dataclass(frozen=True, slots=True)
class StationObservation:
    station: int
    connected: bool = False
    dns_ok: bool = False
    egress_ip: str = ""
    auth_failed: bool = False
    client_usable: bool = False
    latency_ms: float = 0.0
    country: str = ""
    hostname: str = ""
    transport: str = ""
    container: str = ""
    error: str = ""
    reason: str = ""


class StationProbe(Protocol):
    async def probe(self, station: int) -> StationObservation: ...


class StationRepair(Protocol):
    async def repair(self, station: int, cause: str) -> bool: ...


@dataclass(frozen=True)
class WatchdogConfig:
    stations: tuple[int, ...] = (1, 2, 3, 4, 5, 6)
    max_repair_attempts: int = 3
    repair_window_s: float = 3600.0
    repair_backoff_s: float = 0.0
    probe_timeout_s: float = 30.0
    probe_interval_s: float = 30.0


@dataclass
class WatchdogReport:
    probed: int = 0
    healthy: int = 0
    unhealthy: int = 0
    draining: int = 0
    repaired: int = 0
    escalated: int = 0
    errors: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "probed": self.probed,
            "healthy": self.healthy,
            "unhealthy": self.unhealthy,
            "draining": self.draining,
            "repaired": self.repaired,
            "escalated": self.escalated,
            "errors": list(self.errors),
        }


def classify_observation(observation: StationObservation) -> str:
    if observation.auth_failed:
        return AUTH_FAILED
    if not observation.connected:
        return TUNNEL_DOWN
    if not observation.dns_ok:
        return DNS_BROKEN
    if not observation.egress_ip:
        return NO_EGRESS
    if not observation.client_usable:
        if str(observation.reason or "").strip().lower() in _QUOTA_REASONS:
            return QUOTA
        return CLIENT_UNUSABLE
    return HEALTHY


def station_state_for_cause(cause: str) -> str:
    """Map a watchdog cause to the persisted lifecycle state."""

    if cause == HEALTHY:
        return "USABLE"
    if cause == QUOTA:
        return "DRAINING"
    return "REPAIRING"


class WatchdogService:
    def __init__(
        self,
        store: QualificationStore,
        probe: StationProbe | None = None,
        repair: StationRepair | None = None,
        *,
        config: WatchdogConfig | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.probe = probe
        self.repair = repair
        self.config = config or WatchdogConfig()
        self._clock = clock

    async def run_once(self, *, now: float | None = None) -> WatchdogReport:
        ts = float(self._clock() if now is None else now)
        report = WatchdogReport()
        if self.probe is None:
            # No probe adapter is a disabled watchdog, not six broken
            # stations.  Never open artificial incidents.
            self.store.touch_watchdog(status="disabled", now=ts)
            return report
        self.store.touch_watchdog(status="running", now=ts)
        stations = self.config.stations or tuple(
            int(item["station"]) for item in self.store.list_station_states()
        )
        for station in stations:
            report.probed += 1
            observation = await self._observe(int(station))
            cause = classify_observation(observation)
            healthy = cause == HEALTHY
            self.store.upsert_station_state(
                int(station),
                state=station_state_for_cause(cause),
                container=observation.container,
                transport=observation.transport,
                country=observation.country,
                current_ip=observation.egress_ip,
                current_host=observation.hostname,
                client_usable=healthy and observation.client_usable,
                last_probe_at=ts,
                last_200_at=ts if observation.client_usable else None,
                reason=observation.error or cause,
                now=ts,
            )
            if healthy:
                report.healthy += 1
                self.store.close_station_incidents(int(station), remediation="auto-cleared", now=ts)
                continue
            if cause == QUOTA:
                # A quota-drained station is not broken: it keeps its tunnel
                # and will rotate.  Do not open an incident or a repair job.
                report.draining += 1
                self.store.close_station_incidents(
                    int(station),
                    remediation="quota-draining",
                    now=ts,
                )
                continue
            report.unhealthy += 1
            incident_id = self.store.open_incident(
                int(station),
                cause,
                details=observation.error or cause,
                remediation="watchdog",
                now=ts,
            )
            if self.repair is None:
                # Keep the incident open for the operator/dashboard; a probe
                # without a repair adapter must not look auto-cleared.
                continue
            attempts = self.store.repair_attempts(
                int(station),
                cause,
                since=ts - max(60.0, float(self.config.repair_window_s)),
            )
            if attempts >= max(1, int(self.config.max_repair_attempts)):
                self.store.close_incident(incident_id, remediation="escalated", now=ts)
                report.escalated += 1
                continue
            if self.config.repair_backoff_s > 0:
                last = self.store.last_repair_at(int(station), cause)
                if last is not None and ts - last < float(self.config.repair_backoff_s):
                    continue
            job_id = self.store.record_repair_job(
                int(station),
                cause,
                result="running",
                last_error=observation.error,
                now=ts,
            )
            try:
                ok = bool(await self.repair.repair(int(station), cause))
            except Exception as exc:  # repair failure is bounded, never fatal
                ok = False
                observation = StationObservation(
                    station=observation.station,
                    error=f"{type(exc).__name__}: {exc}",
                )
            self.store.finish_repair_job(
                job_id,
                result="succeeded" if ok else "failed",
                last_error=observation.error,
                now=self._clock(),
            )
            if ok:
                report.repaired += 1
                self.store.close_incident(incident_id, remediation="repair-ok", now=self._clock())
            else:
                report.errors.append(f"station-{station}:{cause}")
        self.store.touch_watchdog(
            status="partial" if report.errors else "ok",
            error="; ".join(report.errors[:3]),
            now=self._clock(),
        )
        _LOG.info(
            "qualification.watchdog.run",
            extra={
                "probed": report.probed,
                "healthy": report.healthy,
                "unhealthy": report.unhealthy,
                "repaired": report.repaired,
                "escalated": report.escalated,
                "errors": len(report.errors),
            },
        )
        return report

    def is_stale(self, *, max_age_s: float = 90.0, now: float | None = None) -> bool:
        return self.store.watchdog_stale(max_age_s=max_age_s, now=now)

    async def _observe(self, station: int) -> StationObservation:
        if self.probe is None:
            return StationObservation(station=station, error="no-probe-adapter")
        try:
            observation = await self.probe.probe(station)
        except Exception as exc:
            return StationObservation(
                station=station,
                error=f"{type(exc).__name__}: {exc}",
            )
        if not isinstance(observation, StationObservation):
            return StationObservation(station=station, error="invalid-observation")
        return observation


__all__ = [
    "AUTH_FAILED",
    "CLIENT_UNUSABLE",
    "DNS_BROKEN",
    "HEALTHY",
    "NO_EGRESS",
    "PROBE_ERROR",
    "QUOTA",
    "StationObservation",
    "StationProbe",
    "StationRepair",
    "TUNNEL_DOWN",
    "WatchdogConfig",
    "WatchdogReport",
    "WatchdogService",
    "classify_observation",
]
