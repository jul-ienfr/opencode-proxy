"""Typed views over the Gluetun control plane JSON contract.

The models keep the raw payload next to the fields the orchestrator consumes,
so a fork that adds a field never breaks the rotation path and an unexpected
shape stays inspectable in telemetry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .errors import Exhausted, VerificationFailed


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True, slots=True)
class Capabilities:
    control_plane_version: str = ""
    upstream_version: str = ""
    station_id: str = ""
    features: dict[str, Any] = field(default_factory=dict)
    limits: dict[str, Any] = field(default_factory=dict)
    state_mode: str = ""
    dns_default_ladder: tuple[str, ...] = ()
    transport_default_ladder: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Capabilities:
        return cls(
            control_plane_version=str(payload.get("control_plane_version", "")),
            upstream_version=str(payload.get("upstream_version", "")),
            station_id=str(payload.get("station_id", "")),
            features=_as_dict(payload.get("features")),
            limits=_as_dict(payload.get("limits")),
            state_mode=str(payload.get("state_mode", "")),
            dns_default_ladder=tuple(str(item) for item in _as_list(payload.get("dns_default_ladder"))),
            transport_default_ladder=tuple(str(item) for item in _as_list(payload.get("transport_default_ladder"))),
            raw=dict(payload),
        )

    def supports(self, feature: str) -> bool:
        return bool(self.features.get(feature, False))


@dataclass(frozen=True, slots=True)
class TunnelTarget:
    hostname: str
    endpoint_ip: str = ""
    expected_egress_ip: str = ""

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"hostname": self.hostname}
        if self.endpoint_ip:
            payload["endpoint_ip"] = self.endpoint_ip
        if self.expected_egress_ip:
            payload["expected_egress_ip"] = self.expected_egress_ip
        return payload


@dataclass(frozen=True, slots=True)
class ServerCandidate:
    hostname: str
    endpoint_ip: str = ""
    expected_egress_ip: str = ""

    def to_json(self) -> dict[str, Any]:
        return TunnelTarget(
            hostname=self.hostname,
            endpoint_ip=self.endpoint_ip,
            expected_egress_ip=self.expected_egress_ip,
        ).to_json()


@dataclass(frozen=True, slots=True)
class Constraints:
    require_new_ip: bool = False
    require_new_subnet: bool = False
    exclude_hostnames: tuple[str, ...] = ()
    exclude_ips: tuple[str, ...] = ()
    exclude_subnets: tuple[str, ...] = ()
    forbidden_ips: tuple[str, ...] = ()
    max_candidates: int = 0
    max_attempts: int = 0

    def to_switch_json(self) -> dict[str, Any]:
        return {
            "require_new_ip": self.require_new_ip,
            "require_new_subnet": self.require_new_subnet,
            "exclude_ips": list(self.exclude_ips),
            "exclude_subnets": list(self.exclude_subnets),
            "forbidden_ips": list(self.forbidden_ips),
        }

    def to_rotate_json(self) -> dict[str, Any]:
        payload = self.to_switch_json()
        payload["exclude_hostnames"] = list(self.exclude_hostnames)
        if self.max_candidates > 0:
            payload["max_candidates"] = self.max_candidates
        if self.max_attempts > 0:
            payload["max_attempts"] = self.max_attempts
        return payload


@dataclass(frozen=True, slots=True)
class Attempt:
    hostname: str = ""
    phase: str = ""
    result: str = ""
    reason: str = ""
    observed_ip: str = ""
    expected_ip: str = ""
    started_at: str = ""
    finished_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> Attempt:
        return cls(
            hostname=str(payload.get("hostname", "")),
            phase=str(payload.get("phase", "")),
            result=str(payload.get("result", "")),
            reason=str(payload.get("reason", "")),
            observed_ip=str(payload.get("observed_ip", "")),
            expected_ip=str(payload.get("expected_ip", "")),
            started_at=str(payload.get("started_at", "")),
            finished_at=str(payload.get("finished_at", "")),
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class EgressVerification:
    verified: bool = False
    ip: str = ""
    reason: str = ""
    quorum: int = 0
    source_count: int = 0
    confidence: str = ""
    dissenters: tuple[str, ...] = ()
    cached: bool = False
    verified_at: str = ""
    ttl_s: int = 0
    canary_ok: bool | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: dict[str, Any] | None) -> EgressVerification | None:
        if not isinstance(payload, dict):
            return None
        canary = payload.get("canary_ok")
        return cls(
            verified=bool(payload.get("verified", False)),
            ip=str(payload.get("ip", "") or ""),
            reason=str(payload.get("reason", "") or ""),
            quorum=_as_int(payload.get("quorum")),
            source_count=_as_int(payload.get("source_count")),
            confidence=str(payload.get("confidence", "") or ""),
            dissenters=tuple(str(item) for item in _as_list(payload.get("dissenters"))),
            cached=bool(payload.get("cached", False)),
            verified_at=str(payload.get("verified_at", "") or ""),
            ttl_s=_as_int(payload.get("ttl_s")),
            canary_ok=bool(canary) if isinstance(canary, bool) else None,
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class SwitchJob:
    id: str = ""
    kind: str = ""
    state: str = ""
    station_id: str = ""
    error: str = ""
    target_hostname: str = ""
    attempts: tuple[Attempt, ...] = ()
    verification: EgressVerification | None = None
    finished_at: str = ""
    created_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        return self.state in {
            "committed",
            "rejected",
            "rolled_back",
            "failed_degraded",
            "exhausted",
            "cancelled",
            "interrupted",
        }

    @property
    def committed(self) -> bool:
        return self.state == "committed"

    def raise_for_status(self) -> None:
        """Raise the terminal failure so callers never treat it as success."""

        if self.committed or self.state in ("planned", "noop"):
            return
        if self.state == "exhausted":
            raise Exhausted(
                f"rotation exhausted after {len(self.attempts)} attempts",
                attempts=list(self.attempts),
                job=self,
            )
        if not self.terminal:
            raise VerificationFailed(
                f"job {self.id} is still {self.state}",
                details={"job": self.raw},
            )
        raise VerificationFailed(
            self.error or f"job {self.id} ended in {self.state}",
            verification=self.verification,
            details={"job": self.raw},
        )

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> SwitchJob:
        target = _as_dict(payload.get("target"))
        return cls(
            id=str(payload.get("id", "")),
            kind=str(payload.get("kind", "")),
            state=str(payload.get("state", "")),
            station_id=str(payload.get("station_id", "")),
            error=str(payload.get("error", "")),
            target_hostname=str(target.get("hostname", "")),
            attempts=tuple(
                Attempt.from_json(item) for item in _as_list(payload.get("attempts")) if isinstance(item, dict)
            ),
            verification=EgressVerification.from_json(payload.get("verification")),
            finished_at=str(payload.get("finished_at", "") or ""),
            created_at=str(payload.get("created_at", "") or ""),
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class TunnelState:
    station_id: str = ""
    vpn_status: str = ""
    transport: str = ""
    server: dict[str, Any] = field(default_factory=dict)
    endpoint_ip: str = ""
    egress: EgressVerification | None = None
    dns: dict[str, Any] = field(default_factory=dict)
    desired: dict[str, Any] = field(default_factory=dict)
    last_committed: dict[str, Any] = field(default_factory=dict)
    active_job_id: str = ""
    state_mode: str = ""
    updated_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> TunnelState:
        return cls(
            station_id=str(payload.get("station_id", "")),
            vpn_status=str(payload.get("vpn_status", "")),
            transport=str(payload.get("transport", "")),
            server=_as_dict(payload.get("server")),
            endpoint_ip=str(payload.get("endpoint_ip", "") or ""),
            egress=EgressVerification.from_json(payload.get("egress")),
            dns=_as_dict(payload.get("dns")),
            desired=_as_dict(payload.get("desired")),
            last_committed=_as_dict(payload.get("last_committed")),
            active_job_id=str(payload.get("active_job_id", "") or ""),
            state_mode=str(payload.get("state_mode", "")),
            updated_at=str(payload.get("updated_at", "") or ""),
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class DNSState:
    ladder: tuple[str, ...] = ()
    active_tier: str = ""
    fail_closed: bool = False
    tiers: tuple[dict[str, Any], ...] = ()
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> DNSState:
        return cls(
            ladder=tuple(str(item) for item in _as_list(payload.get("ladder"))),
            active_tier=str(payload.get("active_tier", "") or ""),
            fail_closed=bool(payload.get("fail_closed", False)),
            tiers=tuple(_as_dict(item) for item in _as_list(payload.get("tiers"))),
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class TransportState:
    active_transport: str = ""
    ladder: tuple[str, ...] = ()
    phase: str = ""
    cycle: int = 0
    breaker: dict[str, Any] = field(default_factory=dict)
    attempts: tuple[dict[str, Any], ...] = ()
    next_attempt_at: str = ""
    updated_at: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> TransportState:
        policy = _as_dict(payload.get("policy"))
        active = payload.get("active_transport")
        return cls(
            active_transport=str(active) if active else "",
            ladder=tuple(str(item) for item in _as_list(policy.get("ladder"))),
            phase=str(payload.get("phase", "")),
            cycle=_as_int(payload.get("cycle")),
            breaker=_as_dict(payload.get("breaker")),
            attempts=tuple(_as_dict(item) for item in _as_list(payload.get("attempts"))),
            next_attempt_at=str(payload.get("next_attempt_at", "") or ""),
            updated_at=str(payload.get("updated_at", "") or ""),
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class ControlEvent:
    seq: int = 0
    ts: str = ""
    type: str = ""
    severity: str = ""
    job_id: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ControlEvent:
        return cls(
            seq=_as_int(payload.get("seq")),
            ts=str(payload.get("ts", "") or ""),
            type=str(payload.get("type", "")),
            severity=str(payload.get("severity", "")),
            job_id=str(payload.get("job_id", "") or ""),
            data=_as_dict(payload.get("data")),
            raw=dict(payload),
        )


@dataclass(frozen=True, slots=True)
class EventsPage:
    items: tuple[ControlEvent, ...] = ()
    next_seq: int = 0
    gap: bool = False

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> EventsPage:
        return cls(
            items=tuple(
                ControlEvent.from_json(item) for item in _as_list(payload.get("items")) if isinstance(item, dict)
            ),
            next_seq=_as_int(payload.get("next_seq")),
            gap=bool(payload.get("gap", False)),
        )
