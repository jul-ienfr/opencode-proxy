"""Pure domain objects for the IP registry and allocator.

No I/O, no provider import, no database import.  Everything here is testable
in isolation and safe to use from any adapter.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum


class IpState(StrEnum):
    """Lifecycle of an observed egress IP."""

    FRESH = "fresh"
    CANDIDATE = "candidate"
    CLAIMED = "claimed"
    ASSIGNED = "assigned"
    EXHAUSTED = "exhausted"
    ERROR = "error"
    RETIRED = "retired"


class NeedReason(StrEnum):
    """Why a station needs a new IP."""

    NO_IP = "no_ip"
    DISCONNECTED = "disconnected"
    UNHEALTHY = "unhealthy"
    QUOTA_429 = "quota_429"
    ROTATION = "rotation"
    CYCLE = "cycle"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class Server:
    """One NordVPN server from the catalog (not an observed egress).

    ``source_id`` names the registry that supplied the row and ``stale``
    marks data that did not come from a fresh primary snapshot (fallback
    source, supplementary static catalog or failed refresh).  Both travel
    with the row so the SQLite ``servers`` table keeps provenance instead of
    silently flattening every source into one list.
    """

    hostname: str
    country: str
    server_ip: str | None = None
    city: str | None = None
    load: float = 0.0
    technologies: tuple[str, ...] = ()
    source_id: str = ""
    stale: bool = False
    catalog_fetched_at: float | None = None


@dataclass(frozen=True, slots=True)
class GeoFilter:
    """Optional geography constraint.  Empty countries == no constraint."""

    countries: frozenset[str] = field(default_factory=frozenset)
    require: bool = False

    def allows(self, country: str) -> bool:
        if not self.require or not self.countries:
            return True
        return country in self.countries


@dataclass(frozen=True, slots=True)
class IpRecord:
    """Materialized state for one observed egress IP.

    ``egress_ip`` is the public IP observed upstream (the quota key).
    ``hostname`` is the NordVPN server to reconnect to (the binder key).
    """

    egress_ip: str
    hostname: str
    country: str
    state: IpState = IpState.FRESH
    score: float = 50.0
    last_status: int | None = None
    last_tested_at: float | None = None
    last_ok_at: float | None = None
    reset_at: float | None = None
    error_until: float | None = None
    latency_ms: float | None = None
    tests: int = 0
    successes: int = 0
    failures: int = 0
    assigned_station: int | None = None
    assigned_at: float | None = None
    lease_id: str | None = None
    lease_expires_at: float | None = None
    version: int = 0

    def is_leased(self, now: float) -> bool:
        return bool(self.lease_id) and self.lease_expires_at is not None and now < self.lease_expires_at

    def is_exhausted(self, now: float) -> bool:
        return self.reset_at is not None and now < self.reset_at

    def is_in_error_cooldown(self, now: float) -> bool:
        return self.error_until is not None and now < self.error_until

    def is_assigned(self) -> bool:
        return self.assigned_station is not None

    def with_state(self, state: IpState, *, version: int | None = None) -> IpRecord:
        return replace(
            self,
            state=state,
            version=(self.version + 1 if version is None else version),
        )


@dataclass(frozen=True, slots=True)
class Lease:
    lease_id: str
    egress_ip: str
    hostname: str
    station: int
    expires_at: float

    def is_valid(self, now: float) -> bool:
        return now < self.expires_at


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Result of one official free probe."""

    status: int
    tested_at: float
    retry_after_s: float = 0.0
    latency_ms: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == 200

    @property
    def is_quota_429(self) -> bool:
        return self.status == 429 and self.retry_after_s > 0


@dataclass(frozen=True, slots=True)
class RuntimeResult:
    """A 429/error observed while an assigned IP is serving a request."""

    request_id: str
    station: int
    hostname: str
    egress_ip: str
    observed_at: float
    status: int
    retry_after_s: float = 0.0
    error_type: str | None = None
    content_started: bool = False

    @property
    def is_quota_429(self) -> bool:
        return self.status == 429 and self.retry_after_s > 0


@dataclass(frozen=True, slots=True)
class BindResult:
    """Result of binding a station to a server hostname."""

    ok: bool
    hostname: str
    egress_ip: str | None = None
    error: str | None = None
    mismatch: bool = False


_ALLOWED_TRANSITIONS: dict[IpState, frozenset[IpState]] = {
    IpState.FRESH: frozenset({IpState.CANDIDATE, IpState.CLAIMED, IpState.ERROR, IpState.RETIRED}),
    IpState.CANDIDATE: frozenset({IpState.CLAIMED, IpState.EXHAUSTED, IpState.ERROR, IpState.RETIRED}),
    IpState.CLAIMED: frozenset({IpState.ASSIGNED, IpState.CANDIDATE, IpState.EXHAUSTED, IpState.ERROR}),
    IpState.ASSIGNED: frozenset({IpState.CANDIDATE, IpState.EXHAUSTED, IpState.ERROR, IpState.RETIRED}),
    IpState.EXHAUSTED: frozenset({IpState.CANDIDATE, IpState.RETIRED}),
    IpState.ERROR: frozenset({IpState.CANDIDATE, IpState.RETIRED}),
    IpState.RETIRED: frozenset(),
}


def can_transition(
    source: IpState,
    target: IpState,
    *,
    now: float,
    record: IpRecord | None = None,
) -> bool:
    """Validate a lifecycle transition, including time-based guards.

    ``EXHAUSTED -> CANDIDATE`` is allowed only once ``reset_at`` is reached.
    ``ERROR -> CANDIDATE`` is allowed only once ``error_until`` is reached.
    """

    if target not in _ALLOWED_TRANSITIONS.get(source, frozenset()):
        return False
    if source is IpState.EXHAUSTED and target is IpState.CANDIDATE:
        return record is not None and record.reset_at is not None and now >= record.reset_at
    if source is IpState.ERROR and target is IpState.CANDIDATE:
        return record is not None and record.error_until is not None and now >= record.error_until
    return True
