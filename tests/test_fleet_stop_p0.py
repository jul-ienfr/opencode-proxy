"""[audit 2026-10-30] stop_station doit exclure la station du pool et ne plus
laisser un statut "connected" avec un conteneur supprimé."""

import pytest

from ops.fleet import FleetController


class _Mgr:
    def __init__(self, sid, status="connected", ip="1.1.1.1"):
        self._station = sid
        self.enabled = True
        self._desired_connected = True
        self._disabled = False
        self._status = status
        self._current_ip = ip
        self._error = None
        self.stopped = False
        self.container_stopped = False
        self.started = False
        self.connected = False

    @property
    def status(self):
        return self._status

    @property
    def current_ip(self):
        return self._current_ip

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True
        self._desired_connected = False

    async def stop_container(self):
        self.container_stopped = True

    async def connect(self):
        self.connected = True
        self._status = "connected"

    async def disconnect(self):
        self._status = "disconnected"

    async def restart(self):
        pass


@pytest.mark.asyncio
async def test_stop_station_excludes_and_clears_state():
    m = _Mgr(2)
    ctl = FleetController([m])
    out = await ctl.stop_station(2)
    assert out["ok"] is True
    assert m.stopped and m.container_stopped
    assert m._disabled is True
    assert m.status == "disconnected"
    assert m.current_ip is None


@pytest.mark.asyncio
async def test_heal_and_connect_all_skip_disabled_station():
    m = _Mgr(3, status="disconnected")
    m._disabled = True
    ctl = FleetController([m])

    heal = await ctl.heal()
    assert heal["healed"] == []
    assert m.connected is False

    conn = await ctl.connect_all()
    assert conn["connected"] == 0
    assert m.connected is False


@pytest.mark.asyncio
async def test_explicit_start_reenables_disabled_station():
    m = _Mgr(4, status="disconnected")
    m._disabled = True
    ctl = FleetController([m])
    out = await ctl.start_station(4)
    assert out["ok"] is True
    assert m._disabled is False
    assert m.enabled is True
    assert m.connected is True
