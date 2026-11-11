"""VPN credentials/state/export route tests."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.vpn_state_routes import register_vpn_state_routes


class FakeManager:
    def __init__(self):
        self.saved = 0
        self._ip_history = [{"ip": "1.2.3.4"}]
        self._total_switches = 4

    def save_state(self):
        self.saved += 1

    def get_config(self):
        return {"enabled": True, "protocol": "udp"}


def _app(tmp_path, *, manager=None, writer=None):
    app = FastAPI()
    register_vpn_state_routes(
        app,
        token_checker=lambda request: None,
        manager_getter=lambda: manager,
        credentials_path=str(tmp_path / "credentials.env"),
        write_credentials=writer or (lambda user, password: None),
    )
    return app


def test_credentials_status_and_preview(tmp_path):
    client = TestClient(_app(tmp_path))
    assert client.get("/api/vpn/credentials").json() == {
        "exists": False,
        "username_preview": "",
    }
    (tmp_path / "credentials.env").write_text(
        "OPENVPN_USER=abcdef\nOPENVPN_PASSWORD=secret\n",
        encoding="utf-8",
    )
    payload = client.get("/api/vpn/credentials").json()
    assert payload["exists"] is True
    assert payload["username_preview"] == "abcd****"


def test_save_credentials_validation_and_write(tmp_path):
    written = []
    app = _app(
        tmp_path,
        writer=lambda user, password: written.append((user, password)),
    )
    client = TestClient(app)
    assert client.post("/api/vpn/credentials", json={}).status_code == 400
    response = client.post(
        "/api/vpn/credentials",
        json={"username": "u", "password": "p"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert written == [("u", "p")]


def test_save_credentials_value_error(tmp_path):
    def writer(user, password):
        raise ValueError("bad credentials")

    response = TestClient(_app(tmp_path, writer=writer)).post(
        "/api/vpn/credentials",
        json={"username": "u", "password": "p"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "bad credentials"


def test_save_state_and_export(tmp_path):
    manager = FakeManager()
    client = TestClient(_app(tmp_path, manager=manager))
    assert client.post("/api/vpn/save-state").json() == {"ok": True}
    assert manager.saved == 1
    payload = client.get("/api/vpn/export").json()
    assert payload["config"]["enabled"] is True
    assert payload["state"]["ip_history"] == [{"ip": "1.2.3.4"}]
    assert payload["version"] == "1.0"
    assert client.post("/api/vpn/save-state").status_code == 200
    missing = TestClient(_app(tmp_path, manager=None))
    assert "non initialisé" in missing.get("/api/vpn/export").json()["error"]
