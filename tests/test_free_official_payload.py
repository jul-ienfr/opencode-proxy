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
    # Client sans AUCUN outil : grille officielle pour le gate, et tool_choice
    # "none" pour que le modèle ne l'appelle jamais (le client ne les a pas).
    assert wire["tool_choice"] == "none"
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


def test_les_outils_du_client_sont_conserves_apres_la_grille_officielle():
    """Le gate exige les 10 noms officiels ; l'outil client reste derriere."""
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
    assert names == OFFICIAL_TOOL_NAMES + ["get_weather"]
    assert wire["tools"][-1] == mine
    assert wire["tool_choice"] == "auto"


def test_le_read_du_client_est_conserve_tel_quel():
    """Si le client fournit `read`, on garde SA définition (son schéma).

    Le gate ne vérifie que le nom ; remplacer le schéma (filePath vs
    file_path) casserait l'exécuteur du client.
    """
    mine = {
        "type": "function",
        "function": {"name": "read", "description": "x", "parameters": {"type": "object"}},
    }
    body = {"model": "m", "messages": [], "tools": [mine]}
    wire, _ = oc._free_wire_body(body, force_stream=True)
    names = [t["function"]["name"] for t in wire["tools"]]
    assert names == OFFICIAL_TOOL_NAMES
    read_index = OFFICIAL_TOOL_NAMES.index("read")
    assert wire["tools"][read_index] == mine


V2_SYSTEM = "You are an AI agent running in OpenCode, a coding agent harness. Help the user."
V2_TOOLS = [
    "edit",
    "glob",
    "grep",
    "question",
    "read",
    "shell",
    "skill",
    "subagent",
    "webfetch",
    "websearch",
    "write",
    "execute",
]


def _tool(name):
    return {
        "type": "function",
        "function": {"name": name, "description": "d", "parameters": {"type": "object"}},
    }


def test_corps_officiel_v2_traverse_tel_quel():
    """Client 2.1.4 : prompt v2 + 12 outils, dont `read`.

    Mesuré : système v2 + read -> 200. On ne doit PAS injecter le jeu v1,
    sinon le modèle appelle `bash`/`task`/`todowrite`, que le client v2 ne
    sait pas exécuter.
    """
    body = {
        "model": "m",
        "messages": [
            {"role": "system", "content": V2_SYSTEM},
            {"role": "user", "content": "salut"},
        ],
        "tools": [_tool(n) for n in V2_TOOLS],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": 32,
    }
    wire, forced = oc._free_wire_body(body, force_stream=True)
    assert forced is False
    assert wire == body
    assert [t["function"]["name"] for t in wire["tools"]] == V2_TOOLS


def test_systeme_v2_sans_read_recoit_les_outils_officiels():
    body = {
        "model": "m",
        "messages": [{"role": "system", "content": V2_SYSTEM}, {"role": "user", "content": "salut"}],
        "tools": [_tool("edit")],
        "stream": True,
    }
    wire, _ = oc._free_wire_body(body, force_stream=True)
    names = [t["function"]["name"] for t in wire["tools"]]
    # La grille officielle est ajoutée ; l'outil `edit` du client est conservé
    # à sa place officielle (son schéma reste exécutable côté client).
    assert names == OFFICIAL_TOOL_NAMES
    assert wire["tools"][OFFICIAL_TOOL_NAMES.index("edit")] == body["tools"][0]
    # Le prompt v2 est conservé : pas de prompt v1 ajouté par-dessus.
    assert wire["messages"][0]["content"].startswith(V2_SYSTEM)


def test_systeme_custom_avec_read_recoit_le_prompt_officiel():
    """Mesuré : système court + read -> 429. Il faut donc le prompt officiel,
    et la consigne d'outils est ajoutée à la fin du système du client."""
    body = {
        "model": "m",
        "messages": [{"role": "system", "content": "Tu es bref."}, {"role": "user", "content": "salut"}],
        "tools": [_tool("read")],
        "stream": True,
    }
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert "You are opencode, an interactive CLI tool" in wire["messages"][0]["content"]
    client_system = wire["messages"][1]["content"]
    assert client_system.startswith("Tu es bref.")
    assert "Call ONLY these client tools" in client_system
    # Le read du client est conservé à sa place officielle.
    read_index = OFFICIAL_TOOL_NAMES.index("read")
    assert wire["tools"][read_index] == body["tools"][0]


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


def test_le_collecteur_non_stream_conserve_le_raisonnement():
    """Mesuré : mimo et deepseek envoient delta.reasoning_content.

    Sans ce champ, un client non-stream ne reçoit que la réponse finale — et
    l'amont exige que le raisonnement lui soit renvoyé au tour suivant.
    """
    lines = [
        'data: {"choices":[{"delta":{"role":"assistant","reasoning_content":"17*23 = "}}]}',
        'data: {"choices":[{"delta":{"reasoning_content":"391.","content":"391"}}]}',
        'data: {"choices":[{"delta":{"content":" !"},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    out = oc._collect_chat_completion(lines, "m")
    msg = out["choices"][0]["message"]
    assert msg["content"] == "391 !"
    assert msg["reasoning_content"] == "17*23 = 391."


def test_le_collecteur_accepte_le_dialecte_reasoning():
    """mimo/nemotron émettent `reasoning` + `reasoning_details`, pas
    `reasoning_content`. Mesuré en direct sur l'amont le 2026-10-25."""
    lines = [
        'data: {"choices":[{"delta":{"reasoning":"je "}}]}',
        'data: {"choices":[{"delta":{"reasoning":"calcule"}}]}',
        'data: {"choices":[{"delta":{"reasoning_details":[{"type":"reasoning.text","text":" 17*23"}]}}]}',
        'data: {"choices":[{"delta":{"content":"391"},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]
    out = oc._collect_chat_completion(lines, "m")
    msg = out["choices"][0]["message"]
    assert msg["reasoning_content"] == "je calcule 17*23"
    assert msg["content"] == "391"
    # Parité OpenRouter : le détail est restitué aussi en non-stream.
    assert msg["reasoning_details"] == [{"type": "reasoning.text", "text": "je calcule 17*23"}]


def test_le_normaliseur_de_chunk_ajoute_l_alias_sans_ecraser():
    chunk = {"choices": [{"delta": {"reasoning": "abc", "reasoning_details": [{"text": "abc"}]}}]}
    assert oc._normalize_chat_reasoning_chunk(chunk) is True
    d = chunk["choices"][0]["delta"]
    assert d["reasoning_content"] == "abc"
    # Les champs d'origine restent intacts (clients OpenRouter).
    assert d["reasoning"] == "abc"
    assert d["reasoning_details"] == [{"text": "abc"}]

    # Déjà présent : jamais écrasé.
    chunk2 = {"choices": [{"delta": {"reasoning_content": "officiel", "reasoning": "autre"}}]}
    assert oc._normalize_chat_reasoning_chunk(chunk2) is False
    assert chunk2["choices"][0]["delta"]["reasoning_content"] == "officiel"


def test_le_normaliseur_ignore_les_chunks_sans_raisonnement():
    assert oc._normalize_chat_reasoning_chunk({"choices": [{"delta": {"content": "x"}}]}) is False
    assert oc._normalize_chat_reasoning_chunk({}) is False
