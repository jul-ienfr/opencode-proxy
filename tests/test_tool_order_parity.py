"""Écart E3 — l'ordre des ``tools[]`` doit suivre le client officiel.

Le client officiel trie ses outils par ``function.name`` avant l'envoi
(``toSorted(localeCompare)``, ``request.ts:184``). Chez nous l'ordre du client
était préservé tel quel : deux clients envoyant le même jeu d'outils dans deux
ordres produisaient **deux préfixes de corps différents**, donc deux entrées de
cache — le ``cache_read_input_tokens`` s'en ressentait sans qu'aucun bug ne soit
visible.

Le tri vit à deux endroits, et les deux sont verrouillés ici :

1. ``_sorted_chat_tools`` (``app/protocol/mapping.py``) — le tri lui-même, avec
   son garde-fou : une liste malformée est renvoyée **inchangée**, on ne
   réordonne jamais ce qu'on ne comprend pas ;
2. ``_sanitize_chat_tools`` — qui trie **même sur le fast-path** (aucun nom à
   raccourcir) : l'ordre officiel est une propriété du fil, pas un effet de bord
   du raccourcissement ;
3. ``_free_wire_body`` (jambe free) — les shims ``bash``/``read`` doivent être
   triés **avec** le reste, pas ajoutés en fin de liste.

Purs unitaires, sans réseau.
"""

from __future__ import annotations

import opencode as oc
import protocol_mapping as pm
from app.protocol.mapping import _sanitize_chat_tools, _sorted_chat_tools, _tool_sort_key


def _tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


def _names(tools):
    return [t["function"]["name"] for t in tools]


def test_sorted_chat_tools_orders_by_function_name():
    out = _sorted_chat_tools([_tool("zeta"), _tool("alpha"), _tool("midi")])
    assert _names(out) == ["alpha", "midi", "zeta"]


def test_sorted_chat_tools_does_not_mutate_the_caller():
    src = [_tool("zeta"), _tool("alpha")]
    out = _sorted_chat_tools(src)
    assert _names(src) == ["zeta", "alpha"]
    assert _names(out) == ["alpha", "zeta"]
    assert out is not src


def test_sorted_chat_tools_leaves_a_malformed_list_untouched():
    """Le garde-fou : pas de réordonnancement partiel sur une liste incomprise."""
    for bad in (
        [_tool("ok"), {"type": "function"}],  # function absente
        [_tool("ok"), {"type": "function", "function": {}}],  # nom absent
        [_tool("ok"), {"type": "function", "function": {"name": ""}}],  # nom vide
        [_tool("ok"), "pas-un-dict"],  # entrée non-dict
    ):
        assert _sorted_chat_tools(bad) is bad


def test_sorted_chat_tools_ignores_short_and_non_list_inputs():
    one = [_tool("solo")]
    assert _sorted_chat_tools(one) is one  # déjà trié
    assert _sorted_chat_tools([]) == []
    assert _sorted_chat_tools("nope") == "nope"


# ── La clé doit égaler localeCompare, pas un tri de code points ──
#
# Le point dur d'E3 : `toSorted(localeCompare)` n'ordonne PAS comme `<` sur les
# code points. Les valeurs ci-dessous sont l'ordre REEL du collateur du client
# (`node -e "[...].toSorted((a,b)=>a.localeCompare(b))"`), pas un ordre inventé.
# Un tri de code points mettrait Zed avant alpha et 'A' après 'a_b' — ce qui
# reproduirait, à l'échelle d'un jeu d'outils à casse mixte, l'écart de préfixe
# que E3 est justement censé supprimer.

_LOCALE_COMPARE_ORDER = [
    "_",
    "_wPht",
    "_x",
    "_z1",
    "-",
    "-B",
    "-x",
    "a_b",
    "a-b",
    "aab",
    "alpha",
    "ask",
    "Ask",
    "ask2",
    "Bash",
    "bash_tool",
    "m1",
    "M1",
    "read",
    "Read",
    "x2",
    "X2",
    "Zed",
]


def test_tool_sort_key_matches_locale_compare():
    """Ordre de référence du collator officiel, casse mixte et ponctuation comprises."""
    assert sorted(_LOCALE_COMPARE_ORDER, key=_tool_sort_key) == _LOCALE_COMPARE_ORDER


def test_tool_sort_key_is_not_a_code_point_sort():
    """Contre-épreuve : sans la clé dédiée, au moins un couple est inversé."""
    shuffled = ["Zed", "alpha", "A", "a_b", "Bash", "bash_tool", "m1", "M1", "_x", "-x"]
    by_key = sorted(shuffled, key=_tool_sort_key)
    by_codepoint = sorted(shuffled)
    assert by_key != by_codepoint
    # les deux inversions qui font tout le sujet du test
    assert by_key.index("alpha") < by_key.index("Zed")  # casse mixte
    assert by_key.index("A") < by_key.index("a_b")  # préfixe avant casse
    assert by_key.index("_x") < by_key.index("Bash")  # ponctuation avant lettres


def test_tool_sort_key_is_total_and_never_raises():
    """Hors alphabet du contrat, la clé reste totale : ordre stable, pas d'exception."""
    weird = ["a b", "a\tb", "é", "zz", "a_b", "A", "0"]
    out = sorted(weird, key=_tool_sort_key)
    assert sorted(out, key=_tool_sort_key) == out  # idempotent
    assert set(out) == set(weird)


def test_sanitize_chat_tools_sorts_on_the_fast_path_too():
    """Aucun nom à raccourcir… mais la sortie est quand même triée (E3, mesuré 1.19.0)."""
    name_map: dict = {}
    out = _sanitize_chat_tools([_tool("zeta"), _tool("alpha")], name_map)
    assert _names(out) == ["alpha", "zeta"]
    assert name_map == {}  # rien à raccourcir, donc aucune entrée de map


def test_sanitize_chat_tools_sorts_the_renamed_tools_with_the_rest():
    """Le nom raccourci (digest) prend sa place dans le tri, il n'est pas
    simplement remplacé sur place — sinon l'ordre cesserait d'être trié."""
    long_name = "a" * 80
    name_map: dict = {}
    out = _sanitize_chat_tools([_tool("zeta"), _tool(long_name), _tool("alpha")], name_map)
    assert _names(out) == sorted(_names(out))
    assert len(name_map) == 1  # le seul nom raccourci est enregistré
    assert len(_names(out)[1]) <= 64  # « alpha » puis le nom raccourci puis « zeta »


def test_free_wire_shims_are_sorted_with_the_rest():
    """Jambe free : le `read` officiel ouvre la liste, puis les outils du
    client dans leur ordre d'origine (mesuré : read + outil client -> 200)."""
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [_tool("zzz"), _tool("aaa")],
    }
    wire, _forced = oc._free_wire_body(body)
    # [audit 2026-10-30] La copie officielle (10 outils, ordre officiel) est
    # envoy?e devant les outils client ; le client garde son ordre.
    assert _names(wire["tools"]) == [
        "bash", "edit", "glob", "grep", "read", "skill", "task", "todowrite", "webfetch", "write", "zzz", "aaa"
    ]
    # Le corps d'origine n'est jamais muté (idempotence des retries/hedges).
    assert _names(body["tools"]) == ["zzz", "aaa"]


def test_free_wire_is_idempotent_once_sorted():
    """Réappliquer le wire ne change plus rien : la signature est un point fixe."""
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [_tool("zzz"), _tool("aaa")],
    }
    first, _ = oc._free_wire_body(body)
    second, _ = oc._free_wire_body(first)
    assert _names(second["tools"]) == _names(first["tools"]) == [
        "bash", "edit", "glob", "grep", "read", "skill", "task", "todowrite", "webfetch", "write", "zzz", "aaa"
    ]


def test_free_wire_responses_leg_is_left_alone():
    """Forme Responses : ordre des outils NON mesuré chez le client officiel →
    on n'invente rien (même principe que l'ordre des clés, opencode.py)."""
    body = {
        "model": "m",
        "input": "hi",
        "tools": [{"type": "function", "name": "zzz"}, {"type": "function", "name": "aaa"}],
    }
    wire, _ = oc._free_wire_body(body)
    assert [t["name"] for t in wire["tools"]] == ["zzz", "aaa", "read"]


def test_end_to_end_chat_request_is_sorted():
    """Par le point d'entrée public : le corps Chat qui part est trié (mesuré 1.19.0)."""
    req = {
        "model": "muse-spark",
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {"name": "zzz", "description": "z", "input_schema": {"type": "object"}},
            {"name": "aaa", "description": "a", "input_schema": {"type": "object"}},
        ],
    }
    out = pm.anthropic_to_openai(req, req["model"])
    assert _names(out["tools"]) == ["aaa", "zzz"]
