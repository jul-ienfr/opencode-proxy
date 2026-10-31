"""[audit 2026-10-30] Contrats P0 de conversion alignés sur l'officiel.

Chaque test correspond à un défaut mesuré dans l'audit :
- historique Responses : ``output_text`` assistant et ``instructions`` perdus ;
- historique agentique : ``tool_result`` émis après la réponse assistant finale ;
- ids d'outils scrubbés d'un côté seulement ;
- usage Anthropic → OpenAI : cache non réintégré au total ;
- ``stop`` string, ``finish_reason`` tool_calls, budget de compaction.
"""

from app.compaction.truncate import build_condensed_history
from app.protocol.mapping import (
    _responses_part_to_anthropic,
    _responses_to_chat_response,
    anthropic_to_openai_response,
    anthropic_to_openai_responses,
    openai_responses_to_anthropic,
    openai_to_anthropic_request,
)
from protocol.tokens import estimate_input_tokens


def test_p6_instructions_become_system_prompt():
    out = openai_responses_to_anthropic(
        {
            "model": "m",
            "instructions": "Tu es un assistant médical. Ne donne jamais de diagnostic.",
            "input": "J'ai mal à la tête",
        }
    )
    assert out["system"] == "Tu es un assistant médical. Ne donne jamais de diagnostic."
    assert out["messages"][0]["role"] == "user"


def test_p6_assistant_output_text_is_preserved():
    out = openai_responses_to_anthropic(
        {
            "model": "m",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "Q1"}]},
                {"role": "assistant", "content": [{"type": "output_text", "text": "R1"}]},
                {"role": "user", "content": [{"type": "input_text", "text": "Q2"}]},
            ],
        }
    )
    assistant = [m for m in out["messages"] if m["role"] == "assistant"]
    assert assistant and assistant[0]["content"][0]["text"] == "R1"


def _p4_tool_history():
    return {
        "model": "m",
        "messages": [
            {"role": "user", "content": "q"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call.abc:1",
                        "type": "function",
                        "function": {"name": "read", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call.abc:1", "content": "ok"},
            {"role": "assistant", "content": "final"},
        ],
    }


def test_p4_tool_result_is_immediately_after_tool_use():
    out = openai_to_anthropic_request(_p4_tool_history())
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    tool_use = out["messages"][1]["content"][0]
    tool_result = out["messages"][2]["content"][0]
    assert tool_use["type"] == "tool_use"
    assert tool_result["type"] == "tool_result"
    assert tool_use["id"] == tool_result["tool_use_id"] == "call_abc_1"


def test_p6_tool_result_is_immediately_after_tool_use():
    out = openai_responses_to_anthropic(
        {
            "model": "m",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "q"}]},
                {"type": "function_call", "call_id": "call.abc:1", "name": "read", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "call.abc:1", "output": "ok"},
                {"role": "assistant", "content": [{"type": "output_text", "text": "final"}]},
            ],
        }
    )
    roles = [m["role"] for m in out["messages"]]
    assert roles == ["user", "assistant", "user", "assistant"]
    tool_use = out["messages"][1]["content"][0]
    tool_result = out["messages"][2]["content"][0]
    assert tool_use["id"] == tool_result["tool_use_id"] == "call_abc_1"


def test_p6_orphan_tool_result_is_dropped():
    out = openai_responses_to_anthropic(
        {
            "model": "m",
            "input": [
                {"role": "user", "content": [{"type": "input_text", "text": "q"}]},
                {"type": "function_call_output", "call_id": "call.missing", "output": "ok"},
                {"role": "assistant", "content": [{"type": "output_text", "text": "final"}]},
            ],
        }
    )
    assert all(
        block.get("type") != "tool_result"
        for msg in out["messages"]
        for block in (msg.get("content") or [])
        if isinstance(block, dict)
    )


def test_document_uses_title_not_name():
    doc = _responses_part_to_anthropic(
        {
            "type": "input_file",
            "file_data": "data:application/pdf;base64,QQ==",
            "filename": "rapport.pdf",
        }
    )
    assert doc["type"] == "document"
    assert doc["title"] == "rapport.pdf"
    assert "name" not in doc


def test_anthropic_to_openai_usage_includes_cache():
    response = {
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 97,
            "output_tokens": 10,
            "cache_read_input_tokens": 87552,
            "cache_creation_input_tokens": 0,
        },
    }
    out = anthropic_to_openai_response(response, "m")
    assert out["usage"]["prompt_tokens"] == 87649
    assert out["usage"]["total_tokens"] == 87659
    assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 87552


def test_anthropic_to_responses_usage_includes_cache():
    response = {
        "content": [{"type": "text", "text": "hi"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 97,
            "output_tokens": 10,
            "cache_read_input_tokens": 87552,
        },
    }
    out = anthropic_to_openai_responses(response, "m")
    assert out["usage"]["input_tokens"] == 87649
    assert out["usage"]["total_tokens"] == 87659
    assert out["usage"]["input_tokens_details"]["cached_tokens"] == 87552


def test_chat_stop_string_is_wrapped_for_anthropic():
    out = openai_to_anthropic_request({"model": "m", "messages": [], "stop": "\n\n"})
    assert out["stop_sequences"] == ["\n\n"]


def test_responses_to_chat_finish_reason_tool_calls():
    out = _responses_to_chat_response(
        {
            "status": "completed",
            "output": [
                {"type": "function_call", "call_id": "c1", "name": "read", "arguments": "{}"}
            ],
            "usage": {},
        },
        "m",
    )
    assert out["choices"][0]["finish_reason"] == "tool_calls"


def test_token_estimator_accepts_plain_text():
    assert estimate_input_tokens("abcd" * 1000) > 0


def test_keep_recent_pairs_zero_keeps_current_turn():
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
    ]
    condensed, _recent = build_condensed_history(messages, "summary", 0)
    assert condensed is not None
    assert any(
        m.get("role") == "user" and "current question" in str(m.get("content", ""))
        for m in condensed[1:]
    )

def test_response_cache_never_stores_error_shaped_200():
    from server.cache import ResponseCache

    c = ResponseCache()
    key = c.make_key({"model": "m", "messages": [{"role": "user", "content": "hi"}]})
    assert key
    c.put(key, b'{"error":{"message":"rate limited","type":"rate_limit_error"}}', {})
    assert c.get(key) is None
    assert c.stats()["skipped_errors"] == 1
