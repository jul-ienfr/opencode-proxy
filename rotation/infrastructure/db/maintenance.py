"""Runtime maintenance for the target registry.

Called once by the supervisor at boot (and usable from the CLI): apply the
versioned schema, prune old rows, checkpoint WAL, verify integrity and write a
timestamped backup. A failed maintenance must never prevent the repair loop
from starting; callers log the error and continue.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from .claims import migrate_claims
from .legacy_ip import migrate_ip_registry
from .migrate import apply_schema, connect
from .retention import backup, checkpoint, integrity_ok, prune


def run_once(
    db_path: str | Path,
    *,
    backup_dir: str | Path | None = None,
    claims_source: str | Path | None = None,
    ip_source: str | Path | None = None,
    now: float | None = None,
    days: dict[str, float] | None = None,
) -> dict[str, Any]:
    path = Path(db_path)
    ts = time.time() if now is None else float(now)
    conn = connect(path)
    try:
        version = apply_schema(conn, now=ts)
        copied = migrate_claims(path, claims_source) if claims_source else {}
        legacy_ips = migrate_ip_registry(path, ip_source) if ip_source else {}
        deleted = prune(conn, now=ts, days=days)
        checkpoint(conn)
        ok = integrity_ok(conn)
        target = None
        if backup_dir is not None:
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts))
            target = backup(
                conn,
                Path(backup_dir) / f"registry-{stamp}.db",
            )
        return {
            "db": str(path),
            "version": version,
            "claims": copied,
            "legacy_ips": legacy_ips,
            "deleted": deleted,
            "integrity_ok": ok,
            "backup": str(target) if target else None,
        }
    finally:
        conn.close()
