"""Probe classification and provider reset parsing.

No network I/O here: adapters perform the request and hand a
:class:`ProbeOutcome` to these pure helpers.  This keeps the 200/429/403
taxonomy identical across public, WARP and NordVPN.
"""

from __future__ import annotations

import email.utils
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from free.egress_catalog import (
    ObservationPhase,
    OptionStatus,
    classify_result,
)


@dataclass(frozen=True, slots=True)
class ProbeOutcome:
    status: int
    egress_ip: str = ""
    latency_ms: float = 0.0
    error: str = ""
    retry_after_s: float = 0.0
    body: str = ""
    headers: Mapping[str, str] | None = None

    @property
    def ok(self) -> bool:
        return int(self.status) == 200 and bool(self.egress_ip or self.status)

    @property
    def quota(self) -> bool:
        return int(self.status) == 429


def parse_retry_after(
    headers: Mapping[str, str] | None = None,
    body: str = "",
    *,
    now: float | None = None,
    retry_after_s: float = 0.0,
) -> float:
    """Return the provider reset as an absolute epoch, or 0.

    Accepted sources, in order: ``Retry-After`` seconds, ``Retry-After`` HTTP
    date, ``reset_at``/``retry_after`` in the JSON body.  A local estimate is
    never invented: no source means no reset.
    """

    ts = time.time() if now is None else float(now)
    if float(retry_after_s or 0.0) > 0:
        return ts + float(retry_after_s)
    lowered = {str(key).lower(): str(value) for key, value in dict(headers or {}).items()}
    raw = lowered.get("retry-after", "").strip()
    if raw:
        try:
            return ts + max(0.0, float(raw))
        except ValueError:
            parsed = email.utils.parsedate_to_datetime(raw) if raw else None
            if parsed is not None:
                return max(ts, parsed.timestamp())
    try:
        payload = json.loads(str(body or ""))
    except (TypeError, ValueError):
        return 0.0
    if not isinstance(payload, dict):
        return 0.0
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    for key in ("reset_at", "reset", "retry_after", "retryAfter"):
        value = error.get(key)
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number <= 0:
            continue
        # A small number is a duration; a large number is already an epoch.
        return number if number > 1_000_000_000 else ts + number
    return 0.0


def classify_outcome(
    outcome: ProbeOutcome,
    *,
    phase: str = ObservationPhase.FREE_GATE,
    now: float | None = None,
) -> tuple[str, float, str]:
    """Return ``(result, reset_at, message)`` for an observation."""

    ts = time.time() if now is None else float(now)
    result = classify_result(
        int(outcome.status or 0),
        phase=phase,
        retry_after_s=float(outcome.retry_after_s or 0.0),
    )
    reset_at = 0.0
    if result == OptionStatus.QUOTA:
        reset_at = parse_retry_after(
            outcome.headers,
            outcome.body,
            now=ts,
            retry_after_s=float(outcome.retry_after_s or 0.0),
        )
    message = str(outcome.error or "")[:300]
    return str(result), reset_at, message


def liveness_result(outcome: ProbeOutcome) -> str:
    """Liveness only proves reachability; it never promotes to ``usable``."""

    if int(outcome.status or 0) == 200 and outcome.egress_ip:
        return str(OptionStatus.REACHABLE)
    if int(outcome.status or 0) == 429:
        return str(OptionStatus.QUOTA)
    return str(OptionStatus.DEAD)


def normalize_observation_payload(payload: Any) -> dict[str, Any]:
    """Bound and sanitize a free-gate response for the observation journal."""

    if not isinstance(payload, dict):
        return {}
    allowed = ("type", "message", "reset_at", "retry_after")
    error = payload.get("error") if isinstance(payload.get("error"), dict) else payload
    return {key: error[key] for key in allowed if key in error}


__all__ = [
    "ProbeOutcome",
    "classify_outcome",
    "liveness_result",
    "normalize_observation_payload",
    "parse_retry_after",
]
