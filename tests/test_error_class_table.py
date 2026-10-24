"""V0.5 — la classification d'erreurs est-elle celle du client officiel ?

**Pourquoi ce fichier existe.** Le proxy classait les erreurs amont par **texte**
(une liste de motifs), le client officiel par **statut** (`executor.ts:225-275`).
Les deux vocabulaires ne se recouvrent pas, et l'écart était invisible : un 429
« Too many requests » sortait en `api_error` alors que le client officiel en fait
un rate limit — donc pas de `retry-after` exploité, pas de pause, pas de
distinction quota/débit.

**Ce que ce test vérifie.** Chaque ligne de `tests/fixtures/error_classes.yaml`
est un contrat : la classe officielle, ET le type émis sur le fil. Une ligne
ajoutée au fixture sans code correspondant échoue ; un changement de code qui
casse une ligne échoue aussi.

Les lignes marquées `motif_priority: true` documentent l'ordre voulu : nos motifs
(textuels, plus spécifiques) gagnent sur la table par statut — c'est ce qui garde
`insufficient_quota` en `permission_error` même sur un 429.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.errors import (  # noqa: E402
    classify_status_class,
    classify_upstream_error,
    is_context_overflow,
)

FIXTURE = ROOT / "tests" / "fixtures" / "error_classes.yaml"


@pytest.fixture(scope="module")
def reference():
    return yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def cas(reference):
    return reference["cases"]


class TestTheFixtureItself:
    def test_it_declares_its_source(self, reference):
        source = reference["reference"]
        assert source["client"] == "official-1.19.3"
        assert "executor.ts" in source["executor"]
        assert set(source["retryable_statuses"]) == {429, 503, 504, 529}

    def test_every_case_is_complete(self, cas):
        for c in cas:
            assert "status" in c and "class" in c and "wire_type" in c, c
            assert isinstance(c["status"], int), c
            assert c["class"] in {
                "authentication",
                "permission",
                "quota",
                "rate_limit",
                "invalid_request",
                "context_overflow",
                "content_policy",
                "provider_internal",
                "unknown",
            }, c

    def test_the_reference_table_is_fully_covered(self, cas):
        """Chaque statut que le client officiel traite a au moins une ligne."""
        statuts = {c["status"] for c in cas}
        for attendu in (401, 403, 429, 400, 404, 409, 413, 422, 500, 503, 504, 529):
            assert attendu in statuts, f"statut {attendu} absent du fixture"

    def test_divergences_are_explicit(self, cas):
        for c in cas:
            if c.get("divergence"):
                assert c.get("note"), f"divergence sans justification : {c}"


class TestOfficialClass:
    """`classify_status_class` doit reproduire `executor.ts:225-275`."""

    def test_each_case(self, cas):
        for c in cas:
            obtenu = classify_status_class(c["status"], c["body"])
            assert obtenu == c["class"], (
                f"statut {c['status']} corps {c['body']!r} : classe {obtenu!r}, "
                f"attendu {c['class']!r} (executor.ts)"
            )

    def test_content_policy_wins_over_any_status(self):
        for statut in (400, 401, 429, 500, 503):
            assert classify_status_class(statut, "blocked by content policy") == "content_policy"

    def test_quota_and_rate_limit_are_distinguished_by_the_body(self):
        assert classify_status_class(429, "") == "rate_limit"
        assert classify_status_class(429, '{"error":"quota_exceeded"}') == "quota"
        assert classify_status_class(429, '{"error":"insufficient quota"}') == "quota"

    def test_retryable_statuses_are_not_unknown(self):
        """`executor.ts:91` : 503/504/529 sont rejouables, donc provider_internal."""
        for statut in (503, 504, 529):
            assert classify_status_class(statut, "") == "provider_internal"

    def test_garbage_input_is_safe(self):
        assert classify_status_class("pas un statut", "") == "provider_internal"
        assert classify_status_class(None, None) == "provider_internal"
        assert classify_status_class(400, b"prompt is too long") == "context_overflow"


class TestContextOverflow:
    """Miroir de `isContextOverflow` (`provider-error.ts:36-38`)."""

    @pytest.mark.parametrize(
        "message",
        [
            "prompt is too long",
            "request_too_large",
            "input is too long for requested model",
            "exceeds the context window",
            "context_length_exceeded",
            "too many tokens",
            "token limit exceeded",
            "request entity too large",
            "model_context_window_exceeded",
        ],
    )
    def test_official_patterns(self, message):
        assert is_context_overflow(message)

    @pytest.mark.parametrize(
        "message",
        [
            "rate limit exceeded",
            "too many requests",
            "throttling error: slow down",
            "service unavailable: retry",
            "",
        ],
    )
    def test_official_exclusions(self, message):
        assert not is_context_overflow(message)

    def test_garbage_is_not_an_overflow(self):
        for valeur in (None, 42, b"prompt is too long"):
            assert is_context_overflow(valeur) is False


class TestWireType:
    """`classify_upstream_error` doit émettre le type correspondant."""

    def test_each_case(self, cas):
        for c in cas:
            _, etype = classify_upstream_error(c["status"], c["body"])
            assert etype == c["wire_type"], (
                f"statut {c['status']} corps {c['body']!r} : type {etype!r}, "
                f"attendu {c['wire_type']!r}"
            )

    def test_motifs_keep_priority_over_the_status_table(self, cas):
        """Les cas `motif_priority` prouvent que l'ordre est motif > statut."""
        prioritaires = [c for c in cas if c.get("motif_priority")]
        assert prioritaires, "le fixture doit documenter au moins un cas de priorité"
        for c in prioritaires:
            _, etype = classify_upstream_error(c["status"], c["body"])
            assert etype == c["wire_type"]

    def test_a_bare_503_stays_api_error(self):
        """Contrat historique : un 503 ordinaire n'est pas une surcharge."""
        assert classify_upstream_error(503, "") == (503, "api_error")

    def test_the_status_is_never_rewritten(self, cas):
        for c in cas:
            status, _ = classify_upstream_error(c["status"], c["body"])
            assert status == c["status"]

    def test_402_is_a_documented_proxy_rule(self):
        """402 = facturation : `permission_error` + pause de clé (parité v2)."""
        assert classify_upstream_error(402, "") == (402, "permission_error")
