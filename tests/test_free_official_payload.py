"""Verrouille la copie exacte du client officiel (gate gratuit durci).

Contexte mesuré le 2026-10-25 : depuis le 2026-10-23T23:17:27Z, le gate
gratuit exige la signature COMPLÈTE du client (10 tools + prompt système).
Un corps réduit est refusé 403 (``FreeTierError``) ou 429.

Ces tests empêchent une future « optimisation » de re-minimiser le payload
et de re-casser silencieusement toute la jambe free.
"""

import json

import opencode as oc

OFFICIAL_TOOL_NAMES = [
    "bash",
    "edit",
    "glob",
    "grep",
    "read",
    "skill",
    "task",
    "todowrite",
    "webfetch",
    "write",
]

SYSTEM_MARKER = "You are opencode, an interactive CLI tool"


def test_le_fichier_de_capture_est_complet():
    """Le payload figé porte bien les 10 tools et le prompt système."""
    payload = oc._official_free_payload()
    names = [t["function"]["name"] for t in payload["tools"]]
    assert names == OFFICIAL_TOOL_NAMES
    assert payload["names"] == set(OFFICIAL_TOOL_NAMES)
    assert payload["system"]["role"] == "system"
    assert SYSTEM_MARKER in payload["system"]["content"]
    assert len(payload["system"]["content"]) > 20000


def test_un_corps_minimal_recoit_la_copie_integrale():
    """Un client générique (4 mots) doit partir avec la signature officielle."""
    body = {"model": "m", "messages": [{"role": "user", "content": "Dis OK"}]}
    wire, forced = oc._free_wire_body(body, force_stream=True)

    assert forced is True
    assert wire["stream"] is True
    assert wire["tool_choice"] == "auto"
    assert [t["function"]["name"] for t in wire["tools"]] == OFFICIAL_TOOL_NAMES
    assert wire["messages"][0]["role"] == "system"
    assert SYSTEM_MARKER in wire["messages"][0]["content"]
    # Les messages du client sont conservés après le prompt officiel.
    assert wire["messages"][-1] == {"role": "user", "content": "Dis OK"}
    # L'entrée n'est jamais mutée (retries/hedges réutilisent le dict).
    assert "tools" not in body and "stream" not in body


def test_un_corps_deja_conforme_traverse_sans_etre_modifie():
    """Le vrai client (payload complet) ne doit pas être altéré par le proxy."""
    payload = oc._official_free_payload()
    body = {
        "model": "m",
        "max_tokens": 32,
        "messages": [dict(payload["system"]), {"role": "user", "content": "hi"}],
        "tools": payload["tools"],
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    wire, forced = oc._free_wire_body(body, force_stream=True)

    assert forced is False
    assert wire == body
    assert oc._serialize_json_body(wire) == oc._serialize_json_body(body)


def test_les_outils_du_client_sont_conserves_apres_les_officiels():
    """Mesuré : 10 officiels + extras -> 200 ; un nom officiel renommé -> refus.

    Donc on fusionne (officiels d'abord, extras ensuite) au lieu d'écraser :
    un client tiers garde son function calling.
    """
    mine = {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Météo d'une ville.",
            "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
        },
    }
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "météo ?"}],
        "tools": [mine],
    }
    wire, _ = oc._free_wire_body(body, force_stream=True)
    names = [t["function"]["name"] for t in wire["tools"]]
    assert names[:10] == OFFICIAL_TOOL_NAMES
    assert names[10:] == ["get_weather"]
    # L'outil du client n'est pas copié : c'est le même objet.
    assert wire["tools"][10] is mine


def test_un_outil_client_qui_shadowe_un_nom_officiel_est_ignore():
    """Un outil nommé `read` côté client ne remplace pas la définition officielle."""
    shadow = {
        "type": "function",
        "function": {"name": "read", "description": "x", "parameters": {"type": "object"}},
    }
    body = {"model": "m", "messages": [], "tools": [shadow]}
    wire, _ = oc._free_wire_body(body, force_stream=True)
    names = [t["function"]["name"] for t in wire["tools"]]
    assert names == OFFICIAL_TOOL_NAMES
    assert wire["tools"][4] is not shadow


def test_l_ordre_des_cles_reste_celui_du_client():
    """Ordre officiel : model, max_tokens, messages, tools, tool_choice, stream."""
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    wire, _ = oc._free_wire_body(body, force_stream=True)
    cles = [k for k in wire if k in ("model", "max_tokens", "messages", "tools", "tool_choice", "stream")]
    assert cles == ["model", "messages", "tools", "tool_choice", "stream"]


def test_le_payload_est_serialisable_et_stable():
    """Deux appels donnent exactement les mêmes octets (pas de dérive)."""
    body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
    a, _ = oc._free_wire_body(body, force_stream=True)
    b, _ = oc._free_wire_body(body, force_stream=True)
    assert oc._serialize_json_body(a) == oc._serialize_json_body(b)
    # Le payload figé n'est pas modifié par les passages successifs.
    assert json.loads(json.dumps(oc._official_free_payload()["tools"])) == oc._official_free_payload()["tools"]


def test_le_collecteur_non_stream_recompose_les_tool_calls():
    """La jambe free force stream:true ; un client non-stream doit quand même
    recevoir ses appels d'outils (avant le 2026-10-25 ils étaient jetés)."""
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant"}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"type":"function","function":{"name":"get_weather","arguments":"{\\"city\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"\\"Paris\\"}"}}]},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    out = oc._collect_chat_completion(lines, "m")
    assert out is not None
    choice = out["choices"][0]
    msg = choice["message"]
    assert msg["content"] is None
    assert choice["finish_reason"] == "tool_calls"
    call = msg["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["type"] == "function"
    assert call["function"]["name"] == "get_weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "Paris"}


def test_le_collecteur_non_stream_ne_casse_pas_une_reponse_texte():
    lines = [
        'data: {"choices":[{"delta":{"content":"Bon"}}]}',
        'data: {"choices":[{"delta":{"content":"jour"},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    out = oc._collect_chat_completion(lines, "m")
    msg = out["choices"][0]["message"]
    assert msg["content"] == "Bonjour"
    assert "tool_calls" not in msg
    assert out["choices"][0]["finish_reason"] == "stop"
