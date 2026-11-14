"""Contract tests for the Gluetun 016 control plane client.

Everything runs against an in-process ``httpx.MockTransport``: no socket, no
Gluetun container.  The suite pins the exact paths, payloads, headers,
retry/fallback boundaries and typed errors the orchestrator relies on.
"""

from __future__ import annotations

import json

import httpx
import pytest

from free.control_plane.client import GluetunControlClient
from free.control_plane.errors import (
    ActiveJob,
    ControlPlaneError,
    ControlPlaneTimeout,
    ControlPlaneUnavailable,
    Exhausted,
    UnknownTarget,
    VerificationFailed,
)
from free.control_plane.models import (
    Constraints,
    ServerCandidate,
    SwitchJob,
    TunnelTarget,
)

BASE_URL = "http://vpn-gluetun-2:8000"


def job_json(state: str = "committed", **overrides):
    payload = {
        "id": "job-01J9Z3K4T5",
        "kind": "switch",
        "state": state,
        "station_id": "vpn-gluetun-2",
        "target": {"hostname": "nl-1.nordvpn.com"},
        "attempts": [
            {"hostname": "nl-1.nordvpn.com", "result": "committed"},
        ],
        "verification": {
            "verified": True,
            "ip": "203.0.113.7",
            "quorum": 2,
            "source_count": 3,
        },
        "created_at": "2026-11-13T10:00:00Z",
        "finished_at": "2026-11-13T10:00:04Z",
    }
    if state != "committed":
        payload["verification"] = {"verified": False, "reason": "no_quorum"}
        payload["error"] = "candidate failed"
    payload.update(overrides)
    return payload


def state_json():
    return {
        "station_id": "vpn-gluetun-2",
        "vpn_status": "running",
        "transport": "openvpn-udp",
        "server": {"hostname": "nl-1.nordvpn.com", "country": "Netherlands"},
        "endpoint_ip": "198.51.100.10",
        "egress": {"verified": True, "ip": "203.0.113.7"},
        "desired": {"hostname": "nl-1.nordvpn.com", "transport": "openvpn-udp"},
        "last_committed": {"job_id": "job-1", "at": "2026-11-13T10:00:00Z"},
        "active_job_id": None,
        "state_mode": "state",
        "updated_at": "2026-11-13T10:00:05Z",
    }


class Clock:
    def __init__(self) -> None:
        self.value = 0.0
        self.sleeps: list[float] = []

    def now(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def make_client(handler, **kwargs):
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    transport = httpx.MockTransport(recording)
    http_client = httpx.Client(transport=transport)
    return (
        GluetunControlClient(
            BASE_URL,
            api_key="unit-secret",
            station=2,
            http_client=http_client,
            **kwargs,
        ),
        calls,
    )


def test_switch_serializes_target_constraints_and_idempotency_key():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/vpn/switch"
        assert request.headers["X-API-Key"] == "unit-secret"
        assert request.headers["Idempotency-Key"] == "switch-2-cycle-7"
        body = json.loads(request.content)
        assert body["idempotency_key"] == "switch-2-cycle-7"
        assert body["target"] == {
            "hostname": "nl-1.nordvpn.com",
            "expected_egress_ip": "203.0.113.7",
        }
        assert body["constraints"]["exclude_subnets"] == ["198.51.100.0/24"]
        assert body["options"]["wait"] is True
        return httpx.Response(200, json=job_json())

    client, calls = make_client(handler)
    job = client.switch(
        TunnelTarget(hostname="nl-1.nordvpn.com", expected_egress_ip="203.0.113.7"),
        Constraints(exclude_subnets=("198.51.100.0/24",)),
        idempotency_key="switch-2-cycle-7",
        wait=True,
    )

    assert len(calls) == 1
    assert job.committed
    assert job.verification is not None
    assert job.verification.ip == "203.0.113.7"


def test_rotate_forwards_ordered_candidates_without_scores():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/vpn/rotate"
        body = json.loads(request.content)
        assert [item["hostname"] for item in body["candidates"]] == [
            "first.nordvpn.com",
            "second.nordvpn.com",
        ]
        assert "score" not in request.content.decode()
        assert "quota" not in request.content.decode()
        assert body["constraints"]["max_candidates"] == 2
        return httpx.Response(202, json=job_json("validating", verification=None))

    client, _ = make_client(handler)
    job = client.rotate(
        [
            ServerCandidate("first.nordvpn.com"),
            ServerCandidate("second.nordvpn.com"),
        ],
        Constraints(max_candidates=2),
        idempotency_key="rotate-2-cycle-7",
        wait=False,
    )

    assert job.id == "job-01J9Z3K4T5"
    assert not job.terminal


def test_retries_retryable_status_then_succeeds():
    responses = [
        httpx.Response(503, json={"error": {"code": "not_ready"}}),
        httpx.Response(503, json={"error": {"code": "not_ready"}}),
        httpx.Response(200, json=state_json()),
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    clock = Clock()
    client, calls = make_client(handler, sleep=clock.sleep, now=clock.now)
    state = client.state()

    assert state.transport == "openvpn-udp"
    assert len(calls) == 3
    assert clock.sleeps == [0.5, 1.0]


def test_business_422_is_not_retried():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "unknown_target",
                    "message": "hostname is not in the catalogue",
                }
            },
        )

    client, calls = make_client(handler)
    with pytest.raises(UnknownTarget) as caught:
        client.switch(
            TunnelTarget("nope.nordvpn.com"),
            idempotency_key="switch-2-cycle-7",
        )

    assert len(calls) == 1
    assert caught.value.status == 422


def test_active_job_exposes_the_running_job_id():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "active_job",
                    "message": "another job is already active",
                    "details": {"active_job_id": "job-active"},
                }
            },
        )

    client, _ = make_client(handler)
    with pytest.raises(ActiveJob) as caught:
        client.switch(
            TunnelTarget("nl-1.nordvpn.com"),
            idempotency_key="switch-2-cycle-7",
        )

    assert caught.value.active_job_id == "job-active"


def test_wait_timeout_returns_the_active_job_instead_of_failing():
    active = job_json("applying", verification=None)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/vpn/switch":
            return httpx.Response(202, json=active)
        assert request.url.path == "/v1/vpn/jobs/job-01J9Z3K4T5"
        return httpx.Response(200, json=active)

    clock = Clock()
    client, calls = make_client(handler, sleep=clock.sleep, now=clock.now, poll_interval_s=1.0)
    job = client.switch(
        TunnelTarget("nl-1.nordvpn.com"),
        idempotency_key="switch-2-cycle-7",
        wait=True,
        timeout_s=2,
    )

    assert job.id == "job-01J9Z3K4T5"
    assert not job.terminal
    assert len(calls) > 1
    assert all(sleep == 1.0 for sleep in clock.sleeps)


def test_transport_timeout_is_retried_then_surfaces_as_timeout():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow fork")

    client, calls = make_client(handler)
    with pytest.raises(ControlPlaneTimeout):
        client.state()

    assert len(calls) == 3


def test_timeout_does_not_retry_when_budget_is_one():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client, calls = make_client(handler, retry_max_attempts=1)
    with pytest.raises(ControlPlaneUnavailable):
        client.state()

    assert len(calls) == 1


def test_insufficient_sources_is_a_verification_error():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "insufficient_sources",
                    "message": "not enough sources",
                    "details": {
                        "verification": {
                            "verified": False,
                            "reason": "insufficient_sources",
                        }
                    },
                }
            },
        )

    client, _ = make_client(handler)
    with pytest.raises(VerificationFailed) as caught:
        client.egress(fresh=True)

    assert caught.value.verification["verified"] is False


def test_dry_run_returns_the_plan_without_a_job():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "state": "planned",
                "plan": {"steps": ["preflight", "apply"]},
                "would_cut_tunnel": True,
            },
        )

    client, _ = make_client(handler)
    job = client.switch(
        TunnelTarget("nl-1.nordvpn.com"),
        idempotency_key="switch-2-cycle-7",
        wait=True,
        dry_run=True,
    )

    assert job.state == "planned"
    assert job.raw["would_cut_tunnel"] is True
    job.raise_for_status()


def test_exhausted_job_raises_with_attempts():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=job_json(
                "exhausted",
                attempts=[
                    {"hostname": "a", "result": "rejected"},
                    {"hostname": "b", "result": "skipped"},
                ],
            ),
        )

    client, _ = make_client(handler)
    job = client.rotate(
        [ServerCandidate("a"), ServerCandidate("b")],
        idempotency_key="rotate-2-cycle-7",
        wait=True,
    )

    with pytest.raises(Exhausted) as caught:
        job.raise_for_status()
    assert len(caught.value.attempts) == 2


def test_events_page_parsing_and_since_parameter():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/events"
        assert request.url.params["since"] == "41"
        assert request.url.params["limit"] == "100"
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "seq": 42,
                        "ts": "2026-11-13T10:00:00Z",
                        "type": "job.committed",
                        "severity": "info",
                        "job_id": "job-1",
                        "data": {"kind": "switch"},
                    }
                ],
                "next_seq": 42,
            },
        )

    client, _ = make_client(handler)
    events = client.events(since=41)

    assert len(events) == 1
    assert events[0].seq == 42
    assert events[0].type == "job.committed"


def test_transport_reset_unknown_transport_is_a_deterministic_reject():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "invalid_target",
                    "message": "unknown transport",
                }
            },
        )

    client, calls = make_client(handler)
    with pytest.raises(UnknownTarget):
        client.transport_reset("openvpn-quic")

    assert len(calls) == 1


def test_api_key_is_read_from_env_and_never_rendered():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "env-secret"
        return httpx.Response(
            401,
            json={"error": {"code": "unauthorized", "message": "unauthorized"}},
        )

    transport = httpx.MockTransport(handler)
    client = GluetunControlClient(
        BASE_URL,
        station=2,
        api_key_env="TEST_CONTROL_KEY",
        environ={"TEST_CONTROL_KEY": "env-secret"},
        http_client=httpx.Client(transport=transport),
    )

    assert "env-secret" not in repr(client)
    with pytest.raises(ControlPlaneError) as caught:
        client.state()
    assert "env-secret" not in str(caught.value)


def test_idempotency_key_is_required():
    client = GluetunControlClient(BASE_URL, station=2)
    with pytest.raises(ValueError):
        client.switch(TunnelTarget("nl-1.nordvpn.com"), idempotency_key="")
    with pytest.raises(ValueError):
        client.rotate([ServerCandidate("nl-1.nordvpn.com")], idempotency_key=" ")


async def test_binder_maps_a_committed_job_to_bind_result():
    from free.ip_registry.adapters.gluetun_control_v2 import (
        GluetunControlV2Binder,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/vpn/switch"
        body = json.loads(request.content)
        assert body["idempotency_key"] == "switch-3-cycle-9-nl-1.nordvpn.com"
        return httpx.Response(200, json=job_json())

    client, _ = make_client(handler)
    binder = GluetunControlV2Binder(client, station=3, cycle_id="cycle-9", to_thread=_inline_to_thread)

    result = await binder.assign(3, "nl-1.nordvpn.com")

    assert result.ok
    assert result.egress_ip == "203.0.113.7"


async def test_binder_maps_unavailable_to_a_cold_fallback_hint():
    from free.ip_registry.adapters.gluetun_control_v2 import (
        GluetunControlV2Binder,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client, _ = make_client(handler, retry_max_attempts=1)
    binder = GluetunControlV2Binder(client, station=3, cycle_id="cycle-9", to_thread=_inline_to_thread)

    result = await binder.assign(3, "nl-1.nordvpn.com")

    assert not result.ok
    assert result.error is not None
    assert result.error.startswith("control-unavailable:")


async def test_binder_maps_resolution_failure_to_a_cold_fallback_hint():
    from free.ip_registry.adapters.gluetun_control_v2 import (
        GluetunControlV2Binder,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "invalid_target",
                    "message": "resolution failed: lookup br1 on 127.0.0.1:53: server misbehaving",
                    "details": {"reason": "resolution_failed"},
                }
            },
        )

    client, _ = make_client(handler, retry_max_attempts=1)
    binder = GluetunControlV2Binder(
        client, station=3, cycle_id="cycle-9", to_thread=_inline_to_thread
    )

    result = await binder.assign(3, "br1.nordvpn.com")

    assert not result.ok
    assert result.error == "control-unavailable:resolution-failed"


async def test_binder_maps_a_crashed_station_to_a_compose_fallback_hint():
    from free.ip_registry.adapters.gluetun_control_v2 import (
        GluetunControlV2Binder,
    )

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=job_json(
                "failed_degraded",
                error=(
                    "applying switch target: vpn loop is crashed after "
                    "applying settings: already crashed"
                ),
            ),
        )

    client, _ = make_client(handler)
    binder = GluetunControlV2Binder(
        client, station=3, cycle_id="cycle-9", to_thread=_inline_to_thread
    )

    result = await binder.assign(3, "us1.nordvpn.com")

    assert not result.ok
    assert result.error == "control-unavailable:station-degraded"


async def test_binder_maps_a_noop_job_to_success():
    from free.ip_registry.adapters.gluetun_control_v2 import (
        GluetunControlV2Binder,
    )

    class NoopClient:
        def switch(self, *_args, **_kwargs):
            return SwitchJob.from_json(job_json("noop"))

    binder = GluetunControlV2Binder(
        NoopClient(), station=3, cycle_id="cycle-9", to_thread=_inline_to_thread
    )

    result = await binder.assign(3, "us1.nordvpn.com")

    assert result.ok
    assert result.egress_ip is None


async def test_binder_waits_for_an_active_rotate_job():
    from free.ip_registry.adapters.gluetun_control_v2 import (
        GluetunControlV2Binder,
    )

    class ActiveClient:
        def rotate(self, *_args, **_kwargs):
            raise ActiveJob("busy", active_job_id="job-01J9Z3K4T5")

        def wait_for_active_job(self, _error):
            return SwitchJob.from_json(job_json("committed"))

    binder = GluetunControlV2Binder(
        ActiveClient(), station=3, cycle_id="cycle-9", to_thread=_inline_to_thread
    )

    outcome = await binder.rotate_outcome([ServerCandidate("us1.nordvpn.com")])

    assert outcome.ok
    assert outcome.state == "committed"


async def _inline_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


def test_capabilities_flags_are_exposed():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "control_plane_version": "1.0.0",
                "upstream_version": "v3.41.3",
                "station_id": "vpn-gluetun-2",
                "features": {"switch": True, "transport_ladder": False},
                "limits": {"max_candidates": 20},
            },
        )

    client, _ = make_client(handler)
    capabilities = client.capabilities()

    assert capabilities.supports("switch") is True
    assert capabilities.supports("transport_ladder") is False
    assert capabilities.limits["max_candidates"] == 20
