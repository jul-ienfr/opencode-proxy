"""Régression P2 : réparation auth bornée (restart immédiat, stop réel, park)."""

from __future__ import annotations

import asyncio

import pytest
from test_vpn_freshness import FakeVPNManager, _cfg

import vpn.manager as vm


def _repair_cfg(tmp_path, **over):
    cfg = _cfg(
        tmp_path,
        station_repair={
            "enabled": True,
            "auth_backoff_s": [300, 900, 1800],
            "park_after_failures": 3,
            "park_s": 1800,
            "auth_global_window_s": 60,
            "auth_global_min_stations": 2,
            "auth_global_pause_s": 300,
        },
    )
    cfg.update(over)
    return cfg


def test_decision_auth_repair_is_immediate_not_grace(tmp_path):
    mgr = FakeVPNManager(_repair_cfg(tmp_path), tmp_path=tmp_path)
    mgr._auth_failed = True
    assert mgr._decide_watchdog_action("AUTH_FAILED")[0] == "auth-repair"
    # Le flag OFF conserve la grâce historique (rollback).
    off = FakeVPNManager(_cfg(tmp_path, watchdog_auth_grace_s=240), tmp_path=tmp_path)
    off._auth_failed = True
    assert off._decide_watchdog_action("AUTH_FAILED")[0] == "grace-first"


def test_decision_park_and_credentials(tmp_path):
    mgr = FakeVPNManager(_repair_cfg(tmp_path), tmp_path=tmp_path)
    mgr._auth_failed = True
    mgr._auth_park_until = mgr._now_fn() + 600.0
    assert mgr._decide_watchdog_action("AUTH_FAILED")[0] == "auth-parked"
    mgr._auth_park_until = 0.0
    mgr._credentials_suspect = True
    assert mgr._decide_watchdog_action("AUTH_FAILED")[0] == "credentials-parked"


def test_decision_global_pause_when_another_probe_owns(tmp_path):
    mgr = FakeVPNManager(_repair_cfg(tmp_path), tmp_path=tmp_path)
    mgr._auth_failed = True
    vm._STATION_AUTH_GUARD.clear()
    vm._STATION_AUTH_GUARD.note_failure("2", now=mgr._now_fn())
    vm._STATION_AUTH_GUARD.note_failure("3", now=mgr._now_fn())
    # Une autre station possède la sonde.
    assert vm._STATION_AUTH_GUARD.allow_probe("2", now=mgr._now_fn()) is True
    assert mgr._decide_watchdog_action("AUTH_FAILED")[0] == "global-pause"
    vm._STATION_AUTH_GUARD.clear()


@pytest.mark.asyncio
async def test_repair_loop_restart_immediate_succeeds(tmp_path):
    mgr = FakeVPNManager(_repair_cfg(tmp_path), tmp_path=tmp_path)
    mgr._auth_failed = True
    mgr.log_text = "AUTH_FAILED"
    mgr.clear_logs_on_restart = True

    async def _alive():
        return True

    mgr._probe_tunnel_light = _alive  # type: ignore[method-assign]
    mgr._launch_auth_repair("auth_throttle")
    assert mgr._auth_repair_task is not None
    await asyncio.wait_for(mgr._auth_repair_task, timeout=2.0)
    assert mgr.calls["restart"] == 1
    assert mgr._auth_failed is False
    assert mgr._repair_state.repair_successes == 1
    assert mgr._status == vm.VPNState.CONNECTED
    vm._STATION_AUTH_GUARD.clear()


@pytest.mark.asyncio
async def test_repair_loop_escalates_to_real_stops_then_park(tmp_path):
    mgr = FakeVPNManager(_repair_cfg(tmp_path), tmp_path=tmp_path)
    mgr._auth_failed = True
    mgr.log_text = "AUTH_FAILED"
    mgr.clear_logs_on_restart = False
    actions: list[str] = []

    async def _not_alive():
        return False

    async def _stop_service():
        actions.append("stop")

    async def _ensure_container(force_recreate=None):
        actions.append("recreate" if force_recreate else "start")

    async def _pin_last_good():
        actions.append("pin")
        return False

    async def _apply_stack(*args, **kwargs):
        actions.append("stack")
        return False

    async def _fast_sleep(seconds):
        clock["t"] += float(seconds or 0.0)
        await asyncio.sleep(0)

    clock = {"t": mgr._now_fn()}
    mgr._now_fn = lambda: clock["t"]  # type: ignore[method-assign]
    mgr._probe_tunnel_light = _not_alive  # type: ignore[method-assign]
    mgr._stop_service = _stop_service  # type: ignore[method-assign]
    mgr._ensure_container = _ensure_container  # type: ignore[method-assign]
    mgr._pin_last_good = _pin_last_good  # type: ignore[method-assign]
    mgr._apply_stack = _apply_stack  # type: ignore[method-assign]
    mgr._repair_sleep = _fast_sleep  # type: ignore[method-assign]
    mgr._launch_auth_repair("auth_throttle")
    assert mgr._auth_repair_task is not None
    await asyncio.wait_for(mgr._auth_repair_task, timeout=3.0)
    # restart (fake), puis deux stops réels, pin, recreate, stack, park.
    assert actions.count("stop") >= 2
    assert "pin" in actions
    assert "recreate" in actions
    assert "stack" in actions
    assert mgr._auth_park_until > mgr._now_fn()
    assert mgr._repair_state.repair_failures >= 1
    vm._STATION_AUTH_GUARD.clear()
