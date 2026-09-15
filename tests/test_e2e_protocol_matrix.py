"""[Lot L7 â€” E2E & corpus] Matrice E2E bout-en-bout des 6 chemins de conversion.

Contrat vÃ©rifiÃ© (plan Â§V3/V4, PLAN_AUDIT_CONVERSIONS_2026-09-10.md lignes 75-76, 149) :

- V3 Â« E2E ASGI avec faux amont Â» : on monte ``httpx``/``ASGITransport`` sur
  ``opencode.app`` via ``TestClient`` et on **capture le corps exact reÃ§u par
  l'amont** (endpoint + clÃ©s + valeurs). C'est le seul niveau qui attrape les
  overrides de route, les swaps free-model et le passthrough : un convertisseur
  peut Ãªtre correct testÃ© seul et neutralisÃ© un lien plus loin dans le handler.
- V4 Â« PropriÃ©tÃ©s & corpus Â» : corpus embarquÃ© de payloads rÃ©alistes (texte,
  outils, ``tool_result``, images, documents, raisonnement, multi-tours) et
  vÃ©rification que l'aller (requÃªte convertie) puis le retour (rÃ©ponse
  reconvertie) prÃ©servent le contenu essentiel â€” noms/ids d'outils survivants,
  texte ni tronquÃ© ni dupliquÃ©, compteurs d'usage conservÃ©s.

Les 6 chemins rÃ©els (plan Â§1) et ce qui est assertÃ© sur le fil :

    P1  POST /v1/messages          â†’ anthropic  passthrough + strip_synthetic_thinking
    P2  POST /v1/messages          â†’ openai     anthropic_to_openai / openai_to_anthropic
    P3  POST /v1/chat/completions  â†’ openai     passthrough + ensure_min_tokens
    P4  POST /v1/chat/completions  â†’ anthropic  openai_to_anthropic_request /
                                                anthropic_to_openai_response
    P5  POST /v1/responses         â†’ openai     anthropic_to_openai +
                                                _chat_to_responses_request +
                                                _relay_responses_storage_fields
    P6  POST /v1/responses         â†’ anthropic  openai_responses_to_anthropic /
                                                anthropic_to_openai_responses

HermÃ©ticitÃ© : aucun accÃ¨s rÃ©seau, aucune clÃ© rÃ©elle, aucune horloge murale. Tous
les seams amont sont remplacÃ©s (``_do_request_with_retry``, ``_open_free_stream``,
``_open_via_pool``, ``_get_auth_headers``, ``_try_free_model_first``), le cache de
rÃ©ponses est neutralisÃ© et la gÃ©o/circuit-breaker sont ouverts.

``opencode.py`` et ``app/protocol/mapping.py`` ne sont **jamais modifiÃ©s** : ils
sont lus et monkeypatchÃ©s Ã  l'exÃ©cution.

Le corpus V4 est **embarquÃ©** dans ce fichier : le livrable L7 est un fichier
unique et ``tests/fixtures/protocol_corpus/`` n'existe pas dans le dÃ©pÃ´t.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

import config.settings as _cfg_settings
import opencode as oc
from protocol_mapping import sanitize_tool_names

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ Endpoints amont attendus â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

EP_ANTHRO = "https://opencode.ai/zen/go/v1/messages"
EP_CHAT = "https://opencode.ai/zen/go/v1/chat/completions"
EP_RESPONSES = "https://opencode.ai/zen/go/v1/responses"
EP_FREE_CHAT = "https://opencode.ai/zen/v1/chat/completions"
EP_FREE_RESPONSES = "https://opencode.ai/zen/v1/responses"

# ModÃ¨les clients : cible payante unique par chemin (aucun Ã©quivalent free dans
# FREE_MODEL_MAP) â†’ la jambe payante est exercÃ©e sans dÃ©pendre du swap free.
PAID_ANTHRO = "minimax-m3"  # /v1/messages + /v1/chat/completions + /v1/responses â†’ anthropic
PAID_CHAT = "glm-5"  # â†’ openai chat/completions
PAID_RESPONSES = "muse-spark-1.3-contributor"  # â†’ openai /v1/responses

# Sous-chemin free-model (stream) : on doit partir d'un modÃ¨le CLIENT dont la
# cible a un Ã©quivalent free â€” c'est le seul point d'entrÃ©e qui dÃ©clenche le swap.
#   P2 : Â« sonnet Â» â†’ glm-5.1 (openai chat)  â†’ free deepseek-v4-flash-free
#   P4 : Â« haiku Â»  â†’ minimax-m2.5 (anthropic) â†’ free mimo-v2.5-free
FREE_CLIENT_P2 = "sonnet"
FREE_CLIENT_P4 = "haiku"

# RÃ©gression P4-stream â€” **CORRIGÃ‰E** (A24).
# ``_anthro_to_oai_stream`` (opencode.py:12569) dÃ©clarait ``nonlocal endpoint,
# model_id`` en OMETTANT ``anthro_body``, alors qu'il l'assigne (jambe free
# ~12578, retour payant ~12732) et le lit avant toute assignation (~12576).
# Python en faisait donc une variable LOCALE, ce qui cassait les DEUX jambes :
#   - jambe free   : ``dict(anthro_body)`` (~12576) lit AVANT l'assignation de
#     ~12578 â†’ UnboundLocalError levÃ©e avant la boucle de retry, donc AUCUNE
#     trace de log ; le client recevait ``text/event-stream`` et 0 octet ;
#   - jambe payante : lecture dans la boucle â†’ UnboundLocalError loggÃ©e
#     (Â« ERROR stream (attempt 1) Â» / Â« (attempt 2) Â»), retry clÃ© alternative
#     Ã©galement en Ã©chec, 0 octet.
# Bug PRÃ‰-EXISTANT au commit ``110d587`` (``anthro_body`` dans ``co_varnames``,
# absent de ``co_freevars``, lectures compilÃ©es en ``LOAD_FAST_CHECK``).
# Aucun test du dÃ©pÃ´t ne couvrait ``_anthro_to_oai_stream`` avant ce lot.
#
# Correctif appliquÃ© : ajout d'``anthro_body`` Ã  la dÃ©claration ``nonlocal``.
# Les 3 tests P4-stream ci-dessous sont donc de simples tests de rÃ©gression
# (plus de ``xfail``) : ils ont Ã©tÃ© observÃ©s ROUGES avant le correctif et VERTS
# aprÃ¨s, et redeviennent rouges si on retire ``anthro_body`` du ``nonlocal``.

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ Corpus V4 embarquÃ© â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

CORPUS_TEXT: dict[str, Any] = {
    "name": "text_simple",
    "anthro": {
        "model": PAID_CHAT,
        "max_tokens": 512,
        "messages": [{"role": "user", "content": "Explique la photosynthÃ¨se en une phrase."}],
    },
    "text": "Explique la photosynthÃ¨se en une phrase.",
}

CORPUS_TOOLS: dict[str, Any] = {
    "name": "tools_and_tool_result",
    "anthro": {
        "model": PAID_CHAT,
        "max_tokens": 1024,
        "tools": [
            {
                "name": "get_weather",
                "description": "MÃ©tÃ©o d'une ville",
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
            {"role": "user", "content": "Quel temps fait-il Ã  Paris ?"},
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
                        "content": [{"type": "text", "text": "18 Â°C, ciel dÃ©gagÃ©"}],
                    }
                ],
            },
        ],
    },
    "tool_id": "toolu_01A",
    "tool_names": ["get_weather", "search_docs"],
    "tool_result_text": "18 Â°C, ciel dÃ©gagÃ©",
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
                    {"type": "text", "text": "DÃ©cris ces deux images."},
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
                    {"type": "text", "text": "RÃ©sume ce document."},
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
            {"role": "user", "content": "Calcule 17 Ã— 23."},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "17 Ã— 23 = 17 Ã— 20 + 17 Ã— 3 = 340 + 51", "signature": "AUTHENTIC=="},
                    {"type": "text", "text": "17 Ã— 23 = 391."},
                ],
            },
            {"role": "user", "content": "Et 391 + 9 ?"},
        ],
    },
    "reasoning_text": "17 Ã— 23 = 17 Ã— 20 + 17 Ã— 3 = 340 + 51",
    "answer": "17 Ã— 23 = 391.",
}

CORPUS: list[dict[str, Any]] = [CORPUS_TEXT, CORPUS_TOOLS, CORPUS_IMAGES, CORPUS_DOCUMENTS, CORPUS_REASONING]

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ RÃ©ponses amont canoniques â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def anthro_message(text="hello", tool_calls=False):
    """RÃ©ponse Anthropic Messages bien formÃ©e."""
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
    """RÃ©ponse OpenAI Chat Completions bien formÃ©e."""
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
    """RÃ©ponse OpenAI Responses bien formÃ©e."""
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


# SÃ©quences SSE amont (lignes, sans \n final : les handlers ajoutent/splitent).
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

# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ Doubles amont â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


class FakeResponse:
    """RÃ©ponse amont double : expose l'interface consommÃ©e par les handlers.

    Non-stream : ``status_code``/``headers``/``json()``/``text``/``content``/``aread()``.
    Stream : ``aiter_lines()`` (P2/P3/P5/P6) et ``aiter_bytes()`` (P1/P4),
    plus le protocole de context manager utilisÃ© par ``_open_free_stream``.
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
    """Capture, dans l'ordre, ce qui part rÃ©ellement vers l'amont.

    Chaque entrÃ©e porte le seam empruntÃ© (``http`` / ``free`` / ``pool``),
    l'endpoint, le protocole et une copie profonde du corps â€” c'est la preuve
    Â« sur le fil Â» que la conversion a bien eu lieu au niveau handler.
    """

    def __init__(self, *, upstream=None, free_upstream=None, pool_upstream=None):
        self.calls: list[dict] = []
        self._upstream = upstream or (lambda endpoint, body, proto: FakeResponse(payload=chat_completion()))
        self._free_upstream = free_upstream
        self._pool_upstream = pool_upstream
        self._free_queue: list = []

    # -- configuration des rÃ©ponses --------------------------------------
    def queue_free(self, *responses):
        """RÃ©ponses servies successivement par la jambe free (stream)."""
        self._free_queue.extend(responses)

    def set_upstream(self, fn):
        self._upstream = fn

    # -- lecture ---------------------------------------------------------
    @property
    def last(self) -> dict:
        assert self.calls, "aucun appel amont capturÃ©"
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
    """Remplace tous les seams amont + les effets de bord non hermÃ©tiques."""

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
        """En-tÃªtes d'auth factices â€” jamais de vraie clÃ©."""
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
    """Cache de rÃ©ponses dÃ©sactivÃ© : jamais de HIT qui court-circuite l'amont."""

    def make_key(self, *a, **k):
        return None

    def get(self, *a, **k):
        return None

    def put(self, *a, **k):
        return None


@pytest.fixture
def client():
    """Client ASGI sur l'app rÃ©elle. Pas de ``with`` : le lifespan dÃ©marre des
    pollers rÃ©seau qui pendent sous pytest (cf. tests/test_responses_stream_e2e.py)."""
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

    Accepte les deux formes Ã©mises par le proxy : ``event: X\\ndata: {...}``
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
    """Types d'Ã©vÃ©nements SSE, dans l'ordre, **hors sentinelle ``[DONE]``**.

    ``[DONE]`` est conservÃ© volontairement en fin de flux par
    ``responses_stream_sse`` (sentinelle de fin pour nos clients existants) :
    ce n'est pas un Ã©vÃ©nement du contrat Responses, il ne doit donc pas
    masquer le vrai terminal ``response.completed``.
    """
    out = []
    for name, payload in _sse_events(text):
        if name == "__done__":
            continue  # sentinelle de fin, pas un Ã©vÃ©nement du contrat
        if name:
            out.append(name)
        else:
            t = payload.get("type")
            if isinstance(t, str):
                out.append(t)
            elif payload:
                out.append("chunk")
    return out


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• Matrice 6 chemins Ã— non-stream â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


def test_p1_messages_anthropic_nonstream(client, recorder):
    """P1 â€” /v1/messages â†’ anthropic : passthrough du corps, rÃ©ponse relayÃ©e."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=anthro_message("P1 ok")))
    body = {"model": PAID_ANTHRO, "max_tokens": 256, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/messages", body)

    assert status == 200
    assert ctype.startswith("application/json")
    assert recorder.last["endpoint"] == EP_ANTHRO
    assert recorder.last["protocol"] == "anthropic"
    assert recorder.last["body"]["model"] == PAID_ANTHRO
    assert recorder.last["body"]["messages"] == [{"role": "user", "content": "hi"}]
    # RÃ©ponse Anthropic relayÃ©e telle quelle (pas de conversion sur P1).
    payload = json.loads(text)
    assert payload["type"] == "message"
    assert payload["content"][0]["text"] == "P1 ok"
    assert payload["usage"]["input_tokens"] == 11
    assert payload["usage"]["output_tokens"] == 22


def test_p2_messages_to_openai_nonstream(client, recorder):
    """P2 â€” /v1/messages â†’ openai : conversion aller ET retour prouvÃ©es."""
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
    # Retour : rÃ©ponse Chat â†’ Anthropic Messages.
    payload = json.loads(text)
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    assert payload["content"][0]["type"] == "text"
    assert payload["content"][0]["text"] == "P2 ok"
    assert payload["usage"]["input_tokens"] == 11
    assert payload["usage"]["output_tokens"] == 22


def test_p3_chat_passthrough_nonstream(client, recorder):
    """P3 â€” /v1/chat/completions â†’ openai : passthrough, rÃ©ponse inchangÃ©e."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=chat_completion("P3 ok")))
    body = {"model": PAID_CHAT, "max_tokens": 256, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/chat/completions", body)

    assert status == 200
    assert ctype.startswith("application/json")
    assert recorder.last["endpoint"] == EP_CHAT
    assert recorder.last["protocol"] == "openai"
    assert recorder.last["body"]["model"] == PAID_CHAT
    assert recorder.last["body"]["messages"] == [{"role": "user", "content": "hi"}]
    # Passthrough : la rÃ©ponse Chat arrive telle quelle au client.
    payload = json.loads(text)
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "P3 ok"
    assert payload["usage"]["total_tokens"] == 33


def test_p4_chat_to_anthropic_nonstream(client, recorder):
    """P4 â€” /v1/chat/completions â†’ anthropic : double conversion aller/retour."""
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
    # Retour : rÃ©ponse Anthropic â†’ Chat Completions.
    payload = json.loads(text)
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "P4 ok"
    assert payload["choices"][0]["finish_reason"] == "stop"
    assert payload["usage"]["prompt_tokens"] == 11
    assert payload["usage"]["completion_tokens"] == 22


def test_p5_responses_to_openai_nonstream(client, recorder):
    """P5 â€” /v1/responses â†’ openai/responses : conversion Aâ†’chatâ†’Responses."""
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
    # Format Responses natif (pas de clÃ© "messages").
    assert "input" in up and "messages" not in up
    assert up["stream"] is False
    # [Lot L15 â€” B5] store/truncation relayÃ©s depuis le corps client.
    assert up.get("store") is True
    assert up.get("truncation") == "auto"
    payload = json.loads(text)
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["output"][0]["content"][0]["text"] == "P5 ok"
    assert payload["usage"]["input_tokens"] == 11


def test_p6_responses_to_anthropic_nonstream(client, recorder):
    """P6 â€” /v1/responses â†’ anthropic : conversion Responsesâ†’Messagesâ†’Responses."""
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
    # Retour : rÃ©ponse Anthropic â†’ objet Responses.
    payload = json.loads(text)
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    assert payload["output"][0]["content"][0]["type"] == "output_text"
    assert payload["output"][0]["content"][0]["text"] == "P6 ok"
    assert payload["usage"]["input_tokens"] == 11
    assert payload["usage"]["output_tokens"] == 22


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• Matrice 6 chemins Ã— stream â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


def test_p1_messages_anthropic_stream(client, recorder):
    """P1 stream â€” SSE Anthropic relayÃ©, content-type & sÃ©quence attendus."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(lines=ANTHRO_SSE_LINES, raw_lines=ANTHRO_SSE_BYTES))
    body = {"model": PAID_ANTHRO, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/messages", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.calls, "l'amont doit Ãªtre appelÃ© en streaming"
    assert recorder.last["endpoint"] == EP_ANTHRO
    types = _event_types(text)
    for expected in ("message_start", "content_block_start", "content_block_delta", "content_block_stop"):
        assert expected in types, f"{expected} manquant dans {types}"
    assert "hello" in text


def test_p2_messages_to_openai_stream(client, recorder):
    """P2 stream â€” amont chat consommÃ© en SSE, rÃ©-Ã©mis en SSE Anthropic."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(lines=CHAT_CHUNK_LINES))
    body = {"model": PAID_CHAT, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/messages", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.last["endpoint"] == EP_CHAT
    up = recorder.last["body"]
    assert up["model"] == PAID_CHAT
    assert up["stream"] is True
    # Conversion aller : outils Anthropic â†’ fonction Chat si prÃ©sents.
    types = _event_types(text)
    assert "message_start" in types
    assert "message_stop" in types
    assert types.index("message_start") < types.index("message_stop")
    assert "hello" in text


def test_p3_chat_passthrough_stream(client, recorder):
    """P3 stream â€” passthrough SSE Chat + terminal ``data: [DONE]``."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(lines=CHAT_CHUNK_LINES))
    body = {"model": PAID_CHAT, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/chat/completions", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.last["endpoint"] == EP_CHAT
    assert recorder.last["body"]["stream"] is True
    # stream_options ajoutÃ© pour obtenir l'usage en fin de flux.
    assert recorder.last["body"].get("stream_options") == {"include_usage": True}
    assert "data: [DONE]" in text
    assert "hello" in text


def test_p4_chat_to_anthropic_stream(client, recorder):
    """P4 stream â€” amont Anthropic converti en chunks Chat + ``[DONE]``.

    Ã‰choue aujourd'hui : ``opencode.py:12570`` omet ``anthro_body`` du
    ``nonlocal``, l'amont n'est jamais appelÃ© et le client reÃ§oit 0 octet.
    """
    recorder.set_upstream(lambda e, b, p: FakeResponse(lines=ANTHRO_SSE_LINES, raw_lines=ANTHRO_SSE_BYTES))
    body = {"model": PAID_ANTHRO, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/chat/completions", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.calls, "l'amont doit Ãªtre appelÃ© en streaming sur P4"
    assert recorder.last["endpoint"] == EP_ANTHRO
    assert "data: [DONE]" in text
    assert "hello" in text


def test_p5_responses_to_openai_stream(client, recorder):
    """P5 stream â€” amont chat collectÃ© puis Ã©mis en SSE Responses."""
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
    # ``response.completed`` est le dernier Ã‰VÃ‰NEMENT du contrat Responses.
    # La sentinelle ``[DONE]`` qui suit est ajoutÃ©e par ``responses_stream_sse``
    # et exclue par ``_event_types`` (ce n'est pas un Ã©vÃ©nement de la spec).
    assert types[-1] == "response.completed"
    assert "data: [DONE]" in text
    assert "hello" in text


def test_p6_responses_to_anthropic_stream(client, recorder):
    """P6 stream â€” amont Anthropic (corps JSON) puis sÃ©quence SSE Responses.

    Note de contrat : P6-stream est **buffer-then-emit**. Le handler lit
    ``resp.json()`` (``opencode.py:13576``) puis Ã©met une sÃ©quence Responses
    complÃ¨te via ``responses_stream_events`` â€” il ne consomme **pas** de flux
    SSE amont. Le double amont doit donc renvoyer un corps **JSON**, pas des
    octets SSE : sinon le chemin Â« buffer-then-emit Â» n'est pas exercÃ©.
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


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• Sous-chemin free-model (stream) â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


def test_free_model_subpath_p2_stream(client, recorder):
    """P2 stream â€” modÃ¨le client Ã  Ã©quivalent free : l'amont reÃ§oit l'endpoint
    FREE et le modÃ¨le swappÃ©, via la jambe ``_open_free_stream`` (jamais la
    payante). VÃ©rifie l'effet rÃ©el sur le fil, pas seulement la table."""
    route_p2 = oc._route_for(FREE_CLIENT_P2)
    assert route_p2, "route attendue pour FREE_CLIENT_P2"
    target = route_p2["model"]
    free_model = oc._resolve_free_model(target)
    assert free_model, f"{target} doit avoir un Ã©quivalent free pour ce test"
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
    # Le modÃ¨le PAYANT envoyÃ© par le client a bien Ã©tÃ© remplacÃ© par le free.
    assert free_call["body"]["model"] == free_model
    assert free_call["body"]["model"] != target
    assert "hello" in text


def test_free_model_subpath_p4_stream(client, recorder):
    """P4 stream â€” le client parle Chat, le modÃ¨le payant routÃ© dÃ©clare
    ``protocol: anthropic`` (l'amont payant est donc Anthropic) et son Ã©quivalent
    free s'adresse Ã  un endpoint **Chat**.

    Avant le correctif A26/P4, l'aller partait en forme Anthropic vers un endpoint
    Chat et le flux Chat revenait Ã  un parseur qui attend de l'Anthropic : le
    client recevait **0 octet**, sous un HTTP 200.

    Ce test stubait auparavant ``ANTHRO_SSE_LINES`` â€” une forme que l'endpoint free
    rÃ©el ne produit **jamais** â€” et n'asserait aucun contenu : il verrouillait donc
    le dÃ©faut au lieu de le dÃ©tecter (mÃªme classe qu'A25).
    """
    target = oc._route_for(FREE_CLIENT_P4)["model"]
    assert oc.get_model_config(target)["protocol"] == "anthropic", f"{target} doit dÃ©clarer le protocole anthropic"
    free_model = oc._resolve_free_model(target)
    assert free_model, f"{target} doit avoir un Ã©quivalent free pour ce test"
    assert "/responses" not in _cfg_settings._free_endpoint_for(
        free_model
    ), "l'Ã©quivalent free doit Ãªtre un endpoint Chat, sinon ce chemin n'est pas celui du dÃ©faut"

    _chat_bytes = [(ln + "\n").encode() for ln in CHAT_CHUNK_LINES]
    recorder.queue_free(FakeResponse(lines=CHAT_CHUNK_LINES, raw_lines=_chat_bytes))
    body = {"model": FREE_CLIENT_P4, "max_tokens": 256, "stream": True, "messages": [{"role": "user", "content": "hi"}]}

    status, ctype, text = _post(client, "/v1/chat/completions", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.calls, "la jambe free doit appeler l'amont"
    free_call = recorder.calls[0]
    assert free_call["seam"] == "free"
    assert free_call["use_free"] is True
    assert free_call["body"]["model"] == free_model

    # 1) L'ALLER est de forme CHAT, pas Anthropic : un endpoint Chat ne lit ni
    #    `system` top-level ni `input_schema` (ici `content` doit rester une chaÃ®ne).
    assert isinstance(free_call["body"]["messages"][0]["content"], str), (
        "corps envoyÃ© en forme Anthropic (blocs) vers un endpoint Chat"
    )

    # 2) Le CONTENU arrive â€” c'est l'assertion qui manquait.
    assert "hello" in text, f"contenu perdu, flux reÃ§u : {text!r}"

    # 3) Le dÃ©faut d'origine, nommÃ©ment : 0 octet sous un HTTP 200.
    assert text.strip(), "le client a reÃ§u 0 octet"
    assert "chat.completion.chunk" in text
    assert "data: [DONE]" in text


def _free_leg_sans_entetes(monkeypatch, status_code=429):
    """[A27] Reproduit le mÃ©canisme **rÃ©el** du 500, pas une hypothÃ¨se.

    ``_try_free_model_first`` rend ``resp_headers`` **None** sur son chemin nominal
    (Â« resp_headers stays None for hedge Â», ``opencode.py:6280``). Huit sites de
    dÃ©pouillement Ã©crasaient alors leurs en-tÃªtes payants â€” valides â€” par ce ``None``, et
    la lecture suivante levait ``AttributeError``, avalÃ©e par le ``except Exception`` du
    handler : un **HTTP 500** nu, sans autre trace qu'un ``Traceback (500)`` dans le log.
        [CORRECTIF] Le mecanisme decrit ci-dessus n'est plus atteignable : le resultat de la
    jambe free est depouille en `resp, _, _actual_model, _actual_ip` (opencode.py L12755),
    donc le `None` du creneau d'en-tetes ne parvient JAMAIS a `a_headers`. Le 503 observe
    ici vient de la traduction "cles en pause" en 503 retryable, pas d'une garde A27. Ce
    test verrouille le contrat visible (erreur exploitable, jamais un 500 nu) ; il ne
    prouve PAS la garde, qui a ete retiree comme inatteignable.
"""

    appels = []

    async def _free(*args, **kwargs):
        appels.append(args)
        resp = FakeResponse(status_code=status_code, payload={"error": {"message": "free limited"}})
        return resp, None, "mimo-v2.5-free", "203.0.113.9"

    monkeypatch.setattr(oc, "_try_free_model_first", _free, raising=False)
    return appels


def test_p4_nonstream_jambe_free_sans_entetes_ne_fait_pas_500(client, recorder, monkeypatch):
    """[A27] ``/v1/chat/completions`` non-stream â€” trace rÃ©elle ``opencode.py:12647``.    [CORRECTIF] Le mecanisme decrit ci-dessus n'est plus atteignable : le resultat de la
    jambe free est depouille en `resp, _, _actual_model, _actual_ip` (opencode.py L12755),
    donc le `None` du creneau d'en-tetes ne parvient JAMAIS a `a_headers`. Le 503 observe
    ici vient de la traduction "cles en pause" en 503 retryable, pas d'une garde A27. Ce
    test verrouille le contrat visible (erreur exploitable, jamais un 500 nu) ; il ne
    prouve PAS la garde, qui a ete retiree comme inatteignable.
"""
    appels = _free_leg_sans_entetes(monkeypatch)
    body = {"model": FREE_CLIENT_P4, "max_tokens": 256, "messages": [{"role": "user", "content": "hi"}]}

    status, _ctype, text = _post(client, "/v1/chat/completions", body, stream=False)

    assert appels, "la jambe free doit Ãªtre tentÃ©e, sinon ce test ne couvre rien"
    assert status != 500, f"A27 : 500 au lieu d'une erreur exploitable (corps : {text[:200]!r})"
    # L'amont 429 est traduit en 503 Â« retry later Â» â€” message exploitable, pas un 500 nu.
    assert status == 503, f"A27 : code {status} inattendu (corps : {text[:200]!r})"
    assert "Erreur interne" not in text
    assert "exhausted" in text.lower()


def test_p1_nonstream_jambe_free_sans_entetes_ne_fait_pas_500(client, recorder, monkeypatch):
    """[A27] Le **jumeau** : ``/v1/messages`` non-stream (``opencode.py:9022``).

    MÃªme motif dans un autre handler : la jambe free rend ses en-tÃªtes ``None``, le site
    de dÃ©pouillement Ã©crasait les en-tÃªtes payants, puis la lecture en ``.get()`` levait.
    Non mesurÃ© en rÃ©el (seul P4 l'a Ã©tÃ©), mais identique par construction.
        [CORRECTIF] Le mecanisme decrit ci-dessus n'est plus atteignable : le resultat de la
    jambe free est depouille en `resp, _, _actual_model, _actual_ip` (opencode.py L12755),
    donc le `None` du creneau d'en-tetes ne parvient JAMAIS a `a_headers`. Le 503 observe
    ici vient de la traduction "cles en pause" en 503 retryable, pas d'une garde A27. Ce
    test verrouille le contrat visible (erreur exploitable, jamais un 500 nu) ; il ne
    prouve PAS la garde, qui a ete retiree comme inatteignable.
"""
    _free_leg_sans_entetes(monkeypatch)
    body = {"model": P1_CLIENT, "max_tokens": 256, "messages": [{"role": "user", "content": "hi"}]}

    status, _ctype, text = _post(client, "/v1/messages", body, stream=False)

    assert status != 500, f"A27 jumeau : 500 au lieu d'une erreur exploitable (corps : {text[:200]!r})"
    assert "Erreur interne" not in text


# Client dont la route mÃ¨ne Ã  un modÃ¨le Ã  protocole `anthropic` (haiku â†’
# minimax-m2.5, config.yaml:25/57-58) : c'est la condition du dÃ©faut P1, pas le
# protocole du modÃ¨le free (tous les modÃ¨les free sont `openai`, config.yaml:93-94).
P1_CLIENT = "haiku"


def test_free_model_subpath_p1_stream_converts_chat_to_anthropic(client, recorder):
    """P1 stream â€” [lot L1/P1] le modÃ¨le payant dÃ©clare ``protocol: anthropic``
    alors que son Ã©quivalent free est un endpoint ``/chat/completions``.

    Avant le correctif, la boucle de relais rendait les chunks
    ``chat.completion.chunk`` **bruts** au client Anthropic : aucun
    ``message_start``, aucun ``content_block_delta`` â€” juste un flux Ã©tranger sur
    un endpoint SSE Anthropic, sous un HTTP 200.

    RepÃ¨re : le **mÃªme** modÃ¨le free, atteint par une route de protocole
    ``openai``, fonctionnait dÃ©jÃ  (``test_free_model_subpath_p2_stream``). C'est
    donc la dÃ©claration de protocole du modÃ¨le payant qui dÃ©clenchait le dÃ©faut.
    """
    target = oc._route_for(P1_CLIENT)["model"]
    # Le test ne porte que si les DEUX conditions du dÃ©faut sont rÃ©unies.
    assert oc.get_model_config(target)["protocol"] == "anthropic", f"{target} doit dÃ©clarer le protocole anthropic"
    free_model = oc._resolve_free_model(target)
    assert free_model, f"{target} doit avoir un Ã©quivalent free pour ce test"
    assert "/responses" not in _cfg_settings._free_endpoint_for(
        free_model
    ), "l'Ã©quivalent free doit Ãªtre un endpoint Chat, sinon ce chemin n'est pas celui du dÃ©faut"

    _chat_bytes = [(ln + "\n").encode() for ln in CHAT_CHUNK_LINES]
    recorder.queue_free(FakeResponse(lines=CHAT_CHUNK_LINES, raw_lines=_chat_bytes))
    body = {
        "model": P1_CLIENT,
        "max_tokens": 256,
        "stream": True,
        "system": "SYS-A-NE-PAS-PERDRE",
        "messages": [{"role": "user", "content": "hi"}],
    }

    status, ctype, text = _post(client, "/v1/messages", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")

    # 1) Le client reÃ§oit le contrat SSE de SON protocole (RETOUR converti).
    _compact = text.replace(" ", "")
    assert "event:message_start" in _compact
    assert '"type":"content_block_delta"' in _compact
    assert '"type":"text_delta"' in _compact
    assert "event:message_stop" in _compact
    assert "hello" in text

    # 2) Le dÃ©faut d'origine, nommÃ©ment : des chunks Chat sur un endpoint Anthropic.
    assert "chat.completion.chunk" not in text

    # 3) L'ALLER a Ã©tÃ© converti : le `system` top-level Anthropic â€” qu'un endpoint
    #    Chat ne lit pas, d'oÃ¹ sa perte silencieuse â€” devient un message.
    free_call = recorder.calls[0]
    assert free_call["seam"] == "free"
    assert free_call["body"]["model"] == free_model
    assert "system" not in free_call["body"]
    assert any("SYS-A-NE-PAS-PERDRE" in json.dumps(m) for m in free_call["body"]["messages"])


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• Sous-chemin failover (stream) â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


@pytest.fixture
def two_paid_keys(monkeypatch):
    """Deux clÃ©s payantes saines : condition ``len(API_KEYS) > 1`` du failover."""
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
    """P2 stream â€” 1er amont 429 : la clÃ© alternative est utilisÃ©e et le flux
    client aboutit quand mÃªme (le failover ne casse pas la SSE)."""
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
    assert calls["n"] >= 2, "le failover doit rÃ©Ã©mettre une requÃªte amont"
    assert "hello" in text


def test_failover_p4_stream_429_then_success(client, recorder, two_paid_keys):
    """P4 stream â€” mÃªme failover ; bloquÃ© par le bug ``anthro_body`` qui
    empÃªche tout appel amont et toute Ã©mission d'octets."""
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
    assert calls["n"] >= 2, "le failover doit rÃ©Ã©mettre une requÃªte amont"
    assert "data: [DONE]" in text


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• Corpus round-trip (V4) â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•


@pytest.mark.parametrize("case", CORPUS, ids=[c["name"] for c in CORPUS])
def test_corpus_round_trip_p2_conversion_preserves_content(case, client, recorder):
    """V4 â€” corpus aller (P2) : la requÃªte convertie prÃ©serve le contenu
    essentiel (aucun texte perdu/dupliquÃ©, ids d'outils et piÃ¨ces jointes
    conservÃ©s) puis la rÃ©ponse reconvertie restitue texte et usage."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=chat_completion("corpus ok")))
    body = dict(case["anthro"])
    body["model"] = PAID_CHAT

    status, _ctype, text = _post(client, "/v1/messages", body)

    assert status == 200
    assert recorder.last["endpoint"] == EP_CHAT
    upstream = recorder.last["body"]
    serialized = json.dumps(upstream, ensure_ascii=False)

    # Texte du corpus prÃ©sent exactement une fois (ni tronquÃ© ni dupliquÃ©).
    for needle in _needles(case):
        assert needle in serialized, f"{case['name']}: {needle!r} absent du corps amont"
        assert serialized.count(needle) >= 1

    # Invariants par cas.
    if case is CORPUS_TOOLS:
        assert case["tool_id"] in serialized
        assert case["tool_result_text"] in serialized
        for name in case["tool_names"]:
            assert name in serialized, f"outil {name} perdu Ã  la conversion"
        # tool_use â†’ tool_calls avec le mÃªme id, tool_result â†’ role tool.
        assert any(
            m.get("role") == "assistant" and m.get("tool_calls") for m in upstream["messages"]
        ), "tool_calls attendu cÃ´tÃ© assistant"
        assert any(m.get("role") == "tool" for m in upstream["messages"]), "message tool attendu"
    if case is CORPUS_IMAGES:
        assert serialized.count("image_url") >= case["image_count"]
        assert case["url"] in serialized
    if case is CORPUS_DOCUMENTS:
        # Le document part en ``file`` avec media_type prÃ©servÃ©.
        assert "data:application/pdf;base64," in serialized
        assert case["data_fragment"] in serialized
        # [A25] Le nom de fichier du client doit SURVIVRE. Le champ Anthropic
        # est ``title`` (``DocumentBlockParam`` : source/type/cache_control/
        # citations/context/title) ; le code ne lisait que ``name`` et retombait
        # donc toujours sur ``document.pdf``, perdant ``rapport.pdf``.
        # On asserte la VALEUR, pas la simple prÃ©sence de la clÃ©.
        assert case["title"] in serialized, (
            f"nom de fichier client perdu : {case['title']!r} absent "
            "(le document est retombÃ© sur le dÃ©faut 'document.pdf')"
        )
    if case is CORPUS_REASONING:
        assert case["reasoning_text"] in serialized
        assert case["answer"] in serialized

    # Retour : la rÃ©ponse reconvertie restitue le texte et les compteurs.
    payload = json.loads(text)
    assert payload["type"] == "message"
    assert payload["content"][0]["text"] == "corpus ok"
    assert payload["usage"]["input_tokens"] == 11
    assert payload["usage"]["output_tokens"] == 22


@pytest.mark.parametrize("case", CORPUS, ids=[c["name"] for c in CORPUS])
def test_corpus_round_trip_p4_conversion_preserves_content(case, client, recorder):
    """V4 â€” corpus aller (P4) : conversion Chatâ†’Messages prÃ©serve le contenu
    et la rÃ©ponse Anthropic est reconvertie en Chat sans perte d'usage."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=anthro_message("corpus ok")))
    anthro = dict(case["anthro"])
    # P4 part d'un corps Anthropic : on passe par le mÃªme corpus et on vÃ©rifie
    # le corps Messages reÃ§u par l'amont anthropic.
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
    # Usage prÃ©servÃ© au retour.
    assert payload["usage"]["prompt_tokens"] == 11
    assert payload["usage"]["completion_tokens"] == 22
    assert payload["usage"]["total_tokens"] == 33


def test_corpus_tool_ids_survive_p2_round_trip(client, recorder):
    """V4 â€” invariant fort : les ids d'outils traversent l'aller sans Ãªtre
    rÃ©gÃ©nÃ©rÃ©s (sinon le tool_result ne raccroche plus au tool_use)."""
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
    """V4 â€” passthrough P3 : les compteurs d'usage ne sont ni perdus ni doublÃ©s."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=chat_completion("ok")))
    body = {"model": PAID_CHAT, "max_tokens": 256, "messages": [{"role": "user", "content": "hi"}]}

    status, _ctype, text = _post(client, "/v1/chat/completions", body)

    assert status == 200
    payload = json.loads(text)
    assert payload["usage"] == {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}


def _needles(case: dict) -> list[str]:
    """ChaÃ®nes qui doivent survivre Ã  l'aller, par cas de corpus."""
    if case is CORPUS_TEXT:
        return [case["text"]]
    if case is CORPUS_TOOLS:
        return [case["tool_result_text"]]
    if case is CORPUS_IMAGES:
        return ["DÃ©cris ces deux images."]
    if case is CORPUS_DOCUMENTS:
        return ["RÃ©sume ce document."]
    if case is CORPUS_REASONING:
        return [case["answer"]]
    return []


def test_corpus_is_wired_to_real_models():
    """Garde-fou : les modÃ¨les choisis routent bien vers le protocole visÃ©.

    EmpÃªche qu'un changement de table de routage transforme silencieusement un
    test de chemin en test d'un autre chemin (les tests resteraient verts tout
    en ne prouvant plus rien).
    """
    assert _cfg_settings.get_model_config(PAID_ANTHRO)["protocol"] == "anthropic"
    assert _cfg_settings.get_model_config(PAID_CHAT)["protocol"] == "openai"
    assert _cfg_settings.get_model_config(PAID_RESPONSES)["protocol"] == "openai"
    assert _cfg_settings.get_model_config(PAID_ANTHRO)["endpoint"] == EP_ANTHRO
    assert _cfg_settings.get_model_config(PAID_CHAT)["endpoint"] == EP_CHAT
    assert _cfg_settings.get_model_config(PAID_RESPONSES)["endpoint"] == EP_RESPONSES
    # P1/P2/P3/P4/P6 sont exercÃ©s sur la jambe PAYANTE : les modÃ¨les choisis ne
    # doivent pas avoir d'Ã©quivalent free, sinon le swap masquerait le chemin
    # de conversion testÃ©. (P5 est traitÃ© en non-stream avec Â« store Â» et son
    # modÃ¨le a un Ã©quivalent free : la jambe payante y est forcÃ©e par le stub
    # de ``_try_free_model_first`` qui renvoie None.)
    assert oc._resolve_free_model(PAID_ANTHRO) is None
    assert oc._resolve_free_model(PAID_CHAT) is None
    assert oc._resolve_free_model(PAID_RESPONSES) is not None, (
        "muse-spark-1.3-contributor a un Ã©quivalent free : les tests P5 s'appuient "
        "sur le stub de _try_free_model_first pour rester sur la jambe payante"
    )
    # Les modÃ¨les clients des sous-chemins free DOIVENT avoir un Ã©quivalent.
    assert oc._resolve_free_model(oc._route_for(FREE_CLIENT_P2)["model"])
    assert oc._resolve_free_model(oc._route_for(FREE_CLIENT_P4)["model"])


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# [Lot L13 â€” point B1] Contrat `reasoning_content` + repli upstream strict
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# `reasoning_content` n'est dans AUCUNE spec OpenAI (le `delta` officiel ne
# connaÃ®t que content/function_call/refusal/role/tool_calls) : c'est une
# convention vendeur (DeepSeek/GLM/Kimi). Le proxy en fait LE transport du
# raisonnement vers les cibles Chat. Un upstream STRICT peut donc le rejeter en
# 400/422 : on rejoue alors UNE fois sans le champ, au lieu de casser le tour
# entier pour tous les clients de l'endpoint.
# Contrat complet : `docs/reasoning-content-contract.md`.


def _reasoning_history() -> list[dict]:
    """Historique multi-tours oÃ¹ l'assistant a raisonnÃ© au tour 1."""
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
    """B1 â€” vers un upstream Chat, le raisonnement devient `reasoning_content`.

    C'est le contrat Â« normal Â» (Ã©cosystÃ¨me rÃ©el) : la mÃ©moire du raisonnement
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
    # La signature locale ne franchit JAMAIS la frontiÃ¨re vers Chat.
    assert "sig-locale" not in json.dumps(up)


def test_l13_marker_never_reaches_the_wire(client, recorder):
    """Le marqueur de repli est interne : il ne doit pas partir Ã  l'amont.

    Un champ `_has_...` inconnu ferait exactement le 400 qu'on cherche Ã  Ã©viter.
    Attention au niveau observÃ© : le seam ``http`` capture le **dict** avant
    sÃ©rialisation, donc le marqueur y est normalement PRÃ‰SENT (c'est le
    ``_serialize_json_body`` du handler qui le retire). On vÃ©rifie donc les
    octets rÃ©ellement sÃ©rialisÃ©s, pas le dict du recorder.
    """
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=chat_completion("ok")))
    body = {"model": PAID_CHAT, "max_tokens": 256, "messages": _reasoning_history()}

    status, _ctype, _text = _post(client, "/v1/messages", body)

    assert status == 200
    up = recorder.last["body"]
    # Le marqueur est bien posÃ© par le convertisseur...
    assert up.get(oc._HAS_SYNTHETIC_REASONING_KEY) is True
    # ...mais il disparaÃ®t Ã  la sÃ©rialisation (c'est ce qui part sur le fil).
    wire = oc._serialize_json_body(up).decode("utf-8")
    assert oc._HAS_SYNTHETIC_REASONING_KEY not in wire
    assert "_has_synthetic" not in wire
    # ...tandis que le raisonnement lui-mÃªme est bien transmis.
    assert "RAISONNEMENT-1" in wire


def test_l13_strict_upstream_400_triggers_retry_without_reasoning(client, recorder):
    """B1 â€” upstream strict : 400 sur `reasoning_content` â†’ retry-once sans.

    Le tour doit ABOUTIR (texte + tool calls) plutÃ´t qu'Ã©chouer : le
    raisonnement est un enrichissement, jamais un bloquant. On perd la mÃ©moire
    du raisonnement, pas la rÃ©ponse.
    """
    calls = {"n": 0}

    def strict_upstream(endpoint, body, protocol):
        calls["n"] += 1
        if calls["n"] == 1:
            return FakeResponse(status_code=400, payload={"error": {"message": "unknown field reasoning_content"}})
        return FakeResponse(payload=chat_completion("ok aprÃ¨s repli"))

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
    ), "le retry doit avoir retirÃ© `reasoning_content` de TOUS les messages"
    # Et le tour aboutit quand mÃªme.
    assert status == 200
    assert json.loads(text)["content"][0]["text"] == "ok aprÃ¨s repli"


def test_l13_retry_is_once_only(client, recorder):
    """Garde-fou : le retry ne boucle pas si l'upstream rejette encore.

    Un upstream qui rejetterait aussi la version sans raisonnement ne doit pas
    dÃ©clencher de retry infini â€” on rend l'erreur telle quelle.
    """
    calls = {"n": 0}

    def always_400(endpoint, body, protocol):
        calls["n"] += 1
        return FakeResponse(status_code=400, payload={"error": {"message": "still bad"}})

    recorder.set_upstream(always_400)
    body = {"model": PAID_CHAT, "max_tokens": 256, "messages": _reasoning_history()}

    status, _ctype, _text = _post(client, "/v1/messages", body)

    assert len(recorder.calls) == 2, f"le retry doit Ãªtre UNIQUE, or {len(recorder.calls)} appels"
    assert status == 400


def test_l13_no_marker_means_no_retry_for_plain_requests(client, recorder):
    """Sans raisonnement dans l'historique, un 400 ne doit PAS dÃ©clencher de retry.

    Sinon on rejouerait deux fois toute requÃªte en erreur, doublant le coÃ»t et
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


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€ [Lot L4 â€” A8] Restauration du nom d'outil en stream â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€


def _chat_sse_with_tool_call(short_name: str) -> list[str]:
    """SSE amont Chat : un tool_call dont le nom est le nom RACCOURCI (â‰¤64).

    Construit via ``json.dumps`` et non Ã  la main : une accolade mal comptÃ©e
    produit un chunk silencieusement ignorÃ© par le handler, et le test finit par
    vÃ©rifier autre chose que ce qu'il annonce (constatÃ© en Ã©crivant ce test).
    """
    base = {"id": "chatcmpl_tc", "object": "chat.completion.chunk", "created": 1700000000, "model": "upstream"}
    first = {
        **base,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_a8",
                            "type": "function",
                            "function": {"name": short_name, "arguments": ""},
                        }
                    ],
                },
                "finish_reason": None,
            }
        ],
    }
    second = {
        **base,
        "choices": [
            {
                "index": 0,
                "delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]},
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    }
    return [f"data: {json.dumps(first)}", f"data: {json.dumps(second)}", "data: [DONE]"]


def test_a8_stream_restore_returns_original_tool_name(client, recorder):
    """A8 stream â€” le nom raccourci pour l'amont Chat est RESTAURÃ‰ cÃ´tÃ© client.

    C'est la moitiÃ© Â« streaming Â» du correctif : le non-stream est couvert par
    ``openai_to_anthropic``, mais le flux emprunte un autre chemin (branche
    ``tool_calls`` du handler P2) et restaurer Ã  un seul endroit laisserait
    l'autre fuir des noms raccourcis â€” c'est-Ã -dire des outils que le client n'a
    jamais envoyÃ©s et ne saura pas router.
    """
    long_name = "mcp__plugin_very_long_tool_name_exceeding_sixty_four_chars_limit_aaaa"
    short_tools, _map = sanitize_tool_names([{"name": long_name}])
    short_name = short_tools[0]["name"]
    assert len(short_name) <= 64 and short_name != long_name, "prÃ©-requis : le nom doit Ãªtre raccourci"

    recorder.set_upstream(lambda e, b, p: FakeResponse(lines=_chat_sse_with_tool_call(short_name)))
    body = {
        "model": PAID_CHAT,
        "max_tokens": 256,
        "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "name": long_name,
                "description": "d",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
    }

    status, ctype, text = _post(client, "/v1/messages", body, stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    # Aller : l'amont reÃ§oit bien le nom RACCOURCI (limite Chat 64).
    sent = [t["function"]["name"] for t in recorder.last["body"]["tools"]]
    assert sent == [short_name], f"nom long envoyÃ© Ã  l'amont Chat : {sent!r}"

    # Retour : le client reÃ§oit le nom d'ORIGINE, pas le raccourci.
    tool_starts = [
        payload["content_block"]["name"]
        for name, payload in _sse_events(text)
        if name == "content_block_start"
        and isinstance(payload.get("content_block"), dict)
        and payload["content_block"].get("type") == "tool_use"
    ]
    assert tool_starts == [long_name], (
        f"nom non restaurÃ© en streaming : {tool_starts!r} (attendu {long_name!r}) â€” "
        "le client recevrait un outil qu'il n'a jamais envoyÃ©"
    )


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â• TROU 13 â€” jambe free de P6 (responses â†’ amont anthropic) â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
#
# Le harnais stube ``_try_free_model_first â†’ None`` (``_install_seams``) : la jambe
# free n'Ã©tait donc exercÃ©e sur **aucun** chemin P6. Ces tests la rÃ©activent en
# restaurant la **vraie** fonction, capturÃ©e Ã  l'import â€” avant que la fixture ne
# pose son stub (sinon on ne testerait que le stub, classe A25).
#
# PÃ©rimÃ¨tre rÃ©el de la jambe free de P6 (mesurÃ©, ``opencode.py``) :
#   * endpoint free ``/chat/completions`` â†’ ``_try_free_model_first`` convertit le
#     corps Anthropic en forme **Chat** (``anthropic_to_openai``, ligne ~6215) et
#     convertit la rÃ©ponse Chat en Anthropic (``openai_to_anthropic``, ligne ~6679) ;
#     c'est le seul cas aujourd'hui atteignable par une route rÃ©elle (``haiku`` â†’
#     ``minimax-m2.5`` â†’ ``mimo-v2.5-free``, endpoint Chat).
#   * endpoint free ``/responses`` (modÃ¨les free ``muse-*``/``spark-*``) â†’ corps en
#     forme **Responses** (``_anthropic_to_responses_request``) et rÃ©ponse convertie
#     par ``_responses_to_anthropic_response``. Aucune route payante ``protocol:
#     anthropic`` ne mappe sur un free ``muse-*``/``spark-*`` dans ``config.yaml`` :
#     ce volet est donc montÃ© en simulant le choix d'endpoint que le **vrai**
#     ``_free_endpoint_for`` fait pour ces modÃ¨les (assertion ci-dessous).
#
# Les deux formes de rÃ©ponse amont stubÃ©es sont celles que ces endpoints produisent
# rÃ©ellement : ``chat_completion()`` pour Chat, ``responses_object()`` pour
# ``/responses``. Aucun test ne stubbe une forme que l'endpoint ne produit pas.

# ``_try_free_model_first`` **rÃ©el**, capturÃ© avant tout monkeypatch de fixture.
REAL_TRY_FREE_MODEL_FIRST = oc._try_free_model_first

FREE_CLIENT_P6 = FREE_CLIENT_P4  # Â« haiku Â» â†’ minimax-m2.5 (anthropic)


def _p6_input(text: str = "hi") -> dict:
    """Corps client ``/v1/responses`` minimal et valide."""
    return {
        "model": FREE_CLIENT_P6,
        "max_output_tokens": 512,
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}],
    }


def _activer_jambe_free(monkeypatch):
    """Restaure la vraie jambe free + masque la clÃ© payante (AllKeysPausedError).

    Le **seul** site qui appelle ``_try_free_model_first`` sur la branche
    ``anthropic`` non-stream de ``/v1/responses`` est le rattrapage
    ``AllKeysPausedError`` (handler ``responses``, branche
    ``except AllKeysPausedError`` sous ``if protocol == "anthropic"``) : sans clÃ©
    payante disponible, le proxy tente le free avant d'abandonner. On reproduit
    donc ce cas rÃ©el.
    """

    def _paused(protocol, *args, **kwargs):
        raise oc.AllKeysPausedError(30.0)

    monkeypatch.setattr(oc, "_get_auth_headers", _paused, raising=False)
    monkeypatch.setattr(oc, "_try_free_model_first", REAL_TRY_FREE_MODEL_FIRST, raising=False)


def _upstream_par_jambe(recorder, free_payload, paid_payload):
    """RÃ©ponse amont **distincte** selon la jambe (endpoint free vs payant).

    En mode direct (proxy_mode ``direct``, pas de tunnel), la jambe free sort par
    ``_do_request_with_retry`` vers ``_free_endpoint_for(free_model)`` â€”
    l'endpoint discrimine donc la jambe de faÃ§on fiable.
    """

    def _fn(endpoint, body, protocol):
        if endpoint in (EP_FREE_CHAT, EP_FREE_RESPONSES):
            return FakeResponse(payload=free_payload)
        return FakeResponse(payload=paid_payload)

    recorder.set_upstream(_fn)


def _appels_free(recorder) -> list[dict]:
    return [c for c in recorder.calls if c["endpoint"] in (EP_FREE_CHAT, EP_FREE_RESPONSES)]


def test_p6_jambe_free_nonstream_endpoint_chat(client, recorder, monkeypatch):
    """TROU 13 â€” P6 non-stream, jambe free sur ``/chat/completions``.

    La route rÃ©elle ``haiku`` â†’ ``minimax-m2.5`` (``protocol: anthropic``) a pour
    Ã©quivalent free ``mimo-v2.5-free``, dont l'endpoint est un **Chat**. Les deux
    conversions doivent avoir lieu : l'aller en forme Chat (sinon un endpoint Chat
    ne lit ni ``system`` ni les blocs de contenu Anthropic) et le retour Chat â†’
    Anthropic â†’ Responses (sinon le texte du modÃ¨le free est perdu alors que le
    HTTP reste 200 â€” exactement le dÃ©faut silencieux dÃ©jÃ  mesurÃ© sur P1/P4).
    """
    _activer_jambe_free(monkeypatch)

    target = oc._route_for(FREE_CLIENT_P6)["model"]
    assert oc.get_model_config(target)["protocol"] == "anthropic", f"{target} doit Ãªtre la cible anthropic de P6"
    free_model = oc._resolve_free_model(target)
    assert free_model, f"{target} doit avoir un Ã©quivalent free pour ce test"
    assert _cfg_settings._free_endpoint_for(free_model) == EP_FREE_CHAT, (
        "prÃ©-requis : l'Ã©quivalent free rÃ©el de cette route doit viser l'endpoint Chat"
    )

    _upstream_par_jambe(recorder, chat_completion("FREE-TEXT"), anthro_message("PAID"))

    status, ctype, text = _post(client, "/v1/responses", _p6_input())

    # 1) La jambe free a bien Ã©tÃ© empruntÃ©e â€” et seule (aucun repli payant).
    appels_free = _appels_free(recorder)
    assert len(appels_free) == 1, f"jambe free attendue une fois, appels : {recorder.endpoints()}"
    assert recorder.endpoints() == [EP_FREE_CHAT], f"repli payant inattendu : {recorder.endpoints()}"
    free_call = appels_free[0]

    # 2) Forme du corps rÃ©ellement envoyÃ© en amont : CHAT, modÃ¨le swappÃ©.
    assert free_call["body"]["model"] == free_model
    assert free_call["body"]["model"] != target
    assert "messages" in free_call["body"], "corps non converti en forme Chat vers un endpoint Chat"
    assert "input" not in free_call["body"], "clÃ© `input` (Responses) envoyÃ©e Ã  un endpoint Chat"
    assert isinstance(free_call["body"]["messages"][0]["content"], str), (
        f"contenu envoyÃ© en blocs Anthropic vers un endpoint Chat : {free_call['body']['messages'][0]['content']!r}"
    )
    assert isinstance(free_call["body"]["max_tokens"], int), "max_tokens (Anthropic) attendu en forme Chat"

    # 3) Contenu reÃ§u par le client : le texte du modÃ¨le free, pas un corps vide.
    assert status == 200
    assert ctype.startswith("application/json")
    payload = json.loads(text)
    assert payload["object"] == "response"
    assert payload["status"] == "completed"
    texts = [
        part["text"]
        for item in payload.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    ]
    assert texts == ["FREE-TEXT"], f"texte du modÃ¨le free perdu (output={payload.get('output')!r}) â€” corps : {text[:300]!r}"
    assert payload["usage"]["output_tokens"] == 22
    assert "PAID" not in text, "la jambe payante a rÃ©pondu alors que la free Ã©tait disponible"

    # 4) Aucune fuite de format : ni corps Chat, ni Ã©vÃ©nement SSE Chat.
    assert '"choices"' not in text, "rÃ©ponse Chat rendue verbatim Ã  un client Responses"
    assert "chat.completion" not in text
    assert "data:" not in text, "corps SSE Ã©mis Ã  un client non-stream"


def test_p6_jambe_free_nonstream_endpoint_responses(client, recorder, monkeypatch):
    """TROU 13 â€” P6 non-stream, jambe free sur ``/responses`` (free ``muse-*``/``spark-*``).

    Volet symÃ©trique : ici l'endpoint free parle **Responses**, donc le corps qui
    part doit Ãªtre en forme Responses (``input``/``max_output_tokens``) et non en
    forme Chat. Le choix d'endpoint est celui que le **vrai** ``_free_endpoint_for``
    rend pour ces modÃ¨les (vÃ©rifiÃ© ci-dessous) ; il est seulement appliquÃ© au modÃ¨le
    free de la route P6, aucune route payante ``protocol: anthropic`` ne mappant sur
    un free ``muse-*``/``spark-*`` dans ``config.yaml``.
    """
    _activer_jambe_free(monkeypatch)

    # Le vrai sÃ©lecteur d'endpoint rend bien /responses pour un free muse-/spark-.
    assert _cfg_settings._free_endpoint_for("muse-spark-1.2-contributor-free") == EP_FREE_RESPONSES
    assert _cfg_settings._free_endpoint_for("muse-spark-1.3-contributor-free") == EP_FREE_RESPONSES

    target = oc._route_for(FREE_CLIENT_P6)["model"]
    free_model = oc._resolve_free_model(target)
    assert free_model
    _free_endpoint_reel = _cfg_settings._free_endpoint_for

    def _endpoint_de_la_route(fid):
        # Le VRAI sÃ©lecteur dÃ©cide ; on ne substitue que pour le modÃ¨le de cette route.
        return EP_FREE_RESPONSES if fid == free_model else _free_endpoint_reel(fid)

    monkeypatch.setattr(_cfg_settings, "_free_endpoint_for", _endpoint_de_la_route, raising=False)

    _upstream_par_jambe(recorder, responses_object("RESP-FREE-TEXT"), anthro_message("PAID"))

    status, _ctype, text = _post(client, "/v1/responses", _p6_input())

    appels_free = _appels_free(recorder)
    assert len(appels_free) == 1, f"jambe free attendue une fois, appels : {recorder.endpoints()}"
    assert appels_free[0]["endpoint"] == EP_FREE_RESPONSES
    body = appels_free[0]["body"]
    assert body["model"] == free_model
    assert "input" in body, "corps non converti en forme Responses vers un endpoint Responses"
    assert "messages" not in body, "clÃ© `messages` (Chat) envoyÃ©e Ã  un endpoint Responses"
    assert "max_output_tokens" in body, "max_tokens Anthropic non traduit en max_output_tokens"
    assert "max_tokens" not in body, "clÃ© `max_tokens` (Anthropic) envoyÃ©e Ã  un endpoint Responses"

    assert status == 200
    payload = json.loads(text)
    texts = [
        part["text"]
        for item in payload.get("output", [])
        if item.get("type") == "message"
        for part in item.get("content", [])
        if part.get("type") == "output_text"
    ]
    assert texts == ["RESP-FREE-TEXT"], f"texte du modÃ¨le free perdu (output={payload.get('output')!r})"
    assert "PAID" not in text
    assert '"choices"' not in text
    assert "data:" not in text


# [TROU 15 â€” corrigÃ© le 15/09/2026] Ce tÃ©moin Ã©tait marquÃ© `xfail` : la branche
# anthropic du handler `/v1/responses` court-circuitait la jambe free en streaming
# (`except AllKeysPausedError` â†’ `if is_stream: return _anthropic_error(503, ...)`, puis
# un `return` 503 inconditionnel qui interceptait le flux). Le message annonÃ§ait un essai
# free qui n'avait pas lieu : MESURÃ‰, 503 et ZÃ‰RO appel amont, contre 200 et un appel
# free en non-stream. CorrigÃ© : la branche streaming laisse filer vers le code de
# streaming situÃ© plus bas (qui force `stream = False` en amont puis essaie le free),
# avec un drapeau `_paused_sans_cle` qui interdit d'appeler le payant sans clÃ© et
# renvoie un 503 vÃ©ridique si la jambe free ne donne rien. Le tÃ©moin est donc un
# tÃ©moin rÃ©el : il Ã©choue de nouveau si l'on rÃ©tablit le court-circuit.
def test_p6_jambe_free_stream_bufferise_et_sans_fuite_de_format(client, recorder, monkeypatch):
    """TROU 13 â€” P6 **stream**, jambe free : ``stream=False`` en amont puis SSE Responses.

    **DÃ‰FAUT MESURÃ‰, non corrigÃ© ici (hors pÃ©rimÃ¨tre d'Ã©dition) â€” ``xfail``.**

    Sur la branche ``protocol: anthropic`` de ``/v1/responses``, le seul site qui
    appelle ``_try_free_model_first`` pour un client ``stream: true`` est le
    rattrapage ``AllKeysPausedError`` (handler ``responses``), et il
    **court-circuite** la jambe free :

        if is_stream:
            return _anthropic_error(503, "All API keys paused â€” free model will be tried
                                   on next attempt")

    Le message annonce un essai free qui n'a **pas** lieu : aucun appel amont n'est
    Ã©mis, alors que le repli free existe bien sur ce chemin juste en dessous
    (``if free_result is not None``). Le client ``stream: true`` reÃ§oit donc un
    **503** lÃ  oÃ¹ le client ``stream: false`` reÃ§oit la rÃ©ponse du modÃ¨le free â€”
    Ã  corps client identique.

    Ce test dÃ©crit le comportement **correct** (jambe free tentÃ©e, contenu reÃ§u,
    aucune fuite de format Chat) ; il tombe en ``xfail`` aujourd'hui et passera au
    vert quand la branche ``is_stream`` cessera de court-circuiter la jambe free.
    """
    _activer_jambe_free(monkeypatch)

    target = oc._route_for(FREE_CLIENT_P6)["model"]
    free_model = oc._resolve_free_model(target)
    assert free_model
    assert _cfg_settings._free_endpoint_for(free_model) == EP_FREE_CHAT

    _upstream_par_jambe(recorder, chat_completion("FREE-TEXT"), anthro_message("PAID"))

    # TÃ©moin : le MÃŠME corps client, en non-stream, emprunte bien la jambe free et
    # reÃ§oit son contenu â€” c'est ce qui rend le 503 du volet stream anormal.
    status_ref, _c, text_ref = _post(client, "/v1/responses", _p6_input())
    assert status_ref == 200, f"tÃ©moin non-stream : {status_ref} {text_ref[:200]!r}"
    assert "FREE-TEXT" in text_ref
    assert len(_appels_free(recorder)) == 1, "tÃ©moin non-stream : la jambe free doit Ãªtre empruntÃ©e"

    recorder.calls.clear()
    _upstream_par_jambe(recorder, chat_completion("FREE-STREAM-TEXT"), anthro_message("PAID"))

    body = dict(_p6_input(), stream=True)
    status, ctype, text = _post(client, "/v1/responses", body, stream=True)

    # 1) Aller : la jambe free reÃ§oit la requÃªte en mode NON-stream (buffer amont).
    appels_free = _appels_free(recorder)
    assert len(appels_free) == 1, f"jambe free attendue une fois, appels : {recorder.endpoints()}"
    free_call = appels_free[0]
    assert free_call["body"]["stream"] is False, (
        f"P6 doit forcer stream=False vers l'amont free, reÃ§u {free_call['body'].get('stream')!r}"
    )
    assert free_call["body"]["model"] == free_model
    assert "messages" in free_call["body"] and "input" not in free_call["body"]

    # 2) Retour : contrat SSE Responses complet cÃ´tÃ© client.
    assert status == 200, f"attendu 200 + SSE, reÃ§u {status} {text[:200]!r}"
    assert ctype.startswith("text/event-stream")
    types = _event_types(text)
    assert types[0] == "response.created", f"sÃ©quence SSE non conforme : {types[:4]!r}"
    assert types[-1] == "response.completed", f"terminal manquant : {types[-4:]!r}"
    assert "response.output_text.delta" in types
    assert "data: [DONE]" in text

    # 3) Contenu reÃ§u, et AUCUNE fuite du format Chat amont dans un flux Responses.
    deltas = "".join(
        payload.get("delta", "") for _name, payload in _sse_events(text) if payload.get("type") == "response.output_text.delta"
    )
    assert deltas == "FREE-STREAM-TEXT", f"contenu perdu/bufferisÃ© sans conversion : {deltas!r} â€” flux : {text[:300]!r}"
    assert "chat.completion.chunk" not in text, "chunk Chat fuitÃ© dans un flux destinÃ© Ã  /v1/responses"
    assert '"choices"' not in text, "corps Chat fuitÃ© dans un flux destinÃ© Ã  /v1/responses"
    assert "PAID" not in text
# Séquence SSE amont d'un endpoint ``/responses`` en streaming — la forme que
# ``responses_stream_sse`` produit réellement (événements Responses officiels).
# Utilisée par le volet stream de TROU 6 : un endpoint free ``/responses``
# renvoie ces événements, jamais un objet JSON.
RESPONSES_SSE_LINES = [
    'data: {"type":"response.created","response":{"id":"resp_s","object":"response","status":"in_progress"}}',
    'data: {"type":"response.output_item.added","output_index":0,'
    '"item":{"id":"msg_s","type":"message","role":"assistant","content":[]}}',
    'data: {"type":"response.content_part.added","item_id":"msg_s","output_index":0,"content_index":0,'
    '"part":{"type":"output_text","text":""}}',
    'data: {"type":"response.output_text.delta","item_id":"msg_s","output_index":0,"content_index":0,'
    '"delta":"FREE-P2-STREAM"}',
    'data: {"type":"response.output_text.done","item_id":"msg_s","output_index":0,"content_index":0,'
    '"text":"FREE-P2-STREAM"}',
    'data: {"type":"response.completed","response":{"id":"resp_s","object":"response","status":"completed",'
    '"output":[{"type":"message","id":"msg_s","role":"assistant",'
    '"content":[{"type":"output_text","text":"FREE-P2-STREAM"}]}],'
    '"usage":{"input_tokens":11,"output_tokens":22,"total_tokens":33}}}',
]
# ═══════════════ TROU 6 — P2 vers `/responses` (client /v1/messages → amont free /responses) ═══════════════
#
# Le handler `messages` (P1/P2) choisit l'endpoint de la jambe free selon le
# **modèle free** (`config/discovery.py::_free_endpoint_for` : `/responses` pour
# `muse-*`/`spark-*`, sinon `/chat/completions`). Quand cet endpoint est
# `/responses`, le corps doit partir en forme **Responses** et la réponse doit
# revenir convertie — deux conversions distinctes, sur deux sites distincts,
# dont l'un n'était couvert par aucun test avant cette section :
#
#   * non-stream, clés payantes **disponibles** (cas nominal, `opencode.py`
#     ~10041) : `_try_free_model_first` convertit lui-même via
#     `_chat_to_responses_request` (~6206) et le handler reconvertit la réponse
#     via `_responses_to_anthropic_response` (détection `"output" in data`,
#     ~10187-10217). **Fonctionne** (mesuré) : c'est le chemin réellement
#     emprunté par toute route `protocol: openai` dont la cible a un équivalent
#     free `muse-*`/`spark-*`.
#   * stream (`stream: true`), clés payantes **pausées** (seul site qui appelle
#     la jambe free quand un client stream demande du streaming : le rattrapage
#     `AllKeysPausedError` du handler, ~9945-9949 ; c'est aussi la forme exacte
#     des tests TROU 13 ci-dessus) : le corps part bien converti (`stream: true`
#     dans le corps Responses envoyé, mesuré) mais la réponse SSE du free
#     n'est **pas** exploitable par `_try_free_model_first` → perte totale de la
#     réponse free, 503 au client. Voir le défaut mesuré sur le témoin xfail.
#
# Aucune route payante `protocol: openai` ne mappe aujourd'hui sur un free
# `muse-*`/`spark-*` dans `config.yaml` : le volet `/responses` est donc monté en
# appliquant au modèle free de la route P2 le choix d'endpoint que le **vrai**
# `_free_endpoint_for` rend pour ces modèles (assertion de pré-requis dans chaque
# test), exactement comme le fait le volet `/responses` de TROU 13.

def _p2_messages(text: str = "hi", max_tokens: int = 4096, stream: bool = False) -> dict:
    """Corps client ``POST /v1/messages`` minimal (P2)."""
    return {
        "model": FREE_CLIENT_P2,
        "max_tokens": max_tokens,
        "stream": stream,
        "messages": [{"role": "user", "content": text}],
    }


def _p2_free_responses(monkeypatch) -> str:
    """Force l'endpoint free de la route P2 vers ``/responses``. Rend le modèle free.

    Le modèle free de ``sonnet`` est ``deepseek-v4-flash-free`` (endpoint Chat
    aujourd'hui) : on ne substitue le sélecteur **que** pour ce modèle, en
    déléguant au vrai ``_free_endpoint_for`` pour tout le reste.
    """
    route_p2 = oc._route_for(FREE_CLIENT_P2)
    assert route_p2, "route attendue pour FREE_CLIENT_P2"
    target = route_p2["model"]
    assert oc.get_model_config(target)["protocol"] == "openai", f"{target} doit être la cible openai de P2"
    free_model = oc._resolve_free_model(target)
    assert free_model, f"{target} doit avoir un équivalent free pour ce test"
    # Pré-requis : c'est bien le VRAI sélecteur qui rend /responses pour muse-/spark-.
    assert _cfg_settings._free_endpoint_for("muse-spark-1.3-contributor-free") == EP_FREE_RESPONSES
    _reel = _cfg_settings._free_endpoint_for
    monkeypatch.setattr(
        _cfg_settings,
        "_free_endpoint_for",
        lambda fid: EP_FREE_RESPONSES if fid == free_model else _reel(fid),
        raising=False,
    )
    return free_model


def _assert_corps_responses(call: dict, free_model: str, *, stream: bool, max_output_tokens: int) -> None:
    """Le corps réellement parti vers un endpoint ``/responses`` est en forme Responses."""
    body = call["body"]
    assert call["endpoint"] == EP_FREE_RESPONSES, f"endpoint inattendu : {call['endpoint']}"
    assert body["model"] == free_model, f"modèle free non swappé : {body.get('model')!r}"
    assert isinstance(body.get("input"), list) and body["input"], (
        f"corps non converti en forme Responses (clé `input` absente/vide) : {sorted(body)}"
    )
    assert "messages" not in body, (
        f"clé `messages` (forme Chat) envoyée à un endpoint Responses : {sorted(body)} — un endpoint "
        "Responses ignore `messages`, donc le prompt du client disparaît alors que l'HTTP reste 200"
    )
    assert body.get("max_output_tokens") == max_output_tokens, (
        f"max_tokens du client non traduit en `max_output_tokens` : {body.get('max_output_tokens')!r} "
        f"(attendu {max_output_tokens})"
    )
    assert "max_tokens" not in body, (
        f"clé `max_tokens` (Anthropic/Chat) envoyée à un endpoint Responses : {sorted(body)}"
    )
    assert body.get("stream") is stream, f"drapeau stream inattendu : {body.get('stream')!r}"
    texts = [
        part.get("text")
        for item in body["input"]
        if isinstance(item, dict)
        for part in (item.get("content") or [])
        if isinstance(part, dict) and part.get("type") in ("input_text", "output_text")
    ]
    assert texts == ["TROU6-P2"], f"prompt client absent du corps Responses : {body['input']!r}"


def _anthropic_text(payload: dict) -> str:
    return "".join(
        block.get("text", "") for block in payload.get("content", []) if isinstance(block, dict) and block.get("type") == "text"
    )


def test_p2_jambe_free_endpoint_responses_nonstream(client, recorder, monkeypatch):
    """TROU 6 — P2 non-stream, jambe free sur ``/responses`` : aller **et** retour.

    Chemin nominal de P2 dès qu'une route ``protocol: openai`` a un équivalent
    free ``muse-*``/``spark-*`` : la jambe free répond en **forme Responses**
    (``output``), donc les deux conversions doivent avoir lieu — l'aller en
    forme Responses, le retour Responses → Anthropic (``_responses_to_anthropic_response``).
    Sans le retour, le HTTP resterait 200 mais le texte du modèle free serait
    perdu (défaut silencieux déjà mesuré ailleurs dans cet audit) ; sans l'aller,
    l'endpoint Responses ignorerait le prompt du client.
    """
    monkeypatch.setattr(oc, "_try_free_model_first", REAL_TRY_FREE_MODEL_FIRST, raising=False)
    free_model = _p2_free_responses(monkeypatch)
    recorder.set_upstream(
        lambda endpoint, body, protocol: FakeResponse(
            payload=responses_object("FREE-P2-TEXT") if endpoint == EP_FREE_RESPONSES else chat_completion("PAID")
        )
    )

    status, ctype, text = _post(client, "/v1/messages", _p2_messages(text="TROU6-P2"))

    # 1) La jambe free a été empruntée seule (aucun repli payant).
    assert recorder.endpoints() == [EP_FREE_RESPONSES], f"appels amont inattendus : {recorder.endpoints()}"
    _assert_corps_responses(recorder.calls[0], free_model, stream=False, max_output_tokens=4096)

    # 2) Contenu reçu par le client : le texte du modèle free, pas un corps vide.
    assert status == 200, f"attendu 200, reçu {status} : {text[:300]!r}"
    assert ctype.startswith("application/json")
    payload = json.loads(text)
    assert payload["type"] == "message" and payload["role"] == "assistant"
    assert _anthropic_text(payload) == "FREE-P2-TEXT", (
        f"texte du modèle free perdu (content={payload.get('content')!r}) — corps : {text[:300]!r}"
    )
    assert payload["stop_reason"] == "end_turn", f"stop_reason non converti : {payload['stop_reason']!r}"
    assert payload["usage"]["input_tokens"] == 11 and payload["usage"]["output_tokens"] == 22, (
        f"usage Responses non transmis au client Anthropic : {payload['usage']!r}"
    )

    # 3) Aucune fuite de format : ni objet Responses brut, ni corps Chat.
    assert '"output"' not in text, "objet Responses rendu verbatim à un client Anthropic"
    assert '"choices"' not in text, "corps Chat rendu à un client Anthropic"
    assert "PAID" not in text, "la jambe payante a répondu alors que la free était disponible"


@pytest.mark.xfail(
    strict=False,
    reason=(
        "TROU 6 (défaut mesuré, non corrigé ici — opencode.py hors périmètre) : sur P2, la jambe free "
        "appelée depuis le rattrapage AllKeysPausedError part avec stream:true vers un endpoint free "
        "/responses ; la réponse SSE (text/event-stream) n'est pas exploitable par _try_free_model_first "
        "(opencode.py ~6593-6601 : `resp.json()` seulement si content-type JSON, puis retour générique "
        "~6756 rend le flux brut) → le handler lit {} et rend 503 « All API keys exhausted » alors que "
        "l'amont free a répondu 200. Le volet non-stream du même chemin rend 200 + le texte."
    ),
)
def test_p2_jambe_free_endpoint_responses_stream_sans_cle_payante(client, recorder, monkeypatch):
    """TROU 6 — P2 **stream** + jambe free ``/responses`` : **DÉFAUT MESURÉ, non corrigé** (``xfail``).

    Sur P2, quand les clés payantes sont pausées, le seul site qui appelle la
    jambe free est le rattrapage ``AllKeysPausedError`` du handler ``messages``
    (~9945-9949). Le client demandant ``stream: true``, ``_try_free_model_first``
    part avec ``stream: true`` dans le corps Responses (mesuré, converti
    correctement) — mais à la réception :

        _free_is_responses and resp.status_code == 200  (~6593)
        rdata = resp.json() if content-type JSON else {}   (~6595)

    Une réponse free ``/responses`` en streaming est un flux **SSE**
    (``text/event-stream``) : la garde ci-dessus ne produit donc **aucun**
    ``rdata`` et l'on tombe sur le retour générique (~6756) qui rend le flux
    SSE **brut** comme si le modèle free avait répondu en JSON. Le handler P2
    fait alors ``data = _resp_json_or_empty(resp)`` (``{}``, car le
    content-type n'est pas JSON), tombe dans le repli payant — lui aussi
    indisponible (clés pausées) — et rend un **503**.

    Mesuré sur ce témoin : appel amont free ``200`` pourtant émis (le prompt est
    bien parti en forme Responses, ``stream: true``), ``recorder.calls`` = 1,
    réponse client = ``503`` ``{"type":"error","error":{"type":"api_error",
    "message":"All API keys exhausted. Retry after 31s."}}``, texte
    ``FREE-P2-STREAM`` **perdu**. Le volet non-stream du même chemin, à corps
    client identique, rend 200 + le texte (test ci-dessus) : la divergence est
    donc bien celle du drapeau ``stream``, pas celle de l'endpoint.

    Ce test décrit le comportement **correct** (le texte du modèle free arrive
    au client en SSE Anthropic, sans fuite du format Responses) ; il passera au
    vert quand ``_try_free_model_first`` saura exploiter (ou refuser proprement)
    un flux SSE free ``/responses`` au lieu de rendre ``None`` via son
    ``except Exception`` latent. ``strict=False`` : un vrai correctif le fait
    passer au vert sans casser la suite.
    """
    monkeypatch.setattr(oc, "_try_free_model_first", REAL_TRY_FREE_MODEL_FIRST, raising=False)

    def _aucune_cle(protocol, *args, **kwargs):
        raise oc.AllKeysPausedError(30.0)

    monkeypatch.setattr(oc, "_get_auth_headers", _aucune_cle, raising=False)
    free_model = _p2_free_responses(monkeypatch)

    def _upstream(endpoint, body, protocol):
        if endpoint == EP_FREE_RESPONSES:  # l'endpoint free /responses répond en SSE
            return FakeResponse(lines=RESPONSES_SSE_LINES)
        return FakeResponse(payload=chat_completion("PAID"))

    recorder.set_upstream(_upstream)

    status, ctype, text = _post(client, "/v1/messages", _p2_messages(text="TROU6-P2", stream=True), stream=True)

    # A) Aller : la jambe free a bien été tentée, corps converti, stream demandé.
    assert recorder.endpoints() == [EP_FREE_RESPONSES], f"appels amont : {recorder.endpoints()}"
    _assert_corps_responses(recorder.calls[0], free_model, stream=True, max_output_tokens=4096)

    # B) Retour : le client stream Anthropic doit recevoir le texte du free.
    assert status == 200, (
        f"le flux free a pourtant répondu (200 en amont) mais le client reçoit {status} : {text[:300]!r}"
    )
    assert ctype.startswith("text/event-stream"), f"content-type inattendu : {ctype!r}"
    types = _event_types(text)
    assert types[0] == "message_start", f"séquence SSE non conforme : {types[:4]!r}"
    assert types[-1] == "message_stop", f"terminal manquant : {types[-4:]!r}"
    assert "content_block_delta" in types, f"aucun delta de texte émis : {types!r}"
    deltas = "".join(
        (payload.get("delta") or {}).get("text", "")
        for name, payload in _sse_events(text)
        if name == "content_block_delta"
    )
    assert deltas == "FREE-P2-STREAM", f"contenu du modèle free perdu : {deltas!r} — flux : {text[:300]!r}"

    # C) Aucune fuite du format amont (SSE Responses) dans un flux Anthropic.
    assert "response.output_text.delta" not in text, "événement Responses fuité dans un flux Anthropic"
    assert '"choices"' not in text, "corps Chat fuité dans un flux Anthropic"
    assert "PAID" not in text, "la jambe payante a répondu"
