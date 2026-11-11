"""NordVPN adapter: stations, observed egress IPs, AUTH and quota.

The catalog knows more IPs than simultaneous sessions.  It never opens a
session and never exceeds ``max_active_stations``; the manager remains the
only owner of the session lifecycle.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Iterable
from typing import Any

from free.egress_adapters.base import RefreshResult
from free.egress_catalog import (
    Observation,
    ObservationPhase,
    OptionStatus,
    Provider,
    ProviderPolicy,
    new_option,
)
from free.egress_store import EgressStore
from free.egress_validation import ProbeOutcome, classify_outcome


class NordVpnEgressAdapter:
    provider = str(Provider.NORDVPN)

    def __init__(
        self,
        stations: Iterable[dict[str, Any]] | Callable[[], Iterable[dict[str, Any]]],
        *,
        probe=None,
        max_active_stations: int = 6,
    ) -> None:
        self._stations = stations
        self._probe = probe
        self.max_active_stations = max(1, int(max_active_stations))

    def _rows(self) -> list[dict[str, Any]]:
        raw = self._stations() if callable(self._stations) else self._stations
        return [dict(row) for row in raw if isinstance(row, dict)]

    async def refresh(
        self,
        store: EgressStore,
        policy: ProviderPolicy,
        *,
        run_id: str = "",
        max_candidates: int | None = None,
        observe_only: bool = False,
    ) -> RefreshResult:
        result = RefreshResult(provider=self.provider, observe_only=observe_only)
        rows = self._rows()[: max(1, int(max_candidates or policy.max_options or 10))]
        result.candidates = len(rows)
        if not rows:
            result.errors.append("aucune station NordVPN observée")
            return result
        active = 0
        seen: set[str] = set()
        planned: list[tuple[Any, str]] = []
        for row in rows:
            station = str(row.get("station") or row.get("name") or "").strip()
            server = str(row.get("server") or row.get("hostname") or "").strip()
            egress = str(row.get("egress_ip") or "").strip()
            status = str(row.get("status") or "").strip().lower()
            auth_failed = bool(row.get("auth_failed", False)) or status in (
                "auth_failed",
                "auth",
                "unauthorized",
            )
            if not station:
                continue
            key_unit = egress or f"{station}:{server}"
            if key_unit in seen:
                continue
            seen.add(key_unit)
            option = new_option(
                provider=self.provider,
                kind="nordvpn_station",
                station=station,
                host=server,
                egress_ip=egress,
                source_ids=("nordvpn",),
                meta={"server": server, "status": status},
                now=time.time(),
            )
            if auth_failed:
                store.apply(
                    option,
                    Observation(
                        phase=ObservationPhase.AUTH,
                        status=401,
                        result=str(OptionStatus.AUTH),
                        observed_at=time.time(),
                        message="AUTH_FAILED: station exclue jusqu'à réparation",
                        run_id=run_id,
                    ),
                    policy,
                    run_id=run_id,
                )
                result.quarantined += 1
                continue
            if not egress:
                store.apply(
                    option,
                    Observation(
                        phase=ObservationPhase.EGRESS,
                        status=0,
                        result=str(OptionStatus.DEAD),
                        observed_at=time.time(),
                        message="station sans egress observé",
                        run_id=run_id,
                    ),
                    policy,
                    run_id=run_id,
                )
                result.dead += 1
                continue
            active += 1
            if active > self.max_active_stations:
                # The catalog may know it, but no new session is opened.
                store.apply(
                    option,
                    Observation(
                        phase=ObservationPhase.LIVENESS,
                        status=200 if status == "connected" else 0,
                        result=str(
                            OptionStatus.REACHABLE
                            if status == "connected"
                            else OptionStatus.DEAD
                        ),
                        observed_at=time.time(),
                        message="session cap: observation seulement, pas de nouvelle session",
                        run_id=run_id,
                    ),
                    policy,
                    run_id=run_id,
                )
                continue
            planned.append((option, status))

        semaphore = asyncio.Semaphore(max(1, int(policy.concurrency or 1)))

        async def _limited(option):
            async with semaphore:
                return await self._probe_one(option)

        outcomes = await asyncio.gather(
            *(_limited(option) for option, _status in planned),
            return_exceptions=True,
        )
        for (option, status), outcome in zip(planned, outcomes, strict=False):
            if isinstance(outcome, BaseException):
                outcome = ProbeOutcome(status=0, error=f"{type(outcome).__name__}: {outcome}")
            if outcome is None:
                store.apply(
                    option,
                    Observation(
                        phase=ObservationPhase.LIVENESS,
                        status=200 if status == "connected" else 0,
                        result=str(
                            OptionStatus.REACHABLE
                            if status == "connected"
                            else OptionStatus.DEAD
                        ),
                        observed_at=time.time(),
                        message="free gate not injected",
                        run_id=run_id,
                    ),
                    policy,
                    run_id=run_id,
                )
                continue
            classified, reset_at, message = classify_outcome(
                outcome,
                phase=ObservationPhase.FREE_GATE,
                now=time.time(),
            )
            applied = store.apply(
                option,
                Observation(
                    phase=ObservationPhase.FREE_GATE,
                    status=int(outcome.status or 0),
                    result=classified,
                    observed_at=time.time(),
                    latency_ms=outcome.latency_ms,
                    message=message,
                    run_id=run_id,
                ),
                policy,
                reset_at=reset_at,
                run_id=run_id,
            )
            if applied.status == OptionStatus.USABLE:
                result.promoted += 1
            elif applied.status in (OptionStatus.QUOTA, OptionStatus.AUTH):
                result.quarantined += 1
            else:
                result.dead += 1
        return result

    async def _probe_one(self, option) -> ProbeOutcome | None:
        if self._probe is None:
            return None
        try:
            value = self._probe(option)
            if asyncio.iscoroutine(value):
                value = await value
        except Exception as exc:
            return ProbeOutcome(status=0, error=f"{type(exc).__name__}: {exc}")
        return value if isinstance(value, ProbeOutcome) else None


__all__ = ["NordVpnEgressAdapter"]
