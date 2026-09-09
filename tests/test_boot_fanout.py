"""test_boot_fanout.py — le fan-out stations ne bloque plus le lifespan.

Régression du chantier "vitesse max" : le gather des start() stations
(~90 s de stagger 30 s x N avec sem 2) tournait DANS le lifespan avant le
yield — le serveur HTTP ecoutait mais uvicorn ne servait qu'apres. Desormais
le lifespan cree une tache de fond `_boot_fanout` (fan-out stagger + calcul
boot_error P0-2) et rend la main : le port repond pendant que les stations
demarrent.

Offline : aucun docker, aucun reseau. On rejoue la sequence exacte du
lifespan (semaphore 2 + _start_one + sleep stagger + boot_error) avec des
fakes qui dorment 'stagger' secondes dans start() — comme
refresh_status/_startup_connect en prod — et on verifie :
  * le lifespan rend la main AVANT la fin des start() (non-bloquant) ;
  * tous les start() finissent quand meme (gather fail-soft conserve) ;
  * boot_error est None tant que le fan-out n'a pas fini (fail-open
    /api/vpn-status) puis correct (n/n -> None, k/n -> message CRITICAL) ;
  * le quinconce anti-rafale est conserve (jamais > 2 start() simultanes).
"""

import asyncio

import shared_state


class _FakeStation:
    _in_flight = 0
    _max_in_flight = 0

    def __init__(self, delay, station=1, fail=False):
        self._delay = delay
        self._station = station
        self._fail = fail
        self._status = "disconnected"
        self.enabled = True
        self.started = False

    async def start(self):
        type(self)._in_flight += 1
        type(self)._max_in_flight = max(type(self)._max_in_flight, type(self)._in_flight)
        try:
            await asyncio.sleep(self._delay)
            if self._fail:
                raise RuntimeError("compose up failed")
            self._status = "connected"
            self.started = True
        finally:
            type(self)._in_flight -= 1


def _reset():
    _FakeStation._in_flight = 0
    _FakeStation._max_in_flight = 0
    shared_state.boot_error = "stale-sentinel"
    shared_state.boot_fanout_task = None


async def _run_fanout_sequence(managers, stagger_s):
    """Rejoue la sequence lifespan : _start_one + gather + boot_error."""
    import opencode as oc

    _boot_sem = asyncio.Semaphore(2)

    async def _start_one(_m):
        async with _boot_sem:
            try:
                await _m.start()
            except Exception:
                pass
            await asyncio.sleep(oc._boot_stagger_s())

    async def _boot_fanout():
        results = await asyncio.gather(
            *(_start_one(m) for m in managers if m.enabled), return_exceptions=True
        )
        for _r in results:
            assert not isinstance(_r, Exception)
        n_expected = len(managers)
        connected = sum(1 for _m in managers if str(getattr(_m, "_status", "")) in ("connected", "degraded"))
        if connected < n_expected:
            shared_state.boot_error = f"boot {connected}/{n_expected}"
        else:
            shared_state.boot_error = None

    return asyncio.create_task(_boot_fanout())


async def test_fanout_does_not_block_lifespan(monkeypatch):
    """start() lents (2 s) + stagger 0 : le lifespan rend la main en < 1 s."""
    import opencode as oc

    _reset()
    monkeypatch.setitem(oc.IP_ROTATION, "boot_stagger_s", 0)
    managers = [_FakeStation(2.0, station=k) for k in range(1, 4)]

    t0 = asyncio.get_event_loop().time()
    task = await _run_fanout_sequence(managers, 0)
    elapsed_spawn = asyncio.get_event_loop().time() - t0
    assert elapsed_spawn < 1.0, f"fan-out bloquant: {elapsed_spawn:.2f}s"

    # boot_error fail-open tant que le fan-out tourne.
    assert shared_state.boot_error == "stale-sentinel"
    await asyncio.wait_for(task, timeout=10.0)
    assert shared_state.boot_error is None
    assert all(m.started for m in managers)


async def test_fanout_boot_error_partial_failure(monkeypatch):
    """1 station sur 3 echoue -> boot_error 'boot 2/3', gather ne leve pas."""
    import opencode as oc

    _reset()
    monkeypatch.setitem(oc.IP_ROTATION, "boot_stagger_s", 0)
    managers = [
        _FakeStation(0.05, station=1),
        _FakeStation(0.05, station=2, fail=True),
        _FakeStation(0.05, station=3),
    ]
    task = await _run_fanout_sequence(managers, 0)
    await asyncio.wait_for(task, timeout=10.0)
    assert shared_state.boot_error == "boot 2/3"


async def test_fanout_keeps_stagger_concurrency_cap(monkeypatch):
    """6 stations lentes : jamais plus de 2 start() simultanes (PC-4)."""
    import opencode as oc

    _reset()
    monkeypatch.setitem(oc.IP_ROTATION, "boot_stagger_s", 0)
    managers = [_FakeStation(0.2, station=k) for k in range(1, 7)]
    task = await _run_fanout_sequence(managers, 0)
    await asyncio.wait_for(task, timeout=15.0)
    assert _FakeStation._max_in_flight <= 2, _FakeStation._max_in_flight
    assert shared_state.boot_error is None


async def test_lifespan_source_spawns_fanout_task():
    """Garde-fou : le lifespan ne 'await' plus le gather des start()."""
    import inspect

    import opencode as oc

    src = inspect.getsource(oc.lifespan)
    assert "boot_fanout_task = asyncio.create_task(_boot_fanout())" in src
    # Le gather des start() vit DANS _boot_fanout (fonction imbriquee),
    # pas awaité au niveau lifespan : le lifespan rend la main des le
    # create_task. On l'etablit par l'indentation — le gather imbrique est
    # indente de 8+ espaces (corps de _boot_fanout), jamais 4 (lifespan).
    assert "\n    _gather_results = await asyncio.gather(" not in src
