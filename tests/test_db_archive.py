"""test_db_archive.py — P2-11 : archivage mensuel auto (observability.db).

Contrats :
- archive_old_rows déplace les lignes > N jours vers
  logs/archive/requests-YYYY-MM.db, par mois, avec vérification ;
- le live ne garde que le récent ; le total live+archives est conservé ;
- idempotent (2e passage : 0 déplacé) ; mois en échec jamais supprimé ;
- weekly_maintain archive AVANT de purger (ordre) ;
- days <= 0 / db_path vide = no-op.
"""

import sqlite3

from observability import db as odb


def _live_db(tmp_path, rows):
    db_path = tmp_path / "logs" / "requests.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE requests (id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, model TEXT NOT NULL)"
    )
    conn.executemany("INSERT INTO requests (id, timestamp, model) VALUES (?, ?, 'm')", rows)
    conn.commit()
    return db_path, conn


def test_archive_moves_old_by_month(tmp_path):
    rows = [
        ("o1", "2026-01-10T10:00:00Z"),
        ("o2", "2026-01-20T10:00:00Z"),
        ("o3", "2026-02-05T10:00:00Z"),
        ("r1", "2999-01-01T00:00:00Z"),  # récent garanti (jamais archivé)
    ]
    db_path, conn = _live_db(tmp_path, rows)
    moved, months = odb.archive_old_rows(conn, str(db_path), 30)
    conn.commit()
    assert moved == 3
    assert months == ["2026-01", "2026-02"]
    live = [r[0] for r in conn.execute("SELECT id FROM requests ORDER BY id").fetchall()]
    assert live == ["r1"]
    import os

    arch_dir = os.path.join(os.path.dirname(str(db_path)), "archive")
    jan = sqlite3.connect(f"file:{arch_dir}/requests-2026-01.db?mode=ro", uri=True)
    try:
        got = sorted(r[0] for r in jan.execute("SELECT id FROM requests").fetchall())
    finally:
        jan.close()
    assert got == ["o1", "o2"]
    feb = sqlite3.connect(f"file:{arch_dir}/requests-2026-02.db?mode=ro", uri=True)
    try:
        got2 = [r[0] for r in feb.execute("SELECT id FROM requests").fetchall()]
    finally:
        feb.close()
    assert got2 == ["o3"]
    conn.close()


def test_archive_idempotent_and_noop(tmp_path):
    db_path, conn = _live_db(tmp_path, [("r1", "2999-01-01T00:00:00Z")])
    assert odb.archive_old_rows(conn, str(db_path), 30) == (0, [])
    assert odb.archive_old_rows(conn, str(db_path), 30) == (0, [])  # re-run
    assert odb.archive_old_rows(conn, str(db_path), 0) == (0, [])
    assert odb.archive_old_rows(conn, "", 30) == (0, [])
    conn.close()


def test_weekly_maintain_archives_before_purge(tmp_path):
    """Ordre : archive (60 j) puis purge (90 j) — les 60-90 j sont conservées."""
    import datetime as dt

    now = dt.datetime.now(dt.UTC)

    def _fmt(d):
        return (now - dt.timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")

    db_path, conn = _live_db(tmp_path, [("a70", _fmt(70)), ("a120", _fmt(120)), ("keep", _fmt(10))])
    import threading

    size = odb.weekly_maintain(
        conn, threading.Lock(), purge_days=90, archive_days=60, db_path=str(db_path)
    )
    assert isinstance(size, float)
    live = sorted(r[0] for r in conn.execute("SELECT id FROM requests").fetchall())
    assert live == ["keep"], live  # 70 j archivée, 120 j archivée+hors purge
    import glob
    import os

    arch = sorted(glob.glob(os.path.join(os.path.dirname(str(db_path)), "archive", "*.db")))
    assert len(arch) >= 1
    n_arch = 0
    for f in arch:
        c = sqlite3.connect(f"file:{f}?mode=ro", uri=True)
        try:
            n_arch += c.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
        finally:
            c.close()
    assert n_arch == 2, "70 j + 120 j conservées en archives (zéro perte)"
    conn.close()
