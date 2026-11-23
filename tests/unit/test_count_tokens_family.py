"""023 T066 — count_tokens par famille + multimodal."""

from __future__ import annotations

from free.clients import (
    family_anthropic,
    family_openai_chat,
    family_openai_responses,
    family_passthrough,
    respond,
)


def _capture():
    seen = []

    def fn(body):
        seen.append(body)
        return 42

    return fn, seen


def test_anthropic_counts_only_its_fields():
    fn, seen = _capture()
    body = {
        "system": "sys",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [{"name": "t"}],
        "instructions": "ignored",
        "input": "ignored",
    }
    assert family_anthropic.count_tokens(body, estimate_fn=fn) == 42
    assert seen[0] == {
        "messages": body["messages"],
        "tools": body["tools"],
        "system": "sys",
    }


def test_openai_chat_counts_only_messages_and_tools():
    fn, seen = _capture()
    body = {
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function"}],
        "system": "ignored",
        "instructions": "ignored",
    }
    assert family_openai_chat.count_tokens(body, estimate_fn=fn) == 42
    assert seen[0] == {"messages": body["messages"], "tools": body["tools"]}


def test_openai_responses_counts_instructions_input_tools():
    fn, seen = _capture()
    body = {
        "instructions": "be brief",
        "input": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function"}],
        "messages": [{"ignored": True}],
        "system": "ignored",
    }
    assert family_openai_responses.count_tokens(body, estimate_fn=fn) == 42
    assert seen[0] == {
        "instructions": "be brief",
        "input": body["input"],
        "tools": body["tools"],
    }


def test_passthrough_forwards_the_whole_body():
    fn, seen = _capture()
    body = {"messages": [], "anything": 1}
    assert family_passthrough.count_tokens(body, estimate_fn=fn) == 42
    assert seen[0] is body


def test_respond_dispatches_per_family():
    fn, seen = _capture()
    body = {"messages": [{"role": "user", "content": "x"}], "system": "s"}
    assert respond.count_tokens(body, family_anthropic.FAMILY, estimate_fn=fn) == 42
    assert "system" in seen[0]
    seen.clear()
    assert respond.count_tokens(body, family_openai_chat.FAMILY, estimate_fn=fn) == 42
    assert "system" not in seen[0]


def test_unknown_family_falls_back_to_generic_estimator():
    fn, seen = _capture()
    body = {"messages": []}
    assert respond.count_tokens(body, "unknown-family", estimate_fn=fn) == 42
    assert seen[0] is body


def test_multimodal_costs_more_than_text_only():
    text_only = {"messages": [{"role": "user", "content": "hello"}]}
    with_image = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "hello"},
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "A" * 75000},
                    },
                ],
            }
        ]
    }
    assert family_anthropic.count_tokens(with_image) > family_anthropic.count_tokens(text_only)
