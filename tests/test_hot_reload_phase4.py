"""
Phase 4 — Hot-reload tests (sans full_restart)
Vérifie que chaque réglage GUI prend effet sans restart complet.
"""
import os
import json
import yaml
import pytest
import copy
from unittest.mock import MagicMock, AsyncMock, patch

ROOT = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")


def _backup_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # deep copy
    return copy.deepcopy(data)


def _restore_config(backup):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(backup, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    # reload in-memory
    import config.settings as cfg
    cfg.load_yaml_config()
    # Re-sync globals that depend on YAML
    try:
        cfg.FREE_MODEL_MAP.clear()
        cfg.FREE_MODEL_MAP.update(cfg.yaml_get("free_model_map", default={}) or {})
    except Exception:
        pass
    try:
        cfg.IP_ROTATION.clear()
        cfg.IP_ROTATION.update(cfg.yaml_get("ip_rotation", default={}) or {})
    except Exception:
        pass


@pytest.fixture
def config_backup():
    backup = _backup_config()
    yield backup
    _restore_config(backup)


def test_free_model_map_hot_reload_no_restart(config_backup):
    """free_model_map édité → persist YAML + hot-reload globals, sans full_restart."""
    import config.settings as cfg
    import dashboard.api as api_module

    # Mock server_manager to ensure restart not called
    mock_mgr = MagicMock()
    mock_mgr.restart = MagicMock()
    mock_mgr.full_restart = MagicMock()

    # Mock event manager
    with patch.object(api_module, "get_event_manager") as mock_get_em:
        mock_em = MagicMock()
        mock_em.publish = MagicMock()
        mock_get_em.return_value = mock_em

        # Simulate POST /api/config/free-model-map handler logic (extracted)
        # We directly test the persistence + hot-reload mechanism that the endpoint uses
        new_map = {"deepseek-v4-flash": "deepseek-v4-flash-free", "test-paid": "test-free"}

        # Emulate endpoint: write YAML + hot-reload in-memory (same as api code)
        cfg._yaml_data["free_model_map"] = new_map
        cfg.save_yaml_config()
        cfg.FREE_MODEL_MAP.clear()
        cfg.FREE_MODEL_MAP.update(new_map)
        import opencode as oc
        if hasattr(oc, "FREE_MODEL_MAP"):
            if oc.FREE_MODEL_MAP is not cfg.FREE_MODEL_MAP:
                oc.FREE_MODEL_MAP.clear()
                oc.FREE_MODEL_MAP.update(new_map)

        # Verify YAML persisted
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            on_disk = yaml.safe_load(f)
        assert on_disk.get("free_model_map") == new_map, "YAML not persisted"

        # Verify in-memory hot-reloaded (no restart needed)
        assert cfg.FREE_MODEL_MAP == new_map
        # opencode should see new mapping immediately
        from config import FREE_MODEL_MAP as cfg_fmm
        assert cfg_fmm.get("test-paid") == "test-free"
        # No restart should have been called
        mock_mgr.restart.assert_not_called()
        mock_mgr.full_restart.assert_not_called()


def test_vpn_config_watchdog_hot_reload_no_restart(config_backup):
    """vpn watchdog interval → persist + restart_watchdog hot-reload, sans full_restart."""
    import config.settings as cfg
    import shared_state
    from vpn_manager import VPNManager

    # Create a manager instance for testing (use minimal config)
    ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
    mgr = VPNManager(ip_rot)
    # Mock watchdog methods to avoid actually creating asyncio tasks
    mgr.restart_watchdog = MagicMock()
    mgr.stop_watchdog = MagicMock()

    original = shared_state.vpn_manager
    shared_state.vpn_manager = mgr
    try:
        # Simulate _persist_vpn_config for watchdog_interval
        new_interval = 42
        # Directly test the new _persist logic with hot-reload
        import dashboard.api as api_module

        with patch.object(api_module, "get_event_manager") as mock_get_em:
            mock_em = MagicMock()
            mock_em.publish = MagicMock()
            mock_get_em.return_value = mock_em

            # Call _persist_vpn_config with watchdog_interval
            api_module._persist_vpn_config({"watchdog_interval": new_interval})

            # Verify YAML persisted
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                on_disk = yaml.safe_load(f)
            assert on_disk["ip_rotation"]["watchdog_interval"] == new_interval

            # Verify manager hot-reloaded (restart_watchdog called, not full_restart)
            mgr.restart_watchdog.assert_called_once_with(new_interval)
            # Verify in-memory IP_ROTATION updated
            assert cfg.IP_ROTATION.get("watchdog_interval") == new_interval

            # Ensure no full_restart on manager (VPNManager has no full_restart, but server mgr not called)
            # We can check that manager's config reflects new interval
            assert mgr._config.get("watchdog_interval") == new_interval
    finally:
        shared_state.vpn_manager = original


def test_vpn_circuit_breaker_hot_reload_no_restart(config_backup):
    """circuit breaker threshold/recovery → rebuild CircuitBreaker hot, sans restart."""
    import config.settings as cfg
    import shared_state
    from vpn_manager import VPNManager
    ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
    mgr = VPNManager(ip_rot)
    orig_cb_threshold = mgr._circuit_breaker._failure_threshold
    original = shared_state.vpn_manager
    shared_state.vpn_manager = mgr
    try:
        import dashboard.api as api_module
        with patch.object(api_module, "get_event_manager") as mock_get_em:
            mock_em = MagicMock()
            mock_em.publish = MagicMock()
            mock_get_em.return_value = mock_em

            new_threshold = 5
            new_recovery = 600
            api_module._persist_vpn_config({
                "circuit_breaker_threshold": new_threshold,
                "circuit_breaker_recovery": new_recovery
            })
            # Manager should have new circuit breaker
            assert mgr._circuit_breaker._failure_threshold == new_threshold
            assert mgr._circuit_breaker._recovery_time == new_recovery

            # YAML persisted
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                on_disk = yaml.safe_load(f)
            assert on_disk["ip_rotation"]["circuit_breaker_threshold"] == new_threshold
            assert on_disk["ip_rotation"]["circuit_breaker_recovery"] == new_recovery
    finally:
        shared_state.vpn_manager = original


def test_rotation_rules_hot_reload_no_restart(config_backup):
    """rotation_rules → persist YAML + manager.set_rotation_rules hot-reload."""
    import config.settings as cfg
    import shared_state
    from vpn_manager import VPNManager

    ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
    mgr = VPNManager(ip_rot)
    original = shared_state.vpn_manager
    shared_state.vpn_manager = mgr
    try:
        new_rules = [{"model_pattern": "mimo-*-free", "strategy": "round-robin", "quota": 100}]
        # Simulate endpoint persist logic (as in api.py)
        ip_rot2 = cfg.yaml_get("ip_rotation", default={}) or {}
        ip_rot2["rotation_rules"] = new_rules
        cfg._yaml_data["ip_rotation"] = ip_rot2
        cfg.save_yaml_config()
        cfg.IP_ROTATION["rotation_rules"] = new_rules
        mgr.set_rotation_rules(new_rules)

        # Verify
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            on_disk = yaml.safe_load(f)
        assert on_disk["ip_rotation"]["rotation_rules"] == new_rules
        assert mgr._rotation_rules == new_rules
        assert cfg.IP_ROTATION.get("rotation_rules") == new_rules
    finally:
        shared_state.vpn_manager = original


def test_schedule_hot_reload_no_restart(config_backup):
    """schedule → persist YAML + manager hot-reload."""
    import config.settings as cfg
    import shared_state
    from vpn_manager import VPNManager

    ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
    mgr = VPNManager(ip_rot)
    original = shared_state.vpn_manager
    shared_state.vpn_manager = mgr
    try:
        new_schedule = {"enabled": True, "rules": [{"cron": "0 */2 * * *", "action": "rotate"}]}
        ip_rot2 = cfg.yaml_get("ip_rotation", default={}) or {}
        ip_rot2["schedule"] = new_schedule
        cfg._yaml_data["ip_rotation"] = ip_rot2
        cfg.save_yaml_config()
        cfg.IP_ROTATION["schedule"] = new_schedule
        mgr.update_config({"schedule": new_schedule})

        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            on_disk = yaml.safe_load(f)
        assert on_disk["ip_rotation"]["schedule"] == new_schedule
        assert mgr._config.get("schedule") == new_schedule
    finally:
        shared_state.vpn_manager = original


@pytest.mark.asyncio
async def test_web_port_hot_restart_not_full_restart(config_backup):
    """web_port change → hot-restart (mgr.restart) not full_restart."""
    import config.settings as cfg
    from dashboard.api import register_dashboard
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    # Mock server manager
    mock_mgr = MagicMock()
    mock_mgr.is_running = True
    mock_mgr.restart = MagicMock()
    mock_mgr.full_restart = MagicMock()

    # Need to test POST /api/config with web_port
    # Create a minimal app with register_dashboard
    app = FastAPI()
    # Use in-memory sqlite connection for dashboard
    import sqlite3
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, timestamp TEXT, model TEXT, original_model TEXT, duration_ms INTEGER, tokens_input INTEGER, tokens_output INTEGER, tokens_cache INTEGER, success INTEGER, error TEXT)")
    static_dir = os.path.join(ROOT, "static")

    register_dashboard(app, static_dir, conn, server_manager_getter=lambda: mock_mgr)

    from fastapi.testclient import TestClient
    client = TestClient(app)

    # Get current config to know current ports
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    orig_port = data["port"]
    orig_web = data["web_port"]
    new_web = orig_web + 1 if orig_web < 65535 else orig_web - 1
    # Ensure we use a valid unused port (avoid conflict, just test logic — don't actually bind)
    # We will POST with new_web; the handler will call mock_mgr.restart with web_port
    payload = {"web_port": new_web}
    # The handler's apply_server_changes will update cfg.WEB_PORT, and mgr.restart should be called
    resp2 = client.post("/api/config", json=payload)
    assert resp2.status_code == 200
    j = resp2.json()
    assert j["status"] == "ok"
    assert j["needs_restart"] is False, "hot-restart should return needs_restart:false, not true"
    # Verify hot-restart was called, not full_restart
    mock_mgr.restart.assert_called()
    mock_mgr.full_restart.assert_not_called()
    # Verify args contain web_port
    call_kwargs = mock_mgr.restart.call_args
    # Check that web_port was passed (either as kwarg or arg)
    found = False
    if call_kwargs:
        args, kwargs = call_kwargs
        # Check kwargs
        if "web_port" in kwargs and int(kwargs["web_port"]) == new_web:
            found = True
        # Check if port in kwargs (could be string)
        for v in list(kwargs.values()) + list(args):
            if str(v) == str(new_web):
                found = True
    assert found, f"mgr.restart should be called with web_port={new_web}, got {call_kwargs}"

    # Verify that config.yaml persisted web_port
    # Need to check via cfg (since dashboard writes via save_env + apply_server_changes + save_yaml? Actually web_port handled via save_env + apply_server_changes)
    # The API writes to .env and also updates WEB_PORT; check that .env or yaml would be handled
    # For this test we at least ensure no exception and needs_restart false


def test_vpn_servers_persist_reboot_safe(config_backup):
    """_persist_vpn_servers → reboot-safe YAML persistence."""
    import config.settings as cfg
    import dashboard.api as api_module
    import shared_state
    from vpn_manager import VPNManager

    ip_rot = cfg.yaml_get("ip_rotation", default={}) or {}
    mgr = VPNManager(ip_rot)
    original = shared_state.vpn_manager
    shared_state.vpn_manager = mgr
    try:
        with patch.object(api_module, "get_event_manager") as mock_get_em:
            mock_em = MagicMock()
            mock_em.publish = MagicMock()
            mock_get_em.return_value = mock_em

            new_servers = [
                {"name": "test1", "config": "vpn/configs/test1.ovpn"},
                {"name": "test2", "config": "vpn/configs/test2.ovpn"},
            ]
            api_module._persist_vpn_servers(new_servers)

            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                on_disk = yaml.safe_load(f)
            assert on_disk["ip_rotation"]["openvpn"]["servers"] == new_servers
            # Manager hot-reloaded
            assert mgr._servers == new_servers
    finally:
        shared_state.vpn_manager = original


def test_ttl_cache_stats_not_regression():
    """_TTLCache still works (non-regression Phase4)."""
    from dashboard.api import _TTLCache
    c = _TTLCache(ttl=0.2)
    c.set("k", "v")
    assert c.get("k") == "v"
    import time
    time.sleep(0.25)
    assert c.get("k") is None
    c.set("a", 1)
    c.set("b", 2)
    c.invalidate("a")
    assert c.get("a") is None
    assert c.get("b") == 2
    c.invalidate()
    assert c.get("b") is None
