"""Remapping des chemins factices et alias de tool_calls (impersonation free)."""

from __future__ import annotations

import json

from free.station_identity import (
    StationIdentity,
    template_official_body,
    translate_paths,
    workspace_env_cwd,
    workspace_root_from_messages,
)
from opencode import _ChatToolCallHealer, _heal_tool_call_paths
from streaming.tool_path_healing import ToolPathHealingMiddleware
from streaming.tool_paths import (
    CLIENT_TOOL_NAMES,
    expected_path_key,
    heal_anthropic_payload,
    heal_openai_payload,
    heal_sse_data_line,
    heal_tool_arguments,
    heal_tool_name,
    set_client_tool_names,
    set_client_tool_schemas,
)

REAL = r"C:\Users\julie\Downloads\qwenproxy"
FAKE = "/workspace/oc-warp-ipv6-2"
MAP = {FAKE: REAL}


def _edit_tool() -> dict:
    return {
        "type": "function",
        "function": {
            "name": "edit",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "oldString": {"type": "string"},
                    "newString": {"type": "string"},
                },
                "required": ["path", "oldString", "newString"],
            },
        },
    }


def test_workspace_root_from_messages_reads_real_env() -> None:
    messages = [
        {
            "role": "system",
            "content": (
                "You are an AI agent running in OpenCode.\n"
                f"<env>\n  Working directory: {REAL}\n</env>"
            ),
        }
    ]
    assert workspace_root_from_messages(messages) == REAL


def test_translate_paths_keeps_json_valid() -> None:
    args = json.dumps({"path": f"{FAKE}/.env"}, ensure_ascii=False)
    healed = translate_paths(args, MAP, json_escape=True)
    assert FAKE not in healed
    assert json.loads(healed)["path"] == REAL + r"\.env"


def test_template_official_body_hides_real_path() -> None:
    identity = StationIdentity("warp-ipv6-2", b"x" * 32)
    body = {
        "messages": [
            {
                "role": "system",
                "content": f"<env>\n  Working directory: {REAL}\n</env>",
            }
        ]
    }
    out = template_official_body(body, identity)
    content = out["messages"][0]["content"]
    assert REAL not in content
    assert FAKE in content


def test_workspace_env_cwd_is_stable_and_workspace_specific(tmp_path) -> None:
    identity = StationIdentity("warp-ipv6-2", b"x" * 32)
    first = workspace_env_cwd(identity, str(tmp_path / "first"))
    second = workspace_env_cwd(identity, str(tmp_path / "second"))
    assert first.startswith(FAKE)
    assert second.startswith(FAKE)
    assert first != second
    assert first == workspace_env_cwd(identity, str(tmp_path / "first"))


def test_heal_tool_call_paths_renames_file_path_alias() -> None:
    args = json.dumps(
        {
            "filePath": f"{FAKE}/.env",
            "oldString": "A",
            "newString": "B",
        },
        ensure_ascii=False,
    )
    healed = _heal_tool_call_paths("edit", args, MAP)
    parsed = json.loads(healed)
    assert "filePath" not in parsed
    assert parsed["path"] == REAL + r"\.env"


def test_healer_emits_real_path_and_path_key() -> None:
    healer = _ChatToolCallHealer([_edit_tool()], path_map=MAP)
    chunk = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "edit",
                                "arguments": json.dumps(
                                    {
                                        "filePath": f"{FAKE}/.env",
                                        "oldString": "A",
                                        "newString": "B",
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ]
                },
                "finish_reason": None,
            }
        ]
    }
    out = healer.feed(chunk)
    assert out
    emitted = out[-1]["choices"][0]["delta"]["tool_calls"][0]["function"]
    args = json.loads(emitted["arguments"])
    assert args["path"] == REAL + r"\.env"
    assert "filePath" not in args


def test_heal_openai_payload_renames_alias() -> None:
    payload = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {
                                "name": "edit",
                                "arguments": '{"filePath":"x.txt","oldString":"a","newString":"b"}',
                            },
                        }
                    ]
                },
            }
        ]
    }
    assert heal_openai_payload(payload)
    args = payload["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args)["path"] == "x.txt"


def test_heal_sse_data_line_keeps_non_data_lines() -> None:
    assert heal_sse_data_line(b"event: message") == b"event: message"
    assert heal_sse_data_line(b"data: [DONE]") == b"data: [DONE]"


def test_tool_path_middleware_heals_split_sse_lines() -> None:
    async def app(scope, receive, send):
        line = (
            b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
            b'"function":{"name":"read","arguments":"{\\"filePath\\":\\"a.txt\\"}"}}]}}]}\n\n'
        )
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"text/event-stream")],
            }
        )
        await send({"type": "http.response.body", "body": line[:40], "more_body": True})
        await send({"type": "http.response.body", "body": line[40:], "more_body": False})

    middleware = ToolPathHealingMiddleware(app)
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    import asyncio

    asyncio.run(
        middleware(
            {"type": "http", "method": "POST", "path": "/v1/chat/completions"},
            receive,
            send,
        )
    )
    body = b"".join(
        m.get("body") or b"" for m in sent if m.get("type") == "http.response.body"
    )
    data = body.split(b"data: ", 1)[1].split(b"\n", 1)[0]
    payload = json.loads(data)
    args = payload["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
    assert json.loads(args)["path"] == "a.txt"
    assert "filePath" not in args


def test_heal_tool_arguments_uses_client_file_path_schema() -> None:
    set_client_tool_schemas(
        [
            {
                "type": "function",
                "function": {
                    "name": "edit",
                    "parameters": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"],
                    },
                },
            }
        ]
    )
    try:
        healed = heal_tool_arguments(
            "edit", '{"path":"a.txt","oldString":"a","newString":"b"}'
        )
        parsed = json.loads(healed)
        assert parsed["file_path"] == "a.txt"
        assert "path" not in parsed
        # Déjà au bon nom : ne rien casser.
        same = '{"file_path":"a.txt","oldString":"a","newString":"b"}'
        assert heal_tool_arguments("edit", same) == same
    finally:
        set_client_tool_schemas([])


def test_heal_anthropic_payload_uses_tool_name_state() -> None:
    set_client_tool_schemas(
        [
            {
                "type": "function",
                "function": {
                    "name": "write",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            }
        ]
    )
    try:
        names: dict[int, str] = {}
        start = {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "name": "write", "input": {}},
        }
        heal_anthropic_payload(start, names)
        assert names[0] == "write"
        delta = {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "input_json_delta",
                "partial_json": '{"filePath":"x.txt",',
            },
        }
        assert heal_anthropic_payload(delta, names)
        assert '"path":"x.txt"' in delta["delta"]["partial_json"]
    finally:
        set_client_tool_schemas([])


def test_heal_openai_payload_uses_client_file_path_schema() -> None:
    set_client_tool_schemas(
        [
            {
                "type": "function",
                "function": {
                    "name": "read",
                    "parameters": {
                        "type": "object",
                        "properties": {"file_path": {"type": "string"}},
                        "required": ["file_path"],
                    },
                },
            }
        ]
    )
    try:
        payload = {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "read",
                                    "arguments": '{"path":"a.txt"}',
                                },
                            }
                        ]
                    },
                }
            ]
        }
        assert heal_openai_payload(payload)
        args = payload["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
        assert json.loads(args)["file_path"] == "a.txt"
    finally:
        set_client_tool_schemas([])


def test_heal_tool_name_maps_official_placeholder() -> None:
    token = CLIENT_TOOL_NAMES.set(frozenset({"shell", "read", "edit"}))
    try:
        assert heal_tool_name("bash") == "shell"
        assert heal_tool_name("read") == "read"
        assert heal_tool_name("unknown") == "unknown"
    finally:
        CLIENT_TOOL_NAMES.reset(token)


def test_heal_tool_name_restores_client_capitalisation() -> None:
    """Claude Code déclare ``Read`` ; l'amont officiel émet ``read``."""
    token = CLIENT_TOOL_NAMES.set(frozenset({"Bash", "Read", "Glob", "Write"}))
    try:
        assert heal_tool_name("read") == "Read"
        assert heal_tool_name("glob") == "Glob"
        assert heal_tool_name("write") == "Write"
        assert heal_tool_name("bash") == "Bash"
    finally:
        CLIENT_TOOL_NAMES.reset(token)


def test_client_tool_schemas_read_anthropic_input_schema() -> None:
    """Un client Anthropic porte ``input_schema``, pas ``parameters``."""
    set_client_tool_schemas(
        [
            {
                "name": "Read",
                "input_schema": {
                    "type": "object",
                    "properties": {"file_path": {"type": "string"}},
                    "required": ["file_path"],
                },
            }
        ]
    )
    try:
        assert expected_path_key("Read") == "file_path"
    finally:
        set_client_tool_schemas([])


def test_heal_openai_payload_renames_bash_to_shell() -> None:
    set_client_tool_names(["shell", "read", "edit"])
    try:
        payload = {
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "name": "bash",
                                    "arguments": '{"command":"git status"}',
                                },
                            }
                        ]
                    },
                }
            ]
        }
        assert heal_openai_payload(payload)
        fn = payload["choices"][0]["delta"]["tool_calls"][0]["function"]
        assert fn["name"] == "shell"
    finally:
        set_client_tool_names([])

