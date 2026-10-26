"""Tool calls amont sans nom/id : réparation avant le client.

Symptôme mesuré (deepseek-harness, 2026-10-26) : l'amont free a émis un
``tool_call`` avec ``id=""`` et ``name=""`` alors que les arguments étaient
ceux de ``pwsh``. Le harness a créé un ``tool/call`` vide, n'a pu apparier
aucun ``tool/result`` et a refusé de rejouer le tour ::

    This turn failed format v4 tool/result at seq N requires toolCallId
    matching its tool source

Ces tests verrouillent la réparation (nom inféré des arguments) et le refus
de tout appel vide (drop + finish ``stop``, jamais ``tool_calls``).
"""

import json

import opencode as oc

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "pwsh",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}, "description": {"type": "string"}},
                "required": ["command", "description"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob",
            "parameters": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "grep",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string"},
                    "include": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
]


def _chunk(delta, finish=None):
    return {
        "id": "chatcmpl-upstream",
        "object": "chat.completion.chunk",
        "created": 1700000000,
        "model": "upstream",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def _tool_calls(chunk):
    return (chunk.get("choices") or [{}])[0].get("delta", {}).get("tool_calls") or []


def test_infere_pwsh_depuis_les_arguments_meme_partiels():
    assert oc._infer_tool_name_from_arguments('{"command":"npm test"}', TOOLS) == "pwsh"
    assert oc._infer_tool_name_from_arguments('{"command":"npm', TOOLS) == "pwsh"


def test_infere_meme_si_l_amont_a_omis_l_ouverture():
    """Mesure dsh : les arguments commencent par ``command":`` (pas ``{"``)."""
    malformed = 'command": "npm test","description":"test"}'
    assert oc._infer_tool_name_from_arguments(malformed, TOOLS) == "pwsh"
    repaired = oc._repair_tool_arguments_prefix(malformed)
    assert json.loads(repaired) == {"command": "npm test", "description": "test"}


def test_infere_write_et_read_par_les_cles_requises():
    assert oc._infer_tool_name_from_arguments('{"file_path":"a.ts","content":"x"}', TOOLS) == "write"
    assert oc._infer_tool_name_from_arguments('{"file_path":"a.ts"}', TOOLS) == "read"


def test_ne_devine_jamais_entre_deux_outils_compatibles():
    # glob et grep acceptent tous deux `pattern` : aucun choix sûr.
    assert oc._infer_tool_name_from_arguments('{"pattern":"*.py"}', TOOLS) == ""
    assert oc._infer_tool_name_from_arguments('{"inconnu":1}', TOOLS) == ""
    assert oc._infer_tool_name_from_arguments("", TOOLS) == ""


def test_healer_repare_le_tool_call_vide_mesure_sur_dsh():
    """Arguments de pwsh + id/nom vides : le client doit recevoir un vrai appel."""
    healer = oc._ChatToolCallHealer(TOOLS)
    # Le flux dsh commence par ``command`` (l'ouverture ``{"`` manque) et le
    # nom/id sont vides. Rien d'invalide ne doit partir avant l'inférence.
    pieces = [
        "command",
        '"',
        ": ",
        '"npm test"',
        ',"description":"test"}',
    ]
    emitted = []
    for piece in pieces:
        emitted.extend(healer.feed(_chunk({"tool_calls": [{"index": 0, "function": {"arguments": piece}}]})))
    calls = [tc for c in emitted for tc in _tool_calls(c)]
    assert calls, "aucun tool_call émis"
    # Les fragments suivants complètent le premier delta : on recompose comme
    # le ferait un client OpenAI (id/nom au premier, arguments concaténés).
    acc = {"id": "", "name": "", "arguments": ""}
    for call in calls:
        if call.get("id"):
            acc["id"] = call["id"]
        fn = call.get("function") or {}
        if fn.get("name"):
            acc["name"] = fn["name"]
        acc["arguments"] += fn.get("arguments") or ""
    assert acc["id"].startswith("call_")
    assert acc["name"] == "pwsh"
    assert json.loads(acc["arguments"]) == {
        "command": "npm test",
        "description": "test",
    }
    assert healer.used_names == ["pwsh"]
    assert healer.emitted_indices == {0}


def test_healer_synthetise_un_id_quand_le_nom_est_present():
    healer = oc._ChatToolCallHealer(TOOLS)
    out = healer.feed(
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "",
                        "function": {
                            "name": "pwsh",
                            "arguments": '{"command":"npm test","description":"test"}',
                        },
                    }
                ]
            }
        )
    )
    call = _tool_calls(out[0])[0]
    assert call["id"].startswith("call_")
    assert call["function"]["name"] == "pwsh"


def test_healer_repare_le_prefixe_quand_le_nom_est_present():
    """Même avec un nom valide, ``command": …`` doit redevenir ``{"command": …``."""
    healer = oc._ChatToolCallHealer(TOOLS)
    first = healer.feed(
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "id": "call_ok",
                        "function": {"name": "pwsh", "arguments": "command"},
                    }
                ]
            }
        )
    )
    call = _tool_calls(first[0])[0]
    assert call["function"]["arguments"] == '{"command'
    # Le fragment suivant complète le JSON ; le client concatène.
    rest = healer.feed(
        _chunk(
            {
                "tool_calls": [
                    {
                        "index": 0,
                        "function": {
                            "arguments": '": "npm test","description":"test"}',
                        },
                    }
                ]
            }
        )
    )
    call = _tool_calls(rest[0])[0]
    assert call["function"]["arguments"] == '": "npm test","description":"test"}'
    assert json.loads('{"command' + call["function"]["arguments"]) == {
        "command": "npm test",
        "description": "test",
    }


def test_healer_ecarte_un_appel_non_identifiable_et_ne_dit_pas_tool_calls():
    healer = oc._ChatToolCallHealer(TOOLS)
    first = _chunk(
        {
            "tool_calls": [
                {
                    "index": 0,
                    "id": "",
                    "function": {"name": "", "arguments": '{"inconnu":1}'},
                }
            ]
        }
    )
    assert healer.feed(first) == []
    # Le finish amont annonce tool_calls : sans appel valide, il devient stop
    # et n'est même pas émis si rien n'est parti (retry « stream vide »).
    assert healer.feed(_chunk({}, finish="tool_calls")) == []
    assert healer.dropped_calls == 1


def test_collecteur_non_stream_repare_le_nom_et_l_id():
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"",'
        '"function":{"name":"","arguments":"{\\"command\\":\\"npm test\\",'
        '\\"description\\":\\"test\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    out = oc._collect_chat_completion(lines, "m", tools=TOOLS)
    call = out["choices"][0]["message"]["tool_calls"][0]
    assert call["id"].startswith("call_free_")
    assert call["function"]["name"] == "pwsh"
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_collecteur_non_stream_repare_le_prefixe_manquant():
    """Même réparation que le stream : ``command": …`` → ``{"command": …``."""
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"",'
        '"function":{"name":"","arguments":"command\\": \\"npm test\\",'
        '\\"description\\":\\"test\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    out = oc._collect_chat_completion(lines, "m", tools=TOOLS)
    call = out["choices"][0]["message"]["tool_calls"][0]
    assert call["function"]["name"] == "pwsh"
    assert json.loads(call["function"]["arguments"]) == {
        "command": "npm test",
        "description": "test",
    }


def test_collecteur_non_stream_ecarte_un_nom_vide_non_identifiable():
    lines = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"",'
        '"function":{"name":"","arguments":"{\\"inconnu\\":1}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    out = oc._collect_chat_completion(lines, "m", tools=TOOLS)
    msg = out["choices"][0]["message"]
    assert "tool_calls" not in msg
    assert out["choices"][0]["finish_reason"] == "stop"
