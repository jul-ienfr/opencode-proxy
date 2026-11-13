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
from .models import Constraints, TunnelTarget


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
]
