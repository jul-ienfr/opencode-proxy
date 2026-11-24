"""Tests unitaires des helpers purs de `app.compaction.truncate`.

[T155 2026-11-21] Chaque assertion épingle un comportement observable décrit
par le module (enveloppe officielle, frontières de paires, troncature 1250).
Aucun changement de production : ces tests verrouillent l'existant.
"""

from __future__ import annotations

from app.compaction.truncate import (
    _attached_label,
    _attached_mime,
    _checkpoint_text,
    _content_text,
    _has_tool_call,
    _is_tool_result,
    _pair_text,
    _pending_user_start,
    _recent_message_text,
    _role_of,
    _tool_result_line,
    _truncate_tool_output,
    build_checkpoint_input,
    build_checkpoint_summary,
    build_condensed_history,
    build_condensed_input,
    serialize_recent_context,
    split_keep_recent,
)


def _pair(n: int):
    return [
        {"role": "user", "content": f"question {n}"},
        {"role": "assistant", "content": f"answer {n}"},
    ]


def _history(pairs: int):
    out = []
    for n in range(pairs):
        out.extend(_pair(n))
    return out


def test_role_helpers_are_tolerant():
    assert _role_of({"role": "user"}) == "user"
    assert _role_of({"role": 5}) == ""
    assert _role_of(None) == ""
    assert _has_tool_call({"tool_calls": [{"id": "t"}]}) is True
    assert _has_tool_call(
        {"role": "assistant", "content": [{"type": "tool_use", "name": "read"}]}
    ) is True
    assert _has_tool_call({"content": "text only"}) is False
    assert _is_tool_result({"role": "tool"}) is True
    assert _is_tool_result(
        {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}
    ) is True
    assert _is_tool_result({"role": "user", "content": "ok"}) is False


def test_pair_text_includes_text_reasoning_and_tool_calls():
    text = _pair_text(
        [
            {"role": "user", "content": "question"},
            {
                "role": "assistant",
                "reasoning_content": "je réfléchis",
                "content": [{"type": "text", "text": "réponse"}],
                "tool_calls": [{"function": {"name": "read", "arguments": "{}"}}],
            },
        ]
    )
    assert "user: question" in text
    assert "je réfléchis" in text
    assert "read" in text


def test_pair_text_never_raises_on_bad_input():
    assert _pair_text(None) == ""
    assert _pair_text([None, 5, {"role": "user"}]) != ""


def test_split_keep_recent_keeps_complete_pairs():
    messages = _history(3)
    old, recent = split_keep_recent(messages, 1)
    assert old == _history(2)
    assert recent == _pair(2)
    # La coupe ne démarre jamais sur un tool_result orphelin.
    assert recent[0]["role"] == "user"


def test_split_keep_recent_zero_still_keeps_one_pair():
    old, recent = split_keep_recent(_history(2), 0)
    assert len(old) == 2 and len(recent) == 2


def test_split_keep_recent_budget_mode_keeps_the_newest_pair():
    messages = _history(3)
    old, recent = split_keep_recent(
        messages, 99, keep_tokens=10, estimate_fn=lambda text: 1
    )
    assert _pair(2)[0] in recent
    assert recent[0]["role"] == "user"
    # Estimation fautive → repli legacy, jamais d'exception.
    old2, recent2 = split_keep_recent(
        messages, 1, keep_tokens=10, estimate_fn=lambda text: 1 / 0
    )
    assert old2 == _history(2) and recent2 == _pair(2)


def test_serialize_recent_context_uses_the_official_labels():
    text = serialize_recent_context(
        [
            {"role": "user", "content": "bonjour"},
            {"role": "assistant", "content": "salut"},
            {"role": "tool", "content": "sortie"},
        ]
    )
    assert "[User]: bonjour" in text
    assert "[Assistant]: salut" in text
    assert "[Tool result]: sortie" in text
    assert serialize_recent_context("not a list") == ""


def test_recent_message_text_skips_system_and_tool_result_boundary():
    assert _recent_message_text({"role": "system", "content": "x"}) == ""
    line = _recent_message_text(
        {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}
    )
    assert line.startswith("[Tool result]")
    assert _is_tool_result({"role": "user", "content": line}) is False


def test_truncate_tool_output_boundary_is_1250():
    assert _truncate_tool_output("x" * 1250) == "x" * 1250
    value = _truncate_tool_output("x" * 1251)
    assert value == "x" * 1250 + "\n[truncated]"


def test_attached_mime_and_label():
    assert _attached_mime({"media_type": "image/png"}, "image") == "image/png"
    assert _attached_mime({"source": {"media_type": "image/jpeg"}}, "image") == "image/jpeg"
    assert (
        _attached_mime({"image_url": "data:image/webp;base64,AAAA"}, "image")
        == "image/webp"
    )
    assert _attached_mime({}, "image") == "image"
    assert _attached_label({"type": "image", "name": "shot"}) == "[Attached image: shot]"
    assert _attached_label({"type": "file", "filename": "a.pdf"}) == "[Attached file: a.pdf]"
    assert _attached_label({"type": "text"}) == ""


def test_tool_result_line_marks_errors_and_truncates():
    assert _tool_result_line({"content": "ok"}) == "[Tool result]: ok"
    assert _tool_result_line({"content": "boom", "is_error": True}) == "[Tool error]: boom"
    assert _tool_result_line({"content": "x" * 1300}).endswith("[truncated]")


def test_content_text_joins_text_blocks_and_describes_media():
    text = _content_text(
        [
            {"type": "text", "text": "a"},
            {"type": "input_text", "text": "b"},
            {"type": "image", "name": "img"},
        ]
    )
    assert text == "a\nb\n[Attached image: img]"
    assert _content_text("plain") == "plain"
    assert _content_text(None) == ""


def test_checkpoint_text_envelope_and_recent_context():
    text = _checkpoint_text("résumé", "contexte")
    assert text.startswith("<conversation-checkpoint>")
    assert "<summary>\nrésumé\n</summary>" in text
    assert "<recent-context>\ncontexte\n</recent-context>" in text
    assert "<recent-context>" not in _checkpoint_text("résumé")


def test_pending_user_start_skips_tool_results():
    messages = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
        {"role": "user", "content": "real"},
        {"role": "assistant", "content": "three"},
    ]
    assert _pending_user_start(messages) == 3
    assert _pending_user_start([]) is None


def test_build_checkpoint_messages():
    summary = build_checkpoint_summary("s", 1, "recent")
    assert summary["role"] == "user"
    assert "<recent-context>" in summary["content"]
    item = build_checkpoint_input("s")
    assert item["type"] == "message" and item["role"] == "user"
    assert item["content"][0]["type"] == "input_text"


def test_build_condensed_history_keeps_the_pending_turn_verbatim():
    messages = _history(3)
    condensed, tail = build_condensed_history(messages, "résumé", 1)
    assert condensed is not None
    assert condensed[0]["role"] == "user"
    assert "<conversation-checkpoint>" in condensed[0]["content"]
    assert condensed[-1] == messages[-1]
    # La queue brute est le dernier tour user complet (2 messages).
    assert tail == messages[4:]
    assert tail[0]["role"] == "user"


def test_build_condensed_history_falls_back_on_empty_inputs():
    messages = _history(1)
    assert build_condensed_history(messages, "", 1) == (None, messages)
    assert build_condensed_history([], "s", 1) == (None, [])
    assert build_condensed_history("bad", "s", 1) == (None, "bad")


def test_build_condensed_input_mirrors_the_history_builder():
    history = [
        {"type": "message", "role": "user", "content": "q1"},
        {"type": "message", "role": "assistant", "content": "a1"},
        {"type": "message", "role": "user", "content": "q2"},
    ]
    condensed, tail = build_condensed_input(history, "résumé", 1)
    assert condensed is not None
    assert condensed[0]["type"] == "message"
    assert condensed[-1] == history[-1]
    assert tail == history[2:]
    assert build_condensed_input(history, "", 1) == (None, history)
