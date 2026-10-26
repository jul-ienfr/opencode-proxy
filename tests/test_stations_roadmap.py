"""Tests des briques roadmap stations (health, fleet, quality, stacks)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from free.quality import QualityConfig, QualityRegistry, reset_quality_registry
from free.selector import StationSelector
from ops.fleet import FleetController
from ops.health import HealthPolicy, StatusDebouncer


def test_health_debouncer_suppresses_flap_and_counts_once():
    d = StatusDebouncer(
        policy=HealthPolicy(flap_hysteresis_s=45.0, flap_min_observations=2),
        stable="connected",
    )
    # Une seule observation ne suffit pas.
    assert d.observe("degraded", 100.0) == "connected"
    assert d.observe("degraded", 110.0) == "connected"
    # Assez de temps + 2 observations -> transition stable.
    assert d.observe("degraded", 150.0) == "degraded"
    assert d.flap_count(150.0) == 1
    # Retour connected immédiat (guérison non retardée).
    assert d.observe("connected", 151.0) == "connected"
    assert d.flap_count(151.0) == 2


def test_health_debouncer_disabled_is_legacy_immediate():
    d = StatusDebouncer(policy=HealthPolicy(flap_hysteresis_s=0.0), stable="connected")
    assert d.observe("error", 1.0) == "error"


class _FakeMgr:
    def __init__(self, sid: int):
        self._station = sid
        self._enabled = True
        self._desired_connected = True
        self.status = "disconnected"
        self.current_ip = None
        self.calls: list[str] = []

    async def connect(self):
        self.calls.append("connect")
        self.status = "connected"
        self.current_ip = f"10.0.0.{self._station}"

    async def disconnect(self):
        self.calls.append("disconnect")
        self.status = "disconnected"

    async def start(self):
        self.calls.append("start")

    async def stop(self):
        self.calls.append("stop")

    async def stop_container(self):
        self.calls.append("stop_container")

    async def restart(self):
        self.calls.append("restart")


@pytest.mark.asyncio
async def test_fleet_disconnect_is_persistent_and_all_stations():
    mgrs = [_FakeMgr(i) for i in (1, 2, 3)]
    fleet = FleetController(mgrs)
    res = await fleet.disconnect_all()
    assert res["ok"] is True
    assert all(m.status == "disconnected" for m in mgrs)
    assert all(m._desired_connected is False for m in mgrs)
    # Un refresh ne doit pas annuler l'état voulu.
    assert all(m.calls.count("disconnect") == 1 for m in mgrs)


@pytest.mark.asyncio
async def test_fleet_connect_all_and_heal():
    mgrs = [_FakeMgr(i) for i in (1, 2)]
    fleet = FleetController(mgrs)
    res = await fleet.connect_all()
    assert res["ok"] is True
    assert all(m.status == "connected" for m in mgrs)
    mgrs[1].status = "error"
    healed = await fleet.heal()
    assert healed["ok"] is True
    assert 2 in healed["healed"]
    assert mgrs[1].status == "connected"


def _station(sid: int, ip: str, used: int = 0):
    return SimpleNamespace(
        _station=sid,
        current_ip=ip,
        _quota_per_ip=500,
        _requests_this_ip=used,
        _server_load=None,
    )


def _cfg(**kw):
    base = {
        "enabled": True,
        "min_samples": 3,
        "exploration_pct": 0,
        "latency_floor_ms": 100,
        "latency_ceiling_ms": 2000,
    }
    base.update(kw)
    return QualityConfig(**base)


def test_quality_selector_prefers_fastest_station():
    reset_quality_registry()
    q = QualityRegistry(_cfg())
    for _ in range(5):
        q.observe(1, "fast", 120, 200)
        q.observe(2, "slow", 900, 200)
    sel = StationSelector(q, q.cfg, rng=lambda: 0.99)
    stations = [_station(1, "fast", used=10), _station(2, "slow", used=0)]
    assert sel.choose(stations)._station == 1
    assert [s._station for s in sel.rank(stations)] == [1, 2]


def test_quality_429_penalty_changes_choice():
    reset_quality_registry()
    q = QualityRegistry(_cfg())
    for _ in range(5):
        q.observe(1, "fast", 120, 200)
        q.observe(2, "slow", 800, 200)
    # Une rafale de 429 sur la rapide doit la faire chuter.
    for _ in range(5):
        q.observe(1, "fast", 150, 429)
    sel = StationSelector(q, q.cfg, rng=lambda: 0.99)
    stations = [_station(1, "fast"), _station(2, "slow")]
    assert sel.choose(stations)._station == 2


def test_selector_cold_start_is_deterministic_without_observations():
    """Avant la première probe, l'exploration ne doit pas changer l'ordre."""
    reset_quality_registry()
    q = QualityRegistry(_cfg())
    sel = StationSelector(q, q.cfg, rng=lambda: 0.0)  # forcerait ranked[1]
    stations = [_station(1, "a"), _station(2, "b")]
    assert sel.choose(stations)._station == 1


def test_non_streaming_duration_does_not_pollute_latency():
    """Une réponse non-streaming longue ne doit pas passer pour un tunnel lent.

    Non-régression s6 : duration_ms = temps total du completion ; seule la
    latence TTFB streaming (ou la probe) alimente l'EWMA/le score.
    """
    reset_quality_registry()
    q = QualityRegistry(_cfg())
    q.observe(1, "ip", 120, 200)  # TTFB streaming
    q.observe(1, "ip", 16000, 200, latency=False)  # completion long
    item = q.get(1, "ip")
    assert item.ewma_ms == pytest.approx(120)
    assert item.count == 2
    assert item.success == 2

    # Aucun TTFB streaming : la probe active pilote la latence.
    q.observe(2, "ip2", 15000, 200, latency=False)
    q.observe_probe(2, "ip2", 800, True)
    assert q.get(2, "ip2").latency_ms() == 800


def test_quality_registry_prunes_removed_station():
    reset_quality_registry()
    q = QualityRegistry(_cfg())
    q.observe(1, "a", 100, 200)
    q.observe(9, "b", 100, 200)
    assert q.prune_sids(3) == 1
    assert len(q._items) == 1


def test_pool_state_is_dirty_gated(tmp_path):
    from free.pool import FreeIPPool

    p = object.__new__(FreeIPPool)
    p._per = {
        1: {
            "request_count": 7,
            "ip_stats": {},
            "last_confirmed_ip": "1.2.3.4",
            "bad_until": None,
            "bad_cause": None,
        }
    }
    p._dirty_state = False
    p._last_pool_persist_at = 0.0
    out = tmp_path / "pool.json"
    p._pool_state_file = lambda: str(out)  # type: ignore[method-assign]
    p.save_pool_state()
    assert not out.exists(), "état propre : aucun flush inutile"
    p._dirty_state = True
    p._last_pool_persist_at = 0.0
    p.save_pool_state()
    assert out.exists(), "état dirty : flush immédiat"
    assert p._dirty_state is False


def test_sync_stack_env_preserves_mixed_stacks(tmp_path):
    from vpn.manager import VPNManager

    mgr = object.__new__(VPNManager)
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "VPN_TYPE_STATION1=wireguard\nSOME_SECRET=abc\n", encoding="utf-8"
    )
    mgr._compose_file_path = lambda: str(compose)  # type: ignore[method-assign]
    assert mgr._sync_stack_env({1: "wireguard", 2: "openvpn"}) is True
    text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert "VPN_TYPE_STATION1=wireguard" in text
    assert "VPN_TYPE_STATION2=openvpn" in text
    assert "SOME_SECRET=abc" in text
