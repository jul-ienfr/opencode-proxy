"""Famille Anthropic Messages — 023 (T048).

Conversion requête Anthropic → corps chat officiel, et contrat des événements
SSE attendus. Aucun I/O, aucun état global.
"""

from __future__ import annotations

import copy
from typing import Any

from free.clients import tool_policy

FAMILY = "anthropic_messages"
MAX_OUTPUT_TOKENS = 32000

SSE_EVENTS = (
    "message_start",
    "content_block_start",
    "content_block_delta",
    "content_block_stop",
    "message_delta",
    "message_stop",
)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "image":
                    parts.append("[image]")
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""


def _system_text(system: Any) -> str:
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        return "\n\n".join(
            str(b.get("text") or "")
            for b in system
            if isinstance(b, dict) and b.get("text")
        )
    return ""


def _convert_tool(tool: Any) -> dict[str, Any] | None:
    if not isinstance(tool, dict):
        return None
    name = str(tool.get("name") or "").strip()
    if not name:
        return None
    return {
        "type": "function",
        "function": {
            "name": tool_policy.map_tool_name(name),
            "description": str(tool.get("description") or ""),
            "parameters": copy.deepcopy(
                tool.get("input_schema") or {"type": "object", "properties": {}}
            ),
        },
    }


def to_chat(body: dict[str, Any]) -> dict[str, Any]:
    """Construit un corps chat/completions depuis Anthropic Messages."""
    if not isinstance(body, dict):
        return body
    messages: list[dict[str, Any]] = []
    system_text = _system_text(body.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "user")
        content = msg.get("content")
        if role == "assistant":
            calls = []
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    calls.append(
                        {
                            "id": str(block.get("id") or ""),
                            "type": "function",
                            "function": {
                                "name": tool_policy.map_tool_name(str(block.get("name") or "")),
                                "arguments": _json(block.get("input") or {}),
                            },
                        }
                    )
            out: dict[str, Any] = {"role": "assistant", "content": _text_of(content)}
            if calls:
                out["tool_calls"] = calls
            messages.append(out)
            continue
        if role == "user" and isinstance(content, list):
            tool_results = [
                b for b in content
                if isinstance(b, dict) and b.get("type") == "tool_result"
            ]
            if tool_results:
                for block in tool_results:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(block.get("tool_use_id") or ""),
                            "content": _text_of(block.get("content")),
                        }
                    )
                # [fix 2026-11-21] Le `continue` sautait le append final : tout
                # bloc voisin non-tool_result (texte, image) du même message
                # user était perdu silencieusement. On réinjecte les blocs
                # restants dans un message user séparé, APRÈS les tool results.
                rest = [
                    b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "tool_result")
                ]
                if rest:
                    messages.append({"role": "user", "content": _text_of(rest)})
                continue
        messages.append({"role": role, "content": _text_of(content)})

    tools = [t for t in (_convert_tool(x) for x in (body.get("tools") or [])) if t]
    chat: dict[str, Any] = {
        "model": body.get("model"),
        "messages": messages,
        "tools": tools,
        "stream": bool(body.get("stream")),
    }
    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, int) and not isinstance(max_tokens, bool):
        chat["max_completion_tokens"] = min(int(max_tokens), MAX_OUTPUT_TOKENS)
    if chat["stream"]:
        chat["stream_options"] = {"include_usage": True}
    return chat


def _json(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def error_event(message: str, *, error_type: str = "api_error") -> dict[str, Any]:
    return {"type": "error", "error": {"type": error_type, "message": message}}


def sse_contract() -> tuple[str, ...]:
    return SSE_EVENTS


def count_tokens(
    body: dict[str, Any],
    *,
    estimate_fn: Any | None = None,
) -> int:
    """Compte les tokens pour une requête Anthropic (messages + system + tools)."""
    if estimate_fn is None:
        from protocol.tokens import estimate_input_tokens as estimate_fn
    return estimate_fn(
        {
            "messages": body.get("messages") or [],
            "tools": body.get("tools") or [],
            "system": body.get("system") or None,
        }
    )
