"""Egress configuration tests — providers, weights, strategy, and catalog sanitization."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import (
    EGRESS,
    CLIENTS,
    _egress_provider_default,
    _egress_provider_weight_default,
    _egress_strategy_default,
)
from dashboard.routers.egress_catalog_routes import register_egress_catalog_routes
from free.egress_runtime import build_runtime


def test_egress_provider_defaults_from_settings():
    """EGRESS providers expose enabled/weight defaults from settings."""
    assert isinstance(EGRESS.get("providers"), dict)
    for name in ("nordvpn", "warp", "proxies", "direct"):
        provider = EGRESS["providers"][name]
        assert "enabled" in provider
        assert "weight" in provider
        assert isinstance(provider["enabled"], bool)
        assert isinstance(provider["weight"], int)

    # Defaults reflect config/settings.py migration helpers
    assert _egress_provider_default("nordvpn") is True
    assert _egress_provider_default("warp") is True
    assert _egress_provider_default("proxies") is True
    assert _egress_provider_default("direct") is False
    assert _egress_provider_weight_default("nordvpn") == 52
    assert _egress_provider_weight_default("warp") == 24
    assert _egress_provider_weight_default("proxies") == 24
    assert _egress_provider_weight_default("direct") == 0


def test_egress_strategy_default():
    """EGRESS strategy is exposed and helper returns a valid strategy string."""
    strategy = _egress_strategy_default()
    assert isinstance(strategy, str)
    assert strategy
    assert EGRESS.get("strategy") == strategy


def test_egress_clients_config_shape():
    """CLIENTS config includes the expected top-level keys."""
    for key in ("enabled", "default_family", "registry", "tool_policy", "unknown_client", "families"):
        assert key in CLIENTS
    assert isinstance(CLIENTS["families"], dict)
    for family in ("anthropic_messages", "openai_chat", "openai_responses", "passthrough"):
        assert family in CLIENTS["families"]


def test_egress_disabled_provider_not_in_runtime():
    """A provider disabled in egress_catalog config is not enabled in runtime."""
    runtime = build_runtime(
        {
            "egress_catalog": {
                "enabled": True,
                "providers": {
                    "public": {"enabled": False},
                    "warp": {"enabled": True},
                    "nordvpn": {"enabled": False},
                },
            }
        },
        db_path=":memory:",
        nordvpn_stations=[],
    )
    assert runtime is not None
    try:
        # Runtime keeps provider configs; disabled providers should not be selectable/enabled.
        public_cfg = runtime.providers.get(runtime.providers and next(iter(runtime.providers)).__class__("public")) if False else None
        # Direct check via internal provider map keys/policies.
        from free.egress_catalog import Provider

        # Disabled providers are not materialized as selectable runtime policies.
        enabled = {str(p.provider.value if hasattr(p.provider, "value") else p.provider) for p in runtime.policies.values() if getattr(p, "enabled", True)}
        assert "public" not in enabled
        assert "nordvpn" not in enabled
        assert "warp" in enabled
    finally:
        import asyncio

        asyncio.run(runtime.close())


def test_egress_catalog_options_sanitized():
    """Catalog options must not expose credentials and must expose has_credentials."""

    @dataclass
    class Option:
        key: str
        provider: str
        status: str
        username: str = ""
        password: str = ""

    class FakeStore:
        def list_options(self, provider=None, status=None, kind=None, limit=100, offset=0):
            return [
                Option("k1", "nordvpn", "usable", username="user1", password="pass1"),
                Option("k2", "warp", "usable", username="", password=""),
            ]

        def stats(self):
            return {}

        def list_runs(self, provider=None, limit=20):
            return []

        def list_observations(self, key=None, limit=100):
            return []

        def list_sources(self):
            return []

    app = FastAPI()
    from types import SimpleNamespace

    runtime = SimpleNamespace(store=FakeStore())
    register_egress_catalog_routes(app, lambda: runtime)
    client = TestClient(app)

    resp = client.get("/api/egress/catalog/options")
    assert resp.status_code == 200
    options = resp.json()["options"]
    assert len(options) == 2

    for opt in options:
        assert "username" not in opt
        assert "password" not in opt
        assert "has_credentials" in opt

    assert options[0]["has_credentials"] is True
    assert options[1]["has_credentials"] is False
