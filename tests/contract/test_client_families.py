import json
from pathlib import Path

import pytest

from free import wire_profile
from free.clients import capabilities, respond, tool_policy

FIXTURES = Path("free/clients/fixtures")
CLIENTS = [
    "claude-code",
    "codex",
    "kilo-code",
    "hermes-agent",
    "deepseek",
    "aider",
    "openai-sdk",
    "custom",
]


@pytest.mark.parametrize("client", CLIENTS)
def test_fixture_converts_to_a_chat_wire(client):
    body = json.loads((FIXTURES / client / "request.json").read_text(encoding="utf-8"))
    profile = capabilities.profile_for(client)
    chat = respond.convert_request(body, profile.family)
    assert "system" not in chat
    assert isinstance(chat.get("messages"), list) and chat["messages"]
    has_system = bool(body.get("system") or body.get("instructions"))
    if has_system:
        assert chat["messages"][0]["role"] == "system"
    assert "model" in chat
    if chat.get("stream"):
        assert "stream_options" in chat
    assert chat.get("tool_choice") != "none"
    for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
        value = chat.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            assert value <= 32000


@pytest.mark.parametrize("client", CLIENTS)
def test_tools_are_the_official_grid_plus_bounded_extras(client):
    body = json.loads((FIXTURES / client / "request.json").read_text(encoding="utf-8"))
    chat = respond.convert_request(body, capabilities.profile_for(client).family)
    tools = chat.get("tools") or []
    if not tools:
        return
    grid = tool_policy.build_grid(tools)
    names = [tool_policy.tool_name(t) for t in grid["tools"]]
    for official in tool_policy.official_grid():
        assert official in names
    assert not any(tool_policy.is_forbidden(n) for n in names)
    assert len(grid["extras"]) <= tool_policy.max_client_tools()


@pytest.mark.parametrize("client", CLIENTS)
def test_wire_fingerprint_is_stable_and_sensitive(client):
    body = json.loads((FIXTURES / client / "request.json").read_text(encoding="utf-8"))
    chat = respond.convert_request(body, capabilities.profile_for(client).family)
    a = wire_profile.fingerprint(chat, ua="ua", header_order=("a", "b"), tls_ja3_md5="ja3")
    b = wire_profile.fingerprint(chat, ua="ua", header_order=("a", "b"), tls_ja3_md5="ja3")
    chat2 = dict(chat)
    chat2["messages"] = list(chat.get("messages") or []) + [{"role": "user", "content": "x"}]
    c = wire_profile.fingerprint(chat2, ua="ua", header_order=("a", "b"), tls_ja3_md5="ja3")
    assert a == b and a != c


def test_unknown_client_is_custom_and_safe():
    profile = capabilities.profile_for("unknown-client")
    assert profile.id == "custom"
    assert profile.family == "openai_chat"


def test_anthropic_user_message_keeps_text_next_to_tool_results():
    """[fix 2026-11-21] Le `continue` apres les tool_result sautait le append
    final : le texte voisin du meme message user etait perdu."""
    from free.clients import family_anthropic

    body = {
        "model": "m",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "contexte utile"},
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": "ok",
                    },
                ],
            }
        ],
    }
    chat = family_anthropic.to_chat(body)
    pairs = [(m.get("role"), str(m.get("content"))) for m in chat["messages"]]
    assert ("tool", "ok") in pairs
    assert any(role == "user" and "contexte utile" in text for role, text in pairs)
