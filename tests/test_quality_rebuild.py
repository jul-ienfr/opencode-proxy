"""Tests for the boot-time hostname quality rebuild."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from free.quality import QualityConfig, QualityRegistry


def _db(path, *, with_host: bool = True) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE requests ("
        "timestamp TEXT, station INTEGER, free_model_ip TEXT, success INTEGER, "
        "tokens_output INTEGER, duration_ms INTEGER, ttfb_ms INTEGER"
        f"{', server_host TEXT' if with_host else ''})"
    )
    ts = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    columns = (
        "timestamp, station, free_model_ip, success, tokens_output, duration_ms, ttfb_ms"
        + (", server_host" if with_host else "")
    )
    placeholders = ",".join("?" for _ in range(8 if with_host else 7))
    base = (ts, 1, "203.0.113.7")
    host = ("nl1050.nordvpn.com",) if with_host else ()
    conn.execute(
        f"INSERT INTO requests ({columns}) VALUES({placeholders})",
        base + (1, 100, 1000, 500) + host,
    )
    conn.execute(
        f"INSERT INTO requests ({columns}) VALUES({placeholders})",
        base + (1, 100, 1000, 500) + host,
    )
    conn.execute(
        f"INSERT INTO requests ({columns}) VALUES({placeholders})",
        base + (0, 0, 1000, None) + host,
    )
    conn.commit()
    conn.close()


def test_rebuild_uses_server_host_and_aggregates(tmp_path):
    db = tmp_path / "requests.db"
    _db(db)
    reg = QualityRegistry(QualityConfig(enabled=True))

    rebuilt = reg.rebuild_from_requests(str(db), hours=168)

    assert rebuilt == 1
    item = reg.get(1, "203.0.113.7")
    assert item.server_host == "nl1050.nordvpn.com"
    assert item.success == 2
    assert item.errors == 1
    assert item.tps_n == 2
    assert item.tps_ewma == pytest.approx(100.0)
    assert item.ttfb_n == 2
    assert item.ewma_ms == pytest.approx(500.0)
    assert item.last_ok_at > 0
    host = reg.host_quality("nl1050.nordvpn.com")
    assert host is not None and host["n"] >= 2


def test_rebuild_is_a_noop_without_server_host_column(tmp_path):
    db = tmp_path / "requests.db"
    _db(db, with_host=False)
    reg = QualityRegistry(QualityConfig(enabled=True))
    assert reg.rebuild_from_requests(str(db), hours=168) == 0
    assert reg._items == {}


def test_rebuild_handles_missing_db(tmp_path):
    reg = QualityRegistry(QualityConfig(enabled=True))
    assert reg.rebuild_from_requests(str(tmp_path / "absent.db"), hours=168) == 0
