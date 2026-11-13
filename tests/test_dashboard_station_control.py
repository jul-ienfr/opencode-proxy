"""Dashboard station control routes backed by the 016 control plane."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.station_control_routes import (
    register_station_control_routes,
)
from free.control_plane.models import (
    ControlEvent,
    EventsPage,
    SwitchJob,
    TransportState,
)


def committed_job(job_id: str = "job-7") -> dict[str, Any]:
    return {
        "id": job_id,
        "kind": "switch",
        "state": "committed",
        "station_id": "vpn-gluetun-1",
        "target": {"hostname": "nl-1.nordvpn.com"},
        "attempts": [{"hostname": "nl-1.nordvpn.com", "result": "committed"}],
        "verification": {"verified": True, "ip": "203.0.113.7"},
    }


class FakeControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    def state(self):
        from free.control_plane.models import TunnelState

        return TunnelState.from_json(
            {
                "station_id": "vpn-gluetun-1",
                "vpn_status": "running",
                "transport": "wireguard",
                "egress": {"verified": True, "ip": "203.0.113.7"},
            }
        )

    def job(self, job_id: str):
        self.calls.append(("job", job_id))
        return SwitchJob.from_json(committed_job(job_id))

    def events_page(self, *, since=None, limit=100):
        self.calls.append(("events", (since, limit)))
        return EventsPage(
            items=(ControlEvent.from_json({"seq": 7, "type": "job.committed", "severity": "info"}),),
            next_seq=7,
            gap=False,
        )

    def switch(self, target, constraints, **kwargs):
        self.calls.append(("switch", (target, constraints, kwargs)))
        return SwitchJob.from_json(committed_job(kwargs.get("idempotency_key")))

    def rotate(self, candidates, constraints, **kwargs):
        self.calls.append(("rotate", (candidates, constraints, kwargs)))
        return SwitchJob.from_json(committed_job("job-rotate"))

    def rollback(self, **kwargs):
        self.calls.append(("rollback", kwargs))
        return SwitchJob.from_json(committed_job("job-rollback"))

    def transport_reset(self, transport=None):
        self.calls.append(("transport_reset", transport))
        return TransportState.from_json({"active_transport": "wireguard", "policy": {"ladder": ["wireguard"]}})


def _app(control: Any = None, token_error: Any = None) -> FastAPI:
    app = FastAPI()
    register_station_control_routes(
        app,
        token_checker=lambda _request: token_error,
        managers_getter=lambda: [],
        pool_getter=lambda: None,
        fleet_getter=lambda: object(),
        control_getter=(lambda _station: control) if control is not None else None,
    )
    return app


def test_state_and_events_are_read_from_the_control_plane():
    control = FakeControl()
    client = TestClient(_app(control))

    state = client.get("/api/vpn/station/1/control/state").json()
    assert state["ok"] is True
    assert state["state"]["vpn_status"] == "running"
    assert state["state"]["egress"]["ip"] == "203.0.113.7"

    events = client.get(
        "/api/vpn/station/1/control/events",
        params={"since": 6, "limit": 5000},
    ).json()
    assert events["items"][0]["seq"] == 7
    assert events["next_seq"] == 7
    assert events["gap"] is False
    assert control.calls[-1] == ("events", (6, 500))


def test_switch_submits_a_job_and_the_job_can_be_followed():
    control = FakeControl()
    client = TestClient(_app(control))

    response = client.post(
        "/api/vpn/station/1/control/switch",
        json={
            "hostname": "nl-1.nordvpn.com",
            "expected_egress_ip": "203.0.113.7",
            "idempotency_key": "dash-switch-1",
            "constraints": {"exclude_hostnames": ["burned.nordvpn.com"]},
        },
    )
    assert response.status_code == 202
    assert response.json()["job"]["id"] == "dash-switch-1"

    target, constraints, kwargs = control.calls[-1][1]
    assert target.hostname == "nl-1.nordvpn.com"
    assert target.expected_egress_ip == "203.0.113.7"
    assert constraints.exclude_hostnames == ("burned.nordvpn.com",)
    assert kwargs["idempotency_key"] == "dash-switch-1"
    assert kwargs["wait"] is False

    job = client.get("/api/vpn/station/1/control/jobs/job-7").json()
    assert job["state"] == "committed"
    assert control.calls[-1] == ("job", "job-7")


def test_rotate_forwards_the_ordered_candidates_without_scores():
    control = FakeControl()
    client = TestClient(_app(control))

    response = client.post(
        "/api/vpn/station/1/control/rotate",
        json={
            "candidates": [
                "first.nordvpn.com",
                {"hostname": "second.nordvpn.com"},
            ],
            "constraints": {"max_attempts": 2},
            "transport_policy": ["wireguard", "openvpn-udp"],
        },
    )

    assert response.status_code == 202
    candidates, constraints, kwargs = control.calls[-1][1]
    assert [candidate.hostname for candidate in candidates] == [
        "first.nordvpn.com",
        "second.nordvpn.com",
    ]
    assert constraints.max_attempts == 2
    assert kwargs["transport_policy"] == ["wireguard", "openvpn-udp"]
    assert response.json()["job"]["id"] == "job-rotate"


def test_rollback_and_transport_reset_are_jobs_not_shell_commands():
    control = FakeControl()
    client = TestClient(_app(control))

    rollback = client.post(
        "/api/vpn/station/1/control/rollback",
        json={"idempotency_key": "dash-rollback-1"},
    )
    assert rollback.status_code == 202
    assert control.calls[-1][1]["idempotency_key"] == "dash-rollback-1"

    reset = client.post(
        "/api/vpn/station/1/control/transport/reset",
        json={"transport": "openvpn-tcp"},
    )
    assert reset.status_code == 200
    assert reset.json()["transport"]["active_transport"] == "wireguard"
    assert control.calls[-1] == ("transport_reset", "openvpn-tcp")


def test_unconfigured_control_plane_is_a_clean_503():
    client = TestClient(_app(control=None))
    response = client.get("/api/vpn/station/1/control/state")
    assert response.status_code == 503
    assert response.json()["code"] == "control_plane_unavailable"


def test_token_checker_still_guards_the_new_routes():
    from fastapi.responses import JSONResponse

    denied = JSONResponse(status_code=401, content={"error": "token"})
    client = TestClient(_app(FakeControl(), token_error=denied))
    assert client.get("/api/vpn/station/1/control/state").status_code == 401
