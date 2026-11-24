#!/usr/bin/env python3
"""Purge et compaction bornées — feature 019, tâches T304 et T319.

Deux bases, deux politiques :

* ``registry.db`` : ``warm_pool`` expirés, ``claims`` et ``incidents`` clos de
  plus de 7 jours, ``slo_samples`` de plus de 30 jours ; checkpoint WAL
  au-delà de 4 Mio ; ``integrity_check`` systématique.
* ``requests.db`` : lignes de plus de 7 jours (pas seulement les corps),
  checkpoint WAL, ``VACUUM`` optionnel si la base dépasse le plafond.

Par défaut le script est en **dry-run** : il compte et n'écrit rien. Utiliser
``--apply`` pour exécuter. Aucune rotation, aucun redémarrage.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import sqlite3
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRY = os.path.join(ROOT, "logs", "registry.db")
REQUESTS = os.path.join(ROOT, "logs", "requests.db")
REGISTRY_RETENTION_DAYS = 7
SLO_RETENTION_DAYS = 30
REQUESTS_RETENTION_DAYS = 7
WAL_CHECKPOINT_BYTES = 4 * 1024 * 1024
REQUESTS_CAP_BYTES = 500 * 1024 * 1024


def _connect(path: str, *, read_only: bool) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    try:
        row = conn.execute(sql, params).fetchone()
        return int(row[0] or 0) if row else 0
    except sqlite3.Error:
        return 0


def purge_registry(path: str, *, apply: bool, days: int = REGISTRY_RETENTION_DAYS) -> dict[str, int]:
    now = time.time()
    cutoff = now - days * 86400
    counts = {
        "warm_pool": _count_read(path, "SELECT COUNT(*) FROM warm_pool WHERE expires_at<?", (now,)),
        "claims": _count_read(path, "SELECT COUNT(*) FROM claims WHERE updated_at<?", (cutoff,)),
        "incidents": _count_read(
            path,
            "SELECT COUNT(*) FROM incidents WHERE ended_at IS NOT NULL AND ended_at<?",
            (cutoff,),
        ),
        "slo_samples": _count_read(
            path, "SELECT COUNT(*) FROM slo_samples WHERE sampled_at<?", (now - SLO_RETENTION_DAYS * 86400,)
        ),
    }
    if not apply:
        return counts
    conn = _connect(path, read_only=False)
    try:
        conn.execute("DELETE FROM warm_pool WHERE expires_at<?", (now,))
        conn.execute("DELETE FROM claims WHERE updated_at<?", (cutoff,))
        conn.execute(
            "DELETE FROM incidents WHERE ended_at IS NOT NULL AND ended_at<?",
            (cutoff,),
        )
        try:
            conn.execute("DELETE FROM slo_samples WHERE sampled_at<?", (now - SLO_RETENTION_DAYS * 86400,))
        except sqlite3.Error:
            pass
        conn.commit()
        _checkpoint(conn, path)
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()
    return counts


def _count_read(path: str, sql: str, params: tuple = ()) -> int:
    try:
        conn = _connect(path, read_only=True)
        try:
            return _count(conn, sql, params)
        finally:
            conn.close()
    except sqlite3.Error:
        return 0


def _checkpoint(conn: sqlite3.Connection, path: str) -> bool:
    wal = f"{path}-wal"
    try:
        if os.path.exists(wal) and os.path.getsize(wal) >= WAL_CHECKPOINT_BYTES:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            return True
    except (OSError, sqlite3.Error):
        return False
    return False


def purge_requests(
    path: str,
    *,
    apply: bool,
    days: int = REQUESTS_RETENTION_DAYS,
    vacuum: bool = False,
    remove_backup: bool = False,
) -> dict[str, int]:
    cutoff = (
        (dt.datetime.now(dt.UTC) - dt.timedelta(days=days))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    counts = {
        "requests": _count_read(path, "SELECT COUNT(*) FROM requests WHERE timestamp<?", (cutoff,)),
        "free_model_usage": _count_read(
            path, "SELECT COUNT(*) FROM free_model_usage WHERE timestamp<?", (cutoff,)
        ),
        "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
    }
    if not apply:
        return counts
    conn = _connect(path, read_only=False)
    try:
        conn.execute("DELETE FROM requests WHERE timestamp<?", (cutoff,))
        try:
            conn.execute("DELETE FROM free_model_usage WHERE timestamp<?", (cutoff,))
        except sqlite3.Error:
            pass
        conn.commit()
        _checkpoint(conn, path)
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if vacuum or size > REQUESTS_CAP_BYTES:
            conn.execute("VACUUM")
            counts["vacuumed"] = 1
        counts["bytes_after"] = os.path.getsize(path) if os.path.exists(path) else 0
    finally:
        conn.close()
    if remove_backup:
        for name in os.listdir(os.path.dirname(path)):
            if name.startswith("requests.bak-") and name.endswith(".db"):
                try:
                    os.remove(os.path.join(os.path.dirname(path), name))
                    counts["backup_removed"] = counts.get("backup_removed", 0) + 1
                except OSError:
                    pass
    return counts


def integrity(path: str) -> str:
    try:
        conn = _connect(path, read_only=True)
        try:
            return str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return f"error:{exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="exécuter (défaut : dry-run)")
    parser.add_argument("--registry", default=REGISTRY)
    parser.add_argument("--requests", default=REQUESTS)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--vacuum", action="store_true")
    parser.add_argument("--remove-backup", action="store_true")
    parser.add_argument("--skip-requests", action="store_true")
    args = parser.parse_args(argv)
    print(f"mode={'apply' if args.apply else 'dry-run'}")
    print("registry", purge_registry(args.registry, apply=args.apply, days=args.days))
    print("registry integrity:", integrity(args.registry))
    if not args.skip_requests:
        print(
            "requests",
            purge_requests(
                args.requests,
                apply=args.apply,
                days=args.days,
                vacuum=args.vacuum,
                remove_backup=args.remove_backup,
            ),
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
