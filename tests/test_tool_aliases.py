"""[2026-11-12] Remap des noms d'outils officiels vers ceux du client.

Mesure : la jambe free injecte la grille officielle (``bash``, ``read``…)
pour franchir le gate. Le modèle suit ces noms, mais les clients valident
strictement les leurs :

* Claude Code → ``No such tool available: bash. call Bash instead`` ;
* Codex → ``unsupported call: bash`` / ``unsupported call: read`` ;
* opencode desktop → l'outil shell s'appelle ``shell``.
"""

import json

from app.protocol.chat_sse_to_anthropic import ChatSseToAnthropicState
from app.protocol.mapping import openai_to_anthropic, responses_stream_events
from app.protocol.tool_aliases import (
    adapt_chat_completion,
    adapt_tool_call,
    adapt_tool_name,
    build_tool_alias_map,
    tool_call_target,
)


def _claude_tools():
    return [
        {
            "name": "Bash",
            "description": "Run a shell command",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number"},
                },
                "required": ["command"],
            },
        },
        {
            "name": "Read",
            "input_schema": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
        {
            "name": "Glob",
            "input_schema": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        },
    ]


def _codex_tools():
    return [
        {
            "type": "function",
            "name": "exec_command",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string"},
                    "workdir": {"type": "string"},
                },
                "required": ["cmd"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "view_image",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
        {
            "type": "function",
            "name": "request_user_input",
            "parameters": {
                "type": "object",
                "properties": {"questions": {"type": "array"}},
                "required": ["questions"],
            },
        },
    ]


def _opencode_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "shell",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "filePath": {"type": "string"},
                        "offset": {"type": "number"},
                    },
                    "required": ["filePath"],
                },
            },
        },
    ]


# ─────────────────────── noms ───────────────────────


def test_bash_maps_to_claude_bash_case_insensitively():
    aliases = build_tool_alias_map(_claude_tools())
    assert adapt_tool_name("bash", aliases) == "Bash"


def test_bash_maps_to_opencode_shell():
    aliases = build_tool_alias_map(_opencode_tools())
    assert adapt_tool_name("bash", aliases) == "shell"


def test_bash_maps_to_codex_exec_command():
    aliases = build_tool_alias_map(_codex_tools())
    assert adapt_tool_name("bash", aliases) == "exec_command"


def test_unknown_name_is_left_untouched():
    aliases = build_tool_alias_map(_claude_tools())
    assert adapt_tool_name("custom_internal_tool", aliases) == "custom_internal_tool"


def test_no_aliases_is_a_noop():
    assert adapt_tool_name("bash", {}) == "bash"


# ─────────────────────── arguments ───────────────────────


def test_codex_command_key_becomes_cmd():
    aliases = build_tool_alias_map(_codex_tools())
    name, args = adapt_tool_call("bash", '{"command":"ls -la"}', aliases)
    assert name == "exec_command"
    assert json.loads(args) == {"cmd": "ls -la"}


def test_opencode_read_path_becomes_file_path_key():
    aliases = build_tool_alias_map(_opencode_tools())
    name, args = adapt_tool_call("read", '{"file_path":"a.py"}', aliases)
    assert name == "read"
    assert json.loads(args) == {"filePath": "a.py"}


def test_same_family_rename_does_not_filter_arguments():
    """``bash`` → ``Bash`` ne doit jamais perdre ``command``."""
    aliases = build_tool_alias_map(_claude_tools())
    name, args = adapt_tool_call("bash", '{"command":"pwd"}', aliases)
    assert name == "Bash"
    assert json.loads(args) == {"command": "pwd"}


def test_edit_keys_become_claude_snake_case():
    tools = [
        {
            "name": "Edit",
            "input_schema": {
                "type": "object",
                "properties": {
                    "file_path": {},
                    "old_string": {},
                    "new_string": {},
                    "replace_all": {},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        }
    ]
    aliases = build_tool_alias_map(tools)
    name, args = adapt_tool_call(
        "edit",
        '{"path":"a.py","oldString":"x","newString":"y","replaceAll":true}',
        aliases,
    )
    assert name == "Edit"
    assert json.loads(args) == {
        "file_path": "a.py",
        "old_string": "x",
        "new_string": "y",
        "replace_all": True,
    }


def test_grep_include_becomes_claude_glob():
    tools = [
        {
            "name": "Grep",
            "input_schema": {
                "type": "object",
                "properties": {"pattern": {}, "path": {}, "glob": {}},
                "required": ["pattern"],
            },
        }
    ]
    aliases = build_tool_alias_map(tools)
    name, args = adapt_tool_call(
        "grep", '{"pattern":"foo","path":".","include":"*.py"}', aliases
    )
    assert name == "Grep"
    assert json.loads(args) == {"pattern": "foo", "path": ".", "glob": "*.py"}


# ─────────────────────── repli shell ───────────────────────


def test_read_falls_back_to_codex_shell_when_no_read_tool():
    aliases = build_tool_alias_map(_codex_tools())
    name, args = adapt_tool_call(
        "read", '{"file_path":"a.py"}', aliases, shell_kind="powershell"
    )
    assert name == "exec_command"
    payload = json.loads(args)
    assert list(payload) == ["cmd"]
    assert "Get-Content" in payload["cmd"]
    assert "a.py" in payload["cmd"]


def test_glob_falls_back_to_posix_shell_when_no_glob_tool():
    aliases = build_tool_alias_map(_codex_tools())
    name, args = adapt_tool_call(
        "glob", '{"pattern":"*.py","path":"src"}', aliases, shell_kind="posix"
    )
    assert name == "exec_command"
    assert json.loads(args)["cmd"].startswith("find ")


def test_no_shell_fallback_when_client_declares_the_tool():
    """Claude Code déclare Glob : on garde Glob, jamais une commande shell."""
    aliases = build_tool_alias_map(_claude_tools())
    name, args = adapt_tool_call("glob", '{"pattern":"*.py"}', aliases)
    assert name == "Glob"
    assert json.loads(args) == {"pattern": "*.py"}


def test_codex_write_falls_back_to_shell():
    aliases = build_tool_alias_map(_codex_tools())
    name, args = adapt_tool_call(
        "write", '{"path":"a.py","content":"bonjour"}', aliases
    )
    assert name == "exec_command"
    command = json.loads(args)["cmd"]
    assert "WriteAllBytes" in command
    assert "a.py" in command


def test_codex_edit_falls_back_to_shell():
    aliases = build_tool_alias_map(_codex_tools())
    name, args = adapt_tool_call(
        "edit",
        '{"path":"a.py","oldString":"x","newString":"y"}',
        aliases,
    )
    assert name == "exec_command"
    command = json.loads(args)["cmd"]
    assert "ReadAllText" in command
    assert "oldString not found" in command


def test_codex_question_maps_to_request_user_input():
    aliases = build_tool_alias_map(_codex_tools())
    name, args = adapt_tool_call(
        "question", '{"questions":[{"question":"q"}]}', aliases
    )
    assert name == "request_user_input"
    assert json.loads(args)["questions"][0]["question"] == "q"


def _harness_tools():
    return [
        {
            "type": "function",
            "function": {
                "name": "pwsh",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {},
                        "description": {},
                        "timeoutMs": {},
                        "run_in_background": {},
                        "workdir": {},
                    },
                    "required": ["command", "description"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read",
                "parameters": {
                    "type": "object",
                    "properties": {"file_path": {}},
                    "required": ["file_path"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_fetch",
                "parameters": {
                    "type": "object",
                    "properties": {"url": {}},
                    "required": ["url"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "parameters": {
                    "type": "object",
                    "properties": {"queries": {}},
                    "required": ["queries"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "skill",
                "parameters": {
                    "type": "object",
                    "properties": {"name": {}},
                    "required": ["name"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "ask_user_question",
                "parameters": {
                    "type": "object",
                    "properties": {"questions": {}},
                    "required": ["questions"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "subagent",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "description": {},
                        "prompt": {},
                        "run_in_background": {},
                    },
                    "required": ["description", "prompt"],
                },
            },
        },
    ]


def test_harness_shell_maps_to_pwsh_and_adds_required_description():
    aliases = build_tool_alias_map(_harness_tools())
    name, args = adapt_tool_call(
        "shell",
        '{"command":"Get-ChildItem","timeout":5000,"background":true}',
        aliases,
    )
    assert name == "pwsh"
    payload = json.loads(args)
    assert payload["command"] == "Get-ChildItem"
    assert payload["description"]
    assert payload["timeoutMs"] == 5000
    assert payload["run_in_background"] is True
    assert "timeout" not in payload
    assert "background" not in payload


def test_harness_web_search_query_becomes_queries_list():
    aliases = build_tool_alias_map(_harness_tools())
    name, args = adapt_tool_call("websearch", '{"query":"x"}', aliases)
    assert name == "web_search"
    assert json.loads(args) == {"queries": ["x"]}


def test_harness_webfetch_drops_unknown_client_fields():
    aliases = build_tool_alias_map(_harness_tools())
    name, args = adapt_tool_call(
        "webfetch", '{"url":"https://x","format":"text","timeout":10}', aliases
    )
    assert name == "web_fetch"
    assert json.loads(args) == {"url": "https://x"}


def test_harness_skill_id_becomes_name():
    aliases = build_tool_alias_map(_harness_tools())
    name, args = adapt_tool_call("skill", '{"id":"s"}', aliases)
    assert name == "skill"
    assert json.loads(args) == {"name": "s"}


def test_harness_question_maps_to_ask_user_question():
    aliases = build_tool_alias_map(_harness_tools())
    name, args = adapt_tool_call(
        "question", '{"questions":[{"question":"q"}]}', aliases
    )
    assert name == "ask_user_question"
    assert json.loads(args)["questions"][0]["question"] == "q"


def test_harness_subagent_background_becomes_run_in_background():
    aliases = build_tool_alias_map(_harness_tools())
    name, args = adapt_tool_call(
        "subagent",
        '{"agent":"a","description":"d","prompt":"p","background":true}',
        aliases,
    )
    assert name == "subagent"
    assert json.loads(args) == {
        "description": "d",
        "prompt": "p",
        "run_in_background": True,
    }


# ─────────────────────── intégrations ───────────────────────


def test_openai_to_anthropic_adapts_tool_use_name_and_input():
    resp = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "bash",
                                "arguments": '{"command":"ls"}',
                            },
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2},
    }
    out = openai_to_anthropic(
        resp, "claude", tool_aliases=build_tool_alias_map(_claude_tools())
    )
    block = next(b for b in out["content"] if b["type"] == "tool_use")
    assert block["name"] == "Bash"
    assert block["input"] == {"command": "ls"}


def test_responses_stream_events_adapts_function_call_items():
    response = {
        "id": "resp_1",
        "object": "response",
        "status": "completed",
        "model": "m",
        "output": [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "bash",
                "arguments": '{"command":"ls"}',
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
    }
    events = responses_stream_events(
        response,
        "m",
        tool_aliases=build_tool_alias_map(_claude_tools()),
    )
    added = next(e for e in events if e["type"] == "response.output_item.added")
    assert added["item"]["name"] == "Bash"
    completed = events[-1]
    assert completed["type"] == "response.completed"
    item = completed["response"]["output"][0]
    assert item["name"] == "Bash"


def test_chat_sse_to_anthropic_adapts_tool_use_name():
    state = ChatSseToAnthropicState(
        model="claude", tool_aliases=build_tool_alias_map(_claude_tools())
    )
    chunk = {
        "id": "chatcmpl_1",
        "object": "chat.completion.chunk",
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {
                                "name": "bash",
                                "arguments": '{"command":"ls"}',
                            },
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }
    emitted = "".join(state.feed_chunk(chunk))
    assert '"name":"Bash"' in emitted
    assert '"name":"bash"' not in emitted


def test_adapt_chat_completion_reports_change_and_rewrites():
    payload = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "bash",
                                "arguments": '{"command":"ls"}',
                            }
                        }
                    ]
                }
            }
        ]
    }
    changed = adapt_chat_completion(
        payload, build_tool_alias_map(_opencode_tools())
    )
    assert changed is True
    fn = payload["choices"][0]["message"]["tool_calls"][0]["function"]
    assert fn["name"] == "shell"
    assert json.loads(fn["arguments"]) == {"command": "ls"}


def test_adapt_chat_completion_without_tools_is_a_noop():
    payload = {"choices": [{"message": {"content": "hi"}}]}
    assert adapt_chat_completion(payload, {}) is False


# ─────────────── webfetch : famille absente du client ───────────────


def test_opencode_execute_is_not_a_shell_target():
    """``execute`` est un runtime JavaScript, pas un shell : jamais choisi."""
    tools = _opencode_tools() + [
        {
            "type": "function",
            "function": {
                "name": "execute",
                "parameters": {
                    "type": "object",
                    "properties": {"code": {"type": "string"}},
                    "required": ["code"],
                },
            },
        }
    ]
    aliases = build_tool_alias_map(tools)
    assert adapt_tool_name("bash", aliases) == "shell"
    name, args = adapt_tool_call("bash", '{"command":"ls"}', aliases)
    assert name == "shell"
    assert json.loads(args) == {"command": "ls"}


def test_task_maps_to_claude_agent():
    tools = _claude_tools() + [
        {
            "name": "Agent",
            "input_schema": {
                "type": "object",
                "properties": {"description": {}, "prompt": {}, "subagent_type": {}},
                "required": ["description", "prompt"],
            },
        }
    ]
    aliases = build_tool_alias_map(tools)
    assert adapt_tool_name("task", aliases) == "Agent"


def test_webfetch_falls_back_to_claude_bash_with_posix_curl():
    aliases = build_tool_alias_map(_claude_tools())
    assert tool_call_target("webfetch", aliases) == "Bash"
    name, args = adapt_tool_call(
        "webfetch", '{"url":"https://example.com","format":"text"}', aliases
    )
    assert name == "Bash"
    payload = json.loads(args)
    assert payload["command"].startswith("curl -sL")
    assert "https://example.com" in payload["command"]
    assert "format" not in payload


def test_webfetch_falls_back_to_powershell_when_that_is_the_shell():
    tools = [
        {
            "name": "PowerShell",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        }
    ]
    aliases = build_tool_alias_map(tools)
    name, args = adapt_tool_call("webfetch", '{"url":"https://example.com"}', aliases)
    assert name == "PowerShell"
    assert "Invoke-WebRequest" in json.loads(args)["command"]


def test_stream_defers_webfetch_args_and_emits_shell_command():
    state = ChatSseToAnthropicState(
        tool_aliases=build_tool_alias_map(_claude_tools()),
        shell_kind="posix",
    )
    first = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call_1",
                            "function": {
                                "name": "webfetch",
                                "arguments": '{"url":"https://exa',
                            },
                        }
                    ]
                }
            }
        ]
    }
    second = {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "function": {"arguments": 'mple.com"}'},
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    emitted = "".join(state.feed_chunk(first))
    assert '"name":"Bash"' in emitted
    assert "input_json_delta" not in emitted
    emitted += "".join(state.feed_chunk(second))
    emitted += "".join(state.finalize_events())
    assert "curl -sL" in emitted
    assert "https://example.com" in emitted


def test_openai_to_anthropic_webfetch_falls_back_to_bash():
    resp = {
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "webfetch",
                                "arguments": '{"url":"https://example.com"}',
                            },
                        }
                    ]
                },
                "finish_reason": "tool_calls",
            }
        ]
    }
    out = openai_to_anthropic(
        resp, "m", tool_aliases=build_tool_alias_map(_claude_tools())
    )
    block = out["content"][0]
    assert block["type"] == "tool_use"
    assert block["name"] == "Bash"
    assert "curl" in block["input"]["command"]
