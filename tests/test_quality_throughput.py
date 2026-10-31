"""Qualité par débit (tok/s) et quarantaine slow lane.

Le TTFB seul ne distingue pas une IP rapide au premier octet d'une IP qui
génère à 1 tok/s : ces tests verrouillent la mesure de fin de flux et la
quarantaine qui en découle.
"""

from __future__ import annotations

import pytest

from free.quality import IpQuality, QualityConfig, QualityRegistry, reset_quality_registry
from free.selector import StationSelector


def _cfg(**kw) -> QualityConfig:
    base = {
        "enabled": True,
        "min_samples": 3,
        "exploration_pct": 0,
        "latency_floor_ms": 100,
        "latency_ceiling_ms": 2000,
        "throughput_floor_tps": 20.0,
        "throughput_target_tps": 45.0,
        "slow_lane_min_samples": 3,
        "slow_lane_cooldown_s": 600.0,
    }
    base.update(kw)
    return QualityConfig(**base)


class _Station:
    def __init__(self, sid: int, ip: str, used: int = 0) -> None:
        self._station = sid
        self.current_ip = ip
        self._quota_per_ip = 500
        self._requests_this_ip = used
        self._server_load = None


def test_fast_generation_scores_above_slow_at_equal_ttfb():
    now = 1000.0
    cfg = _cfg()
    fast = IpQuality(sid=1, ip="fast")
    slow = IpQuality(sid=2, ip="slow")
    for item in (fast, slow):
        item.observe(120.0, 200, now)
    fast.observe_completion(10_000.0, 500, now, cfg)
    slow.observe_completion(60_000.0, 500, now, cfg)

    assert fast.tps_ewma == pytest.approx(50.0)
    assert slow.tps_ewma == pytest.approx(8.333, rel=1e-3)
    assert fast.score(cfg, quota_used=0, quota_limit=500, server_load=None, now=now) > slow.score(
        cfg, quota_used=0, quota_limit=500, server_load=None, now=now
    )


def test_slow_lane_quarantine_after_min_samples_and_recovery():
    now = 2000.0
    cfg = _cfg(slow_lane_min_samples=3, slow_lane_cooldown_s=600.0)
    item = IpQuality(sid=1, ip="slow")
    for _ in range(3):
        item.observe(120.0, 200, now)
        item.observe_completion(60_000.0, 69, now, cfg)

    assert item.is_slow_lane(now)
    assert item.slow_lane_until == pytest.approx(now + 600.0)
    assert item.score(cfg, quota_used=0, quota_limit=500, server_load=None, now=now) == 0.0

    # Toujours quarantinée juste avant l'expiration.
    assert item.is_slow_lane(now + 599.0)
    # Après expiration, le score redevient exploitable.
    assert not item.is_slow_lane(now + 601.0)
    assert item.score(cfg, quota_used=0, quota_limit=500, server_load=None, now=now + 601.0) > 0.0

    # Deux mesures franchement rapides font remonter l'EWMA au-dessus du
    # plancher et lèvent la quarantaine (une seule ne suffit pas : α=0,3).
    item.observe_completion(10_000.0, 500, now + 601.0, cfg)
    item.observe_completion(10_000.0, 500, now + 602.0, cfg)
    assert item.slow_lane_until == 0.0


def test_high_ttfb_quarantines_even_with_fast_tps():
    """Un TTFB EWMA > seuil doit quarantiner même si la génération est rapide."""
    now = 3500.0
    cfg = _cfg(ttfb_ceiling_ms=5000.0, slow_lane_min_samples=3)
    item = IpQuality(sid=1, ip="slow-start")
    for _ in range(3):
        item.observe(9000.0, 200, now)
        item.observe_completion(10_000.0, 500, now, cfg)

    assert item.tps_ewma == pytest.approx(50.0)
    assert item.ttfb_n == 3
    assert item.is_slow_lane(now)

    # Récupération : TTFB redescendu sous 80 % du seuil + débit rapide.
    for _ in range(4):
        item.observe(1000.0, 200, now + 1)
        item.observe_completion(10_000.0, 500, now + 1, cfg)
    assert not item.is_slow_lane(now + 2)


def test_truncated_stream_still_counts_tokens_for_slow_lane():
    """Un flux coupé à 69 tokens en 60 s doit quarantiner l'IP, même en échec."""
    now = 3000.0
    cfg = _cfg(slow_lane_min_samples=1)
    item = IpQuality(sid=1, ip="cut")
    item.observe_completion(60_000.0, 69, now, cfg, success=False, stop_reason="error")
    assert item.tps_ewma == pytest.approx(1.15)
    assert item.is_slow_lane(now)
    assert item.last_stop_reason == "error"


def test_selector_avoids_slow_lane_station_at_equal_latency():
    reset_quality_registry()
    cfg = _cfg()
    q = QualityRegistry(cfg)
    now = 4000.0
    for _ in range(3):
        q.observe(1, "fast-ip", 120.0, 200)
        q.observe(2, "slow-ip", 120.0, 200)
        q.get(1, "fast-ip").observe_completion(10_000.0, 500, now, cfg)
        q.get(2, "slow-ip").observe_completion(60_000.0, 69, now, cfg)

    sel = StationSelector(q, cfg, rng=lambda: 0.99)
    stations = [_Station(1, "fast-ip"), _Station(2, "slow-ip")]
    assert sel.choose(stations)._station == 1
    assert [s._station for s in sel.rank(stations)] == [1, 2]


def test_least_inflight_penalty_prefers_idle_station():
    reset_quality_registry()
    cfg = _cfg(exploration_pct=0)
    q = QualityRegistry(cfg)
    now = 4600.0
    for sid in (1, 2):
        q.observe(sid, f"ip-{sid}", 120.0, 200)
        q.get(sid, f"ip-{sid}").observe_completion(10_000.0, 500, now, cfg)

    def _stats(st):
        return (0, 500, None, 6.0 if st._station == 1 else 0.0)

    sel = StationSelector(q, cfg, rng=lambda: 0.99, stats_provider=_stats)
    stations = [_Station(1, "ip-1"), _Station(2, "ip-2")]
    assert sel.choose(stations)._station == 2


def test_exploration_samples_cold_stations_not_only_second():
    """Avec 5 % d'exploration, les stations sans tok/s doivent être tirées
    au hasard — sinon ranked[1] est la seule à jamais sortir du froid."""
    reset_quality_registry()
    cfg = _cfg(exploration_pct=5.0)
    q = QualityRegistry(cfg)
    now = 4500.0
    for _ in range(3):
        q.observe(1, "fast-ip", 120.0, 200)
        q.get(1, "fast-ip").observe_completion(10_000.0, 500, now, cfg)

    draws = iter([0.0, 0.5])  # 0 % < 5 % → exploration ; 0,5 → 2e froid
    sel = StationSelector(q, cfg, rng=lambda: next(draws))
    stations = [_Station(1, "fast-ip"), _Station(2, "cold-2"), _Station(3, "cold-3")]
    chosen = sel.choose(stations)
    assert chosen._station in (2, 3)


def test_snapshot_exposes_throughput_and_slow_lane():
    now = 5000.0
    cfg = _cfg()
    item = IpQuality(sid=1, ip="ip")
    item.observe_completion(20_000.0, 500, now, cfg)
    snap = item.snapshot(cfg, now=now, quota_used=0, quota_limit=500, server_load=None)
    assert snap["tps_ewma"] == pytest.approx(25.0)
    assert snap["tps_n"] == 1
    assert snap["slow_lane"] is False
    assert snap["total_ewma_ms"] == pytest.approx(20_000.0)
