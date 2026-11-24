"""Protocole commun des adapters providers (011)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SlotObservation:
    """Observation d'un slot (pur, sans I/O — schema partagé par les adaptateurs de slot)."""
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


@runtime_checkable
class SlotProvider(Protocol):
    provider: str

    def specs(self) -> list[Any]: ...
    def observe(self, spec: Any) -> Any: ...
    def create(self, spec: Any) -> None: ...
    def recreate(self, spec: Any, reason: str | None = None) -> None: ...
    def delete(self, spec: Any, reason: str | None = None) -> None: ...
    def rotate(self, spec: Any, trigger: str) -> dict[str, Any]: ...
    def probe(self, spec: Any) -> Any: ...
    def persist(self, spec: Any, observation: Any) -> None: ...
