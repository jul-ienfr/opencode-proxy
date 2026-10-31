"""Régressions de l'audit watchdog du 30/10/2026.

Couvre les correctifs P1/P2 qui n'étaient pas verrouillés :
- seuil egress = grâce watchdog, pas auto_wg_egress_ticks ;
- budget de restart dédupliqué par tick et restart « plain » compté ;
- arrêt ordonné d'une rotation coopérative ;
- single-probe du breaker superviseur ;
- dernier recours quand tous les breakers superviseur sont ouverts.
"""

import asyncio
import time

from test_audit_routabilite_pc import _pc_cfg
from test_pool_connection_failure import _Station
from test_vpn_freshness import FakeVPNManager

import shared_state
from free_ip_pool import FreeIPPool
from ops.supervisor import StationSupervisor
from vpn.manager import _serialized_refresh


def test_egress_threshold_is_the_watchdog_grace_not_wg_auto_ticks(tmp_path):
    mgr = FakeVPNManager(
        _pc_cfg(tmp_path, auto_wg_egress_ticks=20, watchdog_egress_grace_ticks=6),
        tmp_path=tmp_path,
    )
    assert mgr._egress_dead_threshold() == 6
    # Un signal pool arme au moins le seuil de grâce (et le compteur WG).
    mgr._egress_failures = 0
    mgr.arm_egress_watchdog()
    assert mgr._egress_failures >= mgr._egress_dead_threshold()


def test_restart_budget_is_deduplicated_within_one_watchdog_tick(tmp_path):
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    mgr._watchdog_tick_active = True
    mgr._watchdog_tick_restart_counted = False
    mgr._note_watchdog_restart()
    mgr._note_watchdog_restart()
    assert len(mgr._watchdog_restarts_1h) == 1
    mgr._watchdog_tick_active = False
    mgr._note_watchdog_restart()
    assert len(mgr._watchdog_restarts_1h) == 2


def test_ensure_container_plain_restart_counts_budget(tmp_path):
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)
    mgr.container_present = True
    assert mgr._watchdog_restarts_1h == []
    asyncio.run(mgr._ensure_container(force_recreate=False))
    assert len(mgr._watchdog_restarts_1h) == 1


def test_stop_cancels_a_rotation_that_ignores_the_cooperative_flag(tmp_path):
    mgr = FakeVPNManager(_pc_cfg(tmp_path), tmp_path=tmp_path)

    async def _stuck_rotation():
        while True:
            await asyncio.sleep(0.01)

    async def _run():
        mgr._rotation_task = asyncio.create_task(_stuck_rotation())
        await mgr.stop()
        assert mgr._rotation_task is None or mgr._rotation_task.done()

    asyncio.run(_run())


def test_supervisor_breaker_allows_only_one_half_open_probe():
    sup = StationSupervisor(station=1, manager=None, breaker_threshold=2, breaker_cooldown_sec=60.0)
    sup.record_failure()
    sup.record_failure()
    assert sup.breaker_open() is True
    # Cooldown expiré : le premier appelant devient la sonde, le second est
    # refusé jusqu'au verdict.
    sup.breaker_open_until = time.monotonic() - 1.0
    assert sup.breaker_open() is False
    assert sup.breaker_open() is True
    sup.record_success()
    assert sup.breaker_open() is False
    assert sup.half_open_pending is False


def test_pool_keeps_a_last_resort_when_all_supervisor_breakers_are_open(monkeypatch):
    st1 = _Station(1, status="connected", current_ip="1.1.1.1")
    st2 = _Station(2, status="connected", current_ip="2.2.2.2")
    pool = FreeIPPool(st1, st2)

    class _OpenBreaker:
        station = 1

        def breaker_open(self):
            return True

        def breaker_open_readonly(self):
            return True

    class _OpenBreaker2(_OpenBreaker):
        station = 2

    monkeypatch.setattr(shared_state, "station_supervisors", [_OpenBreaker(), _OpenBreaker2()])
    chosen = pool._best_station()
    assert chosen in (st1, st2), "tous les breakers ouverts ne doivent pas rendre 0 candidat"


def test_force_refresh_waits_for_the_lock_instead_of_returning_cache():
    class _M:
        def __init__(self):
            self._refresh_lock = asyncio.Lock()
            self.calls = 0

        def get_status(self):
            return "cached"

        async def refresh(self, force=False):
            self.calls += 1
            return f"fresh:{force}"

    async def _run():
        m = _M()
        wrapped = _serialized_refresh(_M.refresh)
        async with m._refresh_lock:
            task = asyncio.create_task(wrapped(m, force=True))
            await asyncio.sleep(0.01)
            assert not task.done(), "force=True ne doit pas renvoyer le cache"
        assert await task == "fresh:True"
        assert m.calls == 1

    asyncio.run(_run())
