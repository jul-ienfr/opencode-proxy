"""T099: transport ladder shadow parity — observation only, never piloting."""

from __future__ import annotations

import httpx

from free.control_plane.client import GluetunControlClient
from free.control_plane.shadow import TransportShadowObserver
from free.control_plane.telemetry import ShadowTelemetry

BASE_URL = "http://vpn-gluetun-3:8000"


def transport_json(
    active: str = "openvpn-tcp",
    *,
    phase: str = "connected",
    breaker: str = "closed",
):
    return {
        "active_transport": active,
        "policy": {"ladder": ["wireguard", "openvpn-udp", "openvpn-tcp"]},
        "phase": phase,
        "cycle": 1,
        "breaker": {"state": breaker},
        "attempts": [{"transport": "wireguard", "result": "failed"}],
        "updated_at": "2026-11-13T10:00:00Z",
    }


def make_observer(handler, recorder, *, retries: int = 1):
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = GluetunControlClient(
        BASE_URL,
        api_key="unit-secret",
        station=3,
        retry_max_attempts=retries,
        sleep=lambda _seconds: None,
        http_client=httpx.Client(transport=httpx.MockTransport(recording)),
    )
    observer = TransportShadowObserver(client, station=3, recorder=recorder, now=lambda: 100.0)
    return observer, calls


def test_transport_shadow_records_parity_and_never_pilots(tmp_path):
    telemetry = ShadowTelemetry(tmp_path / "telemetry.db")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/vpn/transport"
        assert not request.content
        return httpx.Response(200, json=transport_json("openvpn-tcp"))

    observer, calls = make_observer(handler, telemetry)
    comparison = observer.observe(
        legacy_transport="openvpn-tcp",
        legacy_reason="udp_blocked",
    )

    assert comparison.match
    assert comparison.legacy_transport == "openvpn-tcp"
    assert comparison.fork_transport == "openvpn-tcp"
    assert comparison.fork_reason == "breaker:closed"
    assert comparison.error == ""
    assert [request.method for request in calls] == ["GET"]

    rows = telemetry.recent_transport()
    assert len(rows) == 1
    assert rows[0]["ts"] == 100.0
    assert rows[0]["station"] == "3"
    assert rows[0]["legacy_reason"] == "udp_blocked"
    assert rows[0]["match"] is True


def test_transport_divergence_is_recorded_without_cutting_the_tunnel(tmp_path):
    telemetry = ShadowTelemetry(tmp_path / "telemetry.db")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=transport_json("openvpn-tcp"))

    observer, calls = make_observer(handler, telemetry)
    comparison = observer.observe(legacy_transport="wireguard")

    assert not comparison.match
    assert comparison.fork_transport == "openvpn-tcp"
    assert comparison.fork_reason == "breaker:closed"
    assert comparison.error == ""
    assert [request.method for request in calls] == ["GET"]
    assert telemetry.recent_transport()[0]["match"] is False


def test_transport_shadow_error_is_captured_not_raised(tmp_path):
    telemetry = ShadowTelemetry(tmp_path / "telemetry.db")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={"error": {"code": "not_ready", "message": "transport not ready"}},
        )

    observer, calls = make_observer(handler, telemetry)

    comparison = observer.observe(legacy_transport="openvpn-tcp")

    assert not comparison.match
    assert comparison.error == "not_ready:503"
    assert comparison.fork_transport == ""
    assert len(calls) == 1
    row = telemetry.recent_transport()[0]
    assert row["error"] == "not_ready:503"
    assert row["match"] is False


def test_transport_shadow_ignores_a_recorder_without_transport_sink(tmp_path):
    class LegacySink:
        def __init__(self) -> None:
            self.entries: list[dict] = []

        def record(self, entry: dict) -> None:
            self.entries.append(entry)

    sink = LegacySink()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=transport_json("wireguard"))

    observer, _ = make_observer(handler, sink)
    comparison = observer.observe(legacy_transport="wireguard")

    assert comparison.match
    assert sink.entries == []
