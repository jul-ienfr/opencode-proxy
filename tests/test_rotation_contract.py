"""Contract tests for the T074 rotation adapter.

The adapter must forward the orchestrator's ordered candidate list verbatim,
never add a score or a quota, and classify the fork's terminal rotation states
without disabling a station: ``exhausted`` and ``rolled_back`` are ordinary
outcomes the next cycle can repair.
"""

from __future__ import annotations

import json

import httpx

from free.control_plane.client import GluetunControlClient
from free.control_plane.models import Constraints, ServerCandidate
from free.ip_registry.adapters.gluetun_control_v2 import (
    GluetunControlV2Binder,
    RotationOutcome,
)

BASE_URL = "http://vpn-gluetun-4:8000"
TRANSPORT_POLICY = ["wireguard", "openvpn-udp", "openvpn-tcp"]


def job_json(state: str, **overrides):
    payload = {
        "id": "job-rotate-01",
        "kind": "rotate",
        "state": state,
        "station_id": "vpn-gluetun-4",
        "target": {"hostname": "first.nordvpn.com"},
        "attempts": [
            {"hostname": "first.nordvpn.com", "result": "rejected"},
            {"hostname": "second.nordvpn.com", "result": "committed"},
        ],
        "created_at": "2026-11-13T10:00:00Z",
        "finished_at": "2026-11-13T10:00:09Z",
    }
    if state == "committed":
        payload["verification"] = {
            "verified": True,
            "ip": "203.0.113.42",
            "quorum": 2,
            "source_count": 3,
        }
        payload["target"] = {"hostname": "second.nordvpn.com"}
    else:
        payload["verification"] = {"verified": False, "reason": "no_quorum"}
        payload["error"] = "no candidate committed"
    payload.update(overrides)
    return payload


def make_binder(handler, station: int = 4):
    calls: list[httpx.Request] = []

    def recording(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return handler(request)

    client = GluetunControlClient(
        BASE_URL,
        api_key="unit-secret",
        station=station,
        http_client=httpx.Client(transport=httpx.MockTransport(recording)),
        sleep=lambda _seconds: None,
    )
    binder = GluetunControlV2Binder(
        client,
        station=station,
        cycle_id="cycle-11",
        transport_policy=TRANSPORT_POLICY,
        to_thread=_inline_to_thread,
    )
    return binder, calls


async def _inline_to_thread(function, *args, **kwargs):
    return function(*args, **kwargs)


async def test_ordered_candidates_exclusions_and_budget_are_forwarded():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/vpn/rotate"
        assert request.headers["Idempotency-Key"] == "rotate-4-cycle-11"
        body = json.loads(request.content)
        assert [item["hostname"] for item in body["candidates"]] == [
            "first.nordvpn.com",
            "second.nordvpn.com",
            "third.nordvpn.com",
        ]
        assert body["candidates"][0]["expected_egress_ip"] == "203.0.113.9"
        assert body["constraints"]["exclude_hostnames"] == ["burned.nordvpn.com"]
        assert body["constraints"]["max_candidates"] == 3
        assert body["constraints"]["max_attempts"] == 2
        assert body["options"]["transport_policy"] == TRANSPORT_POLICY
        raw = request.content.decode()
        assert "score" not in raw
        assert "quota" not in raw
        return httpx.Response(200, json=job_json("committed"))

    binder, calls = make_binder(handler)

    outcome = await binder.rotate_outcome(
        [
            ServerCandidate("first.nordvpn.com", expected_egress_ip="203.0.113.9"),
            ServerCandidate("second.nordvpn.com"),
            ServerCandidate("third.nordvpn.com"),
        ],
        Constraints(
            exclude_hostnames=("burned.nordvpn.com",),
            max_candidates=3,
            max_attempts=2,
        ),
    )

    assert len(calls) == 1
    assert outcome.ok
    assert outcome.state == "committed"
    assert outcome.station == 4
    assert outcome.hostname == "second.nordvpn.com"
    assert outcome.egress_ip == "203.0.113.42"
    assert len(outcome.attempts) == 2


async def test_exhausted_is_an_outcome_that_keeps_the_station_alive():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=job_json(
                "exhausted",
                attempts=[
                    {"hostname": "first.nordvpn.com", "result": "rejected"},
                    {"hostname": "second.nordvpn.com", "result": "skipped"},
                ],
            ),
        )

    binder, _ = make_binder(handler)
    outcome = await binder.rotate_outcome([ServerCandidate("first.nordvpn.com"), ServerCandidate("second.nordvpn.com")])

    assert isinstance(outcome, RotationOutcome)
    assert outcome.exhausted
    assert not outcome.rolled_back
    assert not outcome.ok
    assert outcome.station == 4
    assert [attempt.hostname for attempt in outcome.attempts] == [
        "first.nordvpn.com",
        "second.nordvpn.com",
    ]


async def test_rolled_back_is_reported_without_a_station_disable():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=job_json("rolled_back"))

    binder, _ = make_binder(handler)
    outcome = await binder.rotate_outcome([ServerCandidate("first.nordvpn.com")])

    assert outcome.rolled_back
    assert not outcome.exhausted
    assert not outcome.ok
    assert outcome.error == "no candidate committed"


async def test_empty_candidates_are_rejected_before_any_request():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("empty candidate list must not reach the fork")

    binder, calls = make_binder(handler)
    outcome = await binder.rotate_outcome([ServerCandidate("   ")])

    assert calls == []
    assert outcome.state == "rejected"
    assert outcome.error == "no-candidates"


async def test_control_plane_outage_is_an_error_outcome_not_a_crash():
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    binder, calls = make_binder(handler)
    outcome = await binder.rotate_outcome([ServerCandidate("first.nordvpn.com")])

    assert not outcome.ok
    assert outcome.state == "error"
    assert outcome.error is not None
    assert outcome.error.startswith("control-unavailable:")
    assert len(calls) == 3


async def test_invalid_target_is_not_retried_and_is_classified():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "invalid_target",
                    "message": "transport_policy is invalid",
                }
            },
        )

    binder, calls = make_binder(handler)
    outcome = await binder.rotate_outcome([ServerCandidate("first.nordvpn.com")])

    assert len(calls) == 1
    assert outcome.state == "error"
    assert outcome.error == "control-invalid-target:invalid_target"


async def test_retry_after_a_timeout_reuses_the_same_idempotency_key():
    keys: list[str] = []
    responses = [
        httpx.Response(503, json={"error": {"code": "not_ready"}}),
        httpx.Response(200, json=job_json("committed")),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.headers["Idempotency-Key"])
        return responses.pop(0)

    binder, _ = make_binder(handler)
    outcome = await binder.rotate_outcome([ServerCandidate("second.nordvpn.com")])

    assert outcome.ok
    assert keys == ["rotate-4-cycle-11", "rotate-4-cycle-11"]
