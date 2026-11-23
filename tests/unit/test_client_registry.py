from free.clients import capabilities, respond


def test_known_client_family():
    assert capabilities.profile_for("claude-code").family == "anthropic_messages"
    assert capabilities.profile_for("codex").family == "openai_responses"
    assert capabilities.profile_for("hermes-agent").family == "openai_chat"


def test_unknown_client_falls_back_to_custom():
    profile = capabilities.profile_for("never-seen")
    assert profile.id == "custom"
    assert profile.family == "openai_chat"


def test_detect_family_from_path_and_headers():
    assert capabilities.detect_from_headers("/v1/messages") == "anthropic_messages"
    assert capabilities.detect_from_headers("/v1/responses") == "openai_responses"
    assert capabilities.detect_from_headers("/v1/chat/completions") == "openai_chat"
    assert capabilities.detect_from_headers("/x", {"anthropic-version": "1"}) == "anthropic_messages"


def test_anthropic_conversion_moves_system_and_maps_tools():
    body = {
        "model": "m",
        "max_tokens": 50000,
        "system": [{"type": "text", "text": "sys"}],
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "tools": [{"name": "Read", "input_schema": {"type": "object"}}],
        "stream": True,
    }
    chat = respond.convert_request(body, "anthropic_messages")
    assert chat["messages"][0] == {"role": "system", "content": "sys"}
    assert chat["messages"][1]["content"] == "hi"
    assert chat["tools"][0]["function"]["name"] == "read"
    assert chat["max_completion_tokens"] == 32000
    assert chat["stream_options"] == {"include_usage": True}
    assert "system" not in chat


def test_openai_chat_moves_root_system_and_normalizes_tool_choice():
    body = {
        "model": "m",
        "system": "sys",
        "messages": [{"role": "user", "content": "hi"}],
        "tool_choice": "none",
        "stream": True,
    }
    chat = respond.convert_request(body, "openai_chat")
    assert chat["messages"][0] == {"role": "system", "content": "sys"}
    assert chat["tool_choice"] == "auto"
    assert chat["stream_options"] == {"include_usage": True}


def test_responses_conversion_builds_messages_and_reasoning():
    body = {
        "model": "m",
        "instructions": "sys",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        "reasoning": {"effort": "high"},
        "max_output_tokens": 100,
        "stream": True,
    }
    chat = respond.convert_request(body, "openai_responses")
    assert chat["messages"][0] == {"role": "system", "content": "sys"}
    assert chat["messages"][1]["content"] == "hi"
    assert chat["reasoning_effort"] == "high"
    assert chat["max_completion_tokens"] == 100


def test_error_payload_shapes_per_family():
    anth = respond.error_payload("anthropic_messages", "boom", error_type="api_error")
    assert anth["type"] == "error" and anth["error"]["message"] == "boom"
    oai = respond.error_payload("openai_chat", "boom", error_type="api_error")
    assert oai["error"]["message"] == "boom"


def test_local_refusal_and_capacity_codes():
    status, payload = respond.local_refusal("openai_chat", "bad field", field="system")
    assert status == 422 and payload["error"]["type"] == "wire_incompatible"
    status, payload = respond.capacity_exhausted("anthropic_messages", reset_at=123.0)
    assert status == 503 and payload["error"]["type"] == "capacity_exhausted"
