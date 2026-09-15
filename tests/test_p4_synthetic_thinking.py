"""[TROU 4] P4 ne doit jamais émettre vers l'amont Anthropic un bloc ``thinking``
à signature LOCALE (forgée par le proxy).

Chemin visé : **P4** = client ``POST /v1/chat/completions`` → amont Anthropic
(handler ``chat_completions``, corps ``anthro_body = openai_to_anthropic_request(body)``).

Défaut mesuré (instrumentation des seams amont, corps réellement transmis) :
``openai_to_anthropic_request`` (``app/protocol/mapping.py`` L2126-2142) réinjecte
le ``reasoning_content`` de l'historique en bloc ``thinking`` portant
``_local_signature(...)`` — une signature que le proxy calcule lui-même. P1
(``/v1/messages`` → Anthropic) appelle ``strip_synthetic_thinking`` avant l'envoi ;
P4 ne l'appelait pas : le bloc signé localement partait tel quel vers l'amont, qui
valide cryptographiquement les signatures (400 « forged signature » en production).

Niveau de preuve : ASGI + doubles amont (``_do_request_with_retry`` /
``_open_free_stream`` / ``_open_via_pool``), on inspecte la copie profonde du corps
capturé *sur le fil*, pas le convertisseur isolé — c'est le seul niveau qui attrape
un nettoyage oublié un lien plus loin dans le handler.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

import opencode as oc
from app.protocol.mapping import _local_signature

# Modèle client à cible Anthropic sans équivalent dans ``FREE_MODEL_MAP``
# (``minimax-m3``) : la jambe payante est exercée sans dépendre du swap free.
PAID_ANTHRO = "minimax-m3"
EP_ANTHRO = "https://opencode.ai/zen/go/v1/messages"

# Raisonnement synthétique : texte + signature locale, exactement ce que le proxy
# fabrique à partir de ``reasoning_content`` (cf. mapping.py ``_local_signature``).
REASONING = "raisonnement synthetique du proxy"
FORGED_SIGNATURE = _local_signature(REASONING)

ANTHRO_PAYLOAD = {
    "id": "msg_p4",
    "type": "message",
    "role": "assistant",
    "model": PAID_ANTHRO,
    "content": [{"type": "text", "text": "P4 ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 11, "output_tokens": 22},
}

ANTHRO_SSE_LINES = [
    'data: {"type":"message_start","message":{"id":"msg_s","type":"message","role":"assistant",'
    '"content":[],"model":"upstream","stop_reason":null,'
    '"usage":{"input_tokens":11,"output_tokens":0}}}',
    'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
    'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}',
    'data: {"type":"content_block_stop","index":0}',
    'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":22}}',
    'data: {"type":"message_stop"}',
]


class FakeResponse:
    """Double de réponse amont : interface consommée par le handler P4."""

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
        self.stream_lines = ANTHRO_SSE_LINES

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
    """Cache désactivé : aucun HIT ne court-circuite l'amont."""

    def make_key(self, *a, **k):
        return None

    def get(self, *a, **k):
        return None

    def put(self, *a, **k):
        return None


@pytest.fixture
def recorder(monkeypatch):
    """Seams amont remplacés : on capture ce qui part RÉELLEMENT sur le fil."""
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
        yield FakeResponse(lines=rec.stream_lines)

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
    """Client ASGI sur l'app réelle (pas de ``with`` : pollers du lifespan)."""
    return TestClient(oc.app)


def _p4_body(*, stream: bool) -> dict:
    """Requête P4 (client Chat) dont l'historique porte un ``reasoning_content``
    qui sera réinjecté en bloc ``thinking`` à signature locale."""
    return {
        "model": PAID_ANTHRO,
        "max_tokens": 128,
        "stream": stream,
        "messages": [
            {"role": "user", "content": "question 1"},
            {"role": "assistant", "content": "reponse 1", "reasoning_content": REASONING},
            {"role": "user", "content": "question 2"},
        ],
    }


def _signed_thinking_blocks(body: dict) -> list[dict]:
    """Blocs ``thinking`` de signature locale présents dans un corps amont."""
    found = []
    for msg in body.get("messages", []) or []:
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "thinking":
                continue
            if block.get("signature") == FORGED_SIGNATURE:
                found.append(block)
    return found


def test_p4_nonstream_upstream_has_no_forged_thinking(recorder, client):
    """P4 non-stream : l'amont Anthropic ne reçoit aucun bloc à signature locale."""
    status, _ctype, _text = _post(client, "/v1/chat/completions", _p4_body(stream=False))

    assert status == 200
    upstream = recorder.last
    assert upstream["endpoint"] == EP_ANTHRO
    assert upstream["protocol"] == "anthropic"
    # Le raisonnement est bien arrivé jusqu'à la conversion…
    assert any(
        b.get("type") == "text" and b.get("text") == "reponse 1"
        for m in upstream["body"]["messages"]
        for b in (m.get("content") or [])
    ), "l'historique assistant doit survivre au strip (texte intact)"
    # …mais le bloc signé localement ne doit PAS partir.
    assert not _signed_thinking_blocks(upstream["body"]), (
        "P4 a émis vers l'amont un bloc thinking à signature LOCALE (forgée) : "
        f"{_signed_thinking_blocks(upstream['body'])!r}"
    )


def test_p4_stream_upstream_has_no_forged_thinking(recorder, client):
    """P4 stream : même exigence sur la jambe réellement empruntée (free ou payante)."""
    status, ctype, raw = _post(client, "/v1/chat/completions", _p4_body(stream=True), stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.calls, "l'amont doit être appelé en streaming"
    for call in recorder.calls:
        assert not _signed_thinking_blocks(call["body"]), (
            f"P4 stream a émis vers l'amont ({call['seam']} {call['endpoint']}) un bloc "
            f"thinking à signature LOCALE (forgée) : {_signed_thinking_blocks(call['body'])!r}"
        )
    assert "hello" in raw


def test_p1_still_strips_forged_thinking_non_regression(recorder, client):
    """P1 (référence) : le nettoyage déjà en place reste effectif."""
    body = {
        "model": PAID_ANTHRO,
        "max_tokens": 128,
        "messages": [
            {"role": "user", "content": "question 1"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": REASONING, "signature": FORGED_SIGNATURE},
                    {"type": "text", "text": "reponse 1"},
                ],
            },
            {"role": "user", "content": "question 2"},
        ],
    }

    status, _ctype, _text = _post(client, "/v1/messages", body)

    assert status == 200
    assert not _signed_thinking_blocks(recorder.last["body"])


def _post(client, url, body, stream=False):
    """POST synchrone ; consomme le flux complet quand ``stream``."""
    if not stream:
        r = client.post(url, json=body)
        return r.status_code, r.headers.get("content-type", ""), r.text
    with client.stream("POST", url, json=body) as r:
        raw = b"".join(r.iter_bytes())
    return r.status_code, r.headers.get("content-type", ""), raw.decode("utf-8", "replace")
