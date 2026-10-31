"""Verrouille le CORPS envoye a la jambe free : sequence de cles du client.

MESURE : sur 243 requetes du client officiel 1.18.31, l'ordre des cles du
corps chat est identique dans 242 cas :
    model, max_tokens, messages, tools, tool_choice, stream, stream_options
et ``tool_choice: "auto"`` est present 242/242 fois dès qu'il y a des tools.

Avant correctif, la jambe free produisait :
    model, stream, stream_options, max_tokens, messages, tools
et n'emettait jamais tool_choice.
"""

import json
import sys

sys.path.insert(0, ".")
import opencode as oc  # noqa: E402

CLIENT_KEY_ORDER = [
    "model",
    "max_tokens",
    "messages",
    "tools",
    "tool_choice",
    "stream",
    "stream_options",
]

_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "d",
        "parameters": {"type": "object", "properties": {}},
    },
}

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


def _chat_tool(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _body(**over):
    base = {
        "model": "mimo-v2.5-free",
        "max_tokens": 32000,
        "messages": [{"role": "user", "content": "bonjour"}],
        "tools": [_TOOL, {**_TOOL, "function": {**_TOOL["function"], "name": "read"}}],
        "tool_choice": "auto",
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    base.update(over)
    return base


def test_body_key_order_matches_client():
    """L'ordre des cles produites est celui du client (242/243 mesures)."""
    wire, _ = oc._free_wire_body(_body(), force_stream=True)
    assert list(wire) == CLIENT_KEY_ORDER


def test_body_key_order_in_serialized_bytes():
    """L'ordre doit survivre a la serialisation (c'est ce qui part)."""
    wire, _ = oc._free_wire_body(_body(), force_stream=True)
    raw = oc._serialize_json_body(wire).decode()
    pos = [(k, raw.find(f'"{k}"')) for k in CLIENT_KEY_ORDER]
    assert all(p >= 0 for _, p in pos), f"cle absente des octets : {raw[:200]}"
    assert [k for k, _ in sorted(pos, key=lambda x: x[1])] == CLIENT_KEY_ORDER


def test_tool_choice_auto_added_when_tools_present():
    """Le client pose « auto » des qu'il y a des tools ; on fait pareil."""
    body = _body()
    body.pop("tool_choice")
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert wire["tool_choice"] == "auto"


def test_tool_choice_none_quand_le_client_n_a_aucun_outil():
    """Corps nu : grille officielle pour le gate, mais aucun appel autorisé."""
    wire, _ = oc._free_wire_body(
        {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True},
        force_stream=True,
    )
    assert wire["tool_choice"] == "none"
    assert [t["function"]["name"] for t in wire["tools"]] == OFFICIAL_TOOL_NAMES
    systems = [m for m in wire["messages"] if m.get("role") == "system"]
    assert systems and len(systems[0]["content"]) > 20000, "prompt officiel complet"
    assert list(wire) == [k for k in CLIENT_KEY_ORDER if k in wire]


def test_summary_request_gets_official_grid_auto_and_max_tokens():
    """La compaction Hermes n'a ni tools ni systeme : le gate exige quand meme
    la grille officielle, `tool_choice: auto` et un `max_tokens`."""
    body = {
        "model": "m",
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {
                "role": "user",
                "content": (
                    "You are a summarization agent creating a context checkpoint. "
                    "Treat the conversation turns below as source material. " + ("turn " * 2000)
                ),
            }
        ],
    }
    wire, _ = oc._free_wire_body(body, force_stream=True)
    names = [t["function"]["name"] for t in wire["tools"]]
    assert names == OFFICIAL_TOOL_NAMES
    assert wire["tool_choice"] == "auto"
    assert wire["max_tokens"] == 4096
    assert wire["reasoning_effort"] == "none"
    systems = [m for m in wire["messages"] if m.get("role") == "system"]
    assert len(systems) == 1
    assert len(systems[0]["content"]) > 20000, "le prompt officiel ne doit jamais etre tronque"
    assert "summarization request" in systems[0]["content"]
    assert "max_tokens" not in body and "tools" not in body


def test_summary_request_keeps_client_max_tokens():
    body = {
        "model": "m",
        "stream": True,
        "max_tokens": 4096,
        "messages": [
            {
                "role": "user",
                "content": "You are a summarization agent creating a context checkpoint. x",
            }
        ],
    }
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert wire["max_tokens"] == 4096


def test_summary_request_caps_output_and_truncates_input():
    """Compaction : 128k de sortie et 54 Ko de transcript -> 4k / 16 Ko."""
    client_content = (
        "You are a summarization agent creating a context checkpoint. "
        "TURNS TO SUMMARIZE:\n" + ("turn detail " * 3000) + "\nEND TEMPLATE"
    )
    body = {
        "model": "m",
        "stream": True,
        "max_tokens": 128000,
        "messages": [{"role": "user", "content": client_content}],
    }
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert wire["max_tokens"] == 4096
    assert wire["reasoning_effort"] == "none"
    user_messages = [m for m in wire["messages"] if m.get("role") == "user"]
    assert len(user_messages) == 1
    assert len(user_messages[0]["content"]) <= 16384 + 200
    assert "summary input truncated by proxy" in user_messages[0]["content"]
    assert body["messages"][0]["content"] == client_content, "corps client intact"
    assert body["max_tokens"] == 128000


def test_hermes_gets_official_grid_plus_core_client_tools():
    """Hermes n'a aucun nom officiel : on injecte les 10 + 9 outils client.

    Mesure 2026-10-30 : `read` + 18 outils Hermes -> faux quota ; grille
    officielle + 9 outils client -> 200 ; 10 + 18 -> faux quota. On garde les
    9 outils client les plus utiles (terminal, read_file, write_file, patch,
    search_files, execute_code, delegate_task, web_search, skill_view).
    """
    body = _body()
    body["tools"] = [
        _chat_tool(name)
        for name in (
            "terminal",
            "read_file",
            "write_file",
            "patch",
            "search_files",
            "execute_code",
            "delegate_task",
            "web_search",
            "skill_view",
            "manage_connections",
            "text_to_speech",
        )
    ]
    wire, _ = oc._free_wire_body(body, force_stream=True)
    names = [t["function"]["name"] for t in wire["tools"]]
    assert names[:10] == OFFICIAL_TOOL_NAMES
    assert len(names) == 19
    for core in (
        "terminal",
        "read_file",
        "write_file",
        "patch",
        "search_files",
        "execute_code",
        "delegate_task",
        "web_search",
        "skill_view",
    ):
        assert core in names
    assert "manage_connections" not in names
    assert "text_to_speech" not in names
    assert len(body["tools"]) == 11, "le corps client ne doit pas etre filtre"


def test_hermes_long_system_is_truncated_on_free_wire():
    """Le prompt Hermes de 31 Ko est coupe a 4 Ko ; l'officiel reste complet."""
    client_system = "# SOUL.md\n" + ("consigne " * 3000)
    body = _body(max_tokens=32000)
    body["messages"] = [
        {"role": "system", "content": client_system},
        {"role": "user", "content": "bonjour"},
    ]
    body["tools"] = [_chat_tool("terminal"), _chat_tool("read_file")]
    wire, _ = oc._free_wire_body(body, force_stream=True)
    systems = [m for m in wire["messages"] if m.get("role") == "system"]
    assert len(systems) == 2
    assert len(systems[0]["content"]) > 20000, "prompt officiel complet"
    assert len(systems[1]["content"]) <= 4096 + 600
    assert "MUST NOT be called" in systems[1]["content"]
    assert "Call ONLY these client tools" in systems[1]["content"]
    assert len(body["messages"][0]["content"]) == len(client_system), "corps client intact"


def test_harness_system_is_not_truncated():
    """Un client deja aligne sur la grille officielle garde son prompt."""
    harness_system = "You are deepseek harness. " + ("policy " * 2000)
    body = _body(max_tokens=32000)
    body["messages"] = [
        {"role": "system", "content": harness_system},
        {"role": "user", "content": "bonjour"},
    ]
    body["tools"] = [
        _chat_tool(name)
        for name in (
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
            "subagent",
        )
    ]
    wire, _ = oc._free_wire_body(body, force_stream=True)
    systems = [m for m in wire["messages"] if m.get("role") == "system"]
    assert any(s["content"].startswith(harness_system) for s in systems)
    assert not any(len(s["content"]) < len(harness_system) for s in systems)


def test_harness_with_official_names_keeps_its_toolset():
    """Le harnais DeepSeek a deja bash/edit/read/... : on ne le remplace pas."""
    body = _body()
    body["tools"] = [
        _chat_tool(name)
        for name in (
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
            "subagent",
            "workflow",
        )
    ]
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert [t["function"]["name"] for t in wire["tools"]] == [t["function"]["name"] for t in body["tools"]]


def test_existing_tool_choice_never_overwritten():
    """Un tool_choice fourni par le client est conserve tel quel."""
    body = _body(tool_choice="auto")
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert wire["tool_choice"] == "auto"


def test_input_never_mutated():
    """La fonction reste sans effet de bord (retries/hedges partagent le dict)."""
    body = _body()
    body.pop("tool_choice")
    avant = json.dumps(body, sort_keys=False)
    oc._free_wire_body(body, force_stream=True)
    assert json.dumps(body, sort_keys=False) == avant
    assert "tool_choice" not in body


def test_idempotent():
    """Deux passages ne changent plus rien (reprises, hedges)."""
    w1, _ = oc._free_wire_body(_body(), force_stream=True)
    w2, _ = oc._free_wire_body(w1, force_stream=True)
    assert list(w1) == list(w2) == CLIENT_KEY_ORDER


def test_unknown_keys_kept_at_end():
    """Une cle non mesuree chez le client n'est ni perdue ni deplacee au hasard."""
    wire, _ = oc._free_wire_body(_body(extra_maison=1), force_stream=True)
    assert wire["extra_maison"] == 1
    assert list(wire)[:7] == CLIENT_KEY_ORDER
    assert list(wire)[7] == "extra_maison"


def test_responses_body_untouched():
    """Corps Responses : ordre non mesure chez le client -> on n'invente rien."""
    body = {"model": "m", "input": [{"role": "user", "content": "x"}], "stream": True}
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert "tool_choice" not in wire, "pas de tool_choice invente en forme Responses"
    assert "prompt_cache_key" in wire, "prompt_cache_key attendu sur /responses"
    assert list(wire)[:2] == ["model", "input"]


def test_max_tokens_clamped_to_official_cap():
    """Hermes demande 128000 ; l'amont free n'accepte que le plafond officiel.

    Mesure : le harnais DeepSeek (32768) passe, Hermes (128000) prend un 429
    sur une conversation de ~318 Ko. Le corps client ne doit pas etre modifie.
    """
    body = _body(max_tokens=128000)
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert wire["max_tokens"] == 32000
    assert body["max_tokens"] == 128000


def test_max_completion_tokens_clamped_to_official_cap():
    """Forme v2 (`max_completion_tokens`) : meme plafond, meme non-mutation."""
    body = _body()
    body.pop("max_tokens")
    body["max_completion_tokens"] = 128000
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert wire["max_completion_tokens"] == 32000
    assert body["max_completion_tokens"] == 128000


def test_small_output_budget_is_preserved():
    """Un client qui demande moins que le plafond n'est pas releve."""
    body = _body(max_tokens=16000)
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert wire["max_tokens"] == 16000


def test_responses_max_output_tokens_clamped():
    """Forme Responses : le champ s'appelle `max_output_tokens`."""
    body = {
        "model": "m",
        "input": [{"role": "user", "content": "x"}],
        "stream": True,
        "max_output_tokens": 128000,
    }
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert wire["max_output_tokens"] == 32000
    assert body["max_output_tokens"] == 128000


def test_clamp_does_not_mutate_the_client_body():
    """La copie d'envoi est isolee, messages compris."""
    body = _body(max_tokens=128000)
    body["messages"] = [
        {"role": "user", "content": "bonjour"},
        {"role": "assistant", "content": "salut"},
    ]
    avant = json.dumps(body, sort_keys=False)
    oc._free_wire_body(body, force_stream=True)
    assert json.dumps(body, sort_keys=False) == avant


def _long_history(n=120):
    """~480 Ko : au-delà du budget de test (400 Ko monkeypatché)."""
    messages = [{"role": "user", "content": "TASK START " + ("a" * 500)}]
    for i in range(n):
        messages.append({"role": "assistant", "content": f"assistant {i} " + ("b" * 2000)})
        messages.append({"role": "user", "content": f"user {i} " + ("c" * 2000)})
    return messages


def test_free_wire_trims_oversized_non_official_history(monkeypatch):
    """Hermes (299 messages) : au-delà du plafond, le tour courant reste."""
    monkeypatch.setattr(oc, "_free_max_wire_bytes", lambda: 400000)
    body = _body(max_tokens=128000)
    body["messages"] = _long_history()
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert oc._quick_body_size(wire) <= oc._free_max_wire_bytes()
    assert len(wire["messages"]) < len(body["messages"])
    assert wire["messages"][-1] == body["messages"][-1], "le tour courant doit rester"
    assert any("TASK START" in str(m.get("content") or "") for m in wire["messages"]), "la tache initiale doit rester"
    assert {"read", "bash"} <= {t["function"]["name"] for t in wire["tools"]}
    assert len(body["messages"]) == len(_long_history()), "le corps client ne doit pas etre tronque"
    assert body["max_tokens"] == 128000


def test_free_wire_trim_keeps_tool_pairs_contiguous():
    """On ne garde jamais un resultat d'outil sans son appel assistant."""
    body = _body(max_tokens=32000)
    messages = [{"role": "user", "content": "task"}]
    for i in range(80):
        call_id = f"call_{i}"
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{}"},
                    }
                ],
            }
        )
        messages.append({"role": "tool", "tool_call_id": call_id, "content": "x" * 3000})
        messages.append({"role": "user", "content": f"next {i}"})
    body["messages"] = messages
    wire, _ = oc._free_wire_body(body, force_stream=True)
    kept = wire["messages"]
    assert kept[0].get("role") == "system"
    non_system = [m for m in kept if m.get("role") != "system"]
    assert non_system and non_system[0].get("role") == "user", "pas de tool result orphelin en tete"
    # Chaque tool_call conserve doit avoir son resultat juste apres.
    for i, m in enumerate(non_system):
        if m.get("role") == "tool":
            assert i > 0 and non_system[i - 1].get("role") == "assistant"


def test_official_signature_is_never_trimmed():
    """Un client officiel (systeme opencode + read) traverse tel quel."""
    body = _body(max_tokens=32000)
    body["messages"] = [
        {
            "role": "system",
            "content": "You are opencode, an interactive CLI tool that helps users with software engineering tasks.",
        },
        *_long_history(20),
    ]
    wire, _ = oc._free_wire_body(body, force_stream=True)
    assert wire is body, "aucune transformation pour la signature officielle"
    assert len(wire["messages"]) == len(body["messages"])


def test_subagent_fork_is_filtered_from_free_tools():
    """[2026-10-29] subagent_fork fait refuser TOUTE la requete free.

    Mesure : safe8 + subagent_fork -> 429 FreeUsageLimitError ;
    safe8 + subagent -> 200 ; safe8 + skill -> 200. La porte free deguise
    le refus en quota. On retire l'outil de la copie d'envoi, jamais du corps
    client.
    """
    body = _body()
    body["tools"] = [
        _TOOL,
        {**_TOOL, "function": {**_TOOL["function"], "name": "read"}},
        {**_TOOL, "function": {**_TOOL["function"], "name": "subagent"}},
        {**_TOOL, "function": {**_TOOL["function"], "name": "subagent_fork"}},
    ]
    wire, _ = oc._free_wire_body(body, force_stream=True)
    names = [t["function"]["name"] for t in wire["tools"]]
    assert "subagent_fork" not in names
    assert "subagent" in names and "read" in names
    assert any(t["function"]["name"] == "subagent_fork" for t in body["tools"])


def test_subagent_fork_is_filtered_from_responses_tools():
    body = {
        "model": "m",
        "input": [{"role": "user", "content": "x"}],
        "stream": True,
        "tools": [
            {"type": "function", "name": "read", "parameters": {"type": "object", "properties": {}}},
            {"type": "function", "name": "subagent_fork", "parameters": {"type": "object", "properties": {}}},
        ],
    }
    wire, _ = oc._free_wire_body(body, force_stream=True)
    names = [t["name"] for t in wire["tools"]]
    assert names == ["read"]


def test_trim_uses_final_wire_size_not_client_body(monkeypatch):
    """Le plafond gate s'applique à la copie finale, pas au corps client.

    Régression : un corps client de 10 Ko (< plafond) + system/outils injectés
    produisait >30 Ko de wire sans trim. Le gate refusait alors que le proxy
    croyait le corps sous la borne.
    """
    monkeypatch.setattr(oc, "_free_max_wire_bytes", lambda: 30000)
    messages = [{"role": "user", "content": "TASK: garder le fil"}]
    for i in range(8):
        messages.append({"role": "assistant", "content": f"réponse {i} " + ("x" * 900)})
        messages.append({"role": "user", "content": f"question {i} " + ("y" * 900)})
    messages.append({"role": "user", "content": "question finale"})
    body = {"model": "m", "stream": True, "messages": messages}
    assert oc._quick_body_size(body) < 30000

    wire, _ = oc._free_wire_body(body, force_stream=True)

    assert oc._quick_body_size(wire) <= 30000
    non_system = [m for m in wire["messages"] if m.get("role") != "system"]
    assert len(non_system) < len(body["messages"]), "l'historique final doit être trimmé"


def test_summary_truncation_keeps_middle_fact():
    """Début, milieu et fin du transcript doivent survivre à la troncature."""
    text = (
        "START_FACT "
        + ("a" * 5000)
        + " MIDDLE_FACT "
        + ("b" * 5000)
        + " END_FACT"
    )
    out = oc._truncate_summary_middle(text, 2000)
    assert "START_FACT" in out
    assert "MIDDLE_FACT" in out
    assert "END_FACT" in out
    assert len(out) <= 2000
    assert "summary input truncated" in out


def test_official_placeholders_are_minimal_but_keep_names():
    """Les 10 placeholders du gate ne doivent pas manger le budget historique."""
    payload = oc._official_free_payload()
    official = [t for t in (payload.get("tools") or []) if isinstance(t, dict)]
    placeholders = oc._free_official_placeholder_defs(official)
    names = [t["function"]["name"] for t in placeholders]
    assert names == [t["function"]["name"] for t in official]
    assert len(json.dumps(placeholders, ensure_ascii=False).encode()) < 3000
    for tool in placeholders:
        assert tool["function"]["parameters"] == {"type": "object", "properties": {}}
        assert "DO NOT CALL" in tool["function"]["description"]
