"""Geo policy route tests (extracted router)."""

from __future__ import annotations

import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.geo_routes import register_geo_routes


class FakeSettings:
    def __init__(self):
        self.GEO_ENABLED = False
        self.GEO_VERSION = 1
        self.GEO_POLICIES = {"US": {"mode": "direct"}}
        self.SORTED_GEO_POLICIES = sorted(self.GEO_POLICIES.items())
        self.GEO_ALLOW_DIRECT_WHEN_COMPATIBLE = True
        self._yaml_data = {}
        self.saved = 0

    def yaml_get(self, section, key, default=None):
        if (section, key) == ("ip_rotation", "control_api_key"):
            return "secret"
        return default

    def save_yaml_config(self):
        self.saved += 1


def _app(tmp_path, *, mtime=100.0):
    settings = FakeSettings()
    clock = {"mtime": mtime}
    known = []
    app = FastAPI()
    register_geo_routes(
        app,
        token_checker=lambda request: None,
        settings=settings,
        config_yaml_mtime=lambda: clock["mtime"],
        config_yaml_known_mtime_setter=known.append,
        root=str(tmp_path),
    )
    return app, settings, clock, known


def test_get_geo_policies():
    app, *_ = _app(None)
    payload = TestClient(app).get("/api/geo-policies").json()
    assert payload["enabled"] is False
    assert payload["version"] == 1
    assert payload["policies"] == {"US": {"mode": "direct"}}
    assert payload["allow_direct_when_compatible"] is True


def test_put_requires_control_key_and_etag():
    app, settings, clock, known = _app(None)
    client = TestClient(app)
    assert client.put("/api/geo-policies", json={}).status_code == 401
    assert client.put(
        "/api/geo-policies",
        json={},
        headers={"X-API-Key": "secret", "If-Match": "999"},
    ).status_code == 412
    clock["mtime"] = 50.0
    response = client.put(
        "/api/geo-policies",
        json={
            "enabled": True,
            "version": 2,
            "policies": {"FR": {"mode": "vpn"}},
            "allow_direct_when_compatible": False,
        },
        headers={"X-API-Key": "secret", "If-Match": "50"},
    ).json()
    assert response["ok"] is True
    assert response["enabled"] is True
    assert response["version"] == 2
    assert response["etag"] == "50"
    assert settings.GEO_ENABLED is True
    assert settings.GEO_VERSION == 2
    assert settings.GEO_POLICIES == {"FR": {"mode": "vpn"}}
    assert settings.SORTED_GEO_POLICIES == [("FR", {"mode": "vpn"})]
    assert settings.GEO_ALLOW_DIRECT_WHEN_COMPATIBLE is False
    assert settings.saved == 1
    assert known == [50.0]


def test_put_validation_and_rollback():
    app, settings, *_ = _app(None)
    client = TestClient(app)
    headers = {"X-API-Key": "secret"}
    assert client.put(
        "/api/geo-policies",
        json={"version": "nope"},
        headers=headers,
    ).status_code == 400
    assert client.put(
        "/api/geo-policies",
        json={"policies": []},
        headers=headers,
    ).status_code == 400
    assert client.post(
        "/api/geo-policies/rollback",
        headers=headers,
    ).json() == {"ok": True, "enabled": False}
    assert settings.GEO_ENABLED is False
    assert settings._yaml_data["geo"]["enabled"] is False


def test_geo_notifications_are_read_and_capped(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    rows = [{"message": str(index)} for index in range(25)]
    (logs / "geo_notifications.json").write_text(
        json.dumps(rows),
        encoding="utf-8",
    )
    app, *_ = _app(tmp_path)
    payload = TestClient(app).get("/api/geo/notifications").json()
    assert len(payload["notifications"]) == 20
    assert payload["notifications"][0]["message"] == "5"
