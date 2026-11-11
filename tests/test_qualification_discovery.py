"""Discovery scheduler tests: refresh, removal, failure is fail-soft."""

from __future__ import annotations

import json

from free.qualification.discovery import DiscoveryConfig, DiscoveryService
from free.qualification.source import NordVpnCatalogSource
from free.qualification.store import QualificationStore


def _row(hostname: str, **changes):
    base = {
        "hostname": hostname,
        "status": "online",
        "load": 10,
        "locations": [{"country": {"name": "Sweden", "city": {"name": "Stockholm"}}}],
        "technologies": [{"identifier": "wireguard_udp"}, {"identifier": "openvpn_udp"}],
    }
    base.update(changes)
    return base


def test_refresh_ingests_and_records_run(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(json.dumps([_row("a.nordvpn.com"), _row("b.nordvpn.com")]), encoding="utf-8")
    store = QualificationStore(tmp_path / "registry.db")
    try:
        service = DiscoveryService(
            store,
            NordVpnCatalogSource(path),
            config=DiscoveryConfig(interval_s=3600),
            clock=lambda: 100.0,
        )
        report = service.run(force=True)
        assert report.status == "ok"
        assert report.server_count == 2
        assert report.transport_count == 2
        assert store.summary()["total"] == 2
        assert store.last_discovery_run()["status"] == "ok"
    finally:
        store.close()


def test_removed_host_is_skipped(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(json.dumps([_row("a.nordvpn.com"), _row("b.nordvpn.com")]), encoding="utf-8")
    store = QualificationStore(tmp_path / "registry.db")
    try:
        service = DiscoveryService(store, NordVpnCatalogSource(path), clock=lambda: 100.0)
        service.run(force=True)
        path.write_text(json.dumps([_row("a.nordvpn.com")]), encoding="utf-8")
        report = service.run(force=True)
        assert report.removed == 1
        removed = store.get("b.nordvpn.com")
        assert removed is not None
        assert str(removed.status) == "skipped"
        assert removed.skipped_reason == "removed-from-catalog"
    finally:
        store.close()


def test_invalid_catalog_keeps_previous_queue(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(json.dumps([_row("a.nordvpn.com")]), encoding="utf-8")
    store = QualificationStore(tmp_path / "registry.db")
    try:
        service = DiscoveryService(store, NordVpnCatalogSource(path), clock=lambda: 100.0)
        service.run(force=True)
        path.write_text("{not json", encoding="utf-8")
        report = service.run(force=True)
        assert report.status == "failed"
        assert report.error
        assert store.summary()["total"] == 1
        assert store.last_discovery_run()["status"] == "failed"
    finally:
        store.close()


def test_is_due_uses_interval(tmp_path):
    path = tmp_path / "api.json"
    path.write_text(json.dumps([_row("a.nordvpn.com")]), encoding="utf-8")
    store = QualificationStore(tmp_path / "registry.db")
    try:
        now = {"value": 100.0}
        service = DiscoveryService(
            store,
            NordVpnCatalogSource(path),
            config=DiscoveryConfig(interval_s=3600),
            clock=lambda: now["value"],
        )
        assert service.is_due()
        service.run(force=True)
        assert not service.is_due()
        now["value"] = 4000.0
        assert service.is_due()
    finally:
        store.close()
