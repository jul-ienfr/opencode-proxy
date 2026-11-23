"""Purge du registre, checkpoint WAL et integrity_check — 019 T304.

Un seul passage idempotent :
* ``warm_pool`` : les preuves expirées (``expires_at <= now``) ;
* ``claims`` : les lignes terminales plus vieilles que ``--retention-days``
  (les claims actifs ``claimed``/``bound`` sont conservés) ;
* historique ``qualification_attempts`` / ``station_cutovers`` / ``incidents``
  / ``discovery_runs`` via :meth:`QualificationStore.purge_attempts` ;
* ``PRAGMA wal_checkpoint(TRUNCATE)`` quand le WAL dépasse ``--wal-max-bytes`` ;
* ``PRAGMA integrity_check`` (rapporté, jamais fatal ici).

Usage::

    python scripts/registry_purge.py
    python scripts/registry_purge.py --db logs/registry.db --retention-days 7
    python scripts/registry_purge.py --check   # exit 1 si integrity != ok
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_DB = ROOT / "logs" / "registry.db"
_ACTIVE_CLAIM_STATES = ("claimed", "bound", "ok")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _wal_bytes(db_path: Path) -> int:
    try:
        return int(os.path.getsize(str(db_path) + "-wal"))
    except OSError:
        return 0


def purge_once(
    db_path: str | Path = DEFAULT_DB,
    *,
    claims_db: str | Path | None = None,
    now: float | None = None,
    retention_days: float = 7.0,
    wal_max_bytes: int = 4 * 1024 * 1024,
    log_fn=None,
) -> dict[str, Any]:
    """Purge un registre et retourne un rapport JSON-sérialisable."""
    ts = time.time() if now is None else float(now)
    cutoff = ts - max(1.0, float(retention_days)) * 86400.0
    path = Path(db_path)
    report: dict[str, Any] = {
        "db": str(path),
        "now": ts,
        "retention_days": float(retention_days),
        "warm_pool": 0,
        "claims": 0,
        "history": 0,
        "wal_bytes_before": _wal_bytes(path),
        "wal_checkpointed": False,
        "integrity": "",
    }

    # Historique de qualification : la purge canonique vit dans le store.
    try:
        from free.qualification.store import QualificationStore

        store = QualificationStore(path, retention_days=retention_days)
        try:
            report["history"] = int(store.purge_attempts(now=ts))
        finally:
            store.close()
    except Exception as exc:  # fail-soft: la purge brute continue
        report["history_error"] = str(exc)

    claims_path = Path(claims_db) if claims_db else path
    conn = sqlite3.connect(str(path), timeout=10.0)
    conn.execute("PRAGMA busy_timeout=10000")
    try:
        if _table_exists(conn, "warm_pool"):
            cur = conn.execute("DELETE FROM warm_pool WHERE expires_at <= ?", (ts,))
            report["warm_pool"] = int(cur.rowcount or 0)
        conn.commit()
        if claims_path == path and _table_exists(conn, "claims"):
            placeholders = ",".join("?" for _ in _ACTIVE_CLAIM_STATES)
            cur = conn.execute(
                f"DELETE FROM claims WHERE updated_at < ? AND status NOT IN ({placeholders})",
                (cutoff, *_ACTIVE_CLAIM_STATES),
            )
            report["claims"] = int(cur.rowcount or 0)
            conn.commit()
        if report["wal_bytes_before"] > int(wal_max_bytes):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
            report["wal_checkpointed"] = True
            report["wal_bytes_after"] = _wal_bytes(path)
        row = conn.execute("PRAGMA integrity_check").fetchone()
        report["integrity"] = str(row[0]) if row else "unknown"
    finally:
        conn.close()

    if claims_path != path:
        cconn = sqlite3.connect(str(claims_path), timeout=10.0)
        cconn.execute("PRAGMA busy_timeout=10000")
        try:
            if _table_exists(cconn, "claims"):
                placeholders = ",".join("?" for _ in _ACTIVE_CLAIM_STATES)
                cur = cconn.execute(
                    f"DELETE FROM claims WHERE updated_at < ? AND status NOT IN ({placeholders})",
                    (cutoff, *_ACTIVE_CLAIM_STATES),
                )
                report["claims"] = int(cur.rowcount or 0)
                cconn.commit()
        finally:
            cconn.close()

    if log_fn is not None:
        log_fn(
            "[registry-purge] warm_pool=%s claims=%s history=%s wal=%s checkpoint=%s integrity=%s"
            % (
                report["warm_pool"],
                report["claims"],
                report["history"],
                report["wal_bytes_before"],
                report["wal_checkpointed"],
                report["integrity"],
            )
        )
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--claims-db", default=None)
    parser.add_argument("--retention-days", type=float, default=7.0)
    parser.add_argument("--wal-max-bytes", type=int, default=4 * 1024 * 1024)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 si PRAGMA integrity_check ne dit pas 'ok'",
    )
    args = parser.parse_args(argv)
    report = purge_once(
        args.db,
        claims_db=args.claims_db,
        retention_days=args.retention_days,
        wal_max_bytes=args.wal_max_bytes,
        log_fn=print,
    )
    print(json.dumps(report, indent=2, sort_keys=True))
    if args.check and str(report.get("integrity", "")).lower() != "ok":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
