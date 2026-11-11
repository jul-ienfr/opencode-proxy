"""Tests for the 429-aware adaptive egress weights."""

from __future__ import annotations

from free.adaptive_weights import adaptive_weights, parse_config


def _sum(weights: dict[str, int]) -> int:
    return sum(int(value or 0) for value in weights.values())


def test_provider_without_samples_keeps_base_weight():
    base = {"warp": 45, "nordvpn": 50, "proxies": 5}
    out = adaptive_weights(base, {"warp": {"ok": 0, "n429": 0}})
    assert out["warp"] == 45
    assert _sum(out) == 100


def test_recent_429_reduces_the_provider_share():
    base = {"warp": 45, "nordvpn": 50, "proxies": 5}
    health = {
        "warp": {"ok": 10, "n429": 0, "recent_429": 0, "blocked_ratio": 0},
        "nordvpn": {"ok": 10, "n429": 0, "recent_429": 0, "blocked_ratio": 0},
        "proxies": {"ok": 0, "n429": 6, "recent_429": 3, "blocked_ratio": 0},
    }
    out = adaptive_weights(base, health)
    assert out["proxies"] < base["proxies"]
    assert _sum(out) == 100


def test_blocked_provider_is_zeroed():
    base = {"warp": 45, "nordvpn": 50, "proxies": 5}
    health = {
        "warp": {"ok": 10, "n429": 5, "recent_429": 0, "blocked_ratio": 1.0},
        "nordvpn": {"ok": 10, "n429": 0, "recent_429": 0, "blocked_ratio": 0},
    }
    out = adaptive_weights(base, health)
    assert out["warp"] == 0
    assert out["nordvpn"] > base["nordvpn"]
    assert _sum(out) == 100


def test_all_blocked_falls_back_to_base():
    base = {"warp": 45, "nordvpn": 50, "proxies": 5}
    health = {
        name: {"ok": 0, "n429": 10, "recent_429": 10, "blocked_ratio": 1.0}
        for name in base
    }
    assert adaptive_weights(base, health) == base


def test_parse_config_clamps_values():
    cfg = parse_config(
        {
            "enabled": 1,
            "window_s": 1,
            "min_samples": 0,
            "recent_429_penalty": 5,
            "min_weight_pct": 99,
        }
    )
    assert cfg["enabled"] is True
    assert cfg["window_s"] == 30.0
    assert cfg["min_samples"] == 1
    assert cfg["recent_429_penalty"] == 0.9
    assert cfg["min_weight_pct"] == 50
