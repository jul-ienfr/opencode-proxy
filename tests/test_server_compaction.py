"""Phase 1 — is_compaction_shape : détection « shape compaction ».

Tests unitaires purs (aucun boot réseau, aucun touch logs/requests.db).
Formes couvertes : officielle OpenCode (1..n msg user texte, sans
outils/system), Responses API équivalente, marqueurs Hermes
([CONTEXT COMPACTION – REFERENCE ONLY]) et Claude-Code
(<conversation-checkpoint>).
"""

import pytest

from app.compaction import is_compaction_shape


def _official(n=1, max_tokens=4096):
    return {
        "model": "opus",
        "messages": [{"role": "user", "content": "résumé : " + "x" * 1200} for _ in range(n)],
        "max_tokens": max_tokens,
        "stream": True,
    }


def test_official_single_user_text_shape():
    assert is_compaction_shape(_official()) is True


def test_short_user_text_is_not_compaction():
    # Garde-fou anti-faux-positif : un simple "hi" (ou tout texte < 1000 chars)
    # n'est PAS une shape-compaction — sinon les tests P4/A27 et tout tour
    # user court bypasseraient le 503.
    body = {
        "model": "haiku",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert is_compaction_shape(body) is False
    body["messages"] = [{"role": "user", "content": "x" * 999}]
    assert is_compaction_shape(body) is False
    body["messages"] = [{"role": "user", "content": "x" * 1000}]
    assert is_compaction_shape(body) is True


def test_official_multi_user_text_shape():
    assert is_compaction_shape(_official(n=3)) is True


def test_official_blocks_form():
    body = {
        "model": "opus",
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hello world" * 200}]}],
        "max_tokens": 4096,
    }
    assert is_compaction_shape(body) is True


def test_no_max_tokens_bound():
    # Pas de borne magique : 128000 (Hermes) reste une shape si user-only.
    assert is_compaction_shape(_official(max_tokens=128000)) is True


def test_tools_present_is_not_compaction():
    body = _official()
    body["tools"] = [{"type": "function", "function": {"name": "bash"}}]
    assert is_compaction_shape(body) is False


def test_empty_tools_is_compaction():
    body = _official()
    body["tools"] = []
    assert is_compaction_shape(body) is True


def test_tool_result_present_is_not_compaction():
    body = {
        "model": "opus",
        "messages": [
            {"role": "user", "content": "run this"},
            {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "tool_result", "tool_use_id": "t1", "content": "out"}]},
        ],
    }
    assert is_compaction_shape(body) is False


def test_assistant_message_is_not_compaction():
    body = {
        "model": "opus",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ],
    }
    assert is_compaction_shape(body) is False


def test_nonempty_system_is_not_compaction():
    body = _official()
    body["system"] = "You are Hermes Agent, be direct."
    assert is_compaction_shape(body) is False


def test_empty_system_is_compaction():
    body = _official()
    body["system"] = ""
    assert is_compaction_shape(body) is True
    body["system"] = []
    assert is_compaction_shape(body) is True


def test_hermes_marker_shape():
    body = {
        "model": "mimo-v2.5",
        "system": "You are Hermes Agent.",
        "messages": [
            {"role": "user", "content": "[CONTEXT COMPACTION – REFERENCE ONLY]\n...summary..."},
            {"role": "assistant", "content": "ack"},
        ],
        "max_tokens": 128000,
    }
    assert is_compaction_shape(body) is True


def test_checkpoint_marker_shape():
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {"role": "user", "content": "<conversation-checkpoint><summary>done X</summary><recent-context>last tool</recent-context></conversation-checkpoint>"},
            {"role": "assistant", "content": "ok"},
        ],
        "stream": True,
    }
    assert is_compaction_shape(body) is True


def test_marker_with_tools_is_not_compaction():
    body = {
        "model": "muse-spark-1.3-contributor",
        "messages": [
            {"role": "user", "content": "<conversation-checkpoint><summary>s</summary></conversation-checkpoint>"},
        ],
        "tools": [{"type": "function", "function": {"name": "bash"}}],
    }
    assert is_compaction_shape(body) is False


def test_normal_agent_turn_is_not_compaction():
    body = {
        "model": "opus",
        "system": "You are a coding agent.",
        "messages": [
            {"role": "user", "content": "fix this bug"},
            {"role": "assistant", "content": "on it"},
        ],
        "tools": [{"type": "function", "function": {"name": "bash"}}],
    }
    assert is_compaction_shape(body) is False


def test_responses_input_shape():
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "summary text here" * 100}]}],
    }
    assert is_compaction_shape(body) is True


def test_responses_short_input_is_not_compaction():
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
    }
    assert is_compaction_shape(body) is False


def test_responses_function_call_is_not_compaction():
    body = {
        "model": "muse-spark-1.3-contributor",
        "input": [
            {"type": "message", "role": "user", "content": "run it"},
            {"type": "function_call", "call_id": "c1", "name": "bash", "arguments": "{}"},
        ],
    }
    assert is_compaction_shape(body) is False


@pytest.mark.parametrize("bad", [None, {}, [], "str", {"messages": []}, {"messages": "x"}, {"input": []}])
def test_garbage_is_not_compaction(bad):
    assert is_compaction_shape(bad) is False


def test_never_raises():
    assert is_compaction_shape({"messages": [{"role": "user", "content": [{"type": 42}]}]}) is False
    assert is_compaction_shape({"messages": [[[{"weird": object()}]]]}) is False
