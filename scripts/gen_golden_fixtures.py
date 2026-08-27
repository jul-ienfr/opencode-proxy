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
]


def call_fn(c: dict):
    name, args = c["fn"], c["input"]
    if name == "anthropic_to_openai":
        return pm.anthropic_to_openai(args["body"], args["model"])
    if name == "openai_to_anthropic":
        return pm.openai_to_anthropic(args["resp"], args["model"])
    if name == "openai_to_anthropic_request":
        return pm.openai_to_anthropic_request(args["oai_body"])
    if name == "anthropic_to_openai_response":
        return pm.anthropic_to_openai_response(args["anthro"], args["model"])
    if name == "openai_responses_to_anthropic":
        return pm.openai_responses_to_anthropic(args["body"])
    if name == "_responses_sse_to_chat_deltas_lines":
        return [pm._responses_sse_to_chat_deltas(line) for line in args["lines"]]
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
