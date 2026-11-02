"""Read-only status endpoint contract (P0.3)."""

from __future__ import annotations

from pathlib import Path

from free.ip_registry.domain import Server
from free.ip_registry.sources import SourceSnapshot
from free.ip_registry.sqlite_registry import SqliteIpRegistry
from free.ip_registry.status import registry_status

ROOT = Path(__file__).resolve().parent.parent


def _config(tmp_path: Path, db: Path) -> dict:
    return {
        "enabled": False,
        "db_path": str(db),
        "allocator": {"mode": "off"},
        "binder": {"mode": "compose_hostname"},
        "sources": {
            "manifest": str(ROOT / "docs" / "nordvpn_sources.yaml"),
            "embedded_path": str(tmp_path / "missing-embedded.json"),
            "cache_path": str(tmp_path / "missing-cache.json"),
            "gluetun_manifest_path": str(tmp_path / "missing-manifest.json"),
        },
    }


def test_status_reports_disabled_registry_and_provenance(tmp_path):
    db = tmp_path / "registry.db"
    reg = SqliteIpRegistry(str(db))
    try:
        reg.sync_catalog(
            [
                Server(hostname="a.nordvpn.com", country="Albania"),
                Server(
                    hostname="b.nordvpn.com",
                    country="Albania",
                    source_id="gluetun_embedded_catalog",
                    stale=True,
                ),
            ],
            now=1000.0,
        )
        reg.record_catalog_snapshot(
            SourceSnapshot(
                source_id="nordvpn_api_servers",
                servers=(Server(hostname="a.nordvpn.com", country="Albania"),),
                fetched_at=1000.0,
                stale=False,
                etag="v1",
            ),
            now=1000.0,
        )
    finally:
        reg.close()

    status = registry_status(_config(tmp_path, db), db_path=db)
    assert status["enabled"] is False
    assert status["allocator_mode"] == "off"
    assert status["allocator"]["mode"] == "off"
    assert isinstance(status["allocator"]["chains"], list)
    assert "claims" in status["allocator"]
    assert status["db"]["exists"] is True
    assert status["db"]["schema_version"] == 2
    assert status["db"]["servers"]["active"] == 2
    assert status["db"]["sources"] == {
        "gluetun_embedded_catalog": 1,
        "nordvpn_api_servers": 1,
    }
    assert status["db"]["snapshots"][0]["etag"] == "v1"
    adapters = {item["id"]: item["adapter"] for item in status["sources"]}
    assert adapters["nordvpn_api_servers"] is True
    assert adapters["nordcache_packed"] is False
    assert adapters["gluetun_embedded_catalog"] is False
    assert "nordcache_packed" in status["no_adapter"]


def test_status_works_without_database(tmp_path):
    status = registry_status(_config(tmp_path, tmp_path / "absent.db"))
    assert status["db"]["exists"] is False
    assert status["db"]["schema_version"] is None
    assert status["db"]["servers"] == {}
    assert status["sources"]
