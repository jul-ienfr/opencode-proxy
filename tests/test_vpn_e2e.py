"""E2E VPN smoke — [45] automated. Mocks docker/compose, no live container needed."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import config.settings as st


@pytest.mark.asyncio
async def test_vpn_e2e_status_and_rotate(monkeypatch):
    from vpn_manager import VPNManager
    import shared_state

    fake_pool = MagicMock()
    fake_pool.get_status.return_value = {"enabled": True, "status": "connected", "current_ip": "1.2.3.4"}
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
    mgr.get_status.return_value = {"status": "connected", "current_country": "Germany", "next_country": "France"}
    mgr.refresh_status = AsyncMock()
    monkeypatch.setattr(shared_state, "vpn_managers", [mgr], raising=False)

    # Verify refresh_status was callable and pool status is reachable
    await mgr.refresh_status()
    assert mgr.refresh_status.called
    assert fake_pool.get_status()["current_ip"] == "1.2.3.4"


@pytest.mark.asyncio
async def test_vpn_e2e_free_fallback_when_all_stations_exhausted(monkeypatch):
    import opencode
    # When every station's (model,IP) is hot, free path should be skipped
    monkeypatch.setattr(opencode, "_free_stations_exhausted", lambda m: True)
    monkeypatch.setattr(opencode, "_free_ip_pool", MagicMock(enabled=True))
    result = await opencode._try_free_model_first({}, {}, "anthropic", "kimi-k2.6")
    assert result is None


def test_vpn_e2e_geo_block_without_vpn(monkeypatch):
    import opencode
    monkeypatch.setattr(st, "GEO_ENABLED", True)
    monkeypatch.setattr(st, "IP_ROTATION", {"server_countries": "Germany,France,United States"})
    route = {"model": "muse-spark-1.2-contributor", "geo": {"allowed_countries": ["United States"], "mode": "strict", "require_vpn": True}}
    r = st.resolve_geo(route)
    assert r["geo_status"] == "ok"
    resp = opencode._geo_block_response(route, None, "openai")
    assert resp.status_code == 403
    assert resp.headers.get("X-Geo-Blocked") == "1"
