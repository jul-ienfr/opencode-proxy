"""T316: rotation jobs expose their queue ``position`` and ``eta_s``.

The control plane fork ships the queue under several spellings; the client
normalizes them and the dashboard router always exposes the two canonical
fields.  No network is used: the HTTP layer is an in-process mock transport.
"""

from __future__ import annotations

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.vpn_jobs_routes import register_vpn_jobs_routes
from free.control_plane.client import (
    GluetunControlClient,
    normalize_eta_s,
    normalize_job_queue,
    normalize_queue_position,
)

BASE_URL = "http://vpn-gluetun-1:8000"


def _client(extra: dict) -> GluetunControlClient:
    """A control client whose single job endpoint returns ``extra`` fields."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/vpn/jobs/job-1":
            return httpx.Response(
                200,
                json={
                    "id": "job-1",
                    "kind": "rotate",
                    "state": "queued",
                    **extra,
                },
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    return GluetunControlClient(
        BASE_URL,
        retry_max_attempts=1,
        sleep=lambda _seconds: None,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_client_job_reads_position_and_eta():
    job = _client({"position": 2, "eta": 30}).job("job-1")

    assert job.raw["position"] == 2
    assert job.raw["eta_s"] == 30


def test_client_job_accepts_queue_position_and_estimated_seconds():
    job = _client({"queue_position": 3, "estimated_seconds": 45}).job("job-1")

    assert job.raw["position"] == 3
    assert job.raw["eta_s"] == 45


def test_client_job_accepts_pos_and_eta_s():
    job = _client({"pos": 1, "eta_s": 12}).job("job-1")

    assert job.raw["position"] == 1
    assert job.raw["eta_s"] == 12


def test_client_job_missing_queue_fields_are_none():
    job = _client({}).job("job-1")

    assert job.raw["position"] is None
    assert job.raw["eta_s"] is None


def test_client_job_preserves_existing_keys_and_types():
    job = _client({"position": 2, "eta": 30}).job("job-1")

    assert job.id == "job-1"
    assert job.kind == "rotate"
    assert job.state == "queued"
    assert job.raw["id"] == "job-1"
    assert job.raw["kind"] == "rotate"
    assert job.raw["state"] == "queued"


def test_normalizers_are_defensive():
    assert normalize_queue_position(2) == 2
    assert normalize_queue_position("4") == 4
    assert normalize_queue_position(0) is None
    assert normalize_queue_position(-1) is None
    assert normalize_queue_position(None) is None
    assert normalize_queue_position("nope") is None
    assert normalize_queue_position(True) is None

    assert normalize_eta_s(30) == 30
    assert normalize_eta_s("12.5") == 12.5
    assert normalize_eta_s(0) == 0
    assert normalize_eta_s(-3) is None
    assert normalize_eta_s(None) is None
    assert normalize_eta_s("soon") is None
    assert normalize_eta_s(False) is None

    normalized = normalize_job_queue({"position": 2, "eta": 30, "id": "job-1"})
    assert normalized["position"] == 2
    assert normalized["eta_s"] == 30.0
    assert normalized["id"] == "job-1"
    # The fork's own spelling is preserved alongside the canonical keys.
    assert normalized["eta"] == 30


class _SnapshotJob:
    def __init__(self, snapshot: dict) -> None:
        self._snapshot = snapshot

    def snapshot(self) -> dict:
        return dict(self._snapshot)


class _Jobs:
    def __init__(self, job: _SnapshotJob) -> None:
        self._job = job

    def get(self, job_id: str):
        return self._job

    def status(self) -> dict:
        return {
            "jobs": [
                {"id": "job-1", "position": 2, "eta_s": 30},
                {"id": "job-2"},
            ],
            "running": 2,
        }


def _dashboard(job: _SnapshotJob) -> TestClient:
    app = FastAPI()
    register_vpn_jobs_routes(
        app,
        token_checker=lambda request: None,
        fleet_getter=lambda: None,
        jobs_getter=lambda: _Jobs(job),
    )
    return TestClient(app)


def test_dashboard_serializer_passes_position_and_eta_through():
    job = _SnapshotJob(
        {"id": "job-1", "status": "running", "position": 2, "eta_s": 30}
    )

    body = _dashboard(job).get("/api/vpn/jobs/job-1").json()

    assert body["id"] == "job-1"
    assert body["position"] == 2
    assert body["eta_s"] == 30


def test_dashboard_serializer_defaults_missing_queue_fields_to_none():
    job = _SnapshotJob({"id": "job-1", "status": "running"})

    body = _dashboard(job).get("/api/vpn/jobs/job-1").json()

    assert body["position"] is None
    assert body["eta_s"] is None


def test_dashboard_list_jobs_always_exposes_queue_fields():
    job = _SnapshotJob({"id": "job-1"})

    body = _dashboard(job).get("/api/vpn/jobs").json()

    jobs = body["jobs"]
    assert jobs[0]["position"] == 2
    assert jobs[0]["eta_s"] == 30
    assert jobs[1]["position"] is None
    assert jobs[1]["eta_s"] is None
