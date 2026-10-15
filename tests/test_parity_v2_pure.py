"""Parité v2 pure : output fallback/cap, fit thinking, matrice npm."""
import config.limits as lim
from config.effort_policy import MIN_THINKING_BUDGET, fit_thinking_budget
from config.npm_routing import endpoint_for_npm, is_routable_npm


def test_output_fallback_and_cap():
    assert lim.OUTPUT_TOKEN_FALLBACK == 32000
    assert lim.OUTPUT_TOKEN_MAX == 256000
    assert lim.OUTPUT_TOKEN_MIN == 1024
    assert lim.output_limit_with_fallback("modele-inconnu-zzz") == 32000
    assert lim.clamp_output_token_limit(500000, "modele-inconnu-zzz") == 256000


def test_fit_thinking_budget_halves():
    assert fit_thinking_budget(10000, 16000) == 8000
    assert fit_thinking_budget(1000, 16000) == 1000
    assert fit_thinking_budget(100, None) == 100
    assert fit_thinking_budget(0, 16000) == 0
    assert MIN_THINKING_BUDGET == 1024


def test_npm_matrix_official():
    assert endpoint_for_npm("@ai-sdk/anthropic") == "/v1/messages"
    assert endpoint_for_npm("@ai-sdk/openai") == "/v1/responses"
    assert endpoint_for_npm("@ai-sdk/openai-compatible") == "/v1/chat/completions"
    assert endpoint_for_npm("@ai-sdk/google") is None
    assert endpoint_for_npm("@ai-sdk/mistral") is None
    assert is_routable_npm("@ai-sdk/google") is False
