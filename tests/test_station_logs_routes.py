"""Per-station Docker log route tests (extracted router)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.station_logs_routes import register_station_logs_routes


class FakeManager:
    def __init__(self, station=1):
        self._station = station
        self._docker_container = f"vpn-{station}"


def _app(*, managers=None, runner=None):
    app = FastAPI()
    register_station_logs_routes(
        app,
        managers_getter=lambda: list(managers or [FakeManager()]),
        docker_runner=runner or (
            lambda *args, **kwargs: SimpleNamespace(
                stdout=b"hello\n",
                stderr=b"warning\n",
            )
        ),
        platform_name="win32",
    )
    return app


def test_unknown_station_is_404():
    response = TestClient(_app(managers=[FakeManager(1)])).get(
        "/api/vpn/station/2/logs"
    )
    assert response.status_code == 404


def test_logs_tail_is_clamped_and_merged():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=b"out", stderr=b"err")

    payload = TestClient(_app(runner=runner)).get(
        "/api/vpn/station/1/logs",
        params={"lines": 5000},
    ).json()
    assert payload["station"] == 1
    assert payload["tail"] == 300
    assert payload["logs"] == "outerr"
    assert calls[0][0] == ["docker", "logs", "--tail", "300", "vpn-1"]
    assert calls[0][1]["creationflags"] == 0x08000000


def test_runner_error_is_502():
    def broken(*args, **kwargs):
        raise RuntimeError("docker down")

    response = TestClient(_app(runner=broken)).get("/api/vpn/station/1/logs")
    assert response.status_code == 502
    assert "docker down" in response.json()["error"]
