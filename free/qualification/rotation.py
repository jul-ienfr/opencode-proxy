"""Per-station rotation planning.

The planner is pure policy: it receives a station snapshot (current IP, quota
headroom, standby) and decides whether to keep, promote a standby, rotate, wait
or protect the last usable station.  Candidate selection is a read-only query
over the qualification queue with the unique-IP gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .domain import Candidate, CandidateStatus
from .store import QualificationStore

KEEP = "keep"
PROMOTE_STANDBY = "promote_standby"
ROTATE = "rotate"
WAIT = "wait"
PROTECT_LAST = "protect_last"


@dataclass(frozen=True, slots=True)
class StationSnapshot:
    station: int
    state: str = "serving"
    current_ip: str = ""
    current_hostname: str = ""
    quota_remaining: int | None = None
    quota_estimate: int = 500
    standby_ip: str = ""
    standby_hostname: str = ""
    standby_size: int = 1

    @property
    def headroom_pct(self) -> float:
        if self.quota_remaining is None:
            return 100.0
        estimate = max(1, int(self.quota_estimate))
        return max(0.0, min(100.0, 100.0 * float(self.quota_remaining) / estimate))


@dataclass(frozen=True, slots=True)
class RotationDecision:
    station: int
    action: str
    reason: str
    candidate: Candidate | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "station": self.station,
            "action": self.action,
            "reason": self.reason,
            "candidate": self.candidate.hostname if self.candidate else "",
            "egress_ip": self.candidate.last_egress_ip if self.candidate else "",
        }


class RotationPlanner:
    def __init__(
        self,
        store: QualificationStore,
        *,
        headroom_threshold_pct: float = 20.0,
        last_station_protection: bool = True,
    ) -> None:
        self.store = store
        self.headroom_threshold_pct = max(0.0, min(100.0, float(headroom_threshold_pct)))
        self.last_station_protection = bool(last_station_protection)

    def plan(
        self,
        snapshot: StationSnapshot,
        *,
        usable_stations: int = 6,
        exclude_ips: set[str] | None = None,
        now: float | None = None,
    ) -> RotationDecision:
        state = str(snapshot.state or "serving").lower()
        if state in ("paused", "repairing"):
            return RotationDecision(snapshot.station, WAIT, f"station-{state}")

        if not snapshot.current_ip:
            candidate = self.select_candidate(
                station=snapshot.station,
                exclude_ips=exclude_ips,
                now=now,
            )
            if candidate is None:
                return RotationDecision(snapshot.station, WAIT, "no-candidate")
            return RotationDecision(snapshot.station, ROTATE, "no-current-ip", candidate)

        if snapshot.headroom_pct > self.headroom_threshold_pct:
            return RotationDecision(snapshot.station, KEEP, "headroom-ok")

        if snapshot.standby_ip and int(snapshot.standby_size or 0) > 0:
            return RotationDecision(snapshot.station, PROMOTE_STANDBY, "standby-ready")

        if self.last_station_protection and int(usable_stations) <= 1:
            return RotationDecision(snapshot.station, PROTECT_LAST, "last-usable-station")

        candidate = self.select_candidate(
            station=snapshot.station,
            exclude_ips=exclude_ips,
            now=now,
        )
        if candidate is None:
            return RotationDecision(snapshot.station, WAIT, "no-candidate")
        return RotationDecision(snapshot.station, ROTATE, "headroom-low", candidate)

    def select_candidate(
        self,
        *,
        station: int,
        exclude_ips: set[str] | None = None,
        now: float | None = None,
    ) -> Candidate | None:
        excluded = {str(ip).strip() for ip in (exclude_ips or set()) if str(ip).strip()}
        for candidate in self.store.top_candidates(100):
            if candidate.hostname in excluded:
                continue
            if candidate.status not in {CandidateStatus.PENDING, CandidateStatus.QUOTA}:
                continue
            if candidate.last_egress_ip and candidate.last_egress_ip in excluded:
                continue
            if candidate.last_egress_ip and self.store.egress_ip_in_use(
                candidate.last_egress_ip,
                station=station,
            ):
                continue
            return candidate
        return None


__all__ = [
    "KEEP",
    "PROMOTE_STANDBY",
    "PROTECT_LAST",
    "ROTATE",
    "RotationDecision",
    "RotationPlanner",
    "StationSnapshot",
    "WAIT",
]
