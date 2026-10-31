"""Google Gemini native dialect adapter (pure conversion tests)."""

from app.protocol.gemini import (
    chat_to_gemini,
    gemini_endpoint,
    gemini_response_to_chat,
    gemini_sse_to_chat_chunk,
)


def test_chat_to_gemini_maps_system_messages_tools_and_generation():
    body = {
        "model": "gemini-3.8-flash",
        "messages": [
            {"role": "system", "content": "You are a coding agent."},
            {"role": "user", "content": "Read a file"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{\"path\":\"a.txt\"}"}}
            ]},
            {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "hello"},
        ],
        "tools": [
            {"type": "function", "function": {
                "name": "read",
                "description": "Read a file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}},
                               "required": ["path"], "$schema": "x", "additionalProperties": False},
            }}
        ],
        "max_tokens": 128,
        "temperature": 0.2,
        "stop": "END",
    }
    out = chat_to_gemini(body, "gemini-3.8-flash")
    assert out["systemInstruction"]["parts"][0]["text"] == "You are a coding agent."
    assert [c["role"] for c in out["contents"]] == ["user", "model", "user"]
    assert out["contents"][1]["parts"][0]["functionCall"]["name"] == "read"
    assert out["contents"][2]["parts"][0]["functionResponse"]["response"]["result"] == "hello"
    decl = out["tools"][0]["functionDeclarations"][0]
    assert decl["name"] == "read"
    assert "$schema" not in decl["parameters"] and "additionalProperties" not in decl["parameters"]
    assert out["generationConfig"]["maxOutputTokens"] == 128
    assert out["generationConfig"]["temperature"] == 0.2
    assert out["generationConfig"]["stopSequences"] == ["END"]


def test_gemini_response_to_chat_maps_text_tools_usage_and_finish():
    data = {
        "candidates": [{
            "content": {"parts": [
                {"text": "Hello "},
                {"text": "world"},
                {"functionCall": {"name": "read", "args": {"path": "a.txt"}}},
            ]},
            "finishReason": "STOP",
        }],
        "usageMetadata": {
            "promptTokenCount": 10,
            "candidatesTokenCount": 4,
            "thoughtsTokenCount": 2,
            "cachedContentTokenCount": 3,
            "totalTokenCount": 16,
        },
    }
    out = gemini_response_to_chat(data, "gemini-3.8-flash")
    msg = out["choices"][0]["message"]
    assert msg["content"] == "Hello world"
    assert msg["tool_calls"][0]["function"]["name"] == "read"
    assert out["choices"][0]["finish_reason"] == "tool_calls"
    assert out["usage"]["prompt_tokens"] == 10
    assert out["usage"]["completion_tokens"] == 6
    assert out["usage"]["prompt_tokens_details"]["cached_tokens"] == 3


def test_gemini_stream_chunk_and_endpoint():
    chunk = gemini_sse_to_chat_chunk(
        {"candidates": [{"content": {"parts": [{"text": "OK"}]}, "finishReason": "STOP"}]},
        "gemini-3.8-flash",
    )
    assert chunk["choices"][0]["delta"]["content"] == "OK"
    assert chunk["choices"][0]["finish_reason"] == "stop"
    assert gemini_endpoint("https://opencode.ai/inference/google/v1beta", "gemini-3.8-flash") == (
        "https://opencode.ai/inference/google/v1beta/models/gemini-3.8-flash:generateContent"
    )
    assert gemini_endpoint("https://opencode.ai/inference/google/v1beta", "gemini-3.8-flash", stream=True).endswith(
        ":streamGenerateContent?alt=sse"
    )


def test_gemini_models_are_routed_natively():
    import opencode as oc

    route = oc._route_for("gemini-3.8-flash")
    assert route and route["model"] == "gemini-3.8-flash"
    cfg = oc.get_model_config(route["model"])
    assert cfg["protocol"] == "gemini"
    assert "/google/" in cfg["endpoint"]
