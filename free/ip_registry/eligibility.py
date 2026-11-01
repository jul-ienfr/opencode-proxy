"""Eligibility rules for selecting an IP for a station."""

from __future__ import annotations

from dataclasses import dataclass

from .domain import GeoFilter, IpRecord, IpState
from .scoring import ScoreWeights, score_record


@dataclass(frozen=True, slots=True)
class EligibilityDecision:
    eligible: bool
    reason: str | None = None


def check_eligibility(
    record: IpRecord,
    now: float,
    *,
    geo: GeoFilter | None = None,
    weights: ScoreWeights | None = None,
    server_load: float = 0.0,
) -> EligibilityDecision:
    """Return whether ``record`` may be claimed for a station.

    Order matters: the most specific reason is returned first for diagnostics.
    """

    if record.state in (IpState.RETIRED,):
        return EligibilityDecision(False, "retired")
    if record.state is IpState.ASSIGNED:
        return EligibilityDecision(False, "already-assigned")
    if record.is_leased(now):
        return EligibilityDecision(False, "leased")
    if record.is_exhausted(now):
        return EligibilityDecision(False, "quota-reset-pending")
    if record.is_in_error_cooldown(now):
        return EligibilityDecision(False, "error-cooldown")
    if record.state not in (
        IpState.FRESH,
        IpState.CANDIDATE,
        IpState.EXHAUSTED,
        IpState.ERROR,
    ):
        return EligibilityDecision(False, f"state-{record.state.value}")
    if geo is not None and not geo.allows(record.country):
        return EligibilityDecision(False, "geo-filtered")
    current = score_record(
        record,
        now,
        weights=weights,
        server_load=server_load,
    )
    minimum = (weights or ScoreWeights()).min_score
    if current < minimum:
        return EligibilityDecision(False, f"score-below-{minimum:g}")
    return EligibilityDecision(True, None)
