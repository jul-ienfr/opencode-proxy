"""V2.5 — refus explicite des familles non converties (`gemini`).

**Le defaut avant ce lot.** Le catalogue nomme 10 modeles `gemini` (9 free + 1
payant) avec leur `base_url` et leur famille, mais le proxy n'a aucun
convertisseur pour ce dialecte. Sans refus explicite, l'id retombait
silencieusement sur l'endpoint chat en protocole `openai` : le client recevait
une erreur d'amont incomprehensible — ou, pire, un 200 vide (le defaut V5.1).

**Ce que ces tests verrouillent.** Le refus est un `501` qui nomme le modele et
la famille, dans le protocole du client. Un modele normal n'est jamais refuse.

**Ce qui a failli m'egarer.** Une premiere sonde a conclu « aucun modele gemini
dans le cache » — parce qu'elle lisait la racine du JSON au lieu de
`providers`. Le cache contenait bien les 10 entrees depuis le debut. C'est la
deuxieme fois de la session qu'une sonde baclee a failli fonder un lot entier
sur une premisse fausse.
"""

import pytest

from config import settings as cs


class TestCatalogUnsupported:
    def test_gemini_models_are_listed(self):
        assert cs.CATALOG_UNSUPPORTED, "les 10 modeles gemini doivent etre listes"
        assert all(family == "gemini" for family in cs.CATALOG_UNSUPPORTED.values())

    def test_known_ids_are_present_on_both_legs(self):
        assert cs.CATALOG_UNSUPPORTED.get("gemini-3-flash") == "gemini"
        assert cs.CATALOG_UNSUPPORTED.get("gemini-4-argon-contributor") == "gemini"

    def test_convertible_models_are_absent(self):
        for model in ("claude-sonnet-4-5", "big-pickle", "glm-5.1"):
            assert model not in cs.CATALOG_UNSUPPORTED

    def test_only_null_protocol_entries_are_captured(self):
        """Aucune entree dotee d'un protocole ne doit figurer dans la table."""
        for rows in (cs.CATALOG_ROUTES or {}).values():
            for model_id, route in (rows or {}).items():
                if isinstance(route, dict) and route.get("protocol"):
                    assert model_id not in cs.CATALOG_UNSUPPORTED


class TestRouteRefusal:
    def test_route_for_refuses_a_gemini_model(self):
        import opencode as oc

        with pytest.raises(oc.UnsupportedModelFamily) as excinfo:
            oc._route_for("gemini-3-flash")
        assert excinfo.value.status_code == 501
        assert excinfo.value.family == "gemini"
        assert "gemini-3-flash" in excinfo.value.client_message

    def test_message_names_the_family_and_the_alternative(self):
        import opencode as oc

        message = oc.UnsupportedModelFamily("gemini-3-pro", "gemini").client_message
        assert "gemini-3-pro" in message
        assert "gemini" in message
        assert "OpenAI" in message or "Anthropic" in message

    def test_a_normal_model_still_routes(self):
        import opencode as oc

        route = oc._route_for("claude-sonnet-4-5")
        assert route, "un modele convertible ne doit pas etre refuse"


class TestHttpContract:
    """Le refus doit atteindre le client avec un statut honnete."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path,body,shape",
        [
            (
                "/v1/messages",
                {"model": "gemini-3-flash", "stream": True, "max_tokens": 16,
                 "messages": [{"role": "user", "content": "salut"}]},
                "anthropic",
            ),
            (
                "/v1/chat/completions",
                {"model": "gemini-3-pro", "stream": True,
                 "messages": [{"role": "user", "content": "salut"}]},
                "openai",
            ),
            (
                "/v1/responses",
                {"model": "gemini-4-argon-contributor", "stream": True, "input": "salut"},
                "openai",
            ),
        ],
    )
    async def test_gemini_request_is_a_501_in_the_client_protocol(self, path, body, shape):
        import httpx

        import opencode as oc

        transport = httpx.ASGITransport(app=oc.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=30) as client:
            response = await client.post(path, json=body)

        assert response.status_code == 501, response.text[:300]
        payload = response.json()
        if shape == "anthropic":
            assert payload["type"] == "error"
            message = payload["error"]["message"]
        else:
            message = payload["error"]["message"]
        assert "gemini" in message
        assert "protocol" in message
