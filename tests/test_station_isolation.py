"""[plan v10 §4 Lot 1] Filet mock pur — isolation par station + escape hatch.

Vérifie AVANT que le watchdog/rotation passent par les superviseurs (Lots
2/3) que : l'état isolé ne fuit pas entre stations, la sync registry→
superviseurs conserve l'état des stations persistantes, l'escape hatch
`supervisor.enabled=false` remet le chemin legacy à vide, et les fixes
14.3.7 (.bak), 14.3.12 (prune max-sid) se comportent comme spécifié.
"""

import json
from types import SimpleNamespace

import pytest

from station_supervisor import (
    StationSupervisor,
    build_supervisors,
    sync_supervisors,
)


def _fake_manager(sid: int):
    return SimpleNamespace(
        _station=sid,
        current_ip=f"1.2.3.{sid}",
        start=None,
        stop=None,
        get_status=lambda sid=sid: {"station": sid, "status": "connected"},
    )


# ── isolation : état par station ─────────────────────────────────────────


def test_state_isolated_between_stations():
    s1 = StationSupervisor(station=1, manager=_fake_manager(1))
    s2 = StationSupervisor(station=2, manager=_fake_manager(2))

    s1.record_failure()
    s1.record_failure()
    assert s1.consecutive_failures == 2
    assert s2.consecutive_failures == 0, "état isolé — pas de fuite inter-stations"

    assert s1.record_failure() is True, "3e échec ouvre le breaker st1"
    assert s1.breaker_open() is True
    assert s2.breaker_open() is False


def test_breaker_half_open_after_cooldown():
    s = StationSupervisor(
        station=1,
        manager=_fake_manager(1),
        breaker_threshold=2,
        breaker_cooldown_sec=60.0,
    )
    s.record_failure()
    s.record_failure()
    assert s.breaker_open() is True
    # cooldown écoulé → half-open (une seule sonde passera, garde Lot 3)
    s.breaker_open_until -= 61.0
    assert s.breaker_open() is False
    assert s.consecutive_failures == 1, "half-open repart à threshold-1"


def test_restart_serialized(monkeypatch):
    calls: list[str] = []

    async def fake_restart():
        calls.append("restart")

    m = _fake_manager(1)
    m.restart = fake_restart
    s = StationSupervisor(station=1, manager=m)
    s.restart_in_progress = True  # simule un restart déjà en vol
    import asyncio

    asyncio.run(s.restart("test"))  # doit être no-op
    assert calls == [], "restart concurrent no-op (garde anti-thundering)"


def test_warmup_reset_on_ip_finalized():
    s = StationSupervisor(station=1, manager=_fake_manager(1))
    tr = s.tracker_for("9.9.9.9")
    tr.record(12000, slow_threshold_ms=8000)
    tr.record(12000, slow_threshold_ms=8000)
    assert tr.consecutive_slow == 2
    s.on_ip_finalized("10.0.0.1")
    assert tr.consecutive_slow == 0, "warm-up v6 : reset après rotation"
    assert "10.0.0.1" in s.ip_latency, "slot pré-crée pour la nouvelle IP"


def test_tracker_bounded_memory():
    s = StationSupervisor(station=1, manager=_fake_manager(1))
    for i in range(40):
        s.tracker_for(f"10.0.{i}.1")
    assert len(s.ip_latency) <= 30, "borne mémoire §4 Lot 1"


def test_should_soft_rotate_frozen_false_in_lot1():
    s = StationSupervisor(station=1, manager=_fake_manager(1))
    tr = s.tracker_for(str(s.manager.current_ip))
    for _ in range(10):
        tr.record(20000, slow_threshold_ms=1000)
    assert (
        s.should_soft_rotate("glm", {"default": 4000, "p95": 5000}) is False
    ), "décision gelée Lot 1 — Lot 3 branchera les seuils"


# ── sync registry → superviseurs ────────────────────────────────────────


def test_build_and_sync_preserves_persistent_state():
    managers_v2 = [_fake_manager(1), _fake_manager(2)]
    sups = build_supervisors(managers_v2)
    sups[0].record_failure()

    # downscale → [st1] puis upscale → [st1, st3] : l'état de st1 survit
    keep = sync_supervisors(sups, managers_v2[:1])
    assert len(keep) == 1 and keep[0].consecutive_failures == 1

    managers_v3 = [_fake_manager(1), _fake_manager(3)]
    grown = sync_supervisors(keep, managers_v3)
    assert [s.station for s in grown] == [1, 3]
    assert grown[0].consecutive_failures == 1, "état conservé pour station persistante"
    assert grown[1].consecutive_failures == 0, "nouvelle station → état vierge"
    assert grown[0].manager is managers_v3[0], "manager re-lié au nouvel objet"


# ── escape hatch + helper opencode ───────────────────────────────────────


class _FakeShared:
    def __init__(self):
        self.vpn_managers = []
        self.station_supervisors = []


def test_sync_helper_escape_hatch_off(monkeypatch):
    import config.settings as st
    import opencode as oc

    data = dict(st._yaml_data)
    data["supervisor"] = {"enabled": False}
    monkeypatch.setattr(st, "_yaml_data", data)

    shared = _FakeShared()
    shared.vpn_managers = [_fake_manager(1), _fake_manager(2)]
    oc._sync_station_supervisors(shared)
    assert shared.station_supervisors == [], "escape hatch v6 : liste vide"


def test_sync_helper_enabled_aligns(monkeypatch):
    import config.settings as st
    import opencode as oc

    data = {k: v for k, v in st._yaml_data.items() if k != "supervisor"}
    monkeypatch.setattr(st, "_yaml_data", data)

    shared = _FakeShared()
    shared.vpn_managers = [_fake_manager(1)]
    oc._sync_station_supervisors(shared)
    assert len(shared.station_supervisors) == 1
    assert isinstance(shared.station_supervisors[0], StationSupervisor)


# ── fix 14.3.7 : load_state retombe sur .bak ─────────────────────────────


@pytest.mark.asyncio
async def test_load_state_falls_back_to_bak(tmp_path, monkeypatch):
    from vpn_manager import VPNManager

    cfg: dict = {}
    m = VPNManager(cfg, station=1)
    state_file = tmp_path / "vpn_state_1.json"
    bak_file = tmp_path / "vpn_state_1.json.bak"

    good = {
        "ip_history": ["1.1.1.1"],
        "total_switches": 7,
        "current_ip": "1.1.1.1",
    }
    bak_file.write_text(json.dumps(good), encoding="utf-8")
    state_file.write_text("{CORRUPTED", encoding="utf-8")

    monkeypatch.setattr(m, "_get_state_path", lambda: str(state_file))
    m.load_state()
    assert m._ip_history == ["1.1.1.1"], ".bak last-good chargé sur corruption"
    assert m._total_switches == 7


@pytest.mark.asyncio
async def test_load_state_no_bak_stays_fail_soft(tmp_path, monkeypatch):
    from vpn_manager import VPNManager

    cfg: dict = {}
    m = VPNManager(cfg, station=1)
    state_file = tmp_path / "vpn_state_1.json"
    state_file.write_text("{CORRUPTED", encoding="utf-8")  # pas de .bak
    monkeypatch.setattr(m, "_get_state_path", lambda: str(state_file))
    m.load_state()  # ne doit PAS lever (comportement fail-soft historique)


# ── fix 14.3.12 : prune_stations reçoit max(sid actifs) ─────────────────


@pytest.mark.asyncio
async def test_set_stations_prunes_by_max_active_sid(monkeypatch):
    import free_ip_pool as fp

    pruned_with: list[int] = []

    class FakeSR:
        def prune_stations(self, max_station: int) -> None:
            pruned_with.append(max_station)

    import shared_state as ss

    monkeypatch.setattr(ss, "shared_rotation", FakeSR(), raising=False)

    pool = object.__new__(fp.FreeIPPool)  # bypass __init__ (lourds)
    pool._per = {}
    pool._pending = set()
    pool._rotation_tasks = {}
    pool._stations = []
    pool._station_ids = set()
    pool.update_config = lambda *a, **k: None
    pool.set_stations([_fake_manager(1), _fake_manager(3)])
    assert pruned_with == [3], (
        "max(1,3)=3 et non count=2 — sinon prune_stations(2) tuerait la station 3 active"
    )
