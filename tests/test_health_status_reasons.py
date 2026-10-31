"""V8.7 — `/health` doit dire POURQUOI il est dégradé, pas seulement qu'il l'est.

**Le cas vécu.** Le proxy répondait `degraded`, avec `upstream: ok`, aucun
circuit ouvert, et un chat free en 1,5 s. Il a fallu interroger trois endpoints
pour trouver la cause : un **pause de clé oublié** (`reason: "test"`, 275 s
restantes) laissé par une vérification. Du point de vue de la supervision c'était
« normal » ; du point de vue humain c'était indéchiffrable.

**Ce que ces tests exigent.** Le statut et ses raisons sont produits par une
fonction pure, donc testable sans réseau, et chaque cause possible a sa phrase.
"""

from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import opencode as oc  # noqa: E402

statut = oc._health_status_reasons


class TestHealthyCase:
    def test_everything_fine_means_ok_and_no_reason(self):
        etat, raisons = statut(upstream_ok=True)
        assert etat == "ok"
        assert raisons == []

    def test_an_empty_pause_dict_is_not_a_reason(self):
        etat, raisons = statut(upstream_ok=True, pauses={})
        assert (etat, raisons) == ("ok", [])


class TestEachCauseIsNamed:
    def test_upstream_down(self):
        etat, raisons = statut(upstream_ok=False)
        assert etat == "degraded"
        assert any("upstream" in r for r in raisons)

    def test_open_circuit_names_the_endpoint(self):
        etat, raisons = statut(upstream_ok=True, open_breakers=["https://opencode.ai/inference/go/openai/v1/chat/completions"])
        assert etat == "degraded"
        assert any("circuit open" in r and "opencode.ai" in r for r in raisons)

    def test_paused_key_shows_the_reason_and_the_remaining_time(self):
        etat, raisons = statut(
            upstream_ok=True,
            pauses={"8b1abb9f9d91": {"remaining_seconds": 275.7, "reason": "test"}},
        )
        assert etat == "degraded"
        assert any("8b1abb9f9d91" in r and "test" in r and "275s" in r for r in raisons)

    def test_paused_key_without_remaining_time_still_named(self):
        etat, raisons = statut(upstream_ok=True, pauses={"abc": {"reason": "credit"}})
        assert etat == "degraded"
        assert any("abc" in r and "credit" in r for r in raisons)

    def test_failing_canary(self):
        etat, raisons = statut(upstream_ok=True, canary_status="failing")
        assert etat == "degraded"
        assert any("canary" in r for r in raisons)

    def test_an_ok_canary_does_not_degrade(self):
        etat, _ = statut(upstream_ok=True, canary_status="ok")
        assert etat == "ok"


class TestReasonsAccumulate:
    def test_two_causes_give_two_reasons(self):
        etat, raisons = statut(
            upstream_ok=False,
            open_breakers=["ep1"],
            pauses={"k": {"reason": "401", "remaining_seconds": 60}},
            canary_status="failing",
        )
        assert etat == "degraded"
        assert len(raisons) == 4

    def test_garbage_pause_entry_does_not_crash(self):
        etat, raisons = statut(upstream_ok=True, pauses={"k": "pas un dict"})
        assert etat == "degraded"
        assert len(raisons) == 1


class TestThePayloadCarriesTheReasons:
    def test_health_response_includes_status_reasons(self):
        from fastapi.testclient import TestClient

        client = TestClient(oc.app)
        # `/health` crée le client HTTP partagé (`oc._client`) dans la boucle du
        # TestClient. Ce client-là est lié à une boucle qui meurt à la fin du
        # test : le laisser en place casse `test_http_client_self_heal`, qui
        # appelle `aclose()` sur le global depuis une AUTRE boucle. On remet donc
        # l'état global à zéro — le prochain test recréera un client propre.
        try:
            reponse = client.get("/health")
            assert reponse.status_code == 200
            corps = reponse.json()
            assert "status_reasons" in corps, "le payload doit exposer ses raisons"
            assert isinstance(corps["status_reasons"], list)
            if corps["status"] == "degraded":
                assert corps["status_reasons"], "dégradé sans raison = indéchiffrable"
            else:
                assert corps["status_reasons"] == []
        finally:
            oc._client = None
            oc._health_cache = None
