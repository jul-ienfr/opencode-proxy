"""[TROU 2 — A8] Noms d'outils > 64 sur P3 (client Chat → amont Chat).

Contrainte amont réelle : Anthropic accepte un nom d'outil de 200 caractères,
Chat/OpenAI le plafonne à **64**. Sur P3 le corps du client partait tel quel
(passthrough) : un nom de 100+ caractères atteignait l'amont non raccourci, et
rien ne restaurait le nom au retour.

Harnais : importé de ``tests/test_e2e_protocol_matrix.py`` (jamais copié) —
``recorder`` capture le corps **réellement** envoyé sur le fil, ``FakeResponse``
joue l'amont. Celui-ci est configuré pour reproduire ce que fait vraiment
l'amont Chat : **il rejoue le nom qu'il a reçu** (donc raccourci si l'aller est
correct), exactement comme un vrai amont qui ne connaît que le nom reçu.
Aucun stub ne « suppose » le comportement corrigé.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import opencode as oc

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_e2e_protocol_matrix import (  # noqa: E402
    EP_CHAT,
    PAID_CHAT,
    FakeResponse,
    _post,
    _sse_events,
    chat_completion,
    client,  # noqa: E402,F811  (fixture du harnais, réutilisée par les tests ci-dessous)
    recorder,  # noqa: E402,F811  (fixture du harnais)
)

# 100 caractères : au-delà de la limite Chat (64), en-deçà de la limite Anthropic (200).
LONG_NAME = "mcp__very_long_server_name__do_something_extremely_descriptive_with_a_long_suffix_for_testing_purposes"

assert len(LONG_NAME) == 102, len(LONG_NAME)


def _chat_response_echoing(body):
    """Réponse Chat qui rejoue le nom d'outil **reçu** (comportement amont réel).

    L'amont Chat ne connaît que ce qu'on lui envoie : s'il reçoit le nom long,
    il le renvoie/le rejette ; s'il reçoit un nom ≤64, il renvoie ce nom ≤64.
    """
    tools = body.get("tools") or []
    sent = tools[0]["function"]["name"] if tools else ""
    payload = chat_completion("call ok", tool_calls=True)
    payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"] = sent
    return FakeResponse(payload=payload)


def _chat_sse_echoing(body):
    """Flux SSE Chat qui rejoue le nom reçu (même contrat que le non-stream)."""
    tools = body.get("tools") or []
    sent = tools[0]["function"]["name"] if tools else ""
    lines = [
        'data: {"id":"chatcmpl_s","object":"chat.completion.chunk","created":1700000000,'
        '"model":"upstream","choices":[{"index":0,"delta":{"role":"assistant","content":""},'
        '"finish_reason":null}]}',
        'data: {"id":"chatcmpl_s","object":"chat.completion.chunk","created":1700000000,'
        f'"model":"upstream","choices":[{{"index":0,"delta":{{"tool_calls":[{{"index":0,'
        f'"id":"call_out_1","type":"function","function":{{"name":"{sent}","arguments":""}}}}]}},'
        '"finish_reason":null}]}',
        'data: {"id":"chatcmpl_s","object":"chat.completion.chunk","created":1700000000,'
        '"model":"upstream","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"{\\"city\\": \\"Paris\\"}"}}]},"finish_reason":null}]}',
        'data: {"id":"chatcmpl_s","object":"chat.completion.chunk","created":1700000000,'
        '"model":"upstream","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],'
        '"usage":{"prompt_tokens":11,"completion_tokens":22,"total_tokens":33}}',
        "data: [DONE]",
    ]
    return FakeResponse(lines=lines)


def _body(stream=False):
    body = {
        "model": PAID_CHAT,
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "Quel temps à Paris ?"}],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": LONG_NAME,
                    "description": "Météo",
                    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": LONG_NAME}},
    }
    if stream:
        body["stream"] = True
    return body


# ───────────────────────────── P3 non-stream ─────────────────────────────


def test_p3_long_tool_name_sanitized_upstream_and_restored_downstream(client, recorder):
    """Aller : l'amont reçoit ≤64. Retour : le client reçoit le nom d'origine."""
    recorder.set_upstream(lambda e, b, p: _chat_response_echoing(b))

    status, ctype, text = _post(client, "/v1/chat/completions", _body())

    assert status == 200, text[:400]
    assert ctype.startswith("application/json")
    assert recorder.last["endpoint"] == EP_CHAT

    # ── Aller : ce qui part réellement sur le fil.
    sent = recorder.last["body"]["tools"][0]["function"]["name"]
    assert len(sent) <= 64, f"nom d'outil non raccourci vers l'amont Chat : {sent!r} ({len(sent)} car.)"
    assert sent != LONG_NAME
    # tool_choice nommé doit suivre le même rename (sinon amont 400 « unknown tool »).
    assert recorder.last["body"]["tool_choice"]["function"]["name"] == sent

    # ── Retour : le client retrouve le nom qu'il a envoyé.
    payload = json.loads(text)
    got = payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"]
    assert got == LONG_NAME, f"nom non restauré au retour : {got!r}"


def test_p3_long_tool_name_in_history_sanitized(client, recorder):
    """Tour N+1 : l'historique Chat rejoue le nom long → même rename."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=chat_completion("ok")))
    body = _body()
    body["messages"] = [
        {"role": "user", "content": "Quel temps à Paris ?"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": LONG_NAME, "arguments": '{"city": "Paris"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "18°C"},
    ]

    status, _ctype, text = _post(client, "/v1/chat/completions", body)

    assert status == 200, text[:400]
    up = recorder.last["body"]
    hist_name = up["messages"][1]["tool_calls"][0]["function"]["name"]
    assert len(hist_name) <= 64, f"nom d'historique non raccourci : {hist_name!r} ({len(hist_name)} car.)"
    # Cohérence : l'historique doit porter exactement le même nom que tools[].
    assert hist_name == up["tools"][0]["function"]["name"]


# ─────────────────────────────── P3 stream ───────────────────────────────


def test_p3_long_tool_name_sanitized_upstream_and_restored_stream(client, recorder):
    """Stream : même contrat que le non-stream (aller ≤64, retour d'origine)."""
    recorder.set_upstream(lambda e, b, p: _chat_sse_echoing(b))

    status, ctype, text = _post(client, "/v1/chat/completions", _body(stream=True), stream=True)

    assert status == 200
    assert ctype.startswith("text/event-stream")
    assert recorder.last["endpoint"] == EP_CHAT

    sent = recorder.last["body"]["tools"][0]["function"]["name"]
    assert len(sent) <= 64, f"nom d'outil non raccourci vers l'amont Chat (stream) : {sent!r} ({len(sent)} car.)"

    events = _sse_events(text)
    names = [
        tc["function"]["name"]
        for _ev, payload in events
        for ch in (payload.get("choices") or [])
        for tc in (ch.get("delta") or {}).get("tool_calls") or []
        if tc.get("function", {}).get("name")
    ]
    assert names, f"aucun tool_call dans le flux client : {text[:400]}"
    assert names[0] == LONG_NAME, f"nom non restauré dans le flux : {names[0]!r}"


def test_p3_short_tool_name_untouched(client, recorder):
    """Non-régression : un nom ≤64 valide n'est ni renommé ni « restauré »."""
    recorder.set_upstream(lambda e, b, p: _chat_response_echoing(b))
    body = _body()
    body["tools"][0]["function"]["name"] = "get_weather"
    body["tool_choice"] = {"type": "function", "function": {"name": "get_weather"}}

    status, _ctype, text = _post(client, "/v1/chat/completions", body)

    assert status == 200
    assert recorder.last["body"]["tools"][0]["function"]["name"] == "get_weather"
    assert recorder.last["body"]["tool_choice"]["function"]["name"] == "get_weather"
    payload = json.loads(text)
    assert payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "get_weather"


def test_p3_no_private_map_key_on_wire(client, recorder):
    """La clé privée de rename ne doit jamais atteindre l'amont.

    Le stub ``_do_request_with_retry`` capture le dict Python AVANT
    sérialisation ; le wire réel passe par ``_serialize_json_body`` (le seam
    amont de production), qui strippe la clé. On mesure donc sur ces bytes-là,
    pas sur le dict du stub (sinon on mesurerait le harnais, pas le proxy).
    """
    recorder.set_upstream(lambda e, b, p: _chat_response_echoing(b))

    status, _ctype, _text = _post(client, "/v1/chat/completions", _body())

    assert status == 200
    wire = oc._serialize_json_body(recorder.last["body"])
    assert b"_tool_name_map" not in wire
    # Le rename a bien eu lieu (sinon l'assertion ci-dessus serait vide de sens).
    assert len(recorder.last["body"]["tools"][0]["function"]["name"]) <= 64


@pytest.mark.parametrize("n", [64, 65, 104])
def test_p3_boundary_lengths(client, recorder, n):
    """Bord : 64 intact, 65 et 104 raccourcis — et restaurés au retour."""
    name = ("x" * (n - 12)) + "__boundary__"
    name = name[:n]
    recorder.set_upstream(lambda e, b, p: _chat_response_echoing(b))
    body = _body()
    body["tools"][0]["function"]["name"] = name
    body["tool_choice"] = {"type": "function", "function": {"name": name}}

    status, _ctype, text = _post(client, "/v1/chat/completions", body)

    assert status == 200
    sent = recorder.last["body"]["tools"][0]["function"]["name"]
    if n <= 64:
        assert sent == name
    else:
        assert len(sent) <= 64
    payload = json.loads(text)
    assert payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == name
