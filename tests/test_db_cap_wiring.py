"""[T304/T319] Câblage des plafonds de taille DB.

Régression : `enforce_size_cap` et `cleanup_legacy_backups` existaient mais
n'étaient appelés nulle part. Ce test fige le câblage :

* les trois clés `database.*` sont présentes et valides dans config.yaml ;
* `run_once` (registre de rotation) applique le cap registre, purge les
  anciens dumps `requests.db.bak*` et expose `cap_ok`/`cap_bytes` ;
* `weekly_maintain` (requests.db) appelle `enforce_size_cap` après VACUUM
  quand il est présent, et reste utilisable s'il a disparu (garde).
"""

from __future__ import annotations

import os
import sqlite3
import threading

import yaml

from observability import db as odb
from rotation.infrastructure.db import maintenance as maint

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.yaml")


# ── 1. config.yaml ──────────────────────────────────────────────────


def test_config_declares_the_three_caps():
    with open(CONFIG, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    database = data["database"]
    assert database["max_bytes"] == 524_288_000
    assert database["registry_max_bytes"] == 52_428_800
    assert database["legacy_backup_max_bytes"] == 1_300_000_000
    for key in ("max_bytes", "registry_max_bytes", "legacy_backup_max_bytes"):
        assert isinstance(database[key], int) and database[key] > 0


def test_maintenance_reads_the_caps_from_config():
    assert maint._db_caps() == (52_428_800, 1_300_000_000)


def test_configured_size_cap_matches_config():
    assert odb.configured_size_cap() == 524_288_000


# ── 2. run_once : cap registre + purge des dumps legacy ─────────────


def test_run_once_on_a_small_registry_reports_cap_ok(tmp_path):
    report = maint.run_once(tmp_path / "registry.db")

    assert report["cap_ok"] is True
    assert report["cap_bytes"] == 52_428_800
    assert report["legacy_backups_deleted"] == 0
    assert report["integrity_ok"] is True


def test_run_once_deletes_large_old_legacy_backup_and_keeps_newest(tmp_path, monkeypatch):
    # Cap legacy minuscule pour ne pas créer de vrais fichiers de 1,3 Go.
    monkeypatch.setattr(maint, "_db_caps", lambda: (52_428_800, 100))

    newest = tmp_path / "requests.db.bak-newest"
    older = tmp_path / "requests.db.bak-older"
    newest.write_bytes(b"n" * 500)
    older.write_bytes(b"o" * 500)
    os.utime(newest, (1_700_000_200, 1_700_000_200))
    os.utime(older, (1_700_000_100, 1_700_000_100))

    report = maint.run_once(tmp_path / "registry.db")

    assert report["cap_ok"] is True
    assert report["legacy_backups_deleted"] == 1
    assert newest.exists(), "le plus récent est toujours conservé"
    assert not older.exists(), "l'ancien dump volumineux est supprimé"


def test_run_once_survives_a_broken_cap_function(tmp_path, monkeypatch):
    """Fail-soft : une erreur du cap n'empêche pas le repair loop de démarrer."""
    import observability.db as odb_mod

    def _boom(*a, **k):
        raise RuntimeError("cap cassé")

    monkeypatch.setattr(odb_mod, "enforce_size_cap", _boom)
    report = maint.run_once(tmp_path / "registry.db")

    assert report["cap_ok"] is False
    assert report["cap_bytes"] == 52_428_800
    assert report["integrity_ok"] is True


# ── 3. cleanup_legacy_backups ───────────────────────────────────────


def test_cleanup_deletes_large_fake_backup_and_keeps_newest(tmp_path):
    newest = tmp_path / "requests.db.bak-2026-01-02.db"
    older = tmp_path / "requests.db.bak-2026-01-01.db"
    newest.write_bytes(b"n" * 2_000)
    older.write_bytes(b"o" * 2_000)
    os.utime(newest, (1_700_000_200, 1_700_000_200))
    os.utime(older, (1_700_000_100, 1_700_000_100))

    logs: list[str] = []
    deleted = odb.cleanup_legacy_backups(str(tmp_path), max_bytes=1_000, keep=1, log_fn=logs.append)

    assert deleted == 1
    assert newest.exists()
    assert not older.exists()
    assert any("deleted" in line for line in logs)


# ── 4. weekly_maintain : enforce_size_cap après VACUUM ──────────────


def _make_requests_db(path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    odb.init_requests_schema_fast(conn, busy_timeout=5000, cache_size=2000, mmap_size=0)
    conn.execute(
        "INSERT INTO requests (id, timestamp, model) VALUES (?, ?, ?)",
        ("r1", "2026-01-01T00:00:00Z", "m"),
    )
    conn.commit()
    return conn


def test_weekly_maintain_calls_enforce_size_cap_and_keeps_return_value(tmp_path, monkeypatch):
    path = tmp_path / "requests.db"
    conn = _make_requests_db(path)
    calls: list[tuple[str, int]] = []

    def _fake_enforce(c, db_path, *, max_bytes, log_fn=None):
        calls.append((str(db_path), int(max_bytes)))
        return True

    monkeypatch.setattr(odb, "enforce_size_cap", _fake_enforce)
    size_mb = odb.weekly_maintain(conn, threading.Lock(), purge_days=0, db_path=str(path))
    conn.close()

    assert isinstance(size_mb, float) and size_mb >= 0
    assert calls == [(str(path), 524_288_000)]


def test_weekly_maintain_works_without_enforce_size_cap(tmp_path, monkeypatch):
    """Garde `globals()` : la fonction retirée ne doit pas lever NameError."""
    path = tmp_path / "requests.db"
    conn = _make_requests_db(path)
    monkeypatch.delattr(odb, "enforce_size_cap")
    size_mb = odb.weekly_maintain(conn, threading.Lock(), purge_days=0, db_path=str(path))
    conn.close()
    assert isinstance(size_mb, float) and size_mb >= 0
