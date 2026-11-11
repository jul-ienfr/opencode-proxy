"""VPN heal route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from dashboard.routers.vpn_heal_routes import register_vpn_heal_routes


class FakeFleet:
    def __init__(self, healed=True):
        self.healed = healed
        self.calls = 0

    async def heal(self):
        self.calls += 1
        return {"healed": self.healed}


def _app(*, managers=None, count=6, fleet=None, apply=None, token_error=None, error=None):
    state = {"managers": list(managers or [])}
    fleet = fleet or FakeFleet()
    calls = []

    async def apply_count(n):
        calls.append(n)
        state["managers"] = [object() for _ in range(n)]
        if error:
            raise RuntimeError(error)
        return None

    app = FastAPI()
    register_vpn_heal_routes(
        app,
        token_checker=lambda request: token_error,
        managers_getter=lambda: list(state["managers"]),
        station_count_getter=lambda: count,
        fleet_getter=lambda: fleet,
        apply_station_count=apply or apply_count,
    )
    return app, fleet, calls


def test_heal_converges_missing_stations():
    app, fleet, calls = _app(managers=[object()], count=6)
    payload = TestClient(app).post("/api/vpn/heal").json()
    assert payload["healed"] is True
    assert payload["n"] == 6
    assert payload["managers"] == 6
    assert calls == [6]
    assert fleet.calls == 1


def test_heal_with_enough_managers_skips_apply():
    app, fleet, calls = _app(managers=[object() for _ in range(6)], count=6)
    payload = TestClient(app).post("/api/vpn/heal").json()
    assert payload["managers"] == 6
    assert calls == []
    assert fleet.calls == 1


def test_heal_error_and_token():
    app, _, _ = _app(error="boom")
    payload = TestClient(app).post("/api/vpn/heal").json()
    assert payload["healed"] is False
    assert "boom" in payload["error"]
    token_app, fleet, _ = _app(
        token_error=JSONResponse({"error": "token"}, status_code=401)
    )
    assert TestClient(token_app).post("/api/vpn/heal").status_code == 401
    assert fleet.calls == 0
