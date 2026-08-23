"""test_apply_station_count.py — hot-reload stack sync (fix 19/08) + shape.

[fix 19/08] the 2-new-stations-on-openvpn bug reported by the operator:
``_apply_station_count`` upscale called ``m.start()`` (compose up) WITHOUT
first syncing the .env substitution keys, so a new station booted on the
compose default ``${VPN_TYPE_STATIONn:-openvpn}`` — OpenVPN under a running
WireGuard fleet.

Fix (locked here): the registry + pool converge FIRST, then
``managers[0]._apply_stack(effective)`` writes ``VPN_TYPE_STATION{1..N}``
before ANY ``start()`` — and the ``_apply_stack`` no-op path (stack already
effective) still re-syncs the .env while skipping the compose recreate.

Offline: ``vpn_manager.VPNManager`` is monkeypatched by a stub whose
``start()``/``stop()``/``stop_container()`` record; managers[0] is the REAL
``FakeVPNManager._apply_stack`` (fake docker) so the env write + prune are
exercised for real. Nothing touches the live system.
"""

import os

import pytest

import opencode
import shared_state
import vpn_manager
import dashboard.api as api
from test_vpn_freshness import _cfg
from test_vpn_stack_nstation import _Rec, _env_map, _seed_env


class _StubMgr:
    """Stand-in for the VPNManager instances the upscale/downscale branch
    creates (the real __init__/start would touch docker)."""

    created = []

    def __init__(self, config, station, shared=None):
        self._station = station
        self._compose_service = "vpn-gluetun" if station <= 1 else f"vpn-gluetun-{station}"
        self._docker_container = "opencode-vpn" if station <= 1 else f"opencode-vpn-{station}"
        self._stack_effective = "wireguard"
        self.started = False
        self.stopped = False
        self.stopped_container = False
        type(self).created.append(station)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def stop_container(self):
        self.stopped_container = True


class _FakePool:
    def __init__(self):
        self.stations = None
        self.cancelled_sids = None

    def set_stations(self, stations):
        self.stations = list(stations)

    async def cancel_rotations(self, sids):
        """[plan 18/08 §2.3] Record the retired sids handed to the downscale
        cancel step (the real pool cancels + awaits them)."""
        self.cancelled_sids = list(sids)


class _FakeWatcher:
    def __init__(self):
        self.managers = None

    def set_managers(self, managers):
        self.managers = dict(managers)


def _canvas(tmp_path, monkeypatch):
    """Real FakeVPNManager×2 (recorders) + harness fakes, wired into
    shared_state as a 2-station deployment. Assigned via monkeypatch so the
    fixture restores the prior globals at teardown — a bare assignment here
    would leak the stale pool/registry into every later suite test."""
    m1 = _Rec(_cfg(tmp_path, vpn_stack="auto"), tmp_path=tmp_path)
    m2 = _Rec(_cfg(tmp_path, vpn_stack="auto"), station=2, tmp_path=tmp_path)
    pool, watcher = _FakePool(), _FakeWatcher()
    monkeypatch.setattr(shared_state, "vpn_managers", [m1, m2], raising=False)
    monkeypatch.setattr(shared_state, "vpn_manager", m1, raising=False)
    monkeypatch.setattr(shared_state, "vpn_manager_2", m2, raising=False)
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)
    monkeypatch.setattr(shared_state, "docker_event_watcher", watcher, raising=False)
    return m1, m2, pool, watcher


@pytest.mark.asyncio
async def test_upscale_3_syncs_env_stack_before_any_start(tmp_path, monkeypatch):
    """Upscale 2→3 under a running WireGuard fleet: the new station must see
    VPN_TYPE_STATION3=wireguard in the .env BEFORE its first compose up.
    the existing STATION1/2 keys and the other .env entries are preserved,
    no compose recreate happens (stack already effective)."""
    (tmp_path / "wireguard.env").write_text("PRIVATE_KEY=x\n", encoding="utf-8")
    _seed_env(
        tmp_path, VPN_TYPE_STATION1="wireguard", VPN_TYPE_STATION2="wireguard", SOME_SECRET="abc123"
    )
    compose = tmp_path / "docker-compose.yml"
    monkeypatch.setenv("VPN_DOCKER_COMPOSE_FILE", str(compose))

    m1, m2, pool, watcher = _canvas(tmp_path, monkeypatch)
    assert m1._stack_effective == "wireguard"  # key present + auto stack

    monkeypatch.setattr(vpn_manager, "VPNManager", _StubMgr)
    _StubMgr.created = []
    persisted = []

    async def _fake_persist(u):
        persisted.append(dict(u))

    monkeypatch.setattr(api, "_persist_vpn_config", _fake_persist)

    await opencode._apply_station_count(3)

    assert _StubMgr.created == [3], "only station 3 is newly created"

    env = _env_map(tmp_path / ".env")
    for s in (1, 2, 3):
        assert env[f"VPN_TYPE_STATION{s}"] == "wireguard", (
            f"station {s} must join the running stack"
        )
    assert env["SOME_SECRET"] == "abc123", "non-station keys preserved"

    assert m1.cmds == [] and m2.cmds == [], "no compose recreate on a no-op stack sync"
    assert persisted[0].get("station_count") == 3
    assert "max_free_attempts" in persisted[0]
    assert [m._station for m in pool.stations] == [1, 2, 3]
    assert set(watcher.managers) == {"opencode-vpn", "opencode-vpn-2", "opencode-vpn-3"}
    assert shared_state.vpn_managers[2].started, "station 3 got its start()"


@pytest.mark.asyncio
async def test_downscale_3_to_2_stops_container_no_stack_call(tmp_path, monkeypatch):
    """Downscale 3→2: the retired station is stopped (state) then
    compose-stopped; the registry/pool/watcher shrink back to 2 and the
    config is persisted. No _apply_stack involved (no env rewrite needed)."""
    stubs = [
        _StubMgr(_cfg(tmp_path), station=1),
        _StubMgr(_cfg(tmp_path), station=2),
        _StubMgr(_cfg(tmp_path), station=3),
    ]
    pool, watcher = _FakePool(), _FakeWatcher()
    # monkeypatch (auto-restore) — never bare assign to shared_state.
    monkeypatch.setattr(shared_state, "vpn_managers", stubs, raising=False)
    monkeypatch.setattr(shared_state, "vpn_manager", stubs[0], raising=False)
    monkeypatch.setattr(shared_state, "vpn_manager_2", stubs[1], raising=False)
    monkeypatch.setattr(shared_state, "free_ip_pool", pool, raising=False)
    monkeypatch.setattr(shared_state, "docker_event_watcher", watcher, raising=False)
    persisted = []

    async def _fake_persist(u):
        persisted.append(dict(u))

    monkeypatch.setattr(api, "_persist_vpn_config", _fake_persist)

    await opencode._apply_station_count(2)

    assert pool.cancelled_sids == [3], "downscale cancels the retired rotations"
    assert stubs[2].stopped and stubs[2].stopped_container
    assert not stubs[0].stopped and not stubs[1].stopped
    assert [m._station for m in pool.stations] == [1, 2]
    assert set(watcher.managers) == {"opencode-vpn", "opencode-vpn-2"}
    assert persisted[0].get("station_count") == 2
    assert "max_free_attempts" in persisted[0]
    assert shared_state.vpn_managers == stubs[:2]
    assert shared_state.vpn_manager_2 is stubs[1]
