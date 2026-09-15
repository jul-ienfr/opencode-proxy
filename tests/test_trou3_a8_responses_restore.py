"""[TROU 3 — A8] P5 : les noms d'outils raccourcis à l'aller doivent être RESTAURÉS.

Contrainte réelle : Anthropic accepte 200 caractères pour un nom d'outil,
Chat/OpenAI 64. Un nom > 64 partant vers un amont Chat/Responses est raccourci par
``sanitize_tool_names`` (aller) et la map ``{short: original}`` voyage avec la
requête sous ``_TOOL_NAME_MAP_KEY``. Si le retour ne consomme pas cette map, le
client reçoit un nom RACCOURCI — un outil qu'il n'a jamais envoyé, donc non
routable côté client.

Asymétrie mesurée dans ``app/protocol/mapping.py`` :
``_responses_to_chat_response`` / ``_responses_to_anthropic_response`` ont un
``name_map`` ; ``openai_chat_to_responses`` (retour P5) et
``anthropic_to_openai_responses`` (retour P6) n'en avaient pas.

MESURES (assertions ci-dessous, vertes avant ET après correctif) :

* P5 aller (``/v1/responses`` → modèle free ``muse-*``/``spark-*``, amont
  ``/responses``) : le nom > 64 est bien raccourci ; ``_tool_name_map`` est
  construite par ``_anthropic_to_responses_request`` et le nom raccourci part
  sur le fil → le trou 3 est bien une **restauration absente au retour**, pas un
  « A8 absent des deux côtés ».
* P6 aller (``/v1/responses`` → amont Anthropic) : l'amont reçoit le nom client
  **tel quel** (78 car.). La jambe Anthropic de P6 ne passe ni par
  ``_sanitize_native_responses_request`` ni par ``anthropic_to_openai`` : aucune
  map n'y est construite, donc le restore-retour y est un no-op. C'est un trou
  distinct, NON corrigé ici (cf. LIMITE du rapport).

PIÈGE MESURÉ : quand l'amont de P5 rend un objet **Responses natif**, le handler
le passe en verbatim (``oai_resp = data``) — le convertisseur n'est pas appelé du
tout. La restauration n'est donc atteignable que quand l'amont rend du **Chat**
(``choices``/``tool_calls``), ce que fait le témoin.

HORS PÉRIMÈTRE (mesuré, non corrigé ici) : le chemin **stream** de P5 ne collecte
que ``content``/``reasoning_content`` du flux Chat amont — un ``tool_calls``
d'amont n'atteint jamais ``chat_resp``, donc aucun ``function_call`` n'est émis.
Le témoin ne peut donc pas être doublé en streaming avant ce correctif-là.

Harnais hermétique : mêmes doubles amont que ``tests/test_e2e_protocol_matrix.py``
(aucun réseau), client ASGI ``TestClient`` sur l'app réelle, corps amont capturé.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from test_e2e_protocol_matrix import (
    EP_ANTHRO,
    EP_RESPONSES,
    PAID_ANTHRO,
    PAID_RESPONSES,
    FakeResponse,
    UpstreamRecorder,
    _install_seams,
    _post,
)

import opencode as oc
from protocol_mapping import sanitize_tool_names

# Nom volontairement > 64 (borne Chat/OpenAI) : tout amont Chat DOIT le
# raccourcir, ce qui rend la restauration au retour indispensable.
LONG_NAME = "mcp__trou3_plugin_super_long_tool_name_that_exceeds_the_sixty_four_chars_limit_zz"
EXPECTED_SHORT = sanitize_tool_names([{"name": LONG_NAME}])[0][0]["name"]
assert len(LONG_NAME) > 64, "pré-requis du test : le nom doit dépasser la borne Chat"
assert len(EXPECTED_SHORT) <= 64 and EXPECTED_SHORT != LONG_NAME, "pré-requis : raccourcissement"

TOOL_SCHEMA = {"type": "object", "properties": {"city": {"type": "string"}}}

# Outils client au format **Responses natif** — c'est la forme que reçoit
# ``/v1/responses``, sur P5 comme sur P6 (nom à plat, schéma sous `parameters`).
RESPONSES_TOOLS = [{"type": "function", "name": LONG_NAME, "description": "d", "parameters": TOOL_SCHEMA}]

RESPONSES_INPUT = [
    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "Quel temps à Paris ?"}]}
]


def _anthro_message_with_tool(name: str) -> dict:
    """Réponse amont Anthropic portant un ``tool_use`` au nom donné."""
    return {
        "id": "msg_trou3",
        "type": "message",
        "role": "assistant",
        "model": "upstream",
        "content": [
            {"type": "text", "text": "je regarde"},
            {"type": "tool_use", "id": "toolu_trou3_1", "name": name, "input": {"city": "Paris"}},
        ],
        "stop_reason": "tool_use",
        "stop_sequence": None,
        "usage": {"input_tokens": 11, "output_tokens": 22},
    }


def _responses_object_with_tool(name: str) -> dict:
    """Réponse amont Responses **natif** portant un ``function_call``."""
    return {
        "id": "resp_trou3",
        "object": "response",
        "status": "completed",
        "model": "upstream",
        "output": [
            {
                "type": "message",
                "id": "msg_trou3",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "je regarde"}],
            },
            {
                "type": "function_call",
                "call_id": "call_trou3_1",
                "name": name,
                "arguments": json.dumps({"city": "Paris"}),
                "status": "completed",
            },
        ],
        "usage": {"input_tokens": 11, "output_tokens": 22, "total_tokens": 33},
    }


def _chat_completion_with_tool(name: str) -> dict:
    """Réponse amont **Chat** portant un ``tool_calls`` au nom donné."""
    return {
        "id": "chatcmpl_trou3",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "upstream",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": "je regarde",
                    "tool_calls": [
                        {
                            "id": "call_trou3_1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps({"city": "Paris"})},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    }


def _upstream_tool_names(body: dict) -> list[str]:
    """Noms d'outils du corps amont, format Responses (à plat) ou Anthropic."""
    names: list[str] = []
    for t in body.get("tools") or []:
        if not isinstance(t, dict):
            continue
        if isinstance(t.get("name"), str):
            names.append(t["name"])
        elif isinstance(t.get("function"), dict):
            names.append(t["function"].get("name", ""))
    return names


def _client_tool_name_from_responses(text: str) -> str:
    """Nom d'outil émis au client Responses (P5/P6)."""
    payload = json.loads(text)
    calls = [it for it in payload.get("output", []) if it.get("type") == "function_call"]
    assert calls, f"aucun function_call dans la réponse client : {payload!r}"
    return calls[0].get("name", "")


def _p5_body(**extra) -> dict:
    body = {
        "model": PAID_RESPONSES,
        "max_output_tokens": 512,
        "input": RESPONSES_INPUT,
        "tools": RESPONSES_TOOLS,
    }
    body.update(extra)
    return body


@pytest.fixture
def client():
    return TestClient(oc.app)


@pytest.fixture
def recorder(monkeypatch):
    rec = UpstreamRecorder()
    _install_seams(monkeypatch, rec)
    return rec


# ══════════════ 1. MESURES — ce qui part réellement vers l'amont ══════════════


def test_p5_measure_upstream_receives_shortened_tool_name(client, recorder):
    """P5 aller — MESURE : l'amont Chat reçoit le nom raccourci ≤64."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=_responses_object_with_tool(EXPECTED_SHORT)))

    status, _ctype, _text = _post(client, "/v1/responses", _p5_body())

    assert status == 200
    assert recorder.last["endpoint"] == EP_RESPONSES
    sent = _upstream_tool_names(recorder.last["body"])
    assert sent == [EXPECTED_SHORT], f"nom envoyé à l'amont P5 : {sent!r}"


def test_p6_measure_upstream_receives_tool_name_verbatim(client, recorder):
    """P6 aller — MESURE : l'amont Anthropic reçoit le nom client TEL QUEL.

    Aucune map n'est construite sur cette jambe : le restore-retour y est un
    no-op (trou distinct, non corrigé ici).
    """
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=_anthro_message_with_tool(EXPECTED_SHORT)))
    body = {
        "model": PAID_ANTHRO,
        "max_output_tokens": 512,
        "input": RESPONSES_INPUT,
        "tools": RESPONSES_TOOLS,
    }

    status, _ctype, _text = _post(client, "/v1/responses", body)

    assert status == 200
    assert recorder.last["endpoint"] == EP_ANTHRO
    sent = _upstream_tool_names(recorder.last["body"])
    assert sent == [LONG_NAME], f"nom envoyé à l'amont P6 : {sent!r}"


def test_p5_measure_native_responses_upstream_bypasses_converter(client, recorder):
    """P5 — MESURE : un amont Responses natif passe VERBATIM (convertisseur non appelé).

    ``oai_resp = data`` : le nom raccourci par l'amont ressort donc raccourci,
    quel que soit le ``name_map``. Chemin non corrigé (hors périmètre : ce n'est
    pas un site d'appel de ``openai_chat_to_responses``).
    """
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=_responses_object_with_tool(EXPECTED_SHORT)))

    status, _ctype, text = _post(client, "/v1/responses", _p5_body())

    assert status == 200
    got = _client_tool_name_from_responses(text)
    assert got == EXPECTED_SHORT, f"P5 natif : nom émis {got!r} (attendu {EXPECTED_SHORT!r} verbatim)"


# ══════════════ 2. TÉMOINS — le retour restaure-t-il le nom client ? ══════════════


def test_p5_upstream_short_name_is_restored_to_client(client, recorder):
    """P5 témoin non-stream — l'amont Chat rend le nom raccourci, le client reçoit l'ORIGINAL."""
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=_chat_completion_with_tool(EXPECTED_SHORT)))

    status, _ctype, text = _post(client, "/v1/responses", _p5_body())

    assert status == 200
    got = _client_tool_name_from_responses(text)
    assert got == LONG_NAME, (
        f"P5 : le client reçoit {got!r} au lieu de {LONG_NAME!r} — "
        "outil jamais envoyé par le client, donc non routable"
    )


def test_p6_upstream_short_name_is_not_restored_on_anthropic_leg(client, recorder):
    """P6 — caractérise le no-op : sans map, le nom de l'amont ressort inchangé.

    La jambe Anthropic de P6 ne construit pas de ``name_map`` (l'amont accepte
    200 caractères) : le paramètre existe pour la symétrie des convertisseurs,
    mais il ne porte aujourd'hui aucune preuve sur P6.
    """
    recorder.set_upstream(lambda e, b, p: FakeResponse(payload=_anthro_message_with_tool(EXPECTED_SHORT)))
    body = {
        "model": PAID_ANTHRO,
        "max_output_tokens": 512,
        "input": RESPONSES_INPUT,
        "tools": RESPONSES_TOOLS,
    }

    status, _ctype, text = _post(client, "/v1/responses", body)

    assert status == 200
    got = _client_tool_name_from_responses(text)
    assert got == EXPECTED_SHORT, f"P6 : nom émis {got!r} (attendu {EXPECTED_SHORT!r} sans map)"
