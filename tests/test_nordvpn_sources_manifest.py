"""Contract tests for the NordVPN source registry manifest.

These tests are intentionally offline: they validate the registry itself, not
the upstream services. Live contract checks belong to the nightly probe.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import yaml

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "docs" / "nordvpn_sources.yaml"
DOC = ROOT / "docs" / "nordvpn-sources.md"

CATEGORIES = {
    "operator",
    "official",
    "project",
    "mirror",
    "community",
    "network",
    "commercial",
    "legacy",
    "tool",
}
KINDS = {
    "catalog",
    "catalog_frontend",
    "catalog_mirror",
    "proxy_list",
    "network_ranges",
    "config_archive",
    "geolocation",
    "credentials",
    "metadata",
    "observations",
    "static_ip_list",
    "dataset",
    "documentation",
    "tool",
}
AUTHS = {"none", "token", "paid", "license"}
REFRESHES = {
    "realtime",
    "5m",
    "6h",
    "24h",
    "daily",
    "weekly",
    "monthly",
    "static",
    "unknown",
    "on_demand",
    "regular",
}
STATUSES = {"active", "fallback", "to_evaluate", "stale", "dead", "reference"}


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def test_manifest_exists_and_has_sources() -> None:
    data = _manifest()
    assert data["version"] == 1
    assert data["sources"]


def test_operator_sources_are_recorded() -> None:
    data = _manifest()
    sources = {s["id"]: s for s in data["sources"]}
    expected = {
        "wg_nord_pages": "https://wg-nord.pages.dev/",
        "nordgen_selfhoster": "https://nordgen.selfhoster.win/",
        "ip_address_list_nordvpn": "https://github.com/ip-address-list/nordvpn",
        "netify_nordvpn": "https://www.netify.ai/resources/vpns/nordvpn",
    }
    for source_id, url in expected.items():
        assert source_id in sources
        assert sources[source_id]["url"] == url
        assert sources[source_id]["recorded_at"] == "2026-11-02"


def test_ids_are_unique() -> None:
    ids = [s["id"] for s in _manifest()["sources"]]
    assert len(ids) == len(set(ids))


def test_required_fields_and_enums() -> None:
    for source in _manifest()["sources"]:
        for field in ("id", "name", "url", "category", "kind", "auth", "refresh", "status"):
            assert source.get(field), (source.get("id"), field)
        assert source["category"] in CATEGORIES, source["id"]
        assert source["kind"] in KINDS, source["id"]
        assert source["auth"] in AUTHS, source["id"]
        assert source["refresh"] in REFRESHES, source["id"]
        assert source["status"] in STATUSES, source["id"]
        parsed = urlparse(source["url"])
        assert parsed.scheme in {"http", "https", "file", "container"}, source["id"]


def test_hierarchy_references_existing_ids() -> None:
    data = _manifest()
    ids = {s["id"] for s in data["sources"]}
    hierarchy = data["hierarchy"]
    for key, value in hierarchy.items():
        if isinstance(value, str):
            if key == "egress_truth":
                assert value == "observed_only"
            else:
                assert value in ids, (key, value)
        elif isinstance(value, list):
            for item in value:
                assert item in ids, (key, item)


def test_source_semantics() -> None:
    sources = {s["id"]: s for s in _manifest()["sources"]}

    assert sources["wg_nord_pages"]["kind"] == "catalog_frontend"
    assert "egress_ip" in sources["wg_nord_pages"]["not_gives"]

    assert sources["ip_address_list_nordvpn"]["kind"] == "proxy_list"
    assert "server_ip" in sources["ip_address_list_nordvpn"]["not_gives"]

    assert sources["netify_nordvpn"]["kind"] == "network_ranges"
    assert "per_server_egress_ip" in sources["netify_nordvpn"]["not_gives"]

    assert sources["cloud_ip_ranges_nordvpn"]["kind"] == "network_ranges"
    assert "egress_ip" in sources["cloud_ip_ranges_nordvpn"]["not_gives"]

    assert sources["x4bnet_lists_vpn"]["status"] == "active"
    assert "hostname" in sources["x4bnet_lists_vpn"]["not_gives"]

    assert sources["nordcache_packed"]["category"] == "mirror"
    assert "provenance_documented" in sources["nordcache_packed"]["not_gives"]


def test_doc_mentions_every_operator_source() -> None:
    text = DOC.read_text(encoding="utf-8")
    for url in (
        "https://wg-nord.pages.dev/",
        "https://nordgen.selfhoster.win/",
        "https://github.com/ip-address-list/nordvpn",
        "https://www.netify.ai/resources/vpns/nordvpn",
    ):
        assert url in text
