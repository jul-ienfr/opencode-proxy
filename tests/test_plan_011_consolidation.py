"""Plan 011 â€” contrat de consolidation NordVPN / WARP (US7).

Tous les tests de ce fichier dÃ©crivent la cible de consolidation et sont
marquÃ©s ``xfail(strict=True)`` tant que le moteur commun n'existe pas. Ils
servent de gate : dÃ¨s qu'un module est livrÃ©, le test correspondant passe en
XPASS et la suite Ã©choue, ce qui oblige Ã  mettre le plan et le marqueur Ã  jour.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]







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
    source += (ROOT / "dashboard" / "routers" / "station_routes.py").read_text(
        encoding="utf-8"
    )
    assert '"/api/stations"' in source
    assert '"/api/stations/{provider}/{station}"' in source


def test_station_audit_cli_exists() -> None:
    assert (ROOT / "scripts" / "station_audit.py").is_file()


def test_lifecycle_logging_is_centralized() -> None:
    source = (ROOT / "free" / "warp_rotation.py").read_text(encoding="utf-8")
    assert "provider" in source
    assert "slot" in source
    assert "evidence" in source
