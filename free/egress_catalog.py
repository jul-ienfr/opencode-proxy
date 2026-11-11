"""Pure domain for the egress catalog.

An :class:`EgressOption` is one rotation bucket:

- public  : an egress IP (two ports behind the same IP are one option);
- WARP    : an observed IPv6 ``/64``;
- NordVPN : an observed egress IP.

No I/O, no provider import, no SQLite.  Adapters and the store call these
functions; every transition is deterministic and testable in isolation.
"""

from __future__ import annotations

import ipaddress
import time
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any


class Provider(StrEnum):
    PUBLIC = "public"
    WARP = "warp"
    NORDVPN = "nordvpn"


class OptionStatus(StrEnum):
    CANDIDATE = "candidate"
    REACHABLE = "reachable"
    USABLE = "usable"
    QUOTA = "quota"
    AUTH = "auth"
    DEAD = "dead"
    EXPIRED = "expired"


class ObservationPhase(StrEnum):
    LIVENESS = "liveness"
    FREE_GATE = "free_gate"
    AUTH = "auth"
    EGRESS = "egress"


# Statuses that may be handed to a rotation.  ``reachable`` is deliberately
# absent: a live proxy that never passed the free gate is not usable.
SELECTABLE = frozenset({OptionStatus.USABLE})

# Statuses that are terminal until a new observation.
BLOCKED = frozenset({OptionStatus.QUOTA, OptionStatus.AUTH, OptionStatus.DEAD})


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    provider: str
    max_active: int = 64
    max_options: int = 20_000
    concurrency: int = 8
    ttl_usable_s: float = 43_200.0
    ttl_quota_max_s: float = 86_400.0
    ttl_dead_s: float = 604_800.0
    ttl_reachable_s: float = 1_800.0
    discovery_interval_s: float = 21_600.0
    revalidate_interval_s: float = 1_800.0

    @classmethod
    def from_config(cls, provider: str, raw: dict[str, Any] | None) -> ProviderPolicy:
        data = raw if isinstance(raw, dict) else {}
        defaults = _DEFAULT_POLICIES.get(provider, ProviderPolicy(provider=provider))
        return cls(
            provider=provider,
            max_active=_int(data.get("max_active"), defaults.max_active, minimum=0),
            max_options=_int(data.get("max_options"), defaults.max_options, minimum=1),
            concurrency=_int(data.get("concurrency"), defaults.concurrency, minimum=1),
            ttl_usable_s=_float(data.get("ttl_usable_s"), defaults.ttl_usable_s),
            ttl_quota_max_s=_float(data.get("ttl_quota_max_s"), defaults.ttl_quota_max_s),
            ttl_dead_s=_float(data.get("ttl_dead_s"), defaults.ttl_dead_s),
            ttl_reachable_s=_float(data.get("ttl_reachable_s"), defaults.ttl_reachable_s),
            discovery_interval_s=_float(
                data.get("discovery_interval_s"), defaults.discovery_interval_s
            ),
            revalidate_interval_s=_float(
                data.get("revalidate_interval_s"), defaults.revalidate_interval_s
            ),
        )


def _int(value: Any, default: int, *, minimum: int) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return default


_DEFAULT_POLICIES = {
    Provider.PUBLIC: ProviderPolicy(provider=Provider.PUBLIC),
    Provider.WARP: ProviderPolicy(
        provider=Provider.WARP,
        max_active=24,
        max_options=24,
        concurrency=6,
        discovery_interval_s=1_800.0,
    ),
    Provider.NORDVPN: ProviderPolicy(
        provider=Provider.NORDVPN,
        max_active=10,
        max_options=10,
        concurrency=4,
        discovery_interval_s=1_800.0,
    ),
}


@dataclass(frozen=True, slots=True)
class Observation:
    phase: str
    status: int
    result: str
    observed_at: float
    latency_ms: float = 0.0
    message: str = ""
    run_id: str = ""


@dataclass(frozen=True, slots=True)
class EgressOption:
    key: str
    provider: str
    kind: str
    host: str = ""
    port: int = 0
    station: str = ""
    egress_ip: str = ""
    block: str = ""
    status: str = OptionStatus.CANDIDATE
    first_seen_at: float = 0.0
    last_seen_at: float = 0.0
    last_checked_at: float = 0.0
    expires_at: float = 0.0
    reset_at: float = 0.0
    success_count: int = 0
    fail_count: int = 0
    last_status: int = 0
    last_latency_ms: float = 0.0
    score: float = 0.0
    source_ids: tuple[str, ...] = ()
    meta: dict[str, Any] = field(default_factory=dict)

    def with_updates(self, **changes: Any) -> EgressOption:
        return replace(self, **changes)


def canonical_key(
    provider: str,
    kind: str,
    *,
    host: str = "",
    port: int = 0,
    station: str = "",
    egress_ip: str = "",
    block: str = "",
) -> str:
    """Stable key.  Two endpoints behind one egress share the egress key."""

    provider = str(provider or "").strip().lower()
    kind = str(kind or "").strip().lower()
    egress = str(egress_ip or "").strip()
    if provider == Provider.PUBLIC:
        endpoint = f"{str(host or '').strip().lower()}:{int(port or 0)}"
        return f"{provider}|{kind}|{endpoint}|{egress}"
    if provider == Provider.WARP:
        unit = str(block or egress).strip()
        return f"{provider}|{kind}|{str(station or '').strip()}|{unit}"
    return f"{provider}|{kind}|{str(station or '').strip()}|{egress}"


def option_block(provider: str, egress_ip: str = "", explicit: str = "") -> str:
    """The rotation unit for a provider.

    Public and NordVPN rotate on one egress IP; WARP rotates on a ``/64``.
    The public pool still applies its own ``max_per_24`` diversity guard.
    """

    value = str(explicit or "").strip()
    if value:
        return value
    raw = str(egress_ip or "").strip()
    if not raw:
        return ""
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return raw
    if str(provider).lower() == Provider.WARP and address.version == 6:
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def new_option(
    *,
    provider: str,
    kind: str,
    host: str = "",
    port: int = 0,
    station: str = "",
    egress_ip: str = "",
    block: str = "",
    source_ids: tuple[str, ...] = (),
    meta: dict[str, Any] | None = None,
    now: float | None = None,
) -> EgressOption:
    ts = time.time() if now is None else float(now)
    normalized_egress = _normalize_ip(egress_ip)
    unit = option_block(provider, normalized_egress, block)
    return EgressOption(
        key=canonical_key(
            provider,
            kind,
            host=host,
            port=port,
            station=station,
            egress_ip=normalized_egress,
            block=unit,
        ),
        provider=str(provider).lower(),
        kind=str(kind).lower(),
        host=str(host or "").strip(),
        port=int(port or 0),
        station=str(station or "").strip(),
        egress_ip=normalized_egress,
        block=unit,
        status=OptionStatus.CANDIDATE,
        first_seen_at=ts,
        last_seen_at=ts,
        expires_at=ts + 300.0,
        source_ids=tuple(str(item) for item in source_ids if str(item).strip()),
        meta=dict(meta or {}),
    )


def _normalize_ip(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return raw


def ttl_for(policy: ProviderPolicy, status: str) -> float:
    if status == OptionStatus.USABLE:
        return policy.ttl_usable_s
    if status == OptionStatus.QUOTA:
        return policy.ttl_quota_max_s
    if status == OptionStatus.AUTH:
        return policy.ttl_quota_max_s
    if status == OptionStatus.DEAD:
        return policy.ttl_dead_s
    if status == OptionStatus.REACHABLE:
        return policy.ttl_reachable_s
    return min(policy.ttl_reachable_s, 300.0)


def transition(
    option: EgressOption,
    observation: Observation,
    policy: ProviderPolicy,
    *,
    reset_at: float = 0.0,
    source_ids: tuple[str, ...] = (),
) -> EgressOption:
    """Apply one observation and return the next immutable option.

    The provider reset is authoritative: a 429 sets ``quota`` and carries the
    exact ``reset_at`` supplied by the adapter.  A missing reset uses the
    policy ceiling, never a fake success.
    """

    now = float(observation.observed_at or time.time())
    result = str(observation.result or "").strip().lower()
    if result == OptionStatus.USABLE:
        status = OptionStatus.USABLE
        expires = now + policy.ttl_usable_s
        reset = 0.0
        successes = option.success_count + 1
        failures = option.fail_count
    elif result == OptionStatus.QUOTA:
        status = OptionStatus.QUOTA
        reset = float(reset_at or 0.0)
        expires = reset if reset > now else now + policy.ttl_quota_max_s
        successes = option.success_count
        failures = option.fail_count + 1
    elif result == OptionStatus.AUTH:
        status = OptionStatus.AUTH
        reset = 0.0
        expires = now + policy.ttl_quota_max_s
        successes = option.success_count
        failures = option.fail_count + 1
    elif result == OptionStatus.REACHABLE:
        status = OptionStatus.REACHABLE
        reset = 0.0
        expires = now + policy.ttl_reachable_s
        successes = option.success_count
        failures = option.fail_count
    else:
        status = OptionStatus.DEAD
        reset = 0.0
        expires = now + policy.ttl_dead_s
        successes = option.success_count
        failures = option.fail_count + 1
    merged_sources = tuple(dict.fromkeys((*option.source_ids, *source_ids)))
    return option.with_updates(
        status=status,
        last_seen_at=now,
        last_checked_at=now,
        expires_at=expires,
        reset_at=reset,
        success_count=successes,
        fail_count=failures,
        last_status=int(observation.status or 0),
        last_latency_ms=float(observation.latency_ms or 0.0),
        source_ids=merged_sources,
        meta={**option.meta, "last_phase": str(observation.phase), "last_message": str(observation.message)[:300]},
    )


def is_expired(option: EgressOption, now: float) -> bool:
    return bool(option.expires_at) and float(option.expires_at) <= float(now)


def is_selectable(
    option: EgressOption,
    now: float,
    policy: ProviderPolicy,
    *,
    claimed_blocks: set[str] | None = None,
) -> bool:
    """Hard gates first; the score only orders what survives."""

    if str(option.provider) != str(policy.provider):
        return False
    if str(option.status) not in SELECTABLE:
        return False
    if is_expired(option, now):
        return False
    if float(option.reset_at or 0.0) > float(now):
        return False
    if not (option.egress_ip or option.block):
        return False
    if claimed_blocks and option.block and option.block in claimed_blocks:
        return False
    return True


def score_option(option: EgressOption, now: float, policy: ProviderPolicy) -> float:
    """Constitution IX: 40 % success, 25 % latency, 20 % throughput,
    10 % stability, 5 % freshness.  Missing throughput is neutral."""

    tests = int(option.success_count) + int(option.fail_count)
    success = (int(option.success_count) / tests) if tests else 0.0
    latency_budget = 8_000.0
    latency = 1.0 - min(1.0, max(0.0, float(option.last_latency_ms or 0.0)) / latency_budget)
    throughput = float(option.meta.get("throughput_score", 0.5) or 0.5)
    stability = 1.0 - min(1.0, int(option.fail_count) / max(1, tests))
    age = max(0.0, now - float(option.last_checked_at or option.last_seen_at or now))
    freshness = 1.0 - min(1.0, age / max(1.0, policy.ttl_usable_s))
    return round(
        100.0
        * (
            0.40 * success
            + 0.25 * latency
            + 0.20 * throughput
            + 0.10 * stability
            + 0.05 * freshness
        ),
        3,
    )


def classify_result(
    status: int,
    *,
    phase: str = ObservationPhase.FREE_GATE,
    retry_after_s: float = 0.0,
    auth_statuses: tuple[int, ...] = (401, 407),
) -> str:
    """Map an HTTP/probe status to a catalog result.

    A 429 is always ``quota`` (never ``dead``); a 5xx/timeout is ``dead``;
    an auth status is ``auth`` only in the auth phase or for a provider that
    declares it.
    """

    code = int(status or 0)
    if code == 200:
        return OptionStatus.USABLE
    if code == 429:
        return OptionStatus.QUOTA
    if code in auth_statuses and str(phase) == ObservationPhase.AUTH:
        return OptionStatus.AUTH
    if code in (401, 407):
        return OptionStatus.AUTH
    if code == 0:
        return OptionStatus.DEAD
    return OptionStatus.DEAD


def reset_from_retry_after(retry_after_s: float, *, now: float) -> float:
    seconds = max(0.0, float(retry_after_s or 0.0))
    return float(now) + seconds if seconds > 0 else 0.0


__all__ = [
    "BLOCKED",
    "SELECTABLE",
    "EgressOption",
    "Observation",
    "ObservationPhase",
    "OptionStatus",
    "Provider",
    "ProviderPolicy",
    "canonical_key",
    "classify_result",
    "is_expired",
    "is_selectable",
    "new_option",
    "option_block",
    "reset_from_retry_after",
    "score_option",
    "transition",
    "ttl_for",
]
