"""QuotaLedger SQLite backend tests (registry.db)."""

from __future__ import annotations

from free.quota_ledger import QuotaLedger

NOW = 1_793_000_000.0


def test_quota_ledger_persists_buckets_in_registry_db(tmp_path):
    db = tmp_path / "registry.db"
    ledger = QuotaLedger(path=str(db), persist=True, clock=lambda: NOW)
    ledger.note_request("1.1.1.1", "m", now=NOW)
    ledger.note_result(
        "1.1.1.1",
        "m",
        429,
        error_type="FreeUsageLimitError",
        retry_after=100.0,
        now=NOW,
    )
    ledger.flush()
    assert ledger.is_exhausted("1.1.1.1", "m", now=NOW)
    assert ledger.reset_at("1.1.1.1", "m", now=NOW) == NOW + 100.0

    reopened = QuotaLedger(path=str(db), persist=True, clock=lambda: NOW)
    assert reopened.is_exhausted("1.1.1.1", "m", now=NOW)
    assert reopened.reset_at("1.1.1.1", "m", now=NOW) == NOW + 100.0

    reopened.note_result("1.1.1.1", "m", 200, now=NOW)
    reopened.flush()
    again = QuotaLedger(path=str(db), persist=True, clock=lambda: NOW)
    assert not again.is_exhausted("1.1.1.1", "m", now=NOW)


def test_sqlite_purges_legacy_socks5_station_keys(tmp_path):
    db = tmp_path / "registry.db"
    ledger = QuotaLedger(path=str(db), persist=True, clock=lambda: NOW)
    ledger.note_result(
        "socks5:warp-ipv6-4",
        "m",
        429,
        error_type="FreeUsageLimitError",
        retry_after=1200.0,
        now=NOW,
    )
    ledger.flush()
    assert ledger.is_exhausted("socks5:warp-ipv6-4", "m", now=NOW)

    reopened = QuotaLedger(path=str(db), persist=True, clock=lambda: NOW)
    assert not reopened.is_exhausted("socks5:warp-ipv6-4", "m", now=NOW)
    assert [row for row in reopened.snapshot() if row["ip"].startswith("socks5:")] == []

    # La purge est durable : une nouvelle ouverture ne les revoit pas.
    again = QuotaLedger(path=str(db), persist=True, clock=lambda: NOW)
    assert again.snapshot() == []


def test_quota_ledger_json_backend_is_unchanged(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = QuotaLedger(path=str(path), persist=True, clock=lambda: NOW)
    ledger.note_result(
        "2.2.2.2",
        "m",
        429,
        error_type="FreeUsageLimitError",
        retry_after=50.0,
        now=NOW,
    )
    ledger.flush()
    assert path.exists()
    restored = QuotaLedger(path=str(path), persist=True, clock=lambda: NOW)
    assert restored.is_exhausted("2.2.2.2", "m", now=NOW)
