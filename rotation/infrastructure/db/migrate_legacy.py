"""Migrate the legacy `ip_registry.db` into the target `registry.db`.

The migration is read-only on the legacy file and creates a brand-new target.
It is intentionally testable on a copy; it is not wired into the runtime yet.
"""

from __future__ import annotations

import ast
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from .migrate import apply_schema, connect

_TECH = {
    "wireguard_udp": "wireguard",
    "wireguard": "wireguard",
    "openvpn_udp": "openvpn-udp",
    "openvpn_tcp": "openvpn-tcp",
}
_PORTS = {"wireguard": 51820, "openvpn-udp": 1194, "openvpn-tcp": 443}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _technologies(value: Any) -> set[str]:
    try:
        raw = ast.literal_eval(str(value or "[]"))
    except Exception:
        raw = []
    out: set[str] = set()
    for item in raw if isinstance(raw, (list, tuple)) else []:
        ident = ""
        if isinstance(item, dict):
            ident = str(item.get("identifier") or "")
        elif isinstance(item, str):
            ident = item
        transport = _TECH.get(ident)
        if transport:
            out.add(transport)
    return out or {"wireguard"}


def _state(value: Any) -> str:
    state = str(value or "candidate").lower()
    return state if state in {
        "fresh", "candidate", "assigned", "draining",
        "exhausted", "error", "retired",
    } else "candidate"


def migrate_legacy(
    legacy_path: str | Path,
    target_path: str | Path,
    *,
    now: float | None = None,
) -> dict[str, int]:
    ts = time.time() if now is None else float(now)
    legacy = sqlite3.connect(f"file:{legacy_path}?mode=ro", uri=True)
    legacy.row_factory = sqlite3.Row
    target = connect(target_path)
    apply_schema(target, now=ts)
    counts = {
        "servers": 0,
        "transports": 0,
        "endpoints": 0,
        "ips": 0,
        "observations": 0,
        "leases": 0,
        "warm": 0,
    }
    try:
        source_ids: set[str] = set()
        if _table_exists(legacy, "servers"):
            for row in legacy.execute("SELECT * FROM servers"):
                host = str(row["hostname"] or "").strip()
                if not host:
                    continue
                source = str(row["source_id"] or "legacy") if "source_id" in row.keys() else "legacy"
                if source not in source_ids:
                    target.execute(
                        "INSERT OR IGNORE INTO catalog_sources(source_id, kind, fetched_at) "
                        "VALUES(?,?,?)",
                        (source, "legacy", ts),
                    )
                    source_ids.add(source)
                country = str(row["country"] or "").strip() or "unknown"
                target.execute(
                    "INSERT OR REPLACE INTO servers("
                    "hostname, country, city, load, source_id, first_seen_at, "
                    "last_seen_at, stale, score_host) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        host,
                        country,
                        str(row["city"] or "") if "city" in row.keys() else "",
                        float(row["load"] or 0) if "load" in row.keys() else 0.0,
                        source,
                        float(row["first_seen_at"] or ts) if "first_seen_at" in row.keys() else ts,
                        float(row["last_seen_at"] or ts) if "last_seen_at" in row.keys() else ts,
                        int(row["stale"] or 0) if "stale" in row.keys() else 0,
                        0.0,
                    ),
                )
                counts["servers"] += 1
                transports = _technologies(row["technologies"] if "technologies" in row.keys() else "[]")
                endpoint_ip = str(row["server_ip"] or "") if "server_ip" in row.keys() else ""
                for transport in sorted(transports):
                    target.execute(
                        "INSERT OR REPLACE INTO server_transports(hostname, transport, supported) "
                        "VALUES(?,?,1)",
                        (host, transport),
                    )
                    counts["transports"] += 1
                    if endpoint_ip:
                        target.execute(
                            "INSERT OR IGNORE INTO endpoints("
                            "hostname, transport, endpoint_ip, port, source, "
                            "first_seen_at, last_seen_at) VALUES(?,?,?,?,?,?,?)",
                            (host, transport, endpoint_ip, _PORTS[transport], source, ts, ts),
                        )
                        counts["endpoints"] += 1

        if _table_exists(legacy, "ip_state"):
            for row in legacy.execute("SELECT * FROM ip_state"):
                host = str(row["hostname"] or "").strip()
                ip = str(row["egress_ip"] or "").strip()
                if not host or not ip:
                    continue
                state = _state(row["state"])
                target.execute(
                    "INSERT OR REPLACE INTO egress_ips("
                    "ip, country, first_seen_at, last_seen_at, last_ok_at, "
                    "last_429_at, reset_at, successes, failures, latency_ms, "
                    "score_ip, state) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ip,
                        str(row["country"] or ""),
                        float(row["updated_at"] or ts) if "updated_at" in row.keys() else ts,
                        float(row["last_tested_at"] or ts) if "last_tested_at" in row.keys() else ts,
                        float(row["last_ok_at"] or 0) if "last_ok_at" in row.keys() else 0.0,
                        float(row["last_tested_at"] or 0) if "last_status" in row.keys() and int(row["last_status"] or 0) == 429 else 0.0,
                        float(row["reset_at"] or 0) if "reset_at" in row.keys() else 0.0,
                        int(row["successes"] or 0) if "successes" in row.keys() else 0,
                        int(row["failures"] or 0) if "failures" in row.keys() else 0,
                        float(row["latency_ms"] or 0) if "latency_ms" in row.keys() else 0.0,
                        float(row["score"] or 0) if "score" in row.keys() else 0.0,
                        state,
                    ),
                )
                counts["ips"] += 1
                target.execute(
                    "INSERT OR IGNORE INTO ip_server_map("
                    "ip, hostname, transport, first_seen_at, last_seen_at) "
                    "VALUES(?,?,?,?,?)",
                    (ip, host, "wireguard", ts, ts),
                )
                station = int(row["assigned_station"] or 0) if "assigned_station" in row.keys() else 0
                if station and state in ("assigned", "candidate"):
                    try:
                        target.execute(
                            "INSERT OR IGNORE INTO leases("
                            "lease_id, station, ip, hostname, transport, started_at, "
                            "renewed_at, expires_at, status) "
                            "VALUES(?,?,?,?,?,?,?,?,'active')",
                            (
                                f"legacy-{uuid.uuid4().hex[:12]}",
                                station,
                                ip,
                                host,
                                "wireguard",
                                ts,
                                ts,
                                ts + 1800.0,
                            ),
                        )
                        counts["leases"] += 1
                    except sqlite3.IntegrityError:
                        pass

        if _table_exists(legacy, "observations"):
            cols = _columns(legacy, "observations")
            for row in legacy.execute("SELECT * FROM observations"):
                host = str(row["hostname"] or "").strip()
                if not host:
                    continue
                target.execute(
                    "INSERT INTO observations("
                    "station, hostname, transport, ip, model, status, valid, "
                    "retry_after_s, reset_at, latency_ms, error, tested_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        int(row["station"] or 0) if "station" in cols else 0,
                        host,
                        "wireguard",
                        str(row["egress_ip"] or "") if "egress_ip" in cols else "",
                        None,
                        int(row["status"] or 0),
                        1,
                        float(row["retry_after_s"] or 0) if "retry_after_s" in cols else 0.0,
                        float(row["reset_at"] or 0) if "reset_at" in cols else 0.0,
                        float(row["latency_ms"] or 0) if "latency_ms" in cols else 0.0,
                        str(row["error"] or "") if "error" in cols else "",
                        float(row["tested_at"] or ts),
                    ),
                )
                counts["observations"] += 1

        if _table_exists(legacy, "verified_spares"):
            for row in legacy.execute("SELECT * FROM verified_spares"):
                ip = str(row["egress_ip"] or "").strip()
                if not ip:
                    continue
                if target.execute(
                    "SELECT 1 FROM egress_ips WHERE ip=?", (ip,)
                ).fetchone() is None:
                    continue
                target.execute(
                    "INSERT OR REPLACE INTO warm_pool("
                    "ip, hostname, transport, model, verified_at, expires_at, score_ip) "
                    "VALUES(?,?,?,?,?,?,0)",
                    (
                        ip,
                        str(row["hostname"] or ""),
                        str(row["transport"] or "wireguard"),
                        str(row["model"] or ""),
                        float(row["last_200_at"] or ts),
                        float(row["expires_at"] or ts),
                    ),
                )
                counts["warm"] += 1

        target.commit()
        return counts
    finally:
        legacy.close()
        target.close()
