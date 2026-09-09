"""[Lot H4] Mapping effort ↔ reasoning : tous niveaux × familles de modèles,
plus le sens inverse (reasoning_effort OpenAI → thinking Anthropic).

Clamp config-driven : ``_effort_to_reasoning`` délègue à
``config.effort_caps.clamp_effort`` (section ``thinking`` de ``config.yaml``,
lue en live). Ces tests valident le comportement avec la config réelle du
repo (spark → xhigh, glm-5 → high, deepseek-v4 → max, mimo-v2.5 → max,
défaut → high) —
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
        (0, "xhigh", "muse-spark-1.3-contributor", "xhigh"),
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
    assert back["thinking"] == {"type": "enabled", "budget_tokens": 16000}
    again = pm.anthropic_to_openai({**back, "model": "glm-5-air"}, "glm-5-air")
    assert again["reasoning_effort"] == "high"


@pytest.mark.parametrize(
    "effort,budget",
    [("low", 4096), ("medium", 10000), ("high", 16000), ("xhigh", 16000), ("max", 16000)],
)
def test_reasoning_effort_to_anthropic_thinking(effort, budget):
    oai = {
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning_effort": effort,
    }
    result = pm.openai_to_anthropic_request(oai)
    assert result["thinking"] == {"type": "enabled", "budget_tokens": budget}


def test_reasoning_effort_absent_no_thinking():
    oai = {
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "hello"}],
    }
    result = pm.openai_to_anthropic_request(oai)
    assert "thinking" not in result


def test_reasoning_effort_none_no_thinking():
    oai = {
        "model": "claude-sonnet-4",
        "messages": [{"role": "user", "content": "hello"}],
        "reasoning_effort": "none",
    }
    result = pm.openai_to_anthropic_request(oai)
    assert "thinking" not in result
