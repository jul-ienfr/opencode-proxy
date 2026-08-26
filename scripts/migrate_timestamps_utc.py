#!/usr/bin/env python3
"""One-shot migration: convert naive local-time timestamps to UTC+Z in logs/requests.db.

Mixed formats broke ORDER BY timestamp DESC: SQLite BINARY collation compares
'2026-08-16T17:01:30' (local, naive) above '2026-08-16T15:01:30Z' (UTC) because
'7' > '5' at position 9 — so the newest UTC rows sank below page 1 of the
dashboard history. Converging every row on the UTC+Z format makes string
ordering chronological again.

MUST be run on the proxy host — naive rows were written in that machine's
local time, and this script converts them using the SYSTEM timezone.

Idempotent: rows already ending in Z are left untouched, so re-running is safe.
A naive row that is NOT in the exact legacy format aborts the migration
(ROLLBACK, DB intact) — committing a still-mixed DB would keep the bug alive.

Usage:
    python scripts/migrate_timestamps_utc.py [--db PATH] [--dry-run]
                                             [--no-backup] [--backup-dir DIR]

Exit codes: 0 success (including dry-run and "nothing to convert"),
            1 migration failed / DB intact, 2 usage error.

Recommended: stop the proxy first (zero lock risk, tiny WAL). With the server
running, BEGIN IMMEDIATE waits up to ~5 s and holds the write lock ~1 s vs the
server's 5 s busy_timeout — worst case one insert is lost (auto-repairing).
"""

import argparse
import os
import re
import shutil
import sqlite3
import sys
from datetime import UTC, datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DB = os.path.join(ROOT, "logs", "requests.db")
TABLES = ["requests", "free_model_usage"]
# Exact legacy write format: time.strftime("%Y-%m-%dT%H:%M:%S") — local naive, no Z.
LEGACY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
BUSY_TIMEOUT_MS = 30000


def _uri(db_path: str, read_only: bool = False) -> str:
    """file: URI with forward slashes (Windows-safe)."""
    path = db_path.replace("\\", "/")
    return f"file:{path}?mode=ro" if read_only else f"file:{path}"


def _connect(db_path: str, read_only: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(_uri(db_path, read_only), uri=True)
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _db_stats(conn: sqlite3.Connection, table: str) -> tuple[int, int, int, int]:
    total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    naive = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE timestamp NOT LIKE '%Z'").fetchone()[
        0
    ]
    lo, hi = conn.execute(
        f"SELECT MIN(length(timestamp)), MAX(length(timestamp)) FROM {table}"
    ).fetchone()
    return total, naive, lo, hi


def _file_sizes(db_path: str) -> tuple[int, int, int]:
    def size(p: str) -> int:
        return os.path.getsize(p) if os.path.exists(p) else 0

    return size(db_path), size(db_path + "-wal"), size(db_path + "-shm")


def _check_disk(db_path: str) -> tuple[float, float, bool]:
    """Free space vs 2.5 × (DB+WAL): backup copies ~DB size and the rewrite of
    ~4 400 rows (bodies stored inline, ~700 KB/row) grows the WAL by ~DB size."""
    db_size, wal_size, shm_size = _file_sizes(db_path)
    needed = 2.5 * (db_size + wal_size + shm_size)
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path))).free
    return free, needed, free >= needed


def _backup(db_path: str, backup_dir: str) -> str:
    """Consistent snapshot via the SQLite backup API (includes WAL content)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_path = os.path.join(backup_dir, os.path.basename(db_path) + f".bak-{stamp}")
    src = _connect(db_path, read_only=True)
    dst = sqlite3.connect(dest_path)
    last_pct = -1

    def progress(status: int, remaining: int, total: int) -> None:
        nonlocal last_pct
        if total:
            pct = int((total - remaining) * 100 / total)
            if pct >= last_pct + 10 or pct == 100:
                last_pct = pct
                print(f"  backup: {pct}%", flush=True)

    print(f"  backup -> {dest_path}")
    # Count the source BEFORE the backup: with the server running, inserts
    # between snapshot and recount are expected, not corruption. The snapshot
    # must contain at least what existed when we started.
    src_count = src.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    src.backup(dst, progress=progress)
    dst_count = dst.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    quick = dst.execute("PRAGMA quick_check").fetchone()
    dst.close()
    src.close()
    if dst_count < src_count or quick is None or quick[0] != "ok":
        raise RuntimeError(
            f"backup verification failed (src={src_count} dst={dst_count} quick_check={quick})"
        )
    if dst_count > src_count:
        print(
            f"  note: {dst_count - src_count} row(s) inserted during backup (live server) — snapshot is consistent"
        )
    print(f"  backup verified: {dst_count} rows, quick_check={quick[0]}")
    return dest_path


def _migrate(conn: sqlite3.Connection) -> tuple[dict, int]:
    """Convert all naive rows in both tables. Returns (per-table converted, total).

    A naive row NOT in the exact legacy format raises ValueError — the caller
    rolls back and exits 1, leaving the DB untouched.
    """
    per_table: dict[str, int] = {}
    converted_total = 0
    for table in TABLES:
        rows = conn.execute(
            f"SELECT id, timestamp FROM {table} WHERE timestamp NOT LIKE '%Z'"
        ).fetchall()
        converted = 0
        for rid, ts in rows:
            if not LEGACY_RE.match(ts):
                raise ValueError(
                    f"{table} row id={rid!r} has non-legacy timestamp {ts!r} — "
                    "cannot convert; aborting (DB intact)"
                )
            new_ts = (
                datetime.fromisoformat(ts)
                .astimezone()
                .astimezone(UTC)
                .strftime("%Y-%m-%dT%H:%M:%SZ")
            )
            conn.execute(
                f"UPDATE {table} SET timestamp = ? WHERE id = ? AND timestamp NOT LIKE '%Z'",
                (new_ts, rid),
            )
            converted += 1
            converted_total += 1
            if converted_total % 500 == 0:
                print(f"  {table}: {converted_total} converted...", flush=True)
        per_table[table] = converted
    return per_table, converted_total


def _verify(conn: sqlite3.Connection) -> bool:
    ok = True
    for table in TABLES:
        naive = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE timestamp NOT LIKE '%Z'"
        ).fetchone()[0]
        bad_len = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE length(timestamp) NOT IN (19, 20)"
        ).fetchone()[0]
        if naive or bad_len:
            ok = False
            print(f"  verify FAILED: {table} naive={naive} bad_lengths={bad_len}", file=sys.stderr)
    return ok


def main() -> None:
    print(
        "migrate_timestamps_utc — run on the proxy host: rows were written in that machine's local time"
    )
    parser = argparse.ArgumentParser(
        description="Convert naive local timestamps to UTC+Z in requests.db"
    )
    parser.add_argument(
        "--db", default=DEFAULT_DB, help=f"SQLite database path (default: {DEFAULT_DB})"
    )
    parser.add_argument("--dry-run", action="store_true", help="report only, write nothing, exit 0")
    parser.add_argument(
        "--no-backup",
        action="store_true",
        help="skip the .bak file (idempotent migration; not recommended)",
    )
    parser.add_argument(
        "--backup-dir", default=None, help="directory for the backup (default: next to the DB)"
    )
    args = parser.parse_args()

    db_path = os.path.abspath(args.db)
    if not os.path.exists(db_path):
        raise SystemExit(f"ERROR: database not found: {db_path}")

    conn = _connect(db_path, read_only=True)
    existing = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('requests','free_model_usage')"
        ).fetchall()
    }
    missing = [t for t in TABLES if t not in existing]
    if missing:
        conn.close()
        raise SystemExit(f"ERROR: missing table(s) {missing} in {db_path}")

    print(f"pre-flight: {db_path}")
    for table in TABLES:
        total, naive, lo, hi = _db_stats(conn, table)
        print(f"  {table}: {total} rows, {naive} naive (no Z), length range {lo}-{hi}")

    free, needed, enough = _check_disk(db_path)
    print(
        f"  disk free: {free / 1e9:.2f} GiB, needed: {needed / 1e9:.2f} GiB (2.5x DB+WAL) -> {'OK' if enough else 'INSUFFICIENT'}"
    )
    backup_path = os.path.join(
        args.backup_dir or os.path.dirname(db_path), os.path.basename(db_path) + ".bak-<timestamp>"
    )
    if args.dry_run:
        print(f"dry-run: no changes. Planned backup: {backup_path}")
        conn.close()
        raise SystemExit(0)
    if not enough:
        conn.close()
        raise SystemExit(
            "ERROR: insufficient free disk space for backup + WAL growth — aborting, DB untouched"
        )

    if args.no_backup:
        print(
            "WARNING: --no-backup — no snapshot will be written (migration is deterministic/idempotent)",
            file=sys.stderr,
        )
        backup_path = None
    else:
        backup_path = _backup(db_path, args.backup_dir or os.path.dirname(db_path))
    conn.close()

    rw = _connect(db_path)
    try:
        rw.execute("PRAGMA journal_mode=WAL")
        rw.execute("BEGIN IMMEDIATE")
        per_table, converted = _migrate(rw)
        if not _verify(rw):
            rw.execute("ROLLBACK")
            rw.close()
            raise SystemExit("ERROR: pre-commit verification failed — ROLLED BACK, DB intact")
        rw.execute("COMMIT")
        rw.close()
    except SystemExit:
        raise
    except Exception as e:
        try:
            rw.execute("ROLLBACK")
        except Exception:
            pass
        rw.close()
        raise SystemExit(
            f"ERROR: migration failed ({type(e).__name__}: {e}) — ROLLED BACK, DB intact"
        ) from e

    for table in TABLES:
        print(f"  {table}: {per_table[table]} converted")
    print(f"summary: {converted} converted, backup={backup_path}")
    print("done — restart the proxy; every new row is now UTC+Z.")


if __name__ == "__main__":
    main()
