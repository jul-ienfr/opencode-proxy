"""[plan v10 §11.5 Lot D] Génère les golden fixtures du contrat V1.

Usage : python scripts/gen_golden_fixtures.py

Chaque fixture = {fn, input, expected, note}. `expected` est la sortie RÉELLE
du code au moment de la génération ; le test paramétré
`tests/test_conversion_golden.py` la rejoue en comparaison exacte après
normalisation des champs non-déterministes (ids msg_/toolu_ générés).

RÈGLE §11.5 : modifier une conversion sans régénérer/réviser explicitement
les fixtures concernées = échec du gate. Les `_note` documentent les écarts
connus vs spec officielle (ex: 14.1.6 images perdues).
"""

from __future__ import annotations

import copy
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import protocol_mapping as pm  # noqa: E402

GOLDEN_DIR = ROOT / "docs" / "v1-response-golden"

# Même normalisation que tests/test_conversion_golden.py : les ids générés
# (uuid4) ne doivent pas produire de faux drift à chaque régénération.
_NONDETERMINISTIC_IDS = [
    (re.compile(r"^msg_[0-9a-f]{24}$"), "<msg_id>"),
    (re.compile(r"^toolu_[0-9a-f]{8}$"), "<toolu_id>"),
    (re.compile(r"^chatcmpl-[0-9a-f]{24}$"), "<chatcmpl_id>"),
    # [Lot L6] `resp_<hex24>` généré par uuid4 dans anthropic_to_openai_responses
    # et openai_chat_to_responses — non déterministe, doit être normalisé comme
    # les ids msg_/chatcmpl (miroir dans tests/test_conversion_golden.py).
    (re.compile(r"^resp_[0-9a-f]{24}$"), "<resp_id>"),
]


def _normalize(obj):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "created" and isinstance(v, int):
                out[k] = "<epoch>"  # timestamp généré à chaque réponse
            else:
                out[k] = _normalize(v)
        return out
    if isinstance(obj, list):
        return [_normalize(v) for v in obj]
    if isinstance(obj, str):
        for pat, repl in _NONDETERMINISTIC_IDS:
            if pat.match(obj):
                return repl
    return obj


def case_req_simple():
    """Texte simple + system string — chemin payé OpenAI-compatible."""
    anthro = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1024,
        "system": "Tu es un assistant concis.",
        "messages": [{"role": "user", "content": "Bonjour"}],
    }
    return {
        "fn": "anthropic_to_openai",
        "input": {"body": anthro, "model": "deepseek-v4-flash"},
        "note": "system string -> premier message system ; max_tokens mappe",
    }


def case_req_tools():
    """Tools + tool_choice + multi-turn tool_result (§11.3 spec officielle)."""
    anthro = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 2048,
        "system": [{"type": "text", "text": "Use tools wisely."}],
        "tools": [
            {
                "name": "get_weather",
                "description": "Get current weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ],
        "tool_choice": {"type": "auto"},
        "messages": [
            {"role": "user", "content": "Quel temps à Paris ?"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_01ABC",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_01ABC",
                        "content": "18C, nuageux",
                    }
                ],
            },
        ],
    }
    return {
        "fn": "anthropic_to_openai",
        "input": {"body": anthro, "model": "deepseek-v4-flash"},
        "note": "tool_result user -> message role=tool ; tool_choice auto -> 'auto'",
    }


def case_resp_text():
    """Réponse texte simple OpenAI -> contrat V1 Anthropic."""
    oai = {
        "choices": [
            {
                "message": {"role": "assistant", "content": "Bonjour ! Comment puis-je aider ?"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 9,
            "prompt_tokens_details": {"cached_tokens": 4},
        },
    }
    return {
        "fn": "openai_to_anthropic",
        "input": {"resp": oai, "model": "deepseek-v4-flash-free"},
        "note": "usage prompt/completion -> input/output tokens ; cached_tokens -> cache_read",
    }


def case_resp_tool_calls():
    """tool_calls + finish_reason=length -> stop_reason mapping."""
    oai = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc123",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city": "Paris"}',
                            },
                        }
                    ],
                },
                "finish_reason": "length",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 15},
    }
    return {
        "fn": "openai_to_anthropic",
        "input": {"resp": oai, "model": "deepseek-v4-flash-free"},
        "note": "finish_reason length PRIORISE sur tool_calls -> max_tokens (contrat §11.5)",
    }


def case_p1_free_leg_tool_name_restored():
    """[A26] Jambe free P1 — le nom d'outil raccourci vers un amont Chat est RESTAURE.

    Le nom porté par la réponse Chat est la forme RACCOURCIE produite à l'aller par
    ``sanitize_tool_names`` ; ``name_map`` étant fourni au retour, le bloc ``tool_use``
    doit porter le nom LONG d'origine — celui que le client avait défini. Sans cette
    restauration, un client Anthropic reçoit un nom d'outil qu'il n'a jamais déclaré.
    """
    long_name = "mcp__" + "tres_long_segment_" * 6 + "outil_final"
    short_tools, name_map = pm.sanitize_tool_names(
        [{"name": long_name, "description": "d", "input_schema": {"type": "object", "properties": {}}}]
    )
    short_name = short_tools[0]["name"]
    oai = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_free_1",
                            "type": "function",
                            "function": {"name": short_name, "arguments": '{"city": "Paris"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 15},
    }
    return {
        "fn": "openai_to_anthropic",
        "input": {"resp": oai, "model": "mimo-v2.5-free", "name_map": name_map},
        "note": (
            "[A26] jambe free P1 : le retour Chat -> Anthropic restaure le nom d'outil "
            f"raccourci ({len(short_name)} car.) vers sa forme longue ({len(long_name)} car.)"
        ),
    }


def case_resp_reasoning():
    """reasoning_content (modèles thinking free/payés) -> bloc thinking."""
    oai = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "La réponse est 42.",
                    "reasoning_content": "Je réfléchis... 6*7=42.",
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 30, "completion_tokens": 25},
    }
    return {
        "fn": "openai_to_anthropic",
        "input": {"resp": oai, "model": "glm-5.1-free"},
        "note": "reasoning_content -> block type=thinking AVANT le text (ordre §11.5)",
    }


def case_oai_request_to_anthro():
    """Client OpenAI (/v1/chat/completions) -> upstream Anthropic."""
    oai = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 512,
        "messages": [
            {"role": "system", "content": "Be brief."},
            {"role": "user", "content": "Hi"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_x1",
                        "function": {"name": "ping", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_x1", "content": "pong"},
            {"role": "user", "content": "Continue"},
        ],
    }
    return {
        "fn": "openai_to_anthropic_request",
        "input": {"oai_body": oai},
        "note": "role=tool bufferisé puis injecté dans le user suivant (contrat)",
    }


def case_anthro_response_to_openai():
    """Upstream Anthropic -> client OpenAI (chemin /v1/chat/completions payé A)."""
    anthro = {
        "id": "msg_01XYZ",
        "type": "message",
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "hmm", "signature": "sig1"},
            {"type": "text", "text": "Answer."},
        ],
        "model": "claude-sonnet-4-5",
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 11,
            "output_tokens": 7,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 3,
        },
    }
    return {
        "fn": "anthropic_to_openai_response",
        "input": {"anthro": anthro, "model": "claude-sonnet-4-5"},
        "note": "thinking -> reasoning_content ; usage inverse cache_* conservé si mappé",
    }


def case_responses_api_entry():
    """Client /v1/responses (OpenAI Responses) -> requête Anthropic."""
    body = {
        "model": "claude-sonnet-4-5",
        "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "Hello"}]}
        ],
        "max_output_tokens": 256,
    }
    return {
        "fn": "openai_responses_to_anthropic",
        "input": {"body": body},
        "note": "/v1/responses input_text -> content text (contrat §11.5 clients-compat)",
    }


def case_sse_deltas():
    """Lignes SSE Responses (préfixe 'data:' strippé par l'appelant) -> deltas."""
    lines = [
        '{"type": "response.output_text.delta", "delta": "Hel"}',
        '{"type": "response.output_text.delta", "delta": "lo"}',
        '{"type": "response.content_part.delta", "delta": {"type": "reasoning_summary_text", "text": "hm"}}',
        "[DONE]",
    ]
    return {
        "fn": "_responses_sse_to_chat_deltas_lines",
        "input": {"lines": lines},
        "note": "contrat streaming /v1/responses — [DONE]/non-parseable -> None",
    }


def case_multiturn_thinking_strip():
    """[Correctif parité multi-tours — remplace Phase D.2] Historique multi-tours
    vers upstream openai-compatible.

    Bloc SYNTHÉTIQUE (signature locale du proxy) et bloc ORIGINAL (signature
    authentique) -> tous deux préservés en reasoning_content (parité avec
    l'usage direct : les signatures ne transitent jamais, seul le texte) ;
    redacted_thinking -> strippé (donnée chiffrée non interprétable hors
    upstreams Anthropic)."""
    anthro = {
        "model": "claude-sonnet-4-5",
        "max_tokens": 512,
        "thinking": {"type": "enabled"},
        "messages": [
            {"role": "user", "content": "Question"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "raisonnement converti par le proxy",
                        "signature": pm._local_signature("raisonnement converti par le proxy"),
                    },
                    {"type": "text", "text": "Réponse A."},
                ],
            },
            {"role": "user", "content": "Suite"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "thinking",
                        "thinking": "vraie réflexion du modèle source",
                        "signature": "SIGNATURE-AUTHENTIQUE-ANTHROPIC==",
                    },
                    {"type": "redacted_thinking", "data": "BLOBCHIFFREAUTHENTIQUE"},
                    {"type": "text", "text": "Réponse B."},
                ],
            },
            {"role": "user", "content": "Encore"},
        ],
    }
    return {
        "fn": "anthropic_to_openai",
        "input": {"body": anthro, "model": "deepseek-v4-flash"},
        "note": (
            "multi-tours : thinking synthétique ET original préservés en "
            "reasoning_content (parité multi-tours), redacted_thinking strippé "
            "(correctif post-livraison PLAN-raisonnement)"
        ),
    }


def case_req_tools_complex():
    """Schema complexe — exerce tous les normalizeurs (proud-beaver V3)."""
    anthro = {
        "model": "muse-spark-1.2-contributor",
        "max_tokens": 1024,
        "tools": [
            {
                "name": "agent_manager",
                "description": "x" * 1100,
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"anyOf": [{"type": "string"}, {"type": "null"}], "description": "q"},
                        "alt": {"type": ["string", "null"]},
                        "config": {
                            "type": "object",
                            "properties": {"strict": {"type": "boolean"}},
                            "additionalProperties": True,
                            "format": "uri",
                        },
                        "deep": {
                            "type": "object",
                            "properties": {
                                "l1": {
                                    "type": "object",
                                    "properties": {
                                        "l2": {
                                            "type": "object",
                                            "properties": {
                                                "l3": {
                                                    "type": "object",
                                                    "properties": {
                                                        "l4": {
                                                            "type": "object",
                                                            "properties": {
                                                                "l5": {
                                                                    "type": "object",
                                                                    "properties": {
                                                                        "l6": {
                                                                            "type": "object",
                                                                            "properties": {
                                                                                "l7": {
                                                                                    "type": "object",
                                                                                    "properties": {
                                                                                        "l8": {
                                                                                            "type": "object",
                                                                                            "properties": {"l9": {"type": "string"}},
                                                                                        }
                                                                                    },
                                                                                }
                                                                            },
                                                                        }
                                                                    },
                                                                }
                                                            },
                                                        }
                                                    },
                                                }
                                            },
                                        }
                                    },
                                }
                            },
                        },
                        "opt": {"type": "string", "enum": []},
                        "refd": {"$ref": "#/$defs/Foo"},
                    },
                    "required": ["query"],
                    "$defs": {"Foo": {"type": "string", "description": "foo"}},
                    "definitions": {"Bar": {"type": "number"}},
                },
            }
        ],
        "messages": [{"role": "user", "content": "hi"}],
    }
    return {
        "fn": "anthropic_to_openai",
        "input": {"body": anthro, "model": "muse-spark-1.2-contributor"},
        "note": "complex schema — anyOf null + type array null + additionalProperties + format + description truncate + nesting flatten + enum vide + $ref",
    }


# ── [Lot L6 — goldens étendus] 15 axes × 6 chemins ──
#
# Les 11 fixtures ci-dessus sont GELÉES (§7.2 : jamais régénérer « pour faire
# passer »). Les cas suivants sont des AJOUTS couvrant la matrice de couverture
# cible (PLAN §6) : effort/thinking, cache_control (messages/tools/top-level),
# max_tokens vs max_completion_tokens, schéma d'outils/strict/noms longs/schéma
# invalide, documents (PDF base64/URL/file_id/texte), images, strip thinking
# multi-tours, usage cache read/creation.


def case_p2_effort_thinking_budget():
    """P2 effort/thinking : thinking enabled + budget_tokens → reasoning_effort."""
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 4096,
                "thinking": {"type": "enabled", "budget_tokens": 10000},
                "system": "S",
                "messages": [{"role": "user", "content": "Q"}],
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe effort : thinking budget_tokens → reasoning_effort=high (plafond modèle)",
    }


def case_p2_effort_output_config():
    """P2 effort : forme Anthropic native output_config.effort (A13)."""
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "output_config": {"effort": "xhigh"},
                "messages": [{"role": "user", "content": "Q"}],
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe effort : output_config.effort (forme Anthropic native) → reasoning_effort",
    }


def case_p2_effort_thinking_disabled():
    """P2 effort : thinking explicitement désactivé → aucun reasoning_effort."""
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "thinking": {"type": "disabled"},
                "messages": [{"role": "user", "content": "Q"}],
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe effort : thinking disabled explicite → pas de reasoning_effort émis",
    }


def case_p2_cache_control_messages():
    """P2 cache_control : breakpoint sur un content block de message."""
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "system": [{"type": "text", "text": "sys"}],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "hi",
                                "cache_control": {"type": "ephemeral", "ttl": "1h"},
                            }
                        ],
                    }
                ],
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe cache_control : ttl 1h propagé du content block au message Chat",
    }


def case_p2_cache_control_tools():
    """P2 cache_control : breakpoint porté par un outil (A5)."""
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "tools": [
                    {
                        "name": "t1",
                        "input_schema": {"type": "object"},
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": "hi"}],
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe cache_control tools (A5) : le breakpoint outil est bien reporté sur l'entrée Chat",
    }


def case_p2_cache_control_top_level():
    """P2 cache_control : breakpoint top-level de la requête Anthropic."""
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
                "system": "s",
                "messages": [{"role": "user", "content": "hi"}],
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe cache_control top-level : transporté tel quel sur le corps Chat",
    }


def case_p2_cache_control_breakpoint_limit():
    """P2 cache_control : plafond 4 breakpoints Anthropic (A20) — élagage."""
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 10,
                "cache_control": {"type": "ephemeral", "ttl": "1h"},
                "tools": [
                    {"name": f"t{i}", "input_schema": {"type": "object"}, "cache_control": {"type": "ephemeral"}}
                    for i in range(3)
                ],
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}}],
                    },
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "b", "cache_control": {"type": "ephemeral"}}],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "c", "cache_control": {"type": "ephemeral"}}],
                    },
                ],
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe cache_control (A20) : 7 breakpoints → élagage des plus anciens, 4 conservés",
    }


def case_p2_max_completion_tokens_o3():
    """P2 max_tokens : modèle o-series → max_completion_tokens (A17/B2)."""
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 77,
                "messages": [{"role": "user", "content": "hi"}],
            },
            "model": "o3-mini",
        },
        "note": "axe max_tokens : modèle o-series → forme max_completion_tokens (max_tokens rejeté en 400)",
    }


def case_p4_max_completion_tokens_input():
    """P4 max_tokens : forme moderne max_completion_tokens lue, jamais remplacée par le défaut."""
    return {
        "fn": "openai_to_anthropic_request",
        "input": {
            "oai_body": {
                "model": "claude-sonnet-4-5",
                "max_completion_tokens": 333,
                "messages": [{"role": "user", "content": "hi"}],
            }
        },
        "note": "axe max_tokens (A17) : max_completion_tokens client → max_tokens Anthropic 333",
    }


def case_p2_tools_long_name_strict_schema():
    """P2 tools : nom > 64 caractères + schéma strict (additionalProperties=false).

    [Lot L4 — A8] Le nom long est désormais SANITIZÉ vers la limite Chat (64) et
    une map de restauration `_tool_name_map` accompagne le corps. Avant le
    correctif, ce cas figeait le défaut (« nom long conservé tel quel »), qui
    faisait répondre 400 à une cible Chat stricte.
    """
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "tools": [
                    {
                        "name": "mcp__plugin_very_long_tool_name_exceeding_sixty_four_chars_limit_aaaa",
                        "description": "d",
                        "input_schema": {
                            "type": "object",
                            "properties": {"a": {"type": "string"}},
                            "required": ["a"],
                            "additionalProperties": False,
                        },
                    }
                ],
                "messages": [{"role": "user", "content": "hi"}],
            },
            "model": "deepseek-v4-flash",
        },
        "note": (
            "axe tools : nom long SANITIZÉ à 64 (lot L4/A8) + map _tool_name_map "
            "de restauration ; le nom d'origine reste retrouvable côté client"
        ),
    }


def case_p2_tools_invalid_schema():
    """P2 tools : input_schema non-dict → schéma vide au lieu d'une exception."""
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "tools": [{"name": "bad", "description": "d", "input_schema": "not-a-dict"}],
                "messages": [{"role": "user", "content": "hi"}],
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe tools : schéma invalide (str) → parameters {} sans lever (jamais de 500)",
    }


def case_p4_tools_strict_schema():
    """P4 tools : profil strict + additionalProperties=false conservés."""
    return {
        "fn": "openai_to_anthropic_request",
        "input": {
            "oai_body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_x",
                            "description": "d",
                            "strict": True,
                            "parameters": {
                                "type": "object",
                                "properties": {"a": {"type": "string"}},
                                "required": ["a"],
                                "additionalProperties": False,
                            },
                        },
                    }
                ],
            }
        },
        "note": "axe tools schéma/strict : profil OpenAI strict → input_schema Anthropic normalisé",
    }


def case_p6_tools_long_name_strict():
    """P6 tools : nom long + strict → input_schema Anthropic, nom inchangé."""
    return {
        "fn": "openai_responses_to_anthropic",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                "tools": [
                    {
                        "type": "function",
                        "name": "mcp__plugin_very_long_tool_name_exceeding_sixty_four_chars_limit_aaaa",
                        "description": "d",
                        "strict": True,
                        "input_schema": {"type": "object", "properties": {"a": {"type": "string"}}},
                    },
                    {"type": "function", "name": "short", "input_schema": {"type": "object"}},
                ],
            }
        },
        "note": "axe tools noms longs P6 : schéma normalisé, nom long transmis tel quel à l'amont",
    }


def case_sanitize_tool_names_long_and_server():
    """sanitize_tool_names : raccourci déterministe + serveur tools intouchés."""
    return {
        "fn": "sanitize_tool_names",
        "input": {
            "tools": [
                {"type": "function", "name": "mcp__plugin_very_long_tool_name_exceeding_sixty_four_chars_limit_aaaa"},
                {"type": "function", "name": "web_search"},
                {"type": "function", "name": "ok-name"},
            ]
        },
        "note": "axe tools noms longs : >64 → name[:57]+sha1[:6] ; web_* jamais renommés ; map retour",
    }


def case_p2_documents_pdf_url_file_text():
    """P2 documents : PDF base64, URL, file_id, texte brut (4 formes)."""
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "document",
                                "name": "a.pdf",
                                "source": {
                                    "type": "base64",
                                    "media_type": "application/pdf",
                                    "data": "JVBERi0xLjQK",
                                },
                            },
                            {
                                "type": "document",
                                "name": "b.pdf",
                                "source": {"type": "url", "url": "https://ex.com/b.pdf"},
                            },
                            {
                                "type": "document",
                                "name": "c.pdf",
                                "source": {"type": "file", "file_id": "file-abc"},
                            },
                            {"type": "document", "source": {"type": "text", "text": "contenu brut"}},
                        ],
                    }
                ],
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe documents : PDF base64 → file_data ; URL sans repli fichier → placeholder texte ; file_id → file_data ; texte",
    }


def case_p2_documents_tool_result_media():
    """P2 documents+images : media dans un tool_result (texte/image/document/audio)."""
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "toolu_01ABC", "name": "read", "input": {"p": "a.pdf"}}
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01ABC",
                                "content": [
                                    {"type": "text", "text": "voici"},
                                    {
                                        "type": "image",
                                        "source": {
                                            "type": "base64",
                                            "media_type": "image/png",
                                            "data": "iVBORw0KGgo=",
                                        },
                                    },
                                    {
                                        "type": "document",
                                        "name": "r.pdf",
                                        "source": {
                                            "type": "base64",
                                            "media_type": "application/pdf",
                                            "data": "JVBERi0=",
                                        },
                                    },
                                    {
                                        "type": "document",
                                        "name": "u.pdf",
                                        "source": {"type": "url", "url": "https://ex.com/u.pdf"},
                                    },
                                    {"type": "audio", "source": {"type": "base64", "data": "AAA"}},
                                ],
                            }
                        ],
                    },
                ],
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe documents+images : tool_result multimodal → parts image_url/file + placeholders [document:url]/[audio]",
    }


def case_p2_image_base64_and_url():
    """P2 images : base64 → data-URI, URL https → image_url natif."""
    return {
        "fn": "anthropic_to_openai",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "iVBORw0KGgo=",
                                },
                            },
                            {"type": "image", "source": {"type": "url", "url": "https://ex.com/i.png"}},
                        ],
                    }
                ],
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe images : base64 → data URI, URL passée telle quelle (file_id image non couvert ici)",
    }


def case_p4_tool_result_media():
    """P4 documents+images : role tool multimodal → tool_result Anthropic."""
    return {
        "fn": "openai_to_anthropic_request",
        "input": {
            "oai_body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "messages": [
                    {"role": "user", "content": "q"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {"id": "call_1", "type": "function", "function": {"name": "t", "arguments": "{}"}}
                        ],
                    },
                    {
                        "role": "tool",
                        "tool_call_id": "call_1",
                        "content": [
                            {"type": "text", "text": "res"},
                            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
                            {"type": "file", "file": {"file_id": "file-1"}},
                            {
                                "type": "file",
                                "file": {
                                    "file_data": "data:application/pdf;base64,JVBERi0=",
                                    "filename": "a.pdf",
                                },
                            },
                        ],
                    },
                ],
            }
        },
        "note": "axe documents+images P4 : tool rôle multimodal → tool_result [text,image,document file_id,document base64]",
    }


def case_p6_documents_file_forms():
    """P6 documents : input_file file_data / file_id / file_url → document Anthropic."""
    return {
        "fn": "openai_responses_to_anthropic",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_file",
                                "filename": "r.pdf",
                                "file_data": "data:application/pdf;base64,JVBERi0=",
                            },
                            {"type": "input_file", "file_id": "file-xyz"},
                            {
                                "type": "input_file",
                                "file_url": "https://ex.com/a.pdf",
                                "filename": "u.pdf",
                            },
                        ],
                    }
                ],
            }
        },
        "note": "axe documents P6 : file_data → base64+name, file_id → source file, file_url → source url",
    }


def case_p6_images_forms():
    """P6 images : input_image data-URI et URL https → blocs image Anthropic."""
    return {
        "fn": "openai_responses_to_anthropic",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_image", "image_url": "data:image/png;base64,iVBORw0KGgo="},
                            {"type": "input_image", "image_url": "https://ex.com/i.png"},
                        ],
                    }
                ],
            }
        },
        "note": "axe documents+images P6 : data-URI décodé en source base64, URL → source url",
    }


def case_p6_reasoning_history_dropped():
    """P6 multi-tours : item reasoning de l'historique droppé (pas de signature forgée)."""
    return {
        "fn": "openai_responses_to_anthropic",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "input": [
                    {"role": "user", "content": [{"type": "input_text", "text": "q"}]},
                    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "résumé précédent"}]},
                    {"role": "assistant", "content": [{"type": "output_text", "text": "a"}]},
                ],
            }
        },
        "note": "axe multi-tours reasoning (assumé) : le summary est droppé, l'assistant devient un bloc texte vide",
    }


def case_p4_thinking_strip_local_signature():
    """P4 multi-tours : reasoning_content → thinking à signature LOCALE, puis strippé."""
    return {
        "fn": "openai_to_anthropic_request",
        "input": {
            "oai_body": {
                "model": "deepseek-v4-flash",
                "max_tokens": 512,
                "messages": [
                    {"role": "user", "content": "q"},
                    {
                        "role": "assistant",
                        "content": "a",
                        "reasoning_content": "raisonnement converti par le proxy",
                    },
                    {"role": "user", "content": "suite"},
                ],
            }
        },
        "note": "axe thinking multi-tours P4 : bloc thinking synthétique retiré de l'historique multi-tours (Phase D)",
    }


def case_strip_synthetic_thinking():
    """P1 helper multi-tours : strip des thinking locaux, originaux/redacted préservés."""
    return {
        "fn": "strip_synthetic_thinking",
        "input": {
            "body": {
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "thinking",
                                "thinking": "synthétique",
                                "signature": pm._local_signature("synthétique"),
                            },
                            {"type": "thinking", "thinking": "vrai", "signature": "authentique-signature"},
                            {"type": "redacted_thinking", "data": "blob"},
                            {"type": "text", "text": "ok"},
                        ],
                    },
                    {"role": "user", "content": "suite"},
                ]
            }
        },
        "note": "axe thinking multi-tours P1 : signature locale strippée, signature authentique et redacted conservés",
    }


def case_anthro_to_responses_tool_use():
    """P6 réponse : thinking + texte + tool_use → items Responses, usage cache read."""
    return {
        "fn": "anthropic_to_openai_responses",
        "input": {
            "anthro": {
                "content": [
                    {"type": "thinking", "thinking": "tk", "signature": "s"},
                    {"type": "text", "text": "ans"},
                    {"type": "tool_use", "id": "toolu_01ABC", "name": "t", "input": {"a": 1}},
                ],
                "stop_reason": "tool_use",
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "cache_read_input_tokens": 3,
                    "output_tokens_details": {"thinking_tokens": 4},
                },
            },
            "model": "claude-sonnet-4-5",
        },
        "note": "axe usage cache read P6 : cache_read_input_tokens → input_tokens_details.cached_tokens",
    }


def case_anthro_to_responses_thinking_omitted():
    """P6 réponse : thinking `display: omitted` (texte vide) → aucun item reasoning vide."""
    return {
        "fn": "anthropic_to_openai_responses",
        "input": {
            "anthro": {
                "content": [
                    {"type": "thinking", "thinking": "   ", "signature": "s"},
                    {"type": "text", "text": "ans"},
                ],
                "stop_reason": "max_tokens",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
            "model": "claude-sonnet-4-5",
        },
        "note": "axe effort/thinking P6 (A19) : thinking vide → pas d'item reasoning fantôme",
    }


def case_chat_to_responses_reasoning_and_usage():
    """P5 réponse : reasoning_content + tool_calls → items Responses + usage détaillé."""
    return {
        "fn": "openai_chat_to_responses",
        "input": {
            "chat_resp": {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Hi",
                            "reasoning_content": "r",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {"name": "t", "arguments": '{"a":1}'},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"cached_tokens": 3},
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe usage cache read P5 : prompt_tokens_details.cached_tokens + reasoning_tokens extraits",
    }


def case_responses_to_chat_response_cache_usage():
    """Responses → cible Chat : usage cache read/non-caché + reasoning_content."""
    return {
        "fn": "_responses_to_chat_response",
        "input": {
            "resp": {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "rs"}]},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_9",
                        "name": "tool_short",
                        "arguments": '{"a":1}',
                    },
                ],
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 9,
                    "total_tokens": 29,
                    "input_tokens_details": {"cached_tokens": 5},
                },
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe usage cache read : cached_tokens ← input_tokens_details (jamais output_tokens_details)",
    }


def case_responses_to_chat_restore_tool_name():
    """Responses → cible Chat : name_map restaure le nom d'outil original."""
    return {
        "fn": "_responses_to_chat_response",
        "input": {
            "resp": {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_9",
                        "name": "tool_short",
                        "arguments": '{"a":1}',
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
            "model": "deepseek-v4-flash",
            "name_map": {"tool_short": "VeryLongOriginalName"},
        },
        "note": "axe tools noms longs (retour) : restore_tool_name via name_map court→original",
    }


def case_responses_to_anthropic_response_tool_use():
    """Responses → cible Anthropic : thinking + texte + tool_use, cache read."""
    return {
        "fn": "_responses_to_anthropic_response",
        "input": {
            "resp": {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {"type": "reasoning", "summary": [{"type": "summary_text", "text": "rs"}]},
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello"}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_9",
                        "name": "tool_short",
                        "arguments": '{"a":1}',
                    },
                ],
                "usage": {
                    "input_tokens": 20,
                    "output_tokens": 9,
                    "total_tokens": 29,
                    "input_tokens_details": {"cached_tokens": 5},
                },
            },
            "model": "claude-sonnet-4-5",
            "name_map": {"tool_short": "VeryLongOriginalName"},
        },
        "note": "axe usage cache read + noms d'outils : cached_tokens → cache_read_input_tokens, nom restauré",
    }


def case_p5_request_chat_to_responses_media():
    """P5 requête : Chat → Responses, media (image/file/vidéo/audio) + orphelin droppé."""
    return {
        "fn": "_chat_to_responses_request",
        "input": {
            "chat": {
                "model": "deepseek-v4-flash",
                "max_tokens": 128,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                            },
                            {"type": "file", "file": {"file_id": "file-1"}},
                            {
                                "type": "file",
                                "file": {
                                    "file_data": "data:application/pdf;base64,JVBERi0=",
                                    "filename": "a.pdf",
                                },
                            },
                            {"type": "video", "video": {"url": "https://ex.com/v.mp4"}},
                            {"type": "input_audio", "input_audio": {"data": "AAA", "format": "ogg"}},
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_missing", "content": "orphan"},
                ],
            }
        },
        "note": "axe documents+images P5 : image/file → input_* ; vidéo et audio → placeholders ; tool orphelin droppé",
    }


def case_p5_request_native_responses_passthrough():
    """P5 requête native : body déjà Responses → sanitize (effort clampé, store/truncation)."""
    return {
        "fn": "_chat_to_responses_request",
        "input": {
            "chat": {
                "model": "deepseek-v4-flash-free",
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
                "reasoning": {"effort": "max"},
                "store": True,
                "truncation": "auto",
            }
        },
        "note": "axe effort P5 natif : reasoning.effort=max clampé au plafond du modèle (high)",
    }


def case_p6_request_tool_choice_required():
    """P6 requête : tool_choice dict → tool Anthropic + orphelin function_call_output."""
    return {
        "fn": "openai_responses_to_anthropic",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 100,
                "tools": [{"type": "function", "name": "t", "parameters": {"type": "object"}}],
                "tool_choice": {"type": "function", "name": "t"},
                "input": [{"type": "function_call_output", "call_id": "call_x", "output": "orphan"}],
            }
        },
        "note": "axe tools tool_choice P6 : dict → {type:tool,name} ; function_call_output sans call précédent conservé",
    }


def case_p6_request_effort_relay():
    """P6 requête : reasoning.effort → output_config.effort Anthropic (A3 corrigée)."""
    return {
        "fn": "openai_responses_to_anthropic",
        "input": {
            "body": {
                "model": "claude-sonnet-4-5",
                "reasoning": {"effort": "high"},
                "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            }
        },
        "note": "axe effort P6 (A3) : reasoning.effort relais → output_config.effort + thinking adaptive",
    }


def case_p4_request_effort_relay():
    """P4 requête : reasoning_effort / output_config.effort → output_config Anthropic."""
    return {
        "fn": "openai_to_anthropic_request",
        "input": {
            "oai_body": {
                "model": "claude-sonnet-4-5",
                "max_tokens": 2048,
                "reasoning_effort": "medium",
                "messages": [{"role": "user", "content": "hi"}],
            }
        },
        "note": "axe effort P4 : reasoning_effort → output_config.effort=medium + thinking adaptive",
    }


def case_openai_to_anthropic_usage_cache_creation():
    """V1 usage : cache_creation_input_tokens + cache_read_input_tokens extraits."""
    return {
        "fn": "openai_to_anthropic",
        "input": {
            "resp": {
                "choices": [{"message": {"role": "assistant", "content": "x"}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 40, "cache_creation_tokens": 60},
                    "completion_tokens_details": {"reasoning_tokens": 7},
                },
            },
            "model": "deepseek-v4-flash",
        },
        "note": "axe usage cache creation (A6) : prompt_tokens_details → cache_read 40 / cache_creation 60",
    }


CASES = [
    case_req_simple,
    case_req_tools,
    case_resp_text,
    case_resp_tool_calls,
    case_resp_reasoning,
    case_oai_request_to_anthro,
    case_anthro_response_to_openai,
    case_responses_api_entry,
    case_sse_deltas,
    case_multiturn_thinking_strip,
    case_req_tools_complex,
    # ── Lot L6 : ajouts (jamais de modification des 11 ci-dessus) ──
    case_p2_effort_thinking_budget,
    case_p2_effort_output_config,
    case_p2_effort_thinking_disabled,
    case_p2_cache_control_messages,
    case_p2_cache_control_tools,
    case_p2_cache_control_top_level,
    case_p2_cache_control_breakpoint_limit,
    case_p2_max_completion_tokens_o3,
    case_p4_max_completion_tokens_input,
    case_p2_tools_long_name_strict_schema,
    case_p2_tools_invalid_schema,
    case_p4_tools_strict_schema,
    case_p6_tools_long_name_strict,
    case_sanitize_tool_names_long_and_server,
    case_p2_documents_pdf_url_file_text,
    case_p2_documents_tool_result_media,
    case_p2_image_base64_and_url,
    case_p4_tool_result_media,
    case_p6_documents_file_forms,
    case_p6_images_forms,
    case_p6_reasoning_history_dropped,
    case_p4_thinking_strip_local_signature,
    case_strip_synthetic_thinking,
    case_anthro_to_responses_tool_use,
    case_anthro_to_responses_thinking_omitted,
    case_chat_to_responses_reasoning_and_usage,
    case_responses_to_chat_response_cache_usage,
    case_responses_to_chat_restore_tool_name,
    case_responses_to_anthropic_response_tool_use,
    case_p5_request_chat_to_responses_media,
    case_p5_request_native_responses_passthrough,
    case_p6_request_tool_choice_required,
    case_p6_request_effort_relay,
    case_p4_request_effort_relay,
    case_openai_to_anthropic_usage_cache_creation,
    case_p1_free_leg_tool_name_restored,
]


def call_fn(c: dict):
    name, args = c["fn"], c["input"]
    if name == "anthropic_to_openai":
        return pm.anthropic_to_openai(args["body"], args["model"])
    if name == "openai_to_anthropic":
        return pm.openai_to_anthropic(args["resp"], args["model"], args.get("name_map"))
    if name == "openai_to_anthropic_request":
        return pm.openai_to_anthropic_request(args["oai_body"])
    if name == "anthropic_to_openai_response":
        return pm.anthropic_to_openai_response(args["anthro"], args["model"])
    if name == "openai_responses_to_anthropic":
        return pm.openai_responses_to_anthropic(args["body"])
    if name == "_responses_sse_to_chat_deltas_lines":
        return [pm._responses_sse_to_chat_deltas(line) for line in args["lines"]]
    # ── [Lot L6] fonctions supplémentaires (dispatcher miroir dans
    # tests/test_conversion_golden.py::_call — les deux DOIVENT rester identiques) ──
    if name == "anthropic_to_openai_responses":
        return pm.anthropic_to_openai_responses(args["anthro"], args["model"])
    if name == "openai_chat_to_responses":
        return pm.openai_chat_to_responses(args["chat_resp"], args["model"])
    if name == "sanitize_tool_names":
        # tuple (tools, name_map) → JSON-sérialisable, forme alignée sur le
        # dispatcher miroir de tests/test_conversion_golden.py
        tools, name_map = pm.sanitize_tool_names(args["tools"])
        return [tools, name_map]
    if name == "_responses_to_chat_response":
        return pm._responses_to_chat_response(args["resp"], args["model"], args.get("name_map"))
    if name == "_responses_to_anthropic_response":
        return pm._responses_to_anthropic_response(args["resp"], args["model"], args.get("name_map"))
    if name == "_chat_to_responses_request":
        return pm._chat_to_responses_request(args["chat"])
    if name == "strip_synthetic_thinking":
        body = copy.deepcopy(args["body"])
        return {"stripped": pm.strip_synthetic_thinking(body), "body": body}
    raise KeyError(name)


def main() -> int:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for make in CASES:
        c = make()
        out = call_fn(c)
        payload = {
            "fn": c["fn"],
            "input": c["input"],
            "expected": _normalize(out),
            "_note": c["note"],
            "_generated_by": "scripts/gen_golden_fixtures.py (plan v10 §11.5)",
        }
        path = GOLDEN_DIR / f"{make.__name__.removeprefix('case_')}.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append(path.name)
    print(f"{len(written)} fixtures écrites dans {GOLDEN_DIR}:")
    for w in written:
        print(f"  - {w}")
    print("\nREVIEW MANUELLE REQUISE avant commit (§11.5 : le golden fait foi).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
