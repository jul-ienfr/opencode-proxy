"""Deterministic IP scoring.

The score is a number in [0, 100].  It is pure: same inputs -> same output.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .domain import IpRecord, IpState


@dataclass(frozen=True, slots=True)
class ScoreWeights:
    availability: float = 0.55
    latency: float = 0.25
    freshness: float = 0.20
    latency_budget_ms: float = 8000.0
    freshness_ttl_h: float = 48.0
    load_penalty_max: float = 10.0
    min_score: float = 20.0

    def normalized(self) -> ScoreWeights:
        total = self.availability + self.latency + self.freshness
        if total <= 0:
            return ScoreWeights()
        return ScoreWeights(
            availability=self.availability / total,
            latency=self.latency / total,
            freshness=self.freshness / total,
            latency_budget_ms=self.latency_budget_ms,
            freshness_ttl_h=self.freshness_ttl_h,
            load_penalty_max=self.load_penalty_max,
            min_score=self.min_score,
        )


def _age_hours(timestamp: float | None, now: float) -> float | None:
    if timestamp is None:
        return None
    return max(0.0, (now - timestamp) / 3600.0)


def score_record(
    record: IpRecord,
    now: float,
    *,
    weights: ScoreWeights | None = None,
    server_load: float = 0.0,
) -> float:
    """Return a bounded score for one IP record.

    Special cases:

    * ``EXHAUSTED`` before ``reset_at`` -> 0 (must not be selected);
    * ``EXHAUSTED`` after ``reset_at`` -> neutral 40 until retested;
    * ``ERROR`` in cooldown -> 5; after cooldown -> 30 until retested.
    """

    if record.state is IpState.RETIRED:
        return 0.0
    if record.state is IpState.EXHAUSTED:
        if record.reset_at is not None and now < record.reset_at:
            return 0.0
        return 40.0
    if record.state is IpState.ERROR:
        if record.error_until is not None and now < record.error_until:
            return 5.0
        return 30.0

    w = (weights or ScoreWeights()).normalized()
    tests = max(0, int(record.tests))
    successes = min(max(0, int(record.successes)), tests)
    availability = (successes + 1.0) / (tests + 2.0)

    if record.latency_ms is None or w.latency_budget_ms <= 0:
        latency_score = 0.5
    else:
        latency_score = 1.0 - min(1.0, max(0.0, record.latency_ms) / w.latency_budget_ms)

    age_h = _age_hours(record.last_tested_at or record.last_ok_at, now)
    if age_h is None or w.freshness_ttl_h <= 0:
        freshness = 0.5
    else:
        freshness = math.exp(-age_h / w.freshness_ttl_h)

    raw = 100.0 * (
        w.availability * availability
        + w.latency * latency_score
        + w.freshness * freshness
    )
    penalty = min(max(0.0, w.load_penalty_max), max(0.0, server_load) / 10.0)
    return max(0.0, min(100.0, raw - penalty))
