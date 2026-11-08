"""Plan 011 â€” contrat de consolidation NordVPN / WARP (US7).

Tous les tests de ce fichier dÃ©crivent la cible de consolidation et sont
marquÃ©s ``xfail(strict=True)`` tant que le moteur commun n'existe pas. Ils
servent de gate : dÃ¨s qu'un module est livrÃ©, le test correspondant passe en
XPASS et la suite Ã©choue, ce qui oblige Ã  mettre le plan et le marqueur Ã  jour.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_provider_adapters_exist_and_share_the_core() -> None:
    from ops.lifecycle import LifecycleEngine, SlotObservation, SlotSpec
    from ops.providers.base import SlotProvider
    from ops.providers.nordvpn import NordVpnProvider
    from ops.providers.warp import WarpProvider

    assert LifecycleEngine and SlotSpec and SlotObservation and SlotProvider
    for adapter in (NordVpnProvider, WarpProvider):
        assert hasattr(adapter, "specs")
        assert hasattr(adapter, "observe")
        assert hasattr(adapter, "create")
        assert hasattr(adapter, "recreate")
        assert hasattr(adapter, "delete")
        assert hasattr(adapter, "rotate")
        assert hasattr(adapter, "probe")
        assert hasattr(adapter, "persist")


def test_same_failure_same_decision_for_both_providers() -> None:
    from ops.lifecycle import LifecycleEngine

    class _Provider:
        def __init__(self, name: str) -> None:
            self.name = name

        def observe(self, spec):
            return {
                "state": "egress_ok",
                "quota_state": "exhausted",
                "reset_at": 1_800_000_000.0,
                "reason": "quota_429",
            }

        def probe(self, spec):
            return {"ok": False, "status": 429, "reason": "FreeUsageLimitError"}

    engine = LifecycleEngine(providers=[_Provider("nordvpn"), _Provider("warp")])
    decisions = engine.decide_failure("quota_429")
    assert decisions["nordvpn"].action == decisions["warp"].action
    assert decisions["nordvpn"].cause == decisions["warp"].cause


def test_repair_policy_is_shared() -> None:
    from ops.lifecycle_policy import RepairPolicy

    policy = RepairPolicy.from_config({})
    assert policy.action_for("server_dead") == "repin"
    assert policy.action_for("quota_429") == "park"
    assert policy.action_for("healthcheck") == "recreate"


def test_rotation_budget_is_shared() -> None:
    from ops.lifecycle_policy import RotationBudget

    budget = RotationBudget(max_per_hour=10, store={})
    assert budget.allow("nordvpn", 1) is True
    assert budget.allow("warp", 1) is True
    for _ in range(10):
        budget.record("nordvpn", 1)
    assert budget.allow("nordvpn", 1) is False


def test_slot_status_schema_is_shared() -> None:
    from ops.lifecycle import SlotStatus

    for provider in ("nordvpn", "warp"):
        status = SlotStatus(
            provider=provider,
            slot_id=1,
            generation=1,
            state="client_usable",
            client_usable=True,
            egress_ip="87.120.102.85",
            block="87.120.102.85/32",
            quota_state="ok",
            reset_at=None,
            last_evidence="sha256:abc;bytes=1536",
            reason=None,
            next_action="none",
            provider_details={},
        )
        payload = status.to_dict()
        assert payload["provider"] == provider
        assert "provider_details" in payload


def test_consolidation_legacy_block_functions_delegate() -> None:
    import free.egress_provider as egress_provider
    import free.quota_ledger as ledger
    import free.warp_rotation as warp_rotation

    assert egress_provider.block_for_egress is ledger.quota_block
    assert warp_rotation.block_for_ip("2a09:bac1:27a0:2060::3db:41") == (
        "2a09:bac1:27a0:2060::/64"
    )


def test_stations_api_route_exists() -> None:
    source = (ROOT / "dashboard" / "api.py").read_text(encoding="utf-8")
    assert '"/api/stations"' in source
    assert '"/api/stations/{provider}/{station}"' in source


def test_station_audit_cli_exists() -> None:
    assert (ROOT / "scripts" / "station_audit.py").is_file()


def test_lifecycle_logging_is_centralized() -> None:
    source = (ROOT / "ops" / "lifecycle_evidence.py").read_text(encoding="utf-8")
    assert "provider" in source
    assert "slot" in source
    assert "evidence" in source
