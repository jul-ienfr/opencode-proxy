"""NordVPN status/country route tests (extracted router)."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.nordvpn_routes import register_nordvpn_routes


class FakeManager:
    def __init__(self, status="connected", current_ip="1.2.3.4"):
        self.status = status
        self.current_ip = current_ip

    def get_status(self):
        return {
            "status": self.status,
            "current_country": "Sweden",
            "city": "Stockholm",
            "ip": self.current_ip,
        }


async def _countries(use_api: bool):
    return ["Sweden", "Japan"] if use_api else ["Sweden"]


def _app(*, managers=None, config=None, credentials_path="/nonexistent"):
    app = FastAPI()
    register_nordvpn_routes(
        app,
        managers_getter=lambda: list(managers or []),
        countries_getter=_countries,
        config_getter=lambda: dict(config or {}),
        credentials_path=credentials_path,
    )
    return app


def test_available_requires_credentials_and_provider(tmp_path):
    cred = tmp_path / "credentials.env"
    cred.write_text("USER=x\n", encoding="utf-8")
    client = TestClient(_app(credentials_path=str(cred), config={"server_provider": "nordvpn"}))
    assert client.get("/api/vpn/nordvpn-available").json() == {"available": True}

    client = TestClient(_app(credentials_path=str(cred), config={"server_provider": "other"}))
    assert client.get("/api/vpn/nordvpn-available").json() == {"available": False}


def test_status_aggregates_managers():
    client = TestClient(_app(managers=[FakeManager("connected", "1.2.3.4")]))
    payload = client.get("/api/vpn/nordvpn-status").json()
    assert payload["connected"] is True
    assert payload["country"] == "Sweden"
    assert payload["city"] == "Stockholm"
    assert payload["ip"] == "1.2.3.4"


def test_status_without_managers_is_false():
    client = TestClient(_app(managers=[]))
    payload = client.get("/api/vpn/nordvpn-status").json()
    assert payload == {"connected": False, "country": None, "city": None, "ip": None}


def test_countries_uses_api_flag():
    client = TestClient(_app(config={"use_nordvpn_api": True}))
    assert client.get("/api/vpn/nordvpn-countries").json()["countries"] == ["Sweden", "Japan"]
    client = TestClient(_app(config={"use_nordvpn_api": False}))
    assert client.get("/api/vpn/countries").json()["countries"] == ["Sweden"]
