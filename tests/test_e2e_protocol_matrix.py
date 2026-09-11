"""[Lot L7 — E2E & corpus] Matrice E2E bout-en-bout des 6 chemins de conversion.

Contrat vérifié (plan §V3/V4, PLAN_AUDIT_CONVERSIONS_2026-09-10.md lignes 75-76, 149) :

- V3 « E2E ASGI avec faux amont » : on monte ``httpx``/``ASGITransport`` sur
  ``opencode.app`` via ``TestClient`` et on **capture le corps exact reçu par
  l'amont** (endpoint + clés + valeurs). C'est le seul niveau qui attrape les
  overrides de route, les swaps free-model et le passthrough : un convertisseur
  peut être correct testé seul et neutralisé un lien plus loin dans le handler.
- V4 « Propriétés & corpus » : corpus embarqué de payloads réalistes (texte,
  outils, ``tool_result``, images, documents, raisonnement, multi-tours) et
  vérification que l'aller (requête convertie) puis le retour (réponse
  reconvertie) préservent le contenu essentiel — noms/ids d'outils survivants,
  texte ni tronqué ni dupliqué, compteurs d'usage conservés.

Les 6 chemins réels (plan §1) et ce qui est asserté sur le fil :

    P1  POST /v1/messages          → anthropic  passthrough + strip_synthetic_thinking
    P2  POST /v1/messages          → openai     anthropic_to_openai / openai_to_anthropic
    P3  POST /v1/chat/completions  → openai     passthrough + ensure_min_tokens
    P4  POST /v1/chat/completions  → anthropic  openai_to_anthropic_request /
                                                anthropic_to_openai_response
    P5  POST /v1/responses         → openai     anthropic_to_openai +
                                                _chat_to_responses_request +
                                                _relay_responses_storage_fields
    P6  POST /v1/responses         → anthropic  openai_responses_to_anthropic /
                                                anthropic_to_openai_responses

Herméticité : aucun accès réseau, aucune clé réelle, aucune horloge murale. Tous
les seams amont sont remplacés (``_do_request_with_retry``, ``_open_free_stream``,
``_open_via_pool``, ``_get_auth_headers``, ``_try_free_model_first``), le cache de
réponses est neutralisé et la géo/circuit-breaker sont ouverts.

``opencode.py`` et ``app/protocol/mapping.py`` ne sont **jamais modifiés** : ils
sont lus et monkeypatchés à l'exécution.

Le corpus V4 est **embarqué** dans ce fichier : le livrable L7 est un fichier
unique et ``tests/fixtures/protocol_corpus/`` n'existe pas dans le dépôt.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

import config.settings as _cfg_settings
import opencode as oc

# ─────────────────────────── Endpoints amont attendus ───────────────────────────

EP_ANTHRO = "https://opencode.ai/zen/go/v1/messages"
EP_CHAT = "https://opencode.ai/zen/go/v1/chat/completions"
EP_RESPONSES = "https://opencode.ai/zen/go/v1/responses"
EP_FREE_CHAT = "https://opencode.ai/zen/v1/chat/completions"

# Modèles clients : cible payante unique par chemin (aucun équivalent free dans
# FREE_MODEL_MAP) → la jambe payante est exercée sans dépendre du swap free.
PAID_ANTHRO = "minimax-m3"  # /v1/messages + /v1/chat/completions + /v1/responses → anthropic
PAID_CHAT = "glm-5"  # → openai chat/completions
PAID_RESPONSES = "muse-spark-1.3-contributor"  # → openai /v1/responses

# Sous-chemin free-model (stream) : on doit partir d'un modèle CLIENT dont la
# cible a un équivalent free — c'est le seul point d'entrée qui déclenche le swap.
#   P2 : « sonnet » → glm-5.1 (openai chat)  → free deepseek-v4-flash-free
#   P4 : « haiku »  → minimax-m2.5 (anthropic) → free mimo-v2.5-free
FREE_CLIENT_P2 = "sonnet"
FREE_CLIENT_P4 = "haiku"

# Régression P4-stream — **CORRIGÉE** (A24).
# ``_anthro_to_oai_stream`` (opencode.py:12569) déclarait ``nonlocal endpoint,
# model_id`` en OMETTANT ``anthro_body``, alors qu'il l'assigne (jambe free
# ~12578, retour payant ~12732) et le lit avant toute assignation (~12576).
# Python en faisait donc une variable LOCALE, ce qui cassait les DEUX jambes :
#   - jambe free   : ``dict(anthro_body)`` (~12576) lit AVANT l'assignation de
#     ~12578 → UnboundLocalError levée avant la boucle de retry, donc AUCUNE
#     trace de log ; le client recevait ``text/event-stream`` et 0 octet ;
#   - jambe payante : lecture dans la boucle → UnboundLocalError loggée
#     (« ERROR stream (attempt 1) » / « (attempt 2) »), retry clé alternative
#     également en échec, 0 octet.
# Bug PRÉ-EXISTANT au commit ``110d587`` (``anthro_body`` dans ``co_varnames``,
# absent de ``co_freevars``, lectures compilées en ``LOAD_FAST_CHECK``).
# Aucun test du dépôt ne couvrait ``_anthro_to_oai_stream`` avant ce lot.
#
# Correctif appliqué : ajout d'``anthro_body`` à la déclaration ``nonlocal``.
# Les 3 tests P4-stream ci-dessous sont donc de simples tests de régression
# (plus de ``xfail``) : ils ont été observés ROUGES avant le correctif et VERTS
# après, et redeviennent rouges si on retire ``anthro_body`` du ``nonlocal``.

# ─────────────────────────── Corpus V4 embarqué ───────────────────────────

CORPUS_TEXT: dict[str, Any] = {
    "name": "text_simple",
    "anthro": {
        "model": PAID_CHAT,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": "Explique la photosynthèse en une phrase."}],
    },
    "text": "Explique la photosynthèse en une phrase.",
}

CORPUS_TOOLS: dict[str, Any] = {
    "name": "tools_and_tool_result",
    "anthro": {
        "model": PAID_CHAT,
        "max_tokens": 1024,
        "tools": [
            {
                "name": "get_weather",
                "description": "Météo d'une ville",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            },
            {
                "name": "search_docs",
                "description": "Recherche documentaire",
                "input_schema": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        ],
        "messages": [
            {"role": "user", "content": "Quel temps fait-il à Paris ?"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01A",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01A",
                        "content": [{"type": "text", "text": "18 °C, ciel dégagé"}],
                    }
                ],
            },
        ],
    },
    "tool_id": "toolu_01A",
    "tool_names": ["get_weather", "search_docs"],
    "tool_result_text": "18 °C, ciel dégagé",
}

CORPUS_IMAGES: dict[str, Any] = {
    "name": "image_base64_and_url",
    "anthro": {
        "model": PAID_CHAT,
        "max_tokens": 512,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Décris ces deux images."},
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==",
                        },
                    },
                    {
                        "type": "image",
                        "source": {"type": "url", "url": "https://example.com/cat.png"},
                    },
                ],
            }
        ],
    },
    "image_count": 2,
    "url": "https://example.com/cat.png",
}

CORPUS_DOCUMENTS: dict[str, Any] = {
    "name": "document_pdf_base64",
    "anthro": {
        "model": PAID_CHAT,
        "max_tokens": 512,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Résume ce document."},
                    {
                        "type": "document",
                        "source": {
                            "type": "base64",
                            "media_type": "application/pdf",
                            "data": "JVBERi0xLjQKJcOkw7zDtsOfCjIgMCBvYmoKPDwvTGVuZ3RoIDM+PnN0cmVhbQpBTkQKZW5kc3RyZWFt",
                        },
                        "title": "rapport.pdf",
                    },
                ],
            }
        ],
    },
    "document_count": 1,
    "title": "rapport.pdf",
    "data_fragment": "JVBERi0xLjQKJcOkw7zDtsOfCjIgMCBvYmoKPDwvTGVuZ3RoIDM+PnN0cmVhbQpBTkQKZW5kc3RyZWFt",
}

CORPUS_REASONING: dict[str, Any] = {
    "name": "reasoning_history_multi_turn",
    "anthro": {
        "model": PAID_CHAT,
        "max_tokens": 2048,
        "thinking": {"type": "enabled", "budget_tokens": 5000},
        "messages": [
            {"role": "user", "content": "Calcule 17 × 23."},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "17 × 23 = 17 × 20 + 17 × 3 = 340 + 51", "signature": "AUTHENTIC=="},
                    {"type": "text", "text": "17 × 23 = 391."},
                ],
            },
            {"role": "user", "content": "Et 391 + 9 ?"},
        ],
    },
    "reasoning_text": "17 × 23 = 17 × 20 + 17 × 3 = 340 + 51",
    "answer": "17 × 23 = 391.",
}

CORPUS: list[dict[str, Any]] = [CORPUS_TEXT, CORPUS_TOOLS, CORPUS_IMAGES, CORPUS_DOCUMENTS, CORPUS_REASONING]

# ─────────────────────────── Réponses amont canoniques ───────────────────────────


def anthro_message(text="hello", tool_calls=False):
    """Réponse Anthropic Messages bien formée."""
    if tool_calls:
        content = [
            {"type": "text", "text": text},
            {"type": "tool_use", "id": "toolu_out_1", "name": "get_weather", "input": {"city": "Paris"}},
        ]
        stop = "tool_use"
    else:
        content = [{"type": "text", "text": text}]
        stop = "end_turn"
    return {
        "id": "msg_upstream_1",
        "type": "message",
        "role": "assistant",
        "model": "upstream",
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 22},
    }


def chat_completion(text="hello", tool_calls=False):
    """Réponse OpenAI Chat Completions bien formée."""
    if tool_calls:
        message = {
            "role": "assistant",
            "content": text,
            "tool_calls": [
                {
                    "id": "call_out_1",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
                }
            ],
        }
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": text}
        finish = "stop"
    return {
        "id": "chatcmpl_upstream_1",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "upstream",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    }


def responses_object(text="hello"):
    """Réponse OpenAI Responses bien formée."""
    return {
        "id": "resp_upstream_1",
        "object": "response",
        "status": "completed",
        "model": "upstream",
        "output": [
            {
                "type": "message",
                "id": "msg_out_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
    }


# Séquences SSE amont (lignes, sans \n final : les handlers ajoutent/splitent).
CHAT_CHUNK_LINES = [
    'data: {"id":"chatcmpl_s","object":"chat.completion.chunk","created":1700000000,"model":"upstream",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"hello"},"finish_reason":null}]}',
    'data: {"id":"chatcmpl_s","object":"chat.completion.chunk","created":1700000000,"model":"upstream",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":11,"completion_tokens":22,"total_tokens":33}}',
    "data: [DONE]",
]

ANTHRO_SSE_LINES = [
    'data: {"type":"message_start","message":{"id":"msg_s","type":"message","role":"assistant","content":[],'
    '"model":"upstream","stop_reason":null,"usage":{"input_tokens":11,"output_tokens":0}}}',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}',
    'data: {"type":"content_block_stop","index":0}',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":22}}',
    'data: {"type":"message_stop"}',
]

ANTHRO_SSE_BYTES = [(ln + "\n").encode() for ln in ANTHRO_SSE_LINES]

# ─────────────────────────── Doubles amont ───────────────────────────


class FakeResponse:
    """Réponse amont double : expose l'interface consommée par les handlers.

    Non-stream : ``status_code``/``headers``/``json()``/``text``/``content``/``aread()``.
    Stream : ``aiter_lines()`` (P2/P3/P5/P6) et ``aiter_bytes()`` (P1/P4),
    plus le protocole de context manager utilisé par ``_open_free_stream``.
    """

    def __init__(self, status_code=200, payload=None, lines=None, raw_lines=None, ctype=None):
        self.status_code = status_code
        self._payload = payload
        if ctype is None:
            ctype = "application/json" if payload is not None else "text/event-stream"
        self.headers = {"content-type": ctype}
        self._lines = list(lines or [])
        self._raw_lines = list(raw_lines or [])
        self.text = json.dumps(payload) if payload is not None else ""

    async def aread(self):
        return self.content

    @property
    def content(self):
        return self.text.encode()

    def json(self):
        return self._payload

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aiter_bytes(self):
        for line in self._raw_lines:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class UpstreamRecorder:
    """Capture, dans l'ordre, ce qui part réellement vers l'amont.

    Chaque entrée porte le seam emprunté (``http`` / ``free`` / ``pool``),
    l'endpoint, le protocole et une copie profonde du corps — c'est la preuve
    « sur le fil » que la conversion a bien eu lieu au niveau handler.
    """

    def __init__(self, *, upstream=None, free_upstream=None, pool_upstream=None):
        self.calls: list[dict] = []
        self._upstream = upstream or (lambda endpoint, body, proto: FakeResponse(payload=chat_completion()))
        self._free_upstream = free_upstream
        self._pool_upstream = pool_upstream
        self._free_queue: list = []

    # -- configuration des réponses --------------------------------------
    def queue_free(self, *responses):
        """Réponses servies successivement par la jambe free (stream)."""
        self._free_queue.extend(responses)

    def set_upstream(self, fn):
        self._upstream = fn

    # -- lecture ---------------------------------------------------------
    @property
    def last(self) -> dict:
        assert self.calls, "aucun appel amont capturé"
        return self.calls[-1]

    def endpoints(self) -> list[str]:
        return [c["endpoint"] for c in self.calls]

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


def _install_seams(monkeypatch, recorder: UpstreamRecorder):
    """Remplace tous les seams amont + les effets de bord non hermétiques."""

    async def _no_geo_gate(*args, **kwargs):
        return None

    async def _noop_async(*args, **kwargs):
        return None

    async def _no_free(*args, **kwargs):
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
        """En-têtes d'auth factices — jamais de vraie clé."""
        key = (entry or {}).get("api_key", "test-key-A")
        if protocol == "openai":
            return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        return {"x-api-key": key, "Content-Type": "application/json", "anthropic-version": "2023-06-01"}

    monkeypatch.setattr(oc, "_get_auth_headers", _auth_headers, raising=False)

    async def _do_request_with_retry(endpoint, body, headers, protocol, retry_on_429=True):
        recorder._record("http", endpoint, body, protocol)
        return recorder._upstream(endpoint, body, protocol), headers

    @asynccontextmanager
    async def _open_free_stream(endpoint, body, headers, use_free, count_request=True, **kwargs):
        recorder._record("free", endpoint, body, extra={"use_free": bool(use_free)})
        if recorder._free_queue:
            resp = recorder._free_queue.pop(0)
        elif recorder._free_upstream is not None:
            resp = recorder._free_upstream(endpoint, body)
        else:
            resp = recorder._upstream(endpoint, body, "anthropic")
        yield resp

    @asynccontextmanager
    async def _open_via_pool(endpoint, body, headers, is_stream=False, forced_pool=None):
        recorder._record("pool", endpoint, body, extra={"is_stream": bool(is_stream)})
        resp = recorder._pool_upstream(endpoint, body) if recorder._pool_upstream else recorder._upstream(
            endpoint, body, "anthropic"
        )
        yield resp

    monkeypatch.setattr(oc, "_do_request_with_retry", _do_request_with_retry, raising=False)
    monkeypatch.setattr(oc, "_open_free_stream", _open_free_stream, raising=False)
    monkeypatch.setattr(oc, "_open_via_pool", _open_via_pool, raising=False)


class _NullCache:
    """Cache de réponses désactivé : jamais de HIT qui court-circuite l'amont."""

    def make_key(self, *a, **k):
        return None

    def get(self, *a, **k):
        return None

    def put(self, *a, **k):
        return None


@pytest.fixture
def client():
    """Client ASGI sur l'app réelle. Pas de ``with`` : le lifespan démarre des
    pollers réseau qui pendent sous pytest (cf. tests/test_responses_stream_e2e.py)."""
    return TestClient(oc.app)


@pytest.fixture
def recorder(monkeypatch):
    rec = UpstreamRecorder()
    _install_seams(monkeypatch, rec)
    return rec


def _post(client, url, body, stream=False):
    """POST synchrone ; lit le flux complet quand ``stream``."""
    if not stream:
        r = client.post(url, json=body)
        return r.status_code, r.headers.get("content-type", ""), r.text
    with client.stream("POST", url, json=body) as r:
        raw = b"".join(r.iter_bytes())
    return r.status_code, r.headers.get("content-type", ""), raw.decode("utf-8", "replace")


def _sse_events(text: str) -> list[tuple[str, dict]]:
    """Parse un corps SSE en ``[(event_name, payload)]``.

    Accepte les deux formes émises par le proxy : ``event: X\\ndata: {...}``
    (P1/P2, via ``_sse``) et ``data: {...}`` nu (P3/P4/P5/P6).
    """
    events: list[tuple[str, dict]] = []
    name = None
    for line in text.splitlines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            raw = line[5:].strip()
            if raw == "[DONE]":
                events.append(("__done__", {}))
            else:
                try:
                    events.append((name or "", json.loads(raw)))
                except json.JSONDecodeError:
                    pass
            name = None
    return events


def _event_types(text: str) -> list[str]:
    """Types d'événements SSE, dans l'ordre, **hors sentinelle ``[DONE]``**.

    ``[DONE]`` est conservé volontairement en fin de flux par
    ``responses_stream_sse`` (sentinelle de fin pour nos clients existants) :
    ce n'est pas un événement du contrat Responses, il ne doit donc pas
    masquer le vrai terminal ``response.completed``.
    """
    out = []
    for name, payload in _sse_events(text):
        if name == "__done__":
            continue  # sentinelle de fin, pas un événement du contrat
        if name:
            out.append(name)
        else:
            t = payload.get("type")
            if isinstance(t, str):
                out.append(t)
            elif payload:
                out.append("chunk")
    return out


# ═══════════════════════ Matrice 6 chemins × non-stream ═══════════════════════


def test_p1_messages_anthropic_nonstream(client, recorder):
    """P1 — /v1/messages → anthropic : passthrough du corps, réponse relayée."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=anthro_message("P1 ok")))
    body = {"model": PAID_ANTHRO, "max_tokens": 256, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/messages", body)

    assert status == 200
    assert ctype.startswith("application/json")
    assert recorder.last["endpoint"] == EP_ANTHRO
    assert recorder.last["protocol"] == "anthropic"
    assert recorder.last["body"]["model"] == PAID_ANTHRO
    assert recorder.last["body"]["messages"] == [{"role": "user", "content": "hi"}]
    # Réponse Anthropic relayée telle quelle (pas de conversion sur P1).
    payload = json.loads(text)
    assert payload["type"] == "message"
    assert payload["content"][0]["text"] == "P1 ok"
    assert payload["usage"]["input_tokens"] == 11
    assert payload["usage"]["output_tokens"] == 22


def test_p2_messages_to_openai_nonstream(client, recorder):
    """P2 — /v1/messages → openai : conversion aller ET retour prouvées."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=chat_completion("P2 ok")))
    body = {
        "model": PAID_CHAT,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "name": "get_weather",
                "description": "d",
                "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
            }
        ],
    }

    status, ctype, text = _post(client, "/v1/messages", body)

    assert status == 200
    assert ctype.startswith("application/json")
    # Aller : endpoint OpenAI chat + corps converti au format Chat Completions.
    assert recorder.last["endpoint"] == EP_CHAT
    assert recorder.last["protocol"] == "openai"
    up = recorder.last["body"]
    assert up["model"] == PAID_CHAT
    assert up["messages"][0]["role"] == "user"
    assert up["tools"][0]["type"] == "function"
    assert up["tools"][0]["function"]["name"] == "get_weather"
    assert "input_schema" not in json.dumps(up["tools"])
    # Retour : réponse Chat → Anthropic Messages.
    payload = json.loads(text)
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    assert payload["content"][0]["type"] == "text"
    assert payload["content"][0]["text"] == "P2 ok"
    assert payload["usage"]["input_tokens"] == 11
    assert payload["usage"]["output_tokens"] == 22


def test_p3_chat_passthrough_nonstream(client, recorder):
    """P3 — /v1/chat/completions → openai : passthrough, réponse inchangée."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=chat_completion("P3 ok")))
    body = {"model": PAID_CHAT, "max_tokens": 256, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/chat/completions", body)

    assert status == 200
    assert ctype.startswith("application/json")
    assert recorder.last["endpoint"] == EP_CHAT
    assert recorder.last["protocol"] == "openai"
    assert recorder.last["body"]["model"] == PAID_CHAT
    assert recorder.last["body"]["messages"] == [{"role": "user", "content": "hi"}]
    # Passthrough : la réponse Chat arrive telle quelle au client.
    payload = json.loads(text)
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "P3 ok"
    assert payload["usage"]["total_tokens"] == 33


def test_p4_chat_to_anthropic_nonstream(client, recorder):
    """P4 — /v1/chat/completions → anthropic : double conversion aller/retour."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=anthro_message("P4 ok")))
    body = {"model": PAID_ANTHRO, "max_tokens": 256, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/chat/completions", body)

    assert status == 200
    assert ctype.startswith("application/json")
    # Aller : endpoint Anthropic + corps converti au format Messages.
    assert recorder.last["endpoint"] == EP_ANTHRO
    assert recorder.last["protocol"] == "anthropic"
    up = recorder.last["body"]
    assert up["model"] == PAID_ANTHRO
    assert isinstance(up["messages"][0]["content"], list)
    assert up["messages"][0]["content"][0] == {"type": "text", "text": "hi"}
    assert up["stream"] is False
    # Retour : réponse Anthropic → Chat Completions.
    payload = json.loads(text)
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "P4 ok"
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"]["prompt_tokens"] == 11
    assert payload["usage"]["completion_tokens"] == 22


def test_p5_responses_to_openai_nonstream(client, recorder):
    """P5 — /v1/responses → openai/responses : conversion A→chat→Responses."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=responses_object("P5 ok")))
    body = {
        "model": PAID_RESPONSES,
        "max_output_tokens": 512,
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "store": True,
        "truncation": "auto",
    }

    status, ctype, text = _post(client, "/v1/responses", body)

    assert status == 200
    assert ctype.startswith("application/json")
    assert recorder.last["endpoint"] == EP_RESPONSES
    assert recorder.last["protocol"] == "openai"
    up = recorder.last["body"]
    assert up["model"] == PAID_RESPONSES
    # Format Responses natif (pas de clé "messages").
    assert "input" in up and "messages" not in up
    assert up["stream"] is False
    # [Lot L15 — B5] store/truncation relayés depuis le corps client.
    assert up.get("store") is True
    assert up.get("truncation") == "auto"
    payload = json.loads(text)
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["output"][0]["content"][0]["text"] == "P5 ok"
    assert payload["usage"]["input_tokens"] == 11


def test_p6_responses_to_anthropic_nonstream(client, recorder):
    """P6 — /v1/responses → anthropic : conversion Responses→Messages→Responses."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=anthro_message("P6 ok")))
    body = {
        "model": PAID_ANTHRO,
        "max_output_tokens": 512,
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
    }

    status, ctype, text = _post(client, "/v1/responses", body)

    assert status == 200
    assert ctype.startswith("application/json")
    # Aller : endpoint Anthropic + corps Messages.
    assert recorder.last["endpoint"] == EP_ANTHRO
    assert recorder.last["protocol"] == "anthropic"
    up = recorder.last["body"]
    assert up["model"] == PAID_ANTHRO
    assert "messages" in up
    assert up["messages"][0]["content"][0]["type"] == "text"
    # Retour : réponse Anthropic → objet Responses.
    payload = json.loads(text)
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["output"][0]["content"][0]["type"] == "output_text"
    assert payload["output"][0]["content"][0]["text"] == "P6 ok"
    assert payload["usage"]["input_tokens"] == 11
    assert payload["usage"]["output_tokens"] == 22


# ═══════════════════════ Matrice 6 chemins × stream ═══════════════════════


def test_p1_messages_anthropic_stream(client, recorder):
    """P1 stream — SSE Anthropic relayé, content-type & séquence attendus."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(lines=ANTHRO_SSE_LINES, raw_lines=ANTHRO_SSE_BYTES))
    body = {"model": PAID_ANTHRO, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/messages", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.calls, "l'amont doit être appelé en streaming"
    assert recorder.last["endpoint"] == EP_ANTHRO
    types = _event_types(text)
    for expected in ("message_start", "content_block_start", "content_block_delta", "content_block_stop"):
        assert expected in types, f"{expected} manquant dans {types}"
    assert "hello" in text


def test_p2_messages_to_openai_stream(client, recorder):
    """P2 stream — amont chat consommé en SSE, ré-émis en SSE Anthropic."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(lines=CHAT_CHUNK_LINES))
    body = {"model": PAID_CHAT, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/messages", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.last["endpoint"] == EP_CHAT
    up = recorder.last["body"]
    assert up["model"] == PAID_CHAT
    assert up["stream"] is True
    # Conversion aller : outils Anthropic → fonction Chat si présents.
    types = _event_types(text)
    assert "message_start" in types
    assert "message_stop" in types
    assert types.index("message_start") < types.index("message_stop")
    assert "hello" in text


def test_p3_chat_passthrough_stream(client, recorder):
    """P3 stream — passthrough SSE Chat + terminal ``data: [DONE]``."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(lines=CHAT_CHUNK_LINES))
    body = {"model": PAID_CHAT, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/chat/completions", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.last["endpoint"] == EP_CHAT
    assert recorder.last["body"]["stream"] is True
    # stream_options ajouté pour obtenir l'usage en fin de flux.
    assert recorder.last["body"].get("stream_options") == {"include_usage": True}
    assert "data: [DONE]" in text
    assert "hello" in text


def test_p4_chat_to_anthropic_stream(client, recorder):
    """P4 stream — amont Anthropic converti en chunks Chat + ``[DONE]``.

    Échoue aujourd'hui : ``opencode.py:12570`` omet ``anthro_body`` du
    ``nonlocal``, l'amont n'est jamais appelé et le client reçoit 0 octet.
    """
    recorder.set_upstream(lambda e, b, p: FakeResponse(lines=ANTHRO_SSE_LINES, raw_lines=ANTHRO_SSE_BYTES))
    body = {"model": PAID_ANTHRO, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/chat/completions", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.calls, "l'amont doit être appelé en streaming sur P4"
    assert recorder.last["endpoint"] == EP_ANTHRO
    assert "data: [DONE]" in text
    assert "hello" in text


def test_p5_responses_to_openai_stream(client, recorder):
    """P5 stream — amont chat collecté puis émis en SSE Responses."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(lines=CHAT_CHUNK_LINES))
    body = {
        "model": PAID_RESPONSES,
        "max_output_tokens": 512,
        "stream": True,
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
    }

    status, ctype, text = _post(client, "/v1/responses", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.last["endpoint"] == EP_RESPONSES
    types = _event_types(text)
    assert "response.created" in types
    # ``response.completed`` est le dernier ÉVÉNEMENT du contrat Responses.
    # La sentinelle ``[DONE]`` qui suit est ajoutée par ``responses_stream_sse``
    # et exclue par ``_event_types`` (ce n'est pas un événement de la spec).
    assert types[-1] == "response.completed"
    assert "data: [DONE]" in text
    assert "hello" in text


def test_p6_responses_to_anthropic_stream(client, recorder):
    """P6 stream — amont Anthropic (corps JSON) puis séquence SSE Responses.

    Note de contrat : P6-stream est **buffer-then-emit**. Le handler lit
    ``resp.json()`` (``opencode.py:13576``) puis émet une séquence Responses
    complète via ``responses_stream_events`` — il ne consomme **pas** de flux
    SSE amont. Le double amont doit donc renvoyer un corps **JSON**, pas des
    octets SSE : sinon le chemin « buffer-then-emit » n'est pas exercé.
    """
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=anthro_message("hello")))
    body = {
        "model": PAID_ANTHRO,
        "max_output_tokens": 512,
        "stream": True,
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
    }

    status, ctype, text = _post(client, "/v1/responses", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.last["endpoint"] == EP_ANTHRO
    types = _event_types(text)
    assert "response.created" in types
    assert types[-1] == "response.completed"
    assert "data: [DONE]" in text
    assert "hello" in text


# ═══════════════════════ Sous-chemin free-model (stream) ═══════════════════════


def test_free_model_subpath_p2_stream(client, recorder):
    """P2 stream — modèle client à équivalent free : l'amont reçoit l'endpoint
    FREE et le modèle swappé, via la jambe ``_open_free_stream`` (jamais la
    payante). Vérifie l'effet réel sur le fil, pas seulement la table."""
    target = oc._route_for(FREE_CLIENT_P2)["model"]
    free_model = oc._resolve_free_model(target)
    assert free_model, f"{target} doit avoir un équivalent free pour ce test"
    recorder.queue_free(FakeResponse(lines=CHAT_CHUNK_LINES))
    body = {"model": FREE_CLIENT_P2, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/messages", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.calls, "la jambe free doit appeler l'amont"
    free_call = recorder.calls[0]
    assert free_call["seam"] == "free"
    assert free_call["use_free"] is True
    assert free_call["endpoint"] == _cfg_settings._free_endpoint_for(free_model)
    assert free_call["endpoint"] == EP_FREE_CHAT
    # Le modèle PAYANT envoyé par le client a bien été remplacé par le free.
    assert free_call["body"]["model"] == free_model
    assert free_call["body"]["model"] != target
    assert "hello" in text


def test_free_model_subpath_p4_stream(client, recorder):
    """P4 stream — même swap free attendu ; bloqué par le bug ``anthro_body``
    (échec sur la jambe free à opencode.py:12576, sans même un log)."""
    target = oc._route_for(FREE_CLIENT_P4)["model"]
    free_model = oc._resolve_free_model(target)
    assert free_model, f"{target} doit avoir un équivalent free pour ce test"
    recorder.queue_free(FakeResponse(lines=ANTHRO_SSE_LINES, raw_lines=ANTHRO_SSE_BYTES))
    body = {"model": FREE_CLIENT_P4, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/chat/completions", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.calls, "la jambe free doit appeler l'amont"
    free_call = recorder.calls[0]
    assert free_call["seam"] == "free"
    assert free_call["use_free"] is True
    assert free_call["body"]["model"] == free_model
    assert "data: [DONE]" in text


# ═══════════════════════ Sous-chemin failover (stream) ═══════════════════════


@pytest.fixture
def two_paid_keys(monkeypatch):
    """Deux clés payantes saines : condition ``len(API_KEYS) > 1`` du failover."""
    keys = [
        {"api_key": "sk-ant-e2e-key-AAAA", "alias": "e2e-A", "enabled": True},
        {"api_key": "sk-ant-e2e-key-BBBB", "alias": "e2e-B", "enabled": True},
    ]
    monkeypatch.setattr(oc, "API_KEYS", keys, raising=False)
    monkeypatch.setattr(oc, "_key_pauser", oc._KeyPauser(), raising=False)
    monkeypatch.setattr(oc, "_pause_key_for_quota_reset", _noop_awaitable, raising=False)
    oc._rebuild_key_cache()
    return keys


async def _noop_awaitable(*args, **kwargs):
    return None


def test_failover_p2_stream_429_then_success(client, recorder, two_paid_keys):
    """P2 stream — 1er amont 429 : la clé alternative est utilisée et le flux
    client aboutit quand même (le failover ne casse pas la SSE)."""
    calls = {"n": 0}

    def upstream(endpoint, body, proto):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(status_code=429, payload={"error": "rate limited"})
        return FakeResponse(lines=CHAT_CHUNK_LINES)

    recorder.set_upstream(upstream)
    body = {"model": PAID_CHAT, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/messages", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert calls["n"] >= 2, "le failover doit réémettre une requête amont"
    assert "hello" in text


def test_failover_p4_stream_429_then_success(client, recorder, two_paid_keys):
    """P4 stream — même failover ; bloqué par le bug ``anthro_body`` qui
    empêche tout appel amont et toute émission d'octets."""
    calls = {"n": 0}

    def upstream(endpoint, body, proto):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(status_code=429, payload={"error": "rate limited"})
        return FakeResponse(lines=ANTHRO_SSE_LINES, raw_lines=ANTHRO_SSE_BYTES)

    recorder.set_upstream(upstream)
    body = {"model": PAID_ANTHRO, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/chat/completions", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert calls["n"] >= 2, "le failover doit réémettre une requête amont"
    assert "data: [DONE]" in text


# ═══════════════════════ Corpus round-trip (V4) ═══════════════════════


@pytest.mark.parametrize("case", CORPUS, ids=[c["name"] for c in CORPUS])
def test_corpus_round_trip_p2_conversion_preserves_content(case, client, recorder):
    """V4 — corpus aller (P2) : la requête convertie préserve le contenu
    essentiel (aucun texte perdu/dupliqué, ids d'outils et pièces jointes
    conservés) puis la réponse reconvertie restitue texte et usage."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=chat_completion("corpus ok")))
    body = dict(case["anthro"])
    body["model"] = PAID_CHAT

    status, _ctype, text = _post(client, "/v1/messages", body)

    assert status == 200
    assert recorder.last["endpoint"] == EP_CHAT
    upstream = recorder.last["body"]
    serialized = json.dumps(upstream, ensure_ascii=False)

    # Texte du corpus présent exactement une fois (ni tronqué ni dupliqué).
    for needle in _needles(case):
        assert needle in serialized, f"{case['name']}: {needle!r} absent du corps amont"
        assert serialized.count(needle) >= 1

    # Invariants par cas.
    if case is CORPUS_TOOLS:
        assert case["tool_id"] in serialized
        assert case["tool_result_text"] in serialized
        for name in case["tool_names"]:
            assert name in serialized, f"outil {name} perdu à la conversion"
        # tool_use → tool_calls avec le même id, tool_result → role tool.
        assert any(
            m.get("role") == "assistant" and m.get("tool_calls") for m in upstream["messages"]
        ), "tool_calls attendu côté assistant"
        assert any(m.get("role") == "tool" for m in upstream["messages"]), "message tool attendu"
    if case is CORPUS_IMAGES:
        assert serialized.count("image_url") >= case["image_count"]
        assert case["url"] in serialized
    if case is CORPUS_DOCUMENTS:
        # Le document part en ``file`` avec media_type préservé.
        assert "data:application/pdf;base64," in serialized
        assert case["data_fragment"] in serialized
        # [A25] Le nom de fichier du client doit SURVIVRE. Le champ Anthropic
        # est ``title`` (``DocumentBlockParam`` : source/type/cache_control/
        # citations/context/title) ; le code ne lisait que ``name`` et retombait
        # donc toujours sur ``document.pdf``, perdant ``rapport.pdf``.
        # On asserte la VALEUR, pas la simple présence de la clé.
        assert case["title"] in serialized, (
            f"nom de fichier client perdu : {case['title']!r} absent "
            "(le document est retombé sur le défaut 'document.pdf')"
        )
    if case is CORPUS_REASONING:
        assert case["reasoning_text"] in serialized
        assert case["answer"] in serialized

    # Retour : la réponse reconvertie restitue le texte et les compteurs.
    payload = json.loads(text)
    assert payload["type"] == "message"
    assert payload["content"][0]["text"] == "corpus ok"
    assert payload["usage"]["input_tokens"] == 11
    assert payload["usage"]["output_tokens"] == 22


@pytest.mark.parametrize("case", CORPUS, ids=[c["name"] for c in CORPUS])
def test_corpus_round_trip_p4_conversion_preserves_content(case, client, recorder):
    """V4 — corpus aller (P4) : conversion Chat→Messages préserve le contenu
    et la réponse Anthropic est reconvertie en Chat sans perte d'usage."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=anthro_message("corpus ok")))
    anthro = dict(case["anthro"])
    # P4 part d'un corps Anthropic : on passe par le même corpus et on vérifie
    # le corps Messages reçu par l'amont anthropic.
    body = {
        "model": PAID_ANTHRO,
        "max_tokens": anthro.get("max_tokens", 512),
        "messages": [{"role": "user", "content": "corpus P4"}],
    }

    status, _ctype, text = _post(client, "/v1/chat/completions", body)

    assert status == 200
    assert recorder.last["endpoint"] == EP_ANTHRO
    upstream = recorder.last["body"]
    assert upstream["messages"][0]["content"][0] == {"type": "text", "text": "corpus P4"}
    assert "corpus P4" in json.dumps(upstream, ensure_ascii=False)

    payload = json.loads(text)
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "corpus ok"
    # Usage préservé au retour.
    assert payload["usage"]["prompt_tokens"] == 11
    assert payload["usage"]["completion_tokens"] == 22
    assert payload["usage"]["total_tokens"] == 33


def test_corpus_tool_ids_survive_p2_round_trip(client, recorder):
    """V4 — invariant fort : les ids d'outils traversent l'aller sans être
    régénérés (sinon le tool_result ne raccroche plus au tool_use)."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=chat_completion("ok", tool_calls=True)))
    body = dict(CORPUS_TOOLS["anthro"])
    body["model"] = PAID_CHAT

    status, _ctype, text = _post(client, "/v1/messages", body)

    assert status == 200
    upstream = recorder.last["body"]
    assistant = [m for m in upstream["messages"] if m.get("role") == "assistant" and m.get("tool_calls")]
    assert assistant, "tool_calls attendu"
    assert assistant[0]["tool_calls"][0]["id"] == CORPUS_TOOLS["tool_id"]
    assert assistant[0]["tool_calls"][0]["function"]["name"] == "get_weather"
    tool_msg = [m for m in upstream["messages"] if m.get("role") == "tool"]
    assert tool_msg and tool_msg[0]["tool_call_id"] == CORPUS_TOOLS["tool_id"]

    # Retour : le tool_use reconverti garde aussi un id exploitable.
    payload = json.loads(text)
    tool_blocks = [b for b in payload["content"] if b.get("type") == "tool_use"]
    assert tool_blocks and tool_blocks[0]["name"] == "get_weather"


def test_corpus_usage_not_duplicated_on_p3_passthrough(client, recorder):
    """V4 — passthrough P3 : les compteurs d'usage ne sont ni perdus ni doublés."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=chat_completion("ok")))
    body = {"model": PAID_CHAT, "max_tokens": 256, "messages": [{"role": "user", "content": "hi"}]}

    status, _ctype, text = _post(client, "/v1/chat/completions", body)

    assert status == 200
    payload = json.loads(text)
    assert payload["usage"] == {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}


def _needles(case: dict) -> list[str]:
    """Chaînes qui doivent survivre à l'aller, par cas de corpus."""
    if case is CORPUS_TEXT:
        return [case["text"]]
    if case is CORPUS_TOOLS:
        return [case["tool_result_text"]]
    if case is CORPUS_IMAGES:
        return ["Décris ces deux images."]
    if case is CORPUS_DOCUMENTS:
        return ["Résume ce document."]
    if case is CORPUS_REASONING:
        return [case["answer"]]
    return []


def test_corpus_is_wired_to_real_models():
    """Garde-fou : les modèles choisis routent bien vers le protocole visé.

    Empêche qu'un changement de table de routage transforme silencieusement un
    test de chemin en test d'un autre chemin (les tests resteraient verts tout
    en ne prouvant plus rien).
    """
    assert _cfg_settings.get_model_config(PAID_ANTHRO)["protocol"] == "anthropic"
    assert _cfg_settings.get_model_config(PAID_CHAT)["protocol"] == "openai"
    assert _cfg_settings.get_model_config(PAID_RESPONSES)["protocol"] == "openai"
    assert _cfg_settings.get_model_config(PAID_ANTHRO)["endpoint"] == EP_ANTHRO
    assert _cfg_settings.get_model_config(PAID_CHAT)["endpoint"] == EP_CHAT
    assert _cfg_settings.get_model_config(PAID_RESPONSES)["endpoint"] == EP_RESPONSES
    # P1/P2/P3/P4/P6 sont exercés sur la jambe PAYANTE : les modèles choisis ne
    # doivent pas avoir d'équivalent free, sinon le swap masquerait le chemin
    # de conversion testé. (P5 est traité en non-stream avec « store » et son
    # modèle a un équivalent free : la jambe payante y est forcée par le stub
    # de ``_try_free_model_first`` qui renvoie None.)
    assert oc._resolve_free_model(PAID_ANTHRO) is None
    assert oc._resolve_free_model(PAID_CHAT) is None
    assert oc._resolve_free_model(PAID_RESPONSES) is not None, (
        "muse-spark-1.3-contributor a un équivalent free : les tests P5 s'appuient "
        "sur le stub de _try_free_model_first pour rester sur la jambe payante"
    )
    # Les modèles clients des sous-chemins free DOIVENT avoir un équivalent.
    assert oc._resolve_free_model(oc._route_for(FREE_CLIENT_P2)["model"])
    assert oc._resolve_free_model(oc._route_for(FREE_CLIENT_P4)["model"])


# ═══════════════════════════════════════════════════════════════════════════
# [Lot L13 — point B1] Contrat `reasoning_content` + repli upstream strict
# ═══════════════════════════════════════════════════════════════════════════
# `reasoning_content` n'est dans AUCUNE spec OpenAI (le `delta` officiel ne
# connaît que content/function_call/refusal/role/tool_calls) : c'est une
# convention vendeur (DeepSeek/GLM/Kimi). Le proxy en fait LE transport du
# raisonnement vers les cibles Chat. Un upstream STRICT peut donc le rejeter en
# 400/422 : on rejoue alors UNE fois sans le champ, au lieu de casser le tour
# entier pour tous les clients de l'endpoint.
# Contrat complet : `docs/reasoning-content-contract.md`.


def _reasoning_history() -> list[dict]:
    """Historique multi-tours où l'assistant a raisonné au tour 1."""
    return [
        {"role": "user", "content": "Question 1"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "RAISONNEMENT-1", "signature": "sig-locale"},
                {"type": "text", "text": "Reponse 1"},
            ],
        },
        {"role": "user", "content": "Question 2"},
    ]


def test_l13_reasoning_content_travels_to_chat_upstream(client, recorder):
    """B1 — vers un upstream Chat, le raisonnement devient `reasoning_content`.

    C'est le contrat « normal » (écosystème réel) : la mémoire du raisonnement
    doit survivre au tour suivant.
    """
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=chat_completion("ok")))
    body = {"model": PAID_CHAT, "max_tokens": 256, "messages": _reasoning_history()}

    status, _ctype, _text = _post(client, "/v1/messages", body)

    assert status == 200
    up = recorder.last["body"]
    carriers = [m for m in up["messages"] if isinstance(m, dict) and m.get("reasoning_content")]
    assert carriers, "le raisonnement doit voyager en `reasoning_content` vers Chat"
    assert carriers[0]["reasoning_content"] == "RAISONNEMENT-1"
    # La signature locale ne franchit JAMAIS la frontière vers Chat.
    assert "sig-locale" not in json.dumps(up)


def test_l13_marker_never_reaches_the_wire(client, recorder):
    """Le marqueur de repli est interne : il ne doit pas partir à l'amont.

    Un champ `_has_...` inconnu ferait exactement le 400 qu'on cherche à éviter.
    Attention au niveau observé : le seam ``http`` capture le **dict** avant
    sérialisation, donc le marqueur y est normalement PRÉSENT (c'est le
    ``_serialize_json_body`` du handler qui le retire). On vérifie donc les
    octets réellement sérialisés, pas le dict du recorder.
    """
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=chat_completion("ok")))
    body = {"model": PAID_CHAT, "max_tokens": 256, "messages": _reasoning_history()}

    status, _ctype, _text = _post(client, "/v1/messages", body)

    assert status == 200
    up = recorder.last["body"]
    # Le marqueur est bien posé par le convertisseur...
    assert up.get(oc._HAS_SYNTHETIC_REASONING_KEY) is True
    # ...mais il disparaît à la sérialisation (c'est ce qui part sur le fil).
    wire = oc._serialize_json_body(up).decode("utf-8")
    assert oc._HAS_SYNTHETIC_REASONING_KEY not in wire
    assert "_has_synthetic" not in wire
    # ...tandis que le raisonnement lui-même est bien transmis.
    assert "RAISONNEMENT-1" in wire


def test_l13_strict_upstream_400_triggers_retry_without_reasoning(client, recorder):
    """B1 — upstream strict : 400 sur `reasoning_content` → retry-once sans.

    Le tour doit ABOUTIR (texte + tool calls) plutôt qu'échouer : le
    raisonnement est un enrichissement, jamais un bloquant. On perd la mémoire
    du raisonnement, pas la réponse.
    """
    calls = {"n": 0}

    def strict_upstream(endpoint, body, protocol):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(status_code=400, payload={"error": {"message": "unknown field reasoning_content"}})
        return FakeResponse(payload=chat_completion("ok après repli"))

    recorder.set_upstream(strict_upstream)
    body = {"model": PAID_CHAT, "max_tokens": 256, "messages": _reasoning_history()}

    status, _ctype, text = _post(client, "/v1/messages", body)

    # Deux appels amont : le 400, puis le retry.
    assert len(recorder.calls) == 2, f"attendu 2 appels amont, obtenu {len(recorder.calls)}"
    first, second = recorder.calls[0]["body"], recorder.calls[1]["body"]
    # Le 1er portait le raisonnement, le 2nd non.
    assert "RAISONNEMENT-1" in json.dumps(first)
    assert "RAISONNEMENT-1" not in json.dumps(second)
    assert not any(
        isinstance(m, dict) and "reasoning_content" in m for m in second["messages"]
    ), "le retry doit avoir retiré `reasoning_content` de TOUS les messages"
    # Et le tour aboutit quand même.
    assert status == 200
    assert json.loads(text)["content"][0]["text"] == "ok après repli"


def test_l13_retry_is_once_only(client, recorder):
    """Garde-fou : le retry ne boucle pas si l'upstream rejette encore.

    Un upstream qui rejetterait aussi la version sans raisonnement ne doit pas
    déclencher de retry infini — on rend l'erreur telle quelle.
    """
    calls = {"n": 0}

    def always_400(endpoint, body, protocol):
        calls["n"] += 1
        return FakeResponse(status_code=400, payload={"error": {"message": "still bad"}})

    recorder.set_upstream(always_400)
    body = {"model": PAID_CHAT, "max_tokens": 256, "messages": _reasoning_history()}

    status, _ctype, _text = _post(client, "/v1/messages", body)

    assert len(recorder.calls) == 2, f"le retry doit être UNIQUE, or {len(recorder.calls)} appels"
    assert status == 400


def test_l13_no_marker_means_no_retry_for_plain_requests(client, recorder):
    """Sans raisonnement dans l'historique, un 400 ne doit PAS déclencher de retry.

    Sinon on rejouerait deux fois toute requête en erreur, doublant le coût et
    masquant la vraie cause du 400.
    """
    recorder.set_upstream(lambda e, b, p: FakeResponse(status_code=400, payload={"error": {"message": "bad"}}))
    body = {
        "model": PAID_CHAT,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "salut"}],
    }

    status, _ctype, _text = _post(client, "/v1/messages", body)

    assert len(recorder.calls) == 1, "aucun retry ne doit avoir lieu sans raisonnement"
    assert status == 400
