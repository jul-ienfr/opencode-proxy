"""Non-stream ``/v1/messages`` shape — the Anthropic client family.

Request direction: :func:`free.clients.family_anthropic.to_chat` (and the
dispatching :func:`free.clients.respond.convert_request`) move root ``system``
blocks into ``messages``, map tools to the official chat grid, and bound
``max_tokens``.

Response direction: :func:`app.protocol.mapping.openai_to_anthropic` is the real
Chat → Anthropic response builder; its output carries ``id`` / ``type`` /
``content`` / ``stop_reason`` / ``usage`` and preserves ``tool_use`` blocks.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.protocol.mapping import openai_to_anthropic
from free.clients import family_anthropic, respond, tool_policy

FIXTURES = Path("free/clients/fixtures")


# ── request shape ───────────────────────────────────────────────────────────


def test_request_system_at_root_moves_into_messages():
    """Root ``system`` becomes the first ``system`` message on the chat wire."""
    body = {
        "model": "test-model",
        "system": "Be helpful",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 1024,
        "stream": False,
    }
    chat = family_anthropic.to_chat(body)
    assert "system" not in chat
    assert chat["messages"][0] == {"role": "system", "content": "Be helpful"}
    assert chat["messages"][1] == {"role": "user", "content": "Hello"}


def test_request_system_as_text_blocks():
    """``system`` given as content blocks is flattened and moved into messages."""
    body = {
        "model": "test-model",
        "system": [{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}],
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    chat = family_anthropic.to_chat(body)
    assert chat["messages"][0]["role"] == "system"
    assert "line one" in chat["messages"][0]["content"]
    assert "line two" in chat["messages"][0]["content"]


def test_request_messages_only_has_no_system():
    """A body with only ``messages`` keeps them and adds no system message."""
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "Hello"}],
        "max_tokens": 1024,
        "stream": False,
    }
    chat = family_anthropic.to_chat(body)
    assert isinstance(chat["messages"], list) and chat["messages"]
    assert chat["messages"][0]["role"] == "user"


def test_request_tools_are_mapped_to_the_official_grid():
    """Anthropic ``input_schema`` tools become chat ``function`` tools, names mapped."""
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [
            {
                "name": "Read",
                "description": "Read a file",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
            }
        ],
        "stream": False,
    }
    chat = family_anthropic.to_chat(body)
    tools = chat.get("tools") or []
    assert len(tools) == 1
    assert tools[0]["type"] == "function"
    # Client alias "Read" maps to the official "read".
    assert tools[0]["function"]["name"] == "read"
    assert tools[0]["function"]["parameters"]["properties"]["path"]["type"] == "string"


def test_request_tool_use_blocks_are_kept_as_tool_calls():
    """Assistant ``tool_use`` blocks survive as chat ``tool_calls``."""
    body = {
        "model": "test-model",
        "messages": [
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Sure"},
                    {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}},
                ],
            },
        ],
        "stream": False,
    }
    chat = family_anthropic.to_chat(body)
    assistant = next(m for m in chat["messages"] if m["role"] == "assistant")
    calls = assistant.get("tool_calls") or []
    assert len(calls) == 1
    assert calls[0]["id"] == "toolu_1"
    # "Bash" alias maps to the official "shell".
    assert calls[0]["function"]["name"] == "shell"
    assert json.loads(calls[0]["function"]["arguments"]) == {"command": "ls"}


def test_request_max_tokens_is_bounded_by_the_family_ceiling():
    """``max_tokens`` above the family ceiling is clamped to 32000."""
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 50000,
        "stream": False,
    }
    chat = family_anthropic.to_chat(body)
    assert chat["max_completion_tokens"] == family_anthropic.MAX_OUTPUT_TOKENS == 32000


def test_request_streaming_requests_usage():
    """A streaming request asks upstream for usage frames."""
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    chat = family_anthropic.to_chat(body)
    assert chat["stream"] is True
    assert chat["stream_options"] == {"include_usage": True}


def test_request_via_convert_request_uses_a_real_fixture():
    """The dispatcher and a real fixture produce the same request shape."""
    body = json.loads((FIXTURES / "claude-code" / "request.json").read_text(encoding="utf-8"))
    chat = respond.convert_request(body, family_anthropic.FAMILY)
    assert "system" not in chat
    assert isinstance(chat.get("messages"), list) and chat["messages"]
    assert chat["messages"][0]["role"] == "system"
    names = [tool_policy.tool_name(t) for t in (chat.get("tools") or [])]
    assert "read" in names


# ── response shape ──────────────────────────────────────────────────────────


def _chat_response(**overrides) -> dict:
    resp = {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Hello there"},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }
    resp.update(overrides)
    return resp


def test_response_has_the_required_anthropic_fields():
    """``id`` / ``type`` / ``content`` / ``stop_reason`` / ``usage`` are present."""
    resp = openai_to_anthropic(_chat_response(), "test-model")
    assert isinstance(resp["id"], str) and resp["id"].startswith("msg_")
    assert resp["type"] == "message"
    assert resp["role"] == "assistant"
    assert isinstance(resp["content"], list) and resp["content"]
    assert resp["content"][0] == {"type": "text", "text": "Hello there"}
    assert resp["stop_reason"] == "end_turn"
    assert resp["stop_sequence"] is None
    usage = resp["usage"]
    assert set(usage) == {
        "input_tokens",
        "output_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
    }
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5


def test_response_tool_calls_become_tool_use_blocks():
    """Chat ``tool_calls`` are preserved as ``tool_use`` blocks with parsed input."""
    chat = _chat_response(
        choices=[
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "read", "arguments": '{"path": "a.txt"}'},
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    )
    resp = openai_to_anthropic(chat, "test-model")
    tool_blocks = [b for b in resp["content"] if b.get("type") == "tool_use"]
    assert len(tool_blocks) == 1
    block = tool_blocks[0]
    assert block["id"]
    assert block["name"] == "read"
    assert block["input"] == {"path": "a.txt"}
    assert resp["stop_reason"] == "tool_use"


def test_response_length_finish_reason_maps_to_max_tokens():
    """A Chat ``length`` finish becomes Anthropic ``max_tokens``."""
    chat = _chat_response(
        choices=[
            {
                "index": 0,
                "message": {"role": "assistant", "content": "truncated"},
                "finish_reason": "length",
            }
        ]
    )
    resp = openai_to_anthropic(chat, "test-model")
    assert resp["stop_reason"] == "max_tokens"


def test_response_cache_read_is_reported_separately():
    """Cached input is reported apart from ``input_tokens`` (no double count)."""
    chat = _chat_response(
        usage={
            "prompt_tokens": 100,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 60},
        }
    )
    resp = openai_to_anthropic(chat, "test-model")
    usage = resp["usage"]
    # Anthropic: non-cached input + cache_read, summing back to the OpenAI total.
    assert usage["input_tokens"] == 40
    assert usage["cache_read_input_tokens"] == 60
    assert usage["input_tokens"] + usage["cache_read_input_tokens"] == 100
