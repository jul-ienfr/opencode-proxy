"""Discovery scheduler for the live NordVPN catalog.

One run reads the API cache, upserts candidates/transports, marks servers that
disappeared as ``skipped``/``removed-from-catalog``, and records a
``discovery_runs`` row.  A failed/empty read never empties the queue: the
previous catalog stays readable and the failure is exposed.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .source import NordVpnCatalogSource
from .store import QualificationStore

_LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class DiscoveryConfig:
    interval_s: float = 21600.0
    stale_max_s: float = 86400.0
    source_id: str = "nordvpn_api_servers"


@dataclass
class DiscoveryReport:
    started_at: float
    finished_at: float = 0.0
    status: str = "running"
    server_count: int = 0
    transport_count: int = 0
    stale: bool = False
    removed: int = 0
    error: str = ""
    run_id: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "server_count": self.server_count,
            "transport_count": self.transport_count,
            "stale": self.stale,
            "removed": self.removed,
            "error": self.error,
        }


class DiscoveryService:
    def __init__(
        self,
        store: QualificationStore,
        source: NordVpnCatalogSource,
        *,
        config: DiscoveryConfig | None = None,
        clock=time.time,
    ) -> None:
        self.store = store
        self.source = source
        self.config = config or DiscoveryConfig()
        self._clock = clock

    def is_due(self, *, now: float | None = None) -> bool:
        ts = float(self._clock() if now is None else now)
        last = self.store.last_discovery_run()
        if last is None:
            return True
        return ts - float(last["started_at"] or 0.0) >= max(60.0, float(self.config.interval_s))

    def run(self, *, now: float | None = None, force: bool = False) -> DiscoveryReport:
        ts = float(self._clock() if now is None else now)
        report = DiscoveryReport(started_at=ts)
        if not force and not self.is_due(now=ts):
            report.status = "skipped"
            report.finished_at = ts
            report.run_id = self.store.record_discovery_run(
                source=self.config.source_id,
                started_at=ts,
                finished_at=ts,
                status="skipped",
                server_count=0,
                transport_count=0,
            )
            return report

        try:
            rows = self.source.load()
        except Exception as exc:  # fail-soft: keep the previous catalog
            report.status = "failed"
            report.error = f"{type(exc).__name__}: {exc}"
            report.finished_at = self._clock()
            report.run_id = self.store.record_discovery_run(
                source=self.config.source_id,
                started_at=ts,
                finished_at=report.finished_at,
                status="failed",
                server_count=0,
                transport_count=0,
                error=report.error,
            )
            _LOG.warning(
                "qualification.discovery.failed",
                extra={
                    "source": self.config.source_id,
                    "error": report.error,
                    "started_at": ts,
                },
            )
            return report

        if not rows:
            report.status = "failed"
            report.error = "empty-catalog"
            report.finished_at = self._clock()
            report.run_id = self.store.record_discovery_run(
                source=self.config.source_id,
                started_at=ts,
                finished_at=report.finished_at,
                status="failed",
                server_count=0,
                transport_count=0,
                error=report.error,
            )
            _LOG.warning(
                "qualification.discovery.empty",
                extra={"source": self.config.source_id, "started_at": ts},
            )
            return report

        stats = self.source.stats(rows)
        candidates = self.source.candidates(rows, now=ts)
        self.store.upsert_candidates(candidates, now=ts)
        self.store.upsert_server_transports(candidates)
        removed = self.store.mark_missing_hosts(
            (candidate.hostname for candidate in candidates),
            reason="removed-from-catalog",
            now=ts,
        )
        fetched_at = self._catalog_fetched_at()
        stale = bool(fetched_at and ts - fetched_at > max(60.0, float(self.config.stale_max_s)))
        report.status = "stale" if stale else "ok"
        report.server_count = int(stats.online)
        report.transport_count = int(stats.with_transports)
        report.stale = stale
        report.removed = int(removed)
        report.finished_at = self._clock()
        report.run_id = self.store.record_discovery_run(
            source=self.config.source_id,
            started_at=ts,
            finished_at=report.finished_at,
            status=report.status,
            server_count=report.server_count,
            transport_count=report.transport_count,
            stale=stale,
            catalog_fetched_at=fetched_at,
        )
        _LOG.info(
            "qualification.discovery.ok",
            extra={
                "source": self.config.source_id,
                "status": report.status,
                "servers": report.server_count,
                "transports": report.transport_count,
                "removed": report.removed,
                "stale": stale,
            },
        )
        return report

    def _catalog_fetched_at(self) -> float | None:
        try:
            return float(Path(self.source.path).stat().st_mtime)
        except OSError:
            return None


__all__ = ["DiscoveryConfig", "DiscoveryReport", "DiscoveryService"]
