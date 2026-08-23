"""Dashboard auth HMAC — Phase2 F-M8: compare_digest + 0.0.0.0 warning."""

import os
import hmac
import importlib
import pytest
from fastapi.testclient import TestClient


def test_hmac_compare_digest():
    # HMAC must be constant-time
    token = "secret123"
    assert hmac.compare_digest(token, "secret123") is True
    assert hmac.compare_digest(token, "secret124") is False


def test_dashboard_token_401_when_set(monkeypatch):
    # Simulate DASHBOARD_TOKEN set
    monkeypatch.setenv("DASHBOARD_TOKEN", "tok123")
    monkeypatch.setenv("OPENCODE_HOST", "127.0.0.1")
    # Need to reload dashboard.api to pick up env
    import dashboard.api as api

    # monkeypatch the module globals directly
    monkeypatch.setattr(api, "_DASHBOARD_TOKEN", "tok123", raising=False)
    monkeypatch.setattr(api, "_DASHBOARD_REQUIRE_TOKEN", False, raising=False)
    monkeypatch.setattr(api, "_host_for_warning", "127.0.0.1", raising=False)

    from dashboard.api import _check_dashboard_token
    from fastapi import Request

    class FakeReq:
        def __init__(self, headers):
            self.headers = headers

    # No header → 401
    resp = _check_dashboard_token(FakeReq({}))
    assert resp is not None
    assert resp.status_code == 401
    # Correct header → None (allowed)
    resp2 = _check_dashboard_token(FakeReq({"X-Dashboard-Token": "tok123"}))
    assert resp2 is None


def test_dashboard_require_token_403_on_0000(monkeypatch):
    monkeypatch.setenv("DASHBOARD_TOKEN", "")
    import dashboard.api as api

    monkeypatch.setattr(api, "_DASHBOARD_TOKEN", "", raising=False)
    monkeypatch.setattr(api, "_DASHBOARD_REQUIRE_TOKEN", True, raising=False)
    monkeypatch.setattr(api, "_host_for_warning", "0.0.0.0", raising=False)
    from dashboard.api import _check_dashboard_token

    class FakeReq:
        headers = {}

    resp = _check_dashboard_token(FakeReq())
    assert resp is not None
    assert resp.status_code == 403
    assert "DASHBOARD_TOKEN required" in resp.body.decode()


def test_dashboard_open_when_no_token_no_require(monkeypatch):
    import dashboard.api as api

    monkeypatch.setattr(api, "_DASHBOARD_TOKEN", "", raising=False)
    monkeypatch.setattr(api, "_DASHBOARD_REQUIRE_TOKEN", False, raising=False)
    monkeypatch.setattr(api, "_host_for_warning", "0.0.0.0", raising=False)
    from dashboard.api import _check_dashboard_token

    class FakeReq:
        headers = {}

    assert _check_dashboard_token(FakeReq()) is None
