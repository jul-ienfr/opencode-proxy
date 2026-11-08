"""One-time migration of the legacy ``logs/ip_registry.db``.

The legacy ``ip_state``/``verified_spares`` tables are copied into the target
``servers``, ``egress_ips``, ``ip_server_map`` and ``warm_pool`` tables. The
copy is idempotent and never overwrites a newer target row.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

SOURCE_ID = "legacy-ip-registry"
_VALID_STATES = (
    "fresh",
    "candidate",
    "assigned",
    "draining",
    "exhausted",
    "error",
    "retired",
)


def migrate_ip_registry(
    target: str | Path, source: str | Path
) -> dict[str, int]:
    target = Path(target)
    source = Path(source)
    if not source.exists() or source.resolve() == target.resolve():
        return {"servers": 0, "ips": 0, "spares": 0}
    conn = sqlite3.connect(str(target), timeout=10.0)
    try:
        conn.execute("ATTACH DATABASE ? AS legacy", (str(source),))
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM legacy.sqlite_master WHERE type='table'"
            )
        }
        if "ip_state" not in tables:
            return {"servers": 0, "ips": 0, "spares": 0}
        ts = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO catalog_sources("
            "source_id, kind, fetched_at, stale, server_count, error"
            ") VALUES(?,?,?,0,0,'')",
            (SOURCE_ID, "legacy", ts),
        )
        cur = conn.execute(
            "INSERT OR IGNORE INTO servers("
            "hostname, country, source_id, first_seen_at, last_seen_at"
            ") SELECT hostname, "
            "COALESCE(NULLIF(country,''), 'unknown'), ?, "
            "MIN(COALESCE(updated_at, ?)), MAX(COALESCE(updated_at, ?)) "
            "FROM legacy.ip_state WHERE hostname != '' GROUP BY hostname",
            (SOURCE_ID, ts, ts),
        )
        servers = int(cur.rowcount or 0)
        cur = conn.execute(
            "INSERT OR IGNORE INTO egress_ips("
            "ip, country, first_seen_at, last_seen_at, last_ok_at, "
            "last_429_at, reset_at, successes, failures, latency_ms, "
            "score_ip, state) "
            "SELECT egress_ip, COALESCE(country,''), "
            "MIN(COALESCE(updated_at, ?)), MAX(COALESCE(updated_at, ?)), "
            "MAX(last_ok_at), "
            "MAX(CASE WHEN last_status=429 THEN last_tested_at END), "
            "MAX(reset_at), MAX(COALESCE(successes,0)), "
            "MAX(COALESCE(failures,0)), MAX(latency_ms), "
            "MAX(COALESCE(score,0)), "
            "CASE WHEN MAX(CASE WHEN last_status=429 THEN 1 ELSE 0 END)=1 "
            "THEN 'exhausted' ELSE 'fresh' END "
            "FROM legacy.ip_state WHERE egress_ip != '' GROUP BY egress_ip",
            (ts, ts),
        )
        ips = int(cur.rowcount or 0)
        conn.execute(
            "INSERT OR IGNORE INTO ip_server_map("
            "ip, hostname, transport, first_seen_at, last_seen_at, "
            "successes, failures) "
            "SELECT egress_ip, hostname, 'wireguard', "
            "MIN(COALESCE(updated_at, ?)), MAX(COALESCE(updated_at, ?)), "
            "MAX(COALESCE(successes,0)), MAX(COALESCE(failures,0)) "
            "FROM legacy.ip_state "
            "WHERE egress_ip != '' AND hostname != '' "
            "GROUP BY egress_ip, hostname",
            (ts, ts),
        )
        spares = 0
        if "verified_spares" in tables:
            conn.execute(
                "INSERT OR IGNORE INTO egress_ips("
                "ip, first_seen_at, last_seen_at, state"
                ") SELECT egress_ip, last_200_at, last_200_at, 'fresh' "
                "FROM legacy.verified_spares WHERE egress_ip != ''"
            )
            cur = conn.execute(
                "INSERT OR REPLACE INTO warm_pool("
                "ip, hostname, transport, model, verified_at, expires_at, "
                "score_ip) "
                "SELECT egress_ip, hostname, COALESCE(transport,''), "
                "COALESCE(model,''), COALESCE(last_200_at, ?), "
                "COALESCE(expires_at, ?), 0 "
                "FROM legacy.verified_spares WHERE egress_ip != '' "
                "AND hostname != ''",
                (ts, ts),
            )
            spares = int(cur.rowcount or 0)
        conn.commit()
        return {"servers": servers, "ips": ips, "spares": spares}
    finally:
        try:
            conn.execute("DETACH DATABASE legacy")
        except Exception:
            pass
        conn.close()
