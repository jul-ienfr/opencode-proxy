"""Idempotent ingestion of Gluetun control plane events into ``registry.db``.

The fork is the source of truth for tunnel events, but the registry stays the
only writer of claims, leases and scores.  This module therefore never creates
a claim or a lease: it applies each ``(station, seq)`` pair exactly once to the
append-only observation/incident/repair tables and persists a cursor so a
restart resumes where the previous process stopped.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from free.ip_registry.registry_db import (
    ingest_control_events,
    load_control_cursor,
)

from .client import GluetunControlClient
from .models import ControlEvent

_ALLOWED_TRANSPORTS = frozenset({"wireguard", "openvpn-udp", "openvpn-tcp"})
_DEFAULT_TRANSPORT = "wireguard"
_DEFAULT_REPAIR_TTL_S = 3600.0
_DEFAULT_REPAIR_THRESHOLD = 2


@dataclass(frozen=True, slots=True)
class EventPollResult:
    cursor: int = 0
    last_seq: int = 0
    ingested: int = 0
    duplicates: int = 0
    skipped: int = 0
    gap: bool = False

    @property
    def advanced(self) -> bool:
        return self.last_seq > self.cursor


class EventIngestor:
    """Poll ``GET /v1/events`` and apply each new event once."""

    def __init__(
        self,
        client: GluetunControlClient,
        *,
        station: int,
        db_path: str | None = None,
        repair_ttl_s: float = _DEFAULT_REPAIR_TTL_S,
        repair_threshold: int = _DEFAULT_REPAIR_THRESHOLD,
        now: Any = None,
    ) -> None:
        self._client = client
        self._station = int(station)
        self._db_path = db_path
        self._repair_ttl_s = max(0.0, float(repair_ttl_s))
        self._repair_threshold = max(1, int(repair_threshold))
        self._now = now or time.time

    def cursor(self) -> int:
        return load_control_cursor(self._station, db_path=self._db_path)

    def poll(self, *, limit: int = 100) -> EventPollResult:
        cursor = self.cursor()
        page = self._client.events_page(since=cursor, limit=limit)
        fresh = sorted(
            (event for event in page.items if event.seq > cursor),
            key=lambda event: event.seq,
        )
        if not fresh:
            return EventPollResult(cursor=cursor, last_seq=cursor, gap=page.gap)

        now = float(self._now())
        events = [_event_payload(event, now) for event in fresh]
        applied = ingest_control_events(
            self._station,
            events,
            apply=self._apply,
            now=now,
            db_path=self._db_path,
        )
        return EventPollResult(
            cursor=cursor,
            last_seq=int(applied["last_seq"]),
            ingested=int(applied["ingested"]),
            duplicates=int(applied["duplicates"]),
            skipped=int(applied["skipped"]),
            gap=page.gap,
        )

    def _apply(
        self,
        conn: sqlite3.Connection,
        station: int,
        event: dict[str, Any],
    ) -> None:
        apply_control_event(
            conn,
            station,
            event,
            repair_ttl_s=self._repair_ttl_s,
            repair_threshold=self._repair_threshold,
        )


def apply_control_event(
    conn: sqlite3.Connection,
    station: int,
    event: dict[str, Any],
    *,
    repair_ttl_s: float = _DEFAULT_REPAIR_TTL_S,
    repair_threshold: int = _DEFAULT_REPAIR_THRESHOLD,
) -> None:
    """Map one new fork event onto the registry tables."""

    event_type = str(event.get("type", "") or "")
    ts = _parse_ts(event.get("ts"), 0.0)
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    data = dict(data or {})
    job_id = str(event.get("job_id", "") or "")

    if event_type in ("job.committed", "switch.noop"):
        _insert_observation(
            conn,
            station,
            data=data,
            status=200,
            valid=1,
            validation=event_type,
            tested_at=ts,
        )
        return

    if event_type == "job.rolled_back":
        _insert_observation(
            conn,
            station,
            data=data,
            status=0,
            valid=0,
            validation=event_type,
            tested_at=ts,
            error=str(data.get("reason", "") or data.get("error", "") or event_type),
        )
        if _event_count(conn, station, event_type) >= repair_threshold:
            _repair_request(
                conn,
                station,
                reason=f"{event_type}.repeated",
                ts=ts,
                ttl_s=repair_ttl_s,
                data=data,
            )
        return

    if event_type == "job.failed_degraded":
        _insert_incident(
            conn,
            station,
            scope="station",
            cause=event_type,
            severity="critical",
            ts=ts,
            data=data,
            job_id=job_id,
            remediation="station repair",
        )
        return

    if event_type == "job.exhausted":
        _repair_request(
            conn,
            station,
            reason=event_type,
            ts=ts,
            ttl_s=repair_ttl_s,
            data=data,
        )
        return

    if event_type == "egress.verified":
        _insert_observation(
            conn,
            station,
            data=data,
            status=200 if data.get("verified", True) else 0,
            valid=1 if data.get("verified", True) else 0,
            validation=event_type,
            tested_at=ts,
            error="" if data.get("verified", True) else str(data.get("reason", "") or event_type),
        )
        return

    if event_type in ("transport.connected", "transport.failed"):
        connected = event_type == "transport.connected"
        _insert_observation(
            conn,
            station,
            data=data,
            status=200 if connected else 0,
            valid=1 if connected else 0,
            validation="transport_attempt",
            tested_at=ts,
            error="" if connected else str(data.get("reason", "") or data.get("error", "") or event_type),
        )
        return

    if event_type in ("transport.cycle_exhausted", "tunnel.repair_needed"):
        _repair_request(
            conn,
            station,
            reason=f"{event_type}:priority=high",
            ts=ts,
            ttl_s=repair_ttl_s,
            data=data,
        )
        return

    if event_type == "dns.tier_down":
        _insert_incident(
            conn,
            station,
            scope="dns",
            cause=event_type,
            severity="warning",
            ts=ts,
            data=data,
            job_id=job_id,
            remediation="dns failover",
        )
        return

    if event_type == "dns.ladder_exhausted":
        _insert_incident(
            conn,
            station,
            scope="dns",
            cause=event_type,
            severity="critical",
            ts=ts,
            data=data,
            job_id=job_id,
            remediation="dns failover",
        )
        return

    if event_type in ("state.drift", "state.invalid"):
        _insert_incident(
            conn,
            station,
            scope="state",
            cause=event_type,
            severity="warning" if event_type == "state.drift" else "error",
            ts=ts,
            data=data,
            job_id=job_id,
            remediation="operator review",
        )


def _event_payload(event: ControlEvent, fallback_ts: float) -> dict[str, Any]:
    return {
        "seq": int(event.seq),
        "type": str(event.type),
        "ts": _parse_ts(event.ts, fallback_ts),
        "job_id": str(event.job_id),
        "severity": str(event.severity),
        "data": dict(event.data),
    }


def _parse_ts(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return fallback
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _event_count(conn: sqlite3.Connection, station: int, event_type: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM control_events WHERE station=? AND event_type=?",
        (int(station), str(event_type)),
    ).fetchone()
    return int(row[0] or 0)


def _insert_observation(
    conn: sqlite3.Connection,
    station: int,
    *,
    data: dict[str, Any],
    status: int,
    valid: int,
    validation: str,
    tested_at: float,
    error: str = "",
) -> None:
    hostname = _text(data, "hostname", "server", "target_hostname")
    ip = _text(data, "ip", "egress_ip", "observed_ip") or None
    transport = _transport(data)
    latency = _number(data, "latency_ms", "latency")
    conn.execute(
        "INSERT INTO observations(station, hostname, transport, ip, provider, "
        "model, status, valid, validation, latency_ms, error, tested_at) "
        "VALUES(?,?,?,?,'','',?,?,?,?,?,?)",
        (
            int(station),
            hostname,
            transport,
            ip,
            int(status),
            int(valid),
            validation,
            latency,
            error,
            float(tested_at),
        ),
    )


def _insert_incident(
    conn: sqlite3.Connection,
    station: int,
    *,
    scope: str,
    cause: str,
    severity: str,
    ts: float,
    data: dict[str, Any],
    job_id: str,
    remediation: str,
) -> None:
    details = json.dumps(
        {
            "severity": severity,
            "event": cause,
            "job_id": job_id,
            "data": data,
        },
        sort_keys=True,
    )
    conn.execute(
        "INSERT INTO incidents(scope, station, started_at, cause, remediation, details) VALUES(?,?,?,?,?,?)",
        (scope, int(station), float(ts), cause, remediation, details),
    )


def _repair_request(
    conn: sqlite3.Connection,
    station: int,
    *,
    reason: str,
    ts: float,
    ttl_s: float,
    data: dict[str, Any],
) -> None:
    conn.execute(
        "INSERT INTO repair_requests(station, reason, model, ip, hostname, "
        "requested_at, expires_at, status) VALUES(?,?,?,?,?,?,?,'pending') "
        "ON CONFLICT(station) DO UPDATE SET reason=excluded.reason, "
        "model=excluded.model, ip=excluded.ip, hostname=excluded.hostname, "
        "requested_at=excluded.requested_at, expires_at=excluded.expires_at, "
        "status='pending', claimed_at=NULL",
        (
            int(station),
            reason,
            _text(data, "model"),
            _text(data, "ip", "egress_ip") or None,
            _text(data, "hostname", "server") or None,
            float(ts),
            float(ts) + max(0.0, float(ttl_s)),
        ),
    )


def _transport(data: dict[str, Any]) -> str:
    value = _text(data, "transport", "transport_effective")
    if value in _ALLOWED_TRANSPORTS:
        return value
    return _DEFAULT_TRANSPORT


def _text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _number(data: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = data.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


__all__ = [
    "EventIngestor",
    "EventPollResult",
    "apply_control_event",
]
