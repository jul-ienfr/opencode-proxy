"""P6 : canary déterministe de la couche fiabilité."""

from __future__ import annotations

from free.reliability_canary import CanaryConfig, CanaryRouter


def test_canary_disabled_keeps_legacy():
    router = CanaryRouter(CanaryConfig.from_config({"reliability_canary": {"enabled": False}}))
    assert all(router.use_new(i) is False for i in range(200))
    assert router.new_count == 0
    assert router.legacy_count == 200


def test_canary_is_deterministic_per_key():
    cfg = CanaryConfig.from_config(
        {"reliability_canary": {"enabled": True, "percent": 5, "shadow": False, "salt": "s"}}
    )
    a = CanaryRouter(cfg)
    b = CanaryRouter(cfg)
    for key in ("m:1", "m:2", "m:3", "m:4"):
        assert a.bucket(key) == b.bucket(key)
        assert a.use_new(key) == b.use_new(key)


def test_canary_five_percent_is_bounded():
    cfg = CanaryConfig.from_config(
        {"reliability_canary": {"enabled": True, "percent": 5, "shadow": False}}
    )
    router = CanaryRouter(cfg)
    chosen = sum(1 for i in range(10_000) if router.use_new(i))
    assert 300 <= chosen <= 700, f"canary 5% hors bornes: {chosen}"
    assert router.new_count == chosen
    snap = router.snapshot()
    assert snap["enabled"] is True
    assert snap["percent"] == 5
    assert snap["total"] == 10_000


def test_canary_shadow_flag_is_reported():
    cfg = CanaryConfig.from_config({"reliability_canary": {"enabled": True, "percent": 5, "shadow": True}})
    router = CanaryRouter(cfg)
    router.use_new(1)
    assert router.snapshot()["shadow"] is True
