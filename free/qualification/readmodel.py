"""Read-only projections for the qualification dashboard/API.

The dashboard must never write the queue or block on the sweep.  These helpers
open ``registry.db`` in read-only URI mode, degrade to empty payloads when the
qualification tables do not exist yet, and return plain JSON-safe dicts.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Callable
from functools import wraps
from pathlib import Path
from typing import Any

from .diagnostics import classify_diagnostic

_CACHE_TTL_S = 5.0
_CACHE_MAX_ENTRIES = 256
_cache: OrderedDict[tuple[Any, ...], tuple[float, Any]] = OrderedDict()
_clock = time.monotonic


def _cache_key(
    name: str,
    path: str | Path,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> tuple[Any, ...]:
    return (
        name,
        str(path),
        tuple(repr(value) for value in args),
        tuple(sorted((key, repr(value)) for key, value in kwargs.items())),
    )


def _cached(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Cache a read projection for five seconds (T056).

    The key includes the database path and every argument, so tests and
    multi-registry deployments never share a payload.  Writers in another
    process are picked up at the next expiry; the API calls ``invalidate``
    after its own mutations.
    """

    @wraps(fn)
    def wrapper(path: str | Path, *args: Any, **kwargs: Any) -> Any:
        key = _cache_key(fn.__name__, path, args, kwargs)
        now = _clock()
        cached = _cache.get(key)
        if cached is not None:
            stored_at, payload = cached
            if now - stored_at < _CACHE_TTL_S:
                _cache.move_to_end(key)
                return payload
            _cache.pop(key, None)
        payload = fn(path, *args, **kwargs)
        _cache[key] = (now, payload)
        if len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)
        return payload

    return wrapper


def invalidate(path: str | Path | None = None) -> None:
    """Drop cached projections, globally or for one registry path."""

    if path is None:
        _cache.clear()
        return
    target = str(path)
    for key in [key for key in _cache if key[1] == target]:
        _cache.pop(key, None)


def cache_info() -> dict[str, Any]:
    """Observable cache state for tests and diagnostics."""

    return {
        "entries": len(_cache),
        "ttl_s": _CACHE_TTL_S,
        "max_entries": _CACHE_MAX_ENTRIES,
    }


def _connect(path: str | Path) -> sqlite3.Connection | None:
    target = Path(path)
    if not target.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn
    except Exception:
        return None


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (str(name),),
        ).fetchone()
        return row is not None
    except Exception:
        return False


def _transports(value: str) -> list[str]:
    try:
        rows = json.loads(value or "[]")
    except Exception:
        return []
    return [str(item) for item in rows if str(item).strip()] if isinstance(rows, list) else []


def _candidate_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "hostname": str(row["hostname"]),
        "country": str(row["country"] or ""),
        "city": str(row["city"] or ""),
        "load": float(row["load"] or 0.0),
        "status": str(row["status"] or ""),
        "transports": _transports(row["transports"]),
        "transport_cursor": str(row["transport_cursor"] or ""),
        "transport_round": int(row["transport_round"] or 0),
        "potential_score": float(row["potential_score"] or 0.0),
        "measured_score": float(row["measured_score"] or 0.0),
        "rtt_ms": float(row["rtt_ms"]) if row["rtt_ms"] is not None else None,
        "quota_remaining": int(row["quota_remaining"]) if row["quota_remaining"] is not None else None,
        "quota_reset_at": float(row["quota_reset_at"]) if row["quota_reset_at"] is not None else None,
        "attempts": int(row["attempts"] or 0),
        "last_attempt_at": float(row["last_attempt_at"]) if row["last_attempt_at"] is not None else None,
        "last_result": str(row["last_result"] or ""),
        "last_egress_ip": str(row["last_egress_ip"] or ""),
        "last_transport": str(row["last_transport"] or ""),
        "next_eligible_at": float(row["next_eligible_at"] or 0.0),
        "skipped_reason": str(row["skipped_reason"] or ""),
        "source_id": str(row["source_id"] or ""),
    }


@_cached
def summary(path: str | Path) -> dict[str, Any]:
    conn = _connect(path)
    if conn is None:
        return {
            "enabled": False,
            "total": 0,
            "by_status": {},
            "by_skipped_reason": {},
            "tested": 0,
            "tests": 0,
        }
    try:
        if not _has_table(conn, "qualification_candidates"):
            return {
                "enabled": False,
                "total": 0,
                "by_status": {},
                "by_skipped_reason": {},
                "tested": 0,
                "tests": 0,
            }
        by_status: dict[str, int] = {}
        for row in conn.execute(
            "SELECT status, COUNT(*) AS n FROM qualification_candidates GROUP BY status"
        ):
            by_status[str(row["status"])] = int(row["n"])
        by_skipped_reason: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT skipped_reason, COUNT(*) AS n
            FROM qualification_candidates
            WHERE status='skipped' AND skipped_reason <> ''
            GROUP BY skipped_reason
            """
        ):
            by_skipped_reason[str(row["skipped_reason"])] = int(row["n"])
        total = sum(by_status.values())
        tests = 0
        tested = 0
        infra = 0
        last = None
        if _has_table(conn, "qualification_attempts"):
            tests = int(conn.execute("SELECT COUNT(*) FROM qualification_attempts").fetchone()[0])
            tested = int(
                conn.execute("SELECT COUNT(DISTINCT hostname) FROM qualification_attempts").fetchone()[0]
            )
            infra = int(
                conn.execute(
                    "SELECT COUNT(*) FROM qualification_attempts WHERE status IN ('no_egress','bind_error','timeout','duplicate')"
                ).fetchone()[0]
            )
            last = conn.execute("SELECT MAX(finished_at) FROM qualification_attempts").fetchone()[0]
        return {
            "enabled": True,
            "total": total,
            "by_status": by_status,
            "by_skipped_reason": by_skipped_reason,
            "tests": tests,
            "tested": tested,
            "infra": infra,
            "last_attempt_at": float(last) if last is not None else None,
            "usable": by_status.get("usable", 0),
            "quota": by_status.get("quota", 0),
            "pending": by_status.get("pending", 0),
            "dead": by_status.get("dead", 0),
            "skipped": by_status.get("skipped", 0),
            "dedicated": by_skipped_reason.get("dedicated", 0),
            "denylisted": by_skipped_reason.get("denylisted", 0),
            "unsupported_transport": by_skipped_reason.get("unsupported-transport", 0),
            "paused": by_skipped_reason.get("operator-paused", 0),
        }
    finally:
        conn.close()


@_cached
def coverage(path: str | Path) -> dict[str, Any]:
    """Country and transport coverage for the operator dashboard."""

    conn = _connect(path)
    if conn is None:
        return {"countries": [], "transports": []}
    try:
        if not _has_table(conn, "qualification_candidates"):
            return {"countries": [], "transports": []}
        pivot: dict[str, dict[str, int]] = {}
        for row in conn.execute(
            """
            SELECT country, status, COUNT(*) AS n
            FROM qualification_candidates
            GROUP BY country, status
            """
        ):
            country = str(row["country"] or "unknown")
            entry = pivot.setdefault(country, {"total": 0})
            entry[str(row["status"])] = int(row["n"])
            entry["total"] += int(row["n"])
        countries: list[dict[str, Any]] = [
            {
                "country": country,
                "total": entry.get("total", 0),
                "usable": entry.get("usable", 0),
                "pending": entry.get("pending", 0),
                "quota": entry.get("quota", 0),
                "dead": entry.get("dead", 0),
                "skipped": entry.get("skipped", 0),
            }
            for country, entry in pivot.items()
        ]
        countries.sort(key=lambda item: (-int(item["total"]), str(item["country"])))
        transports: list[dict[str, Any]] = []
        if _has_table(conn, "server_transports"):
            transports = [
                {
                    "transport": str(row["transport"]),
                    "total": int(row["n"] or 0),
                    "supported": int(row["supported"] or 0),
                }
                for row in conn.execute(
                    """
                    SELECT transport, COUNT(*) AS n, SUM(supported) AS supported
                    FROM server_transports
                    GROUP BY transport ORDER BY transport
                    """
                )
            ]
        return {"countries": countries, "transports": transports}
    finally:
        conn.close()


@_cached
def queue(
    path: str | Path,
    *,
    status: str = "",
    country: str = "",
    transport: str = "",
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    conn = _connect(path)
    if conn is None:
        return {"items": [], "total": 0}
    try:
        if not _has_table(conn, "qualification_candidates"):
            return {"items": [], "total": 0}
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status=?")
            values.append(str(status))
        if country:
            clauses.append("country=?")
            values.append(str(country))
        if transport:
            clauses.append("instr(transports, ?) > 0")
            values.append(f'"{str(transport)}"')
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = int(
            conn.execute(
                f"SELECT COUNT(*) FROM qualification_candidates {where}",
                tuple(values),
            ).fetchone()[0]
        )
        rows = conn.execute(
            f"""
            SELECT * FROM qualification_candidates {where}
            ORDER BY potential_score DESC, COALESCE(quota_remaining,0) DESC,
                     COALESCE(rtt_ms,1e9) ASC, load ASC, hostname ASC
            LIMIT ? OFFSET ?
            """,
            (*values, max(1, int(limit)), max(0, int(offset))),
        ).fetchall()
        return {"items": [_candidate_row(row) for row in rows], "total": total}
    finally:
        conn.close()


@_cached
def next_candidates(path: str | Path, *, limit: int = 20) -> dict[str, Any]:
    conn = _connect(path)
    if conn is None:
        return {"items": [], "reason_empty": "no-db"}
    try:
        if not _has_table(conn, "qualification_candidates"):
            return {"items": [], "reason_empty": "no-table"}
        rows = conn.execute(
            """
            SELECT * FROM qualification_candidates
            WHERE status IN ('pending','quota')
              AND next_eligible_at <= CAST(strftime('%s','now') AS REAL)
              AND (quota_reset_at IS NULL OR quota_reset_at <= CAST(strftime('%s','now') AS REAL))
              AND transports <> '[]'
            ORDER BY
              CASE status WHEN 'pending' THEN 0 ELSE 1 END,
              potential_score DESC,
              COALESCE(quota_remaining,0) DESC,
              COALESCE(rtt_ms,1e9) ASC,
              load ASC,
              hostname ASC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        if not rows:
            return {"items": [], "reason_empty": "all-quota-or-claimed"}
        return {"items": [_candidate_row(row) for row in rows]}
    finally:
        conn.close()


@_cached
def epoch(path: str | Path, epoch_id: str) -> dict[str, Any] | None:
    conn = _connect(path)
    if conn is None:
        return None
    try:
        if not _has_table(conn, "qualification_epochs"):
            return None
        row = conn.execute(
            "SELECT * FROM qualification_epochs WHERE epoch_id=?",
            (str(epoch_id),),
        ).fetchone()
        if row is None:
            return None
        return {
            "epoch_id": str(row["epoch_id"]),
            "provider": str(row["provider"] or ""),
            "status": str(row["status"] or ""),
            "total": int(row["total"] or 0),
            "tested": int(row["tested"] or 0),
            "usable": int(row["usable"] or 0),
            "quota": int(row["quota"] or 0),
            "infra": int(row["infra"] or 0),
            "dead": int(row["dead"] or 0),
            "skipped": int(row["skipped"] or 0),
            "cursor": str(row["cursor"] or ""),
            "started_at": float(row["started_at"] or 0.0),
            "finished_at": (
                float(row["finished_at"])
                if row["finished_at"] is not None
                else None
            ),
            "reason": str(row["reason"] or ""),
        }
    finally:
        conn.close()


@_cached
def epochs(path: str | Path, *, limit: int = 20) -> dict[str, Any]:
    conn = _connect(path)
    if conn is None:
        return {"items": []}
    try:
        if not _has_table(conn, "qualification_epochs"):
            return {"items": []}
        rows = conn.execute(
            "SELECT * FROM qualification_epochs ORDER BY started_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        items = []
        for row in rows:
            started = float(row["started_at"] or 0.0)
            finished = float(row["finished_at"]) if row["finished_at"] is not None else None
            items.append(
                {
                    "epoch_id": str(row["epoch_id"]),
                    "status": str(row["status"] or ""),
                    "total": int(row["total"] or 0),
                    "tested": int(row["tested"] or 0),
                    "usable": int(row["usable"] or 0),
                    "quota": int(row["quota"] or 0),
                    "infra": int(row["infra"] or 0),
                    "dead": int(row["dead"] or 0),
                    "skipped": int(row["skipped"] or 0),
                    "started_at": started,
                    "finished_at": finished,
                    "duration_s": (finished - started) if finished is not None else None,
                    "reason": str(row["reason"] or ""),
                }
            )
        return {"items": items, "total": len(items)}
    finally:
        conn.close()


@_cached
def attempts(
    path: str | Path,
    *,
    hostname: str = "",
    status: str = "",
    epoch_id: str = "",
    limit: int = 50,
) -> dict[str, Any]:
    conn = _connect(path)
    if conn is None:
        return {"items": [], "total": 0}
    try:
        if not _has_table(conn, "qualification_attempts"):
            return {"items": [], "total": 0}
        clauses: list[str] = []
        values: list[Any] = []
        if hostname:
            clauses.append("hostname=?")
            values.append(str(hostname))
        if status:
            clauses.append("status=?")
            values.append(str(status))
        if epoch_id:
            clauses.append("epoch_id=?")
            values.append(str(epoch_id))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            f"SELECT * FROM qualification_attempts {where} ORDER BY started_at DESC LIMIT ?",
            (*values, max(1, int(limit))),
        ).fetchall()
        items = [
            {
                "attempt_id": str(row["attempt_id"]),
                "epoch_id": str(row["epoch_id"] or ""),
                "station": int(row["station"] or 0),
                "hostname": str(row["hostname"]),
                "transport": str(row["transport"] or ""),
                "round": int(row["round"] or 0),
                "status": str(row["status"]),
                "egress_ip": str(row["egress_ip"] or ""),
                "latency_ms": float(row["latency_ms"] or 0.0),
                "rtt_ms": float(row["rtt_ms"] or 0.0),
                "dns_ms": float(row["dns_ms"] or 0.0),
                "connect_ms": float(row["connect_ms"] or 0.0),
                "handshake_ms": float(row["handshake_ms"] or 0.0),
                "endpoint_ip": str(row["endpoint_ip"] or ""),
                "reset_at": float(row["reset_at"] or 0.0),
                "error": str(row["error"] or ""),
                "started_at": float(row["started_at"] or 0.0),
                "finished_at": float(row["finished_at"] or 0.0),
            }
            for row in rows
        ]
        return {"items": items, "total": len(items)}
    finally:
        conn.close()


@_cached
def stations(path: str | Path) -> dict[str, Any]:
    conn = _connect(path)
    if conn is None:
        return {"items": []}
    try:
        if not _has_table(conn, "station_state"):
            return {"items": []}
        runtime_by_station: dict[int, dict[str, Any]] = {}
        if _has_table(conn, "station_runtime"):
            for row in conn.execute("SELECT * FROM station_runtime").fetchall():
                runtime_by_station[int(row["station"])] = {
                    "runtime_state": str(row["state"] or ""),
                    "standby_egress_ip": str(row["standby_egress_ip"] or ""),
                    "standby_transport": str(row["standby_transport"] or ""),
                    "cutover_ms": float(row["cutover_ms"]) if row["cutover_ms"] is not None else None,
                    "availability_1h": float(row["availability_1h"] or 0.0),
                    "availability_24h": float(row["availability_24h"] or 0.0),
                }
        rows = conn.execute("SELECT * FROM station_state ORDER BY station").fetchall()
        return {
            "items": [
                {
                    "station": int(row["station"]),
                    "state": str(row["state"] or ""),
                    "container": str(row["container"] or ""),
                    "transport": str(row["transport_effective"] or ""),
                    "country": str(row["country"] or ""),
                    "current_ip": str(row["current_ip"] or ""),
                    "current_host": str(row["current_host"] or ""),
                    "client_usable": bool(row["client_usable"]),
                    "last_probe_at": float(row["last_probe_at"] or 0.0),
                    "last_200_at": float(row["last_200_at"]) if row["last_200_at"] is not None else None,
                    "reason": str(row["reason"] or ""),
                    **runtime_by_station.get(int(row["station"]), {}),
                }
                for row in rows
            ]
        }
    finally:
        conn.close()


@_cached
def watchdog(path: str | Path) -> dict[str, Any]:
    payload = stations(path)
    conn = _connect(path)
    if conn is None:
        return {**payload, "incidents": []}
    try:
        incidents: list[dict[str, Any]] = []
        heartbeat = None
        if _has_table(conn, "watchdog_state"):
            row = conn.execute("SELECT * FROM watchdog_state WHERE id=1").fetchone()
            if row is not None:
                heartbeat = {
                    "heartbeat_at": float(row["heartbeat_at"] or 0.0),
                    "last_run_at": float(row["last_run_at"]) if row["last_run_at"] is not None else None,
                    "status": str(row["status"] or ""),
                    "error": str(row["error"] or ""),
                }
        if _has_table(conn, "incidents"):
            rows = conn.execute(
                "SELECT * FROM incidents WHERE ended_at IS NULL ORDER BY id DESC LIMIT 100"
            ).fetchall()
            incidents = [
                {
                    "id": int(row["id"]),
                    "scope": str(row["scope"] or ""),
                    "station": int(row["station"]) if row["station"] is not None else None,
                    "started_at": float(row["started_at"] or 0.0),
                    "cause": str(row["cause"] or ""),
                    "remediation": str(row["remediation"] or ""),
                    "details": str(row["details"] or ""),
                }
                for row in rows
            ]
        return {**payload, "incidents": incidents, "heartbeat": heartbeat}
    finally:
        conn.close()


@_cached
def discovery(path: str | Path, *, limit: int = 20) -> dict[str, Any]:
    conn = _connect(path)
    if conn is None:
        return {"items": []}
    try:
        if not _has_table(conn, "discovery_runs"):
            return {"items": []}
        rows = conn.execute(
            "SELECT * FROM discovery_runs ORDER BY started_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return {
            "items": [
                {
                    "id": str(row["id"]),
                    "source": str(row["source"] or ""),
                    "started_at": float(row["started_at"] or 0.0),
                    "finished_at": float(row["finished_at"]) if row["finished_at"] is not None else None,
                    "status": str(row["status"] or ""),
                    "server_count": int(row["server_count"] or 0),
                    "transport_count": int(row["transport_count"] or 0),
                    "stale": bool(row["stale"]),
                    "error": str(row["error"] or ""),
                }
                for row in rows
            ]
        }
    finally:
        conn.close()


@_cached
def rotations(path: str | Path, *, limit: int = 50) -> dict[str, Any]:
    """Cutover history for the rotation dashboard."""

    conn = _connect(path)
    if conn is None:
        return {"items": []}
    try:
        if not _has_table(conn, "station_cutovers"):
            return {"items": []}
        rows = conn.execute(
            "SELECT * FROM station_cutovers ORDER BY started_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return {
            "items": [
                {
                    "cutover_id": str(row["cutover_id"]),
                    "station": int(row["station"]),
                    "from_option_key": str(row["from_option_key"] or ""),
                    "from_egress_ip": str(row["from_egress_ip"] or ""),
                    "to_option_key": str(row["to_option_key"] or ""),
                    "to_egress_ip": str(row["to_egress_ip"] or ""),
                    "transport": str(row["transport"] or ""),
                    "started_at": float(row["started_at"] or 0.0),
                    "finished_at": (
                        float(row["finished_at"])
                        if row["finished_at"] is not None
                        else None
                    ),
                    "duration_ms": (
                        float(row["duration_ms"])
                        if row["duration_ms"] is not None
                        else None
                    ),
                    "result": str(row["result"] or ""),
                    "reason": str(row["reason"] or ""),
                }
                for row in rows
            ]
        }
    finally:
        conn.close()


@_cached
def repairs(path: str | Path, *, limit: int = 50) -> dict[str, Any]:
    conn = _connect(path)
    if conn is None:
        return {"items": []}
    try:
        if not _has_table(conn, "repair_jobs"):
            return {"items": []}
        rows = conn.execute(
            "SELECT * FROM repair_jobs ORDER BY started_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        return {
            "items": [
                {
                    "id": int(row["id"]),
                    "station": int(row["station"]),
                    "cause": str(row["cause"] or ""),
                    "started_at": float(row["started_at"] or 0.0),
                    "finished_at": float(row["finished_at"]) if row["finished_at"] is not None else None,
                    "result": str(row["result"] or ""),
                    "transports_tried": str(row["transports_tried"] or ""),
                    "last_error": str(row["last_error"] or ""),
                }
                for row in rows
            ]
        }
    finally:
        conn.close()


@_cached
def slo(
    path: str | Path,
    *,
    target_pct: float = 99.0,
    now: float | None = None,
) -> dict[str, Any]:
    """Station SLO view: 6/6, availability, heartbeat, incidents and MTTR."""

    ts = float(time.time() if now is None else now)
    payload = watchdog(path)
    items = list(payload.get("items") or [])
    usable = sum(1 for item in items if item.get("client_usable"))
    availability_values = [
        float(item["availability_1h"])
        for item in items
        if float(item.get("availability_1h") or 0.0) > 0
    ]
    if availability_values:
        availability = sum(availability_values) / len(availability_values)
    elif items:
        availability = 100.0 * usable / len(items)
    else:
        availability = 0.0
    heartbeat = payload.get("heartbeat") or {}
    heartbeat_at = float(heartbeat.get("heartbeat_at") or 0.0)
    incidents = list(payload.get("incidents") or [])
    oldest = max(
        (ts - float(incident["started_at"]) for incident in incidents),
        default=0.0,
    )
    repairs_items = list(repairs(path, limit=200).get("items") or [])
    durations = [
        float(item["finished_at"]) - float(item["started_at"])
        for item in repairs_items
        if item.get("finished_at") is not None
    ]
    target = max(0.0, min(100.0, float(target_pct)))
    return {
        "enabled": bool(items) or bool(heartbeat),
        "target_pct": target,
        "stations_total": len(items),
        "stations_usable": usable,
        "fleet_ratio_pct": (100.0 * usable / len(items)) if items else 0.0,
        "six_of_six": bool(items) and usable == len(items) == 6,
        "availability_1h_pct": round(availability, 3),
        "error_budget_pct": round(max(0.0, availability - target), 3),
        "heartbeat_at": heartbeat_at or None,
        "heartbeat_status": str(heartbeat.get("status") or ""),
        "heartbeat_age_s": round(ts - heartbeat_at, 3) if heartbeat_at else None,
        "open_incidents": len(incidents),
        "oldest_incident_age_s": round(oldest, 3) if incidents else 0.0,
        "repairs_finished": len(durations),
        "mttr_s": round(sum(durations) / len(durations), 3) if durations else None,
    }


@_cached
def quota(path: str | Path, *, limit: int = 200) -> dict[str, Any]:
    """Expose the per-IP quota ledger without mutating it."""

    conn = _connect(path)
    if conn is None:
        return {"enabled": False, "summary": {}, "buckets": []}
    try:
        if not _has_table(conn, "quota_buckets"):
            return {"enabled": False, "summary": {}, "buckets": []}
        rows = conn.execute(
            """
            SELECT q.ip, q.provider, q.model, q.day, q.bucket, q.used,
                   q.successes, q.failures, q.last_status, q.last_error,
                   q.reset_at, q.last_seen_at,
                   e.state AS ip_state, e.country, e.block_key
            FROM quota_buckets q
            LEFT JOIN egress_ips e ON e.ip = q.ip
            ORDER BY COALESCE(q.reset_at, 0) DESC, q.last_seen_at DESC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
        now = time.time()
        resets = [
            float(row["reset_at"])
            for row in rows
            if row["reset_at"] is not None and float(row["reset_at"]) > now
        ]
        summary = {
            "ips": len({str(row["ip"]) for row in rows}),
            "buckets": len(rows),
            "used": sum(int(row["used"] or 0) for row in rows),
            "successes": sum(int(row["successes"] or 0) for row in rows),
            "failures": sum(int(row["failures"] or 0) for row in rows),
            "quota_429": sum(1 for row in rows if int(row["last_status"] or 0) == 429),
            "resetting_within_1h": sum(1 for value in resets if value <= now + 3600.0),
            "next_reset_at": min(resets) if resets else None,
        }
        return {
            "enabled": True,
            "summary": summary,
            "buckets": [
                {
                    "ip": str(row["ip"]),
                    "provider": str(row["provider"] or ""),
                    "model": str(row["model"] or "default"),
                    "day": str(row["day"] or ""),
                    "bucket": str(row["bucket"] or "default"),
                    "used": int(row["used"] or 0),
                    "successes": int(row["successes"] or 0),
                    "failures": int(row["failures"] or 0),
                    "last_status": int(row["last_status"])
                    if row["last_status"] is not None
                    else None,
                    "last_error": str(row["last_error"] or ""),
                    "reset_at": float(row["reset_at"])
                    if row["reset_at"] is not None
                    else None,
                    "last_seen_at": float(row["last_seen_at"] or 0.0),
                    "ip_state": str(row["ip_state"] or ""),
                    "country": str(row["country"] or ""),
                    "block_key": str(row["block_key"] or ""),
                }
                for row in rows
            ],
        }
    finally:
        conn.close()


@_cached
def transports(path: str | Path) -> dict[str, Any]:
    conn = _connect(path)
    if conn is None:
        return {"items": []}
    try:
        if not _has_table(conn, "server_transports"):
            return {"items": []}
        rows = conn.execute(
            """
            SELECT transport, COUNT(*) AS n, SUM(supported) AS supported
            FROM server_transports GROUP BY transport ORDER BY transport
            """
        ).fetchall()
        catalog: dict[str, dict[str, Any]] = {
            str(row["transport"]): {
                "transport": str(row["transport"]),
                "total": int(row["n"] or 0),
                "supported": int(row["supported"] or 0),
            }
            for row in rows
        }
        health_rows: dict[str, Any] = {}
        if _has_table(conn, "transport_health"):
            for row in conn.execute(
                """
                SELECT transport, attempts, successes, quota, infra,
                       latency_p50_ms, success_rate, degraded, last_error, updated_at
                FROM transport_health WHERE station = 0
                """
            ).fetchall():
                health_rows[str(row["transport"])] = row
        stats: dict[str, Any] = {}
        if not health_rows and _has_table(conn, "qualification_attempts"):
            for row in conn.execute(
                """
                SELECT transport,
                       COUNT(*) AS attempts,
                       SUM(CASE WHEN status = '200' THEN 1 ELSE 0 END) AS ok,
                       SUM(CASE WHEN status = '429' THEN 1 ELSE 0 END) AS quota,
                       SUM(CASE WHEN status IN
                           ('no_egress', 'bind_error', 'timeout', 'duplicate')
                           THEN 1 ELSE 0 END) AS infra,
                       AVG(CASE WHEN status = '200' AND rtt_ms > 0
                           THEN rtt_ms END) AS avg_rtt_ms,
                       MAX(finished_at) AS last_attempt_at
                FROM qualification_attempts
                WHERE transport <> ''
                GROUP BY transport
                """
            ).fetchall():
                stats[str(row["transport"])] = row
        items = []
        for name in sorted(set(catalog) | set(health_rows) | set(stats)):
            entry = catalog.get(name, {"transport": name, "total": 0, "supported": 0})
            health_row = health_rows.get(name)
            stats_row = stats.get(name)
            if health_row is not None:
                attempts_count = int(health_row["attempts"] or 0)
                successes = int(health_row["successes"] or 0)
                quota = int(health_row["quota"] or 0)
                infra = int(health_row["infra"] or 0)
                success_rate = float(health_row["success_rate"] or 0.0)
                degraded = bool(health_row["degraded"])
                latency_p50_ms = (
                    round(float(health_row["latency_p50_ms"]), 2)
                    if health_row["latency_p50_ms"] is not None
                    else None
                )
                last_error = str(health_row["last_error"] or "")
                updated_at = float(health_row["updated_at"] or 0.0)
                avg_rtt_ms = None
                last_attempt_at = updated_at or None
            else:
                attempts_count = int(stats_row["attempts"] or 0) if stats_row is not None else 0
                successes = int(stats_row["ok"] or 0) if stats_row is not None else 0
                quota = int(stats_row["quota"] or 0) if stats_row is not None else 0
                infra = int(stats_row["infra"] or 0) if stats_row is not None else 0
                success_rate = successes / attempts_count if attempts_count else 0.0
                degraded = bool(attempts_count >= 100 and successes == 0)
                latency_p50_ms = None
                last_error = ""
                updated_at = 0.0
                avg_rtt_ms = (
                    round(float(stats_row["avg_rtt_ms"]), 2)
                    if stats_row is not None and stats_row["avg_rtt_ms"] is not None
                    else None
                )
                last_attempt_at = (
                    float(stats_row["last_attempt_at"])
                    if stats_row is not None and stats_row["last_attempt_at"] is not None
                    else None
                )
            if degraded:
                health = "degraded"
            elif successes > 0:
                health = "healthy"
            elif quota > 0 and quota >= infra:
                health = "quota"
            elif infra > 0:
                health = "infra"
            elif attempts_count > 0:
                health = "failing"
            else:
                health = "unproven"
            diagnostic = classify_diagnostic("", last_error)
            items.append(
                {
                    **entry,
                    "attempts": attempts_count,
                    "ok": successes,
                    "successes": successes,
                    "quota": quota,
                    "infra": infra,
                    "success_rate": round(success_rate, 4),
                    "avg_rtt_ms": avg_rtt_ms,
                    "latency_p50_ms": latency_p50_ms,
                    "last_attempt_at": last_attempt_at,
                    "last_error": last_error,
                    "diagnostic": diagnostic,
                    "degraded": degraded,
                    "health": health,
                }
            )
        return {
            "items": items,
            "openvpn_diagnostics": [
                item
                for item in items
                if item["transport"].startswith("openvpn")
                and item["attempts"] > 0
                and item["successes"] == 0
            ],
        }
    finally:
        conn.close()


__all__ = [
    "attempts",
    "cache_info",
    "coverage",
    "discovery",
    "epoch",
    "epochs",
    "invalidate",
    "next_candidates",
    "queue",
    "quota",
    "repairs",
    "rotations",
    "slo",
    "stations",
    "summary",
    "transports",
    "watchdog",
]
