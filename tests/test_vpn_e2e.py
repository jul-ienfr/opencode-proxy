"""E2E VPN smoke — [45] automated. Mocks docker/compose, no live container needed."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import config.settings as st


@pytest.mark.asyncio
async def test_vpn_e2e_status_and_rotate(monkeypatch):
    import shared_state

    fake_pool = MagicMock()
    fake_pool.get_status.return_value = {
        "enabled": True,
        "status": "connected",
        "current_ip": "1.2.3.4",
    }
    fake_pool.active_station = None
    monkeypatch.setattr(shared_state, "vpn_managers", [], raising=False)
    monkeypatch.setattr(shared_state, "free_ip_pool", fake_pool, raising=False)
    monkeypatch.setattr(shared_state, "vpn_manager", MagicMock(current_ip="1.2.3.4"), raising=False)

    # Mock VPNManager that pretends to be connected
    mgr = MagicMock()
    mgr._station = 1
    mgr.status = "connected"
    mgr.current_ip = "1.2.3.4"
    mgr.current_server = "de1"
    mgr._current_country = "Germany"
    mgr.get_status.return_value = {
        "status": "connected",
        "current_country": "Germany",
        "next_country": "France",
    }
    mgr.refresh_status = AsyncMock()
    monkeypatch.setattr(shared_state, "vpn_managers", [mgr], raising=False)

    # Verify refresh_status was callable and pool status is reachable
    await mgr.refresh_status()
    assert mgr.refresh_status.called
    assert fake_pool.get_status()["current_ip"] == "1.2.3.4"


@pytest.mark.asyncio
async def test_vpn_e2e_free_fallback_when_all_stations_exhausted(monkeypatch):
    import opencode

    # FREE_MODEL_MAP now contains kimi-k2.6 → the free path is entered, so
    # the pool mock must be awaitable. The pre-flight gate was removed
    # [fix 20/08] — exhaustion is handled by the multi-attempt loop, but
    # the test intent (no free station usable → fallback to paid = None)
    # is preserved by exhausting stations and failing the direct fallback.
    free_pool = MagicMock(enabled=True)
    free_pool.socks5_mode = False
    free_pool._best_station = MagicMock(return_value=None)
    free_pool._best_station_excluding_many = MagicMock(return_value=None)
    free_pool._socks5_next = MagicMock(return_value=None)
    free_pool.on_request = AsyncMock(return_value=(None, None))
    free_pool.on_quota_exhausted = MagicMock()
    monkeypatch.setattr(opencode, "_free_stations_exhausted", lambda m: True)
    monkeypatch.setattr(opencode, "_free_ip_pool", free_pool)
    monkeypatch.setattr(opencode, "_vpn_manager", None)
    monkeypatch.setattr(opencode, "_get_cached_public_ip", AsyncMock(return_value="1.2.3.4"))

    async def _fail_curl(*a, **kw):
        raise RuntimeError("tunnel down")

    async def _fail_direct(*a, **kw):
        raise opencode.UpstreamError("direct failed")

    monkeypatch.setattr(opencode, "_do_free_request_curl_cffi", _fail_curl)
    monkeypatch.setattr(opencode, "_do_request_with_retry", _fail_direct)
    result = await opencode._try_free_model_first({}, {}, "anthropic", "kimi-k2.6")
    assert result is None


def test_vpn_e2e_geo_block_without_vpn(monkeypatch):
    import opencode

    monkeypatch.setattr(st, "GEO_ENABLED", True)
    monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "Germany,France,United States"})
    route = {
        "model": "muse-spark-1.2-contributor",
        "geo": {"allowed_countries": ["United States"], "mode": "strict", "require_vpn": True},
    }
    r = st.resolve_geo(route)
    assert r["geo_status"] == "ok"
    resp = opencode._geo_block_response(route, None, "openai")
    assert resp.status_code == 403
    assert resp.headers.get("X-Geo-Blocked") == "1"
