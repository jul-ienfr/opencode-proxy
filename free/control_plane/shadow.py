"""Shadow mode: observe the fork without ever cutting the tunnel.

In ``control_plane.mode=shadow`` the legacy rotation stays authoritative.  For
each legacy decision this observer sends a ``dry_run=true`` switch (the fork
returns a plan and never touches the tunnel), then compares the fork's view of
the final IP/server with the IP the legacy path actually produced.  The result
is written as a ``control_plane_shadow`` row in ``telemetry.db``.

The observer is intentionally non-authoritative: every control plane error is
captured in the row and never propagates, so a broken fork can never break the
stations.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .client import GluetunControlClient
from .errors import ControlPlaneError
from .models import Constraints, TransportState, TunnelTarget


class ShadowRecorder(Protocol):
    def record(self, entry: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class ShadowPlan:
    target: str = ""
    would_cut_tunnel: bool = False
    plan: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def observed(self) -> bool:
        return not self.error


@dataclass(frozen=True, slots=True)
class ShadowComparison:
    station: str
    legacy_target: str
    shadow_target: str
    legacy_ip: str
    shadow_ip: str
    match: bool
    divergence_reason: str
    would_cut_tunnel: bool = False
    warnings: tuple[str, ...] = ()
    error: str = ""

    def as_entry(self, *, ts: float) -> dict[str, Any]:
        return {
            "ts": ts,
            "station": self.station,
            "legacy_target": self.legacy_target,
            "shadow_target": self.shadow_target,
            "legacy_ip": self.legacy_ip,
            "shadow_ip": self.shadow_ip,
            "match": self.match,
            "divergence_reason": self.divergence_reason,
            "would_cut_tunnel": self.would_cut_tunnel,
            "warnings": list(self.warnings),
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class TransportShadowComparison:
    station: str
    legacy_transport: str
    fork_transport: str
    match: bool
    legacy_reason: str = ""
    fork_reason: str = ""
    error: str = ""

    def as_entry(self, *, ts: float) -> dict[str, Any]:
        return {
            "ts": ts,
            "station": self.station,
            "legacy_transport": self.legacy_transport,
            "fork_transport": self.fork_transport,
            "match": self.match,
            "legacy_reason": self.legacy_reason,
            "fork_reason": self.fork_reason,
            "error": self.error,
        }


class TransportShadowObserver:
    """Observe the fork transport without ever driving it.

    Shadow mode forbids sending a ``transport_policy`` or touching
    ``PUT /v1/vpn/settings``.  This observer only reads
    ``GET /v1/vpn/transport`` after a legacy decision and records whether the
    fork landed on the same transport.
    """

    def __init__(
        self,
        client: GluetunControlClient,
        *,
        station: int | str,
        recorder: ShadowRecorder | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._station = str(station)
        self._recorder = recorder
        self._now = now

    def observe(
        self,
        *,
        legacy_transport: str,
        legacy_reason: str = "",
    ) -> TransportShadowComparison:
        legacy = str(legacy_transport or "").strip().lower()
        fork = ""
        fork_reason = ""
        error = ""
        try:
            state = self._client.transport_state()
            fork = _transport_name(state)
            fork_reason = _transport_reason(state)
        except ControlPlaneError as exc:
            error = self._error_text(exc)

        match = bool(legacy) and not error and legacy == fork
        if not match and not error:
            fork_reason = fork_reason or "transport-divergence"
        comparison = TransportShadowComparison(
            station=self._station,
            legacy_transport=legacy,
            fork_transport=fork,
            match=match,
            legacy_reason=str(legacy_reason or ""),
            fork_reason=fork_reason,
            error=error,
        )
        self._record(comparison)
        return comparison

    def _record(self, comparison: TransportShadowComparison) -> None:
        if self._recorder is None:
            return
        record_transport = getattr(self._recorder, "record_transport", None)
        if record_transport is None:
            return
        try:
            record_transport(comparison.as_entry(ts=self._now()))
        except Exception:
            # Telemetry must never break the legacy transport path.
            return

    @staticmethod
    def _error_text(error: ControlPlaneError) -> str:
        code = getattr(error, "code", "") or type(error).__name__
        status = getattr(error, "status", None)
        suffix = f":{status}" if status else ""
        return f"{code}{suffix}"


def _transport_name(state: TransportState) -> str:
    return str(state.active_transport or "").strip().lower()


def _transport_reason(state: TransportState) -> str:
    breaker = state.breaker if isinstance(state.breaker, dict) else {}
    breaker_state = str(breaker.get("state", "") or "").strip()
    phase = str(state.phase or "").strip()
    if breaker_state:
        return f"breaker:{breaker_state}"
    return phase


class ShadowObserver:
    """Non-blocking observer used only when ``mode=shadow``."""

    def __init__(
        self,
        client: GluetunControlClient,
        *,
        station: int | str,
        cycle_id: str,
        recorder: ShadowRecorder | None = None,
        sample_rate: float = 1.0,
        random_fn: Callable[[], float] = random.random,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._station = str(station)
        self._cycle_id = str(cycle_id or "").strip()
        if not self._cycle_id:
            raise ValueError("cycle_id must not be empty")
        self._recorder = recorder
        self._sample_rate = min(1.0, max(0.0, float(sample_rate)))
        self._random = random_fn
        self._now = now

    def should_sample(self) -> bool:
        if self._sample_rate <= 0.0:
            return False
        if self._sample_rate >= 1.0:
            return True
        return self._random() < self._sample_rate

    def observe(
        self,
        target: TunnelTarget,
        constraints: Constraints | None = None,
    ) -> ShadowPlan | None:
        """Ask the fork for a plan without touching the tunnel."""

        if not self.should_sample():
            return None
        hostname = str(target.hostname or "").strip()
        if not hostname:
            return ShadowPlan(error="empty-target")
        try:
            job = self._client.switch(
                TunnelTarget(
                    hostname=hostname,
                    endpoint_ip=target.endpoint_ip,
                    expected_egress_ip=target.expected_egress_ip,
                ),
                constraints or Constraints(),
                idempotency_key=self._key("shadow"),
                wait=False,
                dry_run=True,
            )
        except ControlPlaneError as exc:
            return ShadowPlan(target=hostname, error=self._error_text(exc))
        return ShadowPlan(
            target=hostname,
            would_cut_tunnel=bool(job.raw.get("would_cut_tunnel", False)),
            plan=dict(job.raw.get("plan") or {}),
            warnings=tuple(str(item) for item in (job.raw.get("warnings") or []) if str(item).strip()),
            raw=dict(job.raw),
        )

    def compare(
        self,
        *,
        legacy_target: str,
        legacy_ip: str,
        plan: ShadowPlan | None = None,
    ) -> ShadowComparison:
        """Compare the fork's post-cut view with the legacy result."""

        shadow_target = ""
        shadow_ip = ""
        error = plan.error if plan is not None else ""
        try:
            egress = self._client.egress(fresh=True)
            shadow_ip = str(egress.ip or "").strip()
            state = self._client.state()
            desired = state.desired.get("hostname", "")
            shadow_target = str(desired or state.server.get("hostname", "") or "").strip()
        except ControlPlaneError as exc:
            error = self._error_text(exc)

        legacy_target = str(legacy_target or "").strip()
        legacy_ip = str(legacy_ip or "").strip()
        match_target = not legacy_target or legacy_target.lower() == (shadow_target.lower())
        match_ip = not legacy_ip or legacy_ip == shadow_ip
        match = match_target and match_ip and not error
        reason = ""
        if error:
            reason = "control-plane-error"
        elif not match_ip:
            reason = "ip"
        elif not match_target:
            reason = "target"

        comparison = ShadowComparison(
            station=self._station,
            legacy_target=legacy_target,
            shadow_target=shadow_target,
            legacy_ip=legacy_ip,
            shadow_ip=shadow_ip,
            match=match,
            divergence_reason=reason,
            would_cut_tunnel=bool(plan.would_cut_tunnel) if plan else False,
            warnings=plan.warnings if plan else (),
            error=error,
        )
        self._record(comparison)
        return comparison

    def _record(self, comparison: ShadowComparison) -> None:
        if self._recorder is None:
            return
        try:
            self._recorder.record(comparison.as_entry(ts=self._now()))
        except Exception:
            # Telemetry must never break the legacy rotation path.
            return

    def _key(self, action: str) -> str:
        return f"{action}-{self._station}-{self._cycle_id}"

    @staticmethod
    def _error_text(error: ControlPlaneError) -> str:
        code = getattr(error, "code", "") or type(error).__name__
        status = getattr(error, "status", None)
        suffix = f":{status}" if status else ""
        return f"{code}{suffix}"


__all__ = [
    "ShadowComparison",
    "ShadowObserver",
    "ShadowPlan",
    "ShadowRecorder",
    "TransportShadowComparison",
    "TransportShadowObserver",
]
