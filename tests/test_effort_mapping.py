"""[Lot H4] Mapping effort ↔ reasoning : tous niveaux × familles de modèles,
plus le sens inverse (reasoning_effort OpenAI → thinking Anthropic).

Clamp config-driven : ``_effort_to_reasoning`` délègue à
``config.effort_caps.clamp_effort`` (section ``thinking`` de ``config.yaml``,
lue en live). Ces tests valident le comportement avec la config réelle du
repo (spark → xhigh, glm-5 → high, deepseek-v4 → max, mimo-v2.5 → max,
nemotron-3-ultra → high, nemotron-3.5-lightning → max, défaut → high) —
voir ``tests/test_effort_caps.py`` pour la logique unitaire (caps custom,
longest-prefix, hot-reload logique)."""

import pytest

import protocol_mapping as pm


@pytest.mark.parametrize(
    "effort,model,expected",
    [
        # glm-5 : xhigh/max/high → high, medium → medium, low/autre → low
        ("xhigh", "glm-5-air", "high"),
        ("max", "glm-5-air", "high"),
        ("high", "glm-5-flash", "high"),
        ("medium", "glm-5-air", "medium"),
        ("low", "glm-5-air", "low"),
        ("", "glm-5-air", "low"),
        # deepseek-v4 : cap max → tout est préservé (min(demandé, max) = demandé),
        # "" → low (repli historique du désactivé dans _effort_to_reasoning)
        ("xhigh", "deepseek-v4-flash", "xhigh"),
        ("max", "deepseek-v4-pro", "max"),
        ("high", "deepseek-v4-flash", "high"),
        ("medium", "deepseek-v4-flash", "medium"),
        ("low", "deepseek-v4-flash", "low"),
        ("", "deepseek-v4-pro", "low"),
        # mimo-v2.5 : cap max (upstream Zen 2×200 max confirmé 2026-09-09),
        # tout est préservé ; mimo-v2-pro garde le défaut high
        ("xhigh", "mimo-v2.5", "xhigh"),
        ("max", "mimo-v2.5", "max"),
        ("high", "mimo-v2.5", "high"),
        ("medium", "mimo-v2.5", "medium"),
        ("low", "mimo-v2.5", "low"),
        ("", "mimo-v2.5", "low"),
        ("xhigh", "mimo-v2-pro", "high"),
        ("max", "mimo-v2-pro", "high"),
        ("high", "mimo-v2-pro", "high"),
        # nemotron-3-ultra : 400 "Model is unavailable" meme a high
        # 2026-09-09 -> defaut high conserve ; nemotron-3.5-lightning :
        # cap max (upstream Zen 2x200 max confirme 2026-09-09)
        ("max", "nemotron-3-ultra-free", "high"),
        ("xhigh", "nemotron-3-ultra-free", "high"),
        ("max", "nemotron-3.5-lightning-free", "max"),
        ("xhigh", "nemotron-3.5-lightning-free", "xhigh"),
        # muse-spark : xhigh préservé (upstream Zen 200 confirmé 2026-09-09),
        # max → xhigh (upstream refuse max en 400), high/medium/low inchangés
        ("xhigh", "muse-spark-1.3-contributor", "xhigh"),
        ("max", "muse-spark-1.3-contributor", "xhigh"),
        ("xhigh", "muse-spark-1.3-contributor-free", "xhigh"),
        ("max", "muse-spark-1.2-contributor", "xhigh"),
        ("high", "muse-spark-1.3-contributor", "high"),
        ("medium", "muse-spark-1.3-contributor", "medium"),
        ("low", "muse-spark-1.3-contributor", "low"),
        ("", "muse-spark-1.3-contributor", "low"),
        # config-driven : normalisation casse/espaces, minimal, désactivé
        # (repli historique "low"), niveau inconnu → passthrough inchangé
        ("MAX", "glm-5-air", "high"),
        (" XHigh ", "muse-spark-1.3-contributor", "xhigh"),
        ("minimal", "muse-spark-1.3-contributor", "minimal"),
        ("minimal", "glm-5-air", "minimal"),
        ("none", "muse-spark-1.3-contributor", "low"),
        (None, "muse-spark-1.3-contributor", "low"),
        ("ultra", "muse-spark-1.3-contributor", "ultra"),
    ],
)
def test_effort_to_reasoning_all_levels(effort, model, expected):
    assert pm._effort_to_reasoning(effort, model) == expected


@pytest.mark.parametrize(
    "thinking,budget,model,expected",
    [
        # bornes budget 3999/4000, 9999/10000, 15999/16000 (±1 autour des seuils)
        (3999, "low", "muse-spark-1.3-contributor", "low"),
        (4000, "medium", "muse-spark-1.3-contributor", "medium"),
        (9999, "medium", "muse-spark-1.3-contributor", "medium"),
        (10000, "high", "muse-spark-1.3-contributor", "high"),
        (15999, "high", "muse-spark-1.3-contributor", "high"),
        (16000, "xhigh", "muse-spark-1.3-contributor", "xhigh"),
        # budget 0 : valeur invalide (min spec = 1024) → aucun budget
        # exploitable, donc traité comme « pas de niveau demandé » → max du modèle.
        (0, "max", "muse-spark-1.3-contributor", "xhigh"),
        # budget dérivé au-delà du cap → relegué au plafond du modèle
        # (ex. 20000 sur glm-5 → dérivé xhigh → clampé high)
        (20000, "xhigh→high", "glm-5-air", "high"),
        (20000, "xhigh→xhigh", "muse-spark-1.3-contributor", "xhigh"),
        (20000, "xhigh→xhigh", "deepseek-v4-flash", "xhigh"),
    ],
)
def test_budget_bounds_clamped_to_model_cap(thinking, budget, model, expected):
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "thinking": {"type": "enabled", "budget_tokens": thinking},
    }
    expected_effort = expected.split("→")[-1]
    assert pm.anthropic_to_openai(body, model)["reasoning_effort"] == expected_effort


def test_double_mapping_idempotent_at_cap():
    """Anthropic(max, glm-5) → OpenAI → Anthropic reste high (plafond)."""
    oai = pm.anthropic_to_openai(
        {"model": "glm-5-air", "messages": [{"role": "user", "content": "hi"}], "effort": "max"},
        "glm-5-air",
    )
    assert oai["reasoning_effort"] == "high"
    back = pm.openai_to_anthropic_request({**oai, "model": "glm-5-air"})
    # [Hotfix A14] Plus de budget_tokens : adaptive + output_config.effort.
    assert back["thinking"] == {"type": "adaptive"}
    assert back["output_config"] == {"effort": "high"}
    again = pm.anthropic_to_openai({**back, "model": "glm-5-air"}, "glm-5-air")
    assert again["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "effort,model,expected_effort",
    [
        # deepseek-v4-flash : plafond max → les niveaux passent tels quels
        ("low", "deepseek-v4-flash", "low"),
        ("medium", "deepseek-v4-flash", "medium"),
        ("high", "deepseek-v4-flash", "high"),
        ("xhigh", "deepseek-v4-flash", "xhigh"),
        ("max", "deepseek-v4-flash", "max"),
        # claude-sonnet-4 : pas dans effort_caps → plafond par defaut (high)
        ("xhigh", "claude-sonnet-4", "high"),
        ("max", "claude-sonnet-4", "high"),
    ],
)
def test_reasoning_effort_to_anthropic_thinking(effort, model, expected_effort):
    """[Hotfix A14/A23] reasoning_effort → adaptive + output_config.effort.

    Remplace l'ancienne assertion ``thinking == {type:enabled, budget_tokens}`` :
    cette forme est depreciee (Claude 4.6) et rejetee en 400 (4.7+), et son
    ratio de budget pouvait depasser les ``max_tokens`` du client.
    """
    oai = {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning_effort": effort,
    }
    result = pm.openai_to_anthropic_request(oai)
    assert result["thinking"] == {"type": "adaptive"}
    assert result["output_config"] == {"effort": expected_effort}
    assert "budget_tokens" not in result["thinking"]


@pytest.mark.parametrize("max_tokens", [256, 512, 1024, 4096, 8192, 32768])
def test_no_budget_tokens_regardless_of_max_tokens(max_tokens):
    """[Hotfix A23] L'invariant Anthropic ``max_tokens > budget_tokens`` ne peut
    plus etre viole : aucun budget n'est emis, quelle que soit la valeur de
    ``max_tokens`` demandee par le client (l'ancien ratio emettait 16000 pour
    ``high`` meme avec ``max_tokens=4096``)."""
    result = pm.openai_to_anthropic_request(
        {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "high",
            "max_tokens": max_tokens,
        }
    )
    assert "budget_tokens" not in result.get("thinking", {})
    assert result["thinking"]["type"] == "adaptive"


def test_minimal_effort_does_not_produce_invalid_level():
    """[Hotfix A14] ``minimal`` n'est pas un niveau Anthropic : il se replie sur
    ``low`` (correspondance LiteLLM), il ne disparait pas et ne fuit pas tel quel."""
    result = pm.openai_to_anthropic_request(
        {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "minimal",
        }
    )
    assert result["output_config"] == {"effort": "low"}


def test_unknown_effort_falls_back_to_high():
    """Un niveau hors vocabulaire Anthropic est ramene au defaut documente
    ``high`` plutot que relaye (l'upstream rejetterait en 400)."""
    result = pm.openai_to_anthropic_request(
        {
            "model": "claude-sonnet-4",
            "messages": [{"role": "user", "content": "hello"}],
            "reasoning_effort": "ultra-mega",
        }
    )
    assert result["output_config"] == {"effort": "high"}


def test_explicit_thinking_disabled_is_not_overridden():
    """Un ``thinking: {type:disabled}`` explicite ne doit pas etre converti en
    adaptive : le client a demande explicitement l'absence de raisonnement.

    ``openai_to_anthropic_request`` ne porte pas de thinking pour un body sans
    effort : le seul risque est qu'une conversion ulterieure en ajoute un."""
    body = {
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "hello"}],
        "thinking": {"type": "disabled"},
    }
    result = pm.openai_to_anthropic_request(body)
    assert result.get("thinking", {}).get("type") != "adaptive"
    assert "output_config" not in result


def test_reasoning_effort_absent_no_thinking():
    oai = {
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "hello"}],
    }
    result = pm.openai_to_anthropic_request(oai)
    assert "thinking" not in result


# ── [Hotfix A13] output_config.effort est lu par les convertisseurs ──────────
# Le champ d'effort de l'API Messages n'etait lu nulle part dans mapping.py :
# l'effort d'un client moderne (Claude Code) etait journalise puis jete.


@pytest.mark.parametrize(
    "effort,model,expected",
    [
        ("low", "deepseek-v4-flash", "low"),
        ("medium", "deepseek-v4-flash", "medium"),
        ("high", "deepseek-v4-flash", "high"),
        ("xhigh", "deepseek-v4-flash", "xhigh"),
        ("max", "deepseek-v4-flash", "max"),
        # glm-5 : plafond high → xhigh/max sont ramenes au plafond
        ("xhigh", "glm-5-air", "high"),
        ("max", "glm-5-air", "high"),
    ],
)
def test_output_config_effort_is_read_p2(effort, model, expected):
    """P2 : un client qui envoie output_config.effort obtient un reasoning_effort.

    Le niveau reste soumis au plafond du modele (glm-5 → high).
    """
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "hello"}],
        "output_config": {"effort": effort},
    }
    assert pm.anthropic_to_openai(body, model)["reasoning_effort"] == expected


def test_output_config_effort_respects_model_cap():
    """output_config.effort reste soumis au plafond du modele."""
    body = {
        "model": "glm-5-air",
        "messages": [{"role": "user", "content": "hello"}],
        "output_config": {"effort": "max"},
    }
    assert pm.anthropic_to_openai(body, "glm-5-air")["reasoning_effort"] == "high"


def test_output_config_effort_takes_precedence_over_legacy_top_level():
    """Le champ documente prime sur le champ top-level historique."""
    body = {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "hello"}],
        "effort": "low",
        "output_config": {"effort": "max"},
    }
    assert pm.anthropic_to_openai(body, "deepseek-v4-flash")["reasoning_effort"] == "max"


def test_adaptive_without_budget_takes_the_model_maximum():
    """``thinking`` sans niveau = le MAXIMUM DU MODÈLE (décision produit).

    Le client demande du raisonnement sans préciser d'effort : on lui donne le
    mieux que le modèle cible sache faire, borné par son plafond configuré.
    """
    for model, expected in [
        ("muse-spark-1.3-contributor", "xhigh"),
        ("deepseek-v4-flash", "max"),
        ("glm-5-air", "high"),
    ]:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": "hello"}],
            "thinking": {"type": "adaptive"},
        }
        assert pm.anthropic_to_openai(body, model)["reasoning_effort"] == expected, model


# ── [Hotfix A15] P6 n'envoie plus un nom de champ OpenAI a Anthropic ─────────


def test_responses_effort_becomes_output_config_not_reasoning_effort():
    """Un body Responses routé vers un upstream Anthropic doit porter
    ``output_config.effort`` — pas ``reasoning_effort`` (nom de champ OpenAI)."""
    req = {
        "model": "claude-sonnet-4",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "reasoning": {"effort": "medium"},
    }
    result = pm.openai_responses_to_anthropic(req)
    assert "reasoning_effort" not in result
    assert result["output_config"] == {"effort": "medium"}


def test_budget_tokens_minimum_is_respected():
    """La spec impose ``budget_tokens >= 1024`` : nous n'en emettons plus aucun,
    donc la contrainte ne peut plus etre violee par un petit budget client."""
    body = {
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "hello"}],
        "thinking": {"type": "enabled", "budget_tokens": 512},
    }
    result = pm.openai_to_anthropic_request(body)
    assert "budget_tokens" not in result.get("thinking", {})


def test_reasoning_effort_none_no_thinking():
    oai = {
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning_effort": "none",
    }
    result = pm.openai_to_anthropic_request(oai)
    assert "thinking" not in result
