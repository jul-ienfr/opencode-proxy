"""NordVPN catalog adapter tests (no network)."""

from __future__ import annotations

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
    catalog = NordVpnCatalog(fetch=_fake_servers)
    servers = await catalog.refresh()
    assert [s.hostname for s in servers] == ["al110.nordvpn.com", "fr1.nordvpn.com"]
    albania = servers[0]
    assert albania.country == "Albania"
    assert albania.city == "Tirana"
    assert albania.server_ip == "186.247.44.61"
    assert albania.load == 12.0
    assert set(albania.technologies) == {"WireGuard", "OpenVPN"}


async def test_refresh_failure_keeps_previous_snapshot():
    state = {"fail": False}

    def fetch():
        if state["fail"]:
            raise RuntimeError("network down")
        return _fake_servers()

    catalog = NordVpnCatalog(fetch=fetch)
    await catalog.refresh()
    before = catalog.snapshot()
    state["fail"] = True
    after = await catalog.refresh()
    assert after == before
    assert catalog.last_error is not None
    assert "network down" in catalog.last_error


async def test_staleness():
    now = {"t": 1000.0}
    catalog = NordVpnCatalog(fetch=_fake_servers, ttl_s=100.0, clock=lambda: now["t"])
    assert catalog.is_stale()
    await catalog.refresh()
    assert not catalog.is_stale()
    now["t"] += 101.0
    assert catalog.is_stale()
