"""NordVPN catalog adapter tests (no network)."""

from __future__ import annotations

import json

from free.ip_registry.catalog import NordVpnCatalog


def _fake_servers():
    return [
        {
            "status": "online",
            "hostname": "al110.nordvpn.com",
            "station": "186.247.44.61",
            "load": 12,
            "locations": [
                {
                    "country": {"name": "Albania"},
                    "city": {"name": "Tirana"},
                }
            ],
            "technologies": [
                {"name": "WireGuard"},
                {"name": "OpenVPN"},
            ],
        },
        {
            "status": "offline",
            "hostname": "offline.example",
            "station": "1.2.3.4",
            "locations": [{"country": {"name": "France"}}],
        },
        {
            "status": "online",
            "hostname": "fr1.nordvpn.com",
            "station": "5.6.7.8",
            "load": 40,
            "locations": [{"country": {"name": "France"}, "city": {"name": "Paris"}}],
            "technologies": [{"name": "WireGuard"}],
        },
    ]


async def test_refresh_parses_online_servers_only():
    catalog = NordVpnCatalog(
        fetch=_fake_servers,
        extra_path=None,
        cache_path=None,
        gluetun_manifest_path=None,
    )
    servers = await catalog.refresh()
    assert [s.hostname for s in servers] == ["al110.nordvpn.com", "fr1.nordvpn.com"]
    albania = servers[0]
    assert albania.country == "Albania"
    assert albania.city == "Tirana"
    assert albania.server_ip == "186.247.44.61"
    assert albania.load == 12.0
    assert set(albania.technologies) == {"WireGuard", "OpenVPN"}
    assert albania.source_id == "nordvpn_api_servers"
    assert albania.stale is False
    assert catalog.source_id == "nordvpn_api_servers"
    assert catalog.stale is False


async def test_refresh_failure_keeps_previous_snapshot():
    state = {"fail": False}

    def fetch():
        if state["fail"]:
            raise RuntimeError("network down")
        return _fake_servers()

    catalog = NordVpnCatalog(
        fetch=fetch,
        extra_path=None,
        cache_path=None,
        gluetun_manifest_path=None,
    )
    await catalog.refresh()
    before = catalog.snapshot()
    state["fail"] = True
    after = await catalog.refresh()
    assert after == before
    assert catalog.last_error is not None
    assert "network down" in catalog.last_error
    assert catalog.stale is True


async def test_staleness():
    now = {"t": 1000.0}
    catalog = NordVpnCatalog(
        fetch=_fake_servers,
        ttl_s=100.0,
        clock=lambda: now["t"],
        extra_path=None,
        cache_path=None,
        gluetun_manifest_path=None,
    )
    assert catalog.is_stale()
    await catalog.refresh()
    assert not catalog.is_stale()
    now["t"] += 101.0
    assert catalog.is_stale()


def _embedded():
    return {
        "version": 5,
        "servers": [
            {
                "vpn": "wireguard",
                "country": "Albania",
                "city": "Tirana",
                "hostname": "al41.nordvpn.com",
                "wgpubkey": "0sAxvVg+N0it7/I4PzWdJdtKY66diiuTsXEfYGThKjg=",
                "ips": ["87.120.102.83"],
            },
            {
                "vpn": "openvpn",
                "country": "Albania",
                "hostname": "al41.nordvpn.com",
                "ips": ["87.120.102.83"],
            },
            {
                "vpn": "wireguard",
                "country": "Albania",
                "city": "Tirana",
                "hostname": "al75.nordvpn.com",
                "wgpubkey": "0sAxvVg+N0it7/I4PzWdJdtKY66diiuTsXEfYGThKjg=",
                "ips": ["87.120.102.67"],
            },
        ],
    }


async def test_embedded_catalog_adds_servers_missing_from_the_api(tmp_path):
    extra = tmp_path / "embedded.json"
    extra.write_text(json.dumps(_embedded()), encoding="utf-8")
    catalog = NordVpnCatalog(
        fetch=_fake_servers,
        extra_path=extra,
        cache_path=None,
        gluetun_manifest_path=None,
    )
    servers = await catalog.refresh()
    by_host = {s.hostname: s for s in servers}
    assert "al41.nordvpn.com" in by_host
    assert "al75.nordvpn.com" in by_host
    al41 = by_host["al41.nordvpn.com"]
    # Only the WireGuard entry is kept for an embedded hostname.
    assert al41.technologies == ("WireGuard",)
    assert al41.server_ip == "87.120.102.83"
    assert by_host["al75.nordvpn.com"].server_ip == "87.120.102.67"
    assert by_host["al75.nordvpn.com"].technologies == ("WireGuard",)
    # The OpenVPN duplicate of al41 must not create a second entry.
    assert len([s for s in servers if s.hostname == "al41.nordvpn.com"]) == 1
    # al110/fr1 come from the fake API.
    assert "al110.nordvpn.com" in by_host and "fr1.nordvpn.com" in by_host
    # Provenance: API rows are fresh, supplementary rows are marked stale.
    assert by_host["al110.nordvpn.com"].source_id == "nordvpn_api_servers"
    assert by_host["al110.nordvpn.com"].stale is False
    assert al41.source_id == "gluetun_embedded_catalog"
    assert al41.stale is True
    assert catalog.source_id == "nordvpn_api_servers"
    assert catalog.stale is False


async def test_embedded_catalog_still_works_when_the_api_fails(tmp_path):
    extra = tmp_path / "embedded.json"
    extra.write_text(json.dumps(_embedded()), encoding="utf-8")

    def fetch():
        raise RuntimeError("api down")

    catalog = NordVpnCatalog(
        fetch=fetch,
        extra_path=extra,
        cache_path=None,
        gluetun_manifest_path=None,
    )
    servers = await catalog.refresh()
    assert {s.hostname for s in servers} == {"al41.nordvpn.com", "al75.nordvpn.com"}
    assert catalog.last_error is not None and "api down" in catalog.last_error
    assert catalog.source_id == "gluetun_embedded_catalog"
    assert catalog.stale is True
    assert all(s.stale and s.source_id == "gluetun_embedded_catalog" for s in servers)

