"""Ports (Protocols) for the IP registry and allocator.

Adapters implement these interfaces.  The application layer depends only on
these Protocols, never on a concrete database, VPN provider or HTTP client.
"""

from __future__ import annotations

from typing import Protocol

from .domain import (
    BindResult,
    GeoFilter,
    IpRecord,
    Lease,
    NeedReason,
    ProbeResult,
    RuntimeResult,
    Server,
)


class ServerCatalog(Protocol):
    def snapshot(self) -> tuple[Server, ...]: ...

    async def refresh(self) -> tuple[Server, ...]: ...


class IpRegistry(Protocol):
    def eligible(
        self,
        *,
        geo: GeoFilter | None,
        now: float,
        limit: int = 50,
    ) -> list[IpRecord]: ...

    def claim(self, egress_ip: str, station: int, ttl_s: float, now: float) -> Lease | None: ...

    def record(self, lease: Lease, result: ProbeResult) -> None: ...

    def assign(self, lease: Lease, now: float) -> IpRecord | None: ...

    def release(self, lease: Lease, reason: str, now: float) -> None: ...

    def reconcile(self, now: float) -> dict[str, int]: ...


class StationBinder(Protocol):
    async def assign(self, station: int, hostname: str) -> BindResult: ...

    async def release(self, station: int) -> None: ...

    async def current(self, station: int) -> str | None: ...


class RuntimeQuotaObserver(Protocol):
    """Receives 429/errors observed while an assigned IP is serving."""

    def record_runtime(self, result: RuntimeResult) -> None: ...


class ProbeClient(Protocol):
    async def probe(self, station: int, model: str) -> ProbeResult: ...


class StationHealth(Protocol):
    def needs_ip(self, station: int, now: float) -> NeedReason | None: ...

    def is_healthy(self, station: int, now: float) -> bool: ...
