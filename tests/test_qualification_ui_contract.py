"""Static UI contract for qualification freshness and SLO visibility."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COMPONENT = ROOT / "dashboard-ui" / "src" / "pages" / "vpn" / "QualificationStatus.svelte"


def test_every_candidate_row_exposes_last_test_age():
    text = COMPONENT.read_text(encoding="utf-8")
    assert "<th>dernier test</th>" in text
    assert "fmtAge(candidate.last_attempt_at)" in text
    assert "jamais testé" in text
    assert "last_attempt_at" in text


def test_dedicated_qualifiers_card_exposes_toggle_and_count():
    text = COMPONENT.read_text(encoding="utf-8")
    assert '<Card title="Qualifieurs dédiés">' in text
    assert "/api/egress/qualification/qualifiers" in text
    assert "bind:checked={qualifierEnabled}" in text
    assert "bind:value={qualifierCount}" in text
    assert "Tout arrêter" in text
    assert "recommended_max" in text
    assert "stopQualifiers" in text


def test_slo_card_exposes_fleet_and_transport_health():
    text = COMPONENT.read_text(encoding="utf-8")
    assert "<Card title=\"SLO flotte\">" in text
    assert "/api/egress/qualification/slo?target_pct=99" in text
    assert "six_of_six" in text
    assert "error_budget_pct" in text
    assert "mttr_s" in text
    assert "/api/egress/qualification/transports" in text
    assert "degradedTransports" in text
    assert "Santé des transports" in text
