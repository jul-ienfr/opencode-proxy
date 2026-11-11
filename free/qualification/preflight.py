"""Operator-relative preflight for the qualification queue.

Uses the existing :class:`free.ip_registry.preflight.PreflightProber` through a
small protocol so tests can inject a fake.  A preflight failure is an
infrastructure signal: the hostname stays pending with a bounded backoff, it is
never marked dead.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from typing import Any, Protocol

from .domain import Candidate
from .scoring import PotentialWeights, potential_score
from .store import QualificationStore


class PreflightProberLike(Protocol):
    async def preflight_many(
        self,
        candidates: Any,
        transport: str,
        *,
        concurrency: int = 64,
        timeout: float = 3.0,
        budget_s: float = 30.0,
        port: int | None = None,
    ) -> list[Any]: ...


@dataclass(frozen=True)
class PreflightReport:
    requested: int = 0
    ok: int = 0
    failed: int = 0
    errors: dict[str, int] | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "ok": self.ok,
            "failed": self.failed,
            "errors": dict(self.errors or {}),
        }


@dataclass(frozen=True)
class _MergedResult:
    hostname: str
    ok: bool
    dns_ms: float = 0.0
    connect_ms: float = 0.0
    handshake_ms: float = 0.0
    rtt_ms: float = 0.0
    endpoint_ip: str = ""
    error: str = ""


def _median(values: list[float]) -> float:
    clean = [float(value) for value in values if float(value or 0.0) > 0.0]
    return float(statistics.median(clean)) if clean else 0.0


class PreflightService:
    def __init__(
        self,
        store: QualificationStore,
        prober: PreflightProberLike,
        *,
        weights: PotentialWeights | None = None,
        concurrency: int = 6,
        timeout_s: float = 5.0,
        budget_s: float = 60.0,
        backoff_s: float = 300.0,
        samples: int = 3,
        clock=None,
    ) -> None:
        self.store = store
        self.prober = prober
        self.weights = weights or PotentialWeights()
        self.concurrency = max(1, int(concurrency))
        self.timeout_s = max(0.5, float(timeout_s))
        self.budget_s = max(1.0, float(budget_s))
        self.backoff_s = max(1.0, float(backoff_s))
        self.samples = max(1, int(samples))
        self._clock = clock

    def _now(self) -> float:
        if self._clock is None:
            import time

            return time.time()
        return float(self._clock())

    async def run(
        self,
        *,
        limit: int = 500,
        transport: str = "wireguard",
        now: float | None = None,
    ) -> PreflightReport:
        ts = self._now() if now is None else float(now)
        candidates = self.store.candidates_for_preflight(limit=limit, transport=transport)
        if not candidates:
            return PreflightReport()
        by_host = {candidate.hostname.lower(): candidate for candidate in candidates}
        rows = [{"host": candidate.hostname, "country": candidate.country} for candidate in candidates]
        try:
            results = await self._sample(rows, transport)
        except Exception as exc:  # fail-soft: preflight is an optimization
            for candidate in candidates:
                self.store.set_preflight_error(
                    candidate.hostname,
                    error=f"{type(exc).__name__}: {exc}",
                    next_eligible_at=ts + self.backoff_s,
                    now=ts,
                )
            return PreflightReport(requested=len(candidates), failed=len(candidates), errors={type(exc).__name__: len(candidates)})

        ok = 0
        failed = 0
        errors: dict[str, int] = {}
        for result in results:
            hostname = str(getattr(result, "hostname", "") or "").strip().lower()
            candidate_opt: Candidate | None = by_host.get(hostname)
            if candidate_opt is None:
                continue
            candidate = candidate_opt
            if bool(getattr(result, "ok", False)):
                rtt = float(
                    getattr(result, "rtt_ms", 0.0)
                    or getattr(result, "connect_ms", 0.0)
                    or getattr(result, "dns_ms", 0.0)
                    or 0.0
                )
                updated = candidate.with_updates(
                    rtt_ms=rtt,
                    meta={
                        **dict(candidate.meta or {}),
                        "dns_ms": float(getattr(result, "dns_ms", 0.0) or 0.0),
                        "connect_ms": float(getattr(result, "connect_ms", 0.0) or 0.0),
                        "handshake_ms": float(getattr(result, "handshake_ms", 0.0) or 0.0),
                        "endpoint_ip": str(getattr(result, "endpoint_ip", "") or ""),
                    },
                )
                score = potential_score(updated, now=ts, weights=self.weights)
                self.store.set_preflight(
                    candidate.hostname,
                    rtt_ms=rtt,
                    dns_ms=float(getattr(result, "dns_ms", 0.0) or 0.0),
                    connect_ms=float(getattr(result, "connect_ms", 0.0) or 0.0),
                    handshake_ms=float(getattr(result, "handshake_ms", 0.0) or 0.0),
                    endpoint_ip=str(getattr(result, "endpoint_ip", "") or ""),
                    potential_score=score,
                    now=ts,
                )
                ok += 1
            else:
                error = str(getattr(result, "error", "") or "preflight-failed")
                key = error.split(":", 1)[0] or "unknown"
                errors[key] = errors.get(key, 0) + 1
                self.store.set_preflight_error(
                    candidate.hostname,
                    error=error,
                    next_eligible_at=ts + self.backoff_s,
                    now=ts,
                )
                failed += 1
        return PreflightReport(requested=len(candidates), ok=ok, failed=failed, errors=errors)

    async def _sample(self, rows: list[dict[str, Any]], transport: str) -> list[Any]:
        if self.samples <= 1:
            return await self.prober.preflight_many(
                rows,
                transport,
                concurrency=self.concurrency,
                timeout=self.timeout_s,
                budget_s=self.budget_s,
            )
        collected: dict[str, list[Any]] = {}
        for _ in range(self.samples):
            results = await self.prober.preflight_many(
                rows,
                transport,
                concurrency=self.concurrency,
                timeout=self.timeout_s,
                budget_s=self.budget_s,
            )
            for result in results:
                hostname = str(getattr(result, "hostname", "") or "").strip().lower()
                if hostname:
                    collected.setdefault(hostname, []).append(result)
        merged: list[_MergedResult] = []
        for hostname, samples in collected.items():
            ok_samples = [item for item in samples if bool(getattr(item, "ok", False))]
            ok = len(ok_samples) * 2 > len(samples)
            formatted = ok_samples if ok_samples else samples
            error = ""
            if not ok:
                error = next(
                    (
                        str(getattr(item, "error", "") or "")
                        for item in samples
                        if not bool(getattr(item, "ok", False))
                    ),
                    "preflight-failed",
                )
            merged.append(
                _MergedResult(
                    hostname=hostname,
                    ok=ok,
                    dns_ms=_median(
                        [float(getattr(item, "dns_ms", 0.0) or 0.0) for item in formatted]
                    ),
                    connect_ms=_median(
                        [float(getattr(item, "connect_ms", 0.0) or 0.0) for item in formatted]
                    ),
                    handshake_ms=_median(
                        [float(getattr(item, "handshake_ms", 0.0) or 0.0) for item in formatted]
                    ),
                    rtt_ms=_median(
                        [float(getattr(item, "rtt_ms", 0.0) or 0.0) for item in formatted]
                    ),
                    endpoint_ip=next(
                        (
                            str(getattr(item, "endpoint_ip", "") or "")
                            for item in ok_samples
                            if getattr(item, "endpoint_ip", "")
                        ),
                        "",
                    ),
                    error=error,
                )
            )
        return merged


__all__ = ["PreflightProberLike", "PreflightReport", "PreflightService"]
