"""Noyau de cycle de vie commun NordVPN / WARP (feature 011).

Ce module est pur : aucune I/O, aucun Docker, aucun réseau. Il définit les
objets partagés et les décisions. Les mécanismes spécifiques vivent dans
``ops.providers.*``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class SlotState(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    TUNNEL_UP = "tunnel_up"
    DNS_OK = "dns_ok"
    EGRESS_OK = "egress_ok"
    QUOTA_OK = "quota_ok"
    CLIENT_USABLE = "client_usable"
    DEGRADED = "degraded"
    REPAIRING = "repairing"
    PARKED = "parked"
    FAILED = "failed"
    DISABLED = "disabled"


@dataclass(frozen=True, slots=True)
class SlotSpec:
    provider: str
    slot_id: int
    generation: int = 1
    enabled: bool = True
    stack: str = ""
    transport: str = ""
    endpoint: str = ""
    container: str = ""
    image: str = ""
    family: str = "ipv4"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider:
            raise ValueError("provider is required")
        if int(self.slot_id) <= 0:
            raise ValueError("slot_id must be > 0")
        if int(self.generation) <= 0:
            raise ValueError("generation must be >= 1")


@dataclass(frozen=True, slots=True)
class SlotObservation:
    container_state: str = "missing"
    health: str = "unknown"
    tunnel_up: bool = False
    dns_ok: bool = False
    egress_ip: str | None = None
    block: str | None = None
    quota_state: str = "unknown"
    reset_at: float | None = None
    client_usable: bool = False
    last_evidence: str | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SlotStatus:
    provider: str
    slot_id: int
    generation: int
    state: str
    client_usable: bool
    egress_ip: str | None = None
    block: str | None = None
    quota_state: str = "unknown"
    reset_at: float | None = None
    last_evidence: str | None = None
    reason: str | None = None
    next_action: str = "none"
    provider_details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "slot_id": self.slot_id,
            "generation": self.generation,
            "state": self.state,
            "client_usable": self.client_usable,
            "egress_ip": self.egress_ip,
            "block": self.block,
            "quota_state": self.quota_state,
            "reset_at": self.reset_at,
            "last_evidence": self.last_evidence,
            "reason": self.reason,
            "next_action": self.next_action,
            "provider_details": dict(self.provider_details),
        }


@dataclass(frozen=True, slots=True)
class SlotDecision:
    provider: str
    slot_id: int
    action: str
    cause: str | None = None
    reason: str | None = None
    evidence: str | None = None


class SlotProvider(Protocol):
    provider: str

    def specs(self) -> list[SlotSpec]: ...
    def observe(self, spec: SlotSpec) -> SlotObservation: ...
    def create(self, spec: SlotSpec) -> None: ...
    def recreate(self, spec: SlotSpec, reason: str | None = None) -> None: ...
    def delete(self, spec: SlotSpec, reason: str | None = None) -> None: ...
    def rotate(self, spec: SlotSpec, trigger: str) -> dict[str, Any]: ...
    def probe(self, spec: SlotSpec) -> SlotObservation: ...
    def persist(self, spec: SlotSpec, observation: SlotObservation) -> None: ...


class LifecycleEngine:
    """Moteur commun : applique la même politique aux providers branchés."""

    def __init__(self, providers: list[Any], policy: Any = None) -> None:
        self.providers = list(providers or [])
        self.policy = policy

    def _provider_name(self, provider: Any) -> str:
        return str(getattr(provider, "provider", "") or getattr(provider, "name", ""))

    def decide_failure(self, cause: str) -> dict[str, SlotDecision]:
        action = "inspect"
        if self.policy is not None:
            action = str(self.policy.action_for(cause))
        decisions: dict[str, SlotDecision] = {}
        for provider in self.providers:
            name = self._provider_name(provider)
            decisions[name] = SlotDecision(
                provider=name,
                slot_id=0,
                action=action,
                cause=cause,
                reason=f"{name}:{cause}",
            )
        return decisions

    def reconcile(self) -> list[SlotDecision]:
        decisions: list[SlotDecision] = []
        for provider in self.providers:
            name = self._provider_name(provider)
            try:
                specs = provider.specs()
            except Exception:
                continue
            for spec in specs:
                try:
                    observation = provider.observe(spec)
                except Exception:
                    continue
                if not observation.client_usable:
                    decisions.append(
                        SlotDecision(
                            provider=name,
                            slot_id=int(spec.slot_id),
                            action="repair",
                            cause=observation.reason,
                            evidence=observation.last_evidence,
                        )
                    )
        return decisions
