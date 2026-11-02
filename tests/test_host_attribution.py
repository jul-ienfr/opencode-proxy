"""Contract for the hostname-attribution runtime metric (P0/P1)."""

from __future__ import annotations

from types import SimpleNamespace

import opencode as oc


def test_host_attribution_counts_and_coverage():
    oc._reset_host_attribution()
    oc._note_host_attribution("nl1050.nordvpn.com")
    oc._note_host_attribution("")
    oc._note_host_attribution(None)

    snapshot = oc._host_attribution_snapshot()
    assert snapshot["free_vpn_requests"] == 3
    assert snapshot["with_host"] == 1
    assert snapshot["missing_host"] == 2
    assert snapshot["coverage"] == 0.3333


def test_host_attribution_empty_snapshot_has_no_coverage():
    oc._reset_host_attribution()
    snapshot = oc._host_attribution_snapshot()
    assert snapshot["free_vpn_requests"] == 0
    assert snapshot["coverage"] is None


def test_host_attribution_reset():
    oc._note_host_attribution("a.nordvpn.com")
    oc._reset_host_attribution()
    assert oc._host_attribution_snapshot()["free_vpn_requests"] == 0


def test_attribute_explicit_host_wins(monkeypatch):
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    assert oc._attribute_server_host("explicit.nordvpn.com", 2, "1.2.3.4") == (
        "explicit.nordvpn.com"
    )


def test_attribute_reads_manager_current_host(monkeypatch):
    manager = SimpleNamespace(_station=2, _current_host="nl1050.nordvpn.com")
    monkeypatch.setattr(oc, "_free_ip_pool", SimpleNamespace(_stations=[manager]))
    assert oc._attribute_server_host(None, 2, "1.2.3.4") == "nl1050.nordvpn.com"


def test_attribute_falls_back_to_last_quality_for_station_ip(monkeypatch):
    import free.quality as fq

    monkeypatch.setattr(oc, "_free_ip_pool", None)
    item = SimpleNamespace(server_host="fr1331.nordvpn.com")
    monkeypatch.setattr(
        fq,
        "get_quality_registry",
        lambda: SimpleNamespace(_items={(2, "1.2.3.4"): item}),
    )
    assert oc._attribute_server_host(None, 2, "1.2.3.4") == "fr1331.nordvpn.com"


def test_attribute_never_invents_without_station(monkeypatch):
    monkeypatch.setattr(oc, "_free_ip_pool", None)
    assert oc._attribute_server_host(None, None, "1.2.3.4") is None
