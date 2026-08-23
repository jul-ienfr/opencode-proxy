"""
Unit tests for opencode-proxy.

Tests protocol conversions, circuit breaker, rate limiter,
token estimation, and route matching.
"""

import json
import time
import asyncio
import pytest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import opencode as _opencode_mod
from opencode import (
    anthropic_to_openai,
    anthropic_to_openai_response,
    openai_to_anthropic,
    openai_to_anthropic_request,
    openai_chat_to_responses,
    openai_responses_to_anthropic,
    anthropic_to_openai_responses,
    _chat_to_responses_request,
    _estimate_tokens,
    _route_for,
    _CircuitBreaker,
    _CB_FAILURE_THRESHOLD,
    _CB_RECOVERY_TIMEOUT,
    _Bucket,
    RATE_LIMIT_RPS,
    RATE_LIMIT_BURST,
)
from config.settings import _resolve_protocol, KNOWN_PROTOCOLS


# ── Protocol Conversions ────────────────────────────────────────


class TestAnthropicToOpenAI:
    """Test Anthropic Messages → OpenAI Chat Completions conversion."""

    def test_simple_text_message(self):
        body = {
            "model": "claude-3-opus",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 1024,
        }
        result = anthropic_to_openai(body, "claude-3-opus")
        assert result["model"] == "claude-3-opus"
        # cache_control is added to the last user message for prefix caching
        assert result["messages"] == [
            {"role": "user", "content": "Hello", "cache_control": {"type": "ephemeral"}}
        ]
        assert result["max_tokens"] == 1024
        assert result["stream"] is False

    def test_system_prompt(self):
        body = {
            "model": "claude-3-opus",
            "system": "You are helpful.",
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_to_openai(body, "claude-3-opus")
        assert result["messages"][0]["role"] == "system"
        assert result["messages"][0]["content"] == "You are helpful."
        assert result["messages"][1]["role"] == "user"

    def test_system_prompt_list(self):
        body = {
            "model": "claude-3-opus",
            "system": [{"type": "text", "text": "System message"}],
            "messages": [{"role": "user", "content": "Hi"}],
        }
        result = anthropic_to_openai(body, "claude-3-opus")
        assert result["messages"][0]["role"] == "system"
        assert result["messages"][0]["content"] == "System message"

    def test_tool_calls_conversion(self):
        body = {
            "model": "claude-3-opus",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Let me search."},
                        {
                            "type": "tool_use",
                            "id": "toolu_123",
                            "name": "web_search",
                            "input": {"query": "test"},
                        },
                    ],
                }
            ],
        }
        result = anthropic_to_openai(body, "claude-3-opus")
        msg = result["messages"][0]
        assert msg["role"] == "assistant"
        assert len(msg["tool_calls"]) == 1
        assert msg["tool_calls"][0]["function"]["name"] == "web_search"
        assert json.loads(msg["tool_calls"][0]["function"]["arguments"]) == {"query": "test"}

    def test_tool_results_conversion(self):
        body = {
            "model": "claude-3-opus",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_123",
                            "content": "result text",
                        },
                    ],
                }
            ],
        }
        result = anthropic_to_openai(body, "claude-3-opus")
        msg = result["messages"][0]
        assert msg["role"] == "tool"
        assert msg["tool_call_id"] == "toolu_123"
        assert msg["content"] == "result text"

    def test_thinking_blocks(self):
        body = {
            "model": "claude-3-opus",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "Let me think..."},
                        {"type": "text", "text": "Here's my answer."},
                    ],
                }
            ],
        }
        result = anthropic_to_openai(body, "claude-3-opus")
        msg = result["messages"][0]
        assert msg.get("reasoning_content") == "Let me think..."
        assert msg["content"] == "Here's my answer."

    def test_parameters_passed_through(self):
        body = {
            "model": "claude-3-opus",
            "messages": [{"role": "user", "content": "Hi"}],
            "temperature": 0.7,
            "top_p": 0.9,
            "stop_sequences": ["END"],
        }
        result = anthropic_to_openai(body, "claude-3-opus")
        assert result["temperature"] == 0.7
        assert result["top_p"] == 0.9
        assert result["stop"] == ["END"]

    def test_tools_conversion(self):
        body = {
            "model": "claude-3-opus",
            "messages": [{"role": "user", "content": "Hi"}],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Get weather",
                    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
                }
            ],
            "tool_choice": {"type": "any"},
        }
        result = anthropic_to_openai(body, "claude-3-opus")
        assert len(result["tools"]) == 1
        tool = result["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "get_weather"
        assert tool["function"]["parameters"] == {
            "type": "object",
            "properties": {"city": {"type": "string"}},
        }
        assert result["tool_choice"] == "required"


class TestOpenAIToAnthropic:
    """Test OpenAI Chat Completions → Anthropic Messages conversion."""

    def test_simple_response(self):
        resp = {
            "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_to_anthropic(resp, "claude-3-opus")
        assert result["type"] == "message"
        assert result["role"] == "assistant"
        assert result["content"] == [{"type": "text", "text": "Hello!"}]
        assert result["stop_reason"] == "end_turn"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5

    def test_tool_calls_response(self):
        resp = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "function": {"name": "search", "arguments": '{"q":"test"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_to_anthropic(resp, "claude-3-opus")
        assert result["stop_reason"] == "tool_use"
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "tool_use"
        assert result["content"][0]["name"] == "search"
        assert result["content"][0]["input"] == {"q": "test"}

    def test_reasoning_content(self):
        resp = {
            "choices": [
                {
                    "message": {
                        "content": "Answer",
                        "reasoning_content": "Thinking...",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_to_anthropic(resp, "claude-3-opus")
        assert result["content"][0]["type"] == "thinking"
        assert result["content"][0]["thinking"] == "Thinking..."
        assert result["content"][1]["type"] == "text"
        assert result["content"][1]["text"] == "Answer"

    def test_max_tokens_finish(self):
        resp = {
            "choices": [{"message": {"content": "Truncated"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 100},
        }
        result = openai_to_anthropic(resp, "claude-3-opus")
        assert result["stop_reason"] == "max_tokens"

    def test_cache_tokens(self):
        resp = {
            "choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 50},
            },
        }
        result = openai_to_anthropic(resp, "claude-3-opus")
        assert result["usage"]["cache_read_input_tokens"] == 50


class TestOpenAIChatToResponses:
    """Test direct OpenAI Chat Completions → Responses API conversion."""

    def test_simple_response(self):
        resp = {
            "choices": [{"message": {"content": "Hello!"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_chat_to_responses(resp, "gpt-4o")
        assert result["object"] == "response"
        assert result["status"] == "completed"
        assert result["model"] == "gpt-4o"
        assert len(result["output"]) == 1
        assert result["output"][0]["type"] == "message"
        assert result["output"][0]["content"][0]["text"] == "Hello!"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5

    def test_tool_calls(self):
        resp = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_abc",
                                "function": {
                                    "name": "run_code",
                                    "arguments": '{"code":"print(1)"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_chat_to_responses(resp, "gpt-4o")
        assert result["status"] == "completed"
        assert len(result["output"]) == 1
        assert result["output"][0]["type"] == "function_call"
        assert result["output"][0]["name"] == "run_code"
        assert result["output"][0]["arguments"] == '{"code":"print(1)"}'

    def test_reasoning_content(self):
        resp = {
            "choices": [
                {
                    "message": {
                        "content": "Answer",
                        "reasoning_content": "Thinking...",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_chat_to_responses(resp, "gpt-4o")
        assert result["output"][0]["type"] == "reasoning"
        assert result["output"][0]["summary"][0]["text"] == "Thinking..."
        assert result["output"][1]["type"] == "message"

    def test_length_finish(self):
        resp = {
            "choices": [{"message": {"content": "Truncated"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 100},
        }
        result = openai_chat_to_responses(resp, "gpt-4o")
        assert result["status"] == "incomplete"

    def test_id_format(self):
        resp = {
            "choices": [{"message": {"content": "Hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        result = openai_chat_to_responses(resp, "gpt-4o")
        assert result["id"].startswith("resp_")


# ── Token Estimation ────────────────────────────────────────────


class TestEstimateTokens:
    """Test token estimation function."""

    def test_empty_string(self):
        assert _estimate_tokens("") >= 1

    def test_short_text_uses_char_estimate(self):
        # Short text (< 200 chars) should use len//3
        text = "Hello world"
        result = _estimate_tokens(text)
        assert result == max(1, len(text) // 3)

    def test_long_text_uses_tiktoken(self):
        # Long text (>= 200 chars) should use tiktoken if available
        text = "a" * 300
        result = _estimate_tokens(text)
        assert result > 0
        # tiktoken counts "a" repeated as individual tokens
        assert result > 10

    def test_minimum_tokens(self):
        assert _estimate_tokens("x") >= 1


# ── Circuit Breaker ─────────────────────────────────────────────


class TestCircuitBreaker:
    """Test circuit breaker state machine."""

    def test_initial_state(self):
        cb = _CircuitBreaker()
        assert cb.state == "closed"
        assert cb.should_allow() is True

    def test_failure_counting(self):
        cb = _CircuitBreaker()
        for _ in range(_CB_FAILURE_THRESHOLD - 1):
            cb.record_failure()
            assert cb.state == "closed"
            assert cb.should_allow() is True

    def test_trips_open_after_threshold(self):
        cb = _CircuitBreaker()
        for _ in range(_CB_FAILURE_THRESHOLD):
            cb.record_failure()
        assert cb.state == "open"
        assert cb.should_allow() is False

    def test_success_resets(self):
        cb = _CircuitBreaker()
        for _ in range(_CB_FAILURE_THRESHOLD - 1):
            cb.record_failure()
        cb.record_success()
        assert cb.state == "closed"
        assert cb.failures == 0
        assert cb.should_allow() is True

    def test_recovery_timeout(self):
        cb = _CircuitBreaker()
        for _ in range(_CB_FAILURE_THRESHOLD):
            cb.record_failure()
        assert cb.state == "open"
        # Simulate time passing
        cb.opened_at = time.monotonic() - _CB_RECOVERY_TIMEOUT - 1
        assert cb.should_allow() is True
        assert cb.state == "half_open"

    def test_half_open_success_closes(self):
        cb = _CircuitBreaker()
        cb.state = "half_open"
        cb.record_success()
        assert cb.state == "closed"

    def test_half_open_failure_reopens(self):
        cb = _CircuitBreaker()
        cb.state = "half_open"
        cb.record_failure()
        assert cb.state == "open"


# ── Rate Limiter ────────────────────────────────────────────────


class TestBucket:
    """Test token bucket implementation."""

    @pytest.mark.asyncio
    async def test_initial_tokens(self):
        bucket = _Bucket(rate=10.0, burst=10.0)
        allowed, _ = await bucket.consume()
        assert allowed is True

    @pytest.mark.asyncio
    async def test_consume_until_empty(self):
        bucket = _Bucket(rate=1.0, burst=2.0)
        assert (await bucket.consume())[0] is True
        assert (await bucket.consume())[0] is True
        allowed, retry_after = await bucket.consume()
        assert allowed is False
        assert retry_after > 0

    @pytest.mark.asyncio
    async def test_token_refill(self):
        bucket = _Bucket(rate=100.0, burst=1.0)
        assert (await bucket.consume())[0] is True
        allowed, _ = await bucket.consume()
        assert allowed is False
        # Wait for refill
        await asyncio.sleep(0.02)
        allowed, _ = await bucket.consume()
        assert allowed is True


# ── Route Matching ──────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _disable_mapping_off(monkeypatch):
    """Force DISABLE_MAPPING=False and clear custom routes for route tests."""
    monkeypatch.setattr(_opencode_mod, "DISABLE_MAPPING", False)
    # Rebuild ROUTES without custom route overrides (use config.load_routes with empty custom)
    from config import settings as _cfg_settings

    monkeypatch.setattr(_cfg_settings, "CUSTOM_ROUTES", {})
    _clean_routes = _cfg_settings.load_routes()
    monkeypatch.setattr(_opencode_mod, "ROUTES", _clean_routes)
    monkeypatch.setattr(_opencode_mod, "CUSTOM_ROUTES", {})
    # SORTED_* are computed at import from the real custom_routes.json —
    # rebuild after replacing the dicts, or _route_for matches the stale
    # production overrides ([43]).
    monkeypatch.setattr(
        _cfg_settings, "SORTED_ROUTES", _cfg_settings._sort_routes_by_match(_clean_routes)
    )
    monkeypatch.setattr(_cfg_settings, "SORTED_CUSTOM_ROUTES", [])
    # Prevent maybe_reload_custom_routes (called at top of _route_for) from
    # re-reading config.yaml/custom_routes.json on disk and re-introducing
    # the production custom_routes (e.g. kimik26: kimi-k2.6 → muse-spark)
    # between tests — especially when the suite runs with other modules that
    # may have touched the file mtime. Both references are patched.
    monkeypatch.setattr(_cfg_settings, "maybe_reload_custom_routes", lambda: None)
    monkeypatch.setattr(_opencode_mod, "maybe_reload_custom_routes", lambda: None)


class TestRouteFor:
    """Test model route matching."""

    def test_direct_model_match(self):
        route = _route_for("kimi-k2.6")
        assert route is not None
        assert route["model"] == "kimi-k2.6"

    def test_alias_match_opus(self):
        route = _route_for("claude-opus-4-20250514")
        assert route is not None
        # Should map to the configured OPUS_MAP_MODEL
        assert "model" in route

    def test_alias_match_sonnet(self):
        route = _route_for("claude-sonnet-4-20250514")
        assert route is not None
        assert "model" in route

    def test_alias_match_haiku(self):
        route = _route_for("claude-haiku-4-5-20251001")
        assert route is not None
        assert "model" in route

    def test_unknown_model_returns_none(self):
        route = _route_for("totally-unknown-model-xyz")
        assert route is None

    def test_empty_model_returns_none(self):
        route = _route_for("")
        assert route is None

    def test_case_insensitive(self):
        route = _route_for("KIMI-K2.6")
        assert route is not None
        assert route["model"] == "kimi-k2.6"


# ── Integration: Full Round-Trip ────────────────────────────────


class TestRoundTrip:
    """Test that conversions are lossless for simple cases."""

    def test_anthropic_to_openai_to_anthropic(self):
        """Simple text should survive the round-trip."""
        original = {
            "choices": [{"message": {"content": "Hello world"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        # OpenAI → Anthropic
        anthro = openai_to_anthropic(original, "test-model")
        assert anthro["content"][0]["text"] == "Hello world"

        # Anthropic → OpenAI (response format)
        oai_resp = anthropic_to_openai_response(anthro, "test-model")
        assert oai_resp["choices"][0]["message"]["content"] == "Hello world"

    def test_tool_call_round_trip(self):
        """Tool calls should survive the round-trip."""
        original = {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "function": {"name": "search", "arguments": '{"q":"test"}'},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        }
        anthro = openai_to_anthropic(original, "test-model")
        assert anthro["content"][0]["type"] == "tool_use"
        assert anthro["content"][0]["name"] == "search"

        oai_resp = anthropic_to_openai_response(anthro, "test-model")
        tc = oai_resp["choices"][0]["message"]["tool_calls"][0]
        assert tc["function"]["name"] == "search"
        assert json.loads(tc["function"]["arguments"]) == {"q": "test"}


# ── openai_to_anthropic_request ──────────────────────────────────


class TestOpenAIToAnthropicRequest:
    """Test OpenAI Chat Completions request → Anthropic Messages request conversion."""

    def test_simple_user_message(self):
        body = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello"}],
        }
        result = openai_to_anthropic_request(body)
        assert result["model"] == "gpt-4o"
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][0]["content"][0]["type"] == "text"
        assert result["messages"][0]["content"][0]["text"] == "Hello"

    def test_system_message(self):
        body = {
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello"},
            ],
        }
        result = openai_to_anthropic_request(body)
        assert result["system"] == "You are a helpful assistant."
        # System message should not appear in messages list
        assert all(m["role"] != "system" for m in result["messages"])

    def test_tool_calls_conversion(self):
        body = {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": "Search for cats"},
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_123",
                            "function": {"name": "search", "arguments": '{"q":"cats"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_123", "content": "Found cats"},
            ],
        }
        result = openai_to_anthropic_request(body)
        # Assistant message should have tool_use block
        assistant = result["messages"][1]
        assert assistant["role"] == "assistant"
        assert assistant["content"][0]["type"] == "tool_use"
        assert assistant["content"][0]["name"] == "search"
        # User message should have tool_result
        user = result["messages"][2]
        assert user["content"][0]["type"] == "tool_result"
        assert user["content"][0]["tool_use_id"] == "call_123"


# ── openai_responses_to_anthropic ────────────────────────────────


class TestOpenAIResponsesToAnthropic:
    """Test OpenAI Responses API request → Anthropic Messages request conversion."""

    def test_simple_input(self):
        body = {
            "model": "gpt-4o",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "Hello"}]},
            ],
        }
        result = openai_responses_to_anthropic(body)
        assert result["model"] == "gpt-4o"
        assert result["messages"][0]["role"] == "user"
        assert result["messages"][0]["content"][0]["type"] == "text"
        assert result["messages"][0]["content"][0]["text"] == "Hello"

    def test_function_call_conversion(self):
        body = {
            "model": "gpt-4o",
            "input": [
                {
                    "type": "function_call",
                    "id": "fc_123",
                    "name": "search",
                    "arguments": '{"q":"test"}',
                },
                {"type": "function_call_output", "call_id": "fc_123", "output": "Found results"},
            ],
        }
        result = openai_responses_to_anthropic(body)
        # function_call → assistant tool_use
        assistant = result["messages"][0]
        assert assistant["role"] == "assistant"
        assert assistant["content"][0]["type"] == "tool_use"
        assert assistant["content"][0]["name"] == "search"
        # function_call_output → user tool_result
        user = result["messages"][1]
        assert user["role"] == "user"
        assert user["content"][0]["type"] == "tool_result"
        assert user["content"][0]["tool_use_id"] == "fc_123"

    def test_system_message(self):
        body = {
            "model": "gpt-4o",
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": "Be helpful"}]},
                {"role": "user", "content": [{"type": "input_text", "text": "Hi"}]},
            ],
        }
        result = openai_responses_to_anthropic(body)
        assert result["system"] == "Be helpful"
        assert all(m["role"] != "system" for m in result["messages"])


# ── anthropic_to_openai_responses ────────────────────────────────


class TestAnthropicToOpenAIResponses:
    """Test Anthropic Messages response → OpenAI Responses API response conversion."""

    def test_text_response(self):
        anthro = {
            "content": [{"type": "text", "text": "Hello world"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = anthropic_to_openai_responses(anthro, "test-model")
        assert result["object"] == "response"
        assert result["status"] == "completed"
        assert result["model"] == "test-model"
        msg = result["output"][0]
        assert msg["type"] == "message"
        assert msg["content"][0]["text"] == "Hello world"

    def test_tool_use_response(self):
        anthro = {
            "content": [
                {"type": "tool_use", "id": "toolu_123", "name": "search", "input": {"q": "test"}}
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        result = anthropic_to_openai_responses(anthro, "test-model")
        assert result["status"] == "completed"
        fc = result["output"][0]
        assert fc["type"] == "function_call"
        assert fc["name"] == "search"
        assert json.loads(fc["arguments"]) == {"q": "test"}

    def test_max_tokens_status(self):
        anthro = {
            "content": [{"type": "text", "text": "Partial answer"}],
            "stop_reason": "max_tokens",
            "usage": {"input_tokens": 10, "output_tokens": 100},
        }
        result = anthropic_to_openai_responses(anthro, "test-model")
        assert result["status"] == "incomplete"

    def test_usage_mapping(self):
        anthro = {
            "content": [{"type": "text", "text": "Hi"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 20, "output_tokens": 10, "cache_read_input_tokens": 5},
        }
        result = anthropic_to_openai_responses(anthro, "test-model")
        usage = result["usage"]
        assert usage["input_tokens"] == 20
        assert usage["output_tokens"] == 10
        assert usage["total_tokens"] == 30
        assert usage["output_tokens_details"]["cached_tokens"] == 5


# ── Protocol Resolution ──────────────────────────────────────────


class TestResolveProtocol:
    """Test _resolve_protocol() maps model IDs to correct protocols."""

    def test_glm_is_openai(self):
        assert _resolve_protocol("glm-5.2") == "openai"

    def test_glm51_is_openai(self):
        assert _resolve_protocol("glm-5.1") == "openai"

    def test_kimi_is_openai(self):
        assert _resolve_protocol("kimi-k2.7") == "openai"

    def test_kimi25_is_openai(self):
        assert _resolve_protocol("kimi-k2.5") == "openai"

    def test_deepseek_is_openai(self):
        assert _resolve_protocol("deepseek-v4-pro") == "openai"

    def test_deepseek_flash_is_openai(self):
        assert _resolve_protocol("deepseek-v4-flash") == "openai"

    def test_mimo_is_openai(self):
        assert _resolve_protocol("mimo-v2.5") == "openai"

    def test_mimo_pro_is_openai(self):
        assert _resolve_protocol("mimo-v2-pro") == "openai"

    def test_minimax_is_anthropic(self):
        assert _resolve_protocol("minimax-m2.5") == "anthropic"

    def test_minimax_m3_is_anthropic(self):
        assert _resolve_protocol("minimax-m3") == "anthropic"

    def test_qwen_is_anthropic(self):
        assert _resolve_protocol("qwen3.7-plus") == "anthropic"

    def test_qwen_max_is_anthropic(self):
        assert _resolve_protocol("qwen3.7-max") == "anthropic"

    def test_unknown_falls_back_to_openai(self):
        assert _resolve_protocol("totally-unknown-xyz") == "openai"

    def test_single_word_model(self):
        # Model without hyphen: prefix = full name
        assert _resolve_protocol("gemma3") == "openai"

    def test_known_protocols_has_all_families(self):
        """Verify all expected families are in the registry."""
        expected_openai = {"glm", "kimi", "deepseek", "mimo"}
        expected_anthropic = {"minimax", "qwen"}
        assert expected_openai.issubset(set(k for k, v in KNOWN_PROTOCOLS.items() if v == "openai"))
        assert expected_anthropic.issubset(
            set(k for k, v in KNOWN_PROTOCOLS.items() if v == "anthropic")
        )


# ── Chat-to-Responses reasoning forwarding ──────────────────────


class TestChatToResponsesRequest:
    """Test _chat_to_responses_request() forwards reasoning parameters."""

    def test_reasoning_effort_forwarded(self):
        """reasoning_effort must become reasoning: {summary: auto, effort: ...} in Responses API format."""
        chat = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "Think about 2+2"}],
            "reasoning_effort": "high",
        }
        result = _chat_to_responses_request(chat)
        assert "reasoning" in result
        assert result["reasoning"] == {"summary": "auto", "effort": "high"}

    def test_reasoning_effort_medium(self):
        chat = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "test"}],
            "reasoning_effort": "medium",
        }
        result = _chat_to_responses_request(chat)
        assert result["reasoning"] == {"summary": "auto", "effort": "medium"}

    def test_reasoning_effort_low(self):
        chat = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "test"}],
            "reasoning_effort": "low",
        }
        result = _chat_to_responses_request(chat)
        assert result["reasoning"] == {"summary": "auto", "effort": "low"}

    def test_reasoning_object_forwarded(self):
        """If reasoning is already a dict (Responses API format), pass it through."""
        reasoning = {"summary": "auto", "effort": "high"}
        chat = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "test"}],
            "reasoning": reasoning,
        }
        result = _chat_to_responses_request(chat)
        assert result["reasoning"] == reasoning

    def test_no_reasoning_when_absent(self):
        """No reasoning param → no reasoning in output."""
        chat = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "test"}],
        }
        result = _chat_to_responses_request(chat)
        assert "reasoning" not in result

    def test_reasoning_effort_takes_precedence(self):
        """If both reasoning_effort and reasoning are present, reasoning_effort wins."""
        chat = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "test"}],
            "reasoning_effort": "low",
            "reasoning": {"summary": "auto", "effort": "high"},
        }
        result = _chat_to_responses_request(chat)
        assert result["reasoning"] == {"summary": "auto", "effort": "low"}

    def test_temperature_and_top_p_preserved(self):
        """temperature and top_p must still be forwarded."""
        chat = {
            "model": "muse-spark-1.2-contributor",
            "messages": [{"role": "user", "content": "test"}],
            "temperature": 0.7,
            "top_p": 0.9,
            "reasoning_effort": "high",
        }
        result = _chat_to_responses_request(chat)
        assert result["temperature"] == 0.7
        assert result["top_p"] == 0.9
        assert result["reasoning"] == {"summary": "auto", "effort": "high"}
