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


def test_tool_choice_auto_added_for_shimmed_tools():
    """Meme quand les tools viennent de notre grille shim (corps nu)."""
    wire, _ = oc._free_wire_body(
        {"model": "m", "messages": [{"role": "user", "content": "x"}], "stream": True},
        force_stream=True,
    )
    assert wire["tool_choice"] == "auto"
    assert [t["function"]["name"] for t in wire["tools"]] == ["bash", "read"]
    assert list(wire) == [k for k in CLIENT_KEY_ORDER if k in wire]


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