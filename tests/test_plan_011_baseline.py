"""Plan 011 â€” tests de baseline et de gaps (sans Docker).

Ce fichier est volontairement un mÃ©lange de :

- tests de caractÃ©risation : ils dÃ©crivent le comportement ACTUEL et passent ;
- tests de gap marquÃ©s ``xfail(strict=True)`` : ils dÃ©crivent le comportement
  ATTENDU par le plan 011 et Ã©chouent tant que le correctif n'est pas livrÃ©.

``strict=True`` est important : quand un gap est corrigÃ©, le test passe
(XPASS) et la suite Ã©choue, ce qui force Ã  retirer le marqueur et Ã  mettre le
plan Ã  jour. C'est le mÃ©canisme de Â« tests pour mettre Ã  jour le plan Â».

Aucun Docker, aucun rÃ©seau : ces tests doivent tourner dans le gate par dÃ©faut.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Baseline : ce que la configuration dÃ©clare rÃ©ellement
# ---------------------------------------------------------------------------


def _config() -> dict:
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}


def test_baseline_warp_is_primary_and_nordvpn_is_fallback() -> None:
    ip_rotation = _config().get("ip_rotation") or {}
    # [011] WARP reste primaire ; NordVPN est conservé en secours pour que le
    # client desktop ne tombe pas à 0/6 quand un bucket WARP est épuisé.
    assert ip_rotation.get("egress_providers") == ["warp", "nordvpn"]
    assert ip_rotation.get("station_count") == 6
    assert ip_rotation.get("repair_owner") == "supervisor"
    assert (ip_rotation.get("station_repair") or {}).get("enabled") is False


def test_baseline_six_nordvpn_services_are_declared_in_compose() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose.get("services") or {}
    for station in range(1, 7):
        name = "vpn-gluetun" if station == 1 else f"vpn-gluetun-{station}"
        assert name in services, f"{name} absent du compose"


def test_baseline_warp_config_blocks_are_statically_unique() -> None:
    """La config statique est unique ; le doublon live vient d'ailleurs.

    C'est prÃ©cisÃ©ment le gap : aucun test statique ne peut voir que deux
    conteneurs ont dÃ©rivÃ© vers le mÃªme /64 aprÃ¨s un redÃ©marrage.
    """
    rows = [
        row
        for row in (_config().get("ip_rotation") or {}).get("socks5_proxies") or []
        if str(row.get("provider") or "").lower() == "warp"
        and bool(row.get("managed"))
        and bool(row.get("enabled"))
    ]
    assert len(rows) == 6
    blocks = [str(row.get("block") or "") for row in rows]
    assert all(blocks)
    assert len(set(blocks)) == len(blocks)


def test_baseline_capacity_counts_unique_buckets() -> None:
    from free.capacity import evaluate_capacity

    report = evaluate_capacity(
        providers={
            "warp": {
                "configured": 6,
                "enabled": 6,
                "usable": 6,
                "unique_buckets": 5,
            }
        },
        selected=["warp"],
        strategy="fallback",
        target_slots=6,
        reserve_policy="full",
    )
    assert report.providers["warp"].usable == 5
    assert report.providers["warp"].unique_buckets == 5
    assert report.verdict != "ok"
    assert any(item["code"] == "duplicate_block" for item in report.warnings)


def test_baseline_socks5_endpoint_is_connected_by_construction() -> None:
    from free.pool import Socks5Endpoint

    endpoint = Socks5Endpoint(
        host="127.0.0.1",
        port=11080,
        label="warp-test",
        provider="warp",
        managed=True,
        container="warp-test",
    )
    assert endpoint.egress_ip is None
    assert endpoint.status == "connected"
    assert endpoint.current_ip == "socks5:warp-test"


def test_baseline_supervisor_considers_nordvpn_unselected(monkeypatch) -> None:
    import scripts.fresh_chain_supervisor as supervisor

    monkeypatch.setattr(
        supervisor,
        "_live_ip_rotation",
        lambda: {"egress_providers": ["warp"], "proxy_mode": "socks5"},
    )
    assert supervisor._nordvpn_selected() is False


# ---------------------------------------------------------------------------
# Gaps : comportement attendu par 011, non implÃ©mentÃ© aujourd'hui
# ---------------------------------------------------------------------------


def test_block_normalization_is_single_source() -> None:
    import free.quota_ledger as ledger

    quota_block = getattr(ledger, "quota_block", None)
    assert callable(quota_block)
    assert quota_block("2a09:bac1:27a0:2060::3db:41") == "2a09:bac1:27a0:2060::/64"
    assert quota_block("87.120.102.85") == "87.120.102.85/32"

    import free.egress_provider as egress_provider
    import free.warp_rotation as warp_rotation

    assert egress_provider.block_for_egress is quota_block
    assert warp_rotation.block_for_ip("2a09:bac1:27a0:2060::3db:41") == (
        "2a09:bac1:27a0:2060::/64"
    )


def test_egress_resolver_is_single_source() -> None:
    import free.egress_probe as probe

    assert callable(getattr(probe, "resolve_egress", None))


def test_warp_recreate_binds_loopback() -> None:
    from free.warp_rotation import WarpRotator

    rotator = WarpRotator()
    calls: list[list[str]] = []
    rotator._docker = lambda args, timeout=60: calls.append(args) or ""  # type: ignore[method-assign]
    endpoint = SimpleNamespace(image="caomingjun/warp:latest")

    rotator._recreate("warp-test", 11080, endpoint)

    run = next(args for args in calls if args and args[0] == "run")
    assert "127.0.0.1:11080:1080" in run


def test_warp_recreate_preserves_options() -> None:
    from free.warp_rotation import WarpRotator

    rotator = WarpRotator()
    calls: list[list[str]] = []
    rotator._docker = lambda args, timeout=60: calls.append(args) or ""  # type: ignore[method-assign]
    endpoint = SimpleNamespace(image="caomingjun/warp:latest")

    rotator._recreate("warp-test", 11080, endpoint)

    run = next(args for args in calls if args and args[0] == "run")
    assert any(arg in ("-e", "--env") for arg in run)
    assert any(arg in ("--memory", "--pids-limit") for arg in run)
    assert any(arg == "--label" for arg in run)


def test_warp_rotation_logger_is_attached_to_debug_log() -> None:
    source = (ROOT / "opencode.py").read_text(encoding="utf-8")
    for name in (
        "free.pool",
        "free.warp_rotation",
        "free.quota_ledger",
        "station_repair",
    ):
        assert f'attach_module_logger("{name}")' in source


def test_warp_rotation_accepts_shared_budget() -> None:
    from free.warp_rotation import WarpRotator

    WarpRotator(max_rotations_per_hour=10, budget_store=object())


def test_slot_status_has_common_schema() -> None:
    required = {
        "provider",
        "slot_id",
        "generation",
        "state",
        "client_usable",
        "egress_ip",
        "block",
        "quota_state",
        "reset_at",
        "last_evidence",
        "reason",
        "next_action",
        "provider_details",
    }
    from free.pool import Socks5Endpoint

    endpoint = Socks5Endpoint(
        host="127.0.0.1",
        port=11080,
        label="warp-test",
        provider="warp",
        managed=True,
        container="warp-test",
        egress_ip="2a09:bac1:27a0:2060::3db:41",
    )
    assert required <= set(endpoint.get_status())


def test_pool_duplicate_bucket_detector() -> None:
    from free.egress_reconcile import duplicate_bucket_groups, unique_bucket_count

    endpoints = [
        SimpleNamespace(
            enabled=True,
            label="a",
            egress_ip="2a09:bac1:27a0:2060::1",
            block=None,
        ),
        SimpleNamespace(
            enabled=True,
            label="b",
            egress_ip="2a09:bac1:27a0:2060::2",
            block=None,
        ),
        SimpleNamespace(
            enabled=True,
            label="c",
            egress_ip="2a09:bac5:32d2:be::1",
            block=None,
        ),
    ]
    groups = duplicate_bucket_groups(endpoints)
    assert len(groups) == 1
    assert sorted(next(iter(groups.values()))) == ["a", "b"]
    assert unique_bucket_count(endpoints) == 2


def test_supervisor_maintains_unselected_provider(monkeypatch) -> None:
    import scripts.fresh_chain_supervisor as supervisor

    monkeypatch.setattr(
        supervisor,
        "_live_ip_rotation",
        lambda: {
            "egress_providers": ["warp"],
            "proxy_mode": "socks5",
            "station_count": 6,
            "disabled_stations": [],
        },
    )
    assert supervisor._nordvpn_selected() is False
    assert supervisor._nordvpn_maintained() is True


def test_common_lifecycle_engine_exists() -> None:
    from ops.lifecycle import LifecycleEngine, SlotObservation, SlotSpec, SlotState

    assert LifecycleEngine and SlotSpec and SlotObservation and SlotState


def test_common_provider_protocol_exists() -> None:
    from ops.providers.base import SlotProvider

    assert SlotProvider


def test_startup_policy_is_explicit() -> None:
    from ops.lifecycle_policy import StartupPolicy

    policy = StartupPolicy(repair_owner="supervisor", providers=["warp"])
    assert policy.should_connect("nordvpn") is True
