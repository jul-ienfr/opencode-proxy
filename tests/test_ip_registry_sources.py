"""Multi-source catalog resolution tests (offline)."""

from __future__ import annotations

import json
import os
import time

import pytest

from free.ip_registry.catalog import NordVpnCatalog
from free.ip_registry.sources import (
    JsonCatalogSource,
    OfficialApiSource,
    SourceManifest,
    SourceRegistry,
)
from free.ip_registry.sources.parsers import parse_gluetun_payload, parse_official_payload

MANIFEST = "docs/nordvpn_sources.yaml"


def _official(hostname: str = "al110.nordvpn.com", load: float = 12.0) -> list[dict]:
    return [
        {
            "status": "online",
            "hostname": hostname,
            "station": "186.247.44.61",
            "load": load,
            "locations": [{"country": {"name": "Albania"}, "city": {"name": "Tirana"}}],
            "technologies": [{"name": "WireGuard"}],
        }
    ]


def _embedded(*hostnames: str) -> dict:
    return {
        "servers": [
            {
                "vpn": "wireguard",
                "country": "Albania",
                "city": "Tirana",
                "hostname": hostname,
                "ips": ["87.120.102.83"],
            }
            for hostname in hostnames
        ]
    }


def _write_json(path, payload) -> str:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def test_manifest_loads_primary_fallbacks_and_completeness() -> None:
    manifest = SourceManifest.load(MANIFEST)
    assert manifest.version == 1
    assert manifest.hierarchy["catalog_primary"] == "nordvpn_api_servers"
    assert manifest.catalog_order()[0] == "nordvpn_api_servers"
    assert "gluetun_embedded_catalog" in manifest.catalog_order()
    assert "gluetun_embedded_catalog" in manifest.completeness_order()
    assert all(source_id in manifest.sources for source_id in manifest.catalog_order())


def test_manifest_rejects_duplicate_ids(tmp_path) -> None:
    data = {
        "version": 1,
        "sources": [
            {
                "id": "same",
                "name": "x",
                "url": "https://example.test/x",
                "category": "official",
                "kind": "catalog",
                "auth": "none",
                "refresh": "6h",
                "status": "active",
            },
            {
                "id": "same",
                "name": "y",
                "url": "https://example.test/y",
                "category": "official",
                "kind": "catalog",
                "auth": "none",
                "refresh": "6h",
                "status": "active",
            },
        ],
        "hierarchy": {},
    }
    path = tmp_path / "sources.yaml"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate source id"):
        SourceManifest.load(path)


def test_manifest_rejects_dangling_hierarchy_reference(tmp_path) -> None:
    data = {
        "version": 1,
        "sources": [
            {
                "id": "official",
                "name": "x",
                "url": "https://example.test/x",
                "category": "official",
                "kind": "catalog",
                "auth": "none",
                "refresh": "6h",
                "status": "active",
            }
        ],
        "hierarchy": {"catalog_fallbacks": ["missing"]},
    }
    path = tmp_path / "sources.yaml"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown source"):
        SourceManifest.load(path)


async def test_official_adapter_parses_and_exposes_provenance() -> None:
    source = OfficialApiSource(fetch=lambda: _official(), clock=lambda: 1234.0)
    snapshot = await source.fetch()
    assert snapshot.source_id == "nordvpn_api_servers"
    assert snapshot.fetched_at == 1234.0
    assert snapshot.stale is False
    assert snapshot.ok
    assert snapshot.servers[0].hostname == "al110.nordvpn.com"


async def test_official_adapter_failure_is_a_stale_error_snapshot() -> None:
    def fail():
        raise RuntimeError("api down")

    source = OfficialApiSource(fetch=fail, clock=lambda: 10.0)
    snapshot = await source.fetch()
    assert snapshot.servers == ()
    assert snapshot.stale is True
    assert "api down" in str(snapshot.error)


async def test_fallback_resolution_marks_rows_stale(tmp_path) -> None:
    embedded = _write_json(tmp_path / "embedded.json", _embedded("al41.nordvpn.com"))

    def fail():
        raise RuntimeError("api down")

    registry = SourceRegistry(manifest=SourceManifest.load(MANIFEST))
    registry.register(OfficialApiSource(fetch=fail))
    registry.register(
        JsonCatalogSource(
            source_id="gluetun_embedded_catalog",
            path=embedded,
            parser=parse_gluetun_payload,
            always_stale=True,
        )
    )
    resolution = await registry.resolve()
    assert resolution.ok
    assert resolution.fallback_used is True
    assert resolution.primary_used is False
    assert resolution.stale is True
    assert resolution.source_id == "gluetun_embedded_catalog"
    assert any("api down" in error for error in resolution.errors)
    assert all(server.stale for server in resolution.servers)
    assert all(
        server.source_id == "gluetun_embedded_catalog" for server in resolution.servers
    )


async def test_primary_wins_and_completeness_only_adds_missing(tmp_path) -> None:
    embedded = _write_json(
        tmp_path / "embedded.json",
        _embedded("al110.nordvpn.com", "al41.nordvpn.com"),
    )
    registry = SourceRegistry(manifest=SourceManifest.load(MANIFEST))
    registry.register(OfficialApiSource(fetch=lambda: _official(load=12.0)))
    registry.register(
        JsonCatalogSource(
            source_id="gluetun_embedded_catalog",
            path=embedded,
            parser=parse_gluetun_payload,
            always_stale=True,
            complete=False,
        )
    )
    resolution = await registry.resolve()
    by_host = {server.hostname: server for server in resolution.servers}
    assert resolution.primary_used is True
    assert resolution.fallback_used is False
    assert resolution.stale is False
    assert by_host["al110.nordvpn.com"].load == 12.0
    assert by_host["al110.nordvpn.com"].source_id == "nordvpn_api_servers"
    assert by_host["al110.nordvpn.com"].stale is False
    assert by_host["al41.nordvpn.com"].source_id == "gluetun_embedded_catalog"
    assert by_host["al41.nordvpn.com"].stale is True
    assert resolution.supplementary == ("gluetun_embedded_catalog",)


async def test_missing_source_returns_empty_error_snapshot(tmp_path) -> None:
    source = JsonCatalogSource(
        source_id="gluetun_embedded_catalog",
        path=tmp_path / "absent.json",
        parser=parse_gluetun_payload,
    )
    snapshot = await source.fetch()
    assert snapshot.servers == ()
    assert snapshot.stale is True
    assert "missing file" in str(snapshot.error)


async def test_old_cache_file_is_marked_stale(tmp_path) -> None:
    path = tmp_path / "nordvpn_api.json"
    path.write_text(json.dumps(_official()), encoding="utf-8")
    old = time.time() - 7200
    os.utime(path, (old, old))
    source = JsonCatalogSource(
        source_id="nordvpn_api_cache",
        path=path,
        parser=parse_official_payload,
        max_age_s=3600,
    )
    snapshot = await source.fetch()
    assert snapshot.ok
    assert snapshot.stale is True


async def test_catalog_from_config_uses_sources_section(tmp_path) -> None:
    embedded = _write_json(tmp_path / "embedded.json", _embedded("al41.nordvpn.com"))
    manifest = tmp_path / "sources.yaml"
    manifest.write_text(
        (SourceManifest.load(MANIFEST).path).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    catalog = NordVpnCatalog.from_config(
        {
            "catalog_refresh_s": 60,
            "sources": {
                "manifest": str(manifest),
                "embedded_path": embedded,
                "cache_path": str(tmp_path / "absent-cache.json"),
                "gluetun_manifest_path": str(tmp_path / "absent-manifest.json"),
            },
        }
    )
    # No network: replace the official adapter with a local fake.
    catalog._sources.register(OfficialApiSource(fetch=lambda: _official()))
    servers = await catalog.refresh()
    assert {server.hostname for server in servers} == {
        "al110.nordvpn.com",
        "al41.nordvpn.com",
    }
    assert catalog.source_id == "nordvpn_api_servers"
    assert catalog.stale is False


def test_stamp_helpers_keep_rows_immutable() -> None:
    from free.ip_registry.sources.parsers import stamp_servers

    parsed = parse_official_payload(_official())
    assert parsed[0].source_id == ""
    stamped = stamp_servers(
        parsed,
        source_id="nordvpn_api_servers",
        stale=False,
        fetched_at=99.0,
    )
    assert stamped[0].source_id == "nordvpn_api_servers"
    assert stamped[0].stale is False
    assert stamped[0].catalog_fetched_at == 99.0
    assert parsed[0].source_id == ""
