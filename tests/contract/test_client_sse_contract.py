
from free.clients import (
    family_anthropic,
    family_openai_chat,
    family_openai_responses,
    respond,
)


def test_anthropic_sse_contract_order():
    contract = family_anthropic.sse_contract()
    assert contract[0] == "message_start"
    assert contract[-1] == "message_stop"
    assert "content_block_delta" in contract


def test_openai_chat_sse_contract():
    contract = family_openai_chat.sse_contract()
    assert "[DONE]" in contract
    assert "finish_reason" in contract


def test_responses_sse_contract():
    contract = family_openai_responses.sse_contract()
    assert "response.created" in contract
    assert "response.completed" in contract
    assert "response.failed" in contract


def test_sse_frame_and_done_are_valid():
    frame = respond.sse_frame("message_start", {"type": "message_start"})
    assert frame.startswith(b"event: message_start\ndata: {")
    assert frame.endswith(b"\n\n")
    data_only = respond.sse_frame("", {"choices": []})
    assert data_only.startswith(b"data: {")
    assert respond.sse_done() == b"data: [DONE]\n\n"


def test_error_mapping_is_family_specific():
    anth = respond.local_refusal("anthropic_messages", "bad", field="system")
    assert anth[0] == 422 and anth[1]["error"]["type"] == "wire_incompatible"
    oai = respond.local_refusal("openai_chat", "bad", field="system")
    assert oai[0] == 422 and oai[1]["error"]["type"] == "wire_incompatible"
    cap = respond.capacity_exhausted("openai_chat", reset_at=1.0)
    assert cap[0] == 503 and cap[1]["error"]["code"] == "capacity_exhausted"
    quota = respond.quota_exhausted("anthropic_messages", retry_after="60")
    assert quota[0] == 429


def test_usage_is_preserved_in_anthropic_delta_shape():
    payload = {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}}
    frame = respond.sse_frame("message_delta", payload)
    assert b'"usage"' in frame and b'"stop_reason"' in frame
