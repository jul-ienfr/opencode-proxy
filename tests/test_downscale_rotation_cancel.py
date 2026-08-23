"""test_downscale_rotation_cancel.py — [plan 18/08 §2.3] downscale annule
les rotations en vol des stations retirées.

Un downscale (dropdown 5→3) retire des managers du registre et appelle
``stop_container()`` (compose stop + ``docker rm -f`` [fix 19/08]). Si une
rotation de fond tourne sur une station retirée, elle doit être ANNULÉE
avant — sinon elle se termine sur un conteneur en cours de suppression (ou
le relance). Trois volets :

  * ``cancel_rotations(sids)`` : cancel + await (cap 5 s) des rotations en
    vol ; le WORKER survit (il attrape CancelledError et continue de
    drainer la file — il ne doit jamais mourir, sinon les rotations de la
    flotte restante s'arrêtent) ; ids inconnues = ignorées ; rotation
    coincée dans to_thread = best-effort au cap — et [Revue 19/08] le sid
    est retiré EARLY du pool (guards fermés immédiatement) même quand la
    tâche ne peut pas mourir.
  * ``_launch_rotation`` refuse les stations retirées (un handler qui tient
    un manager retiré — 429 arrivé en plein swap — ne doit pas le re-queuer).
  * ``set_stations`` rafraîchit ``_station_ids`` et sweep les registrations
    périmées (belt-and-braces).

End-to-end : ``_apply_station_count`` (opencode.py) downscale appelle
cancel_rotations AVANT stop()/stop_container(), avec un pool réel et une
rotation en vol bloquée.
"""
import asyncio
import time

import pytest

import free_ip_pool as fip
import shared_state
from test_vpn_freshness import FakeVPNManager, _cfg


def _pool(tmp_path, n):
    """n fake managers wired into a real FreeIPPool (stations 1..n)."""
    ms = [FakeVPNManager(_cfg(tmp_path), station=s, tmp_path=tmp_path)
          for s in range(1, n + 1)]
    pool = fip.FreeIPPool(ms[0], ms[1] if n >= 2 else None)
    pool.set_stations(ms)
    return pool, ms


async def _wait_registered(pool, sid, timeout=2.0):
    """Poll until the pool holds an in-flight rotation registration for sid."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        t = pool._rotation_tasks.get(sid)
        if t is not None and not t.done():
            return t
        await asyncio.sleep(0.02)
    raise AssertionError(f"no in-flight rotation registered for station {sid}")


def _blocking_switch(pool, gate, switched, cancelled_at):
    """Install a switch_ip that blocks on `gate` and RECORDS a CancelledError.

    The recorded list is the honest "the rotation saw the cancellation"
    signal: the worker swallows it by design (and uncancels in 3.12), so
    task.cancelled()/cancelling() flags are NOT reliable after the swallow —
    the behavioral contract is: registration popped + switch never
    completed + a CancelledError was delivered at the blocking await."""
    async def fake_switch_ip(station):
        switched.append(station._station)
        try:
            await gate.wait()
        except asyncio.CancelledError:
            cancelled_at.append(station._station)
            raise
    pool.switch_ip = fake_switch_ip


# ── cancel_rotations ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_rotations_cancels_and_awaits_unwind(tmp_path):
    """A rotation blocked inside switch_ip is cancelled and its
    registration pops (the finally in _rotate_station) — the await returns
    as soon as the rotation has unwound, without waiting for the worker."""
    pool, ms = _pool(tmp_path, 2)
    gate = asyncio.Event()
    switched = []
    cancelled_at = []

    _blocking_switch(pool, gate, switched, cancelled_at)
    pool._launch_rotation(ms[1])        # station 2 rotation
    await _wait_registered(pool, 2)

    await pool.cancel_rotations([2])

    assert cancelled_at == [2], "CancelledError delivered at the blocking await"
    assert 2 not in pool._rotation_tasks, "registration popped by finally"
    assert switched == [2]
    gate.set()                          # release (already unwound, harmless)


@pytest.mark.asyncio
async def test_cancel_rotations_leaves_other_stations_alone(tmp_path):
    """Downscale removing station 3 must not touch the in-flight rotation
    of station 2 (a concurrent rotation in the bounded pool)."""
    pool, ms = _pool(tmp_path, 3)
    gate = asyncio.Event()
    switched = []
    cancelled_at = []

    _blocking_switch(pool, gate, switched, cancelled_at)
    pool._launch_rotation(ms[1])        # station 2 rotation
    pool._launch_rotation(ms[2])        # station 3 rotation
    await _wait_registered(pool, 2)
    await _wait_registered(pool, 3)

    await pool.cancel_rotations([3])

    assert cancelled_at == [3], "only station 3's rotation saw the cancel"
    assert 2 in pool._rotation_tasks
    # worker still alive and station 2's rotation can proceed to completion
    gate.set()
    deadline = time.monotonic() + 2.0
    while 2 in pool._rotation_tasks and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert 2 not in pool._rotation_tasks, "station 2 rotation completed"


@pytest.mark.asyncio
async def test_cancel_rotations_unknown_and_empty_noop(tmp_path):
    """Unknown sids, done tasks, and no sids → fast no-op, no error."""
    pool, ms = _pool(tmp_path, 1)
    await pool.cancel_rotations([])
    await pool.cancel_rotations([99, 42])
    # done task is skipped
    t = asyncio.create_task(asyncio.sleep(0))
    await t
    pool._rotation_tasks[1] = t
    await pool.cancel_rotations([1])    # must not cancel a done task


@pytest.mark.asyncio
async def test_cancel_rotations_stuck_rotation_caps_at_deadline(
        tmp_path, monkeypatch):
    """A rotation that SWALLOWS the cancellation (thread-like, stuck inside
    to_thread) cannot be killed: cancel_rotations polls its registration,
    caps at 5 s (patched to ~0 here) and returns best-effort — the
    downscale must proceed."""
    pool, ms = _pool(tmp_path, 1)
    gate = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def stuck():
        try:
            await gate.wait()
        except asyncio.CancelledError:
            calls.append("swallowed")
            while not release.is_set():
                await asyncio.sleep(0.01)

    t = asyncio.create_task(stuck())
    await asyncio.sleep(0)              # let it start and block on gate.wait()
    pool._rotation_tasks[1] = t
    # fast-forward the deadline: the 5 s cap must not be paid in a test
    real_mono = time.monotonic
    n = {"calls": 0}

    def fake_monotonic():
        n["calls"] += 1
        if n["calls"] >= 3:
            return real_mono() + 10.0     # past the deadline on iteration 3
        return real_mono()

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    await pool.cancel_rotations([1])      # returns, no exception

    assert calls == ["swallowed"]
    # [Revue 19/08 F1a] Best-effort au cap = la tâche coincée survit (un
    # thread n'est pas tuable) MAIS le sid est retiré EARLY — les guards
    # du pool ferment pendant la fenêtre stop()/stop_container(), et tout
    # travail docker ultérieur sur la station retirée est refusé.
    assert 1 not in pool._rotation_tasks, "registration prunée (retire eager)"
    assert 1 not in pool._per, "état per-station pruné"
    assert 1 not in pool._station_ids, "sid retiré"
    assert pool._stations == [], "station retirée de l'ensemble actif"
    assert not t.done(), "tâche coincée toujours vivante (thread non tuable)"
    pool._launch_rotation(ms[0])          # tout handler 429 tardif est refusé
    assert pool._pending == set(), "rien re-queué"
    assert pool._rotation_queue.empty(), "file vierge"
    release.set()
    await t


# ── worker survival ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_worker_survives_cancelled_rotation_and_drains(tmp_path):
    """The core of §2.3: cancelling a rotation cancels the WORKER's current
    _rotate_station call, but the worker catches CancelledError and keeps
    draining the queue — a subsequent rotation on another station still
    runs (no dead-worker stall until the next _ensure_workers)."""
    pool, ms = _pool(tmp_path, 2)
    gate = asyncio.Event()
    switched = []
    cancelled_at = []

    _blocking_switch(pool, gate, switched, cancelled_at)
    pool._launch_rotation(ms[0])        # station 1 rotation, blocks
    await _wait_registered(pool, 1)

    await pool.cancel_rotations([1])
    assert cancelled_at == [1], "CancelledError delivered to station 1"

    worker = pool._worker_tasks[0]
    assert not worker.done(), "worker survived the cancelled rotation"

    # station 2 must still rotate normally: release the gate so its
    # switch_ip passes straight through, then verify it completes. The
    # registration may be too brief to observe (gate already set → the
    # rotation runs start-to-finish within one loop turn), so poll the
    # behavioral completion signal instead: switch ran + registration
    # popped.
    gate.set()
    pool._launch_rotation(ms[1])
    deadline = time.monotonic() + 2.0
    while switched != [1, 2] and time.monotonic() < deadline:
        await asyncio.sleep(0.02)
    assert switched == [1, 2], "station 2 rotation ran to completion"
    assert 2 not in pool._rotation_tasks, "station 2 registration popped"


# ── _launch_rotation / set_stations guards ───────────────────────

@pytest.mark.asyncio
async def test_launch_rotation_ignores_retired_station(tmp_path):
    """A request handler holding a RETIRED manager (429 arrived mid-swap)
    must not re-queue it: no pending entry, no queue item."""
    pool, ms = _pool(tmp_path, 2)
    retired = FakeVPNManager(_cfg(tmp_path), station=5, tmp_path=tmp_path)

    pool._launch_rotation(retired)

    assert pool._pending == set(), "retired station not queued"
    assert pool._rotation_queue.empty()


@pytest.mark.asyncio
async def test_set_stations_refreshes_ids_and_sweeps_stale(tmp_path):
    """set_stations adopts the new set in _station_ids and sweeps stale
    registrations (belt-and-braces for entries that outlived the
    removed-set computation)."""
    pool, ms = _pool(tmp_path, 2)
    done_task = asyncio.create_task(asyncio.sleep(0))
    await done_task
    pool._pending.add(3)                        # stale queue entry
    pool._rotation_tasks[3] = done_task         # stale in-flight entry
    pool._rotation_tasks[99] = done_task

    pool.set_stations([ms[0]])

    assert pool._station_ids == {1}
    assert pool._pending == set()
    assert set(pool._rotation_tasks) == set()
    # and the known station's registration survives
    pool._rotation_tasks[1] = done_task
    pool.set_stations([ms[0]])
    assert pool._rotation_tasks.get(1) is done_task


# ── end-to-end: _apply_station_count downscale ───────────────────

@pytest.mark.asyncio
async def test_apply_station_count_downscale_cancels_before_stop(
        tmp_path, monkeypatch):
    """Full wiring: the downscale branch of _apply_station_count cancels
    the retired station's in-flight rotation BEFORE stop()/stop_container()
    — no docker op of the rotation lands after the cancel, the registry /
    pool / watcher converge, and config.yaml persists the new count."""
    import opencode
    import dashboard.api as dapi

    ms = [FakeVPNManager(_cfg(tmp_path), station=s, tmp_path=tmp_path)
          for s in range(1, 4)]
    pool = fip.FreeIPPool(ms[0], ms[1])
    pool.set_stations(ms)
    gate = asyncio.Event()
    switched = []
    cancelled_at = []

    _blocking_switch(pool, gate, switched, cancelled_at)
    pool._launch_rotation(ms[2])        # station 3 rotation in flight
    await _wait_registered(pool, 3)

    class _Watcher:
        def __init__(self):
            self.managers = None

        def set_managers(self, d):
            self.managers = d

    watcher = _Watcher()
    persisted = {}

    async def fake_persist(cfg):
        persisted.update(cfg)

    monkeypatch.setattr(shared_state, "vpn_managers", ms, raising=False)
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)
    monkeypatch.setattr(shared_state, "docker_event_watcher", watcher,
                        raising=False)
    monkeypatch.setattr(dapi, "_persist_vpn_config", fake_persist)

    await opencode._apply_station_count(2)

    # rotation cancelled and unwound BEFORE any container op
    assert cancelled_at == [3], "station 3's rotation saw the cancel"
    assert 3 not in pool._rotation_tasks
    assert switched == [3], "switch_ip never completed"
    # station 3's container was then removed (compose stop + docker rm -f)
    assert ms[2].calls.get("docker_run", 0) == 2
    # registry / pool / watcher converge on 1-2
    assert [m._station for m in shared_state.vpn_managers] == [1, 2]
    assert pool._station_ids == {1, 2}
    assert set(watcher.managers) == {"opencode-vpn", "opencode-vpn-2"}
    assert persisted.get("station_count") == 2
    assert "max_free_attempts" in persisted
    gate.set()                          # release (already unwound, harmless)
