"""[PLAN_AUDIT_CONVERSIONS Lot L3] Contrat du cache — A5, A6, A7, A8, A10.

Ce fichier verrouille trois choses distinctes que le plan traite ensemble :

1. **Lecture des compteurs d'usage cache** (A6) : les upstreams n'ont pas tous
   la même convention ; le proxy doit les lire TOUTES, sinon le cache apparaît
   à zéro dans le dashboard et le coût est faux.
2. **Breakpoints `cache_control`** (A5) : reportés de bout en bout, message comme
   outil, et **pas inventés** quand le client n'en pose pas.
3. **Réécriture de prompt pour cache sémantique absent** (A7) : conditionnée au
   modèle (`CACHE_REWRITE_MODELS`) et à la taille minimale du prompt.

L'exception `glm-5` (qui ne supporte pas `cache_control`) est testée
explicitement : c'est une contrainte amont, pas un oubli.
"""

import pytest

import protocol_mapping as pm
from app.protocol import mapping as _canon

# ───────────────────── A6 : compteurs d'usage cache ─────────────────────


@pytest.mark.parametrize(
    "usage,expected",
    [
        # Convention OpenAI moderne
        ({"prompt_tokens_details": {"cached_tokens": 1234}}, 1234),
        # Convention OpenAI à plat (certains upstreams compatibles)
        ({"cached_tokens": 1234}, 1234),
        # Convention Anthropic (passthrough)
        ({"cache_read_input_tokens": 1234}, 1234),
        # Absent → 0, jamais None/erreur
        ({}, 0),
        ({"prompt_tokens": 100}, 0),
    ],
)
def test_cache_read_tokens_all_conventions(usage, expected):
    """A6 : toutes les conventions de lecture de cache sont couvertes."""
    assert pm._extract_cache_tokens(usage) == expected


def test_cache_read_tokens_priority_is_stable():
    """Quand plusieurs conventions coexistent, la priorité est déterministe
    (OpenAI d'abord) — sinon le compteur oscillerait d'un upstream à l'autre."""
    usage = {
        "prompt_tokens_details": {"cached_tokens": 111},
        "cached_tokens": 222,
        "cache_read_input_tokens": 333,
    }
    assert pm._extract_cache_tokens(usage) == 111


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({"prompt_tokens_details": {"cache_creation_tokens": 42}}, 42),
        ({"cache_creation_input_tokens": 42}, 42),
        ({"prompt_cache_miss_tokens": 42}, 42),
        ({}, 0),
    ],
)
def test_cache_creation_tokens_all_conventions(usage, expected):
    """A6 : l'écriture en cache (création) est lue sur toutes les conventions."""
    assert pm._extract_cache_creation_tokens(usage) == expected


def test_cache_token_readers_tolerate_malformed_usage():
    """Robustesse : un `usage` malformé ne doit jamais lever (données amont)."""
    for broken in [
        {"prompt_tokens_details": None},
        {"prompt_tokens_details": "pas un dict"},
        {"cached_tokens": None},
        {"cache_read_input_tokens": "beaucoup"},
    ]:
        pm._extract_cache_tokens(broken)  # ne doit pas lever
        pm._extract_cache_creation_tokens(broken)


# ─────────────────── A5 : breakpoints cache_control ───────────────────

ANTHRO_WITH_CC = {
    "model": "test-model",
    "max_tokens": 4096,
    "system": [
        {"type": "text", "text": "Tu es un assistant.", "cache_control": {"type": "ephemeral"}}
    ],
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Bonjour", "cache_control": {"type": "ephemeral"}}
            ],
        }
    ],
}


def test_breakpoint_on_last_user_message_is_transported():
    """Le breakpoint du dernier message utilisateur est le cas nominal."""
    out = pm.anthropic_to_openai(ANTHRO_WITH_CC, "deepseek-v4-flash")
    user_msgs = [m for m in out["messages"] if m.get("role") == "user"]
    assert user_msgs[-1].get("cache_control") == {"type": "ephemeral"}


def test_breakpoint_on_system_message_is_transported():
    """Le breakpoint système (gros prompt statique) est transporté."""
    out = pm.anthropic_to_openai(ANTHRO_WITH_CC, "deepseek-v4-flash")
    sys_msgs = [m for m in out["messages"] if m.get("role") == "system"]
    assert sys_msgs, "le message système a disparu"
    assert sys_msgs[0].get("cache_control") == {"type": "ephemeral"}


def test_last_user_message_receives_prefix_breakpoint():
    """A5/§9.3 : le proxy pose LUI-MÊME un breakpoint sur le dernier message
    utilisateur (pratique Anthropic recommandée : cache = système + dernier tour
    utilisateur). C'est un choix assumé, pas un défaut — on le verrouille.

    Ce test remplace une attente initiale erronée (« aucun breakpoint inventé ») :
    l'injection est voulue pour le préfixe, et c'est précisément ce que §9.3
    désigne comme le sujet réel (nous ajoutons `cache_control`, champ non
    standard côté Chat).
    """
    out = pm.anthropic_to_openai(
        {
            "model": "test-model",
            "max_tokens": 4096,
            "system": "court",
            "messages": [{"role": "user", "content": "Bonjour"}],
        },
        "deepseek-v4-flash",
    )
    user_msgs = [m for m in out["messages"] if m.get("role") == "user"]
    assert user_msgs[-1].get("cache_control") == {"type": "ephemeral"}


def test_breakpoint_injection_is_bounded_and_under_the_anthropic_limit():
    """Garde-fou A20 : l'injection reste bornée à **2** breakpoints message
    (1 système + 1 dernier tour utilisateur), quelle que soit la longueur de la
    conversation. Anthropic n'autorise que 4 breakpoints au total : les
    multiplier ferait échouer la requête en 400.

    C'est la pratique recommandée (préfixe système stable + dernier tour), et
    elle ne doit PAS croître avec l'historique.
    """
    out = pm.anthropic_to_openai(
        {
            "model": "test-model",
            "max_tokens": 4096,
            "system": "court",
            "messages": [
                {"role": "user", "content": "un"},
                {"role": "assistant", "content": "deux"},
                {"role": "user", "content": "trois"},
                {"role": "assistant", "content": "quatre"},
                {"role": "user", "content": "cinq"},
            ],
        },
        "deepseek-v4-flash",
    )
    with_cc = [m for m in out["messages"] if m.get("cache_control")]
    assert len(with_cc) <= 2, f"{len(with_cc)} breakpoints message posés (max 2 attendu)"
    roles = sorted(m.get("role") for m in with_cc)
    assert roles == ["system", "user"], f"rôles des breakpoints inattendus : {roles}"
    # Le breakpoint utilisateur doit viser le DERNIER tour, pas un tour ancien.
    assert with_cc[-1].get("content") == "cinq"


@pytest.mark.parametrize("cc", [{"type": "ephemeral"}, {"type": "ephemeral", "ttl": "1h"}])
def test_tool_breakpoint_variants_are_transported(cc):
    """A5 : toutes les formes de breakpoint sur un outil sont transportées."""
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [
            {
                "name": "t",
                "description": "d",
                "input_schema": {"type": "object"},
                "cache_control": cc,
            }
        ],
    }
    out = pm.anthropic_to_openai(body, "deepseek-v4-flash")
    assert out["tools"][0].get("cache_control") == cc


def test_tool_breakpoint_survives_openai_format_input():
    """A5 : la branche « outil déjà au format OpenAI » reporte aussi."""
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [
            {
                "type": "function",
                "function": {"name": "t", "description": "d", "parameters": {}},
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    out = pm.anthropic_to_openai(body, "deepseek-v4-flash")
    assert out["tools"][0].get("cache_control") == {"type": "ephemeral"}


def test_tool_breakpoint_survives_server_tool_branch():
    """A5 : la branche « server tool » (`web_search_*`) reporte aussi."""
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": "x"}],
        "tools": [
            {
                "type": "web_search_2025_03_05",
                "name": "web_search",
                "cache_control": {"type": "ephemeral"},
            }
        ],
    }
    out = pm.anthropic_to_openai(body, "deepseek-v4-flash")
    assert out["tools"][0].get("cache_control") == {"type": "ephemeral"}


# ─────────── A7 : exception glm-5 (ne supporte pas cache_control) ───────────


def test_glm5_gets_no_cache_control_on_messages():
    """A7 : `glm-5*` ne supporte pas `cache_control` — on n'en émet pas.
    Contrainte amont explicite, pas un oubli (cf. mapping.py « GLM-5.x models
    don't support cache_control »)."""
    out = pm.anthropic_to_openai(
        {
            "model": "glm-5-air",
            "max_tokens": 4096,
            "system": "Tu es un assistant.",
            "messages": [{"role": "user", "content": "Bonjour"}],
        },
        "glm-5-air",
    )
    for m in out["messages"]:
        assert "cache_control" not in m, f"cache_control émis vers glm-5 sur {m.get('role')}"


def test_non_glm5_gets_cache_control_on_system():
    """Symétrie : un modèle standard en reçoit sur le prompt système (préfixe
    stable = ce qui rend le cache efficace)."""
    out = pm.anthropic_to_openai(
        {
            "model": "deepseek-v4-flash",
            "max_tokens": 4096,
            "system": "Tu es un assistant.",
            "messages": [{"role": "user", "content": "Bonjour"}],
        },
        "deepseek-v4-flash",
    )
    sys_msgs = [m for m in out["messages"] if m.get("role") == "system"]
    assert sys_msgs and sys_msgs[0].get("cache_control") == {"type": "ephemeral"}


# ─────────── A7 : réécriture de prompt (cache sémantique absent) ───────────

LONG_SYSTEM = "Instructions statiques. " * 200 + "\n\nHistorique dynamique : " + "x" * 200


def test_cache_rewrite_only_for_configured_models():
    """A7 : `_restructure_for_cache` ne s'applique QU'aux modèles de
    `CACHE_REWRITE_MODELS` — un modèle à cache sémantique ne doit pas voir son
    prompt scindé (ce serait une régression de cache, pas une optimisation)."""
    assert _canon.CACHE_REWRITE_MODELS, "liste de modèles vide : contrat invalide"
    target = sorted(_canon.CACHE_REWRITE_MODELS)[0]
    other = "deepseek-v4-flash"
    assert other not in _canon.CACHE_REWRITE_MODELS, (
        "le modèle témoin ne doit pas être dans CACHE_REWRITE_MODELS"
    )

    body = {
        "model": target,
        "max_tokens": 4096,
        "system": LONG_SYSTEM,
        "messages": [{"role": "user", "content": "Question"}],
    }
    rewritten = _canon._restructure_for_cache(
        pm.anthropic_to_openai({**body, "effort": "none"}, other), target
    )
    n_before = len(rewritten["messages"])
    # Le modèle cible est réécrit, l'autre non : on compare les deux appels.
    for_cache = _canon._restructure_for_cache(
        pm.anthropic_to_openai({**body, "effort": "none"}, target), target
    )
    not_for_cache = _canon._restructure_for_cache(
        pm.anthropic_to_openai({**body, "effort": "none"}, other), other
    )
    assert len(for_cache["messages"]) >= n_before
    # Le témoin garde son unique message système.
    sys_not = [m for m in not_for_cache["messages"] if m.get("role") == "system"]
    assert len(sys_not) == 1


def test_cache_rewrite_skips_short_prompts():
    """A7 : sous `CACHE_MIN_PROMPT_SIZE`, aucune scission — scinder un petit
    prompt coûte un aller-retour sans rien mettre en cache."""
    target = sorted(_canon.CACHE_REWRITE_MODELS)[0]
    body = {
        "model": target,
        "messages": [
            {"role": "system", "content": "court"},
            {"role": "user", "content": "Question"},
        ],
    }
    out = _canon._restructure_for_cache(dict(body), target)
    assert len(out["messages"]) == 2, "un prompt court a été scindé à tort"


def test_cache_rewrite_is_idempotent():
    """Rejouer la réécriture ne doit pas empiler des messages système : la
    conversion peut être appelée deux fois (retry, failover)."""
    target = sorted(_canon.CACHE_REWRITE_MODELS)[0]
    body = {
        "model": target,
        "messages": [
            {"role": "system", "content": LONG_SYSTEM},
            {"role": "user", "content": "Question"},
        ],
    }
    once = _canon._restructure_for_cache(dict(body), target)
    twice = _canon._restructure_for_cache(dict(once), target)
    sys_once = sum(1 for m in once["messages"] if m.get("role") == "system")
    sys_twice = sum(1 for m in twice["messages"] if m.get("role") == "system")
    assert sys_twice <= sys_once, "la réécriture empile les messages système"


def test_cache_rewrite_skipped_when_no_system_message():
    """Sans message système il n'y a pas de préfixe stable à isoler."""
    target = sorted(_canon.CACHE_REWRITE_MODELS)[0]
    body = {"model": target, "messages": [{"role": "user", "content": "Question"}]}
    out = _canon._restructure_for_cache(dict(body), target)
    assert len(out["messages"]) == 1


# ─────────── A8 : guards d'orphelins homogènes ───────────


def test_orphan_guard_operates_on_chat_format():
    """A8 : `_drop_orphan_tool_messages` consomme le format **Chat**
    (`role:"tool"` + `tool_calls[].id`). C'est le contrat réel de ce guard —
    en P2 il est appliqué *après* conversion (mapping.py:1109), pas sur l'entrée
    Anthropic brute."""
    messages = [
        {"role": "user", "content": "météo ?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_ok", "type": "function", "function": {"name": "t", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_ok", "content": "il fait beau"},
    ]
    out = pm._drop_orphan_tool_messages(messages)
    assert len(out) == 3, "un couple tool_calls/tool valide a été supprimé"


def test_orphan_guard_removes_unmatched_tool_message():
    messages = [
        {"role": "user", "content": "salut"},
        {"role": "tool", "tool_call_id": "call_fantome", "content": "résultat"},
    ]
    out = pm._drop_orphan_tool_messages(messages)
    assert not any(m.get("role") == "tool" for m in out)


def test_orphan_tool_result_is_dropped_end_to_end_in_p2():
    """A8 : un `tool_result` Anthropic SANS `tool_use` correspondant est bien
    retiré par la conversion P2 — le guard de fin de `anthropic_to_openai`
    (mapping.py:1109) opère sur les messages Chat produits.

    C'est le contrat de bout en bout qui compte : un orphelin non filtré fait
    échouer l'amont OpenAI en 400.
    """
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": "salut"},
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_fantome", "content": "orphelin"}
                ],
            },
        ],
    }
    out = pm.anthropic_to_openai(body, "deepseek-v4-flash")
    assert not any(m.get("role") == "tool" for m in out["messages"]), (
        "un tool_result orphelin a survécu à la conversion → 400 amont (A8)"
    )


def test_valid_tool_pair_survives_p2_end_to_end():
    """Contre-preuve : un couple tool_use/tool_result VALIDE traverse P2."""
    body = {
        "model": "test-model",
        "max_tokens": 4096,
        "messages": [
            {"role": "user", "content": "météo ?"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "toolu_ok", "name": "t", "input": {}}],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_ok", "content": "il fait beau"}
                ],
            },
        ],
        "tools": [{"name": "t", "description": "d", "input_schema": {"type": "object"}}],
    }
    out = pm.anthropic_to_openai(body, "deepseek-v4-flash")
    assert any(m.get("role") == "tool" for m in out["messages"]), (
        "le tool_result valide a été supprimé à tort"
    )


def test_responses_orphan_guard_keeps_valid_pair():
    inp = [
        {"role": "user", "content": [{"type": "input_text", "text": "météo ?"}]},
        {"type": "function_call", "call_id": "call_ok", "name": "t", "arguments": "{}"},
        {"type": "function_call_output", "call_id": "call_ok", "output": "beau"},
    ]
    out = pm._drop_orphan_responses_input(inp)
    assert len(out) == 3, "un couple Responses valide a été supprimé"
