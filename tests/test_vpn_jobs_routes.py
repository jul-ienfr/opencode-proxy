"""Fleet job route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.vpn_jobs_routes import register_vpn_jobs_routes


class FakeFleet:
    def __init__(self):
        self.calls = []

    def connect_all(self):
        self.calls.append("connect")
        return {"ok": True}

    def disconnect_all(self, stop_containers=False):
        self.calls.append("disconnect")
        return {"ok": True}

    def heal(self):
        self.calls.append("heal")
        return {"ok": True}


class FakeJob:
    def __init__(self, name):
        self.name = name

    def snapshot(self):
        return {"id": self.name, "status": "running"}


class FakeJobs:
    def __init__(self):
        self.created = []
        self.items = {}

    def create(self, name, coro):
        self.created.append(name)
        job = FakeJob(name)
        self.items[name] = job
        return job

    def get(self, job_id):
        return self.items.get(job_id)

    def status(self):
        return {"count": len(self.items)}


def _app(*, token_error=None):
    fleet = FakeFleet()
    jobs = FakeJobs()
    app = FastAPI()
    register_vpn_jobs_routes(
        app,
        token_checker=lambda request: token_error,
        fleet_getter=lambda: fleet,
        jobs_getter=lambda: jobs,
    )
    return app, fleet, jobs


def test_fleet_job_creates_202_and_known_action():
    app, fleet, jobs = _app()
    response = TestClient(app).post("/api/vpn/jobs/fleet/connect")
    assert response.status_code == 202
    assert response.json()["id"] == "fleet:connect"
    assert fleet.calls == ["connect"]
    assert jobs.created == ["fleet:connect"]


def test_unknown_action_and_job_lookup():
    app, _, _ = _app()
    client = TestClient(app)
    assert client.post("/api/vpn/jobs/fleet/nope").status_code == 400
    client.post("/api/vpn/jobs/fleet/heal")
    assert client.get("/api/vpn/jobs/fleet:heal").json()["status"] == "running"
    assert client.get("/api/vpn/jobs/missing").status_code == 404
    assert client.get("/api/vpn/jobs").json()["count"] == 1


def test_fleet_job_token_rejection():
    app, fleet, jobs = _app(
        token_error=JSONResponse({"error": "token"}, status_code=401)
    )
    assert TestClient(app).post("/api/vpn/jobs/fleet/connect").status_code == 401
    assert fleet.calls == []
    assert jobs.created == []
