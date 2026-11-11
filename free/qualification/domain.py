"""Pure domain for the NordVPN qualification pipeline.

The domain has no I/O, no provider import, no SQLite.  It owns the candidate
lifecycle, attempt classification, ordering keys and transport-independent
invariants.  Adapters (store, source, station, clock) live outside this module.

Constitution II: a hostname, a station session and an observed egress IP are
three distinct resources.  A ``no_egress``/``bind_error`` is an infrastructure
failure, never a hostname death sentence; a ``429`` is a quota fact, never
``dead``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class CandidateStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    PROBING = "probing"
    USABLE = "usable"
    QUOTA = "quota"
    AUTH = "auth"
    DEAD = "dead"
    SKIPPED = "skipped"


class AttemptStatus(StrEnum):
    OK = "200"
    QUOTA = "429"
    NO_EGRESS = "no_egress"
    BIND_ERROR = "bind_error"
    AUTH = "auth"
    TIMEOUT = "timeout"
    DUPLICATE = "duplicate"
    DEAD = "dead"
    SKIPPED = "skipped"
    INTERRUPTED = "interrupted"


TERMINAL_CANDIDATE_STATUSES = frozenset(
    {
        CandidateStatus.USABLE,
        CandidateStatus.AUTH,
        CandidateStatus.DEAD,
        CandidateStatus.SKIPPED,
    }
)
CLAIMABLE_CANDIDATE_STATUSES = frozenset(
    {
        CandidateStatus.PENDING,
        CandidateStatus.QUOTA,
    }
)
INFRA_ATTEMPT_STATUSES = frozenset(
    {
        AttemptStatus.NO_EGRESS,
        AttemptStatus.BIND_ERROR,
        AttemptStatus.TIMEOUT,
        AttemptStatus.DUPLICATE,
    }
)


@dataclass(frozen=True, slots=True)
class Candidate:
    """One catalog hostname and its qualification state.

    ``transports`` is ordered best-first (WireGuard before OpenVPN).  The
    ``transport_cursor``/``transport_round`` pair is persisted so a restart
    resumes the cycle instead of restarting at WireGuard.
    """

    hostname: str
    provider: str = "nordvpn"
    country: str = ""
    city: str = ""
    load: float = 0.0
    status: CandidateStatus = CandidateStatus.PENDING
    transports: tuple[str, ...] = ()
    transport_cursor: str = ""
    transport_round: int = 0
    potential_score: float = 0.0
    measured_score: float = 0.0
    rtt_ms: float | None = None
    quota_remaining: int | None = None
    quota_reset_at: float | None = None
    attempts: int = 0
    infra_failures: int = 0
    success_count: int = 0
    fail_count: int = 0
    last_attempt_at: float | None = None
    last_result: str = ""
    last_egress_ip: str = ""
    last_transport: str = ""
    next_eligible_at: float = 0.0
    epoch_id: str = ""
    skipped_reason: str = ""
    source_id: str = ""
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0
    updated_at: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def with_updates(self, **changes: Any) -> Candidate:
        return replace(self, **changes)


@dataclass(frozen=True, slots=True)
class AttemptOutcome:
    """Raw result of one station attempt, before classification.

    ``status`` is an HTTP-like code (200/429/401/0) or 0 for transport errors.
    ``valid`` is the proof-of-content flag (constitution VII).
    """

    status: int = 0
    egress_ip: str = ""
    latency_ms: float = 0.0
    rtt_ms: float = 0.0
    dns_ms: float = 0.0
    connect_ms: float = 0.0
    handshake_ms: float = 0.0
    endpoint_ip: str = ""
    reset_at: float = 0.0
    retry_after_s: float = 0.0
    error: str = ""
    validation: str = ""
    valid: bool = True


@dataclass(frozen=True, slots=True)
class Attempt:
    """Append-only record of one ``(hostname, transport, round)`` try."""

    hostname: str
    station: int
    transport: str
    status: AttemptStatus
    round: int = 0
    egress_ip: str = ""
    latency_ms: float = 0.0
    rtt_ms: float = 0.0
    dns_ms: float = 0.0
    connect_ms: float = 0.0
    handshake_ms: float = 0.0
    endpoint_ip: str = ""
    reset_at: float = 0.0
    retry_after_s: float = 0.0
    error: str = ""
    validation: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    run_id: str = ""
    epoch_id: str = ""
    provider: str = "nordvpn"


def classify_outcome(outcome: AttemptOutcome) -> AttemptStatus:
    """Map a raw station outcome to the catalog taxonomy.

    A 429 is always ``quota``.  A 0 is ``timeout`` when the error says so,
    otherwise ``no_egress``.  A 200 without proof of content is ``dead``: it
    must never be promoted (constitution VII).
    """

    code = int(outcome.status or 0)
    if code == 200:
        return AttemptStatus.OK if bool(outcome.valid) else AttemptStatus.DEAD
    if code == 429:
        return AttemptStatus.QUOTA
    if code in (401, 407):
        return AttemptStatus.AUTH
    if code == 0:
        text = str(outcome.error or "").lower()
        return AttemptStatus.TIMEOUT if "timeout" in text or "timed out" in text else AttemptStatus.NO_EGRESS
    if 500 <= code < 600:
        return AttemptStatus.DEAD
    return AttemptStatus.NO_EGRESS


def next_candidate_status(
    current: CandidateStatus,
    attempt: AttemptStatus,
    *,
    now: float,
    infra_backoff_s: float = 300.0,
    infra_backoff_max_s: float = 3600.0,
    infra_failures: int = 0,
    skipped_reason: str = "",
) -> CandidateStatus:
    """Pure candidate transition for one attempt.

    The caller persists ``next_eligible_at`` (``now + backoff``) for infra
    failures; this function only decides the status.
    """

    if attempt is AttemptStatus.OK:
        return CandidateStatus.USABLE
    if attempt is AttemptStatus.QUOTA:
        return CandidateStatus.QUOTA
    if attempt is AttemptStatus.AUTH:
        # AUTH_FAILED is station-level (bad session/credentials on this
        # station), not a property of the hostname.  Keep the host pending
        # with a bounded backoff instead of condemning it.
        return CandidateStatus.PENDING
    if attempt is AttemptStatus.DEAD:
        return CandidateStatus.DEAD
    if attempt is AttemptStatus.SKIPPED:
        return CandidateStatus.SKIPPED
    if attempt is AttemptStatus.INTERRUPTED:
        return CandidateStatus.PENDING
    if attempt in INFRA_ATTEMPT_STATUSES:
        # An infrastructure failure never kills the hostname.  After a bounded
        # number of retries it is parked for the epoch, not marked dead.
        if infra_failures >= 3:
            return CandidateStatus.SKIPPED
        return CandidateStatus.PENDING
    return CandidateStatus.PENDING


def infra_backoff(
    failures: int,
    *,
    base_s: float = 300.0,
    max_s: float = 3600.0,
    jitter_s: float = 0.0,
) -> float:
    """Exponential-ish bounded backoff for infrastructure failures."""

    attempt = max(1, int(failures))
    delay = min(float(max_s), float(base_s) * (2 ** (attempt - 1)))
    if jitter_s > 0:
        # Deterministic jitter is injected by the caller; this domain helper
        # only returns the bounded delay.
        delay = min(float(max_s), delay + float(jitter_s))
    return max(0.0, delay)


def deterministic_jitter(hostname: str, max_s: float) -> float:
    """Stable per-hostname jitter in ``[0, max_s)``.

    A 429 reset is shared by every host that observed the same IP, but the
    retry must not stampede at the exact reset second. Hashing the hostname
    spreads retries deterministically and survives restarts.
    """

    if float(max_s) <= 0.0:
        return 0.0
    digest = hashlib.sha256(str(hostname or "").encode("utf-8")).digest()
    span_ms = max(1, int(round(float(max_s) * 1000.0)))
    return (int.from_bytes(digest[:8], "big") % span_ms) / 1000.0


def order_key(candidate: Candidate, *, missing_rtt_ms: float = 1.0e9) -> tuple:
    """Queue order: potential, quota, RTT, load, hostname.

    Country and source are deliberately absent (constitution IX/VI).
    """

    rtt = float(candidate.rtt_ms) if candidate.rtt_ms is not None else float(missing_rtt_ms)
    quota = int(candidate.quota_remaining) if candidate.quota_remaining is not None else 0
    return (
        -float(candidate.potential_score),
        -quota,
        rtt,
        float(candidate.load or 0.0),
        str(candidate.hostname),
    )


def is_claimable(candidate: Candidate, now: float) -> bool:
    """Hard gates for the next qualification claim."""

    if candidate.status not in CLAIMABLE_CANDIDATE_STATUSES:
        return False
    if float(candidate.next_eligible_at or 0.0) > float(now):
        return False
    if candidate.status is CandidateStatus.QUOTA:
        reset = float(candidate.quota_reset_at or 0.0)
        if reset > float(now):
            return False
    if not candidate.transports:
        return False
    return True


__all__ = [
    "Attempt",
    "AttemptOutcome",
    "AttemptStatus",
    "CLAIMABLE_CANDIDATE_STATUSES",
    "Candidate",
    "CandidateStatus",
    "INFRA_ATTEMPT_STATUSES",
    "TERMINAL_CANDIDATE_STATUSES",
    "classify_outcome",
    "deterministic_jitter",
    "infra_backoff",
    "is_claimable",
    "next_candidate_status",
    "order_key",
]
