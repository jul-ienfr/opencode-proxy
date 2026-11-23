"""Anthropic SSE stream sequence — ``message_start`` → ``content_block_start``
/ ``content_block_delta`` / ``content_block_stop`` → ``message_delta`` →
``message_stop``; final usage preserved; an error mid-stream maps to an
``error`` event (T062).

Exercises the real :mod:`app.protocol.chat_sse_to_anthropic` adapter
(``ChatSseToAnthropicState`` / ``chat_sse_to_anthropic_events``), whose emitted
event names are the ones declared by :meth:`free.clients.family_anthropic.sse_contract`.
"""

from __future__ import annotations

import json

from app.protocol.chat_sse_to_anthropic import (
    ChatSseToAnthropicState,
    chat_sse_to_anthropic_events,
)
from free.clients import family_anthropic, respond


# ── helpers mirroring the real adapter's wire ─────────────────────────────


def _chunk(delta=None, finish_reason=None, usage=None) -> str:
    """One ``chat.completion.chunk`` data line, as the upstream emits it."""
    body: dict = {
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": delta or {}}],
    }
    if finish_reason is not None:
        body["choices"][0]["finish_reason"] = finish_reason
    if usage is not None:
        body["usage"] = usage
    return f"data: {json.dumps(body, ensure_ascii=False)}"


def _events(lines: list[str], state: ChatSseToAnthropicState) -> list[str]:
    out: list[str] = []
    for line in lines:
        out.extend(chat_sse_to_anthropic_events(line, state=state))
    return out


def _names(events: list[str]) -> list[str]:
    return [e.split("\n", 1)[0].removeprefix("event: ") for e in events]


def _data(events: list[str], name: str) -> list[dict]:
    out: list[dict] = []
    for e in events:
        parts = e.split("\n")
        if parts[0] == f"event: {name}":
            out.append(json.loads(parts[1].removeprefix("data: ")))
    return out


# ── sequence ───────────────────────────────────────────────────────────────


def test_anthropic_stream_full_sequence_order():
    """The emitted event order matches the Anthropic contract for one text block."""
    state = ChatSseToAnthropicState(
        model="claude-haiku-4-5", message_id="msg_stream_order", input_tokens_estimate=12
    )
    events = _events(
        [
            _chunk({"role": "assistant"}),
            _chunk({"content": "Bonjour"}),
            _chunk({"content": " le monde"}),
            _chunk({}, finish_reason="stop", usage={"prompt_tokens": 12, "completion_tokens": 2}),
            "data: [DONE]",
        ],
        state,
    )
    names = _names(events)

    # Order: message_start → content_block_start → deltas → content_block_stop
    # → message_delta → message_stop.
    assert names[0] == "message_start"
    assert names.count("message_start") == 1
    assert names.count("content_block_start") == 1
    assert names.count("content_block_delta") == 2
    assert names.count("content_block_stop") == 1
    assert names.count("message_delta") == 1
    assert names.count("message_stop") == 1
    assert names[-3:] == ["content_block_stop", "message_delta", "message_stop"]

    order = [
        names.index("message_start"),
        names.index("content_block_start"),
        names.index("content_block_delta"),
        names.index("content_block_stop"),
        names.index("message_delta"),
        names.index("message_stop"),
    ]
    assert order == sorted(order), f"sequence out of order: {names}"


def test_anthropic_stream_events_are_the_family_contract():
    """Every emitted lifecycle event belongs to the family's declared contract."""
    state = ChatSseToAnthropicState(model="m", message_id="msg_contract")
    events = _events(
        [_chunk({"content": "x"}), _chunk({}, finish_reason="stop"), "data: [DONE]"],
        state,
    )
    contract = set(family_anthropic.sse_contract())
    for name in _names(events):
        assert name in contract, f"event {name!r} not declared by family_anthropic.sse_contract()"


def test_anthropic_stream_sse_frames_use_respond_shape():
    """Frames serialize exactly like ``respond.sse_frame`` (event: + data: + blank line)."""
    state = ChatSseToAnthropicState(model="m", message_id="msg_frame")
    events = _events([_chunk({"content": "x"}), "data: [DONE]"], state)
    for frame in events:
        assert frame.startswith("event: ")
        assert "\ndata: " in frame
        assert frame.endswith("}\n\n")


# ── usage ──────────────────────────────────────────────────────────────────


def test_anthropic_stream_final_usage_preserved():
    """``message_delta`` carries the final ``output_tokens`` and non-cached input."""
    state = ChatSseToAnthropicState(
        model="m", message_id="msg_usage", input_tokens_estimate=12
    )
    events = _events(
        [
            _chunk({"content": "a"}),
            _chunk(
                {},
                finish_reason="stop",
                usage={"prompt_tokens": 12, "completion_tokens": 7},
            ),
            "data: [DONE]",
        ],
        state,
    )
    message_deltas = _data(events, "message_delta")
    assert len(message_deltas) == 1
    usage = message_deltas[0]["usage"]
    assert usage["input_tokens"] == 12
    assert usage["output_tokens"] == 7
    assert usage["cache_read_input_tokens"] == 0
    assert usage["cache_creation_input_tokens"] == 0


def test_anthropic_stream_message_start_usage_shape():
    """``message_start`` announces zero output tokens and the estimated input."""
    state = ChatSseToAnthropicState(
        model="m", message_id="msg_start", input_tokens_estimate=9
    )
    events = _events([_chunk({"content": "a"}), "data: [DONE]"], state)
    start = _data(events, "message_start")[0]["message"]
    assert start["id"] == "msg_start"
    assert start["type"] == "message"
    assert start["role"] == "assistant"
    assert start["usage"]["input_tokens"] == 9
    assert start["usage"]["output_tokens"] == 0


# ── error mid-stream (T062) ─────────────────────────────────────────────────


def test_anthropic_stream_error_maps_to_error_event():
    """A Chat ``{"error": …}`` line mid-stream becomes ``event: error``."""
    state = ChatSseToAnthropicState(model="m", message_id="msg_error")
    events = _events(
        [
            _chunk({"content": "texte déjà reçu"}),
            f"data: {json.dumps({'error': {'message': 'rate limit exceeded', 'type': 'rate_limit_error'}})}",
            "data: [DONE]",
        ],
        state,
    )
    names = _names(events)

    assert "error" in names, f"no error event in {names}"
    # The error precedes the closing sequence, so a strict client sees the
    # failure but a lenient one still gets a clean end.
    assert names.index("error") < names.index("message_delta")
    assert names[-1] == "message_stop"

    payloads = _data(events, "error")
    assert len(payloads) == 1
    err = payloads[0]
    assert err["type"] == "error"
    # Message preserved and classified into the Anthropic vocabulary.
    assert err["error"]["message"] == "rate limit exceeded"
    assert err["error"]["type"] == "rate_limit_error"


def test_anthropic_stream_error_event_matches_respond_error_payload_shape():
    """The emitted error envelope has the shape of ``respond.error_payload``."""
    state = ChatSseToAnthropicState(model="m", message_id="msg_error_shape")
    events = _events(
        [
            f"data: {json.dumps({'error': {'message': 'boom', 'type': 'api_error'}})}",
            "data: [DONE]",
        ],
        state,
    )
    emitted = _data(events, "error")[0]
    expected = respond.error_payload(
        family_anthropic.FAMILY, "boom", error_type="api_error"
    )
    assert emitted["type"] == expected["type"] == "error"
    assert set(emitted["error"]) == set(expected["error"]) == {"type", "message"}


def test_anthropic_stream_error_after_text_closes_open_block():
    """A mid-stream error must not leave an open content block (strict clients drop the turn)."""
    state = ChatSseToAnthropicState(model="m", message_id="msg_error_block")
    events = _events(
        [
            _chunk({"content": "réponse partielle"}),
            f"data: {json.dumps({'error': {'message': 'connection reset', 'type': 'api_error'}})}",
        ],
        state,
    )
    names = _names(events)
    assert [d["index"] for d in _data(events, "content_block_stop")] == [0]
    assert names.index("content_block_stop") < names.index("message_stop")
    # Honest terminal: the turn was truncated, not a normal end_turn.
    assert _data(events, "message_delta")[0]["delta"]["stop_reason"] == "error"
