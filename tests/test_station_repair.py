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


def test_egress_recovery_is_minutes_not_hours(tmp_path):
    mgr = FakeVPNManager(_repair_cfg(tmp_path), tmp_path=tmp_path)
    mgr._egress_state = "tunnel_down"
    now = mgr._now_fn()
    mgr._egress_dead_since = now - 10
    assert mgr._decide_watchdog_action("egress dead")[0] == "egress-restart"

    mgr._egress_dead_since = now - 70
    assert mgr._decide_watchdog_action("egress dead")[0] == "egress-flip"

    mgr._egress_flip_done = True
    mgr._egress_dead_since = now - 130
    assert mgr._decide_watchdog_action("egress dead")[0] == "egress-recreate"

    mgr._egress_recreate_done = True
    mgr._egress_dead_since = now - 250
    assert mgr._decide_watchdog_action("egress dead")[0] == "egress-park"


def test_egress_waits_only_bounded_auth_cooling(tmp_path):
    mgr = FakeVPNManager(_repair_cfg(tmp_path), tmp_path=tmp_path)
    mgr._egress_state = "tunnel_down"
    now = mgr._now_fn()
    mgr._egress_dead_since = now - 10
    mgr._auth_cool_until = now + 200.0
    assert mgr._decide_watchdog_action("egress dead")[0] == "auth-cooling"
    # À moins de 30 s de cooling, la récupération egress reprend.
    mgr._auth_cool_until = now + 20.0
    assert mgr._decide_watchdog_action("egress dead")[0] == "egress-restart"


def test_egress_success_unparks_egress_station(tmp_path):
    mgr = FakeVPNManager(_repair_cfg(tmp_path), tmp_path=tmp_path)
    now = mgr._now_fn()
    mgr._egress_parked = True
    mgr._parked_until = now + 1000.0
    mgr._repair_state.park(mgr._repair_policy, now, cause="egress")
    mgr._note_effective_ok()
    assert mgr._egress_parked is False
    assert mgr._parked_until == 0.0
    assert mgr._repair_state.parked_until == 0.0
    assert mgr._repair_state.action_required is False


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
async def test_stuck_connecting_is_cancelled_after_hard_deadline(tmp_path):
    mgr = FakeVPNManager(_repair_cfg(tmp_path, watchdog_interval=20), tmp_path=tmp_path)
    mgr._status = vm.VPNState.CONNECTING
    mgr._connect_started_at = mgr._now_fn() - 200.0
    await mgr._watchdog_tick()
    assert mgr._watchdog_last_action == "connect-timeout"
    assert mgr._status == vm.VPNState.ERROR
    assert mgr._egress_state == "tunnel_down"


@pytest.mark.asyncio
async def test_stuck_rotation_is_cancelled_after_hard_deadline(tmp_path):
    mgr = FakeVPNManager(_repair_cfg(tmp_path, watchdog_interval=20), tmp_path=tmp_path)
    mgr._rotation_task = asyncio.create_task(asyncio.sleep(60))
    mgr._rotation_started_at = mgr._now_fn() - 400.0
    try:
        await mgr._watchdog_tick()
        await asyncio.sleep(0)
        assert mgr._watchdog_last_action == "rotation-timeout"
        assert mgr._rotation_task.cancelled() or mgr._rotation_task.done()
    finally:
        if not mgr._rotation_task.done():
            mgr._rotation_task.cancel()


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
