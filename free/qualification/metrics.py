"""Prometheus/JSON metrics for the qualification control plane.

The metrics are derived from the read-only projections; no metric write touches
the queue.  Values are intentionally flat so a scrape never has to know the
storage layout.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from . import readmodel


def snapshot(path: str | Path) -> dict[str, Any]:
    now = time.time()
    summary = readmodel.summary(path)
    transports = readmodel.transports(path)
    quota = readmodel.quota(path)
    stations = readmodel.stations(path)
    epochs = readmodel.epochs(path, limit=1)
    watchdog = readmodel.watchdog(path)
    discovery = readmodel.discovery(path, limit=1)
    repairs = readmodel.repairs(path, limit=200)
    total = int(summary.get("total") or 0)
    tested = int(summary.get("tested") or 0)
    items = list(transports.get("items") or [])
    station_items = list(stations.get("items") or [])
    usable_stations = sum(1 for item in station_items if item.get("client_usable"))
    epoch_items = list(epochs.get("items") or [])
    if epoch_items and epoch_items[0].get("finished_at"):
        epoch_duration = float(epoch_items[0]["finished_at"]) - float(
            epoch_items[0].get("started_at") or 0.0
        )
    else:
        epoch_duration = 0.0
    heartbeat = watchdog.get("heartbeat") or {}
    heartbeat_at = float(heartbeat.get("heartbeat_at") or 0.0)
    discovery_items = list(discovery.get("items") or [])
    return {
        "qualification_tested": tested,
        "qualification_total": total,
        "qualification_attempts": int(summary.get("tests") or 0),
        "qualification_usable": int(summary.get("usable") or 0),
        "qualification_quota": int(summary.get("quota") or 0),
        "qualification_pending": int(summary.get("pending") or 0),
        "qualification_infra": int(summary.get("infra") or 0),
        "qualification_dead": int(summary.get("dead") or 0),
        "qualification_skipped": int(summary.get("skipped") or 0),
        "qualification_skipped_dedicated": int(summary.get("dedicated") or 0),
        "qualification_skipped_unsupported": int(
            summary.get("unsupported_transport") or 0
        ),
        "qualification_paused": int(summary.get("paused") or 0),
        "qualification_200": sum(int(item.get("successes") or 0) for item in items),
        "qualification_429": sum(int(item.get("quota") or 0) for item in items),
        "qualification_queue_depth": int(summary.get("pending") or 0),
        "qualification_coverage_ratio": (tested / total) if total else 0.0,
        "qualification_epoch_duration_s": epoch_duration,
        "qualification_transports_degraded": sum(
            1 for item in items if item.get("degraded")
        ),
        "qualification_transports_healthy": sum(
            1 for item in items if item.get("health") == "healthy"
        ),
        "qualification_quota_429": int((quota.get("summary") or {}).get("quota_429") or 0),
        "qualification_stations_total": len(station_items),
        "qualification_stations_usable": usable_stations,
        "qualification_station_availability": (
            usable_stations / len(station_items) if station_items else 0.0
        ),
        "watchdog_incidents": len(watchdog.get("incidents") or []),
        "watchdog_heartbeat_age_s": (now - heartbeat_at) if heartbeat_at else 0.0,
        "discovery_runs": len(discovery_items),
        "discovery_last_servers": int(
            discovery_items[0].get("server_count") or 0
        )
        if discovery_items
        else 0,
        "repair_jobs": len(repairs.get("items") or []),
    }


def render_prometheus(values: dict[str, Any]) -> str:
    lines: list[str] = []
    for name, value in values.items():
        metric = name if name.startswith("qualification_") else f"qualification_{name}"
        if isinstance(value, bool):
            value = int(value)
        lines.append(f"# TYPE {metric} gauge")
        lines.append(f"{metric} {value}")
    return "\n".join(lines) + "\n"


__all__ = ["render_prometheus", "snapshot"]
