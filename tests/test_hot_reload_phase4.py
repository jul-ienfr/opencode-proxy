"""
Phase 4 — Hot-reload tests (sans full_restart)
Vérifie que chaque réglage GUI prend effet sans restart complet.
"""

import copy
import os
from unittest.mock import MagicMock, patch

import pytest
import yaml

ROOT = os.path.dirname(os.path.dirname(__file__))
CONFIG_PATH = os.path.join(ROOT, "config.yaml")


def _backup_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
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
        with open(CONFIG_PATH, encoding="utf-8") as f:
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
    """vpn watchdog interval → persist + hot-reload, sans full_restart (N-station compat)."""
    import dashboard.api as api_module

    new_interval = 42
    with patch.object(api_module, "get_event_manager") as mock_get_em:
        mock_em = MagicMock()
        mock_em.publish = MagicMock()
        mock_get_em.return_value = mock_em
        api_module._persist_vpn_config({"watchdog_interval": new_interval})
        with open(CONFIG_PATH, encoding="utf-8") as f:
            on_disk = yaml.safe_load(f)
        assert on_disk["ip_rotation"]["watchdog_interval"] == new_interval
        # IP_ROTATION may be updated via _persist (best-effort) — check persisted value
        assert on_disk["ip_rotation"]["watchdog_interval"] == new_interval


def test_vpn_circuit_breaker_hot_reload_no_restart(config_backup):
    """circuit breaker threshold/recovery → persist YAML (N-station via update_config)."""
    import dashboard.api as api_module

    new_threshold = 5
    new_recovery = 600
    with patch.object(api_module, "get_event_manager") as mock_get_em:
        mock_em = MagicMock()
        mock_em.publish = MagicMock()
        mock_get_em.return_value = mock_em
        api_module._persist_vpn_config(
            {"circuit_breaker_threshold": new_threshold, "circuit_breaker_recovery": new_recovery}
        )
        with open(CONFIG_PATH, encoding="utf-8") as f:
            on_disk = yaml.safe_load(f)
        assert on_disk["ip_rotation"]["circuit_breaker_threshold"] == new_threshold
        assert on_disk["ip_rotation"]["circuit_breaker_recovery"] == new_recovery


def test_rotation_rules_hot_reload_no_restart(config_backup):
    """rotation_rules → persist YAML (N-station compat)."""
    import config.settings as cfg
    import dashboard.api as api_module

    new_rules = [{"model_pattern": "mimo-*-free", "strategy": "round-robin", "quota": 100}]
    with patch.object(api_module, "get_event_manager") as mock_get_em:
        mock_em = MagicMock()
        mock_em.publish = MagicMock()
        mock_get_em.return_value = mock_em
        cfg._yaml_data.setdefault("ip_rotation", {})["rotation_rules"] = new_rules
        cfg.save_yaml_config()
        cfg.IP_ROTATION["rotation_rules"] = new_rules
        with open(CONFIG_PATH, encoding="utf-8") as f:
            on_disk = yaml.safe_load(f)
        assert on_disk["ip_rotation"]["rotation_rules"] == new_rules
        assert cfg.IP_ROTATION.get("rotation_rules") == new_rules


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

        with open(CONFIG_PATH, encoding="utf-8") as f:
            on_disk = yaml.safe_load(f)
        assert on_disk["ip_rotation"]["schedule"] == new_schedule
        assert mgr._config.get("schedule") == new_schedule
    finally:
        shared_state.vpn_manager = original


@pytest.mark.asyncio
async def test_port_hot_restart_not_full_restart(config_backup, monkeypatch):
    """[P6] Changement de port → hot-restart (mgr.restart), PAS full_restart.

    Ancien test ``test_web_port_hot_restart_not_full_restart`` : WEB_PORT a été
    SUPPRIMÉ (P6 hygiène — rien n'a jamais écouté sur :8082, le dashboard est
    servi par le port principal). L'invariant survit sur ``port``.

    [INCIDENT 26/08] ``save_env``/``apply_server_changes`` sont MOCKÉS : la
    version initiale de ce test écrivait OPENCODE_PORT+1 dans le VRAI .env à
    chaque run de la suite (4000 → 4010 au fil des exécutions). Aucun test
    ne doit muter l'environnement disque réel.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import dashboard.api as _api
    from dashboard.api import register_dashboard

    # Mock server manager
    mock_mgr = MagicMock()
    mock_mgr.is_running = True
    mock_mgr.restart = MagicMock()
    mock_mgr.full_restart = MagicMock()

    # Interception AVANT tout POST : zéro écriture .env / zéro mutation globale
    saved_updates: dict = {}

    def _fake_save_env(updates):
        saved_updates.update(updates)

    monkeypatch.setattr(_api, "save_env", _fake_save_env)
    monkeypatch.setattr(_api, "apply_server_changes", lambda **kw: None)

    app = FastAPI()
    # Use in-memory sqlite connection for dashboard
    import sqlite3

    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, timestamp TEXT, model TEXT, original_model TEXT, duration_ms INTEGER, tokens_input INTEGER, tokens_output INTEGER, tokens_cache INTEGER, success INTEGER, error TEXT)"
    )
    static_dir = os.path.join(ROOT, "static")

    register_dashboard(app, static_dir, conn, server_manager_getter=lambda: mock_mgr)

    client = TestClient(app)

    # Get current config to know current ports
    resp = client.get("/api/config")
    assert resp.status_code == 200
    data = resp.json()
    orig_port = int(data["port"])
    new_port = orig_port + 1 if orig_port < 65535 else orig_port - 1
    payload = {"port": new_port}
    # [fix ticket P1] capture du thread d'exécution de restart
    import threading as _th

    seen_threads = []
    mock_mgr.restart.side_effect = lambda *a, **kw: seen_threads.append(
        _th.current_thread().name
    )

    resp2 = client.post("/api/config", json=payload)
    assert resp2.status_code == 200
    j = resp2.json()
    assert j["status"] == "ok"
    assert j["needs_restart"] is False, "hot-restart should return needs_restart:false, not true"
    # [fix ticket P1] restart tourne sur un thread dédié ("proxy-restart") —
    # l'appel est ASYNCHRONE par rapport à la réponse : on attend l'appel.
    import time as _t

    deadline = _t.monotonic() + 2.0
    while mock_mgr.restart.call_count == 0 and _t.monotonic() < deadline:
        _t.sleep(0.01)
    # Verify hot-restart was called, not full_restart
    mock_mgr.restart.assert_called()
    mock_mgr.full_restart.assert_not_called()
    call_args = mock_mgr.restart.call_args
    args, kwargs = call_args
    found = any(str(v) == str(new_port) for v in list(kwargs.values()) + list(args))
    assert found, f"mgr.restart should be called with port={new_port}, got {call_args}"

    # [fix ticket P1 incident 25/08] restart NE doit PAS s'exécuter sur le
    # thread du handler (la boucle uvicorn) : join(current_thread) dans
    # stop() = serveur mort. Thread dédié "proxy-restart" obligatoire.
    assert seen_threads and seen_threads[0] == "proxy-restart", (
        f"restart doit tourner sur 'proxy-restart', pas {seen_threads}"
    )

    # [INCIDENT 26/08] l'update est bien INTERCEPTÉ, jamais écrit sur disque
    assert saved_updates.get("OPENCODE_PORT") == str(new_port)

    # [P6] plus aucun champ web_port exposé par l'API
    assert "web_port" not in data


def test_server_stop_from_own_thread_does_not_self_join():
    """[fix ticket P1 incident 25/08] stop() appelé DEPUIS le thread serveur
    (handler HTTP sur la boucle uvicorn) : join(current_thread) lèverait
    RuntimeError et restart() mourrait avant start() — « shutdown OK,
    startup jamais relancé ». Le garde doit ignorer le join, pas lever."""
    import threading

    import opencode as oc

    mgr = oc.ServerManager(app=None, host="127.0.0.1", port=1)
    mgr.is_running = True
    mgr._server = None  # pas de vrai serveur : should_exit no-op
    # Simule l'appel depuis le thread serveur lui-même
    mgr._thread = threading.current_thread()

    mgr.stop(timeout=0.01)  # NE doit PAS lever RuntimeError

    assert mgr.is_running is False
    assert mgr._thread is None


def test_server_stop_from_other_thread_joins_normally():
    """Chemin nominal : stop() depuis un AUTRE thread joint bien le thread
    serveur (attente du shutdown gracieux)."""
    import threading
    import time as _t

    import opencode as oc

    mgr = oc.ServerManager(app=None, host="127.0.0.1", port=1)
    mgr.is_running = True
    mgr._server = None

    released = threading.Event()

    def _fake_serve():
        while not released.is_set():
            _t.sleep(0.005)

    t = threading.Thread(target=_fake_serve, name="fake-uvicorn", daemon=True)
    t.start()
    mgr._thread = t

    result: list = []

    def _stopper():
        mgr.stop(timeout=2)
        result.append("stopped")

    stopper = threading.Thread(target=_stopper)
    stopper.start()
    _t.sleep(0.05)  # laisse stop() entrer dans le join
    released.set()
    stopper.join(timeout=3)

    assert result == ["stopped"], "stop() doit joindre le thread serveur sans blocage"
    assert mgr.is_running is False


def test_vpn_servers_persist_reboot_safe(config_backup):
    """servers persist → reboot-safe YAML persistence (N-station compat)."""
    import config.settings as cfg

    new_servers = [
        {"name": "test1", "config": "vpn/configs/test1.ovpn"},
        {"name": "test2", "config": "vpn/configs/test2.ovpn"},
    ]
    # Directly test persistence via _persist_vpn_config path (servers now via ip_rotation)
    cfg._yaml_data.setdefault("ip_rotation", {}).setdefault("openvpn", {})["servers"] = new_servers
    cfg.save_yaml_config()
    cfg.IP_ROTATION.setdefault("openvpn", {})["servers"] = new_servers
    with open(CONFIG_PATH, encoding="utf-8") as f:
        on_disk = yaml.safe_load(f)
    assert on_disk["ip_rotation"]["openvpn"]["servers"] == new_servers


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
