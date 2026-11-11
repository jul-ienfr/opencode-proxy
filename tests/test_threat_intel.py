"""Threat-intel feeds: parsing, fail-soft refresh, IP/CIDR matching."""

from __future__ import annotations

import pytest

from free.egress_store import EgressStore
from free.threat_intel import ThreatFeed, ThreatIntel, parse_feed_config, parse_indicators


def test_indicators_accept_all_feed_shapes():
    text = """
# comment
1.2.3.4
5.6.7.8:8080
10.0.0.0/8
https://9.10.11.12:443/path
45.0.0.0/24 ; SBL123
8.8.4.4\t5
2606:4700::1
"""
    ips, networks = parse_indicators(text)
    assert {"1.2.3.4", "5.6.7.8", "9.10.11.12", "8.8.4.4", "2606:4700::1"} <= ips
    assert "10.0.0.0/8" not in networks
    assert "45.0.0.0/24" in networks


def test_feed_config_is_parsed():
    feeds = parse_feed_config(
        [
            {"id": "a", "url": "https://a.example", "enabled": True},
            {"id": "b", "url": "https://b.example", "enabled": False},
            "https://c",
        ]
    )
    assert [feed.id for feed in feeds] == ["a", "b", "https://c"]
    assert [feed.enabled for feed in feeds] == [True, False, True]


@pytest.mark.asyncio
async def test_refresh_matches_exact_ip_and_cidr_and_is_fail_soft(tmp_path):
    store = EgressStore(tmp_path / "registry.db")
    calls = []

    def fetcher(url, timeout):
        calls.append(url)
        return "1.2.3.4\n5.6.7.0/24\n"

    threat = ThreatIntel(store, [ThreatFeed("feed-a", "https://feed.example")], fetcher=fetcher)
    try:
        first = threat.refresh_once()
        assert first["counts"]["feed-a"] == 2
        assert threat.matches("1.2.3.4")
        assert threat.matches("5.6.7.99")
        assert not threat.matches("9.9.9.9")

        def broken(url, timeout):
            raise OSError("feed down")

        second = threat.refresh_once(fetcher=broken)
        assert second["errors"]
        assert threat.matches("1.2.3.4")
        feeds = store.threat_feeds()
        assert feeds[0]["source"] == "feed-a"
        assert "feed down" in feeds[0]["error"]
    finally:
        store.close()


def test_local_private_indicators_are_never_loaded(tmp_path):
    store = EgressStore(tmp_path / "registry.db")
    try:
        store.replace_threat_indicators(
            "feed",
            "https://feed.example",
            ips=set(),
            networks={"10.0.0.0/8", "192.168.0.0/16"},
        )
        threat = ThreatIntel(store, [])
        assert not threat.matches("10.1.2.3")
        assert not threat.matches("192.168.1.1")
    finally:
        store.close()
