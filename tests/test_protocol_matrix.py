"""[PLAN_AUDIT_CONVERSIONS Lot L1] Matrice de vérité V1 : 6 chemins × 15 axes.

Objectif du lot : **prouver avant de corriger**. Chaque case exécute la fonction
RÉELLE de conversion sur un corps témoin et assertte la **présence/absence d'un
champ précis** — jamais « ça ne plante pas ». Une case non couverte est
déclarée *gap connu* ici même : impossible d'ajouter un chemin sans le déclarer.

Les 6 chemins (cf. PLAN §1) :

    P1  /v1/messages          → anthropic  (passthrough)
    P2  /v1/messages          → openai     (anthropic_to_openai)
    P3  /v1/chat/completions  → openai     (passthrough + guards)
    P4  /v1/chat/completions  → anthropic  (openai_to_anthropic_request)
    P5  /v1/responses         → openai     (_chat_to_responses_request)
    P6  /v1/responses         → anthropic  (openai_responses_to_anthropic)

Étage V1 = ce fichier. Les étages V2 (goldens), V3 (ASGI) et V4 (corpus) sont
dans les lots L6/L7.
"""

import pytest

import protocol_mapping as pm

# ─────────────────────────── Corps témoins ───────────────────────────
# Un corps unique par protocole d'entrée, réutilisé par tous les axes : c'est
# ce qui rend les cases comparables entre chemins.

ANTHRO_BODY = {
    "model": "test-model",
    "max_tokens": 4096,
    "system": "Tu es un assistant.",
    "messages": [{"role": "user", "content": "Bonjour"}],
}

ANTHRO_BODY_FULL = {
    "model": "test-model",
    "max_tokens": 4096,
    "system": "Tu es un assistant.",
    "messages": [{"role": "user", "content": "Bonjour"}],
    "tools": [
        {
            "name": "get_weather",
            "description": "Météo",
            "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
        }
    ],
    "tool_choice": {"type": "auto"},
}

CHAT_BODY = {
    "model": "test-model",
    "max_tokens": 4096,
    "messages": [{"role": "user", "content": "Bonjour"}],
}

CHAT_BODY_TOOLS = {
    **CHAT_BODY,
    "tools": [
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Météo",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
            },
        }
    ],
}

RESPONSES_BODY = {
    "model": "test-model",
    "input": [
        {"role": "user", "content": [{"type": "input_text", "text": "Bonjour"}]},
    ],
    "max_output_tokens": 4096,
}


# ────────────────────── Axe 1 : effort en entrée ──────────────────────
# A1 : 4 mappings divergents pour la même notion. A2 : xhigh/max écrasés.
# A13 : output_config.effort jamais lu.

ANTHRO_EFFORT_BODIES = {
    "legacy_effort_top_level": {**ANTHRO_BODY, "effort": "high"},
    "documented_output_config": {**ANTHRO_BODY, "output_config": {"effort": "high"}},
    "thinking_adaptive": {**ANTHRO_BODY, "thinking": {"type": "adaptive"}},
    "thinking_enabled_budget": {
        **ANTHRO_BODY,
        "thinking": {"type": "enabled", "budget_tokens": 6000},
    },
}


@pytest.mark.parametrize("case", sorted(ANTHRO_EFFORT_BODIES))
def test_axis_effort_p2_reads_every_input_form(case):
    """P2 : toute forme d'effort en entrée doit produire un `reasoning_effort`.

    Preuve de A13 : la forme documentée `output_config.effort` doit être lue,
    pas seulement journalisée. Preuve de A1/A2 : les autres formes aussi.
    """
    body = ANTHRO_EFFORT_BODIES[case]
    out = pm.anthropic_to_openai(body, "deepseek-v4-flash")
    assert out.get("reasoning_effort"), (
        f"P2 : la forme d'effort {case!r} n'a produit aucun reasoning_effort — "
        "l'effort du client est perdu (A13 si output_config.effort)."
    )


def test_axis_effort_p2_model_cap_enforced():
    """P2 : le plafond du modèle s'applique (A2 — glm-5 plafonne à high)."""
    out = pm.anthropic_to_openai(
        {**ANTHRO_BODY, "output_config": {"effort": "max"}}, "glm-5-air"
    )
    assert out["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "requested,expected",
    [("low", "low"), ("medium", "medium"), ("high", "high"), ("xhigh", "xhigh"), ("max", "max")],
)
def test_axis_effort_p4_no_level_is_lost(requested, expected):
    """P4 : aucun niveau ne doit être écrasé (A2 — l'ancien dict n'avait que
    3 entrées et repliait tout le reste sur 16000)."""
    out = pm.openai_to_anthropic_request(
        {**CHAT_BODY, "model": "deepseek-v4-flash", "reasoning_effort": requested}
    )
    assert out.get("output_config", {}).get("effort") == expected


def test_axis_effort_p4_minimal_is_recognized():
    """A2 : `minimal` est une valeur OpenAI légitime, pas à supprimer."""
    out = pm.openai_to_anthropic_request(
        {**CHAT_BODY, "model": "deepseek-v4-flash", "reasoning_effort": "minimal"}
    )
    assert out.get("output_config", {}).get("effort") == "low"


def test_axis_effort_p6_reaches_anthropic():
    """P6 : l'effort doit survivre jusqu'au corps Anthropic (A3/A15)."""
    req = {**RESPONSES_BODY, "reasoning": {"effort": "medium"}}
    out = pm.openai_responses_to_anthropic(req)
    assert "reasoning_effort" not in out, (
        "P6 : `reasoning_effort` est un nom de champ OPENAI — il ne doit jamais "
        "partir vers un amont Anthropic (A15)."
    )
    assert out.get("output_config", {}).get("effort") == "medium", (
        "P6 : l'effort est perdu vers l'amont Anthropic (A3)."
    )


def test_axis_effort_no_path_emits_deprecated_thinking_enabled():
    """A14/A23 : aucun chemin ne doit plus émettre `thinking.enabled` +
    `budget_tokens` (forme rejetée sur Claude 4.7+, et budget > max_tokens)."""
    outs = {
        "P4": pm.openai_to_anthropic_request(
            {**CHAT_BODY, "reasoning_effort": "high", "max_tokens": 512}
        ),
        "P6": pm.openai_responses_to_anthropic(
            {**RESPONSES_BODY, "reasoning": {"effort": "high"}, "max_output_tokens": 512}
        ),
    }
    for path, out in outs.items():
        thinking = out.get("thinking", {})
        assert thinking.get("type") != "enabled", (
            f"{path} : émet encore thinking.enabled, forme dépréciée/rejetée (A14)."
        )
        assert "budget_tokens" not in thinking, (
            f"{path} : émet encore budget_tokens, qui peut dépasser max_tokens (A23)."
        )


# ─────────────────── Axe 2 : budget thinking ↔ niveau ───────────────────


@pytest.mark.parametrize(
    "budget,expected",
    [
        (3999, "low"),
        (4000, "medium"),
        (9999, "medium"),
        (10000, "high"),
        (15999, "high"),
        (16000, "xhigh"),
    ],
)
def test_axis_budget_to_level_p2(budget, expected):
    """P2 : la table budget → niveau reste le contrat d'entrée legacy."""
    out = pm.anthropic_to_openai(
        {
            **ANTHRO_BODY,
            "thinking": {"type": "enabled", "budget_tokens": budget},
        },
        "muse-spark-1.3-contributor",
    )
    assert out["reasoning_effort"] == expected


def test_axis_budget_never_exceeds_max_tokens_p4():
    """A23 : l'invariant Anthropic `max_tokens > budget_tokens` est
    structurellement respecté — on n'émet plus aucun budget (L16)."""
    out = pm.openai_to_anthropic_request(
        {**CHAT_BODY, "reasoning_effort": "high", "max_tokens": 512}
    )
    budget = out.get("thinking", {}).get("budget_tokens")
    assert budget is None or budget < out["max_tokens"], (
        f"P4 : budget_tokens={budget} >= max_tokens={out.get('max_tokens')} → 400 (A23)."
    )


# ───────────────────── Axe 3 : plancher max_tokens ─────────────────────


def test_axis_min_tokens_floor_p3_is_wired():
    """A4 CORRIGÉE (Lot L2) : `ensure_min_tokens` est désormais appelé par le
    handler /v1/chat/completions comme sur P1/P2/P6.

    Ce test a d'abord été écrit comme *détecteur de trou* (il passait tant que
    l'appel était absent, cf. historique L1). Il est retourné ici : c'est le
    signal que le lot L2 a bien câblé le plancher.
    """
    import inspect
    import sys

    if "opencode" not in sys.modules:
        pytest.skip("opencode non importé dans ce contexte de test (import lourd)")
    src = inspect.getsource(sys.modules["opencode"].chat_completions)
    # On ignore les commentaires : ils citent le nom sans appeler la fonction.
    calls = [
        line for line in src.splitlines()
        if "ensure_min_tokens" in line and not line.lstrip().startswith("#")
    ]
    assert calls, "P3 n'appelle toujours pas ensure_min_tokens (A4 non corrigée)."
    assert any("ensure_min_tokens(body)" in line for line in calls), (
        "l'appel doit porter sur le corps de la requête : ensure_min_tokens(body)"
    )


# ───────────────────────── Axe 4 : cache_control ─────────────────────────


def test_axis_cache_control_on_tools_p2_is_preserved():
    """A5 CORRIGÉE (Lot L3) : le breakpoint `cache_control` posé sur un outil
    survit à la conversion P2.

    Écrit d'abord comme preuve de la perte au Lot 1, retourné ici. Le plan
    §9.3 a déclassé A5 (l'amont OpenAI ignore ce champ) mais la perte restait
    *silencieuse* et asymétrique avec le traitement des messages : on transporte
    désormais, comme pour les messages.
    """
    body = {
        **ANTHRO_BODY,
        "tools": [
            {
                "name": "get_weather",
                "description": "Météo",
                "input_schema": {"type": "object", "properties": {}},
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    out = pm.anthropic_to_openai(body, "deepseek-v4-flash")
    assert out["tools"][0].get("cache_control") == {"type": "ephemeral"}


def test_axis_tool_without_cache_control_gains_no_field():
    """Contre-preuve : on n'injecte pas de `cache_control` fantôme."""
    out = pm.anthropic_to_openai(ANTHRO_BODY_FULL, "deepseek-v4-flash")
    assert "cache_control" not in out["tools"][0]


def test_axis_cache_control_on_last_user_message_p2_is_preserved():
    """Le breakpoint au niveau message, lui, est bien reporté (ne pas casser)."""
    body = {
        **ANTHRO_BODY,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": "Bonjour",
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
            }
        ],
    }
    out = pm.anthropic_to_openai(body, "deepseek-v4-flash")
    assert out["messages"][-1].get("cache_control") == {"type": "ephemeral"}


# ──────────────────────── Axe 5 : usage cache ────────────────────────


@pytest.mark.parametrize(
    "usage,expected_read",
    [
        ({"cache_read_input_tokens": 1234}, 1234),
        ({"prompt_tokens_details": {"cached_tokens": 5678}}, 5678),
        ({}, 0),
    ],
)
def test_axis_cache_read_tokens_extracted(usage, expected_read):
    """A6 : les deux conventions d'usage cache doivent être lues."""
    assert pm._extract_cache_tokens(usage) == expected_read


def test_axis_cache_creation_tokens_extracted():
    """A6 : la création de cache (champ Anthropic) doit être lue."""
    assert pm._extract_cache_creation_tokens({"cache_creation_input_tokens": 42}) == 42


# ───────────────────────── Axe 6 : tools ─────────────────────────


def test_axis_tools_p2_shape():
    """P2 : tools Anthropic → tools OpenAI `{type:function, function:{...}}`."""
    out = pm.anthropic_to_openai(ANTHRO_BODY_FULL, "deepseek-v4-flash")
    assert out["tools"][0]["type"] == "function"
    assert out["tools"][0]["function"]["name"] == "get_weather"


def test_axis_tools_p4_shape():
    """P4 : tools OpenAI → tools Anthropic `{name, description, input_schema}`."""
    out = pm.openai_to_anthropic_request(CHAT_BODY_TOOLS)
    assert out["tools"][0]["name"] == "get_weather"
    assert "input_schema" in out["tools"][0]


def test_axis_tools_choice_dict_to_dict():
    """Axe tool_choice : dict↔dict sans perte de sémantique."""
    out = pm.openai_to_anthropic_request(
        {
            **CHAT_BODY_TOOLS,
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        }
    )
    assert out["tool_choice"] == {"type": "tool", "name": "get_weather"}


def test_axis_tools_choice_any_becomes_required():
    """P4 : `any` (Anthropic) → `required` (OpenAI), pas `auto`."""
    out = pm.anthropic_to_openai(
        {**ANTHRO_BODY_FULL, "tool_choice": {"type": "any"}}, "deepseek-v4-flash"
    )
    assert out["tool_choice"] == "required"


# ─────────────────── Axe 7 : noms d'outils longs ───────────────────


def test_axis_long_tool_name_is_sanitized_and_restorable():
    """A8 : un nom > 64 car. est raccourci, et la table permet le retour."""
    long_name = "a_very_long_tool_name_" + "x" * 80
    tools = [{"name": long_name, "description": "d", "input_schema": {"type": "object"}}]
    sanitized, name_map = pm.sanitize_tool_names(tools)
    assert len(sanitized[0]["name"]) <= 64, "nom non raccourci → 400 côté OpenAI (A8)."
    assert pm.restore_tool_name(sanitized[0]["name"], name_map) == long_name


# ──────────────────────── Axe 8 : documents ────────────────────────


def test_axis_document_url_p2_becomes_file():
    """P2 : un document URL devient un `file` OpenAI (ou un repli texte)."""
    body = {
        **ANTHRO_BODY,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Résume"},
                    {
                        "type": "document",
                        "source": {"type": "url", "url": "https://example.com/a.pdf"},
                    },
                ],
            }
        ],
    }
    out = pm.anthropic_to_openai(body, "deepseek-v4-flash")
    dumped = str(out)
    assert "example.com/a.pdf" in dumped, (
        "l'URL du document est perdue : ni `file` ni repli texte (A9)."
    )


def test_axis_document_url_responses_p6():
    """P6 : un document URL doit survivre jusqu'au corps Anthropic."""
    req = {
        **RESPONSES_BODY,
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_file", "file_url": "https://example.com/a.pdf"},
                ],
            }
        ],
    }
    out = pm.openai_responses_to_anthropic(req)
    assert "example.com/a.pdf" in str(out), "document URL perdu en P6 (A9)."


# ────────────────────────── Axe 9 : images ──────────────────────────

_B64_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def test_axis_image_p2_becomes_image_url():
    """P2 : image Anthropic base64 → data-URI OpenAI (verrou existant, à ne pas
    casser)."""
    body = {
        **ANTHRO_BODY,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Décris"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": _B64_PNG},
                    },
                ],
            }
        ],
    }
    out = pm.anthropic_to_openai(body, "deepseek-v4-flash")
    assert "data:image/png;base64," in str(out), "image base64 perdue en P2."


def test_axis_image_in_tool_result_p2():
    """A9 : une image portée par un `tool_result` doit survivre."""
    body = {
        **ANTHRO_BODY,
        "messages": [
            {"role": "user", "content": "prends une capture"},
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "toolu_1", "name": "shot", "input": {}}
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": _B64_PNG,
                                },
                            }
                        ],
                    }
                ],
            },
        ],
    }
    out = pm.anthropic_to_openai(body, "deepseek-v4-flash")
    assert "data:image/png;base64," in str(out), "image dans tool_result perdue en P2."


# ────────────── Axe 10 : estimation tokens (A10) ──────────────


def test_axis_token_estimate_ignores_media_size():
    """A10 CORRIGÉE (Lot L4) : l'estimation de tokens tient désormais compte de
    la TAILLE des médias.

    Au Lot 1, ce test prouvait la sous-estimation (`_extract_text` réduisait
    toute image à `[image:base64]`, donc deux images de tailles très
    différentes donnaient le même compte). Le lot L4 a ajouté un coût média
    proportionnel dans `protocol/tokens.py` : le test est retourné.

    L'extracteur `_extract_text` conserve volontairement son marqueur court :
    il sert AUSSI à produire du contenu réel (système, tool_result), pas
    seulement à compter — c'est l'estimateur qui devait changer, pas lui.
    """
    from protocol.tokens import estimate_input_tokens

    small = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _B64_PNG},
        }
    ]
    huge = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": _B64_PNG * 500},
        }
    ]
    def body(content):
        return {"model": "t", "max_tokens": 128, "messages": [{"role": "user", "content": content}]}
    n_small = estimate_input_tokens(body(small))
    n_huge = estimate_input_tokens(body(huge))
    assert n_huge > n_small, (
        "l'estimation ignore toujours la taille des médias : une grande image "
        "doit coûter plus qu'une petite (A10)"
    )


# ─────────────── Axe 11 : orphelins tool_result ───────────────


def test_axis_orphan_tool_messages_dropped():
    """Guards d'orphelins : un `tool` sans `tool_use` correspondant est retiré."""
    messages = [
        {"role": "user", "content": "salut"},
        {"role": "tool", "tool_call_id": "call_inexistant", "content": "résultat"},
        {"role": "assistant", "content": "ok"},
    ]
    out = pm._drop_orphan_tool_messages(messages)
    assert not any(m.get("role") == "tool" for m in out), "orphelin non retiré (A8)."


def test_axis_orphan_responses_input_dropped():
    """Idem pour le format Responses (`function_call_output`)."""
    inp = [
        {"role": "user", "content": [{"type": "input_text", "text": "salut"}]},
        {"type": "function_call_output", "call_id": "call_inexistant", "output": "x"},
    ]
    out = pm._drop_orphan_responses_input(inp)
    assert not any(i.get("type") == "function_call_output" for i in out), (
        "orphelin Responses non retiré (A8)."
    )


# ─────────────── Axe 12 : ordre des blocs en réponse ───────────────


def test_axis_response_block_order_p4():
    """Ordre contractuel des blocs : `thinking` → `text` → `tool_use`."""
    anthro = {
        "id": "msg_1",
        "model": "claude-sonnet-4",
        "content": [
            {"type": "thinking", "thinking": "je réfléchis", "signature": "sig"},
            {"type": "text", "text": "Voici."},
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Paris"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    out = pm.anthropic_to_openai_response(anthro, "deepseek-v4-flash")
    choice = out["choices"][0]["message"]
    assert choice.get("reasoning_content") == "je réfléchis"
    assert "Voici." in (choice.get("content") or "")
    assert choice["tool_calls"][0]["function"]["name"] == "get_weather"


# ──────────────── Axe 13 : thinking multi-tours ────────────────


def test_axis_multiturn_thinking_strip_p1():
    """P1 : le thinking synthétique du proxy est strippé avant l'amont."""
    body = {
        **ANTHRO_BODY,
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "contenu", "signature": pm._local_signature("contenu")},
                    {"type": "text", "text": "Réponse"},
                ],
            }
        ],
    }
    removed = pm.strip_synthetic_thinking(body)
    assert removed >= 1, "thinking synthétique non strippé en P1."
    remaining = body["messages"][0]["content"]
    assert not any(b.get("type") == "thinking" for b in remaining)


def test_axis_multiturn_thinking_to_reasoning_content_p2():
    """P2 : le thinking multi-tours devient `reasoning_content` côté Chat.

    Le message assistant converti est repéré par son rôle (l'index dépend de la
    présence d'un message système en tête).
    """
    body = {
        **ANTHRO_BODY,
        "messages": [
            {"role": "user", "content": "Question"},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "raisonnement", "signature": "SIG"},
                    {"type": "text", "text": "Réponse"},
                ],
            },
        ],
    }
    out = pm.anthropic_to_openai(body, "deepseek-v4-flash")
    assistants = [m for m in out["messages"] if m.get("role") == "assistant"]
    assert assistants, "le message assistant a disparu de la conversion."
    assert assistants[0].get("reasoning_content") == "raisonnement", (
        "le thinking multi-tours n'est pas transporté en reasoning_content (A1)."
    )


# ──────────── Axe 14 : streaming (A11 faux streaming) ────────────


def test_axis_responses_stream_is_incremental():
    """A11 CORRIGÉ (L5) : le handler `/v1/responses` émet une séquence
    incrémentale au lieu du seul `response.completed` terminal.

    Avant L5, ce test vérifiait le **gap** (aucun delta, TTFB = durée totale de
    génération) et sa docstring indiquait de le retourner quand L5 passerait.
    C'est fait : le handler délègue désormais à l'émetteur d'événements, dont
    les tests détaillés vivent dans `test_responses_stream_contract.py` et
    `test_responses_stream_e2e.py`.
    """
    import inspect
    import sys

    if "opencode" not in sys.modules:
        pytest.skip("opencode non importé dans ce contexte de test (import lourd)")
    src = inspect.getsource(sys.modules["opencode"])
    assert "responses_stream_events(" in src, (
        "A11 régressé : le handler ne construit plus de séquence incrémentale."
    )


def test_axis_chat_stream_conversion_produces_deltas():
    """P4 stream : les deltas Responses deviennent des deltas Chat.

    Format d'entrée = JSON nu (sans préfixe `data: `) : c'est ce que documente
    le golden `sse_deltas`.
    """
    out = pm._responses_sse_to_chat_deltas(
        '{"type":"response.output_text.delta","delta":"Bonjour"}'
    )
    assert out, "aucun delta produit pour un fragment Responses."
    assert out["choices"][0]["delta"]["content"] == "Bonjour"


def test_axis_responses_reasoning_delta_becomes_reasoning_content():
    """P4 stream : le résumé de raisonnement Responses alimente
    `reasoning_content` (transport du raisonnement vers un client Chat)."""
    out = pm._responses_sse_to_chat_deltas(
        '{"type":"response.content_part.delta",'
        '"delta":{"type":"reasoning_summary_text","text":"hm"}}'
    )
    assert out["choices"][0]["delta"]["reasoning_content"] == "hm"


# ──────────── Axe 15 : sanitize/restore symétrie ────────────


@pytest.mark.parametrize(
    "name",
    [
        "simple",
        "with-dash",
        "with_underscore",
        "CamelCase",
        "a" * 64,
        "name with spaces",
        "name/with/slashes",
        "outil.avec.points",
    ],
)
def test_axis_tool_name_roundtrip_is_identity(name):
    """A8 : sanitize puis restore doit rendre EXACTEMENT le nom d'origine.

    C'est l'invariant qui protège du sanitize *lossy* (`foo/bar` et `foo_bar`
    se replieraient sinon sur la même clé).
    """
    tools = [{"name": name, "description": "d", "input_schema": {"type": "object"}}]
    sanitized, name_map = pm.sanitize_tool_names(tools)
    assert pm.restore_tool_name(sanitized[0]["name"], name_map) == name
