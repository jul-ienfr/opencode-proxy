"""WARP adapter: containers and observed ``/64`` blocks.

The catalog never creates containers.  It records each observed block, its
container, its TTL and its free-gate state; a recreated container expires the
old block instead of reusing it.
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


class WarpEgressAdapter:
    provider = str(Provider.WARP)

    def __init__(
        self,
        containers: Iterable[dict[str, Any]] | Callable[[], Iterable[dict[str, Any]]],
        *,
        probe=None,
    ) -> None:
        self._containers = containers
        self._probe = probe

    def _rows(self) -> list[dict[str, Any]]:
        raw = self._containers() if callable(self._containers) else self._containers
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
        rows = self._rows()
        cap = max(1, int(max_candidates or policy.max_options or len(rows) or 1))
        rows = rows[:cap]
        result.candidates = len(rows)
        if not rows:
            result.errors.append("aucun conteneur WARP observé")
            return result
        seen_blocks: dict[str, str] = {}
        planned: list[tuple[dict[str, Any], Any]] = []
        for row in rows:
            if not bool(row.get("enabled", True)):
                continue
            container = str(row.get("container") or row.get("station") or "").strip()
            egress = str(row.get("egress_ip") or "").strip()
            block = str(row.get("block") or "").strip()
            if not container or not egress:
                result.errors.append(f"conteneur WARP incomplet: {container or '?'}")
                continue
            option = new_option(
                provider=self.provider,
                kind="warp_container",
                station=container,
                host=container,
                egress_ip=egress,
                block=block,
                source_ids=("warp",),
                meta={"healthy": bool(row.get("healthy", True)), "ipv6": ":" in egress},
                now=time.time(),
            )
            unit = option.block or egress
            if unit in seen_blocks:
                continue
            seen_blocks[unit] = container
            planned.append((row, option))

        semaphore = asyncio.Semaphore(max(1, int(policy.concurrency or 1)))

        async def _limited(option):
            async with semaphore:
                return await self._probe_one(option)

        outcomes = await asyncio.gather(
            *(_limited(option) for _row, option in planned),
            return_exceptions=True,
        )
        for (row, option), outcome in zip(planned, outcomes, strict=False):
            if isinstance(outcome, BaseException):
                outcome = ProbeOutcome(status=0, error=f"{type(outcome).__name__}: {outcome}")
            if outcome is None:
                # No probe injected: a healthy container is only reachable,
                # never usable.  This keeps the free gate authoritative.
                store.apply(
                    option,
                    Observation(
                        phase=ObservationPhase.LIVENESS,
                        status=200 if bool(row.get("healthy", True)) else 0,
                        result=str(
                            OptionStatus.REACHABLE
                            if bool(row.get("healthy", True))
                            else OptionStatus.DEAD
                        ),
                        observed_at=time.time(),
                        message="healthy container, free gate not injected",
                        run_id=run_id,
                    ),
                    policy,
                    run_id=run_id,
                )
                continue
            classified, reset_at, message = classify_outcome(
                outcome,
                phase=ObservationPhase.FREE_GATE,
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
        self._expire_recreated(store, policy, seen_blocks, run_id)
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

    def _expire_recreated(
        self,
        store: EgressStore,
        policy: ProviderPolicy,
        seen_blocks: dict[str, str],
        run_id: str,
    ) -> None:
        """A container that now reports another block retires the old one."""

        for option in store.list_options(provider=self.provider, limit=500):
            if option.station not in seen_blocks.values():
                continue
            current = seen_blocks.get(option.block)
            if current is None:
                store.apply(
                    option,
                    Observation(
                        phase=ObservationPhase.EGRESS,
                        status=0,
                        result=str(OptionStatus.DEAD),
                        observed_at=time.time(),
                        message="bloc WARP remplacé par une recréation de conteneur",
                        run_id=run_id,
                    ),
                    policy,
                    run_id=run_id,
                )


__all__ = ["WarpEgressAdapter"]
