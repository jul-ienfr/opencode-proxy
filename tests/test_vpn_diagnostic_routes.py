"""VPN discovery/upload/diagnostic route tests (extracted router)."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.routers.vpn_diagnostic_routes import (
    register_vpn_diagnostic_routes,
)


def _app(tmp_path, *, managers=None, mode="vpn"):
    persisted = []

    async def servers(_code, limit=5):
        assert limit == 5
        return [{"host": "de1.nordvpn.com"}]

    async def countries(_force=False):
        return [{"code": "DE", "name": "Germany"}]

    app = FastAPI()
    register_vpn_diagnostic_routes(
        app,
        token_checker=lambda request: None,
        managers_getter=lambda: list(managers or []),
        proxy_mode_getter=lambda: mode,
        servers_by_country=servers,
        countries_getter=countries,
        settings=SimpleNamespace(
            IP_ROTATION={"server_countries": "France"}
        ),
        persist_vpn_config=lambda updates: persisted.append(updates),
        docker_diag=lambda: {"running": True, "available": True},
        compose_config=lambda: ["compose: OK"],
        config_yaml_mtime=lambda: 10.0,
        config_yaml_known_mtime_getter=lambda: 5.0,
        root=str(tmp_path),
    )
    return TestClient(app), persisted


def test_discover_adds_country_name(tmp_path):
    client, persisted = _app(tmp_path)
    payload = client.post(
        "/api/vpn/discover-and-add",
        json={"country": "de"},
    ).json()
    assert payload == {"count": 1, "country": "Germany"}
    assert persisted == [{"server_countries": "France, Germany"}]


def test_upload_config_sanitizes_name(tmp_path):
    client, persisted = _app(tmp_path)
    response = client.post(
        "/api/vpn/upload-config",
        data={"name": "my config!"},
        files={"config": ("x.ovpn", b"client\nremote x\n")},
    ).json()
    assert response["ok"] is True
    assert response["path"].endswith("my_config_.ovpn")
    assert (tmp_path / "vpn_configs" / "custom" / "my_config_.ovpn").is_file()
    assert persisted[-1]["custom_ovpn_file"].endswith("my_config_.ovpn")


def test_diagnostic_reports_mode_docker_and_dirty_config(tmp_path):
    managers = [
        SimpleNamespace(status="error", current_ip=None),
    ]
    client, _ = _app(tmp_path, managers=managers)
    payload = client.get("/api/vpn/diagnostic").json()
    assert payload["current_mode"] == "vpn"
    assert payload["status"] == "not_connected"
    assert payload["docker"]["running"] is True
    assert payload["config_yaml_dirty"] is True
    assert payload["compose_config"] == ["compose: OK"]
    assert "Docker OK" in payload["recommendation"]
