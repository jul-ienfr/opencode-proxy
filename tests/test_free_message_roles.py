"""Tests de normalisation des rôles de messages free (011 client fix)."""

from __future__ import annotations

from opencode import _sanitize_free_messages


def test_developer_becomes_system() -> None:
    messages, changed = _sanitize_free_messages(
        [{"role": "developer", "content": "rules"}]
    )
    assert changed == 1
    assert messages[0]["role"] == "system"


def test_orphan_tool_becomes_user() -> None:
    messages, changed = _sanitize_free_messages(
        [{"role": "tool", "content": "result"}]
    )
    assert changed == 1
    assert messages[0] == {"role": "user", "content": "result"}


def test_unknown_role_becomes_user() -> None:
    messages, changed = _sanitize_free_messages(
        [{"role": "reasoning", "content": "thought"}]
    )
    assert changed == 1
    assert messages[0] == {"role": "user", "content": "thought"}


def test_invalid_assistant_tool_call_is_removed() -> None:
    messages, changed = _sanitize_free_messages(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "", "function": {"name": ""}}],
            }
        ]
    )
    assert changed == 1
    assert messages[0]["tool_calls"] == []


def test_valid_messages_unchanged() -> None:
    source = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a"},
        {"role": "tool", "tool_call_id": "1", "content": "t"},
    ]
    messages, changed = _sanitize_free_messages(source)
    assert changed == 0
    assert messages == source
