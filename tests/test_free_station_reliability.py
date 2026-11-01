"""Intégration P1/P3 : le pool applique santé stricte, quota et capacité honnête."""

from __future__ import annotations

import time

import free.pool as fp


class _RelStation:
    def __init__(self, sid: int, ip: str, *, config: dict | None = None):
        self._station = sid
        self._config = config or {}
        self.current_ip = ip
        self.pid = None
        self._quota_per_ip = 500
        self.enabled = True
        self.proxy_mode = "vpn"
        self.status = "connected"
        self.current_server = None
        self._egress_state = "tunnel_up_full"
        self._last_effective_ok_at = time.monotonic()
        self.socks5_url = f"socks5://127.0.0.1:{1080 + sid}"

    def get_status(self) -> dict:
        return {"station": self._station, "status": self.status}

    def note_free_request(self) -> None:
        pass

    def arm_egress_watchdog(self) -> None:
        pass


def _cfg(**over):
    cfg = {
        "ip_quota": {"enabled": True, "quota_reserve_pct": 10, "rotation_threshold_pct": 20},
        "station_health": {"strict": True, "max_effective_age_s": 60},
        "rotation_planner": {"enabled": True, "spare_ready": False},
        "quota_per_ip_by_model": {"m": 20},
        "quota_per_ip": 500,
        "bad_ip_ttl_s": 1800,
        "quota_ip_block_s": 120,
        "station_bad_ttl_s": 60,
        "station_connect_retry_interval_s": 300,
    }
    cfg.update(over)
    return cfg


def _pool():
    cfg = _cfg()
    st1 = _RelStation(1, "1.1.1.1", config=cfg)
    st2 = _RelStation(2, "2.2.2.2", config=cfg)
    return fp.FreeIPPool(st1, st2), st1, st2


def test_dynamic_rotation_threshold_uses_profile_and_percent():
    pool, st1, _ = _pool()
    # profil 20, réserve 2, seuil de headroom 4 => rotation à 16 requêtes.
    assert pool._rotation_threshold(st1, "m") == 16


def test_model_usable_rejects_draining_ip_and_selector_picks_fresh_one():
    pool, st1, st2 = _pool()
    assert pool.model_usable("m", st1)[0] is True
    for _ in range(17):
        pool._note_model_request("m", st1)
    assert pool.model_usable("m", st1)[0] is False
    chosen = pool._best_station(model="m")
    assert chosen is st2
    assert pool._last_selection is not None
    assert pool._last_selection.station is st2


def test_strict_health_rejects_auth_failed_and_stale_egress():
    pool, st1, st2 = _pool()
    st1._auth_failed = True
    assert pool.station_usable(st1) is False
    assert pool.model_usable("m", st1)[0] is False
    st1._auth_failed = False
    st1._last_effective_ok_at = time.monotonic() - 600.0
    assert pool.station_usable(st1) is False
    # st2 reste sain.
    assert pool.station_usable(st2) is True


def test_pinned_station_skips_quota_rotation():
    """A pinned hostname cannot change IP by restarting: no rotation loop."""
    pool, st1, _ = _pool()
    calls = []
    pool._launch_rotation = lambda station, **kw: calls.append(station)
    st1._pinned_hostname = "al41.nordvpn.com"

    pool.on_quota_exhausted(st1, quota=True)

    assert calls == []
    st1._pinned_hostname = None
    pool.on_quota_exhausted(st1, quota=True)
    assert calls == [st1]


def test_recent_health_probe_keeps_an_idle_station_fresh():
    """Une sonde egress réussie vaut preuve: pas de faux `egress-stale`.

    Le watchdog peut espacer ses ticks au-delà de l'ancien seuil de 60 s ;
    sans `health.last_ok_at`, une station saine était rotationnée.
    """
    pool, st1, _ = _pool()
    st1._last_effective_ok_at = time.monotonic() - 600.0
    pool._station_health.observe(1, True, now=time.monotonic())
    assert pool.station_usable(st1) is True
    assert pool._max_effective_age_s >= 180.0


def test_mark_station_ok_recovers_a_probing_station():
    """Un 200 réel doit rendre la station routable immédiatement.

    Régression : ``mark_station_ok`` nourrissait les bons probes AVANT
    ``repair_succeeded()``, qui remettait ``good_probes=0`` et l'état à
    ``probing`` — la station restait exclue malgré son tunnel sain.
    """
    pool, st1, _ = _pool()
    health = pool._station_health.get(1)
    health.repair_succeeded()
    assert health.state == "probing"

    pool.mark_station_ok(st1)

    assert health.state == "usable"
    assert health.good_probes >= 2
    assert pool.station_usable(st1) is True
    assert pool.model_usable("m", st1)[0] is True


def test_suspect_station_remains_usable_but_ranked_degraded():
    """Un probe raté (SUSPECT) ne doit pas faire tomber le pool à 1/7 :
    la station reste servable en dernier recours, derrière les saines."""
    pool, st1, st2 = _pool()
    health = pool._station_health.get(1)
    assert health is not None
    health.observe_probe(False, now=time.monotonic())
    assert pool.station_usable(st1) is True
    assert pool.station_usable(st2) is True
    assert pool.usable_station_count() == 2
    assert pool._degraded_rank(st1) == 1
    assert pool._degraded_rank(st2) == 0
    assert pool._prefer_healthy([st1, st2]) == [st2]


def test_usable_station_count_tracks_real_routability_only():
    """Le gate de warmup doit voir 1 station dès qu'elle est routable, et 0
    quand aucun tunnel ne l'est (auth failed / IP absente / down)."""
    pool, st1, st2 = _pool()
    assert pool.usable_station_count() == 2
    assert pool.has_usable_station() is True

    st1._auth_failed = True
    assert pool.usable_station_count() == 1

    st2._auth_failed = True
    assert pool.usable_station_count() == 0
    assert pool.has_usable_station() is False

    st2._auth_failed = False
    st2.status = "disconnected"
    assert pool.has_usable_station() is False


def test_capacity_snapshot_counts_usable_and_quota():
    pool, st1, st2 = _pool()
    cap = pool.capacity_snapshot()
    assert cap["configured_stations"] == 2
    assert cap["usable_stations"] == 2
    st1._auth_failed = True
    cap = pool.capacity_snapshot()
    assert cap["usable_stations"] == 1
    assert cap["capacity_degraded"] is True
    assert "1/2" in str(cap["capacity_reason"])


def test_spare_station_is_not_selected_until_promoted_and_capacity_excludes_it():
    cfg = _cfg(rotation_planner={"enabled": True, "spare_station": 3})
    st1 = _RelStation(1, "1.1.1.1", config=cfg)
    st2 = _RelStation(2, "2.2.2.2", config=cfg)
    st3 = _RelStation(3, "3.3.3.3", config=cfg)
    pool = fp.FreeIPPool(st1, st2)
    pool.set_stations([st1, st2, st3])
    assert pool._best_station(model="m") in (st1, st2)
    cap = pool.capacity_snapshot()
    assert cap["configured_stations"] == 2
    assert cap["stations_total"] == 3
    assert cap["spare_station"] == 3
    assert cap["warm_spare_ready"] is True
    # Spare non qualifié : plus de spare chaud.
    st3._auth_failed = True
    assert pool._spare_qualified("m") is False
    assert pool.capacity_snapshot()["warm_spare_ready"] is False
    # Promotion explicite : le spare devient sélectionnable.
    st3._auth_failed = False
    pool._spare_promoted = True
    pool._draining_stations.add(1)
    chosen = pool._best_station(model="m")
    assert chosen in (st2, st3)


def test_duplicate_ip_is_degraded_and_one_station_is_drained():
    cfg = _cfg(rotation_planner={"enabled": True, "spare_station": 0})
    st1 = _RelStation(1, "same-ip", config=cfg)
    st2 = _RelStation(2, "same-ip", config=cfg)
    st3 = _RelStation(3, "other-ip", config=cfg)
    pool = fp.FreeIPPool(st1, st2)
    pool.set_stations([st1, st2, st3])
    cap = pool.capacity_snapshot()
    assert cap["duplicate_stations"] == 1
    assert cap["capacity_degraded"] is True
    assert "partagent une IP" in str(cap["capacity_reason"])
    launched = []
    pool._launch_rotation = lambda station, **kw: launched.append(station)  # type: ignore[method-assign]
    pool._last_model = "m"
    pool._plan_proactive_rotation("m")
    assert len(launched) == 1
    assert len(pool._draining_stations) == 1
    # La station conservée n'est pas dans le drain.
    assert 1 not in pool._draining_stations and 2 in pool._draining_stations


def test_spare_is_not_warm_if_it_shares_an_active_ip():
    cfg = _cfg(rotation_planner={"enabled": True, "spare_station": 3})
    st1 = _RelStation(1, "1.1.1.1", config=cfg)
    st2 = _RelStation(2, "2.2.2.2", config=cfg)
    st3 = _RelStation(3, "1.1.1.1", config=cfg)
    pool = fp.FreeIPPool(st1, st2)
    pool.set_stations([st1, st2, st3])
    assert pool._spare_qualified("m") is False
    assert pool.capacity_snapshot()["warm_spare_ready"] is False


def test_get_status_exposes_reliability_instrumentation():
    pool, st1, _ = _pool()
    pool._last_model = "m"
    status = pool.get_status()
    assert status["configured_stations"] == 2
    assert status["usable_stations"] == 2
    assert status["active_stations_with_quota"] == 2
    assert status["quota_profile"]["reserve_pct"] == 10.0
    assert status["quota_profile"]["rotation_pct"] == 20.0
    assert status["stations"][0]["station_usable"] is True
    assert status["stations"][0]["model_usable"] is True
    assert status["stations"][0]["ip_state"] in ("qualified", "active")
    assert status["stations"][0]["quota_headroom"] == 20
