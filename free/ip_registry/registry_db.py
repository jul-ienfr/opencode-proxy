"""Runtime assignment helpers on the target ``registry.db`` schema.

``SqliteIpRegistry`` still owns the legacy ``ip_state``/``assignments``
schema used by the manual discovery scripts. The live proxy and supervisor
must not create that legacy database: their assignments, leases and
observations live in ``registry.db`` (``egress_ips``, ``leases``,
``ip_server_map``, ``observations``).

The functions here are the small runtime surface the supervisor and the
proxy lifespan need: read the IP assigned to a station, release it, record a
proven winner and reconcile abandoned leases at boot.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = ROOT / "logs" / "registry.db"
_SOURCE = "runtime"
_LEASE_TTL_S = 6 * 3600.0


def registry_db_path(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else DEFAULT_DB


def _connect(path: str | Path):
    from rotation.infrastructure.db.migrate import connect

    return connect(path)


def _ensure_runtime_source(conn: sqlite3.Connection, ts: float) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO catalog_sources("
        "source_id, kind, fetched_at, stale, server_count, error"
        ") VALUES(?,?,?,0,0,'')",
        (_SOURCE, "runtime", ts),
    )


def _released_state_sql(now: float) -> str:
    # An IP whose quota reset is still in the future stays exhausted; any
    # other released IP becomes selectable again.
    return (
        "CASE WHEN last_429_at IS NOT NULL AND reset_at IS NOT NULL "
        f"AND reset_at > {float(now)} THEN 'exhausted' ELSE 'fresh' END"
    )


def assigned_for_station(
    station: int, *, db_path: str | Path | None = None
) -> dict[str, Any] | None:
    conn = _connect(registry_db_path(db_path))
    try:
        row = conn.execute(
            "SELECT l.lease_id, l.station, l.ip, l.hostname, l.transport, "
            "l.model, l.expires_at, COALESCE(e.country,'') AS country "
            "FROM leases l LEFT JOIN egress_ips e ON e.ip=l.ip "
            "WHERE l.station=? AND l.status='active' "
            "ORDER BY l.started_at DESC LIMIT 1",
            (int(station),),
        ).fetchone()
        return dict(row) if row is not None else None
    except Exception:
        return None
    finally:
        conn.close()


def release_station(
    station: int,
    reason: str = "replaced",
    *,
    now: float | None = None,
    db_path: str | Path | None = None,
) -> int:
    ts = time.time() if now is None else float(now)
    conn = _connect(registry_db_path(db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT ip FROM leases WHERE station=? AND status='active'",
            (int(station),),
        ).fetchall()
        for row in rows:
            ip = str(row["ip"])
            conn.execute(
                "UPDATE leases SET status='released', released_at=?, reason=? "
                "WHERE station=? AND ip=? AND status='active'",
                (ts, str(reason), int(station), ip),
            )
            conn.execute(
                "UPDATE egress_ips SET state=" + _released_state_sql(ts) + ", "
                "last_seen_at=? WHERE ip=?",
                (ts, ip),
            )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        return 0
    finally:
        conn.close()


def assign_observed(
    *,
    egress_ip: str,
    hostname: str,
    country: str = "",
    station: int,
    latency_ms: float | None = None,
    tested_at: float | None = None,
    db_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Persist a proven 200 and make ``station`` the sole owner of the IP."""

    ip = str(egress_ip or "").strip()
    host = str(hostname or "").strip()
    if not ip:
        return None
    ts = time.time() if tested_at is None else float(tested_at)
    conn = _connect(registry_db_path(db_path))
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT station FROM leases WHERE ip=? AND status='active' LIMIT 1",
            (ip,),
        ).fetchone()
        if existing is not None and int(existing["station"]) != int(station):
            conn.rollback()
            return None
        _ensure_runtime_source(conn, ts)
        conn.execute(
            "INSERT INTO servers(hostname, country, source_id, first_seen_at, "
            "last_seen_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(hostname) DO UPDATE SET last_seen_at=excluded.last_seen_at",
            (host, str(country or "unknown"), _SOURCE, ts, ts),
        )
        conn.execute(
            "INSERT INTO egress_ips(ip, country, first_seen_at, last_seen_at, "
            "last_ok_at, reset_at, successes, latency_ms, state) "
            "VALUES(?,?,?,?,?,NULL,1,?,'assigned') "
            "ON CONFLICT(ip) DO UPDATE SET country=excluded.country, "
            "last_seen_at=excluded.last_seen_at, last_ok_at=excluded.last_ok_at, "
            "reset_at=NULL, successes=egress_ips.successes+1, "
            "latency_ms=COALESCE(excluded.latency_ms, egress_ips.latency_ms), "
            "state='assigned'",
            (ip, str(country or ""), ts, ts, ts, latency_ms),
        )
        if host:
            conn.execute(
                "INSERT INTO ip_server_map(ip, hostname, transport, "
                "first_seen_at, last_seen_at, successes) "
                "VALUES(?,?,'wireguard',?,?,1) "
                "ON CONFLICT(ip, hostname, transport) DO UPDATE SET "
                "last_seen_at=excluded.last_seen_at, "
                "successes=ip_server_map.successes+1",
                (ip, host, ts, ts),
            )
        conn.execute(
            "INSERT INTO observations(station, hostname, transport, ip, model, "
            "status, valid, latency_ms, tested_at) "
            "VALUES(?,?,'wireguard',?,'',200,1,?,?)",
            (int(station), host, ip, latency_ms, ts),
        )
        # One active lease per station: the proven IP replaces the old one.
        conn.execute(
            "UPDATE leases SET status='released', released_at=?, reason='replaced' "
            "WHERE station=? AND status='active' AND ip!=?",
            (ts, int(station), ip),
        )
        if existing is not None and int(existing["station"]) == int(station):
            conn.execute(
                "UPDATE leases SET hostname=?, renewed_at=?, expires_at=?, "
                "status='active' WHERE ip=? AND station=? AND status='active'",
                (host, ts, ts + _LEASE_TTL_S, ip, int(station)),
            )
        else:
            conn.execute(
                "INSERT INTO leases(lease_id, station, ip, hostname, transport, "
                "model, started_at, renewed_at, expires_at, status) "
                "VALUES(?,?,?,?,'wireguard','',?,?,?,'active')",
                (
                    f"runtime-{int(station)}-{ip}-{int(ts)}",
                    int(station),
                    ip,
                    host,
                    ts,
                    ts,
                    ts + _LEASE_TTL_S,
                ),
            )
        conn.commit()
        return {
            "egress_ip": ip,
            "hostname": host,
            "country": str(country or ""),
            "station": int(station),
        }
    except sqlite3.IntegrityError:
        conn.rollback()
        return None
    except Exception:
        conn.rollback()
        return None
    finally:
        conn.close()


def reconcile(
    *, now: float | None = None, db_path: str | Path | None = None
) -> dict[str, int]:
    """Release expired leases, revive reset IPs and clear orphan assignments."""

    ts = time.time() if now is None else float(now)
    conn = _connect(registry_db_path(db_path))
    released_leases = 0
    released_ips = 0
    revived_ips = 0
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT station, ip FROM leases "
            "WHERE status='active' AND expires_at <= ?",
            (ts,),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE leases SET status='expired', released_at=?, "
                "reason='ttl' WHERE station=? AND ip=? AND status='active'",
                (ts, int(row["station"]), str(row["ip"])),
            )
            conn.execute(
                "UPDATE egress_ips SET state=" + _released_state_sql(ts) + ", "
                "last_seen_at=? WHERE ip=?",
                (ts, str(row["ip"])),
            )
            released_leases += 1
        # A 429 reset_at is the authoritative expiry. Once it has passed the
        # IP is selectable again even if no probe has touched it yet; keeping
        # state='exhausted' forever hid revived IPs from the dashboard and the
        # candidate query.
        revived = conn.execute(
            "UPDATE egress_ips SET state='fresh', last_seen_at=? "
            "WHERE state='exhausted' "
            "AND (reset_at IS NULL OR reset_at <= ?)",
            (ts, ts),
        )
        revived_ips = int(revived.rowcount or 0)
        orphan = conn.execute(
            "UPDATE egress_ips SET state=" + _released_state_sql(ts) + ", "
            "last_seen_at=? WHERE state='assigned' AND ip NOT IN "
            "(SELECT ip FROM leases WHERE status='active')",
            (ts,),
        )
        released_ips = int(orphan.rowcount or 0)
        conn.commit()
    except Exception:
        conn.rollback()
        return {
            "released_leases": 0,
            "released_ips": 0,
            "revived_ips": 0,
        }
    finally:
        conn.close()
    return {
        "released_leases": released_leases,
        "released_ips": released_ips,
        "revived_ips": revived_ips,
    }


__all__ = [
    "DEFAULT_DB",
    "assign_observed",
    "assigned_for_station",
    "reconcile",
    "registry_db_path",
    "release_station",
]
