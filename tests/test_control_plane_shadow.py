"""Shadow-mode contract tests: observe, compare, never cut, never claim."""

from __future__ import annotations

import json

import httpx
import pytest

from free.control_plane.client import GluetunControlClient
from free.control_plane.models import Constraints, TunnelTarget
from free.control_plane.shadow import ShadowObserver
from free.control_plane.telemetry import ShadowTelemetry

BASE_URL = "http://vpn-gluetun-4:8000"


def make_client(handler):
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    return (
        GluetunControlClient(
            BASE_URL,
            station=4,
            api_key="shadow-secret",
            http_client=httpx.Client(transport=httpx.MockTransport(recording)),
        ),
        calls,
    )


def test_observe_uses_only_a_dry_run_and_never_mutates():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/vpn/switch"
        body = json.loads(request.content)
        assert body["options"]["dry_run"] is True
        assert body["target"]["hostname"] == "nl-2.nordvpn.com"
        return httpx.Response(
            200,
            json={
                "state": "planned",
                "plan": {"steps": ["preflight", "apply"]},
                "would_cut_tunnel": True,
                "warnings": ["drain disabled"],
            },
        )

    client, calls = make_client(handler)
    observer = ShadowObserver(client, station=4, cycle_id="cycle-1")
    plan = observer.observe(TunnelTarget("nl-2.nordvpn.com"), Constraints())

    assert plan is not None
    assert plan.observed
    assert plan.would_cut_tunnel is True
    assert plan.warnings == ("drain disabled",)
    assert len(calls) == 1
    assert all(call.method == "POST" for call in calls)


def test_sampling_at_zero_makes_no_call_at_all():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("sampled-out shadow must not call the fork")

    client, calls = make_client(handler)
    observer = ShadowObserver(client, station=4, cycle_id="cycle-1", sample_rate=0.0)

    assert observer.should_sample() is False
    assert observer.observe(TunnelTarget("nl-2.nordvpn.com")) is None
    assert calls == []


def test_observe_failure_is_contained():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"code": "not_ready", "message": "no jobs"}})

    client, _ = make_client(handler)
    observer = ShadowObserver(client, station=4, cycle_id="cycle-1", sample_rate=1.0)
    plan = observer.observe(TunnelTarget("nl-2.nordvpn.com"))

    assert plan is not None
    assert not plan.observed
    assert "not_ready" in plan.error


def test_compare_matches_ip_and_target():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/egress":
            return httpx.Response(200, json={"verified": True, "ip": "203.0.113.9"})
        assert request.url.path == "/v1/vpn/state"
        return httpx.Response(
            200,
            json={
                "station_id": "vpn-gluetun-4",
                "vpn_status": "running",
                "server": {"hostname": "nl-2.nordvpn.com"},
                "desired": {"hostname": "nl-2.nordvpn.com"},
            },
        )

    client, calls = make_client(handler)
    rows: list[dict] = []
    observer = ShadowObserver(
        client,
        station=4,
        cycle_id="cycle-1",
        recorder=_ListRecorder(rows),
        now=lambda: 123.5,
    )
    comparison = observer.compare(legacy_target="nl-2.nordvpn.com", legacy_ip="203.0.113.9")

    assert comparison.match
    assert comparison.divergence_reason == ""
    assert len(calls) == 2
    assert rows == [
        {
            "ts": 123.5,
            "station": "4",
            "legacy_target": "nl-2.nordvpn.com",
            "shadow_target": "nl-2.nordvpn.com",
            "legacy_ip": "203.0.113.9",
            "shadow_ip": "203.0.113.9",
            "match": True,
            "divergence_reason": "",
            "would_cut_tunnel": False,
            "warnings": [],
            "error": "",
        }
    ]


@pytest.mark.parametrize(
    ("shadow_ip", "shadow_target", "reason"),
    [
        ("203.0.113.99", "nl-2.nordvpn.com", "ip"),
        ("203.0.113.9", "nl-3.nordvpn.com", "target"),
    ],
)
def test_compare_records_the_divergence_reason(shadow_ip, shadow_target, reason):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/egress":
            return httpx.Response(200, json={"verified": True, "ip": shadow_ip})
        return httpx.Response(
            200,
            json={
                "vpn_status": "running",
                "desired": {"hostname": shadow_target},
            },
        )

    client, _ = make_client(handler)
    rows: list[dict] = []
    observer = ShadowObserver(
        client,
        station=4,
        cycle_id="cycle-1",
        recorder=_ListRecorder(rows),
    )
    comparison = observer.compare(legacy_target="nl-2.nordvpn.com", legacy_ip="203.0.113.9")

    assert not comparison.match
    assert comparison.divergence_reason == reason
    assert rows[0]["divergence_reason"] == reason


def test_compare_error_is_recorded_and_never_raised():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": {"code": "not_ready", "message": "down"}})

    client, _ = make_client(handler)
    rows: list[dict] = []
    observer = ShadowObserver(
        client,
        station=4,
        cycle_id="cycle-1",
        recorder=_ListRecorder(rows),
    )
    comparison = observer.compare(legacy_target="nl-2.nordvpn.com", legacy_ip="203.0.113.9")

    assert not comparison.match
    assert comparison.divergence_reason == "control-plane-error"
    assert comparison.error
    assert rows[0]["error"] == comparison.error


def test_telemetry_roundtrip(tmp_path):
    telemetry = ShadowTelemetry(tmp_path / "telemetry.db")
    telemetry.record(
        {
            "ts": 1.0,
            "station": "4",
            "legacy_target": "a",
            "shadow_target": "b",
            "legacy_ip": "1.1.1.1",
            "shadow_ip": "1.1.1.1",
            "match": True,
            "divergence_reason": "",
            "would_cut_tunnel": True,
            "warnings": ["w1"],
            "error": "",
        }
    )
    telemetry.record(
        {
            "ts": 2.0,
            "station": "5",
            "legacy_ip": "1.1.1.1",
            "shadow_ip": "1.1.1.2",
            "match": False,
            "divergence_reason": "ip",
        }
    )

    rows = telemetry.recent(limit=10)

    assert [row["station"] for row in rows] == ["5", "4"]
    assert rows[0]["divergence_reason"] == "ip"
    assert rows[1]["match"] is True
    assert rows[1]["warnings"] == ["w1"]


class _ListRecorder:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def record(self, entry: dict) -> None:
        self._rows.append(entry)
