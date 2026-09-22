"""POST /v1/systemone — passthrough TypeSafe SystemOne (Jev).

Contrats (docs : https://opencode.ai/docs/fr/zen/#jev + probes live) :
  * le corps {model, state, questions} est relayé TEL QUEL vers
    https://opencode.ai/zen/v1/systemone (jamais vers /chat/completions) ;
  * `jev-1.13` (payant) → clé Zen du pool (Bearer <clé>) ;
  * `jev-1.13-free` → `Bearer public` anonyme direct ;
  * validation : model connu + state non vide + questions typées ;
  * réponse amont relayée telle quelle, usage → compteurs.

Pattern établi : import module-level, jamais de boot réseau, jamais de
touch sur logs/requests.db live (cf. test_phase0_contracts.py).
"""

import json

import pytest
from fastapi.testclient import TestClient

import opencode as oc


class _FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {
            "model": "jev-1.13-free",
            "answers": {"is_urgent": {"type": "noul", "noul": 0.96}},
            "usage": {"input_tokens": 290, "output_tokens": 23},
        }
        raw = json.dumps(self._payload).encode()
        self.content = raw
        self.text = raw.decode()

    def json(self):
        return self._payload


class _FakeClient:
    """Remplace oc._ensure_http_client() : capture l'appel, rend 200."""

    def __init__(self):
        self.calls = []

    async def post(self, endpoint, content=None, headers=None):
        self.calls.append({"endpoint": endpoint, "content": content, "headers": headers})
        return _FakeResp()


@pytest.fixture()
def client(monkeypatch):
    fake = _FakeClient()
    monkeypatch.setattr(oc, "_ensure_http_client", lambda: fake)
    # Silence DB + logs (pas de touch requests.db live)
    monkeypatch.setattr(oc, "_save_and_log_request", _noop_save())
    monkeypatch.setattr(oc, "_log_and_save_error", _noop_err())
    monkeypatch.setattr(oc, "_update_token_usage", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_debug", lambda *a, **k: None)
    monkeypatch.setattr(oc, "_log", lambda *a, **k: None)
    c = TestClient(oc.app)
    c._systemone_fake = fake
    return c


def _noop_save():
    async def _save(*a, **k):
        return None

    return _save


def _noop_err():
    async def _err(*a, **k):
        return None

    return _err


def _body(model="jev-1.13-free"):
    return {
        "model": model,
        "state": "My payments have failed for three days. Please help now.",
        "questions": {
            "is_urgent": {"type": "noul", "instructions": "Does this request require urgent attention?"}
        },
    }


def test_systemone_free_passthrough_anonymous(client):
    """Free → endpoint systemone, Bearer public, corps relayé tel quel."""
    r = client.post("/v1/systemone", json=_body("jev-1.13-free"))
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["model"] == "jev-1.13-free"
    assert data["answers"]["is_urgent"]["noul"] == 0.96

    fake = client._systemone_fake
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["endpoint"] == "https://opencode.ai/zen/v1/systemone"
    assert call["headers"]["Authorization"] == "Bearer public"
    wire = json.loads(call["content"])
    assert wire["model"] == "jev-1.13-free"
    assert wire["state"].startswith("My payments")
    assert wire["questions"]["is_urgent"]["type"] == "noul"


def test_systemone_rejects_chat_payload(client):
    """Pas de state/questions → 400 (jamais relayé à l'amont)."""
    r = client.post("/v1/systemone", json={"model": "jev-1.13-free", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 400
    assert "state" in r.json()["error"]["message"]
    assert client._systemone_fake.calls == []


def test_systemone_rejects_bad_question_type(client):
    bad = _body()
    bad["questions"] = {"q": {"type": "generate", "instructions": "write a poem"}}
    r = client.post("/v1/systemone", json=bad)
    assert r.status_code == 400
    assert "noul" in r.json()["error"]["message"]
    assert client._systemone_fake.calls == []


def test_systemone_rejects_unknown_model(client):
    r = client.post("/v1/systemone", json=_body("nope-9000"))
    assert r.status_code == 404
    assert client._systemone_fake.calls == []


def test_systemone_rejects_non_systemone_model(client):
    """Un LLM de chat routé ici par erreur → 400 garde (pas de fuite
    {state, questions} vers /chat/completions)."""
    r = client.post("/v1/systemone", json=_body("glm-5.1"))
    assert r.status_code == 400
    assert "SystemOne" in r.json()["error"]["message"]
    assert client._systemone_fake.calls == []


def test_systemone_upstream_error_relayed(client):
    """Statut amont non-200 → relayé tel quel (ex. 402 solde vide)."""
    fake = client._systemone_fake

    async def _post_402(endpoint, content=None, headers=None):
        fake.calls.append({"endpoint": endpoint, "content": content, "headers": headers})
        return _FakeResp(
            status=402,
            payload={"error": {"type": "server_error", "message": "Upstream request failed: Insufficient account funds"}},
        )

    fake.post = _post_402
    r = client.post("/v1/systemone", json=_body("jev-1.13-free"))
    assert r.status_code == 402
    assert "funds" in r.json()["error"]["message"]


def test_systemone_config_endpoints():
    """config.yaml : les deux ids jev pointent sur /v1/systemone."""
    assert oc.get_model_config("jev-1.13")["endpoint"].endswith("/v1/systemone")
    assert oc.get_model_config("jev-1.13-free")["endpoint"].endswith("/v1/systemone")


def test_systemone_capabilities_are_decision_only():
    """Jev : pas de génération de texte ni de tool calls (doc TypeSafe)."""
    from config import get_model_capabilities

    caps = get_model_capabilities("jev-1.13")
    assert caps["toolcall"] is False
    assert caps["reasoning"] is False
    assert caps["output"] == ["structured"]
