"""Régression 2026-10-29 — streaming free quand toutes les clés sont mortes.

Symptôme mesuré : `POST /v1/messages` avec `stream:true` sur un modèle free au
protocole OpenAI (`deepseek-v4.1-flash-free`, le modèle principal du client)
alors que la clé unique est en pause 401. Le handler tombait dans le repli
free **non-stream** de la branche OpenAI : `_try_free_model_first` lisait la
réponse SSE comme du JSON, obtenait `{}` (0 token) et rendait un `200 JSON`
avec un message vide. Le client croyait avoir une réponse.

Contrat : en streaming, on ne passe jamais par `_try_free_model_first` ; le
chemin `anthropic_stream` tente la jambe free avant l'auth et relaie le flux.
"""

from test_e2e_protocol_matrix import (  # noqa: F401  (fixtures ré-exportées)
    CHAT_CHUNK_LINES,
    FakeResponse,
    client,
    recorder,
)

import opencode as oc


def test_streaming_free_not_routed_through_nonstream_fallback(client, recorder, monkeypatch):  # noqa: F811
    # Toutes les clés sont mortes/pausées.
    def _no_key(_protocol, entry=None):
        raise oc.AllKeysPausedError(60)

    monkeypatch.setattr(oc, "_get_auth_headers", _no_key)
    monkeypatch.setattr(oc, "_has_free_leg", lambda _m: True)

    called = {"free_first": False}

    async def _must_not_be_used(*_a, **_k):
        called["free_first"] = True
        raise AssertionError("repli non-stream appelé pour une requête streaming")

    monkeypatch.setattr(oc, "_try_free_model_first", _must_not_be_used)

    # La jambe free streaming répond un vrai flux chat (converti en Anthropic).
    recorder._free_upstream = lambda _endpoint, _body: FakeResponse(lines=CHAT_CHUNK_LINES)

    body = {
        "model": "deepseek-v4.1-flash-free",
        "max_tokens": 32,
        "stream": True,
        "messages": [{"role": "user", "content": "Say OK"}],
    }
    with client.stream("POST", "/v1/messages", json=body) as r:
        raw = b"".join(r.iter_bytes()).decode("utf-8", "replace")
        ctype = r.headers.get("content-type", "")

    assert called["free_first"] is False, "le repli non-stream a été utilisé en streaming"
    assert ctype.startswith("text/event-stream"), f"content-type inattendu: {ctype!r} — corps={raw[:300]!r}"
    assert "message_start" in raw and "message_stop" in raw, raw[:500]


def test_chat_stream_free_not_refused_when_all_keys_paused(client, recorder, monkeypatch):  # noqa: F811
    """`/v1/chat/completions` stream : free-direct, jamais 503 « prochain essai »."""
    monkeypatch.setattr(
        oc,
        "_get_auth_headers",
        lambda _protocol, entry=None: (_ for _ in ()).throw(oc.AllKeysPausedError(60)),
    )
    monkeypatch.setattr(oc, "_has_free_leg", lambda _m: True)

    called = {"free_first": False}

    async def _must_not_be_used(*_a, **_k):
        called["free_first"] = True
        raise AssertionError("repli non-stream appelé pour un chat streaming")

    monkeypatch.setattr(oc, "_try_free_model_first", _must_not_be_used)
    recorder._free_upstream = lambda _endpoint, _body: FakeResponse(lines=CHAT_CHUNK_LINES)

    body = {
        "model": "deepseek-v4.1-flash-free",
        "max_tokens": 32,
        "stream": True,
        "messages": [{"role": "user", "content": "Say OK"}],
    }
    with client.stream("POST", "/v1/chat/completions", json=body) as r:
        raw = b"".join(r.iter_bytes()).decode("utf-8", "replace")
        ctype = r.headers.get("content-type", "")

    assert called["free_first"] is False
    assert ctype.startswith("text/event-stream"), f"content-type inattendu: {ctype!r} — corps={raw[:300]!r}"
    assert "chat.completion.chunk" in raw, raw[:500]
