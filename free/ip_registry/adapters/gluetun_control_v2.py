"""Rotation binder backed by the 016 control plane client.

The binder is deliberately thin: it forwards the ordered candidate list to the
fork and maps the terminal job to :class:`BindResult`.  It never calls
``docker exec``/``docker cp`` and never re-scores a candidate.  The legacy
:class:`~free.ip_registry.adapters.gluetun_control.GluetunControlBinder` stays
available as the cold fallback when the control plane is unreachable.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from free.control_plane.client import GluetunControlClient
from free.control_plane.errors import (
    ActiveJob,
    ControlPlaneError,
    ControlPlaneUnavailable,
    UnknownTarget,
)
from free.control_plane.models import (
    Constraints,
    ServerCandidate,
    SwitchJob,
    TunnelTarget,
)

from ..domain import BindResult


class GluetunControlV2Binder:
    """Async binder that drives ``POST /v1/vpn/switch`` and ``/rotate``."""

    def __init__(
        self,
        client: GluetunControlClient,
        *,
        station: int,
        cycle_id: str,
        transport_policy: str | list[str] | None = None,
        to_thread: Callable[..., Awaitable[object]] = asyncio.to_thread,
    ) -> None:
        self._client = client
        self._station = int(station)
        self._cycle_id = str(cycle_id or "").strip()
        if not self._cycle_id:
            raise ValueError("cycle_id must not be empty")
        self._transport_policy = transport_policy
        self._to_thread = to_thread

    async def assign(
        self,
        station: int,
        hostname: str,
        country: str | None = None,
        *,
        expected_egress_ip: str | None = None,
        idempotency_key: str | None = None,
        **_: object,
    ) -> BindResult:
        hostname = str(hostname or "").strip()
        if not hostname:
            return BindResult(False, hostname, error="empty-hostname")

        target = TunnelTarget(
            hostname=hostname,
            expected_egress_ip=str(expected_egress_ip or ""),
        )
        key = idempotency_key or self._key("switch", station)
        try:
            job = await self._to_thread(
                self._client.switch,
                target,
                Constraints(),
                idempotency_key=key,
                wait=True,
                transport_policy=self._transport_policy,
            )
        except ControlPlaneError as exc:
            return BindResult(False, hostname, error=self._error_text(exc))
        return self._bind_result(job, hostname)

    async def rotate(
        self,
        candidates: list[ServerCandidate],
        constraints: Constraints | None = None,
        *,
        wait: bool = True,
    ) -> SwitchJob:
        return await self._to_thread(
            self._client.rotate,
            list(candidates),
            constraints or Constraints(),
            idempotency_key=self._key("rotate", self._station),
            wait=wait,
            transport_policy=self._transport_policy,
        )

    async def release(self, station: int) -> None:
        return None

    async def current(self, station: int) -> str | None:
        try:
            verification = await self._to_thread(self._client.egress)
        except ControlPlaneError:
            return None
        return str(verification.ip or "").strip() or None

    def current_transport(self, station: int) -> str:
        try:
            return str(self._client.state().transport or "")
        except ControlPlaneError:
            return ""

    def _bind_result(self, job: SwitchJob, hostname: str) -> BindResult:
        if job.committed:
            ip = job.verification.ip if job.verification else ""
            return BindResult(True, hostname, egress_ip=ip or None)
        return BindResult(
            False,
            hostname,
            error=f"control-{job.state or 'unknown'}:{job.error[:120]}",
        )

    def _key(self, action: str, station: int) -> str:
        return f"{action}-{int(station)}-{self._cycle_id}"

    @staticmethod
    def _error_text(error: ControlPlaneError) -> str:
        if isinstance(error, ControlPlaneUnavailable):
            reason = error.code or "unavailable"
            return f"control-unavailable:{reason}"
        if isinstance(error, UnknownTarget):
            return f"control-invalid-target:{error.code or 'unknown-target'}"
        if isinstance(error, ActiveJob):
            return f"control-active-job:{error.active_job_id}"
        return f"control-error:{error.code or 'error'}:{str(error)[:120]}"


__all__ = ["GluetunControlV2Binder"]
