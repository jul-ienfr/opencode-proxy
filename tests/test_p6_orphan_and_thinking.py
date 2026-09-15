"""[TROU 5] P6 : garde orphelin contournée, et signature locale forgée (mesure).

Chemin visé : **P6** = client ``POST /v1/responses`` → amont **Anthropic**
(handler ``responses``, corps ``anthro_body = openai_responses_to_anthropic(body)``).

DÉFAUT MESURÉ (lecture du code, corroborée par le sous-agent du TROU 4) : dans le handler,
``anthro_body`` est construit **avant** la garde orphelin, qui filtre ``body`` :

    anthro_body = openai_responses_to_anthropic(body)   # construit ici
    ...
    body["messages"] = _drop_orphan_tool_messages(...)   # garde sur `body`
    elif "input" in body:
        body["input"] = _drop_orphan_responses_input(...)
    if isinstance(anthro_body, dict) and "messages" in anthro_body:
        pass                                             # <-- ne faisait RIEN

Le bloc final avait l'apparence d'une garde (« keep for completeness ») sans en être une :
un ``function_call_output`` orphelin partait donc vers l'amont Anthropic dès que le client
parlait Responses, alors que le même orphelin est écarté sur les autres chemins.

Niveau de preuve : ASGI + doubles amont, on inspecte le corps **réellement transmis**, pas
le convertisseur isolé — seul niveau qui attrape un filtre appliqué au mauvais objet.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import opencode as oc
from app.protocol.mapping import _local_signature

# Cible Anthropic sans équivalent free : la jambe payante est exercée sans swap.
PAID_ANTHRO = "minimax-m3"
EP_ANTHRO = "https://opencode.ai/zen/go/v1/messages"

REASONING = "raisonnement synthetique du proxy"
FORGED_SIGNATURE = _local_signature(REASONING)

ANTHRO_PAYLOAD = {
    "id": "msg_p6",
    "type": "message",
    "role": "assistant",
    "model": PAID_ANTHRO,
    "content": [{"type": "text", "text": "P6 ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 11, "output_tokens": 22},
}


class FakeResponse:
    """Double de réponse amont : interface consommée par le handler P6."""

    def __init__(self, payload=None, lines=None):
        self.status_code = 200
        self._payload = payload
        if payload is not None:
            self.headers = {"content-type": "application/json"}
            self.text = json.dumps(payload)
        else:
            self.headers = {"content-type": "text/event-stream"}
            self.text = ""
        self._lines = list(lines or [])

    async def aread(self):
        return self.text.encode()

    @property
    def content(self):
        return self.text.encode()

    def json(self):
        return self._payload

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aiter_bytes(self):
        for line in self._lines:
            yield (line + "\n").encode()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class UpstreamRecorder:
    """Capture, dans l'ordre, le corps exact reçu par chaque jambe amont."""

    def __init__(self):
        self.calls: list[dict] = []

    def _record(self, seam, endpoint, body, protocol=None, extra=None):
        entry = {
            "seam": seam,
            "endpoint": endpoint,
            "protocol": protocol,
            "body": json.loads(json.dumps(body, default=str)),
        }
        if extra:
            entry.update(extra)
        self.calls.append(entry)
        return entry

    @property
    def last(self) -> dict:
        assert self.calls, "aucun appel amont capturé"
        return self.calls[-1]


class _NullCache:
    def make_key(self, *a, **k):
        return None

    def get(self, *a, **k):
        return None

    def put(self, *a, **k):
        return None


@pytest.fixture
def recorder(monkeypatch):
    rec = UpstreamRecorder()

    async def _no_geo_gate(*a, **k):
        return None

    async def _noop_async(*a, **k):
        return None

    async def _no_free(*a, **k):
        return None

    monkeypatch.setattr(oc, "_enforce_geo_gate", _no_geo_gate, raising=False)
    monkeypatch.setattr(oc, "_cb_should_allow", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(oc, "_cb_record_failure", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc, "_cb_record_success", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc, "_save_and_log_request", _noop_async, raising=False)
    monkeypatch.setattr(oc, "_log_and_save_error", _noop_async, raising=False)
    monkeypatch.setattr(oc, "_save_request", _noop_async, raising=False)
    monkeypatch.setattr(oc, "_update_token_usage", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc, "_log_free_model_usage", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(oc, "_estimate_input_tokens", lambda *a, **k: 7, raising=False)
    monkeypatch.setattr(oc, "_alias_for_key", lambda key: "test-alias", raising=False)
    monkeypatch.setattr(oc, "_try_free_model_first", _no_free, raising=False)
    monkeypatch.setattr(oc, "_response_cache", _NullCache(), raising=False)

    def _auth_headers(protocol, entry=None):
        key = (entry or {}).get("api_key", "test-key-A")
        if protocol == "openai":
            return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        return {"x-api-key": key, "Content-Type": "application/json", "anthropic-version": "2023-06-01"}

    monkeypatch.setattr(oc, "_get_auth_headers", _auth_headers, raising=False)

    async def _do_request_with_retry(endpoint, body, headers, protocol, retry_on_429=True):
        rec._record("http", endpoint, body, protocol)
        return FakeResponse(payload=ANTHRO_PAYLOAD), headers

    @asynccontextmanager
    async def _open_free_stream(endpoint, body, headers, use_free, count_request=True, **kwargs):
        rec._record("free", endpoint, body, extra={"use_free": bool(use_free)})
        yield FakeResponse(payload=ANTHRO_PAYLOAD)

    @asynccontextmanager
    async def _open_via_pool(endpoint, body, headers, is_stream=False, forced_pool=None):
        rec._record("pool", endpoint, body, extra={"is_stream": bool(is_stream)})
        yield FakeResponse(payload=ANTHRO_PAYLOAD)

    monkeypatch.setattr(oc, "_do_request_with_retry", _do_request_with_retry, raising=False)
    monkeypatch.setattr(oc, "_open_free_stream", _open_free_stream, raising=False)
    monkeypatch.setattr(oc, "_open_via_pool", _open_via_pool, raising=False)
    return rec


@pytest.fixture
def client():
    return TestClient(oc.app)


def _orphan_tool_results(body: dict) -> list[dict]:
    """Blocs ``tool_result`` dont l'id n'a AUCUN ``tool_use`` correspondant.

    C'est la définition exacte d'un orphelin : l'amont Anthropic refuse une
    conversation où un ``tool_result`` ne répond à aucun ``tool_use``.
    """
    messages = body.get("messages") or []
    annonces: set[str] = set()
    for msg in messages:
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                ident = block.get("id")
                if isinstance(ident, str):
                    annonces.add(ident)
    orphelins = []
    for msg in messages:
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                if block.get("tool_use_id") not in annonces:
                    orphelins.append(block)
    return orphelins


def _signed_thinking_blocks(body: dict) -> list[dict]:
    found = []
    for msg in body.get("messages") or []:
        for block in msg.get("content") or []:
            if isinstance(block, dict) and block.get("type") == "thinking":
                if block.get("signature") == FORGED_SIGNATURE:
                    found.append(block)
    return found


def _p6_body(*, avec_orphelin: bool) -> dict:
    """Requête P6 (client Responses)."""
    entree: list[dict] = [
        {"role": "user", "content": [{"type": "input_text", "text": "question"}]}
    ]
    if avec_orphelin:
        entree.append(
            {
                "type": "function_call_output",
                "call_id": "call_orphelin_9",
                "output": "resultat sans appel correspondant",
            }
        )
    return {"model": PAID_ANTHRO, "max_output_tokens": 128, "input": entree}


def test_p6_nonstream_n_envoie_pas_de_tool_result_orphelin(recorder, client):
    """L'amont Anthropic ne doit recevoir aucun ``tool_result`` orphelin (P6 non-stream)."""
    r = client.post("/v1/responses", json=_p6_body(avec_orphelin=True))

    assert r.status_code == 200, r.text
    upstream = recorder.last
    assert upstream["endpoint"] == EP_ANTHRO, f"jambe amont inattendue : {upstream['endpoint']}"
    assert upstream["protocol"] == "anthropic"
    orphelins = _orphan_tool_results(upstream["body"])
    assert not orphelins, (
        "P6 a transmis a l'amont Anthropic un tool_result sans tool_use correspondant : "
        f"{orphelins!r} — la garde orphelin a ete appliquee a `body` au lieu du corps envoye"
    )


def test_p6_nonstream_aucune_signature_locale_forgee(recorder, client):
    """MESURE : un bloc ``thinking`` à signature locale peut-il partir en amont sur P6 ?

    Ce témoin est un **constat de mesure** : si le convertisseur P6 ne forge jamais de
    signature, il passe avant comme après l'ajout de ``strip_synthetic_thinking`` — et
    cela signifie que cet ajout est de la défense en profondeur, pas un correctif prouvé.
    """
    r = client.post("/v1/responses", json=_p6_body(avec_orphelin=False))

    assert r.status_code == 200, r.text
    upstream = recorder.last
    forges = _signed_thinking_blocks(upstream["body"])
    assert not forges, f"P6 a emis un bloc thinking a signature locale (forgee) : {forges!r}"


def test_p6_nonstream_historique_intact_hors_orphelin(recorder, client):
    """Contre-témoin : la garde ne doit pas manger le contenu légitime.

    Sans ce test, une garde qui viderait toute la conversation passerait au vert.
    """
    r = client.post("/v1/responses", json=_p6_body(avec_orphelin=False))

    assert r.status_code == 200, r.text
    textes = [
        block.get("text")
        for msg in recorder.last["body"].get("messages") or []
        for block in msg.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    assert "question" in textes, f"le message utilisateur a disparu de l'amont : {textes!r}"
