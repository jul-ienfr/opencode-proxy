"""Qualification sweep orchestration.

The sweep claims one hostname at a time, walks its supported transports in the
configured order, records every attempt, and promotes a proven 200.  It is
deliberately adapter-driven: tests inject a fake station, production injects
the existing Gluetun/manager path.  No Docker/network import lives here.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Protocol

from .domain import (
    Attempt,
    AttemptOutcome,
    AttemptStatus,
    Candidate,
    CandidateStatus,
    classify_outcome,
    deterministic_jitter,
    infra_backoff,
    next_candidate_status,
)
from .store import QualificationStore
from .transport import DEFAULT_TRANSPORT_ORDER, TransportPlan, build_plan

_LOG = logging.getLogger(__name__)


class StationAdapter(Protocol):
    """One station's bind + free-gate probe."""

    async def qualify(self, candidate: Candidate, transport: str, station: int) -> AttemptOutcome: ...


@dataclass
class SweepConfig:
    station: int = 1
    max_hosts: int = 0
    max_attempts_per_host: int = 3
    infra_backoff_s: float = 300.0
    infra_backoff_max_s: float = 3600.0
    lease_s: float = 300.0
    strategy: str = "auto"
    manual_transport: str = ""
    transport_order: tuple[str, ...] = DEFAULT_TRANSPORT_ORDER
    transport_rounds: int = 2
    transport_retry_backoff_s: float = 60.0
    auth_backoff_s: float = 3600.0
    transport_health_min_attempts: int = 100
    quota_retry_jitter_s: float = 0.0
    shadow: bool = False
    run_id: str = ""


@dataclass
class SweepResult:
    epoch_id: str
    claimed: int = 0
    promoted: int = 0
    quota: int = 0
    infra: int = 0
    dead: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def snapshot(self) -> dict[str, Any]:
        return {
            "epoch_id": self.epoch_id,
            "claimed": self.claimed,
            "promoted": self.promoted,
            "quota": self.quota,
            "infra": self.infra,
            "dead": self.dead,
            "skipped": self.skipped,
            "errors": list(self.errors),
        }


class QualificationSweep:
    def __init__(
        self,
        store: QualificationStore,
        adapter: StationAdapter | None = None,
        *,
        config: SweepConfig | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.config = config or SweepConfig()
        self._clock = clock

    async def run_epoch(
        self,
        *,
        epoch_id: str = "",
        max_hosts: int | None = None,
        shadow: bool | None = None,
    ) -> SweepResult:
        eid = epoch_id or f"epoch-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
        limit = self.config.max_hosts if max_hosts is None else max(0, int(max_hosts))
        is_shadow = self.config.shadow if shadow is None else bool(shadow)
        result = SweepResult(epoch_id=eid)
        self.store.create_epoch(eid, total=self.store.summary()["total"], now=self._clock())
        _LOG.info(
            "qualification.epoch.start",
            extra={
                "epoch_id": eid,
                "station": self.config.station,
                "shadow": is_shadow,
                "max_hosts": limit,
            },
        )
        def _close_epoch(status: str, *, reason: str = "") -> None:
            self.store.close_epoch(
                eid,
                status=status,
                now=self._clock(),
                reason=reason or "; ".join(result.errors[:3]),
                tested=result.claimed,
                usable=result.promoted,
                quota=result.quota,
                infra=result.infra,
                dead=result.dead,
                skipped=result.skipped,
            )
            _LOG.info(
                "qualification.epoch.end",
                extra={
                    "epoch_id": eid,
                    "status": status,
                    "claimed": result.claimed,
                    "promoted": result.promoted,
                    "quota": result.quota,
                    "infra": result.infra,
                    "dead": result.dead,
                    "skipped": result.skipped,
                    "errors": len(result.errors),
                },
            )

        in_flight: Candidate | None = None
        try:
            while limit == 0 or result.claimed < limit:
                candidate = self.store.claim_next(
                    self.config.station,
                    now=self._clock(),
                    lease_s=self.config.lease_s,
                    epoch_id=eid,
                )
                if candidate is None:
                    break
                result.claimed += 1
                if is_shadow:
                    self.store.release(candidate.hostname, status=CandidateStatus.PENDING, now=self._clock())
                    continue
                if self.adapter is None:
                    self.store.release(
                        candidate.hostname,
                        status=CandidateStatus.PENDING,
                        now=self._clock(),
                        reason="no-station-adapter",
                    )
                    result.errors.append("no-station-adapter")
                    continue
                try:
                    in_flight = candidate
                    await self.qualify(candidate, epoch_id=eid)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # fail-soft: one host must not stop the sweep
                    self.store.release(
                        candidate.hostname,
                        status=CandidateStatus.PENDING,
                        now=self._clock(),
                        next_eligible_at=self._clock() + self.config.infra_backoff_s,
                        reason=f"{type(exc).__name__}: {exc}"[:500],
                    )
                    result.errors.append(f"{candidate.hostname}:{type(exc).__name__}")
                    continue
                in_flight = None
                current = self.store.get(candidate.hostname)
                if current is not None:
                    if current.status is CandidateStatus.USABLE:
                        result.promoted += 1
                    elif current.status is CandidateStatus.QUOTA:
                        result.quota += 1
                    elif current.status is CandidateStatus.DEAD:
                        result.dead += 1
                    elif current.status is CandidateStatus.SKIPPED:
                        result.skipped += 1
                    else:
                        result.infra += 1
        except asyncio.CancelledError:
            # A stopped slot must not leave a ``running`` epoch or a host
            # leased forever.  Release only an unmeasured claim: a candidate
            # that already reached ``usable``/``quota`` keeps its verdict.
            if in_flight is not None:
                current = self.store.get(in_flight.hostname)
                if current is not None and current.status in {
                    CandidateStatus.CLAIMED,
                    CandidateStatus.PROBING,
                }:
                    self.store.release(
                        in_flight.hostname,
                        status=CandidateStatus.PENDING,
                        now=self._clock(),
                        reason="cancelled",
                    )
            _close_epoch("cancelled", reason="cancelled")
            raise
        status = "complete" if not self.store.has_claimable(now=self._clock()) else "partial"
        _close_epoch(status)
        return result

    async def qualify(self, candidate: Candidate, *, epoch_id: str = "") -> Attempt:
        """Run the full transport cycle for one candidate."""

        if int(candidate.transport_round or 0) >= max(1, int(self.config.max_attempts_per_host)):
            self.store.release(
                candidate.hostname,
                status=CandidateStatus.SKIPPED,
                now=self._clock(),
                reason="max-attempts",
            )
            return self._terminal_attempt(candidate, AttemptStatus.SKIPPED, "max-attempts")
        plan = self._plan_for(candidate)
        if plan.current is None:
            self.store.release(
                candidate.hostname,
                status=CandidateStatus.SKIPPED,
                now=self._clock(),
                reason="unsupported-transport",
            )
            return self._terminal_attempt(candidate, AttemptStatus.SKIPPED, "unsupported-transport")

        last: Attempt | None = None
        quota_reset_at = float(candidate.quota_reset_at or 0.0)
        while plan.current is not None:
            transport = plan.current
            started = self._clock()
            if self.adapter is None:
                outcome = AttemptOutcome(status=0, error="no-station-adapter")
            else:
                try:
                    outcome = await self.adapter.qualify(candidate, transport, self.config.station)
                except Exception as exc:  # classify as infrastructure, never host death
                    outcome = AttemptOutcome(status=0, error=f"{type(exc).__name__}: {exc}")
            status = classify_outcome(outcome)
            if status is AttemptStatus.OK and self.store.egress_ip_in_use(
                outcome.egress_ip,
                station=self.config.station,
            ):
                status = AttemptStatus.DUPLICATE
            now = self._clock()
            next_plan = plan.advance(status)
            wrapped = int(next_plan.round) > int(plan.round)
            infra_failures = int(candidate.infra_failures or 0) + (
                1
                if status
                in {
                    AttemptStatus.NO_EGRESS,
                    AttemptStatus.BIND_ERROR,
                    AttemptStatus.TIMEOUT,
                    AttemptStatus.DUPLICATE,
                }
                else 0
            )
            next_status = next_candidate_status(
                candidate.status,
                status,
                now=now,
                infra_failures=infra_failures,
            )
            next_eligible = 0.0
            reset_at = None
            if status is AttemptStatus.QUOTA:
                reset_at = float(outcome.reset_at or 0.0) or (now + max(5.0, float(outcome.retry_after_s or 86400.0)))
                reset_at += deterministic_jitter(
                    candidate.hostname,
                    self.config.quota_retry_jitter_s,
                )
                quota_reset_at = min(quota_reset_at, reset_at) if quota_reset_at > 0 else reset_at
                next_status = CandidateStatus.QUOTA
                next_eligible = quota_reset_at
            elif status in {AttemptStatus.NO_EGRESS, AttemptStatus.BIND_ERROR, AttemptStatus.TIMEOUT}:
                if quota_reset_at > 0:
                    # A host that already hit a 429 stays quota even if another
                    # transport fails at the infrastructure layer.
                    next_status = CandidateStatus.QUOTA
                    next_eligible = quota_reset_at
                else:
                    next_eligible = now + infra_backoff(
                        infra_failures,
                        base_s=self.config.infra_backoff_s,
                        max_s=self.config.infra_backoff_max_s,
                    )
            elif status is AttemptStatus.AUTH:
                # Do not park the hostname for a station-local auth failure.
                next_eligible = now + max(60.0, float(self.config.auth_backoff_s))
            if wrapped and status not in {AttemptStatus.OK, AttemptStatus.AUTH, AttemptStatus.DEAD, AttemptStatus.SKIPPED}:
                # The cycle wrapped to the first transport: wait before round 2
                # instead of hammering the same host in one call.
                next_eligible = max(
                    float(next_eligible or 0.0),
                    now + max(5.0, float(self.config.transport_retry_backoff_s)),
                )
            meta = dict(candidate.meta or {})
            dns_ms = float(
                getattr(outcome, "dns_ms", 0.0) or meta.get("dns_ms", 0.0) or 0.0
            )
            connect_ms = float(
                getattr(outcome, "connect_ms", 0.0) or meta.get("connect_ms", 0.0) or 0.0
            )
            handshake_ms = float(
                getattr(outcome, "handshake_ms", 0.0)
                or meta.get("handshake_ms", 0.0)
                or 0.0
            )
            endpoint_ip = str(
                getattr(outcome, "endpoint_ip", "") or meta.get("endpoint_ip", "") or ""
            )
            attempt = Attempt(
                hostname=candidate.hostname,
                station=self.config.station,
                transport=transport,
                status=status,
                round=plan.round,
                egress_ip=str(outcome.egress_ip or ""),
                latency_ms=float(outcome.latency_ms or 0.0),
                rtt_ms=float(outcome.rtt_ms or 0.0),
                dns_ms=dns_ms,
                connect_ms=connect_ms,
                handshake_ms=handshake_ms,
                endpoint_ip=endpoint_ip,
                reset_at=float(reset_at or 0.0),
                retry_after_s=float(outcome.retry_after_s or 0.0),
                error=str(outcome.error or ""),
                validation=str(outcome.validation or ""),
                started_at=started,
                finished_at=now,
                run_id=self.config.run_id or f"sweep-{uuid.uuid4().hex[:8]}",
                epoch_id=epoch_id,
            )
            recorded = self.store.record_attempt(attempt)
            if not recorded:
                # Same (hostname, transport, round, epoch): a crash-resume
                # replay must not increment counters twice.
                return attempt
            _LOG.info(
                "qualification.attempt",
                extra={
                    "hostname": attempt.hostname,
                    "station": attempt.station,
                    "transport": attempt.transport,
                    "round": attempt.round,
                    "status": str(attempt.status),
                    "egress_ip": attempt.egress_ip,
                    "rtt_ms": attempt.rtt_ms,
                    "epoch_id": attempt.epoch_id,
                },
            )
            self.store.apply_attempt(
                candidate.hostname,
                attempt,
                candidate_status=next_status,
                next_eligible_at=next_eligible,
                quota_reset_at=quota_reset_at or None,
                transport_cursor=str(next_plan.current or ""),
                transport_round=int(next_plan.round),
                reason=str(outcome.error or ""),
                transport_health_min_attempts=self.config.transport_health_min_attempts,
                now=now,
            )
            last = attempt
            if status is AttemptStatus.OK:
                self.store.promote(candidate.hostname, egress_ip=attempt.egress_ip, now=now)
                return attempt
            if status in {AttemptStatus.AUTH, AttemptStatus.DEAD, AttemptStatus.SKIPPED}:
                return attempt
            if wrapped:
                # Round 2 is scheduled by the next claim after the backoff.
                return attempt
            # Continue to the next transport/round (429 and infra failures).
            plan = next_plan
            updated = self.store.get(candidate.hostname)
            if updated is not None:
                candidate = updated
        if last is None:
            last = self._terminal_attempt(candidate, AttemptStatus.SKIPPED, "no-transport")
        return last

    def _plan_for(self, candidate: Candidate) -> TransportPlan:
        base = build_plan(
            candidate.transports,
            strategy=self.config.strategy,
            manual=self.config.manual_transport,
            order=self.config.transport_order,
            max_rounds=self.config.transport_rounds,
        )
        if not base.order:
            return base
        cursor = 0
        if candidate.transport_cursor in base.order:
            cursor = base.order.index(candidate.transport_cursor)
        return build_plan(
            candidate.transports,
            strategy=self.config.strategy,
            manual=self.config.manual_transport,
            order=self.config.transport_order,
            max_rounds=self.config.transport_rounds,
            cursor=cursor,
            round_no=candidate.transport_round,
        )

    def _terminal_attempt(self, candidate: Candidate, status: AttemptStatus, error: str) -> Attempt:
        now = self._clock()
        attempt = Attempt(
            hostname=candidate.hostname,
            station=self.config.station,
            transport="",
            status=status,
            round=int(candidate.transport_round or 0),
            error=error,
            started_at=now,
            finished_at=now,
            run_id=self.config.run_id or "sweep",
        )
        self.store.record_attempt(attempt)
        return attempt


__all__ = ["QualificationSweep", "StationAdapter", "SweepConfig", "SweepResult"]
