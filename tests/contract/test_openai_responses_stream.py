"""OpenAI Responses SSE stream — ``response.created`` → ``response.output_item.*``
→ ``response.completed``; ``response.failed`` on error.

Exercises the real :class:`app.protocol.mapping.ResponsesStreamEmitter`,
:func:`app.protocol.mapping.responses_stream_events` and
:func:`app.protocol.mapping.responses_stream_sse`, and checks the lifecycle names
against :meth:`free.clients.family_openai_responses.sse_contract`.
"""

from __future__ import annotations

import json

from app.protocol.mapping import (
    ResponsesStreamEmitter,
    responses_stream_events,
    responses_stream_sse,
)
from free.clients import family_openai_responses


# ── fixtures ────────────────────────────────────────────────────────────────


def _response(text: str = "Bonjour le monde") -> dict:
    return {
        "id": "resp_test",
        "object": "response",
        "status": "completed",
        "model": "test-model",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
    }


def _types(events: list[dict]) -> list[str]:
    return [e["type"] for e in events]


# ── lifecycle: created → output_item.* → completed ─────────────────────────


def test_responses_stream_created_first_completed_last():
    """``response.created`` opens the stream; ``response.completed`` closes it."""
    events = responses_stream_events(_response(), "test-model")
    types = _types(events)

    assert types[0] == "response.created"
    assert types[-1] == "response.completed"
    assert events[0]["sequence_number"] == 0


def test_responses_stream_output_items_are_opened_and_closed():
    """Each output item brackets its deltas with ``output_item.added`` / ``.done``."""
    events = responses_stream_events(_response("a longer text that gets split"), "test-model")
    types = _types(events)

    assert "response.output_item.added" in types
    assert "response.output_item.done" in types
    added = [e["output_index"] for e in events if e["type"] == "response.output_item.added"]
    done = [e["output_index"] for e in events if e["type"] == "response.output_item.done"]
    assert added == done == [0]

    # Every text delta lies inside the item's added/done window.
    first_added = types.index("response.output_item.added")
    first_done = types.index("response.output_item.done")
    for idx, t in enumerate(types):
        if t == "response.output_text.delta":
            assert first_added < idx < first_done


def test_responses_stream_completed_carries_usage():
    """The terminal ``response.completed`` transports the final usage."""
    events = responses_stream_events(_response(), "test-model")
    completed = [e for e in events if e["type"] == "response.completed"]
    assert len(completed) == 1
    usage = completed[0]["response"]["usage"]
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 20


def test_responses_stream_text_deltas_rebuild_the_answer():
    """Concatenated ``output_text.delta`` fragments equal the original text."""
    text = "Voici une réponse complète." * 6
    events = responses_stream_events(_response(text), "test-model")
    rebuilt = "".join(
        e.get("delta", "") for e in events if e["type"] == "response.output_text.delta"
    )
    assert rebuilt == text


def test_responses_stream_lifecycle_names_are_declared_by_the_family():
    """The required lifecycle events are part of ``family_openai_responses`` contract."""
    contract = set(family_openai_responses.sse_contract())
    events = responses_stream_events(_response(), "test-model")
    for required in (
        "response.created",
        "response.output_item.added",
        "response.output_item.done",
        "response.completed",
    ):
        assert required in contract, f"{required} missing from family contract"
        assert required in _types(events), f"{required} not emitted"


# ── SSE serialization ───────────────────────────────────────────────────────


def test_responses_stream_sse_terminates_with_done():
    """The serialized body ends with the ``[DONE]`` sentinel used by existing clients."""
    events = responses_stream_events(_response(), "test-model")
    body = responses_stream_sse(events)
    assert body.endswith(b"data: [DONE]\n\n")


def test_responses_stream_sse_frames_are_parseable():
    """Every event is serialized as a parseable ``data:`` JSON object."""
    events = responses_stream_events(_response(), "test-model")
    body = responses_stream_sse(events)
    frames = [
        ln[len("data: "):]
        for ln in body.decode("utf-8").splitlines()
        if ln.startswith("data: ") and ln[len("data: "):] != "[DONE]"
    ]
    assert len(frames) == len(events)
    for raw in frames:
        parsed = json.loads(raw)
        assert "type" in parsed
        assert "sequence_number" in parsed


# ── response.failed on error ────────────────────────────────────────────────


def test_responses_stream_failed_is_the_terminal_on_error():
    """A failed stream ends with a coherent ``response.failed`` terminal."""
    emitter = ResponsesStreamEmitter("test-model")
    emitter.created()
    failed = emitter.failed(message="upstream cut")

    assert failed["type"] == "response.failed"
    response = failed["response"]
    assert response["status"] == "failed"
    assert response["object"] == "response"
    assert response["error"]["message"] == "upstream cut"
    assert response["error"]["code"] == "stream_error"


def test_responses_stream_failed_is_declared_by_the_family():
    """``response.failed`` belongs to ``family_openai_responses.sse_contract()``."""
    assert "response.failed" in family_openai_responses.sse_contract()


def test_responses_stream_failed_serializes_as_a_data_frame():
    """The failure terminal survives SSE serialization as a parseable frame."""
    emitter = ResponsesStreamEmitter("test-model")
    emitter.created()
    failed = emitter.failed(message="boom")
    body = responses_stream_sse([emitter.created(), failed])
    payloads = [
        json.loads(ln[len("data: "):])
        for ln in body.decode("utf-8").splitlines()
        if ln.startswith("data: ") and ln[len("data: "):] != "[DONE]"
    ]
    failed_payloads = [p for p in payloads if p["type"] == "response.failed"]
    assert len(failed_payloads) == 1
    assert failed_payloads[0]["response"]["error"]["message"] == "boom"
