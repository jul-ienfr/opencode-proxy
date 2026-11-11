"""HTTP station probe tests: status mapping without network."""

from __future__ import annotations

import json

import pytest

from free.qualification.station_probe import HttpStationProbe, build_http_station_probe


def _payload():
    return {
        "stations": [
            {
                "station": 1,
                "ip": "1.2.3.4",
                "country": "Sweden",
                "model_usable": True,
                "vpn": {
                    "status": "connected",
                    "current_host": "se1.nordvpn.com",
                    "type": "wireguard",
                },
            },
            {
                "station": 2,
                "ip": "",
                "model_usable": False,
                "error": "AUTH_FAILED",
                "vpn": {"status": "error"},
            },
        ]
    }


@pytest.mark.asyncio
async def test_connected_usable_station():
    probe = HttpStationProbe(fetch=_payload)
    observation = await probe.probe(1)
    assert observation.connected is True
    assert observation.dns_ok is True
    assert observation.client_usable is True
    assert observation.egress_ip == "1.2.3.4"
    assert observation.hostname == "se1.nordvpn.com"
    assert observation.transport == "wireguard"


@pytest.mark.asyncio
async def test_auth_failed_station():
    probe = HttpStationProbe(fetch=_payload)
    observation = await probe.probe(2)
    assert observation.connected is False
    assert observation.auth_failed is True
    assert observation.client_usable is False


@pytest.mark.asyncio
async def test_missing_station_and_unavailable_payload():
    probe = HttpStationProbe(fetch=_payload)
    missing = await probe.probe(9)
    assert missing.connected is False
    assert missing.error == "station-missing"

    broken = HttpStationProbe(fetch=lambda: {"error": "boom"})
    unavailable = await broken.probe(1)
    assert unavailable.connected is False
    assert unavailable.error == "status-unavailable"


@pytest.mark.asyncio
async def test_dns_error_marks_dns_not_ok():
    payload = {
        "stations": [
            {
                "station": 3,
                "ip": "3.3.3.3",
                "model_usable": False,
                "error": "DNS resolution failed",
                "vpn": {"status": "connected"},
            }
        ]
    }
    probe = HttpStationProbe(fetch=lambda: payload)
    observation = await probe.probe(3)
    assert observation.connected is True
    assert observation.dns_ok is False


@pytest.mark.asyncio
async def test_connected_station_without_model_usable_is_usable():
    payload = {
        "stations": [
            {
                "station": 4,
                "ip": "4.4.4.4",
                "vpn": {"status": "connected"},
            }
        ]
    }
    probe = HttpStationProbe(fetch=lambda: payload)
    observation = await probe.probe(4)
    assert observation.connected is True
    assert observation.client_usable is True


@pytest.mark.asyncio
async def test_vpn_status_fleet_shape_is_mapped():
    payload = {
        "stations": [
            {
                "station": 3,
                "vpn_status": "degraded",
                "current_ip": "3.3.3.3",
                "current_server": "opencode-vpn-3",
                "vpn": {
                    "status": "degraded",
                    "ip": "3.3.3.3",
                    "container": "opencode-vpn-3",
                    "ovpn_protocol_effective": "tcp",
                },
            }
        ]
    }
    probe = HttpStationProbe(fetch=lambda: payload)
    observation = await probe.probe(3)
    assert observation.connected is True
    assert observation.client_usable is True
    assert observation.egress_ip == "3.3.3.3"
    assert observation.container == "opencode-vpn-3"
    assert observation.transport == "tcp"


@pytest.mark.asyncio
async def test_default_fetch_parses_json(monkeypatch):
    payload = json.dumps(_payload()).encode("utf-8")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return payload

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Response())
    probe = HttpStationProbe(base_url="http://127.0.0.1:4000")
    observation = await probe.probe(1)
    assert observation.connected is True
    assert observation.client_usable is True


@pytest.mark.asyncio
async def test_default_fetch_exception_is_status_unavailable(monkeypatch):
    def boom(request, timeout):
        raise OSError("network down")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    probe = HttpStationProbe()
    observation = await probe.probe(1)
    assert observation.connected is False
    assert observation.error == "status-unavailable"


@pytest.mark.asyncio
async def test_invalid_station_rows_are_skipped():
    payload = {
        "stations": [
            None,
            {"station": "not-a-number"},
            {"station": 1, "ip": "1.1.1.1", "model_usable": True, "vpn": {"status": "connected"}},
        ]
    }
    probe = HttpStationProbe(fetch=lambda: payload)
    observation = await probe.probe(1)
    assert observation.connected is True


def test_build_http_station_probe():
    probe = build_http_station_probe("http://localhost:4000")
    assert probe.base_url == "http://localhost:4000"


@pytest.mark.asyncio
async def test_model_usable_reason_is_mapped_for_quota_draining():
    payload = {
        "stations": [
            {
                "station": 3,
                "ip": "3.3.3.3",
                "model_usable": False,
                "model_usable_reason": "daily-bucket-exhausted",
                "vpn": {"status": "connected"},
            }
        ]
    }
    probe = HttpStationProbe(fetch=lambda: payload)
    observation = await probe.probe(3)
    assert observation.connected is True
    assert observation.reason == "daily-bucket-exhausted"
