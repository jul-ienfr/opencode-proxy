"""Deterministic scores for the qualification queue and rotation.

Two scores, deliberately distinct:

* ``potential_score`` ranks hostnames **before** the first measurement.  It
  uses reliability, operator-relative RTT, known quota headroom, API load and
  freshness.  Country and source are never factors.
* ``measured_score`` ranks observed egress options **after** a 200, using the
  constitution IX weights (40/25/20/10/5).

The module is pure: same inputs + same clock -> same output.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .domain import Candidate


@dataclass(frozen=True, slots=True)
class PotentialWeights:
    reliability: float = 0.30
    latency: float = 0.30
    quota: float = 0.20
    load: float = 0.15
    freshness: float = 0.05
    latency_budget_ms: float = 250.0
    quota_estimate: int = 500
    freshness_ttl_h: float = 24.0

    def normalized(self) -> PotentialWeights:
        total = self.reliability + self.latency + self.quota + self.load + self.freshness
        if total <= 0:
            return PotentialWeights(
                latency_budget_ms=self.latency_budget_ms,
                quota_estimate=self.quota_estimate,
                freshness_ttl_h=self.freshness_ttl_h,
            )
        return PotentialWeights(
            reliability=self.reliability / total,
            latency=self.latency / total,
            quota=self.quota / total,
            load=self.load / total,
            freshness=self.freshness / total,
            latency_budget_ms=self.latency_budget_ms,
            quota_estimate=self.quota_estimate,
            freshness_ttl_h=self.freshness_ttl_h,
        )


@dataclass(frozen=True, slots=True)
class MeasuredWeights:
    success: float = 0.40
    latency: float = 0.25
    throughput: float = 0.20
    stability: float = 0.10
    freshness: float = 0.05
    latency_budget_ms: float = 8000.0
    ttl_usable_s: float = 43200.0

    def normalized(self) -> MeasuredWeights:
        total = self.success + self.latency + self.throughput + self.stability + self.freshness
        if total <= 0:
            return MeasuredWeights(
                latency_budget_ms=self.latency_budget_ms,
                ttl_usable_s=self.ttl_usable_s,
            )
        return MeasuredWeights(
            success=self.success / total,
            latency=self.latency / total,
            throughput=self.throughput / total,
            stability=self.stability / total,
            freshness=self.freshness / total,
            latency_budget_ms=self.latency_budget_ms,
            ttl_usable_s=self.ttl_usable_s,
        )


def reliability(candidate: Candidate) -> float:
    """Laplace-smoothed 200 rate; infrastructure failures are excluded."""

    ok = max(0, int(candidate.success_count))
    bad = max(0, int(candidate.fail_count))
    return (ok + 1.0) / (ok + bad + 2.0)


def potential_score(
    candidate: Candidate,
    *,
    now: float,
    weights: PotentialWeights | None = None,
) -> float:
    """Score 0..100 used to order the qualification queue."""

    w = (weights or PotentialWeights()).normalized()

    rel = reliability(candidate)

    if candidate.rtt_ms is None:
        latency = 0.5
    else:
        budget = max(1.0, float(w.latency_budget_ms))
        latency = 1.0 - min(1.0, max(0.0, float(candidate.rtt_ms)) / budget)

    if candidate.quota_remaining is None:
        quota = 0.5
    elif candidate.quota_reset_at is not None and float(candidate.quota_reset_at) > float(now):
        quota = 0.0
    else:
        estimate = max(1, int(w.quota_estimate))
        quota = min(1.0, max(0.0, float(candidate.quota_remaining) / estimate))

    load = 1.0 - min(1.0, max(0.0, float(candidate.load or 0.0)) / 100.0)

    seen = float(candidate.last_seen_at or candidate.first_seen_at or now)
    age_h = max(0.0, (float(now) - seen) / 3600.0)
    freshness = math.exp(-age_h / max(1.0, float(w.freshness_ttl_h)))

    return round(
        100.0
        * (
            w.reliability * rel
            + w.latency * latency
            + w.quota * quota
            + w.load * load
            + w.freshness * freshness
        ),
        3,
    )


def measured_score(
    candidate: Candidate,
    *,
    now: float,
    weights: MeasuredWeights | None = None,
    latency_ms: float | None = None,
) -> float:
    """Constitution IX score 0..100 used after a proven 200."""

    w = (weights or MeasuredWeights()).normalized()

    ok = max(0, int(candidate.success_count))
    bad = max(0, int(candidate.fail_count))
    tests = ok + bad
    success = (ok / tests) if tests else 0.0

    latency_value = float(latency_ms if latency_ms is not None else candidate.meta.get("last_latency_ms", 0.0) or 0.0)
    latency = 1.0 - min(1.0, max(0.0, latency_value) / max(1.0, float(w.latency_budget_ms)))

    throughput = float(candidate.meta.get("throughput_score", 0.5) or 0.5)
    throughput = min(1.0, max(0.0, throughput))
    stability = 1.0 - min(1.0, (bad / tests) if tests else 0.0)

    checked = float(candidate.last_attempt_at or candidate.last_seen_at or now)
    age_s = max(0.0, float(now) - checked)
    freshness = 1.0 - min(1.0, age_s / max(1.0, float(w.ttl_usable_s)))

    return round(
        100.0
        * (
            w.success * success
            + w.latency * latency
            + w.throughput * throughput
            + w.stability * stability
            + w.freshness * freshness
        ),
        3,
    )


__all__ = [
    "MeasuredWeights",
    "PotentialWeights",
    "measured_score",
    "potential_score",
    "reliability",
]
