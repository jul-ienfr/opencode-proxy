"""OpenAI Chat Completions SSE stream — ``choices[].delta`` → ``finish_reason``
→ ``[DONE]``; ``usage`` when ``include_usage``; error object mid-stream.

Exercises the real :mod:`free.clients.respond` framing/serialization helpers and
the declared :meth:`free.clients.family_openai_chat.sse_contract`, and checks the
real Responses→Chat delta adapter (:func:`app.protocol.mapping._responses_sse_to_chat_deltas`)
yields the same chunk shape.
"""

from __future__ import annotations

import json

from app.protocol.mapping import _responses_sse_to_chat_deltas
from free.clients import family_openai_chat, respond


# ── helpers ─────────────────────────────────────────────────────────────────


def _chunk(delta=None, finish_reason=None, usage=None) -> dict:
    """A ``chat.completion.chunk`` payload as the Chat wire defines it."""
    body: dict = {
        "id": "chatcmpl_stream",
        "object": "chat.completion.chunk",
        "model": "test-model",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
    }
    if usage is not None:
        body["usage"] = usage
    return body


def _frames(payloads: list[dict]) -> bytes:
    """Serialize chat payloads then the ``[DONE]`` sentinel via the real helpers."""
    body = b"".join(respond.sse_frame("", payload) for payload in payloads)
    return body + respond.sse_done()


def _parse_frames(body: bytes) -> list:
    """Parse a ``data:`` SSE body back into payloads/``[DONE]``."""
    out: list = []
    for line in body.decode("utf-8").splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        if payload == "[DONE]":
            out.append("[DONE]")
        else:
            out.append(json.loads(payload))
    return out


# ── declared contract ───────────────────────────────────────────────────────


def test_chat_stream_family_declares_chunk_finish_usage_done():
    contract = family_openai_chat.sse_contract()
    assert "chunk" in contract
    assert "finish_reason" in contract
    assert "usage" in contract
    assert contract[-1] == "[DONE]"


# ── chunk → finish_reason → [DONE] ──────────────────────────────────────────


def test_chat_stream_delta_then_finish_reason_then_done():
    """The wire: delta chunks, then a chunk carrying ``finish_reason``, then ``[DONE]``."""
    body = _frames(
        [
            _chunk({"role": "assistant", "content": ""}),
            _chunk({"content": "Bonjour"}),
            _chunk({"content": " le monde"}),
            _chunk({}, finish_reason="stop"),
        ]
    )
    parsed = _parse_frames(body)

    # Every non-terminal item is a chunk object.
    for obj in parsed[:-1]:
        assert obj["object"] == "chat.completion.chunk"
        assert isinstance(obj["choices"], list) and obj["choices"]
        assert "delta" in obj["choices"][0]

    # Content is streamed through delta, in order.
    text = "".join(
        obj["choices"][0]["delta"].get("content", "") for obj in parsed[:-1]
    )
    assert text == "Bonjour le monde"

    # Only the last chunk carries a non-null finish_reason.
    finish_reasons = [obj["choices"][0].get("finish_reason") for obj in parsed[:-1]]
    assert finish_reasons[-1] == "stop"
    assert all(fr is None for fr in finish_reasons[:-1])

    # The stream terminates with the [DONE] sentinel.
    assert parsed[-1] == "[DONE]"
    assert body.endswith(respond.sse_done())


def test_chat_stream_done_frame_is_the_respond_sentinel():
    """``[DONE]`` is exactly ``respond.sse_done()``."""
    assert respond.sse_done() == b"data: [DONE]\n\n"
    assert _frames([_chunk({"content": "x"})]).endswith(respond.sse_done())


# ── usage when include_usage ────────────────────────────────────────────────


def test_chat_stream_request_forces_include_usage():
    """A streaming request is converted with ``stream_options.include_usage``."""
    body = {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    chat = respond.convert_request(body, family_openai_chat.FAMILY)
    assert chat["stream"] is True
    assert chat["stream_options"] == {"include_usage": True}


def test_chat_stream_usage_frame_is_preserved():
    """When upstream sends usage, the final chunk carries it intact."""
    usage = {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}
    body = _frames(
        [
            _chunk({"content": "hi"}),
            _chunk({}, finish_reason="stop", usage=usage),
        ]
    )
    parsed = _parse_frames(body)
    usage_chunks = [obj for obj in parsed[:-1] if isinstance(obj.get("usage"), dict)]
    assert usage_chunks, "no usage frame emitted"
    assert usage_chunks[-1]["usage"] == usage


# ── error object mid-stream ─────────────────────────────────────────────────


def test_chat_stream_error_object_uses_respond_shape():
    """A mid-stream failure is framed as an ``{"error": {...}}`` object."""
    payload = respond.error_payload(
        family_openai_chat.FAMILY, "upstream exploded", error_type="api_error"
    )
    assert "error" in payload
    frame = respond.sse_frame("", payload)
    assert frame.startswith(b"data: {")
    assert frame.endswith(b"\n\n")

    parsed = json.loads(frame.decode().removeprefix("data: "))
    assert parsed["error"]["message"] == "upstream exploded"
    assert parsed["error"]["type"] == "api_error"


def test_chat_stream_capacity_exhausted_is_503_not_429():
    """The proxy's 'no capacity' signal stays a 503 so the client backs off, not throttles."""
    status, payload = respond.capacity_exhausted(family_openai_chat.FAMILY, reset_at=123.0)
    assert status == 503
    assert payload["error"]["type"] == "capacity_exhausted"
    assert payload["error"]["code"] == "capacity_exhausted"


# ── real Responses→Chat delta adapter emits the same chunk shape ────────────


def test_responses_to_chat_delta_adapter_shape():
    """The real bridge produces ``choices[].delta`` chunks, not invented shapes."""
    chunk = _responses_sse_to_chat_deltas(
        json.dumps({"type": "response.output_text.delta", "delta": "Bonjour"})
    )
    assert chunk is not None
    assert isinstance(chunk["choices"], list) and chunk["choices"]
    choice = chunk["choices"][0]
    assert choice["delta"] == {"content": "Bonjour"}
    assert choice["finish_reason"] is None


def test_responses_completed_adapter_carries_usage():
    """The real bridge surfaces final usage (and a private finish reason) at completion."""
    completed = _responses_sse_to_chat_deltas(
        json.dumps(
            {
                "type": "response.completed",
                "response": {
                    "id": "r",
                    "status": "completed",
                    "usage": {"input_tokens": 10, "output_tokens": 4, "total_tokens": 14},
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ok"}],
                        }
                    ],
                },
            }
        )
    )
    assert completed is not None
    assert completed["usage"]["prompt_tokens"] == 10
    assert completed["usage"]["completion_tokens"] == 4
    assert completed["_finish_reason"] == "stop"
