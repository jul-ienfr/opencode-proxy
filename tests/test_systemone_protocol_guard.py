"""V8.5 — un modèle SystemOne demandé en chat doit être refusé clairement.

**Le cas qui a coûté une fausse conclusion.** `jev-1.13-free` a été déclaré
« refusé en amont » après une dizaine d'appels en erreur. La vraie cause : ce
n'est pas un modèle de chat — son endpoint est
`https://opencode.ai/inference/v1/systemone`, un protocole à questions
(`questions` + `state` + `criteria`). L'amont répondait
`api_usage_error / Invalid request`, et le proxy relayait ce message tel quel.

**Ce que ces tests exigent.** Un client qui demande un modèle SystemOne sur
`/v1/messages` ou `/v1/chat/completions` reçoit un `400` qui **nomme** le bon
endpoint. Pas un 502, pas une trame d'erreur opaque : un message actionnable.
"""

from __future__ import annotations

import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import opencode as oc  # noqa: E402


@pytest.fixture(scope="module")
def client():
    return TestClient(oc.app)


SYSTEMONE_MODELS = [name for name in ("jev-1.13", "jev-1.13-free") if name in oc._cfg_settings.MODELS]


class TestTheRouteIsReallySystemOne:
    def test_systemone_models_point_at_the_systemone_endpoint(self):
        """Le fait qui fonde tout le lot : à vérifier, sinon le test ne prouve rien."""
        for name in SYSTEMONE_MODELS:
            endpoint = (oc._cfg_settings.MODELS.get(name) or {}).get("endpoint") or ""
            assert "systemone" in endpoint, f"{name} n'est pas routé vers SystemOne"

    def test_chat_models_do_not_point_at_systemone(self):
        for name in ("big-pickle", "deepseek-v4.1-flash-free"):
            endpoint = (oc._cfg_settings.MODELS.get(name) or {}).get("endpoint") or ""
            if name in oc._cfg_settings.MODELS:
                assert "systemone" not in endpoint


class TestMessagesRefusesCleanly:
    @pytest.mark.parametrize("model", SYSTEMONE_MODELS or ["jev-1.13-free"])
    def test_messages_returns_400_naming_the_right_endpoint(self, client, model):
        resp = client.post(
            "/v1/messages",
            json={
                "model": model,
                "stream": True,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 400, resp.text[:200]
        body = resp.text
        assert "/v1/systemone" in body, "l'erreur doit nommer l'endpoint à utiliser"
        assert "SystemOne" in body or "systemone" in body

    def test_messages_still_accepts_a_chat_model(self, client):
        """Garde-fou : le refus ne doit pas être trop large."""
        route = oc._route_for("big-pickle")
        if not route:
            pytest.skip("big-pickle non routé")
        endpoint = (oc.get_model_config(route["model"]) or {}).get("endpoint") or ""
        assert "systemone" not in endpoint


class TestChatRefusesCleanly:
    @pytest.mark.parametrize("model", SYSTEMONE_MODELS or ["jev-1.13-free"])
    def test_chat_completions_returns_400_naming_the_right_endpoint(self, client, model):
        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": model,
                "stream": True,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.status_code == 400, resp.text[:200]
        assert "/v1/systemone" in resp.text


class TestTheEndpointStillAcceptsSystemOne:
    def test_systemone_still_validates_its_own_body(self, client):
        """Le refus en chat ne doit pas casser le chemin SystemOne lui-même."""
        resp = client.post(
            "/v1/systemone",
            json={"model": "jev-1.13-free", "state": "x", "questions": {}},
        )
        assert resp.status_code == 400
        assert "questions" in resp.text.lower()
