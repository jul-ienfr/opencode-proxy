"""T077: cold legacy fallback only on transport failures, never on business ones."""

from __future__ import annotations

import httpx

from free.control_plane.client import (
    GluetunControlClient,
    legacy_fallback,
    legacy_fallback_reason,
)
from free.control_plane.errors import (
    ControlPlaneError,
    ControlPlaneTimeout,
    ControlPlaneUnavailable,
    LegacyFallback,
    UnknownTarget,
)
from free.ip_registry.adapters.gluetun_control_v2 import GluetunControlV2Binder
from free.ip_registry.domain import BindResult

BASE_URL = "http://vpn-gluetun-5:8000"


def job_json(state: str = "committed", **overrides):
    payload = {
        "id": "job-active",
        "kind": "switch",
        "state": state,
        "station_id": "vpn-gluetun-5",
        "target": {"hostname": "nl-1.nordvpn.com"},
        "attempts": [{"hostname": "nl-1.nordvpn.com", "result": "committed"}],
        "verification": {"verified": True, "ip": "203.0.113.7"},
    }
    if state != "committed":
        payload["verification"] = {"verified": False}
        payload["error"] = "failed"
    payload.update(overrides)
    return payload


class FakeLegacy:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str | None]] = []

    async def assign(self, station, hostname, country=None, **_):
        self.calls.append((int(station), str(hostname), country))
        return BindResult(True, hostname, egress_ip="203.0.113.99")


def make_binder(handler, legacy=None, trace=None, retries: int = 2):
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = GluetunControlClient(
        BASE_URL,
        api_key="unit-secret",
        station=5,
        retry_max_attempts=retries,
        poll_interval_s=0.01,
        sleep=lambda _seconds: None,
        http_client=httpx.Client(transport=httpx.MockTransport(recording)),
    )
    binder = GluetunControlV2Binder(
        client,
        station=5,
        cycle_id="cycle-1",
        legacy=legacy,
        on_fallback=trace.append if trace is not None else None,
        to_thread=_inline_to_thread,
    )
    return binder, calls


async def _inline_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def http_error(status: int, code: str = "not_ready") -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": code}})


def test_fallback_reason_is_transport_only():
    assert legacy_fallback_reason(ControlPlaneTimeout("slow")) == "timeout"
    assert legacy_fallback_reason(ControlPlaneUnavailable("refused")) == "unavailable"
    assert legacy_fallback_reason(ControlPlaneError("down", status=503)) == "http_503"
    assert legacy_fallback_reason(ControlPlaneError("down", status=502)) == "http_502"
    assert legacy_fallback_reason(UnknownTarget("bad", status=422)) == ""
    assert legacy_fallback_reason(ControlPlaneError("denied", status=403)) == ""
    assert legacy_fallback_reason(ControlPlaneError("quota", status=429)) == ""

    fallback = legacy_fallback(ControlPlaneError("down", status=503))
    assert isinstance(fallback, LegacyFallback)
    assert fallback.reason == "http_503"
    try:
        legacy_fallback(UnknownTarget("bad", status=422))
    except UnknownTarget:
        pass
    else:  # pragma: no cover - explicit regression guard
        raise AssertionError("business errors must never become a legacy fallback")


async def test_5xx_after_retries_switches_to_the_cold_legacy_path():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/vpn/switch":
            return http_error(503)
        if request.url.path == "/v1/vpn/state":
            return httpx.Response(200, json={"active_job_id": None})
        raise AssertionError(f"unexpected path {request.url.path}")

    legacy = FakeLegacy()
    trace: list[str] = []
    binder, calls = make_binder(handler, legacy=legacy, trace=trace)

    result = await binder.assign(5, "nl-1.nordvpn.com", country="Netherlands")

    assert result.ok
    assert result.egress_ip == "203.0.113.99"
    assert legacy.calls == [(5, "nl-1.nordvpn.com", "Netherlands")]
    assert trace == ["http_503"]
    paths = [request.url.path for request in calls]
    assert paths.count("/v1/vpn/switch") == 2
    assert paths.count("/v1/vpn/state") == 1


async def test_timeout_with_an_active_job_waits_instead_of_a_second_job():
    switch_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal switch_calls
        if request.url.path == "/v1/vpn/switch":
            switch_calls += 1
            return http_error(504)
        if request.url.path == "/v1/vpn/state":
            return httpx.Response(200, json={"active_job_id": "job-active"})
        if request.url.path == "/v1/vpn/jobs/job-active":
            return httpx.Response(200, json=job_json())
        raise AssertionError(f"unexpected path {request.url.path}")

    legacy = FakeLegacy()
    binder, _ = make_binder(handler, legacy=legacy)

    result = await binder.assign(5, "nl-1.nordvpn.com")

    assert result.ok
    assert result.egress_ip == "203.0.113.7"
    assert legacy.calls == []
    assert switch_calls == 2


async def test_409_active_job_is_followed_and_never_creates_a_second_job():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/vpn/switch":
            return httpx.Response(
                409,
                json={
                    "error": {
                        "code": "active_job",
                        "message": "busy",
                        "details": {"active_job_id": "job-active"},
                    }
                },
            )
        if request.url.path == "/v1/vpn/jobs/job-active":
            return httpx.Response(200, json=job_json())
        raise AssertionError(f"unexpected path {request.url.path}")

    legacy = FakeLegacy()
    binder, calls = make_binder(handler, legacy=legacy)

    result = await binder.assign(5, "nl-1.nordvpn.com")

    assert result.ok
    assert legacy.calls == []
    assert [request.url.path for request in calls] == [
        "/v1/vpn/switch",
        "/v1/vpn/jobs/job-active",
    ]


async def test_422_rejects_the_candidate_without_any_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/vpn/switch"
        return http_error(422, code="unknown_target")

    legacy = FakeLegacy()
    binder, calls = make_binder(handler, legacy=legacy)

    result = await binder.assign(5, "nope.nordvpn.com")

    assert not result.ok
    assert result.error is not None
    assert result.error.startswith("control-invalid-target:")
    assert legacy.calls == []
    assert len(calls) == 1


async def test_403_stops_without_a_fallback():
    def handler(request: httpx.Request) -> httpx.Response:
        return http_error(403, code="forbidden")

    legacy = FakeLegacy()
    binder, calls = make_binder(handler, legacy=legacy)

    result = await binder.assign(5, "nl-1.nordvpn.com")

    assert not result.ok
    assert result.error is not None
    assert result.error.startswith("control-error:")
    assert legacy.calls == []
    assert len(calls) == 1


async def test_unconfigured_legacy_reports_the_unavailable_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/vpn/switch":
            return http_error(503)
        return httpx.Response(200, json={"active_job_id": None})

    binder, _ = make_binder(handler, legacy=None)
    result = await binder.assign(5, "nl-1.nordvpn.com")

    assert not result.ok
    assert result.error is not None
    assert result.error.startswith("control-error:")
