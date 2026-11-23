"""Contract tests for the unified egress configuration (023 T015).

Real sources under test:
- ``config.settings.EGRESS`` / ``CLIENTS`` (static snapshot).
- ``config.settings.get_egress_config()`` (live helper; the operator endpoint
  delegates to the same data).
- ``dashboard.routers.egress_catalog_routes`` (read-only catalog contract).

No invented API: the helper is the only config surface exposed today.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config import settings as config_settings
from config.settings import CLIENTS, EGRESS

# ---------------------------------------------------------------------------
# Minimal fake runtime for the read-only catalog routes
# ---------------------------------------------------------------------------


@dataclass
class _Option:
    """Dataclass option so the route's ``asdict()`` path is exercised."""

    key: str
    provider: str
    status: str
    kind: str
    host: str
    port: int
    egress_ip: str
    username: str = ""
    password: str = ""


class _FakeStore:
    """Minimal mock that satisfies the egress catalog routes interface."""

    def summary(self) -> dict[str, Any]:
        return {"nordvpn": {"usable": 1}, "warp": {"usable": 1}}

    def list_runs(self, provider=None, limit: int = 20):  # type: ignore[override]
        return []

    def list_options(
        self,
        provider: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[_Option]:  # type: ignore[override]
        return [
            _Option(
                key="warp:1.2.3.4",
                provider=provider or "warp",
                status=status or "usable",
                kind=kind or "https",
                host="1.2.3.4",
                port=443,
                egress_ip="1.2.3.4",
                username="admin",
                password="s3cret",
            ),
            _Option(
                key="warp:5.6.7.8",
                provider=provider or "warp",
                status=status or "usable",
                kind=kind or "https",
                host="5.6.7.8",
                port=443,
                egress_ip="5.6.7.8",
            ),
        ]

    def list_observations(self, key: str | None = None, limit: int = 50):
        return []

    def list_sources(self):
        return []


class _SimpleRuntime:
    """Wraps _FakeStore and exposes the catalog route's expected helpers."""

    def __init__(self) -> None:
        self.store = _FakeStore()
        self.threat_intel = None

    def rotation_plan(self) -> dict[str, Any]:
        return {"nordvpn": {}, "warp": {}}


# ---------------------------------------------------------------------------
# 1. Static snapshot / live helper
# ---------------------------------------------------------------------------


def test_egress_config_has_the_four_providers() -> None:
    """EGRESS['providers'] lists exactly the four declared providers."""
    providers = EGRESS["providers"]
    assert set(providers) == {"nordvpn", "warp", "proxies", "direct"}
    for name, cfg in providers.items():
        assert isinstance(cfg["enabled"], bool), f"{name} enabled should be bool"
        assert isinstance(cfg["weight"], int), f"{name} weight should be int"
        assert cfg["weight"] >= 0, f"{name} weight must be non-negative"


def test_egress_strategy_and_client_mode() -> None:
    """Strategy and wire.client_mode are well-formed."""
    assert EGRESS["strategy"] in {"balance", "priority", "failover"}
    wire = EGRESS["wire"]
    assert isinstance(wire["client_mode"], str) and wire["client_mode"]
    assert isinstance(wire["burn_guard"], bool)


def test_egress_rotation_budgets() -> None:
    """Rotation budget keys exist with correct types."""
    rot = EGRESS["rotation"]
    required_keys = {
        "min_interval_s",
        "max_per_hour",
        "probe_budget_per_ip",
        "probe_cooldown_s",
        "release_on_single_429",
        "require_ip_agreement",
    }
    assert set(rot) == required_keys, f"Missing/extra rotation keys: {set(rot) ^ required_keys}"
    assert isinstance(rot["min_interval_s"], int) and rot["min_interval_s"] >= 60
    assert isinstance(rot["max_per_hour"], int) and rot["max_per_hour"] >= 1
    assert isinstance(rot["probe_budget_per_ip"], int) and rot["probe_budget_per_ip"] >= 0
    assert isinstance(rot["probe_cooldown_s"], int) and rot["probe_cooldown_s"] >= 0
    assert isinstance(rot["release_on_single_429"], bool)
    assert isinstance(rot["require_ip_agreement"], bool)


def test_clients_config_shape() -> None:
    """CLIENTS has the expected families and top-level keys."""
    for key in ("enabled", "default_family", "registry", "tool_policy", "unknown_client", "families"):
        assert key in CLIENTS, f"Missing CLIENTS key {key}"
    families = CLIENTS["families"]
    assert isinstance(families, dict)
    for family in ("anthropic_messages", "openai_chat", "openai_responses", "passthrough"):
        assert family in families, f"Missing client family {family}"


def test_egress_static_snapshot_matches_live_helper() -> None:
    """EGRESS (static import) mirrors get_egress_config() structure."""
    live = config_settings.get_egress_config()
    assert set(live["providers"]) == {"nordvpn", "warp", "proxies", "direct"}
    for name, cfg in live["providers"].items():
        assert isinstance(cfg["enabled"], bool)
        assert isinstance(cfg["weight"], int) and cfg["weight"] >= 0
    assert live["strategy"] in {"balance", "priority", "failover"}
    assert isinstance(live["wire"]["client_mode"], str) and live["wire"]["client_mode"]
    assert set(live["rotation"]) == {
        "min_interval_s",
        "max_per_hour",
        "probe_budget_per_ip",
        "probe_cooldown_s",
        "release_on_single_429",
        "require_ip_agreement",
    }
    # The static snapshot should not drift from the live builder.
    assert set(EGRESS["providers"]) == set(live["providers"])
    assert EGRESS["strategy"] == live["strategy"]
    assert EGRESS["wire"]["client_mode"] == live["wire"]["client_mode"]
    assert set(EGRESS["rotation"]) == set(live["rotation"])


def test_disabled_provider_reports_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider disabled in egres config reports enabled=False from the helper."""
    original = copy.deepcopy(config_settings._yaml_data.get("egress", {}))
    data = copy.deepcopy(original)
    data.setdefault("providers", {})["warp"] = {"enabled": False, "weight": 0}
    monkeypatch.setitem(config_settings._yaml_data, "egress", data)
    try:
        cfg = config_settings.get_egress_config()
        assert cfg["providers"]["warp"]["enabled"] is False
        assert cfg["providers"]["warp"]["weight"] == 0
    finally:
        monkeypatch.setitem(config_settings._yaml_data, "egress", original)


# ---------------------------------------------------------------------------
# 2. Dashboard /api/egress/catalog contract (read-only)
# ---------------------------------------------------------------------------


def _catalog_client() -> TestClient:
    from dashboard.routers.egress_catalog_routes import register_egress_catalog_routes

    app = FastAPI()
    register_egress_catalog_routes(app, lambda: _SimpleRuntime())
    return TestClient(app)


def test_catalog_route_reports_providers_and_rotation() -> None:
    """GET /api/egress/catalog returns enabled:true plus providers/rotation."""
    client = _catalog_client()
    resp = client.get("/api/egress/catalog")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.json()}"
    data = resp.json()
    assert data["enabled"] is True
    assert "providers" in data
    assert "rotation" in data


def test_catalog_options_never_expose_credentials() -> None:
    """GET /api/egress/catalog/options never returns username/password fields."""
    client = _catalog_client()
    resp = client.get("/api/egress/catalog/options")
    assert resp.status_code == 200
    options = resp.json()["options"]
    assert len(options) == 2
    for opt in options:
        assert "username" not in opt, f"username leaked in option {opt}"
        assert "password" not in opt, f"password leaked in option {opt}"
        assert "has_credentials" in opt
    assert options[0]["has_credentials"] is True
    assert options[1]["has_credentials"] is False