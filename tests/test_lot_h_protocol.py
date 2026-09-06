"""[Lot H] Tests conversions protocoles — H1, H2, H3, H5, H6, H7."""

import hashlib

import pytest

import protocol_mapping as pm


class TestH1CacheCreationTokens:
    def test_from_prompt_tokens_details(self):
        resp = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "prompt_tokens_details": {"cache_creation_tokens": 42, "cached_tokens": 10},
            },
        }
        out = pm.openai_to_anthropic(resp, "test-model")
        assert out["usage"]["cache_creation_input_tokens"] == 42
        assert out["usage"]["cache_read_input_tokens"] == 10

    def test_from_top_level_field(self):
        resp = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "cache_creation_input_tokens": 77,
            },
        }
        out = pm.openai_to_anthropic(resp, "test-model")
        assert out["usage"]["cache_creation_input_tokens"] == 77

    def test_from_prompt_cache_miss_tokens(self):
        resp = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "prompt_cache_miss_tokens": 33,
            },
        }
        out = pm.openai_to_anthropic(resp, "test-model")
        assert out["usage"]["cache_creation_input_tokens"] == 33

    def test_fallback_zero(self):
        resp = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        out = pm.openai_to_anthropic(resp, "test-model")
        assert out["usage"]["cache_creation_input_tokens"] == 0


class TestH2ReasoningPreserved:
    def test_reasoning_content_becomes_thinking_block(self):
        oai = {
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "42", "reasoning_content": "6*7=42"},
                {"role": "user", "content": "next"},
            ],
        }
        result = pm.openai_to_anthropic_request(oai)
        asst_blocks = result["messages"][1]["content"]
        thinking = [b for b in asst_blocks if b.get("type") == "thinking"]
        assert len(thinking) == 1
        assert thinking[0]["thinking"] == "6*7=42"
        assert thinking[0]["signature"] == pm._local_signature("6*7=42")

    def test_reasoning_field_also_converted(self):
        oai = {
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "assistant", "content": "ok", "reasoning": "internal thought"},
            ],
        }
        result = pm.openai_to_anthropic_request(oai)
        asst_blocks = result["messages"][0]["content"]
        thinking = [b for b in asst_blocks if b.get("type") == "thinking"]
        assert len(thinking) == 1
        assert thinking[0]["thinking"] == "internal thought"

    def test_empty_reasoning_not_converted(self):
        oai = {
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "assistant", "content": "ok", "reasoning_content": "   "},
            ],
        }
        result = pm.openai_to_anthropic_request(oai)
        asst_blocks = result["messages"][0]["content"]
        thinking = [b for b in asst_blocks if b.get("type") == "thinking"]
        assert len(thinking) == 0

    def test_user_reasoning_not_converted(self):
        oai = {
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "hello", "reasoning_content": "should not appear"},
            ],
        }
        result = pm.openai_to_anthropic_request(oai)
        user_blocks = result["messages"][0]["content"]
        thinking = [b for b in user_blocks if b.get("type") == "thinking"]
        assert len(thinking) == 0


class TestH3RedactedThinkingCache:
    def setup_method(self):
        pm._redacted_thinking_cache.clear()

    def test_redacted_thinking_stored_in_cache(self):
        data_blob = "ABCDEF" * 20
        body = {
            "model": "test",
            "messages": [
                {
                    "role": "assistant",
                    "content": [{"type": "redacted_thinking", "data": data_blob}],
                }
            ],
        }
        pm.anthropic_to_openai(body, "glm-5-air")
        expected_key = hashlib.sha256(data_blob.encode("utf-8", "ignore")).hexdigest()
        assert expected_key in pm._redacted_thinking_cache
        assert pm._redacted_thinking_cache[expected_key]["data"] == data_blob

    def test_cache_lru_bounded(self):
        pm._redacted_thinking_cache.clear()
        for i in range(pm._REDACTED_THINKING_CACHE_MAX + 10):
            data = f"blob_{i}_" + "A" * 64
            body = {
                "model": "test",
                "messages": [
                    {"role": "assistant", "content": [{"type": "redacted_thinking", "data": data}]}
                ],
            }
            pm.anthropic_to_openai(body, "glm-5-air")
        assert len(pm._redacted_thinking_cache) <= pm._REDACTED_THINKING_CACHE_MAX


class TestH5CacheStreamPropagation:
    def test_openai_to_anthropic_cache_read_mapped(self):
        resp = {
            "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 200,
                "completion_tokens": 80,
                "prompt_tokens_details": {"cached_tokens": 150},
            },
        }
        out = pm.openai_to_anthropic(resp, "test")
        assert out["usage"]["cache_read_input_tokens"] == 150

    def test_anthropic_to_openai_response_cache_read_mapped(self):
        anthro = {
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 300,
                "output_tokens": 60,
                "cache_read_input_tokens": 250,
            },
        }
        out = pm.anthropic_to_openai_response(anthro, "test")
        assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 250

    def test_anthropic_to_openai_response_no_cache_no_details(self):
        anthro = {
            "content": [{"type": "text", "text": "hello"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 20},
        }
        out = pm.anthropic_to_openai_response(anthro, "test")
        assert "prompt_tokens_details" not in out["usage"]


class TestH6ToolsRoundtrip:
    def test_tool_use_to_tool_calls(self):
        body = {
            "model": "test",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_abc123",
                            "name": "get_weather",
                            "input": {"city": "Paris"},
                        }
                    ],
                }
            ],
        }
        out = pm.anthropic_to_openai(body, "glm-5-air")
        msg = out["messages"][-1]
        assert msg["tool_calls"][0]["id"] == "toolu_abc123"
        assert msg["tool_calls"][0]["function"]["name"] == "get_weather"
        assert '"city"' in msg["tool_calls"][0]["function"]["arguments"]

    def test_tool_result_to_role_tool(self):
        body = {
            "model": "test",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_abc123",
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
                            "tool_use_id": "toolu_abc123",
                            "content": "sunny 22C",
                        }
                    ],
                },
            ],
        }
        out = pm.anthropic_to_openai(body, "glm-5-air")
        tool_msgs = [m for m in out["messages"] if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "toolu_abc123"
        assert tool_msgs[0]["content"] == "sunny 22C"

    def test_openai_tool_calls_to_anthropic_tool_use(self):
        oai = {
            "model": "claude-sonnet-4",
            "messages": [
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_xyz",
                            "type": "function",
                            "function": {"name": "search", "arguments": '{"q": "test"}'},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "call_xyz", "content": "result here"},
                {"role": "user", "content": "thanks"},
            ],
        }
        result = pm.openai_to_anthropic_request(oai)
        asst_blocks = result["messages"][0]["content"]
        tool_use = [b for b in asst_blocks if b.get("type") == "tool_use"]
        assert len(tool_use) == 1
        assert tool_use[0]["id"] == "call_xyz"
        assert tool_use[0]["name"] == "search"
        assert tool_use[0]["input"] == {"q": "test"}

        user_blocks = result["messages"][1]["content"]
        tool_result = [b for b in user_blocks if b.get("type") == "tool_result"]
        assert len(tool_result) == 1
        assert tool_result[0]["tool_use_id"] == "call_xyz"
        assert tool_result[0]["content"] == "result here"

    def test_orphan_tool_messages_filtered(self):
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "tool", "tool_call_id": "nonexistent", "content": "orphan"},
            {"role": "assistant", "content": "ok", "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "valid"},
        ]
        filtered = pm._drop_orphan_tool_messages(msgs)
        tool_msgs = [m for m in filtered if m.get("role") == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["tool_call_id"] == "call_1"


class TestH7ResponsesRoundtrip:
    def test_anthropic_to_responses_to_anthropic_tools(self):
        anthro = {
            "content": [
                {"type": "thinking", "thinking": "let me think"},
                {"type": "text", "text": "here is the answer"},
                {
                    "type": "tool_use",
                    "id": "toolu_rt1",
                    "name": "calculator",
                    "input": {"expr": "2+2"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {
                "input_tokens": 50,
                "output_tokens": 30,
                "cache_read_input_tokens": 20,
            },
        }
        responses = pm.anthropic_to_openai_responses(anthro, "test-model")

        reasoning_items = [i for i in responses["output"] if i.get("type") == "reasoning"]
        assert len(reasoning_items) == 1
        assert reasoning_items[0]["summary"][0]["text"] == "let me think"

        msg_items = [i for i in responses["output"] if i.get("type") == "message"]
        assert len(msg_items) == 1
        assert msg_items[0]["content"][0]["text"] == "here is the answer"

        fn_items = [i for i in responses["output"] if i.get("type") == "function_call"]
        assert len(fn_items) == 1
        assert fn_items[0]["call_id"] == "toolu_rt1"
        assert fn_items[0]["name"] == "calculator"

        back = pm._responses_to_anthropic_response(responses, "test-model")
        thinking_blocks = [b for b in back["content"] if b.get("type") == "thinking"]
        assert len(thinking_blocks) == 1
        assert thinking_blocks[0]["thinking"] == "let me think"

        text_blocks = [b for b in back["content"] if b.get("type") == "text"]
        assert len(text_blocks) == 1
        assert text_blocks[0]["text"] == "here is the answer"

        tool_blocks = [b for b in back["content"] if b.get("type") == "tool_use"]
        assert len(tool_blocks) == 1
        assert tool_blocks[0]["id"] == "toolu_rt1"
        assert tool_blocks[0]["name"] == "calculator"
        assert tool_blocks[0]["input"] == {"expr": "2+2"}

        assert back["stop_reason"] == "tool_use"

    def test_chat_to_responses_to_chat_reasoning(self):
        chat = {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "final answer",
                        "reasoning_content": "step by step",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "prompt_tokens_details": {"cached_tokens": 80},
            },
        }
        responses = pm.openai_chat_to_responses(chat, "test-model")

        reasoning_items = [i for i in responses["output"] if i.get("type") == "reasoning"]
        assert len(reasoning_items) == 1
        assert reasoning_items[0]["summary"][0]["text"] == "step by step"

        back = pm._responses_to_chat_response(responses, "test-model")
        msg = back["choices"][0]["message"]
        assert msg["content"] == "final answer"
        assert msg["reasoning_content"] == "step by step"
        assert back["usage"]["prompt_tokens_details"]["cached_tokens"] == 80
