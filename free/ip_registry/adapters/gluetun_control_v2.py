"""Rotation binder backed by the 016 control plane client.

The binder is deliberately thin: it forwards the ordered candidate list to the
fork and maps the terminal job to :class:`BindResult`.  It never calls
``docker exec``/``docker cp`` and never re-scores a candidate.  The legacy
:class:`~free.ip_registry.adapters.gluetun_control.GluetunControlBinder` stays
available as the cold fallback when the control plane is unreachable.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from free.control_plane.client import GluetunControlClient, legacy_fallback_reason
from free.control_plane.errors import (
    ActiveJob,
    ControlPlaneError,
    ControlPlaneUnavailable,
    Exhausted,
    UnknownTarget,
)
from free.control_plane.models import (
    Attempt,
    Constraints,
    ServerCandidate,
    SwitchJob,
    TunnelTarget,
)

from ..domain import BindResult


@dataclass(frozen=True, slots=True)
class RotationOutcome:
    """Terminal view of ``POST /v1/vpn/rotate``.

    ``exhausted`` and ``rolled_back`` are ordinary outcomes, never exceptions
    the caller must translate into a station disable: the station stays
    repairable by the next cycle.
    """

    station: int
    state: str
    ok: bool = False
    exhausted: bool = False
    rolled_back: bool = False
    hostname: str = ""
    egress_ip: str | None = None
    attempts: tuple[Attempt, ...] = ()
    error: str = ""
    job: SwitchJob | None = field(default=None, repr=False)


class GluetunControlV2Binder:
    """Async binder that drives ``POST /v1/vpn/switch`` and ``/rotate``."""

    # The hybrid binder uses this marker to give the control plane the first
    # attempt and to collapse the per-transport chain loop into one call.
    is_control_plane = True

    def __init__(
        self,
        client: GluetunControlClient,
        *,
        station: int,
        cycle_id: str,
        transport_policy: str | list[str] | None = None,
        legacy: object | None = None,
        on_fallback: Callable[[str], None] | None = None,
        to_thread: Callable[..., Awaitable[object]] = asyncio.to_thread,
    ) -> None:
        self._client = client
        self._station = int(station)
        self._cycle_id = str(cycle_id or "").strip()
        if not self._cycle_id:
            raise ValueError("cycle_id must not be empty")
        self._transport_policy = transport_policy
        self._legacy = legacy
        self._on_fallback = on_fallback
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
        key = idempotency_key or self._key("switch", station, hostname)
        try:
            job = await self._to_thread(
                self._client.switch,
                target,
                Constraints(),
                idempotency_key=key,
                wait=True,
                transport_policy=self._transport_policy,
            )
        except ActiveJob as exc:
            return await self._wait_active(exc, hostname)
        except ControlPlaneError as exc:
            return await self._fallback_or_error(exc, station, hostname, country)
        return self._bind_result(job, hostname)

    async def _wait_active(self, error: ActiveJob, hostname: str) -> BindResult:
        busy = f"control-busy:{error.active_job_id or 'unknown'}"
        try:
            job = await self._to_thread(
                self._client.wait_for_active_job,
                error,
            )
        except ControlPlaneError:
            return BindResult(False, hostname, error=busy)
        if not job.terminal:
            return BindResult(False, hostname, error=busy)
        return self._bind_result(job, hostname)

    async def _fallback_or_error(
        self,
        error: ControlPlaneError,
        station: int,
        hostname: str,
        country: str | None,
    ) -> BindResult:
        # A broken station often cannot resolve a new hostname because its
        # DNS is down. The fork cannot fix that from inside the dead tunnel;
        # recreating the container lets the runtime ladder resolve through the
        # provider catalogue instead. The hybrid binder sees the
        # control-unavailable prefix and falls back to Compose.
        if "resolution" in str(error).lower():
            return BindResult(
                False, hostname,
                error="control-unavailable:resolution-failed",
            )

        reason = legacy_fallback_reason(error)
        if not reason:
            return BindResult(False, hostname, error=self._error_text(error))

        active_id = await self._active_job_id()
        if active_id:
            active = ActiveJob(
                "an active job already owns the tunnel",
                active_job_id=active_id,
            )
            return await self._wait_active(active, hostname)

        if self._legacy is None:
            return BindResult(False, hostname, error=self._error_text(error))

        self._trace(reason)
        assign = getattr(self._legacy, "assign", None)
        if assign is None:
            return BindResult(False, hostname, error=self._error_text(error))
        return await assign(station, hostname, country=country)

    async def _active_job_id(self) -> str:
        try:
            state = await self._to_thread(self._client.state)
        except ControlPlaneError:
            return ""
        return str(getattr(state, "active_job_id", "") or "").strip()

    def _trace(self, reason: str) -> None:
        if self._on_fallback is None:
            return
        try:
            self._on_fallback(reason)
        except Exception:
            pass

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
            idempotency_key=self._rotate_key(candidates),
            wait=wait,
            transport_policy=self._transport_policy,
        )

    async def rotate_outcome(
        self,
        candidates: list[ServerCandidate],
        constraints: Constraints | None = None,
        *,
        wait: bool = True,
    ) -> RotationOutcome:
        """Rotate and normalize the terminal state without touching the registry."""

        ordered = [candidate for candidate in candidates if str(candidate.hostname or "").strip()]
        if not ordered:
            return RotationOutcome(
                station=self._station,
                state="rejected",
                error="no-candidates",
            )
        try:
            job = await self.rotate(ordered, constraints, wait=wait)
        except ActiveJob as exc:
            try:
                job = await self._to_thread(
                    self._client.wait_for_active_job, exc
                )
            except ControlPlaneError as wait_exc:
                return RotationOutcome(
                    station=self._station,
                    state="error",
                    error=self._error_text(wait_exc),
                )
        except Exhausted as exc:
            return RotationOutcome(
                station=self._station,
                state="exhausted",
                exhausted=True,
                attempts=tuple(exc.attempts),
                error=str(exc)[:160],
            )
        except ControlPlaneError as exc:
            return RotationOutcome(
                station=self._station,
                state="error",
                error=self._error_text(exc),
            )

        state = job.state or "unknown"
        outcome = RotationOutcome(
            station=self._station,
            state=state,
            attempts=job.attempts,
            error=job.error[:160],
            job=job,
        )
        if job.committed:
            return RotationOutcome(
                station=self._station,
                state=state,
                ok=True,
                hostname=job.target_hostname or ordered[0].hostname,
                egress_ip=(job.verification.ip if job.verification else None) or None,
                attempts=job.attempts,
                job=job,
            )
        if state == "exhausted":
            return RotationOutcome(
                station=self._station,
                state=state,
                exhausted=True,
                attempts=job.attempts,
                error=outcome.error,
                job=job,
            )
        if state == "rolled_back":
            return RotationOutcome(
                station=self._station,
                state=state,
                rolled_back=True,
                attempts=job.attempts,
                error=outcome.error,
                job=job,
            )
        return outcome

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
        if job.state == "noop":
            # The target is already the active tunnel; this is success, not a
            # rejection. The chain then probes the model normally.
            return BindResult(True, hostname)
        if job.committed:
            ip = job.verification.ip if job.verification else ""
            return BindResult(True, hostname, egress_ip=ip or None)
        error_text = str(job.error or "").lower()
        if "resolution" in error_text:
            # Compose fallback: a fresh container resolves the target from the
            # provider catalogue, without depending on the dead tunnel's DNS.
            return BindResult(
                False, hostname,
                error="control-unavailable:resolution-failed",
            )
        if job.state in ("failed_degraded", "rolled_back") and (
            "crashed" in error_text or "awaiting tunnel" in error_text
        ):
            # The in-place switch cannot repair a station whose VPN loop is
            # crashed or whose ladder needs a fresh process. Recreate it with
            # the current image and let the runtime ladder own the transports.
            return BindResult(
                False, hostname,
                error="control-unavailable:station-degraded",
            )
        return BindResult(
            False,
            hostname,
            error=f"control-{job.state or 'unknown'}:{job.error[:120]}",
        )

    def _key(self, action: str, station: int, target: str = "") -> str:
        # The key must be stable for one request and different for another
        # target: reusing a chain-wide key made the fork answer
        # ``idempotency_conflict`` on every hostname after the first.
        suffix = f"-{target}" if target else ""
        return f"{action}-{int(station)}-{self._cycle_id}{suffix}"

    def _rotate_key(self, candidates: list[ServerCandidate]) -> str:
        hostnames = ",".join(
            str(candidate.hostname or "") for candidate in candidates
        )
        digest = hashlib.sha256(hostnames.encode("utf-8")).hexdigest()[:12]
        return f"rotate-{self._station}-{self._cycle_id}-{digest}"

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


__all__ = ["GluetunControlV2Binder", "RotationOutcome"]
