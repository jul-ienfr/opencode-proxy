"""[Lot H4] Mapping effort ↔ reasoning : tous niveaux × familles de modèles,
plus le sens inverse (reasoning_effort OpenAI → thinking Anthropic)."""

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
        # deepseek-v4 : xhigh/max → max, tout le reste → high
        ("xhigh", "deepseek-v4-flash", "max"),
        ("max", "deepseek-v4-pro", "max"),
        ("high", "deepseek-v4-flash", "high"),
        ("medium", "deepseek-v4-flash", "high"),
        ("low", "deepseek-v4-flash", "high"),
        ("", "deepseek-v4-pro", "high"),
        # défaut (mimo, etc.) : xhigh/max/high → high, medium → medium, reste → low
        ("xhigh", "mimo-v2.5", "high"),
        ("max", "mimo-v2-pro", "high"),
        ("high", "mimo-v2.5", "high"),
        ("medium", "mimo-v2.5", "medium"),
        ("low", "mimo-v2.5", "low"),
        ("", "mimo-v2.5", "low"),
    ],
)
def test_effort_to_reasoning_all_levels(effort, model, expected):
    assert pm._effort_to_reasoning(effort, model) == expected


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
