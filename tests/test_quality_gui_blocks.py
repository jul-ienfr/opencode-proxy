"""Tests des options GUI qualité (validation, registre hostname, rotation anticipée)."""

from __future__ import annotations

from dashboard.api import _quality_blocks_errors
from free.quality import QualityConfig, QualityRegistry
from free.pool import FreeIPPool
from vpn.manager import VPNManager


def test_quality_blocks_validation_accepts_partial() -> None:
    assert _quality_blocks_errors({}) == []
    assert _quality_blocks_errors({"quality_rotation": {"enabled": True, "min_requests": 12}}) == []
    assert _quality_blocks_errors(
        {"quality_preselection": {"enabled": True, "fast_tps": 55, "slow_tps": 18}}
    ) == []


def test_quality_blocks_validation_rejects_bad_values() -> None:
    errs = _quality_blocks_errors({"quality_preselection": {"fast_tps": 20, "slow_tps": 30}})
    assert any("slow_tps" in e for e in errs)
    errs = _quality_blocks_errors({"quality_rotation": {"min_samples": 0}})
    assert any("min_samples" in e for e in errs)
    errs = _quality_blocks_errors({"quality_rotation": {"inconnu": 1}})
    assert any("non supporté" in e for e in errs)
    errs = _quality_blocks_errors({"post_rotation_probe": {"enabled": "oui"}})
    assert any("booléen" in e for e in errs)


def _cfg() -> QualityConfig:
    return QualityConfig(
        enabled=True,
        throughput_floor_tps=20.0,
        throughput_target_tps=45.0,
        slow_lane_min_samples=3,
        slow_lane_cooldown_s=600.0,
        ttfb_ceiling_ms=8000.0,
    )


def test_registry_attributes_quality_to_hostname() -> None:
    reg = QualityRegistry(_cfg())
    for _ in range(3):
        reg.observe_completion(
            1, "203.0.113.7", 1000.0, 80, server_host="nl1050.nordvpn.com"
        )
    q = reg.host_quality("nl1050.nordvpn.com")
    assert q is not None
    assert q["n"] == 3
    assert q["tps"] is not None and q["tps"] > 50
    assert q["slow"] is False
    assert reg.host_quality("autre.nordvpn.com") is None


def test_registry_detects_slow_lane_and_trend() -> None:
    reg = QualityRegistry(_cfg())
    for _ in range(3):
        reg.observe_completion(1, "198.51.100.9", 1000.0, 1, server_host="fr1331.nordvpn.com")
    assert reg.is_slow_lane(1, "198.51.100.9") is True
    q = reg.host_quality("fr1331.nordvpn.com")
    assert q is not None and q["slow"] is True

    # Tendance : 100 → 60 tok/s → ~40 % de baisse.
    reg2 = QualityRegistry(_cfg())
    for tps in (100, 100, 100, 80, 60):
        reg2.observe_completion(2, "192.0.2.44", 1000.0, tps)
    assert reg2.is_degrading(2, "192.0.2.44", 25.0) is True
    assert reg2.is_degrading(2, "192.0.2.44", 60.0) is False
    assert reg2.is_slow_lane(2, "192.0.2.44") is False


def test_manager_preselection_orders_fast_then_unknown_then_slow() -> None:
    m = VPNManager.__new__(VPNManager)
    m._least_loaded_enabled = True
    m._nord_loads_cache = (
        0.0,
        {"France": [("slow.nordvpn.com", 10), ("fast.nordvpn.com", 30), ("new.nordvpn.com", 5)]},
    )
    m._least_loaded_topk = 3
    m._config = {
        "quality_preselection": {
            "enabled": True,
            "fast_tps": 50.0,
            "slow_tps": 20.0,
            "max_load": 80,
            "tier_b_quota": 250,
            "tier_c_quota": 60,
        }
    }
    m._host_blacklisted = lambda _h: False  # type: ignore[method-assign]
    m._server_recent = lambda _h: False  # type: ignore[method-assign]
    qualities = {
        "fast.nordvpn.com": {"n": 5, "tps": 70.0, "slow": False},
        "slow.nordvpn.com": {"n": 5, "tps": 8.0, "slow": True},
    }
    m._host_quality_fn = lambda h: qualities.get(h)  # type: ignore[method-assign]
    assert m._least_loaded_hostnames("France") == [
        "fast.nordvpn.com",
        "new.nordvpn.com",
        "slow.nordvpn.com",
    ]


def test_pool_early_rotation_respects_min_requests_and_guard() -> None:
    pool = FreeIPPool.__new__(FreeIPPool)
    pool.quality = QualityRegistry(_cfg())
    pool._quality_rotation = {
        "enabled": True,
        "min_requests": 10,
        "min_samples": 3,
        "trend_pct": 25.0,
        "ttfb_ceiling_ms": 8000.0,
        "max_per_hour": 6,
        "global_slow_guard": 2,
    }
    pool._quality_rotations = []
    pool.latency_engine = None

    class Station:
        _station = 1
        _quota_per_ip = 500
        _current_ip = "198.51.100.9"
        _current_host = "fr1331.nordvpn.com"

    st = Station()
    pool._stations = [st]
    pool._per = {1: {"request_count": 9}}
    pool._station_usable = lambda *_a, **_k: True  # type: ignore[method-assign]
    launched: list = []
    pool._launch_rotation = lambda s: launched.append(s)  # type: ignore[method-assign]

    for _ in range(3):
        pool.quality.observe_completion(1, st._current_ip, 1000.0, 1)
    assert pool.quality.is_slow_lane(1, st._current_ip) is True

    pool.on_quality_completion(st, st._current_ip, success=True)
    assert launched == [], "min_requests=10 doit bloquer à 9"

    pool._per[1]["request_count"] = 10
    pool.on_quality_completion(st, st._current_ip, success=True)
    assert launched == [st]
    assert len(pool._quality_rotations) == 1

    # Garde flotte : 2 stations actives, les 2 lentes → pause.
    pool._stations = [st, type("S2", (), {"_station": 2, "_current_ip": "198.51.100.10", "_current_host": "x"})()]
    pool.quality.observe_completion(2, "198.51.100.10", 1000.0, 1)
    for _ in range(2):
        pool.quality.observe_completion(2, "198.51.100.10", 1000.0, 1)
    pool._launch_rotation = lambda s: launched.append(s)  # type: ignore[method-assign]
    before = len(launched)
    pool.on_quality_completion(st, st._current_ip, success=True)
    assert len(launched) == before, "flotte entière lente → aucune rotation"
